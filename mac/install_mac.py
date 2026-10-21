"""dsh-pet-ascii 的 macOS 安装器。已在 macOS 26.4 / arm64、上游 v4.3.4 上验证安装与重新签名；
完整验证范围见 mac/README.md。给你自己装好的 dsh-pet-standalone-webm-chat.app 装上 / 卸下扩展。

做的事：
  1. 把 .app 里的主程序和它的签名封条（_CodeSignature/CodeResources）备份到
     ~/Library/Application Support/dsh-pet-ascii/backup/
  2. 把主程序里 PyInstaller 归档的入口脚本 pet_entry 换成多了几行的“包装版”：
     启动时先把 ~/Library/Application Support/dsh-pet-ascii 加进导入路径，`import pet_memory.bootstrap` 并安装，
     出错就跳过，然后照原样运行原版入口。程序的其余部分一个字节都不变。
     （Mac 上的程序带签名，签名之外 PyInstaller 还把归档“挂”在 __LINKEDIT 段里，所以要先去掉旧签名、
     改完再把 __LINKEDIT 和字符串表的长度改对——和 PyInstaller 打包时做的一样——最后用系统自带的
     codesign 重新做一次本地（ad-hoc）签名。上游的 Mac 版本来就是 ad-hoc 签名，所以信任等级不变。）
  3. 把 pet_memory/ 和 ascii_pet/ 复制到 ~/Library/Application Support/dsh-pet-ascii/（不放进 .app 里面）

用法（在终端里，先 cd 到本文件夹）：
    python3 install_mac.py               安装或更新
    python3 install_mac.py --uninstall   卸载：还原原版程序和签名封条，删除扩展文件夹
    python3 install_mac.py --status      只查看当前状态
    python3 install_mac.py --dry-run     只检查、不写入
  可选：--app <.app 的路径>  --yes（不询问）  --remove-quarantine（去掉下载隔离标记，见 mac/README.md）

需要 python3（任意 3.8 以上，只用标准库；没有的话在终端输入 python3，系统会提示安装“命令行开发者工具”）。
运行前请先退出桌宠。你的记忆、设置、房间都在 ~/Library/Application Support/dsh-pet-standalone-webm-chat，安装器不碰。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import plistlib
import shutil
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import install as win  # noqa: E402  与 Windows 安装器共用 CArchive 的读写和自检

APP_NAME = "dsh-pet-standalone-webm-chat"
APP_BUNDLE = APP_NAME + ".app"
EXE_REL = Path("Contents") / "MacOS" / APP_NAME
SEAL_REL = Path("Contents") / "_CodeSignature" / "CodeResources"
ADDON_DIR = Path.home() / "Library" / "Application Support" / "dsh-pet-ascii"
DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
TESTED_UPSTREAM_MAC = "4.3.4"

MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGICS = (0xCAFEBABE, 0xBEBAFECA, 0xCAFEBABF, 0xBFBAFECA)
CPU_NAMES = {0x0100000C: "arm64", 0x01000007: "x86_64"}
LC_SEGMENT_64, LC_SYMTAB, LC_CODE_SIGNATURE = 0x19, 0x2, 0x1D
PAGE = 0x4000                                   # arm64 的页大小（x86_64 是 0x1000，按 0x4000 对齐也满足）

# 由 patch_src/build_entry_blobs.py 用 Python 3.11 编译 patch_src/pet_entry_wrapper_mac.py 生成（可用 --check 核对）
# sha256 9844fbc1a5eca8529e9bcfc981703a8c9250b4b8c158a0bc6a549cdd388104b8
MAC_WRAPPER_ENTRY_B64 = (
    "4wAAAAAAAAAAAAAAAAYAAAAAAAAA87ABAACXAGQAWgBkAWQCbAFaAQkAZAFkAmwCWgNlA2oEAAAA"
    "AAAAAACgBQAAAAAAAAAAAAAAAAAAAAAAAAAAZQNqBAAAAAAAAAAAoAYAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAGQDpgEAAKsBAAAAAAAAAABkBGQFZAamBAAAqwQAAAAAAAAAAFoHZQNqBAAAAAAAAAAAoAgA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAGUDagQAAAAAAAAAAKAFAAAAAAAAAAAAAAAAAAAAAAAAAABlB2QH"
    "pgIAAKsCAAAAAAAAAACmAQAAqwEAAAAAAAAAAHIkZQdlAWoEAAAAAAAAAAB2AXIbZQFqBAAAAAAA"
    "AAAAoAkAAAAAAAAAAAAAAAAAAAAAAAAAAGQBZQemAgAAqwIAAAAAAAAAAAEAZAFkAmwKbQtaDAEA"
    "AgBlDGoNAAAAAAAAAACmAAAAqwAAAAAAAAAAAAEAbgsjAGUOJAByAwEAWQBuBHcAeANZAHcBZAFk"
    "AmwPWhACAGURAgBlEGoSAAAAAAAAAABkCKYBAACrAQAAAAAAAAAApgEAAKsBAAAAAAAAAAABAGQC"
    "UwApCXXVAQAAClB5SW5zdGFsbGVyIOS4u+WFpeWPo++8iG1hY09TIOeJiO+8jOW3suWKoOijheaJ"
    "qeWxle+8jOmAmueUqOWMheijheeJiO+8jOeUsSBtYWMvaW5zdGFsbF9tYWMucHkg55Sf5oiQ77yJ"
    "44CCCgrmianlsZXoo4XlnKggfi9MaWJyYXJ5L0FwcGxpY2F0aW9uIFN1cHBvcnQvZHNoLXBldC1h"
    "c2Npae+8iOS4jeaUvui/myAuYXBwIOmHjOmdou+8ie+8muWFiOaKiui/meS4quaWh+S7tuWkueWK"
    "oOi/m+WvvOWFpei3r+W+hO+8jArlsJ3or5Xlronoo4UgcGV0X21lbW9yeSDph4znmoTooaXkuIHv"
    "vIzlho3ljp/moLfov5DooYzljp/niYjlhaXlj6PvvIjljp/niYjlhaXlj6PnmoTlrZfoioLnoIHl"
    "sLHlrZjlnKjmnKzmlofku7bnmoTluLjph4/ph4zvvInjgIIK5omp5bGV57y65aSx5oiW5Ye66ZSZ"
    "5pe26Z2Z6buY6Lez6L+H77yM5qGM5a6g5oyJ5Y6f5qC36L+Q6KGM44CC5Yig6Zmk6YKj5Liq5paH"
    "5Lu25aS55Y2z5Y+v5YGc55So44CCCukAAAAATvoBftoHTGlicmFyeXoTQXBwbGljYXRpb24gU3Vw"
    "cG9ydHoNZHNoLXBldC1hc2NpadoKcGV0X21lbW9yeXMwAAAAQEBEU0hfUEVUX0FTQ0lJX09SSUdJ"
    "TkFMX1BFVF9FTlRSWV9QTEFDRUhPTERFUkBAKRPaB19fZG9jX1/aA3N5c9oCb3PaB19wZXRfb3Pa"
    "BHBhdGjaBGpvaW7aCmV4cGFuZHVzZXLaC19wZXRfYWRkb25z2gVpc2RpctoGaW5zZXJ02hRwZXRf"
    "bWVtb3J5LmJvb3RzdHJhcNoJYm9vdHN0cmFw2hVfcGV0X21lbW9yeV9ib290c3RyYXDaB2luc3Rh"
    "bGzaCUV4Y2VwdGlvbtoHbWFyc2hhbNoMX3BldF9tYXJzaGFs2gRleGVj2gVsb2Fkc6kA8wAAAAD6"
    "DHBldF9lbnRyeS5wefoIPG1vZHVsZT5yHAAAAAEAAABzIAEAAPADAQEB8AIGAQTwAAYBBPAOAAEL"
    "gAqACoAK8AQKAQnYBBjQBBjQBBjQBBjgEhmULNcSI9ISI6BHpEzXJDvSJDu4Q9EkQNQkQMAp0E1i"
    "0GRz0RJ01BJ0gEvYBw6EfNcHGdIHGZgnnCzXGivSGiuoS7gc0RpG1BpG0QdH1AdH8AABBSjIS9Bf"
    "YtRfZ9BMZ9BMZ9gIC4wIjw+KD5gBmDvRCCfUCCfQCCfYBDjQBDjQBDjQBDjQBDjQBDjgBCHQBBnU"
    "BCHRBCPUBCPQBCPQBCP42AcQ8AABAQnwAAEBCfAAAQEJ2AQIgETwAwEBCfj4+PAGAAEf0AAe0AAe"
    "0AAe4AAEgATQBReAXNQFF9AYS9EFTNQFTNEATdQATdAATdAATdAATXMSAAAAiEIlQi4Awi4FQjYD"
    "wjUBQjYD"
)


# ---------------------------------------------------------------- Mach-O
class MachO:
    """只认 64 位 Mach-O（Apple 芯片的 arm64 或 Intel 的 x86_64），读出要改的几条加载命令。"""

    def __init__(self, data: bytes, label: str = "主程序"):
        if len(data) < 32:
            raise win.PatchError(f"{label} 太小，不是程序")
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic in FAT_MAGICS or struct.unpack_from(">I", data, 0)[0] in FAT_MAGICS:
            raise win.PatchError(f"{label} 是通用二进制（多种芯片合在一起），本安装器只支持单一架构的程序")
        if magic != MH_MAGIC_64:
            raise win.PatchError(f"{label} 不是 64 位 Mach-O 程序")
        self.data = data
        self.cputype = struct.unpack_from("<i", data, 4)[0] & 0xFFFFFFFF
        self.ncmds, self.sizeofcmds = struct.unpack_from("<II", data, 16)
        self.cmds = []
        off = 32
        for _ in range(self.ncmds):
            cmd, size = struct.unpack_from("<II", data, off)
            if size < 8 or off + size > 32 + self.sizeofcmds:
                raise win.PatchError(f"{label} 的加载命令损坏")
            self.cmds.append((off, cmd, size))
            off += size
        self.linkedit = self.symtab = self.codesig = None
        for off, cmd, size in self.cmds:
            if cmd == LC_SEGMENT_64 and data[off + 8:off + 24].rstrip(b"\0") == b"__LINKEDIT":
                vmaddr, vmsize, fileoff, filesize = struct.unpack_from("<QQQQ", data, off + 24)
                self.linkedit = (off, vmaddr, vmsize, fileoff, filesize)
            elif cmd == LC_SYMTAB:
                self.symtab = (off,) + struct.unpack_from("<IIII", data, off + 8)
            elif cmd == LC_CODE_SIGNATURE:
                self.codesig = (off,) + struct.unpack_from("<II", data, off + 8)
        if self.linkedit is None or self.symtab is None:
            raise win.PatchError(f"{label} 缺少 __LINKEDIT 段或符号表，不像 PyInstaller 打包的程序")

    @property
    def arch(self) -> str:
        return CPU_NAMES.get(self.cputype, hex(self.cputype))


def strip_signature(data: bytes) -> bytes:
    """去掉代码签名（等同于 `codesign --remove-signature`）：删掉签名数据和 LC_CODE_SIGNATURE 这条加载命令。"""
    m = MachO(data)
    if m.codesig is None:
        return data
    off, dataoff, _ = m.codesig
    size = 16
    b = bytearray(data[:dataoff])
    end = 32 + m.sizeofcmds
    b[off:end - size] = b[off + size:end]                       # 后面的加载命令往前挪，空出来的补 0
    b[end - size:end] = bytes(size)
    struct.pack_into("<II", b, 16, m.ncmds - 1, m.sizeofcmds - size)
    return bytes(b)


def fix_linkedit(data: bytes) -> bytes:
    """让 __LINKEDIT 段和字符串表罩住末尾的 CArchive（PyInstaller 打包 Mac 程序时做的同一件事，codesign 才肯签）。"""
    m = MachO(data)
    n = len(data)
    b = bytearray(data)
    off, _, _, fileoff, _ = m.linkedit
    filesize = n - fileoff
    vmsize = (filesize + PAGE - 1) // PAGE * PAGE
    struct.pack_into("<QQQ", b, off + 32, vmsize, fileoff, filesize)
    soff, _, _, stroff, _ = m.symtab
    struct.pack_into("<I", b, soff + 20, n - stroff)
    return bytes(b)


def mac_wrapper_entry(original: bytes) -> bytes:
    blob = base64.b64decode("".join(MAC_WRAPPER_ENTRY_B64))
    needle = struct.pack("<I", len(win.PLACEHOLDER)) + win.PLACEHOLDER
    i = blob.find(needle)
    if i <= 0 or blob.count(needle) != 1 or blob[i - 1] not in (0x73, 0xF3):
        raise win.PatchError("内嵌的 Mac 入口模板损坏")
    return blob[:i] + struct.pack("<I", len(original)) + original + blob[i + len(needle):]


def archive_of(data: bytes, label: str):
    """主程序里的 CArchive（签名去掉后，归档就在文件末尾）。"""
    body = strip_signature(data)
    pos = body.rfind(win.MAGIC)
    if pos < 0:
        raise win.PatchError(f"{label} 不是 PyInstaller 程序（找不到 CArchive 标记）")
    return body, win.Archive(body[:pos + win.COOKIE_SIZE], label)


def patch_executable(data: bytes) -> tuple:
    """返回（改好、还没签名的主程序，说明）。不写任何文件。"""
    m = MachO(data, "原版主程序")
    body, arc = archive_of(data, "原版主程序")
    if arc.pyver != win.PY_VERSION:
        raise win.PatchError(f"这个版本的桌宠用的是 Python {arc.pyver // 100}.{arc.pyver % 100}，"
                             "本安装器内嵌的入口只适用于 Python 3.11，请等待扩展更新")
    e = arc.entry(win.ENTRY_NAME)
    if e["typ"] != b"s":
        raise win.PatchError("pet_entry 不是入口脚本类型，跳过")
    original = arc.content(win.ENTRY_NAME)
    if win.is_patched(original):
        raise win.PatchError("这个程序已经装过扩展了")
    if not original[:1] or original[0] & 0x7F != 0x63:
        raise win.PatchError("原版入口不是 Python 字节码，无法包装")
    mb = MachO(body)
    soff, _, _, stroff, strsize = mb.symtab
    if stroff + strsize < arc.start:
        raise win.PatchError("字符串表没有罩住归档：这不是 PyInstaller 的 Mac 程序布局，为安全起见不处理")
    new_entry = mac_wrapper_entry(original)
    out = fix_linkedit(arc.rebuild({win.ENTRY_NAME: new_entry}))
    # 自检：Mach-O 能解析、没有旧签名、__LINKEDIT 和字符串表正好到文件末尾；归档能解析，入口是新的，其余条目逐字节相同
    mo = MachO(out, "新主程序")
    off, _, vmsize, fileoff, filesize = mo.linkedit
    _, _, _, stroff2, strsize2 = mo.symtab
    if mo.codesig is not None or fileoff + filesize != len(out) or stroff2 + strsize2 != len(out) or vmsize < filesize:
        raise win.PatchError("自检失败：新主程序的段信息不对")
    chk = win.Archive(out, "新主程序")
    if chk.content(win.ENTRY_NAME) != new_entry:
        raise win.PatchError("自检失败：新入口不对")
    if [x["name"] for x in chk.entries] != [x["name"] for x in arc.entries]:
        raise win.PatchError("自检失败：条目列表变了")
    for a, b in zip(arc.entries, chk.entries):
        if a["name"] != win.ENTRY_NAME and arc.raw(a) != chk.raw(b):
            raise win.PatchError(f"自检失败：{a['name']} 变了")
    cmds_end = 32 + mo.sizeofcmds + 16                         # 加载命令区之后、归档之前的内容（代码、符号）不能变
    if out[cmds_end:arc.start] != body[cmds_end:arc.start]:
        raise win.PatchError("自检失败：程序本体变了")
    return out, f"包装入口（{m.arch}，上游本身是 ad-hoc 签名；改完重新做 ad-hoc 签名）"


def is_patched_exe(data: bytes) -> bool:
    _, arc = archive_of(data, "主程序")
    return win.is_patched(arc.content(win.ENTRY_NAME))


def upstream_version(data: bytes) -> str:
    """从主程序里的 PYZ 读出上游版本（pet/__init__ 里的 __version__；读不到返回“未知”）。"""
    import re

    try:
        _, arc = archive_of(data, "主程序")
        pyz = arc.content("PYZ.pyz")
        import marshal

        toc_pos = struct.unpack("!i", pyz[8:12])[0]
        try:
            toc = marshal.loads(pyz[toc_pos:])
        except Exception:
            return "未知"
        toc = dict(toc) if isinstance(toc, list) else toc
        _, pos, length = toc["pet"]
        code = zlib.decompress(pyz[pos:pos + length])
        m = re.search(rb"__version__.{0,40}?(\d+\.\d+\.\d+)", code, re.S) or re.search(rb"(\d+\.\d+\.\d+)", code)
        return m.group(1).decode() if m else "未知"
    except Exception:
        return "未知"


# ---------------------------------------------------------------- .app
def find_app(arg) -> Path:
    if arg:
        p = Path(arg).expanduser()
        if p.suffix != ".app" and (p / APP_BUNDLE).is_dir():
            p = p / APP_BUNDLE
        if not (p / EXE_REL).is_file():
            raise win.PatchError(f"{p} 里没有 {EXE_REL}（请给出 {APP_BUNDLE} 的完整路径）")
        return p
    for base in (Path("/Applications"), Path.home() / "Applications", Path.home() / "Downloads", Path.home() / "Desktop"):
        p = base / APP_BUNDLE
        if (p / EXE_REL).is_file():
            return p
    raise win.PatchError(f"没找到 {APP_BUNDLE}。请先把上游的 Mac 版拖进“应用程序”文件夹，或者用 --app 指定它的位置")


def check_bundle(app: Path) -> None:
    try:
        info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except Exception as exc:
        raise win.PatchError(f"读不了 {app}/Contents/Info.plist：{exc}")
    if info.get("CFBundleExecutable") != APP_NAME:
        raise win.PatchError(f"{app} 不是 {APP_NAME}（它的主程序叫 {info.get('CFBundleExecutable')}）。"
                             "本扩展只支持带 AI 对话的 webm-chat 版")
    if not (app / "Contents" / "Frameworks" / "Python.framework" / "Versions" / "3.11").exists() \
            and not (app / "Contents" / "Frameworks" / "python3.11").exists():
        print("  提示：没在 .app 里看到 Python 3.11，稍后会按主程序里的记录再确认一次")


def ensure_not_running(app: Path) -> None:
    exe = str(app / EXE_REL)
    try:
        r = subprocess.run(["pgrep", "-f", exe], capture_output=True, text=True)
    except OSError:
        return
    if r.stdout.strip():
        raise win.PatchError("桌宠正在运行。请先从菜单栏图标退出（所有实例），再重新运行")


def codesign(app: Path) -> None:
    """重新做 ad-hoc 签名（系统自带的 codesign，不需要开发者账号）。"""
    try:
        r = subprocess.run(["codesign", "--force", "--sign", "-", str(app)], capture_output=True, text=True)
    except OSError as exc:
        raise win.PatchError(f"找不到 codesign（macOS 自带的工具）：{exc}")
    if r.returncode != 0:
        raise win.PatchError("重新签名失败：" + (r.stderr or r.stdout).strip())
    v = subprocess.run(["codesign", "--verify", str(app)], capture_output=True, text=True)
    if v.returncode != 0:
        raise win.PatchError("签名校验没通过：" + (v.stderr or v.stdout).strip())


def remove_quarantine(app: Path) -> None:
    r = subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)], capture_output=True, text=True)
    print("  已去掉下载隔离标记" if r.returncode == 0 else "  去掉隔离标记失败：" + (r.stderr or r.stdout).strip())


def copy_addons(addon_dir: Path, src_root: Path = REPO) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".pytest_cache")
    addon_dir.mkdir(parents=True, exist_ok=True)
    for name, marker in (("pet_memory", "bootstrap.py"), ("ascii_pet", str(Path("pet_ascii") / "__init__.py"))):
        src, dst = src_root / name, addon_dir / name
        if not (src / marker).is_file():
            raise win.PatchError(f"安装包不完整：找不到 {src / marker}")
        win.remove_ours(dst, marker)
        shutil.copytree(src, dst, ignore=ignore)
        print(f"  已复制 {name} → {dst}")


def backup_dir(addon_dir: Path) -> Path:
    return addon_dir / "backup"


def write_exe(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".dsh-new")
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    tmp.replace(path)


def status(app: Path, addon_dir: Path = ADDON_DIR) -> dict:
    exe = app / EXE_REL
    data = exe.read_bytes()
    bk = backup_dir(addon_dir)
    st = {"app": app, "arch": MachO(data).arch, "patched": is_patched_exe(data), "version": upstream_version(data),
          "backup": (bk / APP_NAME).is_file(), "pet_memory": (addon_dir / "pet_memory" / "bootstrap.py").is_file(),
          "ascii_pet": (addon_dir / "ascii_pet" / "pet_ascii" / "__init__.py").is_file()}
    return st


def print_status(st: dict, addon_dir: Path = ADDON_DIR) -> None:
    yn = lambda v: "是" if v else "否"  # noqa: E731
    print(f"桌宠：{st['app']}（{st['arch']}，上游版本 {st['version']}；本扩展的 Mac 版按 {TESTED_UPSTREAM_MAC} 制作）")
    print(f"  主程序已装扩展入口：{yn(st['patched'])}    原版备份：{yn(st['backup'])}")
    print(f"  扩展文件夹：{addon_dir}")
    print(f"    pet_memory：{yn(st['pet_memory'])}    ascii_pet：{yn(st['ascii_pet'])}")


def install(app: Path, addon_dir: Path = ADDON_DIR, src_root: Path = REPO, sign=codesign, dry_run=False) -> str:
    exe = app / EXE_REL
    data = exe.read_bytes()
    if is_patched_exe(data):
        mode = "主程序已经装过扩展入口，这次只更新扩展文件"
        new = None
    else:
        new, mode = patch_executable(data)
    if dry_run:
        return mode
    if new is not None:
        bk = backup_dir(addon_dir)
        bk.mkdir(parents=True, exist_ok=True)
        (bk / APP_NAME).write_bytes(data)
        seal = app / SEAL_REL
        if seal.is_file():
            (bk / "CodeResources").write_bytes(seal.read_bytes())
        (bk / "backup.json").write_text(json.dumps({
            "app": str(app), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "sha256": hashlib.sha256(data).hexdigest(),
            "upstream": upstream_version(data)}, ensure_ascii=False, indent=1), encoding="utf-8")
        write_exe(exe, new)
        try:
            sign(app)
        except Exception:
            restore(app, addon_dir)                              # 签不上就立刻还原，桌宠保持原样
            raise
    copy_addons(addon_dir, src_root)
    (addon_dir / "app_path").write_text(str(app), encoding="utf-8")
    return mode


def restore(app: Path, addon_dir: Path = ADDON_DIR) -> bool:
    bk = backup_dir(addon_dir)
    orig = bk / APP_NAME
    if not orig.is_file():
        return False
    data = orig.read_bytes()
    if is_patched_exe(data):
        raise win.PatchError("备份里的主程序也是装过扩展的，不能用来还原")
    write_exe(app / EXE_REL, data)
    if (bk / "CodeResources").is_file():
        (app / SEAL_REL).write_bytes((bk / "CodeResources").read_bytes())
    return True


def uninstall(app: Path, addon_dir: Path = ADDON_DIR) -> list:
    done = []
    if is_patched_exe((app / EXE_REL).read_bytes()):
        if not restore(app, addon_dir):
            raise win.PatchError(f"找不到原版备份（{backup_dir(addon_dir)}）。请从上游重新下载 Mac 版覆盖安装")
        done.append("已还原原版主程序和签名封条（原版自带的 ad-hoc 签名重新生效）")
    for name, marker in (("pet_memory", "bootstrap.py"), ("ascii_pet", str(Path("pet_ascii") / "__init__.py"))):
        if win.remove_ours(addon_dir / name, marker):
            done.append(f"已删除 {addon_dir / name}")
    shutil.rmtree(backup_dir(addon_dir), ignore_errors=True)
    (addon_dir / "app_path").unlink(missing_ok=True)
    return done


# ---------------------------------------------------------------- 命令行
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="dsh-pet-ascii 扩展的 macOS 安装器")
    ap.add_argument("--app", help=f"{APP_BUNDLE} 的路径（默认在“应用程序”等常见位置找）")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--remove-quarantine", action="store_true", help="去掉 .app 的下载隔离标记（等同于在“隐私与安全性”里点“仍要打开”）")
    args = ap.parse_args(argv)
    if sys.platform != "darwin" and not args.status and not args.dry_run:
        print("这是 macOS 用的安装器。Windows 请运行仓库根目录的 install.bat")
        return 2
    try:
        app = find_app(args.app)
        check_bundle(app)
        st = status(app)
        print_status(st)
        if args.status:
            return 0
        if args.uninstall:
            ensure_not_running(app)
            if not args.yes and input("确定卸载扩展吗？[y/N] ").strip().lower() not in ("y", "yes"):
                return 1
            for line in uninstall(app):
                print("  " + line)
            print(f"\n卸载完成。你的记忆、设置、房间仍在 {DATA_DIR}，不需要的话可以自己删除。")
            return 0
        print("\n已在 macOS 26.4 / arm64、上游 v4.3.4 上验证安装与签名；验证范围见 mac/README.md。"
              "出问题时运行 --uninstall 可恢复备份。")
        if args.dry_run:
            print("检查通过：" + install(app, dry_run=True) + "（--dry-run：没有写入任何文件）")
            return 0
        ensure_not_running(app)
        if not args.yes and input("开始安装吗？[y/N] ").strip().lower() not in ("y", "yes"):
            return 1
        mode = install(app)
        print("  " + mode)
        if args.remove_quarantine:
            remove_quarantine(app)
        print("\n安装完成。像平时一样打开桌宠；第一次打开如果提示“无法验证开发者”，见 mac/README.md。")
        print("然后双击本文件夹里的 open_lab.command，在「显示与设置」页勾选「随桌宠启动字符画窗口，并与对话联动」，再重开桌宠。")
        return 0
    except PermissionError as exc:
        print(f"\n没有权限修改 {exc.filename}。macOS 需要你允许“终端”修改其他应用："
              "系统设置 → 隐私与安全性 → App 管理（App Management）→ 打开“终端”，然后重新运行。")
        return 1
    except win.PatchError as exc:
        print(f"\n没有完成：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
