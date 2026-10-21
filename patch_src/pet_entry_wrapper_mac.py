"""
PyInstaller 主入口（macOS 版，已加装扩展，通用包装版，由 mac/install_mac.py 生成）。

扩展装在 ~/Library/Application Support/dsh-pet-ascii（不放进 .app 里面）：先把这个文件夹加进导入路径，
尝试安装 pet_memory 里的补丁，再原样运行原版入口（原版入口的字节码就存在本文件的常量里）。
扩展缺失或出错时静默跳过，桌宠按原样运行。删除那个文件夹即可停用。
"""
import sys

try:
    import os as _pet_os

    _pet_addons = _pet_os.path.join(_pet_os.path.expanduser("~"), "Library", "Application Support", "dsh-pet-ascii")
    if _pet_os.path.isdir(_pet_os.path.join(_pet_addons, "pet_memory")) and _pet_addons not in sys.path:
        sys.path.insert(0, _pet_addons)
    import pet_memory.bootstrap as _pet_memory_bootstrap

    _pet_memory_bootstrap.install()
except Exception:
    pass

import marshal as _pet_marshal

exec(_pet_marshal.loads(b"@@DSH_PET_ASCII_ORIGINAL_PET_ENTRY_PLACEHOLDER@@"))
