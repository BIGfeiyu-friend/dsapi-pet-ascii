"""字符画窗口是普通顶层窗口：任务栏里有图标（她现在的脸），— 是最小化到任务栏，托盘 / 灵动岛能把最小化的窗口恢复。"""
import types

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii import integration


@pytest.fixture
def win(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime
    from pet_ascii.window import AsciiPetWindow

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    rt = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    w = AsciiPetWindow(rt)
    monkeypatch.setattr(integration, "_state", {**integration._state, "win": w, "rt": rt})
    w.show()
    QApplication.processEvents()
    yield w
    dispose(w, rt)
    fr.CUSTOM = {}


def test_window_is_a_real_taskbar_window(win):
    from PySide6.QtCore import Qt

    flags = win.windowFlags()
    assert not (flags & Qt.Tool) == Qt.Tool                     # 不再是工具窗口（工具窗口不进任务栏）
    assert flags & Qt.WindowMinimizeButtonHint and flags & Qt.WindowSystemMenuHint   # 点任务栏图标能最小化 / 恢复
    assert flags & Qt.FramelessWindowHint                        # 外观不变：仍然没有系统边框
    win.toggle_pin()                                             # 切换置顶后仍是普通窗口
    assert not (win.windowFlags() & Qt.Tool) == Qt.Tool and win.windowFlags() & Qt.WindowMinimizeButtonHint
    win.toggle_pin()


def test_dash_button_minimizes_and_tray_restores(win):
    from PySide6.QtWidgets import QApplication

    assert "min" in win.title.btns and "fold" not in win.title.btns
    assert "最小化" in win.title.btns["min"].toolTip()
    win.title.btns["min"].click()
    QApplication.processEvents()
    assert win.isMinimized()
    integration.open_ascii_window()                              # 托盘的“显示 / 隐藏”、灵动岛的“显示桌宠”
    QApplication.processEvents()
    assert not win.isMinimized() and win.isVisible() and integration._state["win"] is win


def test_fold_moved_to_menu_and_title_double_click(win):
    from PySide6.QtWidgets import QMenu

    m = QMenu()
    win.fill_ascii_menu(m)
    texts = [a.text() for a in m.actions()]
    assert any(t.startswith("收起画面") for t in texts) and "最小化到任务栏" in texts
    win.toggle_fold()
    m2 = QMenu()
    win.fill_ascii_menu(m2)
    assert "展开画面" in [a.text() for a in m2.actions()]
    win.toggle_fold()


def test_taskbar_icon_is_her_face_and_follows_sleep(win):
    win._refresh_icon(force=True)
    icon = win.windowIcon()
    assert not icon.isNull() and icon.availableSizes()[0].width() == 64
    first = win._icon_key
    win.rt.sleep.fall_asleep("yawn")
    for _ in range(140):
        win.rt.engine.tick(0.1)
        win.rt.sleep.tick()
    c, k, w, h, off = win.rt.engine.frame()
    win.view.set_frame(c, k, w, h, off)
    win._refresh_icon()
    assert win._icon_key != first and win._icon_key[1] == "asleep"     # 睡着了：任务栏图标换成睡脸
