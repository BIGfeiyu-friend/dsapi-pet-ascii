"""去掉字符画 DS娘用不到的原桌宠功能（v1.3.6，字符画设置 trim_unused_menu，默认开；关掉后重启桌宠即恢复菜单）。

右键菜单（字符画窗口里整合的原桌宠菜单）顶层去掉这些项：
  播放动画、切换角色、播放速率（原来那只视频桌宠的动画，它已常驻隐藏）、音乐（原桌宠的听歌识别 / 歌词；
  字符画的 Spotify 在「字符画」子菜单里，不受影响）、桌宠控制、快捷启动、工具与帮助、Agent 联动、主动识屏、待办提醒，
  以及最上面的彩蛋「厉害了我的鲸 · 请点击」。
只按顶层菜单项的文字精确匹配（忽略空格和 &），不进子菜单，所以「字符画」等子菜单里的同名项不会被误删。
它们在后台的部分由桌宠自己的设置关掉（music_sing_enabled、music_lyric_enabled、todo_reminder_enabled、
menu_easter_egg.enabled），设置窗口里仍可重新打开。
"""
from __future__ import annotations

import logging

log = logging.getLogger("pet.ascii")

TRIM_TOP_MENU = ("播放动画", "切换角色", "播放速率", "音乐", "桌宠控制", "快捷启动", "工具与帮助", "Agent联动",
                 "主动识屏", "待办提醒")
EASTER_EGG_TEXTS = ("厉害了我的鲸",)


def enabled() -> bool:
    try:
        from .config import AsciiConfig

        return bool(AsciiConfig.load().trim_unused_menu)
    except Exception:
        return True


def _norm(text: str) -> str:
    return "".join((text or "").replace("&", "").split())


def _action_texts(act) -> list:
    """菜单项的文字；彩蛋那种自定义控件项（QWidgetAction）也把控件里的文字算上。"""
    out = [act.text() or ""]
    widget = getattr(act, "defaultWidget", None)
    w = widget() if callable(widget) else None
    if w is not None:
        try:
            from PySide6.QtWidgets import QLabel

            labels = [w] if isinstance(w, QLabel) else []
            labels += w.findChildren(QLabel)
            out += [lb.text() for lb in labels]
            out.append(w.toolTip() or "")
        except Exception:
            pass
    return out


def collapse_separators(menu) -> None:
    """去掉开头、结尾和连续重复的分隔线。"""
    prev_sep = True
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


def retarget_size_menu(menu, win) -> bool:
    """原桌宠菜单顶层的「大小」（调的是常驻隐藏的原桌宠）→ 换成调字符画窗口的大小（v1.3.7）。返回是否换了。"""
    for act in menu.actions():
        sub = act.menu()
        if sub is not None and sub is not menu and _norm(act.text()) == "大小":
            win.fill_size_menu(sub)
            try:                                       # 原桌宠若在子菜单弹出时重新填，再换回来（信号按连接顺序执行，这个在后）
                sub.aboutToShow.connect(lambda s=sub: win.fill_size_menu(s))
            except Exception:
                pass
            return True
    return False


def prune_top_menu(menu) -> list:
    """从原桌宠菜单的顶层去掉用不到的项。返回去掉的项的文字。"""
    names = {_norm(t) for t in TRIM_TOP_MENU}
    removed = []
    for act in list(menu.actions()):
        if act.isSeparator():
            continue
        texts = _action_texts(act)
        if _norm(texts[0]) in names or any(egg in t for t in texts for egg in EASTER_EGG_TEXTS):
            menu.removeAction(act)
            removed.append(_norm(texts[0]) or "彩蛋")
    if removed:
        collapse_separators(menu)
    return removed
