"""灵动岛收进字符画窗口右下角的按钮（点击弹出卡片）：用一个方法名和原灵动岛相同的假灵动岛验证转发、隐藏、余额常驻和还原。"""
import types

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii import integration
from pet_ascii.island_bar import IslandBar, is_balance_text


def test_balance_text_detection():
    assert is_balance_text("余额 ¥23.45") and is_balance_text("¥ 0.50（赠送 ¥10）") and is_balance_text("12.30 CNY")
    assert not is_balance_text("查询中…") and not is_balance_text("刚刚查询过，稍后自动更新") and not is_balance_text("余额 --")
    from pet_ascii.island_bar import short_balance

    assert short_balance("余额 ¥12.34（充值 ¥12.34 / 赠送 ¥0.00）") == "余额 ¥12.34"   # 明细放到悬停提示
    assert short_balance("余额 ¥8.88") == "余额 ¥8.88"


def test_bar_keeps_balance_when_status_arrives():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    bar = IslandBar(None, pricing_tier=lambda: "idle")
    bar.set_balance("闲时 · 08:30 切到峰时", "查询中…")
    assert bar.balance_label.text() == "查询中…"                  # 还没查到余额：先显示状态
    bar.set_balance("闲时 · 08:30 切到峰时", "余额 ¥23.45")
    bar.set_balance("闲时 · 08:30 切到峰时", "刚刚查询过，稍后自动更新")
    assert bar.balance_label.text() == "余额 ¥23.45"              # 状态不会盖掉余额
    assert "刚刚查询过" in bar.status_label.full_text() and "刚刚" in bar.status_label.full_text()
    assert bar.tier_label.full_text().startswith("闲时")
    bar.set_message("（摸摸头）辛苦啦\n早点睡")
    assert bar.msg_label.full_text() == "› （摸摸头）辛苦啦\n早点睡"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QWidget

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime
    from pet_ascii.window import AsciiPetWindow

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(integration, "_state", {**integration._state, "island_mirror_installed": False,
                                                "island_integrated": False, "island_bar": None,
                                                "pet_hidden_by_us": False})

    class FakeIsland(QWidget):
        """和桌宠 DynamicIsland 同名的几个方法和属性。"""
        def __init__(self):
            super().__init__()
            self._balance_text, self._balance_tier_text, self._last_message = "余额 ¥8.88", "峰时 · 00:30 切到闲时", ""
            self.events = []

        def set_balance_info(self, tier_text, balance_text):
            self._balance_tier_text, self._balance_text = tier_text, balance_text

        def set_last_message(self, text):
            self._last_message = text

        def notify_event(self, kind):
            self.events.append(kind)

        def set_pet_visible(self, visible):
            self.pet_visible = visible

    mod = types.SimpleNamespace(DynamicIsland=FakeIsland)
    island = FakeIsland()
    island.show()

    class Shell:
        def __init__(self):
            self.island = island
            self.refreshes = 0
            self.settings_opened = 0

        def _quiet_balance_refresh(self):
            self.refreshes += 1
            self.island.set_balance_info("峰时 · 00:30 切到闲时", "余额 ¥7.77")

        def _open_settings_from_island(self):
            self.settings_opened += 1

        def _island_tier_hint(self):
            return "峰时 · 00:30 切到闲时"

    shell = Shell()
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    rt = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    win = AsciiPetWindow(rt, on_close=integration._on_ascii_closed)
    win.set_chat_entry(lambda: None)
    integration._state.update(shell=shell, rt=rt, win=win)
    integration._install_island_mirror(mod)
    yield types.SimpleNamespace(island=island, shell=shell, win=win, rt=rt)
    win.on_close = None                             # 收尾时关窗不再走 integration 的关窗流程（托盘提示定时器等）
    dispose(win, rt, island, integration._state.get("island_bar"))
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def test_attach_puts_island_behind_a_button(env):
    h0 = env.win.sizeHint().height()
    assert integration.attach_island_bar(env.win)
    bar, popup = env.win.island_bar, env.win.island_popup
    assert bar is not None and popup is not None and bar.card
    assert env.win.sizeHint().height() == h0                                # 窗口不变高：内容收在按钮里
    assert env.win.island_btn.isVisibleTo(env.win) and env.win.chat_btn.isVisibleTo(env.win)
    btn, view = env.win.island_btn.geometry(), env.win.view.geometry()
    assert btn.right() > view.width() * 0.7 and btn.bottom() > view.height() * 0.8   # 在右下角
    assert not popup.isVisible() and not bar.chat_btn.isVisibleTo(bar)     # 平时收着；卡片里不重复“>_ 对话”
    assert not env.island.isVisible()                                      # 屏幕上的灵动岛藏起来了
    env.island.show()                                                      # 桌宠自己再 show 也保持隐藏
    assert not env.island.isVisible()
    assert bar.balance_label.text() == "余额 ¥8.88"                        # 现有余额已经搬进卡片
    env.win.toggle_island_popup()                                          # 点击按钮：弹出，并查一次余额
    assert popup.isVisible() and env.shell.refreshes == 1 and bar.balance_label.text() == "余额 ¥7.77"
    env.island.set_balance_info("峰时 · 00:30 切到闲时", "余额 ¥6.66")    # 灵动岛的数据转发到卡片里
    env.island.set_last_message("今天也辛苦啦")
    assert bar.balance_label.text() == "余额 ¥6.66" and "今天也辛苦啦" in bar.msg_label.full_text()
    bar.settings_btn.click()
    assert env.shell.settings_opened == 1
    import time

    env.win.toggle_island_popup()                                          # 再点一下：收起
    assert not popup.isVisible()
    popup.closed_at = time.monotonic() - 1
    env.island.notify_event("balance")                                    # 有新事件：按钮短暂变粉
    assert "#ff8cc0" in env.win.island_btn.styleSheet()


def test_toggle_off_and_close_restore_island(env):
    integration.attach_island_bar(env.win)
    on, fn = integration._island_toggle_item()
    assert on
    fn(False)                                                              # 菜单里关掉：按钮去掉，灵动岛回到屏幕上
    assert env.win.island_bar is None and not env.win.island_btn.isVisibleTo(env.win)
    assert env.island.isVisible() and env.rt.cfg.integrate_island is False
    assert env.win.chat_btn.isVisibleTo(env.win)
    fn(True)
    assert env.win.island_bar is not None and not env.island.isVisible()
    env.win.show()
    env.win.close()                                                        # 字符画窗口关了：灵动岛回到屏幕上
    assert env.island.isVisible()
