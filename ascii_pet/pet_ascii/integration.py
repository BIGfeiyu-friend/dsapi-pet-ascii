"""接入现有桌宠（由 _internal/pet_memory/bootstrap.py 在桌宠进程内调用）。

- 桌宠 exe 带 --ascii-lab / --ascii-pet 参数启动时：直接打开开发测试台 / 字符画窗口（使用桌宠自带的 Python 与 PySide6）
- 正常启动且 config.json 中 attach_to_pet = true 时：桌宠启动后打开字符画窗口，并与对话联动：
  chat_link = inline（默认）：在聊天系统提示末尾加一段表情说明，主模型每次回复开头顺带输出 <face ...> 标签；
    标签在流式输出中被剥离（聊天窗口、气泡、语音、会话记录与记忆都看不到），本地骨架立即演出，不额外请求 API；
    回复流式输出期间嘴巴会动。没有标签时按回复关键词本地兜底。
  chat_link = ai：回复结束后另起一次表情生成请求（字符画专用模型）。
- hide_original_pet = true（默认）：原来那个会到处跑的桌宠常驻隐藏（用桌宠自己的“显示/隐藏”逻辑，它会暂停动画和计时器）。
  关掉字符画窗口后它也不出来；托盘的“显示 / 隐藏”、灵动岛的“显示桌宠”改为重新打开字符画窗口。
  只有在字符画菜单里点“显示原来的桌宠”才显示它（选择会记住）。
- integrate_pet_menu = true（默认）：在字符画窗口上右键，弹出的就是原桌宠的完整菜单（走它自己的菜单流程：
  模板、样式、勾选项保持打开、菜单关闭后才执行的动作都照常），只是定位在字符画窗口旁边；字符画的选项插在最前面。
"""
from __future__ import annotations

import logging
import sys
import time

log = logging.getLogger("pet.ascii")

_state = {"started": False, "rt": None, "win": None, "lab": None, "shell": None, "pet_menu_installed": False,
          "menu_extend": False, "ascii_menu_until": 0.0, "pet_hidden_by_us": False}


def run_standalone(argv: list) -> int:
    if "--ascii-room" in argv:                 # 主人的“房间”窗口：独立进程，只看不改（送礼物除外）
        from .room_viewer import main as room_main

        return room_main(argv)
    from . import __main__ as entry

    args = ["--lab"] if "--ascii-lab" in argv else []
    if "--mock" in argv:
        args.append("--mock")
    return entry.main(args)


def install() -> None:
    import os

    from pet.chat import service  # type: ignore

    # 自检开关只对本进程有效：读取后立刻移除，桌宠之后派生的子进程不会继承
    _state["selftest_dir"] = os.environ.pop("PET_ASCII_SELFTEST_CHAT", "")
    _state["selftest_prompt"] = os.environ.pop("PET_ASCII_SELFTEST_PROMPT", "")
    _state["selftest_menu"] = os.environ.pop("PET_ASCII_SELFTEST_MENU", "")
    _state["selftest_chatwin"] = os.environ.pop("PET_ASCII_SELFTEST_CHATWIN", "")
    _state["selftest_island"] = os.environ.pop("PET_ASCII_SELFTEST_ISLAND", "")
    _state["selftest_vis"] = os.environ.pop("PET_ASCII_SELFTEST_VIS", "")
    _state["selftest_sleep"] = os.environ.pop("PET_ASCII_SELFTEST_SLEEP", "")
    _state["selftest_create"] = os.environ.pop("PET_ASCII_SELFTEST_CREATE", "")
    _state["selftest_room"] = os.environ.pop("PET_ASCII_SELFTEST_ROOM", "")
    _state["selftest_closetip"] = os.environ.pop("PET_ASCII_SELFTEST_CLOSETIP", "")
    orig_init = service.ChatService.__init__

    def chat_service_init(self, *a, **k):
        orig_init(self, *a, **k)
        _schedule_start(2500)

    service.ChatService.__init__ = chat_service_init
    _install_no_spawn()
    _install_inline()
    _install_chat_theme()

    # 主触发点：桌宠主程序启动完成（AppShell.start 在 QApplication 和日志就绪之后、进入事件循环之前调用）。
    # ChatService 只在第一次打开聊天窗口时才创建，单靠它窗口会迟迟不出现。
    try:
        from pet import app as pet_app  # type: ignore

        orig_start = pet_app.AppShell.start

        def shell_start(self, *a, **k):
            _state["shell"] = self
            result = orig_start(self, *a, **k)
            try:
                _install_pet_menu()
                _install_chat_positioning()
                _hide_pet_at_start()
            except Exception:
                log.exception("字符画：接管原桌宠菜单 / 隐藏原桌宠失败（不影响桌宠本体）")
            try:
                _install_island_mirror()
                _hide_island_at_start()
            except Exception:
                log.exception("字符画：接管灵动岛失败（灵动岛保持原样）")
            _schedule_start(3000)
            return result

        pet_app.AppShell.start = shell_start
    except Exception as exc:
        _state["hook_note"] = f"AppShell 挂载失败：{exc}"


# ---------------------------------------------------------------------- 原来的桌宠：隐藏 / 显示
def _primary_pet():
    """主桌宠窗口（AppShell._instances[0].win）。拿不到时返回 None。"""
    try:
        inst = getattr(_state.get("shell"), "_instances", None) or []
        return getattr(inst[0], "win", None) if inst else None
    except Exception:
        return None


def _pet_visible() -> bool:
    w = _primary_pet()
    return bool(w is not None and w.isVisible())


def set_pet_visible(visible: bool, remember: bool = False) -> None:
    """用桌宠自己的切换逻辑显示/隐藏主桌宠（托盘、灵动岛的状态会一起同步；隐藏时它会暂停动画和计时器）。"""
    w = _primary_pet()
    if w is None or w.isVisible() == visible:
        return
    if remember and _state.get("rt") is not None:   # 先记下选择：之后的“显示”不会被拦成打开字符画窗口
        cfg = _state["rt"].cfg
        cfg.hide_original_pet = not visible
        cfg.save()
    toggle = getattr(_state.get("shell"), "_toggle_primary_pet_visible", None)
    _state["allow_pet_show"] = bool(visible)          # 这是明确要显示：放行
    try:
        if callable(toggle):
            toggle()
        else:
            w.show() if visible else w.hide()
    finally:
        _state["allow_pet_show"] = False
    _state["pet_hidden_by_us"] = not visible
    log.info("字符画：%s原来的桌宠", "显示" if visible else "隐藏")


def _keep_pet_hidden() -> bool:
    """原来的桌宠是否要保持隐藏（随桌宠启动字符画、并且选了隐藏原桌宠——默认如此，选择会记住）。"""
    rt = _state.get("rt")
    if rt is not None:
        cfg = rt.cfg
    else:
        from .config import AsciiConfig

        cfg = AsciiConfig.load()
    return bool(cfg.attach_to_pet and cfg.hide_original_pet)


def _install_pet_show_guard() -> None:
    """原来的桌宠常驻隐藏：托盘的“显示 / 隐藏”、灵动岛卡片的“显示桌宠”等地方要把它显示出来时，
    改为打开字符画窗口。只有在字符画菜单里明确点“显示原来的桌宠”（会记住）才真正显示它。
    """
    w = _primary_pet()
    if w is None or getattr(w, "_ascii_show_guard", False):
        return
    orig_show = type(w).show

    def guarded_show(*a, **k):
        if _state.get("allow_pet_show") or not _keep_pet_hidden():
            return orig_show(w, *a, **k)
        from PySide6.QtCore import QTimer

        log.info("字符画：原来的桌宠保持隐藏，改为打开字符画窗口")
        QTimer.singleShot(0, open_ascii_window)
        return None

    w.show = guarded_show
    w._ascii_show_guard = True


def _hide_pet_at_start() -> None:
    from PySide6.QtCore import QTimer

    if _keep_pet_hidden():
        set_pet_visible(False)                      # 立刻藏：赶在第一次绘制之前，启动时不再闪一下
        _install_pet_show_guard()
        QTimer.singleShot(0, lambda: set_pet_visible(False))


def _pet_toggle_item():
    if _pet_visible():
        return "隐藏原来的桌宠", lambda: set_pet_visible(False, remember=True)
    return "显示原来的桌宠", lambda: set_pet_visible(True, remember=True)


def _on_ascii_closed() -> None:
    # 字符画窗口关了：原来的桌宠默认保持隐藏（托盘的“显示 / 隐藏”或灵动岛的“显示桌宠”会重新打开字符画窗口）；
    # 灵动岛放回屏幕上，桌面上仍有一个入口。只有关掉了“隐藏原来的桌宠”时，才把原桌宠显示回来。
    if not _keep_pet_hidden() and _state.get("pet_hidden_by_us") and not _pet_visible():
        set_pet_visible(True)
    win = _state.get("win")
    if win is not None and getattr(win, "island_popup", None) is not None:
        try:
            win.set_island_bar(None)
        except RuntimeError:
            pass
    _release_island()
    _state["win"] = None
    _schedule_close_tip()


CLOSE_TIP_TITLE = "{name}还在后台"
CLOSE_TIP_TEXT = ("字符画窗口关掉了，桌宠还在后台运行（房间里的生活也照常）。\n"
                  "想再打开：右键托盘图标，选「显示 / 隐藏」。\n"
                  "要彻底退出：右键托盘图标，选「退出」。")
CLOSE_TIP_DELAY_MS = 800


def _schedule_close_tip() -> None:
    """稍等一下再提示：换窗口样式时窗口会马上重建、退出桌宠时事件循环已经停了，这两种情况都不会弹。"""
    rt = _state.get("rt")
    if rt is None or not getattr(rt.cfg, "close_tray_tip", True):
        return
    from PySide6.QtCore import QTimer

    QTimer.singleShot(CLOSE_TIP_DELAY_MS, _show_close_tip)


def _show_close_tip() -> bool:
    if _state.get("win") is not None:              # 窗口又打开了（重建、托盘里马上点了显示）：不提示
        return False
    try:
        from PySide6.QtWidgets import QSystemTrayIcon

        tray = getattr(_state.get("shell"), "tray", None)
        if tray is None or not tray.isVisible() or not QSystemTrayIcon.supportsMessages():
            log.info("字符画：窗口已关闭（托盘不可用，没有弹提示）")
            return False
        rt = _state.get("rt")
        name = rt.pet_name() if rt is not None else "桌宠"
        tray.showMessage(CLOSE_TIP_TITLE.format(name=name), CLOSE_TIP_TEXT,
                         QSystemTrayIcon.MessageIcon.Information, 8000)
        log.info("字符画：窗口已关闭，已在托盘提示怎么再打开")
        return True
    except Exception:
        log.exception("字符画：托盘提示失败")
        return False


# ---------------------------------------------------------------------- 灵动岛：放进字符画窗口底部
# 余额、峰谷提示、最近消息仍由桌宠自己的灵动岛逻辑产生（查询、缓存、限频、没配 Key 等提示都照旧）：
# 把 DynamicIsland 的 set_balance_info / set_last_message / notify_event / set_pet_visible 转发给底部的栏，
# 原来浮在屏幕上的灵动岛保持隐藏（它隐藏时会自己停掉计时器和动画）。
def _island():
    return getattr(_state.get("shell"), "island", None)


def _install_island_mirror(di=None) -> None:
    if _state.get("island_mirror_installed"):
        return
    if di is None:
        from pet import dynamic_island as di  # type: ignore

    Island = di.DynamicIsland
    orig = {n: getattr(Island, n) for n in ("set_balance_info", "set_last_message", "notify_event", "set_pet_visible",
                                            "setVisible", "show")}

    def bar():
        return _state.get("island_bar") if _state.get("island_integrated") else None

    def forward(fn, *args):
        b = bar()
        if b is not None:
            try:
                getattr(b, fn)(*args)
            except RuntimeError:            # 栏已被删除
                pass

    def set_balance_info(self, tier_text, balance_text, *a, **k):
        result = orig["set_balance_info"](self, tier_text, balance_text, *a, **k)
        forward("set_balance", tier_text, balance_text)
        return result

    def set_last_message(self, text, *a, **k):
        result = orig["set_last_message"](self, text, *a, **k)
        forward("set_message", text)
        return result

    def notify_event(self, kind, *a, **k):
        result = orig["notify_event"](self, kind, *a, **k)
        forward("flash")
        return result

    def set_pet_visible(self, visible, *a, **k):
        result = orig["set_pet_visible"](self, visible, *a, **k)
        forward("refresh_state")
        return result

    def set_visible(self, visible, *a, **k):
        if visible and _state.get("island_integrated"):    # 已经放进字符画窗口：屏幕上的灵动岛保持隐藏
            visible = False
        return orig["setVisible"](self, visible, *a, **k)

    def show(self, *a, **k):
        # 桌宠自己的代码用 island.show() 显示灵动岛（灵动岛对象在接管之前就建好了，C++ 里的虚函数改写不一定生效，
        # 所以 Python 这一层也拦一下）
        if _state.get("island_integrated"):
            return orig["setVisible"](self, False)
        return orig["show"](self, *a, **k)

    Island.set_balance_info = set_balance_info
    Island.set_last_message = set_last_message
    Island.notify_event = notify_event
    Island.set_pet_visible = set_pet_visible
    Island.setVisible = set_visible
    Island.show = show
    _state["island_mirror_installed"] = True


def _hide_island_at_start() -> None:
    from .config import AsciiConfig

    cfg = AsciiConfig.load()
    if cfg.attach_to_pet and cfg.integrate_island and cfg.window_style != "floating":
        _state["island_integrated"] = True             # 从现在起屏幕上的灵动岛保持隐藏，等字符画窗口接手
        island = _island()
        if island is not None:
            island.hide()


def _pricing_tier():
    try:
        from pet import balance as b  # type: ignore

        return b.deepseek_pricing_tier(b.beijing_now())
    except Exception:
        return None


def refresh_balance() -> None:
    """立即查询余额（走桌宠自己的静默查询：30 秒缓存、一分钟内不重复查）。"""
    shell, bar = _state.get("shell"), _state.get("island_bar")
    if shell is None or bar is None:
        return
    if _island() is None:
        bar.set_balance("", "灵动岛在桌宠设置里关着，暂时查不了余额")
        return
    fn = getattr(shell, "_quiet_balance_refresh", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            log.exception("字符画：查询余额失败")


def _open_island_settings() -> None:
    fn = getattr(_state.get("shell"), "_open_settings_from_island", None)
    if callable(fn):
        fn()


def attach_island_bar(win=None) -> bool:
    """把灵动岛收进字符画窗口右下角的“◈ 灵动岛”按钮（点击弹出卡片），并把屏幕上的灵动岛藏起来。成功返回 True。"""
    from PySide6.QtCore import QTimer

    from .island_bar import IslandBar

    win = win or _state.get("win")
    rt, shell = _state.get("rt"), _state.get("shell")
    if win is None or rt is None or shell is None or not win.framed:
        return False
    if _state.get("island_bar") is not None:
        return True
    bar = IslandBar(None, on_chat=win._open_chat, on_toggle_pet=lambda: set_pet_visible(not _pet_visible(), remember=True),
                    on_settings=_open_island_settings, on_refresh=refresh_balance, pet_visible=_pet_visible,
                    pricing_tier=_pricing_tier, card=True, show_chat=False)   # “>_ 对话”在窗口左下角，卡片里不重复
    win.island_on_open = refresh_balance                # 点开卡片时查一次余额（和原灵动岛展开卡片时一样）
    island = _island()
    if island is not None:                              # 先把灵动岛当前的状态搬过来
        bar.set_balance(getattr(island, "_balance_tier_text", ""), getattr(island, "_balance_text", ""))
        bar.set_message(getattr(island, "_last_message", ""))
    tier_hint = getattr(shell, "_island_tier_hint", None)
    if callable(tier_hint) and not bar.tier_text:
        try:
            bar.set_balance(tier_hint(), "")
        except Exception:
            pass
    _state["island_bar"] = bar
    _state["island_integrated"] = True
    win.set_island_bar(bar)
    if island is not None:
        island.hide()
    QTimer.singleShot(1500, refresh_balance)            # 启动后先查一次
    timer = QTimer(bar)
    timer.timeout.connect(refresh_balance)
    timer.start(int(max(1.0, float(rt.cfg.island_balance_refresh_min)) * 60000))
    log.info("字符画：灵动岛已收进字符画窗口右下角的按钮（点击展开；余额每 %g 分钟在后台更新一次）",
             rt.cfg.island_balance_refresh_min)
    return True


def _release_island() -> None:
    """不再集成：屏幕上的灵动岛恢复显示（如果它在桌宠设置里是开着的）。"""
    was = _state.get("island_integrated")
    _state["island_integrated"] = False
    _state["island_bar"] = None
    island = _island()
    if was and island is not None:
        island.show()


def set_island_integrated(on: bool) -> None:
    rt, win = _state.get("rt"), _state.get("win")
    if rt is not None:
        rt.cfg.integrate_island = bool(on)
        rt.cfg.save()
    if on:
        attach_island_bar(win)
    else:
        if win is not None:
            win.set_island_bar(None)
        _release_island()


def _island_toggle_item():
    return bool(_state.get("island_bar") is not None), set_island_integrated


# ---------------------------------------------------------------------- 原来桌宠的右键菜单
def _install_no_spawn() -> None:
    """删去“生小肥鱼”（字符画设置 remove_spawn_pet）：生成本身、右键菜单两项、设置里的一组。"""
    from . import no_spawn, trim

    _state["trim_menu"] = trim.enabled()                 # 用不到的原桌宠菜单项（v1.3.6，trim_unused_menu）
    _state["no_spawn"] = False
    if not no_spawn.enabled():
        return
    try:
        _state["no_spawn"] = no_spawn.install_pet()
        no_spawn.install_settings()
        log.info("字符画：已删去「生小肥鱼」（remove_spawn_pet）")
    except Exception:
        log.exception("字符画：删去「生小肥鱼」失败（桌宠照常运行）")


def _install_pet_menu(pwin=None) -> None:
    """包一层原桌宠的菜单流程：从字符画窗口打开时，菜单定位在字符画窗口旁边，并把字符画的选项插在最前面。

    原桌宠的 _show_context_menu 会按它自己窗口的位置摆放菜单、做滑入动画，勾选项会关掉再重开菜单
    （reopen_context_menu）。所以从字符画窗口打开时，临时让它“以为自己在字符画窗口的位置”，
    重开时也沿用；其余情况（右键原桌宠本身）完全照旧。
    """
    if _state["pet_menu_installed"]:
        return
    if pwin is None:
        from pet import window as pwin  # type: ignore

    PetWindow = pwin.PetWindow
    orig_populate = pwin._populate_context_menu
    orig_show = PetWindow._show_context_menu
    orig_reopen = PetWindow.reopen_context_menu

    def populate(menu, pet):
        orig_populate(menu, pet)
        if _state.get("no_spawn"):
            try:
                from .no_spawn import prune_menu

                prune_menu(menu)
            except Exception:
                log.exception("字符画：从菜单里去掉「生小肥鱼」失败")
        if _state.get("trim_menu"):
            try:
                from .trim import prune_top_menu

                prune_top_menu(menu)
            except Exception:
                log.exception("字符画：去掉用不到的菜单项失败")
        win = _state.get("win")
        rt = _state.get("rt")
        if win is not None and rt is not None and getattr(rt.cfg, "hide_original_pet", True):
            try:                                            # 原桌宠藏着：「大小」改成调字符画窗口
                from .trim import retarget_size_menu

                retarget_size_menu(menu, win)
            except Exception:
                log.exception("字符画：把「大小」改成调字符画窗口失败")
        if _state.get("menu_extend") and win is not None:
            try:
                menu.setProperty("asciiPetMenu", True)
                _style_like_pet_menu(win.extend_pet_menu(menu))
                menu._ascii_left = win.left_menus(menu)      # 各级子菜单往左展开（顶层菜单原桌宠已经摆在窗口左边）
            except Exception:
                log.exception("字符画：往原桌宠菜单里加字符画选项失败")

    def show_context_menu(self, global_pos, *a, **k):
        win = _state.get("win")
        if _state.get("ascii_menu_until", 0.0) < time.monotonic() or win is None:
            return orig_show(self, global_pos, *a, **k)
        _state["ascii_menu_until"] = 0.0
        from PySide6.QtCore import QRect

        rect = QRect(win.geometry())
        self.visible_content_rect = lambda *_a, **_k: QRect(rect)
        self._screen_available = lambda *_a, **_k: win.screen()
        _state["menu_extend"] = True
        try:
            return orig_show(self, global_pos, *a, **k)
        finally:
            _state["menu_extend"] = False
            for name in ("visible_content_rect", "_screen_available"):
                self.__dict__.pop(name, None)

    def reopen_context_menu(self, menu, *a, **k):
        try:
            if menu is not None and menu.property("asciiPetMenu"):
                _state["ascii_menu_until"] = time.monotonic() + 2.0     # 勾选项重开菜单：仍然按字符画窗口来
        except Exception:
            pass
        return orig_reopen(self, menu, *a, **k)

    pwin._populate_context_menu = populate
    PetWindow._show_context_menu = show_context_menu
    PetWindow.reopen_context_menu = reopen_context_menu
    _state["pet_menu_installed"] = True
    log.info("字符画：已接管原桌宠的右键菜单（从字符画窗口打开时使用）")


def _style_like_pet_menu(pairs: list) -> None:
    """让字符画的子菜单套用原桌宠菜单当前的样式（现代/经典主题、勾选标记、响应式字体）。"""
    try:
        from pet.context_menus.menu_styles.common import (  # type: ignore
            inherit_menu_style, install_responsive_menu_style)
    except Exception:
        return
    try:
        from pet.context_menus.menu_styles.modern import install_modern_check_indicators  # type: ignore
    except Exception:
        install_modern_check_indicators = None
    for parent, sub in pairs:
        try:
            inherit_menu_style(parent, sub)
            if install_modern_check_indicators is not None and str(sub.property("menuStyle") or "") == "modern":
                install_modern_check_indicators(sub)
            install_responsive_menu_style(sub)
        except Exception:
            log.exception("字符画：子菜单套用原菜单样式失败")


def open_pet_chat() -> bool:
    """打开桌宠的“AI 对话”窗口（和右键菜单里的“AI 对话”是同一个窗口、同一套逻辑）。"""
    try:
        inst = (getattr(_state.get("shell"), "_instances", None) or [None])[0]
    except Exception:
        inst = None
    if inst is None or not callable(getattr(inst, "open_chat", None)):
        return False
    inst.open_chat()
    return True


def _install_chat_positioning(pet_app=None) -> None:
    """对话窗口每次打开都会摆到原桌宠旁边（position_near_pet）。原桌宠藏起来时，让它摆到字符画窗口旁边。

    不论从哪里打开（左下角入口、右键菜单、托盘、灵动岛）都生效；原桌宠显示着的时候完全照旧。
    """
    if _state.get("chat_pos_installed"):
        return
    if pet_app is None:
        from pet import app as pet_app  # type: ignore

    PetInstance = pet_app.PetInstance
    orig_open = PetInstance.open_chat

    def open_chat(self, *a, **k):
        win, pet = _state.get("win"), getattr(self, "win", None)
        if win is None or pet is None or pet.isVisible() or not win.isVisible():
            return orig_open(self, *a, **k)
        from PySide6.QtCore import QRect

        rect = QRect(win.geometry())
        pet.visible_content_rect = lambda *_a, **_k: QRect(rect)
        try:
            return orig_open(self, *a, **k)
        finally:
            pet.__dict__.pop("visible_content_rect", None)

    PetInstance.open_chat = open_chat
    _state["chat_pos_installed"] = True


def pet_menu_from_ascii(global_pos) -> bool:
    """在字符画窗口上右键：弹出原桌宠的完整菜单。拿不到原桌宠时返回 False（改用字符画自己的菜单）。"""
    w = _primary_pet()
    if w is None or not _state.get("pet_menu_installed") or _state.get("rt") is None \
            or not _state["rt"].cfg.integrate_pet_menu:
        return False
    _state["ascii_menu_until"] = time.monotonic() + 2.0
    w._show_context_menu(global_pos)
    return True


def _install_chat_theme() -> None:
    """“AI 对话”经典窗口的 ASCII 风格皮肤（chat_theme.py）。要在任何聊天窗口创建之前装上。"""
    try:
        from . import chat_theme
        from .config import AsciiConfig, data_dir, pet_app_config

        cfg = AsciiConfig.load()
        if not cfg.attach_to_pet:
            return
        pc = pet_app_config()
        _state["pet_alias"] = str((pc.get("character_aliases") or {}).get(pc.get("character") or "") or "")
        def character() -> str:                       # LINE 头像用当前字符画角色的笑脸
            rt = _state.get("rt")
            return rt.cfg.character if rt is not None else cfg.character

        chat_theme.install(skin=cfg.chat_theme, name_provider=lambda: _state.get("pet_alias", ""),
                           tile_path=str(data_dir() / "chat_tile.png"), title=cfg.chat_title,
                           avatar_path=str(data_dir() / "chat_avatar_{character}.png"), character=character)
    except Exception:
        log.exception("字符画：安装 ASCII 对话框皮肤失败（聊天窗口保持原样）")


def set_chat_theme(name) -> None:
    """“AI 对话”窗口的风格：line / ascii / original（旧的 True / False = ascii / original）。立即生效并记住。"""
    from . import chat_theme

    if isinstance(name, bool):
        name = "ascii" if name else "original"
    chat_theme.set_skin(name)
    rt = _state.get("rt")
    if rt is not None:
        rt.cfg.chat_theme = chat_theme.skin()
        rt.cfg.save()


def _inline_active() -> bool:
    rt = _state.get("rt")
    return rt is not None and rt.cfg.chat_link == "inline"


WOKEN_MEMO = "\n〔你刚才睡着了（很久没人理你），被这条消息吵醒——可以带一点刚醒的迷糊或小起床气，但照常回答〕"


def prepare_inline_messages(rt, msgs: list) -> None:
    """真实对话请求的表情部分（系统提示里的固定说明另外加）：本轮用户消息末尾附上自创表情备忘和一句格式提醒。

    会话历史里她以前的回复都是剥掉标签后的文字，模型会照着历史省略标签；末尾提醒一句就能恢复。
    动态内容放在最后，不影响前缀缓存；只改发给 API 的这份请求，不改会话记录和记忆。
    """
    from .inline import TURN_REMINDER, custom_memo

    cfg = rt.cfg
    last = msgs[-1]
    if last.get("role") == "user" and isinstance(last.get("content"), str):
        tail = custom_memo(rt.custom.names(20)) if cfg.custom_presets else ""
        if cfg.inline_reminder:
            tail += TURN_REMINDER
        from . import creative

        if creative.SCENES:
            tail += "\n〔特别演出：" +"、".join(list(creative.SCENES)[:6]) + "（<face … scene=名字>，只在特别的时刻用）〕"
        sleep = getattr(rt, "sleep", None)
        if sleep is not None and sleep.consume_woken_by_chat() and getattr(cfg, "sleep_tell_model", True):
            tail += WOKEN_MEMO
        last["content"] += tail


def prepare_system_instructions(rt, msgs: list) -> None:
    """系统提示末尾的表情说明；连上 Spotify 时再加放歌的写法（只在连上 / 断开时变化，不常打断前缀缓存）。"""
    from .inline import INSTRUCTION, MUSIC_INSTRUCTION

    content = msgs[0].get("content") or ""
    if INSTRUCTION not in content:
        msgs[0]["content"] = (content.rstrip() + "\n\n" + INSTRUCTION) if content else INSTRUCTION
    music = getattr(rt, "music", None)
    if music is not None and music.available() and MUSIC_INSTRUCTION not in msgs[0]["content"]:
        msgs[0]["content"] = msgs[0]["content"].rstrip() + "\n" + MUSIC_INSTRUCTION


def prepare_music_memo(rt, msgs: list) -> None:
    """Spotify（v1.3）：本轮消息末尾附一句——主人在听什么、她上次放歌的结果、可以怎么放（不改系统提示）。"""
    music = getattr(rt, "music", None)
    if music is None:
        return
    try:
        last = msgs[-1]
        asked = bool(getattr(rt, "_music_asked", False))       # 只看主人这句原话（不看附上去的记忆、房间近况）
        tail = music.memo(asked=asked, liked_only=bool(getattr(rt, "_music_liked_only", False)))
        if tail and last.get("role") == "user" and isinstance(last.get("content"), str):
            last["content"] += tail
    except Exception:
        log.exception("字符画·Spotify：附加放歌备忘失败")


def prepare_room_bridge(rt, msgs: list) -> None:
    """虚拟房间 → 聊天（v1.1）：系统提示末尾附“房间里的记忆”（长期 + 印象，很少变化），本轮消息末尾附“房间近况”。
    只改发给 API 的这份请求，不改会话记录和桌宠记忆。"""
    life = getattr(rt, "room", None)
    cfg = rt.cfg
    if life is None or not cfg.room_enabled or not getattr(cfg, "room_bridge_to_chat", True):
        return
    try:
        from .room_life import ROOM_BLOCK_MARK

        if msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str) and \
                ROOM_BLOCK_MARK not in msgs[0]["content"]:
            block = life.chat_block()
            if block:
                msgs[0]["content"] = msgs[0]["content"].rstrip() + "\n\n" + block
        last = msgs[-1]
        if last.get("role") == "user" and isinstance(last.get("content"), str):
            from .room_life import asks_about_room

            last["content"] += life.chat_recent(force=asks_about_room(getattr(rt, "_pending_user", "")))
    except Exception:
        log.exception("字符画·房间：附加房间的记忆失败（这次聊天不带）")


def prepare_persona(builder, rt, msgs: list, a: tuple, k: dict) -> None:
    """副人格：记下正典（桌宠设置里的人设，供夜间反思和审核对照）；试运行 / 正式运行时把副人格块插在正典之后。

    影子模式只记录正典，不改请求。正典取自桌宠自己的 effective_system_prompt，与记忆模块组装系统提示时用的是同一个。
    """
    persona = getattr(rt, "persona", None)
    if persona is None or msgs[0].get("role") != "system" or rt.cfg.persona_mode == "off":
        return
    settings = a[0] if len(a) > 0 else k.get("settings")
    cid = a[1] if len(a) > 1 else k.get("character_id")
    role_prompt = a[4] if len(a) > 4 else k.get("role_prompt")
    canon = builder.effective_system_prompt(settings, cid, role_prompt)
    persona.note_canon(canon)
    if persona.inject_into(msgs, canon, rt.cfg):
        log.debug("字符画·副人格：已插入副人格块（%d 条）", len(persona.live_entries()))


def _install_inline() -> None:
    """系统提示里加表情说明 + 流式输出中剥离表情标签。两处都在字符画窗口启动后才生效。"""
    import functools

    from pet.chat import prompt as pprompt  # type: ignore
    from pet.chat import providers  # type: ignore

    orig_build = pprompt.PromptBuilder.build_messages

    @functools.wraps(orig_build)
    def build_messages(self, *a, **k):
        msgs = orig_build(self, *a, **k)
        rt = _state.get("rt")
        try:
            if rt is not None:
                rt.record_user(a[3] if len(a) > 3 else k.get("user_text", ""))
            if rt is not None and msgs:
                prepare_room_bridge(rt, msgs)          # 先于表情提醒：格式提醒要留在最后
                prepare_music_memo(rt, msgs)
            if _inline_active() and msgs:
                prepare_inline_messages(rt, msgs)
        except Exception:
            log.exception("字符画：附加表情提醒失败")
        try:
            if rt is not None and msgs:
                prepare_persona(self, rt, msgs, a, k)
        except Exception:
            log.exception("字符画·副人格：记录正典 / 插入副人格块失败")
        try:
            if _inline_active() and msgs and msgs[0].get("role") == "system":
                prepare_system_instructions(rt, msgs)
        except Exception:
            log.exception("字符画：添加表情说明失败")
        return msgs

    pprompt.PromptBuilder.build_messages = build_messages

    orig_stream = providers.OpenAICompatibleProvider.stream

    def stream(self, messages, config, cancel_event, response_holder=None):
        rt = _state.get("rt")
        if rt is None:
            yield from orig_stream(self, messages, config, cancel_event, response_holder)
            return
        from .inline import TagFilter

        flt = TagFilter()
        _state["tag_seen"] = False

        def deliver():
            for tag in flt.pop_tags():
                _state["tag_seen"] = True
                log.info("字符画：对话表情标签 %s", tag)
                rt.note_tag(tag)
                rt.inline_tag.emit(tag)

        for delta in orig_stream(self, messages, config, cancel_event, response_holder):
            visible = flt.feed(delta)
            deliver()
            if visible:
                rt.talk_signal.emit(0.6)
                yield visible
        rest = flt.flush()
        deliver()
        if rest:
            yield rest

    providers.OpenAICompatibleProvider.stream = stream


def _schedule_start(delay_ms: int) -> None:
    if _state["started"]:
        return
    _state["started"] = True
    from PySide6.QtCore import QTimer

    QTimer.singleShot(delay_ms, _start)


def _open_lab():
    from .lab import Lab

    if _state["lab"] is None:
        _state["lab"] = Lab(_state["rt"])
    _state["lab"].show()
    _state["lab"].raise_()


def _create_window(rt, cfg):
    """建字符画窗口并接上原桌宠的菜单、对话入口、灵动岛（启动时、以及关掉后从托盘 / 灵动岛重新打开时）。"""
    from .window import AsciiPetWindow

    win = AsciiPetWindow(rt, open_lab=_open_lab, on_close=_on_ascii_closed)
    if _state.get("shell") is not None:
        win.pet_menu = pet_menu_from_ascii          # 右键：原桌宠的完整菜单 + 字符画选项
        win.pet_toggle = _pet_toggle_item
        if _primary_pet() is not None:
            win.set_chat_entry(open_pet_chat)       # 左下角的“AI 对话”入口（右键菜单里的入口照旧）
        win.island_toggle = _island_toggle_item
    _state["win"] = win
    if _state.get("shell") is not None and cfg.integrate_island:
        try:
            attach_island_bar(win)                  # 灵动岛收进窗口右下角的按钮
        except Exception:
            log.exception("字符画：把灵动岛收进窗口失败（灵动岛保持原样）")
            _release_island()
    elif _state.get("island_integrated"):
        _release_island()
    win.show()
    if cfg.hide_original_pet and _pet_visible():    # 启动过程中原桌宠又被显示出来了：再藏一次
        set_pet_visible(False)
    _install_pet_show_guard()
    return win


def _on_launched_again() -> None:
    """桌宠又被启动了一次（single_instance）：不多开，把这只的字符画窗口拿到最前面（关掉了就重新打开）。"""
    log.info("字符画：桌宠又被启动了一次，已把正在运行的这只叫出来（没有多开）")
    open_ascii_window()


def open_ascii_window() -> None:
    """打开字符画窗口：已经开着就提到最前，关掉了就重新建一个（运行时、表情状态都还在）。"""
    win = _state.get("win")
    if win is not None:
        win.restore_from_taskbar()                  # 最小化了就恢复
        return
    rt = _state.get("rt")
    if rt is None:                                  # 还在启动中：字符画窗口稍后会自己出来
        return
    try:
        _create_window(rt, rt.cfg)
        log.info("字符画：重新打开字符画窗口")
    except Exception:
        log.exception("字符画：重新打开字符画窗口失败")


def _start() -> None:
    try:
        from .config import AsciiConfig

        if _state.get("hook_note"):
            log.warning("字符画：%s（已改用聊天服务创建时启动）", _state["hook_note"])
        cfg = AsciiConfig.load()
        if not cfg.attach_to_pet:
            return
        from pet.chat import service  # type: ignore

        from .runtime import Runtime

        rt = Runtime(cfg)
        _state["rt"] = rt
        if cfg.single_instance and _state.get("single_server") is None:   # 再次启动桌宠时，把这只叫出来
            from .single_instance import start_server

            _state["single_server"] = start_server(_on_launched_again, log)
        if rt.room is not None:                 # 虚拟房间：桌宠运行时她就会自己进去（与“房间”窗口开没开无关）
            rt.room.autorun = True
        _create_window(rt, cfg)

        def on_reply(text):
            try:
                log.info("字符画：收到对话回复（%d 字），交给表情生成", len(text or ""))
                rt.on_chat_reply(text, tag_seen=bool(_state.get("tag_seen")))
            except Exception:
                log.exception("字符画：处理对话回复失败")
            if _state.get("island_bar") is not None:   # 聊完一句，余额可能变了：稍后查一次（有限频）
                from PySide6.QtCore import QTimer

                QTimer.singleShot(3000, refresh_balance)

        def on_result(res):
            if res is None:
                return
            s = res.summary()
            log.info("字符画：生成完成 来源=%s 模式=%s 操作=%s 耗时=%.2fs token=%s/%s 缓存命中=%s 复用=%s 兜底=%s%s",
                     s["source"], s["mode"], ",".join(s["ops"]), s["elapsed"], s["prompt_tokens"],
                     s["completion_tokens"], s["cache_hit_tokens"], s["cached"], s["fallback"],
                     f" 错误={s['error']}" if s["error"] else "")

        rt.listeners.append(on_result)
        service.ChatService.register_global_finished(on_reply)
        log.info("字符画窗口已随桌宠启动（%s，对话联动=%s，回复监听数=%d）", rt.director.client_desc, cfg.chat_link,
                 len(service.ChatService._global_finished_listeners))
        if _state.get("selftest_dir"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(3000, _chat_selftest)
        if _state.get("selftest_menu"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(2500, _menu_selftest)
        if _state.get("selftest_chatwin"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(2500, _chatwin_selftest)
        if _state.get("selftest_island"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(9000, _island_selftest)       # 等启动后那次余额查询回来
        if _state.get("selftest_vis"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(3000, _vis_selftest)
        if _state.get("selftest_sleep"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(3000, _sleep_selftest)
        if _state.get("selftest_create"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(4000, _create_selftest)
        if _state.get("selftest_room"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(4000, _room_selftest)
        if _state.get("selftest_closetip"):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(4000, _closetip_selftest)
    except Exception:
        log.exception("字符画窗口启动失败（不影响桌宠本体）")


def _closetip_selftest() -> None:
    """关窗提示自检：关掉字符画窗口 → 等托盘提示 → 记下结果 → 重新打开字符画窗口。"""
    import json
    import os

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QSystemTrayIcon

    out = _state["selftest_closetip"]
    tray = getattr(_state.get("shell"), "tray", None)
    report = {"tray": tray is not None, "tray_visible": bool(tray is not None and tray.isVisible()),
              "supports_messages": QSystemTrayIcon.supportsMessages(), "shown": []}
    if tray is not None:                            # 记下真正调用了 showMessage（照常弹出）
        orig = tray.showMessage

        def spy(*a, **k):
            report["shown"].append([str(x) for x in a[:2]])
            return orig(*a, **k)

        tray.showMessage = spy
    win = _state.get("win")
    if win is not None:
        win.close()

    def done():
        report["window_closed"] = _state.get("win") is None
        open_ascii_window()
        report["window_reopened"] = _state.get("win") is not None and _state["win"].isVisible()
        with open(os.path.join(out, "selftest_closetip.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log.info("字符画关窗提示自检：报告已写入 %s", out)

    timer = QTimer()
    timer.setSingleShot(True)
    timer.setInterval(CLOSE_TIP_DELAY_MS + 1500)
    timer.timeout.connect(done)
    _state["closetip_selftest_timer"] = timer
    timer.start()


def _room_selftest() -> None:
    """虚拟房间自检：送一张便条当礼物 → 收件 → 让她进房间一次（真实 API）→ 等她出来，写报告。只用于测试用的数据目录。"""
    import json
    import os
    import time as _t

    from PySide6.QtCore import QTimer

    out = _state["selftest_room"]
    rt = _state.get("rt")
    life = rt.room
    report = {"data_dir": str(life.room.root), "start": _t.strftime("%H:%M:%S")}
    try:
        life.room.add_gift_text("自检：这是一张测试便条。房间是你自己的，想做什么都可以。", "测试便条")
        life.check()
        report["pending_after_inbox"] = list(life.state["pending"])
        report["entered"] = life.enter(force=True)
    except Exception as exc:
        report["error"] = repr(exc)
    t0 = _t.monotonic()
    timer = QTimer()
    timer.setInterval(1000)
    _state["room_selftest_timer"] = timer

    def poll():
        if life.busy and _t.monotonic() - t0 < 400:
            return
        timer.stop()
        report["report"] = life.last_report
        report["state"] = {k: life.state[k] for k in ("energy", "boredom", "wakes_today", "tokens_today", "next_wake",
                                                       "pending", "echo")}
        report["tree"] = life.room.tree_text()
        report["log"] = life.room.tail_log(30)
        report["memory"] = life.memory.counts()
        report["short"] = life.memory.short[-5:]
        report["think_text"] = rt.engine.think_text
        with open(os.path.join(out, "selftest_room.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log.info("字符画·房间自检：报告已写入 %s", out)

    timer.timeout.connect(poll)
    timer.start()


def _menu_selftest() -> None:
    """菜单自检：从字符画窗口弹出整合后的菜单，列出全部菜单项并截图，然后直接关掉（不点任何菜单项）。"""
    import json
    import os

    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication

    out = _state["selftest_menu"]
    win = _state.get("win")
    report = {"pet_found": _primary_pet() is not None, "pet_visible": _pet_visible(),
              "pet_menu_installed": _state["pet_menu_installed"]}

    def dump(menu, depth=0):
        items = []
        for a in menu.actions():
            if a.isSeparator():
                items.append("—")
            elif a.menu() is not None and depth < 2:
                items.append({a.text(): dump(a.menu(), depth + 1)})
            else:
                items.append(a.text() + (" ☑" if a.isCheckable() and a.isChecked() else ""))
        return items

    def probe_expr_menu(menu, sub, done):
        """切换表情：在真实桌宠菜单里打开，记下样式、尺寸、能不能滚，再发 3 格滚轮看列表有没有动。"""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QStyle

        info = report.setdefault("expr_menu", {})
        holder = menu                              # v0.8：切换表情和“字符画”在同一层
        act = next((a for a in menu.actions() if a.menu() is not None and a.text() == "切换表情"), None)
        if act is None:
            holder = sub
            act = next((a for a in sub.actions() if a.menu() is not None and a.text() == "切换表情"), None)
        if act is None:
            info["found"] = False
            done()
            return
        info["level"] = "top" if holder is menu else "字符画"
        expr = act.menu()
        sub_act = next((a for a in menu.actions() if a.menu() is sub), None)
        sub.popup(menu.mapToGlobal(menu.actionGeometry(sub_act).topRight()) if sub_act else menu.geometry().topRight())

        def opened():
            # 和 Qt 自己弹子菜单一样，先按默认位置（父菜单右边）弹出；往左挪是 LeftMenus 在 Show 事件里做的
            report["ascii_submenu"] = {"geometry": [sub.x(), sub.y(), sub.width(), sub.height()],
                                       "parent_left": menu.x(), "opens_left": sub.x() + sub.width() <= menu.x() + 6}
            sub.close() if holder is menu else None
            expr.popup(holder.mapToGlobal(holder.actionGeometry(act).topRight()))
            QTimer.singleShot(500, measured)

        def state():
            acts = expr.actions()
            vis = [i for i, a in enumerate(acts) if a.isVisible() and expr.rect().contains(expr.actionGeometry(a))]
            return {"shown": [vis[0], vis[-1]] if vis else None, "items": len(acts), "height": expr.height(),
                    "last_visible": bool(vis) and vis[-1] == len(acts) - 1,
                    "scroll_state": list(expr.scroll_state()) if hasattr(expr, "scroll_state") else None}

        def measured():
            try:
                info.update(found=True, cls=type(expr).__name__, style=type(expr.style()).__name__,
                            scrollable=expr.style().styleHint(QStyle.StyleHint.SH_Menu_Scrollable, None, expr),
                            menu_style=str(expr.property("menuStyle") or ""), qss_len=len(expr.styleSheet()),
                            max_h=expr.maximumHeight(), geometry=[expr.x(), expr.y(), expr.width(), expr.height()],
                            parent_left=holder.x(), opens_left=expr.x() + expr.width() <= holder.x() + 6,
                            size_hint=list(expr.sizeHint().toTuple()), visible=expr.isVisible(),
                            screen=list(expr.screen().availableGeometry().getRect()) if expr.screen() else None,
                            before=state())
                expr.grab().save(os.path.join(out, "selftest_menu_expr.png"))
                scr = expr.screen() or QApplication.primaryScreen()     # 整体布局截图：菜单、子菜单和字符画窗口
                x0 = min(expr.x(), menu.x()) - 10
                y0 = min(expr.y(), menu.y(), win.y()) - 10
                x1 = win.x() + win.width() + 10
                y1 = max(expr.y() + expr.height(), menu.y() + menu.height(), win.y() + win.height()) + 10
                sg = scr.geometry()
                scr.grabWindow(0, x0 - sg.x(), y0 - sg.y(), x1 - x0, y1 - y0).save(os.path.join(out, "selftest_menu_layout.png"))
                pos = QPointF(expr.width() / 2, expr.height() / 2)
                for _ in range(3):
                    QApplication.sendEvent(expr, QWheelEvent(pos, QPointF(expr.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                                                             QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                                                             Qt.NoScrollPhase, False))
                QApplication.processEvents()
                info["after_wheel"] = state()
                info["wheel_calls"] = getattr(expr, "wheel_calls", None)
                expr.grab().save(os.path.join(out, "selftest_menu_expr_wheel.png"))
                if hasattr(expr, "wheelEvent"):                      # 绕过事件过滤器直接调用，看是不是被别人吞了
                    expr.wheelEvent(QWheelEvent(pos, QPointF(expr.mapToGlobal(pos.toPoint())), QPoint(0, 0),
                                                QPoint(0, -120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
                    QApplication.processEvents()
                    info["after_direct_call"] = state()
            except Exception as exc:
                info["error"] = repr(exc)
            done()

        QTimer.singleShot(300, opened)

    def grab_and_close():
        menu = QApplication.activePopupWidget()
        report["menu_found"] = menu is not None
        pending = False
        try:
            if menu is not None:
                report["menu_items"] = dump(menu)
                report["menu_geometry"] = [menu.x(), menu.y(), menu.width(), menu.height()]
                report["ascii_window_geometry"] = [win.x(), win.y(), win.width(), win.height()]
                menu.grab().save(os.path.join(out, "selftest_menu.png"))
                sub = next((a.menu() for a in menu.actions() if a.menu() is not None and a.text() == "字符画"), None)
                if sub is not None:
                    sub.adjustSize()
                    sub.grab().save(os.path.join(out, "selftest_menu_ascii.png"))
                    pending = True
                    probe_expr_menu(menu, sub, menu.close)
        finally:
            if menu is not None and not pending:
                menu.close()

    try:
        QTimer.singleShot(1500, grab_and_close)
        center = win.mapToGlobal(QPoint(win.width() // 2, win.height() // 2))
        report["shown_by_pet_menu"] = pet_menu_from_ascii(center)       # 菜单关闭前不会返回
    except Exception as exc:
        report["error"] = repr(exc)
        log.exception("字符画菜单自检失败")
    report["pet_visible_after"] = _pet_visible()
    with open(os.path.join(out, "selftest_menu.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    log.info("字符画菜单自检：报告已写入 %s", out)


def _create_selftest() -> None:
    """创作实验自检：在真实桌宠进程里立即开始一次“发明新部件”的实验（后台线程、真实 API、视觉评价），
    完成后写报告并截图。只写字符画数据目录（组件库 / 表情库 / 实验记录），不碰会话和记忆。"""
    import json
    import os

    from PySide6.QtCore import QTimer

    out = _state["selftest_create"]
    rt, win = _state.get("rt"), _state.get("win")
    t_start = time.time()
    report = {"vision": rt.vision.status(), "started": rt.experiment_now(kind="invent")}

    def done(rep):
        report.update(experiment={k: v for k, v in rep.items() if k != "usage"}, tokens=(rep.get("usage") or {}).get("total_tokens"),
                      wall_s=round(time.time() - t_start, 1), latency=rt.latency.stats(), vision_after=rt.vision.status(),
                      components=rt.parts.summary(10))

        def finish():
            try:
                if win is not None:
                    win.grab().save(os.path.join(out, "create_window.png"))
            except Exception as exc:
                report["grab_error"] = repr(exc)
            with open(os.path.join(out, "selftest_create.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1, default=str)
            log.info("字符画创作自检：报告已写入 %s", out)

        QTimer.singleShot(2500, finish)

    rt.experiment_done.connect(done)


def _sleep_selftest() -> None:
    """睡眠自检：把“上次互动”往前拨 31 分钟走真正的入睡判断，截几张睡眠图；再从工作线程调用 record_user
    （和对话请求组装时同一个入口，probe=True，不写对话素材、不碰会话和记忆）把她吵醒，截几张吵醒图；最后再睡一次、双击戳醒。"""
    import json
    import os
    import threading

    from PySide6.QtCore import QTimer

    out = _state["selftest_sleep"]
    rt, win = _state.get("rt"), _state.get("win")
    sl = rt.sleep
    report = {"enabled": rt.cfg.sleep_enabled, "after_min": rt.cfg.sleep_after_min,
              "timer_active": rt.sleep_timer.isActive(), "timer_ms": rt.sleep_timer.interval(), "steps": []}

    def snap(name):
        st = {"name": name, "state": sl.state, "expr": rt.current_expression(), "think": rt.engine.think_text,
              "title": win.title.expr.text() if win is not None and win.title is not None else "",
              "effects": [type(e).__name__ for e in rt.engine.effects], "beats": sl.beats}
        try:
            if win is not None:
                win.grab().save(os.path.join(out, f"sleep_{name}.png"))
        except Exception as exc:
            st["error"] = repr(exc)
        report["steps"].append(st)

    def finish():
        report["history"] = sl.history
        report["falls"], report["wakes"] = falls, wakes
        report["woken_by_chat_flag_cleared"] = sl.consume_woken_by_chat() or True
        with open(os.path.join(out, "selftest_sleep.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log.info("字符画睡眠自检：报告已写入 %s", out)

    falls, wakes = [], []
    sl.last_activity -= 31 * 60                      # 相当于 31 分钟没有互动
    report["check_started_sleep"] = sl.check()
    falls.append(sl.last_fall)
    plan = [(1500, lambda: snap("falling")), (16000, lambda: snap("asleep_1")), (32000, lambda: snap("asleep_2")),
            (50000, lambda: snap("asleep_3")),
            (50500, lambda: threading.Thread(target=rt.record_user, args=("（睡眠自检）",), kwargs={"probe": True}).start()),
            (50800, lambda: (wakes.append(sl.last_wake), snap("waking_0"))), (51700, lambda: snap("waking_1")),
            (53000, lambda: snap("waking_2")), (57500, lambda: snap("awake")),
            (58000, lambda: (sl.fall_asleep(), falls.append(sl.last_fall))), (74000, lambda: snap("asleep_again")),
            (74500, lambda: (rt.poke("被戳了一下"), wakes.append(sl.last_wake))), (75200, lambda: snap("poked")),
            (81000, lambda: snap("awake_again")), (81500, finish)]
    from PySide6.QtCore import Qt

    timers = []
    for ms, fn in plan:                                     # 精确计时（默认的粗略计时长延迟会差几秒，顺序会乱）
        t = QTimer(rt)
        t.setSingleShot(True)
        t.setTimerType(Qt.PreciseTimer)
        t.timeout.connect(fn)
        t.start(ms)
        timers.append(t)
    _state["selftest_timers"] = timers


def _vis_selftest() -> None:
    """原桌宠常驻隐藏自检：关掉字符画窗口 → 原桌宠不出来；模拟托盘“显示 / 隐藏” → 原桌宠仍隐藏，字符画窗口重新打开。"""
    import json
    import os

    from PySide6.QtCore import QTimer

    out = _state["selftest_vis"]
    report = {"pet_visible_at_start": _pet_visible(), "ascii_open_at_start": _state.get("win") is not None}
    old = _state.get("win")
    if old is not None:
        old.close()

    def after_close():
        report["pet_visible_after_close"] = _pet_visible()
        report["ascii_open_after_close"] = _state.get("win") is not None
        w = _primary_pet()
        if w is not None:
            w.show()                                        # 托盘的“显示 / 隐藏”就是这样调用的
        QTimer.singleShot(1500, after_tray)

    def after_tray():
        win = _state.get("win")
        report["pet_visible_after_tray_show"] = _pet_visible()
        report["ascii_reopened"] = bool(win is not None and win is not old and win.isVisible())
        report["island_button_visible"] = bool(win is not None and win.island_btn.isVisible())
        with open(os.path.join(out, "selftest_vis.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log.info("字符画原桌宠隐藏自检：报告已写入 %s", out)

    QTimer.singleShot(1500, after_close)


def _island_selftest() -> None:
    """灵动岛栏自检：记录底部栏的内容、屏幕上的灵动岛是否藏好，并截图（只看，不点按钮）。"""
    import json
    import os

    out = _state["selftest_island"]
    win, bar, island = _state.get("win"), _state.get("island_bar"), _island()
    report = {"bar": bar is not None, "island_exists": island is not None,
              "island_visible": bool(island is not None and island.isVisible()),
              "chat_entry_overlay_visible": bool(win is not None and win.chat_btn.isVisible())}
    from PySide6.QtCore import QTimer

    try:
        if win is not None:
            report["window_geometry"] = [win.x(), win.y(), win.width(), win.height()]
            report["island_button_visible"] = win.island_btn.isVisible()
            win.grab().save(os.path.join(out, "selftest_island_closed.png"))
            if win.island_popup is not None:
                win.toggle_island_popup()                   # 模拟点一下“◈ 灵动岛”按钮
    except Exception as exc:
        report["error"] = repr(exc)

    def opened():
        try:
            pop = win.island_popup if win is not None else None
            report["popup_visible"] = bool(pop is not None and pop.isVisible())
            if bar is not None:
                report.update(balance=bar.balance_label.text(), status=bar.status_label.full_text(),
                              tier=bar.tier_label.full_text(), message_len=len(bar.msg_label.full_text()),
                              pet_button=bar.pet_btn.text())
            if pop is not None and pop.isVisible():
                report["popup_geometry"] = [pop.x(), pop.y(), pop.width(), pop.height()]
                pop.grab().save(os.path.join(out, "selftest_island_popup.png"))
                pop.hide()                                  # 看完收起来
        except Exception as exc:
            report["error2"] = repr(exc)
        with open(os.path.join(out, "selftest_island.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log.info("字符画灵动岛自检：报告已写入 %s", out)

    QTimer.singleShot(2500, opened)                         # 等点开时那次余额查询回来


def _chatwin_selftest() -> None:
    """对话框自检：打开“AI 对话”窗口，截图并记录皮肤状态，然后关上（只看，不发消息）。"""
    import json
    import os

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    out = _state["selftest_chatwin"]
    report = {}
    was_open = False
    try:
        inst = (getattr(_state.get("shell"), "_instances", None) or [None])[0]
        prev = getattr(inst, "legacy_chat_window", None)
        was_open = bool(prev is not None and prev.isVisible())
        aw = _state.get("win")
        if aw is not None and aw.chat_btn.isVisible():      # 走左下角的新入口（和用户点击一样）
            aw.grab().save(os.path.join(out, "selftest_ascii_window.png"))
            report["entry"] = "chat_btn"
            report["chat_btn_geometry"] = list(aw.chat_btn.geometry().getRect())
            report["view_size"] = [aw.view.width(), aw.view.height()]
            aw.chat_btn.click()
        else:
            report["entry"] = "open_chat"
            inst.open_chat()
    except Exception as exc:
        report["error"] = repr(exc)

    def grab():
        try:
            wins = [w for w in QApplication.topLevelWidgets() if w.objectName() == "chat-window" and w.isVisible()]
            report["chat_windows"] = len(wins)
            if wins:
                w = wins[0]
                w.grab().save(os.path.join(out, "selftest_chatwin.png"))
                bubbles = list(getattr(w, "_bubbles", []) or [])
                report.update(window=type(w).__module__, bubbles=len(bubbles),
                              ascii_bubbles=sum(1 for b in bubbles if b.property("asciiTheme")),
                              hbar_visible=w.scroll.horizontalScrollBar().isVisible(),
                              max_bubble_width=max((b.width() for b in bubbles), default=0),
                              viewport_width=w.scroll.viewport().width(), dark="#0b0e14" in w.styleSheet(),
                              title=w.title_label.text(), avatar=w.avatar_label.text(),
                              chat_geometry=list(w.frameGeometry().getRect()))
                from . import chat_theme

                ai = [b for b in bubbles if getattr(b, "role", "") != "user"]
                report.update(skin=chat_theme.skin(), line_bubbles=sum(1 for b in bubbles if b.property("lineTheme")),
                              line_sky=bool(w.phone_shell.property("lineSkin")),
                              line_avatars=sum(1 for b in ai if not b.avatar.pixmap().isNull()),
                              line_read=sum(1 for b in bubbles if b.property("lineRead")),
                              line_stamped=sum(1 for b in bubbles if getattr(b, "_line_ts", None) is not None),
                              input_in_row=w.composer.layout().indexOf(w.input) == -1,
                              input_height=w.input.height(), placeholder=w.input.placeholderText())
                aw = _state.get("win")
                if aw is not None:
                    report["ascii_geometry"] = list(aw.frameGeometry().getRect())
                if not was_open:
                    w.close()
        except Exception as exc:
            report["grab_error"] = repr(exc)
        with open(os.path.join(out, "selftest_chatwin.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        log.info("字符画对话框自检：报告已写入 %s", out)

    QTimer.singleShot(2500, grab)


PROBE_SYSTEM = "你是桌宠。用一句话回复，开头用中文括号写出你的神态动作，例如（歪着头眨了眨眼）。"
PROBE_USER = "（这是字符画联动自检消息，不属于正式对话）跟我打个招呼吧。"


def _chat_selftest() -> None:
    """联动自检：用一个独立的 ChatService 发一条探针消息。

    它和聊天窗口走同一个 ChatService → 全局回复监听 → 字符画生成的路径，
    但不经过会话存储和记忆模块的提示词构建，因此不会写入任何聊天记录或记忆。
    """
    import json
    import os
    import time

    out = _state["selftest_dir"]
    try:
        from pet.chat.models import ProviderConfig, SecretStore  # type: ignore
        from pet.chat.providers import OpenAICompatibleProvider  # type: ignore
        from pet.chat.service import ChatService  # type: ignore
        from PySide6.QtCore import QTimer

        from .config import pet_app_config

        chat = pet_app_config().get("chat") or {}
        pid = chat.get("active_provider") or "openai-main"
        cfg = ProviderConfig.from_dict(pid, (chat.get("providers") or {}).get(pid) or {})
        cfg.api_key = (SecretStore().get(cfg.api_key_ref) if cfg.api_key_ref else "") or cfg.api_key
        svc = ChatService(OpenAICompatibleProvider())
        _state["probe"] = svc
        report = {"started": time.strftime("%H:%M:%S"), "model": cfg.model}
        t0 = time.perf_counter()

        def finish(rid, text):
            report["probe_reply"] = text
            report["probe_seconds"] = round(time.perf_counter() - t0, 2)
            log.info("字符画自检：探针回复已完成（%.2fs）", report["probe_seconds"])
            QTimer.singleShot(12000, snapshot)

        def fail(rid, text):
            report["probe_error"] = text
            log.warning("字符画自检：探针请求失败 %s", text)
            snapshot()

        def snapshot():
            rt = _state["rt"]
            last = rt.director.last
            report["ascii_result"] = last.summary() if last else None
            report["inline"] = rt.last_inline
            report["inline_count"] = rt.inline_count
            report["last_created"] = rt.last_created
            report["last_idle"] = rt.last_idle
            report["dialogue_rounds"] = len(rt.dialogue)
            report["dialogue_since_evolve"] = rt.dialogue.since_evolve
            report["idle_state"] = rt.idle_state
            report["custom_presets"] = rt.custom.names()
            report["chat_link"] = rt.cfg.chat_link
            report["face_spec"] = rt.engine.visual_state().get("face")
            report["engine_expression"] = rt.engine.expression
            report["ascii_window_visible"] = bool(_state["win"] and _state["win"].isVisible())
            report["listeners"] = len(ChatService._global_finished_listeners)
            if _state["win"] is not None:
                _state["win"].grab().save(os.path.join(out, "selftest_chat.png"))
            with open(os.path.join(out, "selftest_chat.json"), "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            log.info("字符画自检：报告已写入 %s", out)

        svc.finished.connect(finish)
        svc.error.connect(fail)
        from .inline import INSTRUCTION

        from .inline import custom_memo

        rt = _state["rt"]
        system = PROBE_SYSTEM + ("\n\n" + INSTRUCTION if _inline_active() else "")
        user = _state.get("selftest_prompt") or PROBE_USER
        rt.record_user(user, probe=True)       # 探针不计入表情进化的对话素材
        if _inline_active() and rt.cfg.custom_presets:
            user += custom_memo(rt.custom.names(20))
        report["probe_user"] = user
        svc.send([{"role": "system", "content": system}, {"role": "user", "content": user}], cfg)
        log.info("字符画自检：已发送探针消息（不写入会话与记忆）")
    except Exception:
        log.exception("字符画自检失败")
