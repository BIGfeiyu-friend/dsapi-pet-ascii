"""字符画窗口右边的黑边（v1.3.9）：标题栏里的长文字不会把窗口撑宽；窗口比内容大时会缩回去。"""
import types

import pytest

from pet_ascii import face_rig as fr


@pytest.fixture(scope="module")
def win(tmp_path_factory):
    import os

    from conftest import dispose
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime
    from pet_ascii.window import AsciiPetWindow

    old_home = os.environ.get("PET_ASCII_HOME")
    os.environ["PET_ASCII_HOME"] = str(tmp_path_factory.mktemp("strip"))
    app = QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    cfg.cell_h = 6
    rt = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    w = AsciiPetWindow(rt)
    w.show()
    app.processEvents()
    yield types.SimpleNamespace(w=w, app=app, rt=rt)
    dispose(w, rt)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}
    if old_home is None:
        os.environ.pop("PET_ASCII_HOME", None)
    else:
        os.environ["PET_ASCII_HOME"] = old_home


LONG = "· 睡眠中 zZ  「主人……不要走……我还没把窗外的星星数完呢……再陪我一会儿嘛……」"


def test_long_status_does_not_widen_the_window(win):
    w = win.w
    base = w.width()
    w.title.set_status(LONG)
    for _ in range(5):
        win.app.processEvents()
        w.layout().activate()
    assert w.width() == base and w.layout().minimumSize().width() <= base
    shown = w.title.expr.text()
    assert shown != LONG and shown.endswith("…") and w.title.expr.toolTip() == LONG   # 省略显示，悬停看全文
    w.title.set_status("· 犯困…")
    win.app.processEvents()
    assert w.title.expr.text() == "· 犯困…" and w.title.expr.toolTip() == ""


def test_real_sleep_talk_keeps_the_window_size(win):
    w, sl = win.w, win.rt.sleep
    base = w.width()
    sl.state = "asleep"
    sl.talk_line, sl.talk_until = LONG, win.rt.engine.t + 30
    w.title.refresh()
    for _ in range(5):
        win.app.processEvents()
        w.layout().activate()
    assert w.width() == base
    sl.state, sl.talk_line, sl.talk_until = "awake", "", 0.0


def test_window_wider_than_its_content_snaps_back(win):
    w = win.w
    base = (w.width(), w.height())
    w.resize(base[0] + 93, base[1])                            # 以前被撑宽的样子（右边黑边）
    win.app.processEvents()
    assert w.width() == base[0] + 93
    assert w.fit_to_content() is True
    win.app.processEvents()
    assert (w.width(), w.height()) == base
    assert w.fit_to_content() is False                         # 已经刚好：不动
