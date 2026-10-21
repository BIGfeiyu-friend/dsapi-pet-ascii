"""删去“生小肥鱼”（v1.1.3，字符画设置 remove_spawn_pet，默认开；关掉后重启桌宠即恢复原样）。

不改桌宠自己的文件，运行时处理三处：
  1. 生小肥鱼本身：AppShell.spawn_pet / spawn_in_process_window 换成什么都不做（在桌宠接好菜单回调之前换掉，
     所以菜单、快捷键、设置里任何走到这里的入口都不会再生出小肥鱼）
  2. 右键菜单：去掉「生小肥鱼」「退出子肥鱼」两项（字符画窗口里整合的原桌宠菜单）
  3. 桌宠设置：隐藏“生小肥鱼”一组（继承大小、小肥鱼大小、继承灵动岛、一键退出子肥鱼）；
     设置窗口有时在桌宠进程里打开，有时是单独的 --settings 进程，两处都处理
已经存在的小肥鱼配置（config-slot-N.json、sessions-slot-N）一概不动。
"""
from __future__ import annotations

import logging

log = logging.getLogger("pet.ascii")

SPAWN_MENU_TEXTS = ("生小肥鱼", "退出子肥鱼")
SPAWN_SETTING_WIDGETS = ("spawn_inherit_size_check", "spawn_scale_combo", "spawn_inherit_dynamic_island_check",
                         "clear_spawned_pets_btn")
SPAWN_SECTION_TITLE = "生小肥鱼"


def enabled() -> bool:
    try:
        from .config import AsciiConfig

        return bool(AsciiConfig.load().remove_spawn_pet)
    except Exception:
        return True


# ---------------------------------------------------------------------- 1. 生小肥鱼本身
def _blocked(self, *a, **k):
    log.info("字符画：「生小肥鱼」已删去（字符画设置 remove_spawn_pet），这次没有生成")
    return None


def install_pet(app_mod=None) -> bool:
    """在桌宠进程里调用（AppShell 创建之前）。返回是否装上。"""
    if app_mod is None:
        from pet import app as app_mod  # type: ignore
    shell = app_mod.AppShell
    done = False
    for name in ("spawn_pet", "spawn_in_process_window"):
        if callable(getattr(shell, name, None)):
            setattr(shell, name, _blocked)
            done = True
    return done


# ---------------------------------------------------------------------- 2. 右键菜单
def _plain(text: str) -> str:
    return (text or "").replace("&", "").strip()


def prune_menu(menu) -> int:
    """从（原桌宠的）菜单里去掉生小肥鱼相关的项，子菜单也处理；去掉后留下的连续分隔线合并。返回去掉了几项。"""
    removed = 0
    for act in list(menu.actions()):
        sub = act.menu()
        if sub is not None and sub is not menu:
            removed += prune_menu(sub)
        if _plain(act.text()).startswith(SPAWN_MENU_TEXTS):
            menu.removeAction(act)
            removed += 1
    if removed:
        prev_sep = True                                   # 开头的分隔线也去掉
        for act in list(menu.actions()):
            if act.isSeparator():
                if prev_sep:
                    menu.removeAction(act)
                prev_sep = True
            elif act.isVisible():
                prev_sep = False
        acts = menu.actions()
        if acts and acts[-1].isSeparator():
            menu.removeAction(acts[-1])
    return removed


# ---------------------------------------------------------------------- 3. 桌宠设置
def hide_spawn_settings(dlg) -> list:
    """隐藏设置窗口里“生小肥鱼”一组：每一行，以及只装着这几行的那一组（连标题）。返回被隐藏的控件。"""
    from PySide6.QtWidgets import QWidget

    rows = []
    for name in SPAWN_SETTING_WIDGETS:
        w = getattr(dlg, name, None)
        r = w
        while r is not None and r is not dlg and type(r).__name__ != "SettingRow":
            r = r.parentWidget()
        target = r if (r is not None and r is not dlg) else w
        if target is not None and target not in rows:
            rows.append(target)
    hidden = []
    for r in rows:
        r.hide()
        hidden.append(r)
    for r in rows:                                        # 最高一层“只装着这几行”的祖先 = 这一组（带标题）
        a, group = r.parentWidget(), None
        while a is not None and a is not dlg:
            others = [x for x in a.findChildren(QWidget) if type(x).__name__ == "SettingRow" and x not in rows]
            if others or a.parentWidget() is None:
                break
            group, a = a, a.parentWidget()
        if group is not None and group not in hidden:
            group.hide()
            hidden.append(group)
    if rows:                                              # 标题不在组里（直接放在页面上）时，单独藏起来
        from PySide6.QtWidgets import QLabel

        for lab in dlg.findChildren(QLabel):
            if _plain(lab.text()) == SPAWN_SECTION_TITLE and lab not in hidden:
                lab.hide()
                hidden.append(lab)
    return hidden


def install_settings(msd=None) -> bool:
    """包一层设置窗口：建好后（以及重新整理导航后）把“生小肥鱼”一组藏起来。"""
    if msd is None:
        from pet import modern_settings_dialog as msd  # type: ignore
    Dialog = msd.ModernSettingsDialog
    if getattr(Dialog, "_ascii_no_spawn", False):
        return True
    orig_init = Dialog.__init__

    def __init__(self, *a, **k):
        orig_init(self, *a, **k)
        _hide_quietly(self)
        _settings_selftest(self)

    Dialog.__init__ = __init__
    orig_nav = getattr(Dialog, "_rebuild_domain_navigation", None)
    if callable(orig_nav):
        def _rebuild_domain_navigation(self, *a, **k):
            out = orig_nav(self, *a, **k)
            _hide_quietly(self)
            return out

        Dialog._rebuild_domain_navigation = _rebuild_domain_navigation
    Dialog._ascii_no_spawn = True
    try:
        install_menu_editor()
    except Exception:
        log.exception("字符画：菜单布局编辑器里隐藏「生小肥鱼」失败")
    return True


SPAWN_ACTION_IDS = ("spawn_pet", "clear_spawned_pets")


def hide_tree_items(tree) -> int:
    """菜单布局编辑器（结构树和实时预览树）里，把生小肥鱼相关的条目藏起来。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidgetItemIterator

    n = 0
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        data = item.data(0, Qt.ItemDataRole.UserRole)
        ident = data.get("id") if isinstance(data, dict) else None
        if ident in SPAWN_ACTION_IDS or _plain(item.text(0)).startswith(SPAWN_MENU_TEXTS):
            item.setHidden(True)
            n += 1
        it += 1
    return n


def install_menu_editor(mle=None) -> bool:
    if mle is None:
        from pet import settings_menu_layout_editor as mle  # type: ignore
    Editor = mle.MenuLayoutEditor
    if getattr(Editor, "_ascii_no_spawn", False):
        return True
    orig_changed = Editor._on_changed

    def _on_changed(self, *a, **k):
        out = orig_changed(self, *a, **k)
        for name in ("tree", "preview"):
            t = getattr(self, name, None)
            if t is not None:
                try:
                    hide_tree_items(t)
                except Exception:
                    log.exception("字符画：隐藏菜单编辑器里的「生小肥鱼」失败")
        return out

    Editor._on_changed = _on_changed
    Editor._ascii_no_spawn = True
    return True


def _hide_quietly(dlg) -> None:
    try:
        hide_spawn_settings(dlg)
    except Exception:
        log.exception("字符画：隐藏设置里的“生小肥鱼”失败")


def leftovers(dlg, hidden: list) -> list:
    """设置窗口里还看得到的“小肥鱼”字样（不在被隐藏的控件里面）。自检用。"""
    from PySide6.QtWidgets import QAbstractButton, QLabel

    out = []
    for w in dlg.findChildren(QLabel) + dlg.findChildren(QAbstractButton):
        text = w.text() if hasattr(w, "text") else ""
        if "小肥鱼" not in text:
            continue
        a, covered = w, False
        while a is not None and a is not dlg:
            if a in hidden:
                covered = True
                break
            a = a.parentWidget()
        if not covered:
            out.append(text[:40])
    out += [f"[列表] {t}" for t in item_texts(dlg) if "小肥鱼" in t]
    return out


def item_texts(root) -> list:
    """列表 / 树 / 下拉框里的条目文字（菜单设置页可能用这些列出菜单项）。"""
    from PySide6.QtWidgets import QComboBox, QListWidget, QTreeWidget, QTreeWidgetItemIterator

    out = []
    for lw in root.findChildren(QListWidget):
        out += [lw.item(i).text() for i in range(lw.count()) if not lw.item(i).isHidden()]
    for tw in root.findChildren(QTreeWidget):
        it = QTreeWidgetItemIterator(tw)
        while it.value():
            item = it.value()
            if not item.isHidden():
                out += [item.text(c) for c in range(item.columnCount())]
            it += 1
    for cb in root.findChildren(QComboBox):
        out += [cb.itemText(i) for i in range(cb.count())]
    return out


def _settings_selftest(dlg) -> None:
    import os

    out = os.environ.get("PET_ASCII_SELFTEST_SETTINGS")
    if not out:
        return
    from PySide6.QtCore import QTimer

    def snap():
        import json

        hidden = hide_spawn_settings(dlg)
        rep = {"hidden": len(hidden), "leftovers": leftovers(dlg, hidden),
               "spawn_widgets_found": [n for n in SPAWN_SETTING_WIDGETS if getattr(dlg, n, None) is not None]}
        dlg.grab().save(os.path.join(out, "settings.png"))
        with open(os.path.join(out, "selftest_settings.json"), "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        dlg.close()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()

    QTimer.singleShot(2500, snap)
