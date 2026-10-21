"""右键「大小」调的是字符画窗口（v1.3.7）：档位按实际窗口尺寸标注，点了就变，勾上当前档。"""
import types

import pytest

from pet_ascii import face_rig as fr


@pytest.fixture(scope="module")
def win(tmp_path_factory):
    """三个测试共用一个窗口，结束时立即删掉（不留给垃圾回收在别的测试中途再释放——那样偶尔会让 Qt 堆损坏）。"""
    import os

    from conftest import dispose
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime
    from pet_ascii.window import AsciiPetWindow

    old_home = os.environ.get("PET_ASCII_HOME")
    os.environ["PET_ASCII_HOME"] = str(tmp_path_factory.mktemp("size_menu"))
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


def _labels(menu):
    return [(a.text(), a.isChecked()) for a in menu.actions()]


def test_size_options_match_the_real_window(win):
    from pet_ascii.window import TITLE_H

    w = win.w
    for h in (6, 8):
        w.set_cell(h)
        win.app.processEvents()
        ww, hh = w.size_for_cell(h)
        assert w.view.sizeHint().width() == ww and w.view.sizeHint().height() + TITLE_H == hh


def test_size_menu_lists_sizes_checks_current_and_resizes(win):
    from PySide6.QtWidgets import QMenu

    w = win.w
    w.set_cell(6)
    menu = QMenu()
    menu.addAction("320px")                                      # 原桌宠的旧档位会被清掉
    w.fill_size_menu(menu)
    labels = _labels(menu)
    assert all(t.endswith(" px") and " × " in t for t, _ in labels)
    assert [c for _, c in labels].count(True) == 1
    assert labels[[t for t, _ in labels].index("%d × %d px" % w.size_for_cell(6))][1] is True
    before = w.height()
    bigger = next(a for a in menu.actions() if a.text() == "%d × %d px" % w.size_for_cell(8))
    bigger.trigger()
    win.app.processEvents()
    assert win.rt.cfg.cell_h == 8 and w.height() > before


def test_sizes_that_do_not_fit_the_screen_are_left_out(win):
    from PySide6.QtWidgets import QMenu

    menu = QMenu()
    win.w.fill_size_menu(menu, avail_height=720)                   # 不替换窗口的方法（会留下引用环，拖到别的测试里才释放）
    heights = [int(a.text().split("×")[1].split()[0]) for a in menu.actions()]
    assert heights and max(heights) <= 720 - 20
