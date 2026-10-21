"""
PyInstaller 主入口（已加装 pet_memory：永久记忆 + 响应提速）。

与原版的唯一区别：在加载 pet.app 之前尝试安装 _internal/pet_memory 里的补丁；
pet_memory 缺失或出错时静默跳过，桌宠按原样运行。删除 _internal/pet_memory 文件夹即可停用。
"""
import sys

try:
    import pet_memory.bootstrap as _pet_memory_bootstrap

    _pet_memory_bootstrap.install()
except Exception:
    pass

if "--settings" in sys.argv:
    from pet.__main__ import _run_settings

    sys.exit(_run_settings())

from pet.app import main

if __name__ == "__main__":
    sys.exit(main())
