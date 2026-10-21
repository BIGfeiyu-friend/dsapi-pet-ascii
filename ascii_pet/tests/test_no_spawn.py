"""删去“生小肥鱼”（remove_spawn_pet）：不再生成、右键菜单去掉两项、设置里那一组藏起来。"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_spawning_is_blocked(monkeypatch):
    from pet_ascii import no_spawn

    calls = []

    class AppShell:
        def spawn_pet(self):
            calls.append("spawn")

        def spawn_in_process_window(self):
            calls.append("in-process")

    assert no_spawn.install_pet(SimpleNamespace(AppShell=AppShell))
    shell = AppShell()
    assert shell.spawn_pet() is None and shell.spawn_in_process_window() is None and calls == []


def test_real_pet_still_has_the_methods_we_replace(monkeypatch):
    """桌宠更新后如果改了名字，这里会先报出来（运行时则只是装不上，桌宠照常）。"""
    app = pytest.importorskip("pet.app")
    monkeypatch.setattr(app.AppShell, "spawn_pet", app.AppShell.spawn_pet)          # 测试结束后还原
    monkeypatch.setattr(app.AppShell, "spawn_in_process_window", app.AppShell.spawn_in_process_window)
    from pet_ascii import no_spawn

    assert no_spawn.install_pet(app)
    assert app.AppShell.spawn_pet is no_spawn._blocked


def test_menu_items_are_removed(qapp):
    from PySide6.QtWidgets import QMenu

    from pet_ascii.no_spawn import prune_menu

    m = QMenu()
    m.addAction("AI 对话")
    m.addSeparator()
    m.addAction("生小肥鱼")
    m.addAction("退出子肥鱼")
    m.addSeparator()
    m.addAction("隐藏桌宠")
    sub = m.addMenu("更多")
    sub.addAction("&生小肥鱼")
    sub.addAction("回到右下角")
    assert prune_menu(m) == 3
    texts = [a.text() for a in m.actions()]
    assert "生小肥鱼" not in texts and "退出子肥鱼" not in texts
    assert [a.isSeparator() for a in m.actions()] == [False, True, False, False]   # 两条分隔线合成一条
    assert [a.text() for a in sub.actions()] == ["回到右下角"]


def test_real_menu_labels_match_what_we_remove():
    """原桌宠菜单里这两项的文字（桌宠更新后改了文字，这里会先报出来）。"""
    shared = pytest.importorskip("pet.context_menus.shared")
    from pet_ascii.no_spawn import SPAWN_MENU_TEXTS

    def strings(fn):
        return [c for c in fn.__code__.co_consts if isinstance(c, str)]

    assert SPAWN_MENU_TEXTS[0] in strings(shared.add_spawn_pet)
    assert SPAWN_MENU_TEXTS[1] in strings(shared.add_clear_spawned_pets)


def build_settings(qapp):
    from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel, QPushButton, QVBoxLayout, QWidget

    class SettingRow(QWidget):
        def __init__(self, child):
            super().__init__()
            QVBoxLayout(self).addWidget(child)

    dlg = QWidget()
    page = QWidget(dlg)
    pl = QVBoxLayout(page)
    fish = QWidget()
    fl = QVBoxLayout(fish)
    fl.addWidget(QLabel("生小肥鱼"))
    dlg.spawn_inherit_size_check = QCheckBox("生小肥鱼继承大小")
    dlg.spawn_scale_combo = QComboBox()
    dlg.spawn_inherit_dynamic_island_check = QCheckBox("生小肥鱼继承灵动岛")
    dlg.clear_spawned_pets_btn = QPushButton("一键退出…")
    for w in (dlg.spawn_inherit_size_check, dlg.spawn_scale_combo, dlg.spawn_inherit_dynamic_island_check,
              dlg.clear_spawned_pets_btn):
        fl.addWidget(SettingRow(w))
    other = QWidget()
    ol = QVBoxLayout(other)
    ol.addWidget(QLabel("多开碰撞"))
    dlg.collision_check = QCheckBox("参与碰撞")
    ol.addWidget(SettingRow(dlg.collision_check))
    pl.addWidget(fish)
    pl.addWidget(other)
    return dlg, fish, other


def test_settings_group_is_hidden_but_nothing_else(qapp):
    from pet_ascii.no_spawn import hide_spawn_settings, leftovers

    dlg, fish, other = build_settings(qapp)
    dlg.show()
    hidden = hide_spawn_settings(dlg)
    assert fish in hidden and fish.isHidden()
    assert not other.isHidden() and not dlg.collision_check.parentWidget().isHidden()
    assert leftovers(dlg, hidden) == []
    dlg.close()


def test_settings_dialog_is_wrapped(qapp):
    from pet_ascii import no_spawn

    class ModernSettingsDialog:
        def __init__(self):
            self.built = True

        def _rebuild_domain_navigation(self):
            return "nav"

    seen = []
    orig = no_spawn._hide_quietly
    no_spawn._hide_quietly = lambda d: seen.append(d)
    try:
        assert no_spawn.install_settings(SimpleNamespace(ModernSettingsDialog=ModernSettingsDialog))
        d = ModernSettingsDialog()
        assert d.built and d._rebuild_domain_navigation() == "nav" and seen == [d, d]
        assert no_spawn.install_settings(SimpleNamespace(ModernSettingsDialog=ModernSettingsDialog))   # 不重复包
    finally:
        no_spawn._hide_quietly = orig


def test_switch_off_keeps_the_feature(monkeypatch):
    from pet_ascii import integration, no_spawn

    monkeypatch.setattr(no_spawn, "enabled", lambda: False)
    called = []
    monkeypatch.setattr(no_spawn, "install_pet", lambda *a: called.append(1))
    integration._install_no_spawn()
    assert called == [] and integration._state["no_spawn"] is False


def test_menu_layout_editor_hides_spawn_entries(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

    from pet_ascii import no_spawn

    class MenuLayoutEditor:
        def __init__(self):
            self.tree, self.preview = QTreeWidget(), QTreeWidget()

        def _on_changed(self):
            self.preview.clear()
            for t in (self.tree, self.preview):
                for ident, label in (("chat", "AI 对话"), ("spawn_pet", "生小肥鱼"), ("clear_spawned_pets", "退出子肥鱼")):
                    item = QTreeWidgetItem(t, [label])
                    item.setData(0, Qt.ItemDataRole.UserRole, {"type": "action", "id": ident})
            return "ok"

    assert no_spawn.install_menu_editor(SimpleNamespace(MenuLayoutEditor=MenuLayoutEditor))
    ed = MenuLayoutEditor()
    assert ed._on_changed() == "ok"
    for t in (ed.tree, ed.preview):
        shown = [t.topLevelItem(i).text(0) for i in range(t.topLevelItemCount()) if not t.topLevelItem(i).isHidden()]
        assert shown == ["AI 对话"]


def test_real_menu_layout_editor_has_the_hook_point():
    mle = pytest.importorskip("pet.settings_menu_layout_editor")
    assert callable(getattr(mle.MenuLayoutEditor, "_on_changed", None))
