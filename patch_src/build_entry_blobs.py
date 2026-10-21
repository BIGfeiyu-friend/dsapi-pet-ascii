"""重新生成 install.py 里内嵌的两段入口字节码（仅开发者需要，普通安装用不到）。

桌宠自带的是 Python 3.11，入口脚本必须用 3.11 编译，所以本脚本只能用 Python 3.11 运行：

    py -3.11 patch_src\\build_entry_blobs.py          打印两段字节码的 sha256 和 base64
    py -3.11 patch_src\\build_entry_blobs.py --check  检查 install.py 里内嵌的常量是否与源码一致

- pet_entry_patched.py：针对 4.2.1 原版入口的改版（与原版相比只多了 pet_memory 那几行）
- pet_entry_wrapper.py：通用包装版，用于未知版本的原版入口；占位符会在安装时被换成原版入口的字节码
- pet_entry_wrapper_mac.py：macOS 版的通用包装（先把 ~/Library/Application Support/dsh-pet-ascii 加进导入路径），内嵌在 mac/install_mac.py
"""
from __future__ import annotations

import base64
import hashlib
import marshal
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build(name: str) -> bytes:
    src = (HERE / name).read_text(encoding="utf-8")
    code = compile(src, "pet_entry.py", "exec", dont_inherit=True, optimize=0)
    return marshal.dumps(code)


def main() -> int:
    if sys.version_info[:2] != (3, 11):
        print("需要 Python 3.11（桌宠自带的版本），当前是", sys.version.split()[0])
        return 2
    blobs = {
        "PATCHED_ENTRY_B64": build("pet_entry_patched.py"),
        "WRAPPER_ENTRY_B64": build("pet_entry_wrapper.py"),
        "MAC_WRAPPER_ENTRY_B64": build("pet_entry_wrapper_mac.py"),
    }
    where = {"MAC_WRAPPER_ENTRY_B64": HERE.parent / "mac" / "install_mac.py"}
    if "--check" in sys.argv:
        ok = True
        for key, blob in blobs.items():
            text = where.get(key, HERE.parent / "install.py").read_text(encoding="utf-8")
            m = re.search(key + r' = \(\n((?:\s+"[A-Za-z0-9+/=]+"\n)+)\)', text)
            embedded = base64.b64decode("".join(re.findall(r'"([A-Za-z0-9+/=]+)"', m.group(1)))) if m else b""
            same = embedded == blob
            ok &= same
            print(key, "一致" if same else "不一致", hashlib.sha256(blob).hexdigest())
        return 0 if ok else 1
    for key, blob in blobs.items():
        b64 = base64.b64encode(blob).decode()
        print(key, len(blob), "bytes sha256", hashlib.sha256(blob).hexdigest())
        print(key + " = (")
        for i in range(0, len(b64), 76):
            print('    "' + b64[i:i + 76] + '"')
        print(")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
