"""原桌宠：默认隐藏 + 右键菜单整合进字符画窗口。用一个和原桌宠方法名相同的假桌宠窗口验证接管逻辑。"""
import types

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii import integration


@pytest.fixture
def env(tmp_path, monkeypatch):
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication, QMenu, QWidget

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime
    from pet_ascii.window import AsciiPetWindow

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(integration, "_state", {**integration._state, "pet_menu_installed": False,
                                                "pet_hidden_by_us": False, "ascii_menu_until": 0.0})
    mod = types.SimpleNamespace()

    class FakePet(QWidget):
        """和原桌宠 PetWindow 同名的几个方法：按自己的位置摆菜单、调用模块级 _populate_context_menu、勾选项重开菜单。"""
        def visible_content_rect(self, *a, **k):
            return QRect(5000, 5000, 100, 100)

        def _screen_available(self, *a, **k):
            return "pet-screen"

        def _show_context_menu(self, global_pos):
            menu = QMenu(self)
            mod._populate_context_menu(menu, self)
            self.last_menu = menu
            self.last_items = [a.text() for a in menu.actions()]
            self.last_rect = self.visible_content_rect()

        def reopen_context_menu(self, menu):
            self._show_context_menu(None)

    def populate(menu, pet):
        menu.addAction("聊天")
        menu.addAction("退出")

    mod.PetWindow = FakePet
    mod._populate_context_menu = populate
    pet = FakePet()

    class Shell:
        _instances = [types.SimpleNamespace(win=pet)]

        def _toggle_primary_pet_visible(self):
            pet.setVisible(not pet.isVisible())

    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    rt = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    win = AsciiPetWindow(rt, on_close=integration._on_ascii_closed)
    win.pet_menu = integration.pet_menu_from_ascii
    win.pet_toggle = integration._pet_toggle_item
    integration._state.update(shell=Shell(), rt=rt, win=win)
    integration._install_pet_menu(mod)
    yield types.SimpleNamespace(pet=pet, win=win, rt=rt, mod=mod)
    win.on_close = None                             # 收尾时关窗不再走 integration 的关窗流程（托盘提示定时器等）
    dispose(win, rt, pet)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def test_hide_and_show_original_pet(env):
    env.pet.show()
    integration.set_pet_visible(False)
    assert not env.pet.isVisible() and integration._state["pet_hidden_by_us"]
    label, fn = integration._pet_toggle_item()
    assert label == "显示原来的桌宠"
    fn()                                                     # 菜单里点“显示原来的桌宠”：显示，并记住选择
    assert env.pet.isVisible() and env.rt.cfg.hide_original_pet is False
    assert integration._pet_toggle_item()[0] == "隐藏原来的桌宠"


def test_menu_from_ascii_window_is_pet_menu_plus_ascii_items(env):
    env.win.move(300, 200)
    assert integration.pet_menu_from_ascii(env.win.geometry().center())
    items = env.pet.last_items
    assert items[0].startswith("👍") and items[1].startswith("👎") and items[2] == "切换表情" and items[3] == "字符画"
    assert items[4] == "" and items[5:] == ["聊天", "退出"]      # 切换表情和“字符画”同一层；原桌宠菜单完整保留在后面
    assert env.pet.last_rect == env.win.geometry()            # 菜单按字符画窗口的位置摆放
    assert env.pet.visible_content_rect().x() == 5000         # 用完恢复原样
    sub = next(a.menu() for a in env.pet.last_menu.actions() if a.text() == "字符画")
    texts = [a.text() for a in sub.actions()]
    assert "切换表情" not in texts and "关闭字符画窗口" in texts         # 切换表情已经提到上一层
    assert any(t in ("显示原来的桌宠", "隐藏原来的桌宠") for t in texts)
    env.pet.reopen_context_menu(env.pet.last_menu)            # 勾选项重开菜单：仍然带字符画选项、按字符画窗口摆放
    assert env.pet.last_items[3] == "字符画" and env.pet.last_rect == env.win.geometry()


def test_right_click_on_original_pet_unchanged(env):
    env.pet._show_context_menu(None)
    assert env.pet.last_items == ["聊天", "退出"] and env.pet.last_rect.x() == 5000


def test_original_pet_stays_hidden_and_tray_reopens_ascii(env):
    from PySide6.QtWidgets import QApplication

    env.rt.cfg.attach_to_pet = True                     # 随桌宠运行（默认 hide_original_pet=True）
    env.pet.show()
    integration.set_pet_visible(False)
    integration._install_pet_show_guard()
    env.win.show()
    env.win.close()                                     # 关掉字符画窗口：原来的桌宠不再弹出来
    assert not env.pet.isVisible() and integration._state["win"] is None
    env.pet.show()                                      # 托盘的“显示 / 隐藏”、灵动岛的“显示桌宠”都是这样调用的
    QApplication.processEvents()
    assert not env.pet.isVisible()                      # 仍然隐藏……
    reopened = integration._state["win"]
    assert reopened is not None and reopened is not env.win and reopened.isVisible()   # ……改为重新打开字符画窗口
    integration.set_pet_visible(True, remember=True)    # 只有在字符画菜单里明确选“显示原来的桌宠”才显示
    assert env.pet.isVisible() and env.rt.cfg.hide_original_pet is False
    integration.set_pet_visible(False, remember=True)
    env.pet.show()                                      # 又选回隐藏：照样被拦下
    QApplication.processEvents()
    assert not env.pet.isVisible()
    reopened.on_close = None
    dispose(reopened)


def test_expression_menu_lists_everything_and_scrolls_with_wheel(env):
    """切换表情：内置 + 全部自创表情（超过 30 个也都在），限高、滚轮能上下滑动，现在的表情打勾并滚到它那里。"""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QMenu, QStyle

    from pet_ascii.window import ScrollMenu

    import time

    store = env.rt.custom
    for i in range(36):                     # 直接放进库里（add() 会把太像的合并掉）
        store.items[f"测试表情{i:02d}"] = {"spec": fr.normalize({"preset": "smile", "blush": i / 40}), "origin": "chat",
                                            "note": "测试", "updated": time.time() - i, "uses": 0}
    store._commit()
    env.rt.apply_preset("测试表情00", blend_ms=0)
    m = QMenu()
    env.win.fill_ascii_menu(m)
    sub = next(a.menu() for a in m.actions() if a.text() == "切换表情")
    assert isinstance(sub, ScrollMenu)
    sub.max_frac = 0.3          # 比屏幕矮、但比限高高：Qt 自带的滚动不会启用（真实桌宠里就是这种情况），要靠 ScrollMenu 自己滚
    acts = sub.actions()
    items = [a for a in acts if not a.isSeparator()]
    assert len(items) == len(fr.PRESETS) + 36
    assert [a.text() for a in items if a.isChecked()] == [fr.display_name("测试表情00")]
    sub.popup(QPoint(10, 10))
    QApplication.processEvents()
    screen_h = sub.screen().availableGeometry().height()
    assert sub.height() <= int(screen_h * 0.3) + 2 and sub.sizeHint().height() < screen_h
    visible = [i for i, a in enumerate(acts) if a.isVisible()]
    assert 5 <= len(visible) < len(acts) and visible == list(range(visible[0], visible[-1] + 1))   # 连续的一段
    checked = next(a for a in acts if a.isChecked())
    assert checked.isVisible() and sub.rect().contains(sub.actionGeometry(checked))  # 打开时现在的表情就在窗口里
    sub.scroll_to(0)
    QApplication.processEvents()
    top0 = sub._top
    QApplication.sendEvent(sub, QWheelEvent(QPointF(20, 60), QPointF(30, 70), QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                                            Qt.NoModifier, Qt.NoScrollPhase, False))
    QApplication.processEvents()
    assert sub._top == top0 + 3 and not acts[0].isVisible()                       # 一格滚轮往下 3 项
    assert sub.scroll_state() == (True, True)
    for _ in range(40):
        QApplication.sendEvent(sub, QWheelEvent(QPointF(20, 60), QPointF(30, 70), QPoint(0, 0), QPoint(0, -120),
                                                Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
    QApplication.processEvents()
    assert acts[-1].isVisible() and sub.rect().contains(sub.actionGeometry(acts[-1]))   # 能滚到最后一个
    assert sub.scroll_state() == (True, False)
    acts[-1].trigger()                                                          # 点最后一个自创表情：切过去
    assert fr.display_name(env.rt.current_expression()) == acts[-1].text()
    sub.close()
    QApplication.processEvents()
    assert all(a.isVisible() for a in acts)                                     # 关上后恢复（不影响别处遍历菜单项）


def test_closing_lab_keeps_the_pet_window_animating(env, caplog):
    """随桌宠运行时，开发测试台和字符画窗口共用一个 Runtime：关掉测试台不能停掉帧循环（以前会停，画面卡住）。"""
    env.rt.start()
    integration._state["lab"] = None
    integration._open_lab()
    lab = integration._state["lab"]
    assert lab.view in env.rt.views
    lab.close()
    assert env.rt.timer.isActive() and env.rt.sleep_timer.isActive()           # 帧循环、睡眠检查都还在跑
    assert env.win.view in env.rt.views and lab.view not in env.rt.views
    integration._open_lab()                                                     # 再打开：画面重新接上
    assert lab.view in env.rt.views
    lab.close()
    integration._state["lab"] = None
    # 帧循环出错：记进日志、不抛出（以前在无控制台的桌宠里会悄悄停住）
    import logging

    def boom(dt):
        raise RuntimeError("测试用的帧错误")

    real = env.rt.engine.tick
    env.rt.engine.tick = boom
    with caplog.at_level(logging.ERROR, logger="pet.ascii"):
        env.rt._tick()
        env.rt._tick()
    env.rt.engine.tick = real
    assert sum("帧循环出错" in r.getMessage() for r in caplog.records) == 1    # 同一种错误一分钟只记一次
    env.rt.timer.stop()


def test_submenus_open_to_the_left(env):
    """每一级子菜单都往左展开（不挡住右边的字符画）；左边放不下时才保持在右边。"""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication, QMenu

    top = QMenu()
    env.win.fill_ascii_menu(top)
    sub = next(a.menu() for a in top.actions() if a.text() == "背景")
    helper = env.win.left_menus(top, anchor=QRect(700, 100, 300, 300))
    top.popup(QPoint(600, 120))
    QApplication.processEvents()
    assert top.x() + top.width() <= 700 - 2                     # 顶层菜单在 anchor（字符画窗口）左边
    act = next(a for a in top.actions() if a.menu() is sub)
    sub.popup(top.mapToGlobal(top.actionGeometry(act).topRight()))   # 和 Qt 一样先按右边弹出
    QApplication.processEvents()
    assert sub.x() + sub.width() <= top.x() + helper.OVERLAP     # 被挪到父菜单左边
    assert abs(sub.y() + sub.actionGeometry(sub.actions()[0]).top() - top.mapToGlobal(top.actionGeometry(act).topLeft()).y()) <= 2
    sub.close()
    top.close()
    QApplication.processEvents()
    # 左边放不下：保持右边
    top2 = QMenu()
    env.win.fill_ascii_menu(top2)
    sub2 = next(a.menu() for a in top2.actions() if a.text() == "背景")
    env.win.left_menus(top2)
    top2.popup(QPoint(0, 120))
    QApplication.processEvents()
    act2 = next(a for a in top2.actions() if a.menu() is sub2)
    sub2.popup(top2.mapToGlobal(top2.actionGeometry(act2).topRight()))
    QApplication.processEvents()
    assert sub2.x() >= top2.x() + top2.width() - 10
    sub2.close()
    top2.close()


def test_standalone_window_falls_back_to_own_menu(env):
    env.win.pet_menu = lambda pos: False                      # 拿不到原桌宠（例如 --ascii-pet 独立运行）
    from PySide6.QtWidgets import QMenu

    m = QMenu()
    env.win.fill_ascii_menu(m)
    assert "说点什么 / 输入表达意图…" == m.actions()[0].text()
