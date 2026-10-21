"""
PyInstaller 主入口（已加装 pet_memory，通用包装版，由 install.py 生成）。

先尝试安装 _internal/pet_memory 里的补丁，再原样运行原版入口（原版入口的字节码就存在本文件的常量里）。
pet_memory 缺失或出错时静默跳过，桌宠按原样运行。删除 _internal/pet_memory 文件夹即可停用。
"""
import sys

try:
    import pet_memory.bootstrap as _pet_memory_bootstrap

    _pet_memory_bootstrap.install()
except Exception:
    pass

import marshal as _pet_marshal

exec(_pet_marshal.loads(b"@@DSH_PET_ASCII_ORIGINAL_PET_ENTRY_PLACEHOLDER@@"))
