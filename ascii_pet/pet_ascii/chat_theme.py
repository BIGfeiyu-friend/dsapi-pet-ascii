"""“AI 对话”经典聊天窗口的 ASCII 风格皮肤（运行时套用，不改桌宠的任何文件）。

风格与字符画窗口一致：深色终端面板、等宽字体的标签、用制表符画的消息边框（┌─[ 名字 ]──┐ │ └──┘）、
生成中的回复末尾有闪烁的块状光标 ▌、背景里有极淡的字符纹理。
可读性优先：正文仍用清晰的中文字体（微软雅黑）并放大到 13px，文字与底色的对比度都在 WCAG AA 以上
（正文约 13:1，次要文字 ≥ 4.5:1）；等宽字体只用在边框、名字、按钮和状态上。

只作用于经典窗口（chat_ui_style = classic）。用户在桌宠设置里给聊天窗口设置了背景图时保持原样。

v1.4.0 起有三种风格（chat_theme）：line（LINE 风格，见 chat_line.py）/ ascii（本文件的字符画风格）/ original（桌宠原样），
这里负责给桌宠的窗口类打补丁，并在三者之间随时切换。
"""
from __future__ import annotations

import logging
import weakref

log = logging.getLogger("pet.ascii")

MONO = '"Cascadia Mono", "Consolas", "Menlo", "Microsoft YaHei UI", "PingFang SC", monospace'
BODY = '"Microsoft YaHei UI", "PingFang SC", "Hiragino Sans GB", "Segoe UI", sans-serif'

C = {
    "bg": "#0b0e14", "panel": "#0f141c", "line": "#2a3344", "line2": "#3b475c",
    "text": "#e6edf3", "text2": "#a7b3c2", "dim": "#7d8aa0",
    "pink": "#ff8cc0", "cyan": "#5fd4ff", "amber": "#f5b84a", "red": "#ff7a7a", "green": "#3ddc97",
    "bubble_ai": "#121925", "bubble_user": "#0f2233", "bubble_err": "#25141a", "bubble_stop": "#161b22",
}
USER_LABEL = "ME"          # 你的消息边框上的名字
FRAME_COLORS = {"user": C["cyan"], "assistant": C["pink"], "streaming": C["amber"], "error": C["red"],
                "stopped": C["dim"]}

SKINS = ("line", "ascii", "original")
SKIN_LABELS = (("line", "LINE 风格"), ("ascii", "ASCII 字符画风格"), ("original", "桌宠原样"))

_state = {"enabled": True, "skin": "ascii", "installed": False, "tile": "", "name": None, "title": "DeepSeek"}
_windows: "weakref.WeakSet" = weakref.WeakSet()


def qss(tile: str = "") -> str:
    tex = f'background-image: url("{tile}"); background-repeat: repeat-xy;' if tile else ""
    c = C
    return f"""
QDialog#chat-window {{ background: transparent; color: {c['text']}; font-family: {BODY}; }}
QFrame#phone-shell {{ background: {c['bg']}; color: {c['text']}; border: 1px solid {c['line2']}; border-radius: 10px; }}
QFrame#chat-title-bar {{ min-height: 60px; background: {c['panel']}; border-bottom: 1px dashed {c['pink']};
    border-top-left-radius: 9px; border-top-right-radius: 9px; }}
QLabel#title-label {{ color: {c['text']}; font-family: {MONO}; font-size: 15px; font-weight: 700; }}
QLabel#subtitle-label {{ color: {c['dim']}; font-family: {MONO}; font-size: 10px; }}
QToolButton#window-minimize-button, QToolButton#window-close-button, QToolButton#window-pin-button {{
    min-width: 28px; max-width: 28px; min-height: 26px; max-height: 26px; border: 1px solid transparent;
    border-radius: 3px; color: {c['text2']}; background: transparent; font-family: {MONO}; font-size: 15px; }}
QToolButton#window-minimize-button:hover, QToolButton#window-pin-button:hover {{ border-color: {c['line2']}; color: {c['text']}; }}
QToolButton#window-close-button:hover {{ border-color: {c['red']}; color: {c['red']}; }}
QToolButton#window-pin-button:checked {{ border-color: {c['cyan']}; color: {c['cyan']}; }}
QFrame#chat-context-bar {{ background: {c['panel']}; border-bottom: 1px solid {c['line']}; }}
QLabel#status-dot {{ color: {c['green']}; font-size: 12px; }}
QLabel#status-dot[state="busy"] {{ color: {c['amber']}; }}
QLabel#status-label {{ color: {c['green']}; font-family: {MONO}; font-size: 11px; font-weight: 700; }}
QLabel#provider-label, QLabel#context-caption {{ color: {c['dim']}; font-family: {MONO}; font-size: 11px; }}
QToolButton#follow-pet-button, QToolButton#new-session-button, QToolButton#rename-session-button,
QToolButton#delete-session-button, QToolButton#clear-session-button {{
    min-height: 28px; padding: 0 9px; border: 1px solid {c['line']}; border-radius: 3px;
    color: {c['text2']}; background: {c['bubble_ai']}; font-family: {MONO}; font-size: 11px; }}
QToolButton#follow-pet-button:hover, QToolButton#new-session-button:hover, QToolButton#rename-session-button:hover,
QToolButton#delete-session-button:hover, QToolButton#clear-session-button:hover {{ border-color: {c['pink']}; color: {c['pink']}; }}
QToolButton#follow-pet-button:checked {{ border-color: {c['pink']}; color: {c['bg']}; background: {c['pink']}; }}
QComboBox#session-combo {{ min-height: 28px; padding: 0 9px; border: 1px solid {c['line']}; border-radius: 3px;
    color: {c['text']}; background: {c['bubble_ai']}; selection-color: {c['text']}; selection-background-color: {c['bubble_ai']}; }}
QComboBox#session-combo:hover, QComboBox#session-combo:focus {{ border: 1px solid {c['pink']}; }}
QComboBox#session-combo QAbstractItemView, QAbstractItemView#session-list {{ color: {c['text']}; background: {c['panel']};
    border: 1px solid {c['line2']}; outline: none; selection-color: {c['bg']}; selection-background-color: {c['pink']}; }}
QAbstractItemView#session-list::item {{ min-height: 28px; padding: 6px 10px; color: {c['text']}; background: {c['panel']}; }}
QAbstractItemView#session-list::item:hover {{ color: {c['pink']}; background: {c['bubble_ai']}; }}
QAbstractItemView#session-list::item:selected {{ color: {c['bg']}; background: {c['pink']}; }}
QComboBox#session-combo QAbstractItemView::item, QComboBox#session-combo QAbstractItemView::item:hover,
QComboBox#session-combo QAbstractItemView::item:selected {{ color: {c['text']}; }}
QScrollArea#message-scroll {{ background: {c['bg']}; border: none; color: {c['text']}; }}
QScrollArea#message-scroll QWidget#qt_scrollarea_viewport {{ background: {c['bg']}; }}
QWidget#message-view, QWidget#message-timeline {{ background-color: {c['bg']}; {tex} }}
QLabel#empty-state {{ color: {c['dim']}; font-family: {MONO}; font-size: 14px; }}
QFrame#message-bubble {{ padding: 21px 15px 16px 15px; border: none; border-radius: 3px; background: {c['bubble_ai']}; }}
QFrame#message-bubble[role="user"] {{ background: {c['bubble_user']}; }}
QFrame#message-bubble[state="error"] {{ background: {c['bubble_err']}; }}
QFrame#message-bubble[state="stopped"] {{ background: {c['bubble_stop']}; }}
QLabel#bubble-meta {{ color: {c['dim']}; font-family: {MONO}; font-size: 10px; }}
QLabel#bubble-body {{ color: {c['text']}; font-family: {BODY}; font-size: 13px; }}
QFrame#message-bubble[role="user"] QLabel#bubble-body {{ color: #eaf6ff; }}
QLabel#bubble-status {{ color: {c['amber']}; font-family: {MONO}; font-size: 10px; }}
QPushButton#retry-button {{ min-height: 24px; padding: 0 10px; border: 1px solid {c['red']}; border-radius: 3px;
    color: {c['red']}; background: transparent; font-family: {MONO}; }}
QPushButton#retry-button:hover {{ color: {c['bg']}; background: {c['red']}; }}
QFrame#chat-composer {{ background: {c['panel']}; border-top: 1px dashed {c['line2']};
    border-bottom-left-radius: 9px; border-bottom-right-radius: 9px; }}
QPlainTextEdit#chat-input {{ padding: 10px 12px; border: 1px solid {c['line']}; border-radius: 3px; color: {c['text']};
    background: {c['bg']}; font-family: {BODY}; font-size: 13px; selection-color: {c['bg']}; selection-background-color: {c['pink']}; }}
QPlainTextEdit#chat-input:focus {{ border: 1px solid {c['cyan']}; }}
QLabel#composer-hint {{ color: {c['dim']}; font-family: {MONO}; font-size: 10px; }}
QPushButton#send-button {{ min-height: 34px; padding: 0 18px; border: 1px solid {c['pink']}; border-radius: 3px;
    color: {c['pink']}; background: transparent; font-family: {MONO}; font-weight: 700; }}
QPushButton#send-button:hover {{ color: {c['bg']}; background: {c['pink']}; }}
QPushButton#send-button:disabled {{ color: #4f5b6d; border-color: {c['line']}; background: transparent; }}
QPushButton#send-button[busy="true"] {{ color: {c['amber']}; border-color: {c['amber']}; }}
QScrollBar:vertical {{ width: 9px; margin: 4px 2px 4px 0; background: transparent; }}
QScrollBar::handle:vertical {{ min-height: 30px; border-radius: 2px; background: {c['line']}; }}
QScrollBar::handle:vertical:hover {{ background: {c['line2']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


AVATAR_QSS = (f"background: transparent; color: {C['pink']}; border: 1px solid {C['pink']}; border-radius: 3px; "
              f"font-family: {MONO}; font-size: 15px; font-weight: 700;")


# ---------------------------------------------------------------------- 对比度（可读性检查用）
def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((_lum(fg), _lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


# ---------------------------------------------------------------------- 背景字符纹理
def make_tile(path: str) -> str:
    """生成一块极淡的字符纹理（约 5% 亮度差，不影响阅读），平铺在消息区背景上。"""
    import random

    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    img = QImage(144, 96, QImage.Format_RGB32)
    img.fill(QColor(C["bg"]))
    p = QPainter(img)
    f = QFont("Consolas")
    f.setPixelSize(11)
    p.setFont(f)
    rng = random.Random(7)
    for _ in range(26):
        p.setPen(QColor(255, 255, 255, rng.choice((9, 12, 15))))
        p.drawText(rng.randrange(0, 136), rng.randrange(10, 94), rng.choice(".:·'`-+*"))
    p.end()
    img.save(path)
    return path.replace("\\", "/")


# ---------------------------------------------------------------------- 消息边框
def _frame_color(bubble) -> str:
    state = getattr(bubble, "state", "normal")
    if state in FRAME_COLORS:
        return FRAME_COLORS[state]
    return FRAME_COLORS["user"] if getattr(bubble, "role", "") == "user" else FRAME_COLORS["assistant"]


def _speaker(bubble) -> str:
    if getattr(bubble, "role", "") == "user":
        return USER_LABEL
    provider = _state.get("name")
    try:
        name = provider() if callable(provider) else ""
    except Exception:
        name = ""
    meta = getattr(bubble, "meta", None)
    return name or (meta.text() if meta is not None else "") or "她"


def paint_frame(bubble, painter) -> None:
    """用制表符画边框：┌─[ 名字 ]────┐ 两侧竖线 └──────┘。名字放在上边框里（她在左，你在右）。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPen

    W, H = bubble.width(), bubble.height()
    font = QFont("Consolas")
    font.setPixelSize(12)
    font.setStyleHint(QFont.Monospace)
    fm = QFontMetrics(font)
    cw = max(5, fm.horizontalAdvance("─"))
    asc, lh = fm.ascent(), fm.height()
    color = QColor(_frame_color(bubble))
    painter.setRenderHint(painter.RenderHint.TextAntialiasing, True)
    painter.setFont(font)
    painter.setPen(color)

    def run(x0: float, x1: float, y: float) -> None:            # 从 x0 到 x1 铺满 ─
        if x1 - x0 < 1:
            return
        n = int((x1 - x0) // cw)
        if n > 0:
            painter.drawText(QPointF(x0, y), "─" * n)
        if x0 + n * cw < x1:                                      # 余下不足一格：补一个，右端对齐
            painter.drawText(QPointF(x1 - cw, y), "─")

    top_y, bot_y = asc + 1, H - (lh - asc) - 1
    label = f"[ {_speaker(bubble)} ]"
    bold = QFont(font)
    bold.setBold(True)
    lw = QFontMetrics(bold).horizontalAdvance(label)
    painter.drawText(QPointF(0, top_y), "┌")
    painter.drawText(QPointF(W - cw, top_y), "┐")
    if getattr(bubble, "role", "") == "user":
        lx = max(cw * 2, W - cw * 2 - lw)
        run(cw, lx, top_y)
        run(lx + lw, W - cw, top_y)
    else:
        lx = cw * 2
        run(cw, lx, top_y)
        run(lx + lw, W - cw, top_y)
    painter.setFont(bold)
    painter.drawText(QPointF(lx, top_y), label)
    painter.setFont(font)
    painter.drawText(QPointF(0, bot_y), "└")
    painter.drawText(QPointF(W - cw, bot_y), "┘")
    run(cw, W - cw, bot_y)
    pen = QPen(color)                                             # 两侧竖线：画成线，保证连续不断
    pen.setWidthF(1.0)
    painter.setPen(pen)
    x_l, x_r = cw / 2.0, W - cw / 2.0
    y0, y1 = lh - 2, H - lh + 3
    if y1 > y0:
        painter.drawLine(QPointF(x_l, y0), QPointF(x_l, y1))
        painter.drawLine(QPointF(x_r, y0), QPointF(x_r, y1))
    painter.setPen(Qt.NoPen)


# ---------------------------------------------------------------------- 套用 / 还原
_orig_bg: dict = {}


def _paint_message_area(win, on: bool) -> None:
    """消息区：它的容器没有对象名，样式表管不到，直接设调色板（深色 + 极淡的字符纹理）。"""
    from PySide6.QtGui import QBrush, QColor, QPalette, QPixmap

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    scroll = getattr(win, "scroll", None)
    host = scroll.widget() if scroll is not None else getattr(win, "message_view", None)
    if host is None:
        return
    # 容器本身画深色 + 纹理；里面堆叠的页面（空状态页、消息页）原本各自填灰色，改成透明
    pages = host.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
    for w, fill in [(host, True)] + [(p, False) for p in pages]:
        if on:
            _orig_bg.setdefault(id(w), (QPalette(w.palette()), w.autoFillBackground()))
            if fill:
                pal = QPalette(w.palette())
                tile = _state.get("tile")
                pal.setBrush(QPalette.Window, QBrush(QPixmap(tile)) if tile else QBrush(QColor(C["bg"])))
                w.setPalette(pal)
            w.setAutoFillBackground(fill)
        elif id(w) in _orig_bg:
            pal, auto = _orig_bg.pop(id(w))
            w.setPalette(pal)
            w.setAutoFillBackground(auto)


class _ResizeWatcher:
    """消息区尺寸变化时重新计算气泡宽度（窗口刚创建时是默认尺寸，显示后才是真正的宽度）。"""
    _filter_cls = None

    @classmethod
    def attach(cls, win) -> None:
        from PySide6.QtCore import QEvent, QObject

        if cls._filter_cls is None:
            class Filter(QObject):
                def eventFilter(self, obj, event):
                    if event.type() == QEvent.Resize:
                        target = self.parent()
                        if target is not None:
                            try:
                                target._update_bubble_widths()
                            except Exception:
                                pass
                    return False

            cls._filter_cls = Filter
        scroll = getattr(win, "scroll", None)
        if scroll is None or getattr(win, "_ascii_resize_filter", None) is not None:
            return
        f = cls._filter_cls(win)
        scroll.viewport().installEventFilter(f)
        win._ascii_resize_filter = f


WIDTH_RATIO = 0.80          # 去掉头像后，气泡可以更宽：每行多放几个字，长消息更好读


def _fit_bubble(bubble, max_width: int) -> None:
    """长消息撑满可用宽度（QLabel 自动换行默认会挑一个偏窄的宽度）；短消息保持紧凑。"""
    body = getattr(bubble, "body", None)
    if body is None:
        return
    if bubble.property("lineTheme"):
        from . import chat_line

        chrome = chat_line.chrome(bubble)
    elif bubble.property("asciiTheme"):
        chrome = 34
    else:
        body.setMinimumWidth(0)
        return
    body.ensurePolished()
    fm = body.fontMetrics()
    natural = max((fm.horizontalAdvance(line) for line in plain_text(body).split("\n")), default=0)
    inner = max(120, max_width - chrome)
    if bubble.property("lineTheme"):                              # LINE：短消息也按一行的宽度，不会无故折行
        body.setMinimumWidth(min(natural + 2, inner) if natural else 0)
        return
    body.setMinimumWidth(min(natural, inner) if natural > inner * 0.6 else 0)


def plain_text(body) -> str:
    """气泡正文的纯文本。上游 4.3 起她的气泡按 Markdown 渲染成富文本，body.text() 是一整段 HTML。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextDocument

    t = body.text() or ""
    fmt = body.textFormat()
    if fmt == Qt.RichText or (fmt == Qt.AutoText and t.lstrip()[:1] == "<" and "</" in t):
        doc = QTextDocument()
        doc.setHtml(t)
        return doc.toPlainText()
    return t


def _apply_bubble(bubble, on: bool) -> None:
    try:
        bubble.setProperty("asciiTheme", bool(on))
        for name in ("avatar", "meta"):                           # 圆头像和名字行由边框上的 [ 名字 ] 代替
            w = getattr(bubble, name, None)
            if w is not None:
                w.setVisible(not on)
        if hasattr(bubble, "_ascii_raw"):
            bubble._refresh_cursor()
        bubble.update()
    except RuntimeError:                                          # 气泡已被删除
        pass


def apply_window(win) -> None:
    """按当前开关给一个聊天窗口套用 / 还原皮肤（_style 会先恢复原样式）。"""
    try:
        win._style()
    except Exception:
        log.exception("字符画：重新套用对话框样式失败")


def _set_skin_state(name) -> None:
    name = name if name in SKINS else "ascii"
    _state["skin"] = name
    _state["enabled"] = name != "original"


def set_skin(name: str) -> None:
    """切换风格（line / ascii / original），已打开的聊天窗口立即生效。"""
    _set_skin_state(name)
    for win in list(_windows):
        apply_window(win)


def skin() -> str:
    return _state["skin"] if _state["enabled"] else "original"


def set_enabled(on: bool) -> None:
    """旧接口：开 = ASCII 风格，关 = 桌宠原样。"""
    set_skin("ascii" if on else "original")


def is_enabled() -> bool:
    """旧接口：现在是不是 ASCII 风格。"""
    return skin() == "ascii"


def _apply_title(win, on: bool) -> None:
    """标题栏：皮肤开着时显示 _state["title"]（默认 DeepSeek），头像字母跟着标题；关掉时还原桌宠原来的文字。"""
    title, avatar = getattr(win, "title_label", None), getattr(win, "avatar_label", None)
    if title is None:
        return
    if on and _state.get("title"):
        if not hasattr(win, "_ascii_orig_title"):
            win._ascii_orig_title = (title.text(), avatar.text() if avatar is not None else "")
        title.setText(_state["title"])
        if avatar is not None:
            avatar.setText(_state["title"][:1].upper())
    elif hasattr(win, "_ascii_orig_title"):
        text, initial = win._ascii_orig_title
        del win._ascii_orig_title
        title.setText(text)
        if avatar is not None:
            avatar.setText(initial)


def install(enabled: bool = True, name_provider=None, tile_path: str = "", title: str = "DeepSeek",
            skin: str | None = None, avatar_path: str = "", character=None) -> None:
    """给经典聊天窗口打补丁（只做一次）。之后可以用 set_skin（或旧接口 set_enabled）随时切换。
    skin：line / ascii / original；不给时按 enabled 选 ascii 或 original。avatar_path / character 给 LINE 头像用。"""
    from . import chat_line

    _set_skin_state(skin or ("ascii" if enabled else "original"))
    _state["name"] = name_provider
    _state["title"] = title
    if avatar_path or character is not None:
        chat_line.configure(avatar_path=avatar_path, character=character)
    if _state["installed"]:
        return
    from PySide6.QtGui import QPainter

    from pet.chat import legacy_widgets as lw  # type: ignore

    Window, Bubble = lw.ChatWindow, lw.MessageBubble
    orig_style = Window._style
    orig_avatar = Window._apply_avatar_style
    orig_widths = Window._update_bubble_widths
    orig_char_theme = Window._apply_character_theme
    orig_add = Window._add
    orig_load = Window._load
    orig_remove = Window._remove_bubble
    orig_init = Bubble.__init__
    orig_set_content = Bubble.set_content
    orig_set_state = Bubble.set_state
    orig_paint = Bubble.paintEvent

    def skin_of(win) -> str:
        if not _state["enabled"] or getattr(win, "_bg_pixmap", None):      # 设置了背景图：保持原样
            return "original"
        return _state["skin"]

    def active(win) -> bool:
        return skin_of(win) != "original"

    def dress(win, b, sk: str) -> None:
        """按风格给一个气泡套用 / 还原（先还原另一种，再套当前这种）。"""
        if sk != "line":
            chat_line.apply_bubble(b, False)
        _apply_bubble(b, sk == "ascii")
        if sk == "line":
            chat_line.apply_bubble(b, True)

    def style(self, *a, **k):
        result = orig_style(self, *a, **k)
        _windows.add(self)
        sk = skin_of(self)
        try:
            if sk == "ascii":
                if tile_path and not _state["tile"]:
                    try:
                        _state["tile"] = make_tile(tile_path)
                    except Exception:
                        _state["tile"] = ""
                self.setStyleSheet(qss())
                label = getattr(self, "avatar_label", None)
                if label is not None:
                    label.setStyleSheet(AVATAR_QSS)
            if sk != "line":
                chat_line.apply_window(self, False)
            _paint_message_area(self, sk == "ascii")
            if sk == "line":
                chat_line.apply_window(self, True)
            _apply_title(self, sk != "original")
            _ResizeWatcher.attach(self)
            for b in list(getattr(self, "_bubbles", []) or []):
                dress(self, b, sk)
            if sk == "line":
                chat_line.regroup(self)
            self._update_bubble_widths()
            self.update()
        except Exception:
            log.exception("字符画：套用对话框皮肤失败")
        return result

    def update_widths(self, *a, **k):
        result = orig_widths(self, *a, **k)
        try:
            scroll = getattr(self, "scroll", None)
            sk = skin_of(self)
            if sk != "original" and scroll is not None:
                vw = scroll.viewport().width()
                if sk == "line":
                    width = max(260, min(vw - 22, int(vw * chat_line.WIDTH_RATIO)))
                else:
                    width = max(320, int(vw * WIDTH_RATIO))
                for b in list(getattr(self, "_bubbles", []) or []):
                    b.setMaximumWidth(width)
                    _fit_bubble(b, width)
            else:
                for b in list(getattr(self, "_bubbles", []) or []):
                    _fit_bubble(b, b.maximumWidth())
        except Exception:
            log.exception("字符画：调整气泡宽度失败")
        return result

    def add(self, role, text, *a, **k):
        bubble = orig_add(self, role, text, *a, **k)
        try:
            if bubble is not None:
                chat_line.stamp(bubble)                        # 新消息：现在的时间（载入历史时下面再换成存下来的时间）
                sk = skin_of(self)
                dress(self, bubble, sk)
                if sk == "line":
                    chat_line.regroup(self)
                    _fit_bubble(bubble, bubble.maximumWidth())
        except Exception:
            log.exception("字符画：新气泡套用皮肤失败")
        return bubble

    def load(self, *a, **k):
        result = orig_load(self, *a, **k)
        try:
            chat_line.stamp_loaded(self)
            if skin_of(self) == "line":
                chat_line.regroup(self)
        except Exception:
            log.exception("字符画：给历史消息对时间失败")
        return result

    def remove_bubble(self, *a, **k):
        result = orig_remove(self, *a, **k)
        try:
            if skin_of(self) == "line":
                chat_line.regroup(self)
        except Exception:
            log.exception("字符画：删除气泡后重新分组失败")
        return result

    def apply_character_theme(self, *a, **k):
        if hasattr(self, "_ascii_orig_title"):       # 换角色：先丢掉旧的“原标题”，由原逻辑重新设置后再覆盖
            del self._ascii_orig_title
        result = orig_char_theme(self, *a, **k)
        if active(self):
            _apply_title(self, True)
        return result

    def apply_avatar_style(self, label, color, *a, **k):
        try:
            sk = skin_of(self)
            if sk == "ascii" and label is getattr(self, "avatar_label", None):
                label.setStyleSheet(AVATAR_QSS)
                return None
            if sk == "line" and label is not getattr(self, "avatar_label", None) and label.objectName() == "bubble-avatar":
                label.setStyleSheet(chat_line.AVATAR_QSS)             # 圆形头像图片自己带形状
                return None
            return orig_avatar(self, label, color, *a, **k)
        except RuntimeError:                  # 上游 4.3 换会话时会给已经删除的头像再套一次样式：跳过
            return None

    def bubble_init(self, *a, **k):
        orig_init(self, *a, **k)
        content = a[1] if len(a) > 1 else k.get("content", "")
        self._ascii_raw = content if isinstance(content, str) else str(content or "")
        self._ascii_shown = self._ascii_raw
        if _state["enabled"]:
            win = self.window()
            if win is None or active(win) or win is self:
                dress(win, self, _state["skin"])

    def shown_text(self, raw: str) -> str:
        streaming = getattr(self, "state", "") == "streaming"
        if self.property("asciiTheme") and streaming:
            return raw + " ▌"                                          # 生成中：末尾的块状光标
        if self.property("lineTheme") and streaming and not raw.strip():
            return chat_line.TYPING                                    # 她还在想：气泡里显示 “···”
        return raw

    def refresh_cursor(self):
        raw = getattr(self, "_ascii_raw", None)
        if raw is None:
            return
        shown = shown_text(self, raw)
        if getattr(self, "_ascii_shown", None) != shown:            # 不拿 body.text() 比：4.3 起那是 HTML
            orig_set_content(self, shown)
            self._ascii_shown = shown
            if self.property("lineTheme") or self.property("asciiTheme"):
                _fit_bubble(self, self.maximumWidth())

    def set_content(self, text, *a, **k):
        self._ascii_raw = text if isinstance(text, str) else str(text)
        shown = shown_text(self, self._ascii_raw)
        result = orig_set_content(self, shown if shown != self._ascii_raw else text, *a, **k)
        self._ascii_shown = shown
        if self.property("asciiTheme") or self.property("lineTheme"):
            _fit_bubble(self, self.maximumWidth())
        return result

    def set_state(self, state, *a, **k):
        result = orig_set_state(self, state, *a, **k)
        self._refresh_cursor()
        if self.property("lineTheme"):
            chat_line.sync_status(self)
            self.update()                                              # 出错 / 停止：气泡颜色跟着变
        return result

    def paint(self, event):
        orig_paint(self, event)
        if self.property("asciiTheme") or self.property("lineTheme"):
            p = QPainter(self)
            try:
                if self.property("lineTheme"):
                    chat_line.paint_bubble(self, p)
                else:
                    paint_frame(self, p)
            finally:
                p.end()

    Window._style = style
    Window._apply_character_theme = apply_character_theme
    Window._apply_avatar_style = apply_avatar_style
    Window._update_bubble_widths = update_widths
    Window._add = add
    Window._load = load
    Window._remove_bubble = remove_bubble
    Bubble.__init__ = bubble_init
    Bubble.set_content = set_content
    Bubble.set_state = set_state
    Bubble._refresh_cursor = refresh_cursor
    Bubble.paintEvent = paint
    _state["installed"] = True
    current = _state["skin"] if _state["enabled"] else "original"       # （参数 skin 遮住了同名函数）
    log.info("字符画：已为“AI 对话”经典窗口装上皮肤（%s）", dict(SKIN_LABELS).get(current, current))
