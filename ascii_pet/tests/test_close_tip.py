"""关掉字符画窗口时的托盘提示：桌宠还在后台、怎么再打开（右键托盘 →「显示 / 隐藏」）、怎么彻底退出。"""
from types import SimpleNamespace

import pytest


class FakeTray:
    def __init__(self, visible=True):
        self.visible = visible
        self.messages = []

    def isVisible(self):
        return self.visible

    def showMessage(self, title, text, icon=None, ms=0):
        self.messages.append((title, text, ms))


@pytest.fixture
def integ(monkeypatch):
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    from pet_ascii import integration

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QSystemTrayIcon, "supportsMessages", staticmethod(lambda: True))
    tray = FakeTray()
    rt = SimpleNamespace(cfg=SimpleNamespace(close_tray_tip=True), pet_name=lambda: "DeepSeek娘")
    for k, v in {"shell": SimpleNamespace(tray=tray), "rt": rt, "win": None}.items():
        monkeypatch.setitem(integration._state, k, v)
    return integration, tray, rt


def test_tip_says_she_is_still_running_and_how_to_reopen(integ):
    integration, tray, _rt = integ
    assert integration._show_close_tip()
    title, text, ms = tray.messages[0]
    assert title == "DeepSeek娘还在后台"
    assert "后台运行" in text and "「显示 / 隐藏」" in text and "「退出」" in text and ms >= 5000


def test_no_tip_when_the_window_is_back_or_tray_missing(integ, monkeypatch):
    integration, tray, _rt = integ
    monkeypatch.setitem(integration._state, "win", object())      # 换样式重建 / 马上又打开了
    assert not integration._show_close_tip()
    monkeypatch.setitem(integration._state, "win", None)
    tray.visible = False
    assert not integration._show_close_tip()
    monkeypatch.setitem(integration._state, "shell", None)
    assert not integration._show_close_tip()
    assert tray.messages == []


def test_tip_is_delayed_and_can_be_turned_off(integ, monkeypatch):
    integration, tray, rt = integ
    from PySide6.QtCore import QTimer

    scheduled = []
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: scheduled.append((ms, fn))))
    integration._schedule_close_tip()
    assert scheduled == [(integration.CLOSE_TIP_DELAY_MS, integration._show_close_tip)]
    rt.cfg.close_tray_tip = False
    integration._schedule_close_tip()
    assert len(scheduled) == 1


def test_closing_the_window_schedules_the_tip(integ, monkeypatch):
    integration, tray, _rt = integ
    calls = []
    monkeypatch.setattr(integration, "_schedule_close_tip", lambda: calls.append(1))
    monkeypatch.setattr(integration, "_release_island", lambda: None)
    monkeypatch.setattr(integration, "_keep_pet_hidden", lambda: True)
    integration._on_ascii_closed()
    assert calls == [1] and integration._state["win"] is None
