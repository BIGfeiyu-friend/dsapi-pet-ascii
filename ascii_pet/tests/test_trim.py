"""去掉字符画用不到的原桌宠菜单项（v1.3.6，trim.py）。"""
import pytest


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pet_menu():
    """照着原桌宠菜单的样子搭一个：彩蛋、分隔线、要留的项、要去的项、子菜单。"""
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QWidget, QWidgetAction

    menu = QMenu()
    egg = QWidgetAction(menu)
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.addWidget(QLabel("厉害了我的鲸"))
    lay.addWidget(QLabel("请点击"))
    egg.setDefaultWidget(w)
    menu.addAction(egg)
    menu.addSeparator()
    menu.addAction("AI 对话")
    menu.addAction("看看屏幕")
    menu.addSeparator()
    for t in ("播放动画", "切换角色", "播放速率"):
        menu.addMenu(t)
    menu.addMenu("大小")
    menu.addMenu("音乐")
    menu.addSeparator()
    for t in ("桌宠控制", "快捷启动", "工具与帮助", "Agent 联动", "主动识屏"):
        menu.addMenu(t)
    menu.addAction("待办提醒")
    menu.addSeparator()
    menu.addAction("桌宠设置")
    menu.addAction("退出")
    return menu


def _texts(menu):
    return ["|" if a.isSeparator() else a.text() for a in menu.actions()]


def test_unused_items_and_easter_egg_are_removed(app):
    from pet_ascii.trim import prune_top_menu

    menu = _pet_menu()
    removed = prune_top_menu(menu)
    assert len(removed) == 11
    assert _texts(menu) == ["AI 对话", "看看屏幕", "|", "大小", "|", "桌宠设置", "退出"]


def test_submenu_items_with_the_same_name_are_kept(app):
    """字符画子菜单里的 Spotify / “音乐”之类不受影响：只看顶层。"""
    from PySide6.QtWidgets import QMenu

    from pet_ascii.trim import prune_top_menu

    menu = QMenu()
    sub = menu.addMenu("字符画")
    sub.addAction("音乐")
    sub.addAction("Spotify（让她可以放歌）")
    menu.addMenu("音乐")
    prune_top_menu(menu)
    assert _texts(menu) == ["字符画"] and _texts(sub) == ["音乐", "Spotify（让她可以放歌）"]


def test_size_menu_is_retargeted_to_the_ascii_window(app):
    from PySide6.QtWidgets import QMenu

    from pet_ascii.trim import retarget_size_menu

    menu = QMenu()
    other = menu.addMenu("字符画")
    other.addAction("字符大小")
    size = menu.addMenu("大小")
    size.addAction("320px")
    calls = []

    class Win:
        def fill_size_menu(self, m):
            calls.append(m)
            m.clear()
            m.addAction("452 × 520 px")

    assert retarget_size_menu(menu, Win()) is True
    assert calls == [size] and _texts(size) == ["452 × 520 px"] and _texts(other) == ["字符大小"]
    assert retarget_size_menu(QMenu(), Win()) is False


def test_switch_off_keeps_menu(app, tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    from pet_ascii import trim

    assert trim.enabled() is True
    (tmp_path / "config.json").write_text(json.dumps({"trim_unused_menu": False}), encoding="utf-8")
    assert trim.enabled() is False
