"""dsh-pet-ascii 安装器：给你自己电脑上已装好的桌宠（dsh-pet-standalone-webm-chat）装上 / 卸下扩展。

做的事（只动桌宠安装目录，不碰 %APPDATA% 里的任何数据）：
  1. 备份 dsh-pet-standalone-webm-chat.exe → dsh-pet-standalone-webm-chat.exe.orig
  2. 把 exe 里 PyInstaller 归档（CArchive）的入口脚本 pet_entry 换成多了两行的版本：
     启动时先 `import pet_memory.bootstrap; pet_memory.bootstrap.install()`，出错就跳过，然后照原样启动。
     exe 的其余部分（程序本体、PYZ、所有资源）一个字节都不变。
  3. 复制 pet_memory\\ 到 安装目录\\_internal\\pet_memory\\，复制 ascii_pet\\ 到 安装目录\\ascii_pet\\

用法（也可以直接双击 install.bat / uninstall.bat）：
    python install.py              安装或更新
    python install.py --uninstall  卸载：还原原版 exe，删除两个扩展文件夹
    python install.py --status     只查看当前状态
    python install.py --dry-run    只检查、不写入
  可选：--dir <桌宠安装目录>  --yes（不询问）  --generic（强制用通用入口，调试用）

需要 Python 3.8 以上（任意版本，只用标准库）。运行前请先从托盘退出桌宠。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import shutil
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE_NAME = "dsh-pet-standalone-webm-chat.exe"
APP_DIR_NAME = "dsh-pet-standalone-webm-chat"
TESTED_UPSTREAM = "4.2.1"
INNO_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{3424d6cc-af3c-4383-8797-ab520b923aa6}_is1"

MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE_FMT = "!8sIIII64s"
COOKIE_SIZE = struct.calcsize(COOKIE_FMT)
TOC_FMT = "!IIIIBc"
TOC_SIZE = struct.calcsize(TOC_FMT)
ENTRY_NAME = "pet_entry"
ZLIB_LEVEL = 9  # 与 PyInstaller 打包时一致，压缩结果逐字节相同
PY_VERSION = 311  # 内嵌的入口字节码是 Python 3.11 的
MARKER = b"pet_memory.bootstrap"
PLACEHOLDER = b"@@DSH_PET_ASCII_ORIGINAL_PET_ENTRY_PLACEHOLDER@@"

# 已知的原版入口（解压后的 sha256）→ 上游版本。命中时使用逐字节固定的改版入口（patch_src/pet_entry_patched.py）
KNOWN_ORIGINAL_ENTRY = {
    "748dd98d70b1ae5a207aa3e09903eceb6885441e5e9b0aebf6865d58701cb41a": "4.2.1",
}

# 下面两段由 patch_src/build_entry_blobs.py 用 Python 3.11 编译生成（可用 --check 核对）
# patch_src/pet_entry_patched.py，sha256 b184d001fb74c7ff46fd9a62602a3cb037ec7d8a40c6b2d106e00eb5b025c552
PATCHED_ENTRY_B64 = (
    "4wAAAAAAAAAAAAAAAAQAAAAAAAAA8/AAAACXAGQAWgBkAWQCbAFaAQkAZAFkAmwCbQNaBAEAAgBl"
    "BGoFAAAAAAAAAACmAAAAqwAAAAAAAAAAAAEAbgsjAGUGJAByAwEAWQBuBHcAeANZAHcBZANlAWoH"
    "AAAAAAAAAAB2AHIeZAFkBGwIbQlaCQEAAgBlAWoKAAAAAAAAAAACAGUJpgAAAKsAAAAAAAAAAACm"
    "AQAAqwEAAAAAAAAAAAEAZAFkBWwLbQxaDAEAZQ1kBmsCAAAAAHIaAgBlAWoKAAAAAAAAAAACAGUM"
    "pgAAAKsAAAAAAAAAAACmAQAAqwEAAAAAAAAAAAEAZAJTAGQCUwApB3UxAQAAClB5SW5zdGFsbGVy"
    "IOS4u+WFpeWPo++8iOW3suWKoOijhSBwZXRfbWVtb3J577ya5rC45LmF6K6w5b+GICsg5ZON5bqU"
    "5o+Q6YCf77yJ44CCCgrkuI7ljp/niYjnmoTllK/kuIDljLrliKvvvJrlnKjliqDovb0gcGV0LmFw"
    "cCDkuYvliY3lsJ3or5Xlronoo4UgX2ludGVybmFsL3BldF9tZW1vcnkg6YeM55qE6KGl5LiB77yb"
    "CnBldF9tZW1vcnkg57y65aSx5oiW5Ye66ZSZ5pe26Z2Z6buY6Lez6L+H77yM5qGM5a6g5oyJ5Y6f"
    "5qC36L+Q6KGM44CC5Yig6ZmkIF9pbnRlcm5hbC9wZXRfbWVtb3J5IOaWh+S7tuWkueWNs+WPr+WB"
    "nOeUqOOAggrpAAAAAE56Ci0tc2V0dGluZ3MpAdoNX3J1bl9zZXR0aW5ncykB2gRtYWlu2ghfX21h"
    "aW5fXykO2gdfX2RvY19f2gNzeXPaFHBldF9tZW1vcnkuYm9vdHN0cmFw2glib290c3RyYXDaFV9w"
    "ZXRfbWVtb3J5X2Jvb3RzdHJhcNoHaW5zdGFsbNoJRXhjZXB0aW9u2gRhcmd22gxwZXQuX19tYWlu"
    "X19yAwAAANoEZXhpdNoHcGV0LmFwcHIEAAAA2ghfX25hbWVfX6kA8wAAAAD6DHBldF9lbnRyeS5w"
    "efoIPG1vZHVsZT5yFQAAAAEAAABz4gAAAPADAQEB8AIFAQTwAAUBBPAMAAELgAqACoAK8AQFAQnY"
    "BDjQBDjQBDjQBDjQBDjQBDjgBCHQBBnUBCHRBCPUBCPQBCPQBCP42AcQ8AABAQnwAAEBCfAAAQEJ"
    "2AQIgETwAwEBCfj4+PAGAAQQkDOUONADG9ADG9gEKtAEKtAEKtAEKtAEKtAEKuAEDIBDhEiIXYhd"
    "iV+MX9EEHdQEHdAEHeAAGNAAGNAAGNAAGNAAGNAAGOADC4h60gMZ0AMZ2AQMgEOESIhUiFSJVoxW"
    "0QQU1AQU0AQU0AQU0AQU8AMABBrQAxlzDAAAAIgVHgCeBSYDpQEmAw=="
)
# patch_src/pet_entry_wrapper.py（含占位符），sha256 94271184705f7a80ca85aa09444f309a1805b28281e4ed4b5d02bb8ccce6b75f
WRAPPER_ENTRY_B64 = (
    "4wAAAAAAAAAAAAAAAAUAAAAAAAAA85AAAACXAGQAWgBkAWQCbAFaAQkAZAFkAmwCbQNaBAEAAgBl"
    "BGoFAAAAAAAAAACmAAAAqwAAAAAAAAAAAAEAbgsjAGUGJAByAwEAWQBuBHcAeANZAHcBZAFkAmwH"
    "WggCAGUJAgBlCGoKAAAAAAAAAABkA6YBAACrAQAAAAAAAAAApgEAAKsBAAAAAAAAAAABAGQCUwAp"
    "BHVnAQAAClB5SW5zdGFsbGVyIOS4u+WFpeWPo++8iOW3suWKoOijhSBwZXRfbWVtb3J577yM6YCa"
    "55So5YyF6KOF54mI77yM55SxIGluc3RhbGwucHkg55Sf5oiQ77yJ44CCCgrlhYjlsJ3or5Xlrono"
    "o4UgX2ludGVybmFsL3BldF9tZW1vcnkg6YeM55qE6KGl5LiB77yM5YaN5Y6f5qC36L+Q6KGM5Y6f"
    "54mI5YWl5Y+j77yI5Y6f54mI5YWl5Y+j55qE5a2X6IqC56CB5bCx5a2Y5Zyo5pys5paH5Lu255qE"
    "5bi46YeP6YeM77yJ44CCCnBldF9tZW1vcnkg57y65aSx5oiW5Ye66ZSZ5pe26Z2Z6buY6Lez6L+H"
    "77yM5qGM5a6g5oyJ5Y6f5qC36L+Q6KGM44CC5Yig6ZmkIF9pbnRlcm5hbC9wZXRfbWVtb3J5IOaW"
    "h+S7tuWkueWNs+WPr+WBnOeUqOOAggrpAAAAAE5zMAAAAEBARFNIX1BFVF9BU0NJSV9PUklHSU5B"
    "TF9QRVRfRU5UUllfUExBQ0VIT0xERVJAQCkL2gdfX2RvY19f2gNzeXPaFHBldF9tZW1vcnkuYm9v"
    "dHN0cmFw2glib290c3RyYXDaFV9wZXRfbWVtb3J5X2Jvb3RzdHJhcNoHaW5zdGFsbNoJRXhjZXB0"
    "aW9u2gdtYXJzaGFs2gxfcGV0X21hcnNoYWzaBGV4ZWPaBWxvYWRzqQDzAAAAAPoMcGV0X2VudHJ5"
    "LnB5+gg8bW9kdWxlPnIRAAAAAQAAAHOZAAAA8AMBAQHwAgUBBPAABQEE8AwAAQuACoAKgArwBAUB"
    "CdgEONAEONAEONAEONAEONAEOOAEIdAEGdQEIdEEI9QEI9AEI9AEI/jYBxDwAAEBCfAAAQEJ8AAB"
    "AQnYBAiARPADAQEJ+Pj48AYAAR/QAB7QAB7QAB7gAASABNAFF4Bc1AUX0BhL0QVM1AVM0QBN1ABN"
    "0ABN0ABN0ABNcwwAAACIFR4AngUmA6UBJgM="
)


class PatchError(Exception):
    pass


class Archive:
    """PyInstaller onedir exe 末尾的 CArchive：入口脚本、引导模块和 PYZ 都在里面。"""

    def __init__(self, data: bytes, label: str = "exe"):
        self.data = data
        pos = data.rfind(MAGIC)
        if pos < 0:
            raise PatchError(f"{label} 不是 PyInstaller 程序（找不到 CArchive 标记）")
        if pos + COOKIE_SIZE != len(data):
            raise PatchError(f"{label} 末尾还有额外数据（可能被签名或改过），为安全起见不处理")
        _, pkg_len, toc_off, toc_len, self.pyver, self.pylib_raw = struct.unpack_from(COOKIE_FMT, data, pos)
        self.start = len(data) - pkg_len
        if self.start < 0 or toc_off + toc_len + COOKIE_SIZE > pkg_len:
            raise PatchError(f"{label} 的 CArchive 头信息不合理")
        self.entries = []
        p, end = self.start + toc_off, self.start + toc_off + toc_len
        while p < end:
            esz, off, dlen, ulen, flag, typ = struct.unpack_from(TOC_FMT, data, p)
            if esz < TOC_SIZE or p + esz > end:
                raise PatchError(f"{label} 的 CArchive 目录损坏")
            name_raw = data[p + TOC_SIZE:p + esz]
            name = name_raw.rstrip(b"\0").decode("utf-8", "replace")
            self.entries.append(dict(esz=esz, off=off, dlen=dlen, ulen=ulen, flag=flag, typ=typ,
                                     name_raw=name_raw, name=name))
            p += esz

    def entry(self, name: str) -> dict:
        for e in self.entries:
            if e["name"] == name:
                return e
        raise PatchError(f"CArchive 里没有 {name}（这可能不是 webm-chat 版的桌宠）")

    def raw(self, e: dict) -> bytes:
        a = self.start + e["off"]
        return self.data[a:a + e["dlen"]]

    def content(self, name: str) -> bytes:
        e = self.entry(name)
        raw = self.raw(e)
        return zlib.decompress(raw) if e["flag"] else raw

    def rebuild(self, replace: dict) -> bytes:
        """按原顺序重写归档，只替换 replace 里给出的条目内容，其余条目原样拷贝。"""
        body, toc = bytearray(), bytearray()
        for e in self.entries:
            if e["name"] in replace:
                content = replace[e["name"]]
                raw = zlib.compress(content, ZLIB_LEVEL) if e["flag"] else content
                ulen = len(content)
            else:
                raw, ulen = self.raw(e), e["ulen"]
            toc += struct.pack(TOC_FMT, e["esz"], len(body), len(raw), ulen, e["flag"], e["typ"]) + e["name_raw"]
            body += raw
        pkg_len = len(body) + len(toc) + COOKIE_SIZE
        cookie = struct.pack(COOKIE_FMT, MAGIC, pkg_len, len(body), len(toc), self.pyver, self.pylib_raw)
        return bytes(self.data[:self.start]) + bytes(body) + bytes(toc) + cookie


def is_patched(entry: bytes) -> bool:
    return MARKER in entry


def wrapper_entry(original: bytes) -> bytes:
    """通用包装版入口：把原版入口的字节码塞进包装脚本的 bytes 常量（marshal 里是 's' + 4 字节长度 + 内容）。"""
    blob = base64.b64decode("".join(WRAPPER_ENTRY_B64))
    needle = struct.pack("<I", len(PLACEHOLDER)) + PLACEHOLDER
    i = blob.find(needle)
    if i <= 0 or blob.count(needle) != 1 or blob[i - 1] not in (0x73, 0xF3):
        raise PatchError("内嵌的通用入口模板损坏")
    return blob[:i] + struct.pack("<I", len(original)) + original + blob[i + len(needle):]


def new_entry_for(original: bytes, force_generic: bool) -> tuple:
    version = KNOWN_ORIGINAL_ENTRY.get(hashlib.sha256(original).hexdigest())
    if version and not force_generic:
        return base64.b64decode("".join(PATCHED_ENTRY_B64)), f"固定改版入口（适配上游 {version}）"
    if not original[:1] or original[0] & 0x7F != 0x63:  # marshal 的 code 对象以 'c' 开头
        raise PatchError("原版入口不是 Python 字节码，无法包装")
    return wrapper_entry(original), "通用包装入口（未见过的上游版本：先装扩展，再原样运行原版入口）"


def build_patched(source: bytes, force_generic: bool = False) -> tuple:
    arc = Archive(source, "原版 exe")
    if arc.pyver != PY_VERSION:
        raise PatchError(f"这个版本的桌宠用的是 Python {arc.pyver // 100}.{arc.pyver % 100}，"
                         "本安装器内嵌的入口只适用于 Python 3.11，请等待扩展更新")
    e = arc.entry(ENTRY_NAME)
    if e["typ"] != b"s":
        raise PatchError("pet_entry 不是入口脚本类型，跳过")
    original = arc.content(ENTRY_NAME)
    if is_patched(original):
        raise PatchError("这个 exe 已经装过扩展了")
    new_entry, mode = new_entry_for(original, force_generic)
    out = arc.rebuild({ENTRY_NAME: new_entry})
    # 自检：新归档能解析，入口是新的，其余条目与原版逐字节相同
    chk = Archive(out, "新 exe")
    if chk.content(ENTRY_NAME) != new_entry or out[:chk.start] != source[:arc.start]:
        raise PatchError("自检失败：新 exe 的入口或程序头不对")
    if [x["name"] for x in chk.entries] != [x["name"] for x in arc.entries]:
        raise PatchError("自检失败：条目列表变了")
    for a, b in zip(arc.entries, chk.entries):
        if a["name"] != ENTRY_NAME and arc.raw(a) != chk.raw(b):
            raise PatchError(f"自检失败：{a['name']} 变了")
    return out, mode


# ---------------------------------------------------------------- 安装目录

def registry_info() -> dict:
    try:
        import winreg
    except ImportError:
        return {}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, INNO_KEY) as k:
                info = {}
                for name in ("InstallLocation", "DisplayVersion"):
                    try:
                        info[name] = winreg.QueryValueEx(k, name)[0]
                    except OSError:
                        pass
                return info
        except OSError:
            continue
    return {}


def resolve_dir(arg) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    loc = registry_info().get("InstallLocation")
    if loc and (Path(loc) / EXE_NAME).exists():
        return Path(loc).resolve()
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_DIR_NAME


def upstream_version(d: Path) -> str:
    info = registry_info()
    loc = info.get("InstallLocation")
    try:
        if loc and Path(loc).resolve() == d.resolve():
            return info.get("DisplayVersion") or "未知"
    except OSError:
        pass
    return "未知"


def ensure_not_running(exe: Path) -> None:
    """运行中的 exe 无法以写方式打开；以此判断桌宠是否还开着（只针对这个路径的 exe）。"""
    if not exe.exists():
        return
    try:
        with open(exe, "r+b"):
            pass
    except PermissionError:
        raise PatchError("桌宠正在运行（或 exe 被占用）。请先在托盘图标上右键退出（所有实例），再重新运行。")


def remove_ours(path: Path, marker: str) -> bool:
    """只删除确实是本扩展的文件夹（里面要有 marker 文件），避免误删别的东西。"""
    if not path.exists():
        return False
    if not (path / marker).is_file():
        raise PatchError(f"{path} 看起来不是本扩展的文件夹（缺少 {marker}），为安全起见没有删除")
    shutil.rmtree(path)
    return True


def copy_addons(d: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for src, dst, marker in ((HERE / "pet_memory", d / "_internal" / "pet_memory", "bootstrap.py"),
                             (HERE / "ascii_pet", d / "ascii_pet", os.path.join("pet_ascii", "__init__.py"))):
        if not (src / marker).is_file():
            raise PatchError(f"安装包不完整：找不到 {src / marker}")
        remove_ours(dst, marker)
        shutil.copytree(src, dst, ignore=ignore)
        print(f"  已复制 {src.name} → {dst}")


def status(d: Path) -> dict:
    exe, orig = d / EXE_NAME, d / (EXE_NAME + ".orig")
    st = {"dir": d, "exe": exe.exists(), "orig": orig.exists(), "patched": None, "orig_patched": None,
          "pet_memory": (d / "_internal" / "pet_memory" / "bootstrap.py").is_file(),
          "ascii_pet": (d / "ascii_pet" / "pet_ascii" / "__init__.py").is_file()}
    if st["exe"]:
        st["patched"] = is_patched(Archive(exe.read_bytes()).content(ENTRY_NAME))
    if st["orig"]:
        st["orig_patched"] = is_patched(Archive(orig.read_bytes(), ".orig").content(ENTRY_NAME))
    return st


def print_status(st: dict) -> None:
    yn = lambda v: "是" if v else ("否" if v is not None else "-")  # noqa: E731
    print(f"桌宠安装目录：{st['dir']}")
    print(f"  上游版本（安装记录）：{upstream_version(st['dir'])}（本扩展测试过的是 {TESTED_UPSTREAM}）")
    print(f"  exe 存在：{yn(st['exe'])}    exe 已装扩展入口：{yn(st['patched'])}")
    print(f"  .orig 备份存在：{yn(st['orig'])}    备份是原版：{yn(None if st['orig_patched'] is None else not st['orig_patched'])}")
    print(f"  _internal\\pet_memory：{yn(st['pet_memory'])}    ascii_pet：{yn(st['ascii_pet'])}")


def confirm(args, question: str) -> bool:
    if args.yes or args.dry_run:
        return True
    try:
        return input(question + "（回车继续，输入 n 取消）：").strip().lower() not in ("n", "no", "否")
    except EOFError:
        return False


def cmd_install(d: Path, args) -> None:
    exe, orig = d / EXE_NAME, d / (EXE_NAME + ".orig")
    if not exe.is_file() or not (d / "_internal").is_dir():
        raise PatchError(f"在 {d} 没找到桌宠（{EXE_NAME} 和 _internal 文件夹）。"
                         "请先安装上游 webm-chat 版，或用 --dir 指定安装目录。")
    ensure_not_running(exe)
    st = status(d)
    print_status(st)
    current = exe.read_bytes()
    backup_current = False
    if st["patched"]:
        if st["orig"] and not st["orig_patched"]:
            source, note = orig.read_bytes(), "exe 已装过扩展：用 .orig 原版重新生成入口，并更新扩展文件"
        else:
            source, note = None, "exe 已装过扩展但没有可用的原版备份：只更新扩展文件，不动 exe"
    else:
        source, note = current, "exe 是原版：备份为 .orig 后写入新入口，并复制扩展文件"
        backup_current = True
    new_exe, mode = (None, "不变")
    if source is not None:
        new_exe, mode = build_patched(source, args.generic)
    print(f"\n计划：{note}\n  入口：{mode}")
    if args.dry_run:
        print("（--dry-run：只检查，没有写入任何文件）")
        return
    if not confirm(args, "开始安装？"):
        print("已取消。")
        return
    if backup_current:
        if orig.exists() and orig.read_bytes() != current:
            prev = d / (EXE_NAME + ".orig.prev")
            if prev.exists():
                prev.unlink()
            orig.rename(prev)
            print(f"  旧的 .orig（多半来自更新前的版本）已改名为 {prev.name}")
        shutil.copy2(exe, orig)
        print(f"  已备份原版 → {orig.name}")
    if new_exe is not None:
        tmp = d / (EXE_NAME + ".new")
        tmp.write_bytes(new_exe)
        os.replace(tmp, exe)
        print(f"  已写入新入口 → {exe.name}")
    copy_addons(d)
    print("\n安装完成。像平时一样启动桌宠即可。")
    print("提示：桌宠自动更新会替换 exe，更新后请重新运行本安装器（你的记忆和设置都在 %APPDATA%，不受影响）。")


def cmd_uninstall(d: Path, args) -> None:
    exe, orig = d / EXE_NAME, d / (EXE_NAME + ".orig")
    ensure_not_running(exe)
    st = status(d)
    print_status(st)
    if args.dry_run:
        print("（--dry-run：只检查，没有写入任何文件）")
        return
    if not confirm(args, "开始卸载？"):
        print("已取消。")
        return
    if st["patched"]:
        if st["orig"] and not st["orig_patched"]:
            os.replace(orig, exe)
            print("  已用 .orig 还原原版 exe")
        else:
            print("  [注意] exe 装过扩展，但没有可用的原版备份。扩展文件夹删除后桌宠仍能照常运行；"
                  "想要完全原版，请重新安装上游安装包。")
    elif st["orig"]:
        print(f"  exe 已经是原版（可能被自动更新替换过），保留不动；旧备份 {orig.name} 可自行删除")
    for path, marker in ((d / "_internal" / "pet_memory", "bootstrap.py"),
                         (d / "ascii_pet", os.path.join("pet_ascii", "__init__.py"))):
        if remove_ours(path, marker):
            print(f"  已删除 {path}")
    print("\n卸载完成。你的聊天记忆、字符画数据和设置在 %APPDATA%\\" + APP_DIR_NAME +
          "\\ 下（memory\\、ascii_pet\\），安装器没有动它们；不需要的话可以自己删除。")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="dsh-pet-ascii 扩展安装器")
    ap.add_argument("--dir", help="桌宠安装目录（默认从安装记录或 %%LOCALAPPDATA%%\\Programs 查找）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--uninstall", action="store_true", help="卸载扩展并还原原版 exe")
    g.add_argument("--status", action="store_true", help="只查看状态")
    ap.add_argument("--dry-run", action="store_true", help="只检查，不写入")
    ap.add_argument("--yes", "-y", action="store_true", help="不询问确认")
    ap.add_argument("--generic", action="store_true", help="强制使用通用包装入口（调试用）")
    args = ap.parse_args(argv)
    if sys.platform != "win32" and not args.dir:
        print("本扩展只支持 Windows 版桌宠。")
        return 2
    d = resolve_dir(args.dir)
    try:
        if args.status:
            print_status(status(d))
        elif args.uninstall:
            cmd_uninstall(d, args)
        else:
            cmd_install(d, args)
    except PatchError as e:
        print("\n[失败] " + str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
