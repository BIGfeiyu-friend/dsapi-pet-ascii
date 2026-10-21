"""上传前的隐私检查：扫描本文件夹，找出不该公开的东西。

    python scan_private.py [要扫描的文件夹，默认是本文件所在的文件夹]

内置检查（对谁都适用）：
  - 看起来像 API Key 的字符串（sk-…）、32 位十六进制 ID（如 Spotify Client ID）、写成赋值的令牌 / 密钥
  - 邮箱地址、带用户名的 Windows 路径（C:\\Users\\<用户名>\\…）
  - 不该出现的文件：数据库（*.db / *.sqlite）、日志（*.log / *.jsonl）、memory / sessions 文件夹、
    桌宠的 exe 和 .orig 备份、*.pyc 和 __pycache__、反汇编转储（*.pyc.txt）

个人关键词（你的用户名、设备名、昵称、Client ID 等）不要写进本文件（它会被公开），
而是每行一个写在下面任一位置（# 开头的行是注释）：
  - 环境变量 SCAN_PRIVATE_TERMS 指向的文件
  - 本文件夹旁边的 dsh-pet-ascii-private-terms.txt（即 ..\\dsh-pet-ascii-private-terms.txt）
  - 本文件夹里的 scan_private.local.txt（已在 .gitignore 中忽略；但用网页拖拽上传时不会被忽略，所以更推荐上一种）
文本文件逐行检查；图片等二进制文件也会检查个人关键词和用户路径（防止元数据里带路径）。
发现问题时返回 1，干净时返回 0。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TERMS_FILE_NAME = "dsh-pet-ascii-private-terms.txt"
LOCAL_TERMS_NAME = "scan_private.local.txt"

TEXT_EXT = {".py", ".md", ".txt", ".json", ".bat", ".ps1", ".cfg", ".ini", ".toml", ".yml", ".yaml",
            ".csv", ".html", ".js", ".css", ".gitignore", ".spec", ""}

TEXT_PATTERNS = [
    ("疑似 API Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("32 位十六进制 ID（Client ID / 令牌？）", re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{32}(?![0-9A-Fa-f])")),
    ("写成赋值的密钥 / 令牌", re.compile(r"(?i)\b(api[_-]?key|secret|refresh_token|access_token|password)\b\s*[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-./+]{16,}[\"']")),
    ("邮箱地址", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")),
    ("带用户名的 Windows 路径", re.compile(r"(?i)(\b[a-z]:|/[a-z])[\\/]{1,2}users[\\/]{1,2}(?!public\b)[^\\/\s\"'<>*?|]+")),
]
BINARY_PATTERNS = [
    ("带用户名的 Windows 路径", re.compile(rb"(?i)[a-z]:[\\/]{1,2}users[\\/]{1,2}(?!public)[^\\/\x00\s\"'<>*?|]{1,40}")),
]

BAD_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".log", ".jsonl",
                ".exe", ".orig", ".orig.prev", ".exe.new", ".pyc", ".pyo", ".pyc.txt")
BAD_DIR_NAMES = {"__pycache__", "memory", "sessions", "anim_cache", ".pytest_cache"}
BAD_FILE_NAMES = {"memory_config.json", "notes.md", "balance_cache.json", "settings_standalone.pyc.txt"}
SKIP_DIRS = {".git"}


def load_terms(root: Path) -> tuple:
    candidates = []
    if os.environ.get("SCAN_PRIVATE_TERMS"):
        candidates.append(Path(os.environ["SCAN_PRIVATE_TERMS"]))
    candidates += [root.parent / TERMS_FILE_NAME, root / LOCAL_TERMS_NAME]
    terms, used = [], []
    for c in candidates:
        if c.is_file():
            used.append(c)
            for line in c.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
    return sorted(set(terms), key=str.lower), used


def is_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXT or path.name.startswith("."):
        try:
            path.read_bytes()[:4096].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return False


def scan(root: Path) -> int:
    terms, used = load_terms(root)
    term_res = [(t, re.compile(re.escape(t), re.IGNORECASE)) for t in terms]
    term_bytes = [(t, [t.encode("utf-8").lower(), t.encode("utf-16-le").lower()]) for t in terms]
    findings = []
    n_files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        d = Path(dirpath)
        for dn in dirnames:
            if dn.lower() in BAD_DIR_NAMES or dn.lower().startswith("sessions-slot-"):
                findings.append((d / dn, 0, "不该上传的文件夹", dn))
        for fn in filenames:
            p = d / fn
            n_files += 1
            rel = p.relative_to(root)
            low = fn.lower()
            if low in BAD_FILE_NAMES or low.endswith(BAD_SUFFIXES):
                findings.append((rel, 0, "不该上传的文件", fn))
            if p.resolve() == Path(__file__).resolve():
                pass  # 本文件只含通用规则；照样扫描，确认没有误写个人关键词
            if is_text(p):
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    for label, rx in TEXT_PATTERNS:
                        m = rx.search(line)
                        if m:
                            findings.append((rel, i, label, m.group(0)))
                    for t, rx in term_res:
                        if rx.search(line):
                            findings.append((rel, i, "个人关键词", t))
            else:
                data = p.read_bytes()
                low_data = data.lower()
                for label, rx in BINARY_PATTERNS:
                    m = rx.search(data)
                    if m:
                        findings.append((rel, 0, label + "（二进制）", m.group(0).decode("latin-1")))
                for t, encs in term_bytes:
                    if any(e and e in low_data for e in encs):
                        findings.append((rel, 0, "个人关键词（二进制）", t))
    print(f"扫描：{root}")
    print(f"  文件 {n_files} 个；个人关键词 {len(terms)} 个，来自："
          + ("、".join(str(u) for u in used) if used else "（没有找到关键词文件，只做了通用检查）"))
    if not findings:
        print("结果：CLEAN，没有发现问题。")
        return 0
    print(f"结果：发现 {len(findings)} 处需要处理：")
    for rel, line, label, what in findings:
        where = f"{rel}:{line}" if line else f"{rel}"
        shown = what if label != "个人关键词" and "关键词" not in label else "（关键词已隐藏）"
        print(f"  [{label}] {where}  {shown[:80]}")
    return 1


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE
    return scan(root)


if __name__ == "__main__":
    sys.exit(main())
