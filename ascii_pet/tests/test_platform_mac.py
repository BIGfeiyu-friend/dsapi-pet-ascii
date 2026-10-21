"""macOS（实验版）支持：数据目录、打开 Spotify、bootstrap 找字符画模块，以及 Mac 安装器对 Mach-O 的改写。
测试机是 Windows：Mac 的分支靠把 sys.platform 临时换成 "darwin" 来测；安装器用一个合成的小 Mach-O 程序测；
真机上的 codesign 签名测不了（安装器里那一步用桩代替）。"""
import base64
import importlib.util
import marshal
import struct
import sys
import types
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]                     # 仓库根目录：install.py、mac/、pet_memory/ 都在这里


# ---------------------------------------------------------------------- 数据目录 / Spotify / bootstrap
def test_data_dirs_follow_upstream_on_mac(monkeypatch, tmp_path):
    from pet_ascii import config, persona_evidence

    monkeypatch.delenv("PET_ASCII_HOME", raising=False)
    monkeypatch.delenv("PET_ASCII_MEMORY_DB", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    fake_pet = types.ModuleType("pet")                         # 拿不到桌宠自己的算法：按系统推断
    monkeypatch.setitem(sys.modules, "pet", fake_pet)
    monkeypatch.setitem(sys.modules, "pet.config", None)
    base = tmp_path / "Library" / "Application Support" / config.APP_DIR_NAME
    assert config.app_base_dir() == base
    assert config.data_dir() == base / "ascii_pet" and (base / "ascii_pet").is_dir()
    assert persona_evidence.memory_db_path() == base / "memory" / "memory.db"
    fake_cfg = types.ModuleType("pet.config")                  # 拿得到：跟着桌宠走
    fake_cfg._default_base = lambda: tmp_path / "UpstreamBase"
    fake_cfg.APP_DIR_NAME = "dsh-pet-standalone-webm-chat"
    fake_pet.config = fake_cfg
    monkeypatch.setitem(sys.modules, "pet.config", fake_cfg)
    assert config.app_base_dir() == tmp_path / "UpstreamBase" / "dsh-pet-standalone-webm-chat"


def test_windows_data_dir_unchanged(monkeypatch, tmp_path):
    from pet_ascii import config

    monkeypatch.delenv("PET_ASCII_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.app_base_dir() == tmp_path / config.APP_DIR_NAME


def test_spotify_opens_the_mac_app(monkeypatch):
    import subprocess

    from pet_ascii import spotify

    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: calls.append(args))
    assert spotify.launch_desktop_app() is True and calls == [["open", "spotify:"]]


def test_bootstrap_finds_ascii_pet_next_to_pet_memory():
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", ROOT / "pet_memory" / "bootstrap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cands = [Path(c) for c in mod._ascii_candidates()]
    assert cands[0] == Path(sys.executable).resolve().parent / "ascii_pet"          # Windows：exe 旁边
    assert cands[1] == ROOT / "ascii_pet"                                            # Mac：和 pet_memory 放在一起


# ---------------------------------------------------------------------- Mac 安装器（合成的小 Mach-O）
@pytest.fixture(scope="module")
def im():
    sys.path.insert(0, str(ROOT / "mac"))
    try:
        import install_mac

        yield install_mac
    finally:
        sys.path.remove(str(ROOT / "mac"))


def carchive(win, entries) -> bytes:
    body, toc = b"", b""
    tsz = struct.calcsize(win.TOC_FMT)
    for name, typ, content in entries:
        flag = 1 if typ == b"s" else 0
        raw = zlib.compress(content, 9) if flag else content
        nm = name.encode() + b"\0"
        nm += b"\0" * ((-(tsz + len(nm))) % 16)
        toc += struct.pack(win.TOC_FMT, tsz + len(nm), len(body), len(raw), len(content), flag, typ) + nm
        body += raw
    pkg_len = len(body) + len(toc) + win.COOKIE_SIZE
    return body + toc + struct.pack(win.COOKIE_FMT, win.MAGIC, pkg_len, len(body), len(toc), 311, b"Python".ljust(64, b"\0"))


def macho(win, entry: bytes) -> bytes:
    """合成一个最小的 arm64 Mach-O：__TEXT + __LINKEDIT，字符串表罩住末尾的 CArchive（PyInstaller 的布局），最后是代码签名。"""
    PAGE = 0x4000
    pkg = carchive(win, [("pyiboot01_bootstrap", b"s", marshal.dumps(compile("pass", "b", "exec"))),
                         ("pet_entry", b"s", entry), ("PYZ.pyz", b"z", b"PYZ\0" + b"\x00" * 60)])
    seg = struct.Struct("<II16sQQQQiiII")
    symoff, stroff = PAGE, PAGE + 16
    strtab = b"\0_main\0".ljust(16, b"\0")
    sig_off = (stroff + len(strtab) + len(pkg) + 15) // 16 * 16
    sig = struct.pack(">III", 0xFADE0CC0, 64, 0).ljust(64, b"\0")
    end = sig_off + len(sig)
    cmds = [seg.pack(0x19, 72, b"__TEXT", 0x100000000, PAGE, 0, PAGE, 5, 5, 0, 0),
            seg.pack(0x19, 72, b"__LINKEDIT", 0x100000000 + PAGE, (end - PAGE + PAGE - 1) // PAGE * PAGE, PAGE, end - PAGE, 1, 1, 0, 0),
            struct.pack("<IIIIII", 0x2, 24, symoff, 1, stroff, len(strtab) + len(pkg)),
            struct.pack("<IIII", 0x1D, 16, sig_off, len(sig))]
    lc = b"".join(cmds)
    hdr = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, len(cmds), len(lc), 0, 0)
    head = (hdr + lc).ljust(PAGE, b"\0")
    data = head + b"S" * 16 + strtab + pkg
    return data + b"\0" * (sig_off - len(data)) + sig


ORIGINAL = marshal.dumps(compile("import sys\nsys._dsh_original_ran = True\n", "pet_entry.py", "exec"))


def test_patch_rewrites_only_the_entry_and_linkedit(im):
    win = im.win
    orig = macho(win, ORIGINAL)
    new, _ = im.patch_executable(orig)
    m0, m1 = im.MachO(orig), im.MachO(new)
    assert m1.codesig is None and m1.ncmds == m0.ncmds - 1                       # 旧签名连同加载命令一起去掉
    _, _, vmsize, fileoff, filesize = m1.linkedit
    assert fileoff + filesize == len(new) and vmsize % 0x4000 == 0 and vmsize >= filesize
    _, _, _, stroff, strsize = m1.symtab
    assert stroff + strsize == len(new)                                          # 字符串表罩住新的归档
    arc0, arc1 = win.Archive(orig[:orig.rfind(win.MAGIC) + win.COOKIE_SIZE]), win.Archive(new)
    assert [e["name"] for e in arc1.entries] == [e["name"] for e in arc0.entries]
    assert arc1.raw(arc1.entry("PYZ.pyz")) == arc0.raw(arc0.entry("PYZ.pyz"))
    entry = arc1.content("pet_entry")
    assert win.MARKER in entry and ORIGINAL in entry and im.is_patched_exe(new) and not im.is_patched_exe(orig)
    with pytest.raises(win.PatchError):
        im.patch_executable(new)                                                 # 装过的不再装


def test_mac_wrapper_runs_addons_then_original(im, tmp_path, monkeypatch):
    if sys.version_info[:2] != (3, 11):
        pytest.skip("入口字节码只能在 Python 3.11 下执行")
    addons = tmp_path / "Library" / "Application Support" / "dsh-pet-ascii"
    (addons / "pet_memory").mkdir(parents=True)
    (addons / "pet_memory" / "__init__.py").write_text("", encoding="utf-8")
    (addons / "pet_memory" / "bootstrap.py").write_text("import sys\ndef install():\n    sys._dsh_addon_installed = True\n",
                                                         encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for k in ("pet_memory", "pet_memory.bootstrap"):
        monkeypatch.delitem(sys.modules, k, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    code = marshal.loads(im.mac_wrapper_entry(ORIGINAL))
    try:
        exec(code, {"__name__": "__main__"})
        assert getattr(sys, "_dsh_addon_installed", False) and getattr(sys, "_dsh_original_ran", False)
        assert sys.path[0] == str(addons)
    finally:
        for k in ("_dsh_addon_installed", "_dsh_original_ran"):
            if hasattr(sys, k):
                delattr(sys, k)
        for k in ("pet_memory", "pet_memory.bootstrap"):
            sys.modules.pop(k, None)


def test_install_and_uninstall_restore_the_app_exactly(im, tmp_path):
    orig = macho(im.win, ORIGINAL)
    app = tmp_path / im.APP_BUNDLE
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "_CodeSignature").mkdir(parents=True)
    (app / im.EXE_REL).write_bytes(orig)
    (app / im.SEAL_REL).write_bytes(b"<plist>seal</plist>")
    src = tmp_path / "src"
    (src / "pet_memory").mkdir(parents=True)
    (src / "pet_memory" / "bootstrap.py").write_text("x = 1", encoding="utf-8")
    (src / "ascii_pet" / "pet_ascii").mkdir(parents=True)
    (src / "ascii_pet" / "pet_ascii" / "__init__.py").write_text("y = 1", encoding="utf-8")
    addons, signed = tmp_path / "addons", []
    im.install(app, addons, src, sign=signed.append)
    assert im.is_patched_exe((app / im.EXE_REL).read_bytes()) and signed == [app]
    assert (addons / "backup" / im.APP_NAME).read_bytes() == orig and (addons / "app_path").read_text() == str(app)
    assert (addons / "ascii_pet" / "pet_ascii" / "__init__.py").is_file()
    im.install(app, addons, src, sign=signed.append)                             # 再装一次：只更新扩展文件，不再签名
    assert signed == [app]
    im.uninstall(app, addons)
    assert (app / im.EXE_REL).read_bytes() == orig and (app / im.SEAL_REL).read_bytes() == b"<plist>seal</plist>"
    assert not (addons / "pet_memory").exists() and not (addons / "backup").exists()

    def broken_sign(a):
        raise im.win.PatchError("模拟签名失败")

    with pytest.raises(im.win.PatchError):
        im.install(app, addons, src, sign=broken_sign)
    assert (app / im.EXE_REL).read_bytes() == orig                                # 签不上：立刻还原


def test_embedded_mac_entry_matches_its_source(im):
    if sys.version_info[:2] != (3, 11):
        pytest.skip("入口字节码要用 Python 3.11 编译")
    src = (ROOT / "patch_src" / "pet_entry_wrapper_mac.py").read_text(encoding="utf-8")
    code = compile(src, "pet_entry.py", "exec", dont_inherit=True, optimize=0)
    # 比代码对象而不是 marshal 字节：marshal 的输出会随进程里字符串的驻留情况变（引用标记），内容相同
    assert marshal.loads(base64.b64decode("".join(im.MAC_WRAPPER_ENTRY_B64))) == code
