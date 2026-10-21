"""字符画窗口左下角的“AI 对话”入口，以及原桌宠藏起来时对话窗口摆到字符画窗口旁边。"""
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
    from pet_ascii.window import CHAT_BTN_MARGIN, AsciiPetWindow

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    app = QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    rt = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    w = AsciiPetWindow(rt)
    w.show()
    app.processEvents()
    yield types.SimpleNamespace(w=w, app=app, rt=rt, margin=CHAT_BTN_MARGIN)
    dispose(w, rt)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def test_entry_hidden_until_connected_and_sits_bottom_left(win):
    w, b = win.w, win.w.chat_btn
    assert not b.isVisible()                                 # 独立运行（没有原桌宠）时不显示
    calls = []
    w.set_chat_entry(lambda: calls.append(1))
    win.app.processEvents()
    assert b.isVisible() and b.text().startswith(">_")
    assert b.x() == win.margin and b.geometry().bottom() == w.view.height() - win.margin - 1
    b.click()
    assert calls == [1]
    w.set_cell(6)                                            # 字符变小、窗口变小：入口仍贴在左下角
    win.app.processEvents()
    assert b.geometry().bottom() == w.view.height() - win.margin - 1
    w.set_chat_entry(None)
    assert not b.isVisible()


def test_chat_opens_next_to_ascii_window_while_pet_hidden(win, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget

    class FakePet(QWidget):
        def visible_content_rect(self, *a, **k):
            return QRect(5000, 5000, 100, 100)

    class FakeInstance:
        def __init__(self):
            self.win = FakePet()
            self.anchor = None

        def open_chat(self):                                  # 和原桌宠一样：按原桌宠的位置摆对话窗口
            self.anchor = self.win.visible_content_rect()

    mod = types.SimpleNamespace(PetInstance=FakeInstance)
    monkeypatch.setattr(integration, "_state", {**integration._state, "chat_pos_installed": False, "win": win.w})
    integration._install_chat_positioning(mod)
    inst = FakeInstance()
    monkeypatch.setitem(integration._state, "shell", types.SimpleNamespace(_instances=[inst]))
    assert integration.open_pet_chat()
    assert inst.anchor == win.w.geometry()                    # 原桌宠藏着：摆到字符画窗口旁边
    assert inst.win.visible_content_rect().x() == 5000         # 用完恢复
    inst.win.show()
    integration.open_pet_chat()
    assert inst.anchor.x() == 5000                            # 原桌宠显示着：完全照旧
    dispose(inst.win)
