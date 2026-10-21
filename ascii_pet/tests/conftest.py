import collections
import gc
import os
import sys
import tempfile
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 测试数据写到临时目录，不碰真实配置/缓存
os.environ["PET_ASCII_HOME"] = tempfile.mkdtemp(prefix="pet_ascii_test_")
# 桌宠记忆库默认指向一个不存在的文件：测试绝不读到真实的聊天记忆（需要的测试自己建临时库）
os.environ["PET_ASCII_MEMORY_DB"] = os.path.join(os.environ["PET_ASCII_HOME"], "no_such_memory.db")


# ---------------------------------------------------------------------- 确定性收尾（v1.3.7）
# 只 close() / stop() 不够：C++ 窗口、定时器留给 Python 垃圾回收，回收发生在之后随便哪个测试的中途，
# 而且是在触发回收的那个线程里析构（可能是后台线程）。每个 Runtime 还留下一个永远不退出的调度线程。
# 积累多了全量测试偶尔 Windows 堆损坏崩溃（0xc0000374，退出码 127）。建了窗口 / Runtime 的夹具都用 dispose() 收尾。
def dispose(*objs, timeout: float = 2.0) -> None:
    """按 关窗 → 停 Runtime → 停后台线程 → deleteLater → 立即处理 DeferredDelete 的顺序拆掉 objs。

    认得：QWidget / QObject、Runtime（连同它的调度线程、表情库写盘线程、房间对象）、Director、CustomPresets。
    None 和已经删掉的对象跳过，重复调用无害。"""
    import shiboken6
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QApplication, QWidget

    from pet_ascii.ai.director import Director
    from pet_ascii.custom_presets import CustomPresets
    from pet_ascii.runtime import Runtime

    def alive(o):
        return not isinstance(o, QObject) or shiboken6.isValid(o)

    objs = [o for o in objs if o is not None and alive(o)]
    for o in objs:                                   # 先关窗：closeEvent 里还要用 Runtime（remove_view / on_close）
        if isinstance(o, QWidget):
            o.close()
    qobjs, stuck = [], []
    for o in objs:
        if isinstance(o, Runtime):
            o.stop()
            if not o.director.stop(timeout):
                stuck.append("pet-ascii-director")
            if not o.custom.close(timeout):
                stuck.append("pet-ascii-presets-save")
            if isinstance(o.room, QObject) and alive(o.room):   # RoomLife 没有父对象，和 Runtime 互相引用：只能靠循环回收
                qobjs.append(o.room)
        elif isinstance(o, Director):
            if not o.stop(timeout):
                stuck.append("pet-ascii-director")
        elif isinstance(o, CustomPresets):
            if not o.close(timeout):
                stuck.append("pet-ascii-presets-save")
        if isinstance(o, QObject):
            qobjs.append(o)
    app = QApplication.instance()
    if qobjs and app is not None:
        for o in qobjs:
            if alive(o):
                o.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    for o in qobjs:                                  # 万一 DeferredDelete 没处理（嵌套事件循环里调用）：直接删
        if alive(o):
            shiboken6.delete(o)
    assert not stuck, f"dispose：后台线程 {timeout}s 内没有退出：{stuck}"


@pytest.fixture
def disposer():
    """测试函数里直接 new 的窗口 / Runtime：`rt = disposer(Runtime(...))`，测试结束（失败也算）时 dispose()。"""
    objs = []

    def track(obj):
        objs.append(obj)
        return obj

    yield track
    dispose(*objs)


def _py_owned_top_levels():
    """Python 自己拥有的顶层窗口（没有父对象、在测试里 new 出来的）：它们本来就等着垃圾回收在随机时刻释放。
    Qt 自己的（提示框等）和别人拥有的不算，绝不能删。"""
    import shiboken6
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return {}
    out = {}
    for w in QApplication.topLevelWidgets():
        if shiboken6.isValid(w) and shiboken6.ownedByPython(w):
            out[shiboken6.getCppPointer(w)[0]] = w
    return out


def _count_threads(name: str) -> int:
    return sum(1 for t in threading.enumerate() if t.name == name)


@pytest.fixture(autouse=True)
def _no_qt_leaks(request):
    """每个测试的保险：
    - 测试里直接 new、没交给 dispose() 的顶层窗口（菜单、测试台…）在这里确定性删掉，并在界面线程里做一次循环回收；
    - 测试多出来的调度线程 = 有 Runtime / Director 没 dispose()：直接报错，免得漏网的又攒起来。"""
    before = set(_py_owned_top_levels())
    directors = _count_threads("pet-ascii-director")
    yield
    leftover = [w for k, w in _py_owned_top_levels().items() if k not in before]
    if leftover:
        dispose(*leftover)
    gc.collect()                                     # 剩下的循环垃圾在界面线程里回收，不留给后台线程
    grown = _count_threads("pet-ascii-director") - directors
    assert grown <= 0, f"{request.node.nodeid} 留下了 {grown} 个调度线程：Runtime / Director 要用 conftest.dispose() 收尾"


def pytest_terminal_summary(terminalreporter):
    """测试结束时还活着的后台线程（ASCII 输出：控制台是 GBK 时中文会乱码）。正常应为 0 或只有桌宠自带模块的几个。"""
    names = collections.Counter(t.name for t in threading.enumerate() if t is not threading.main_thread())
    terminalreporter.write_line(f"leftover threads: {sum(names.values())} {dict(names) or ''}".rstrip())
