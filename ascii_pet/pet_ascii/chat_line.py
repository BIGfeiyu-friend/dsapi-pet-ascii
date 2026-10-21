"""“AI 对话”经典窗口的 LINE 风格皮肤（v1.4.0；运行时套用，不改桌宠的任何文件）。

参照 LINE 手机版的聊天界面（颜色参考其截图取色；与 LINE / LY Corporation 无关，没有使用其任何素材）：
- 背景：淡蓝的天空（#8BABD7 → #9DB8E0）带几朵极淡的云；标题栏透明，深色粗体名字；
- 她的消息：左边圆形头像（她的字符画笑脸），白色圆角气泡，左上角一个小尾巴；
  你的消息：右边绿色气泡（#6CE67B），不显示自己的头像，尾巴在右上角；
- 同一个人在同一分钟里连着发的几条：只有第一条带头像和尾巴（和 LINE 一样）；
- 每条消息外侧底部是发送时间（HH:MM）；她开始回复之后，你的消息旁边显示「既読」；
- 她还在想（回复还是空的）时，气泡里显示 “···”；
- 底部输入栏：白底，圆角的 “Aa” 输入框，右边绿色的「发送」；输入框随内容长高（最多 5 行）。

可读性：正文 #111 在白 / 绿气泡上 ≥ 11:1；时间、既読在天空背景上 ≥ 4.5:1（WCAG AA）。
由 chat_theme.py 调用（那里给桌宠的窗口类打补丁、在 ASCII / LINE / 原样之间切换）。
"""
from __future__ import annotations

import logging
import random
from datetime import datetime

log = logging.getLogger("pet.ascii")

BODY = '"Microsoft YaHei UI", "PingFang SC", "Hiragino Sans GB", "Segoe UI", sans-serif'

L = {
    "sky_top": "#8babd7", "sky_bot": "#9db8e0",
    "bubble_ai": "#ffffff", "bubble_user": "#6ce67b", "bubble_err": "#ffe1e1", "bubble_stop": "#eef0f3",
    "text": "#111111", "time": "#2b3b57", "title": "#0b0f14", "dim": "#4a5568",
    "bar": "#ffffff", "pill": "#f1f2f4", "placeholder": "#8a929c", "rule": "#e3e5e8",
    "send": "#6ce67b", "send_hover": "#5bd96b", "send_text": "#0b2e13", "err_text": "#b42318", "online": "#06c755",
    "avatar_bg": "#0b0e14",
}
WIDTH_RATIO = 0.92          # 一行（头像 + 气泡 + 时间）最多占消息区宽度的比例
TIME_W = 38                 # 气泡外侧放时间 / 既読 的宽度
TAIL_W = 8                  # 尾巴伸出气泡的宽度
PAD_X, PAD_Y = 12, 8        # 气泡内边距
AVATAR = 34                 # 头像直径（和桌宠原来的头像一样大，布局不用变）
GAP = 9                     # 头像和气泡之间
READ_MARK = "既読"
TYPING = "• • •"
PLACEHOLDER = "Aa"
INPUT_MIN, INPUT_MAX_LINES = 36, 5
SHELL_RADIUS = 10

_state = {"avatar_path": "", "character": None}
_avatar_src: dict = {}       # 角色 → 头像原图（QImage，128px 圆形）
_avatar_pm: dict = {}        # (角色, 尺寸, dpr) → QPixmap
_sky_cache: dict = {}


def configure(avatar_path: str = "", character=None) -> None:
    """avatar_path：头像缓存 PNG 的路径模板（含 {character}）；character：角色名或返回角色名的函数。"""
    _state["avatar_path"] = avatar_path
    _state["character"] = character


def _character() -> str:
    c = _state.get("character")
    try:
        c = c() if callable(c) else c
    except Exception:
        c = None
    return str(c or "maid_bust")


def qss() -> str:
    c = L
    return f"""
QDialog#chat-window {{ background: transparent; color: {c['text']}; font-family: {BODY}; }}
QFrame#phone-shell {{ background: transparent; border: 1px solid rgba(20, 40, 80, 46); border-radius: {SHELL_RADIUS}px; }}
QFrame#chat-title-bar {{ min-height: 50px; background: transparent; border: none; }}
QLabel#title-label {{ color: {c['title']}; font-family: {BODY}; font-size: 17px; font-weight: 700; background: transparent; }}
QLabel#subtitle-label {{ color: {c['time']}; font-size: 11px; background: transparent; }}
QToolButton#window-minimize-button, QToolButton#window-close-button, QToolButton#window-pin-button {{
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; border: none; border-radius: 15px;
    background: transparent; color: {c['title']}; }}
QToolButton#window-minimize-button:hover, QToolButton#window-pin-button:hover {{ background: rgba(255, 255, 255, 120); }}
QToolButton#window-close-button:hover {{ background: rgba(229, 72, 77, 210); }}
QToolButton#window-pin-button:checked {{ background: rgba(255, 255, 255, 170); }}
QFrame#chat-context-bar {{ background: rgba(255, 255, 255, 105); border: none; border-bottom: 1px solid rgba(20, 40, 80, 30); }}
QLabel#status-dot {{ color: {c['online']}; font-size: 11px; background: transparent; }}
QLabel#status-dot[state="busy"] {{ color: #e08a00; }}
QLabel#status-label {{ color: #0b5d2a; font-size: 11px; font-weight: 700; background: transparent; }}
QLabel#provider-label, QLabel#context-caption {{ color: {c['time']}; font-size: 11px; background: transparent; }}
QToolButton#follow-pet-button, QToolButton#new-session-button, QToolButton#rename-session-button,
QToolButton#delete-session-button, QToolButton#clear-session-button {{
    min-height: 28px; padding: 0 9px; border: none; border-radius: 14px; color: #1f2a3d;
    background: rgba(255, 255, 255, 185); font-size: 11px; }}
QToolButton#follow-pet-button:hover, QToolButton#new-session-button:hover, QToolButton#rename-session-button:hover,
QToolButton#delete-session-button:hover, QToolButton#clear-session-button:hover {{ background: #ffffff; }}
QToolButton#follow-pet-button:checked {{ color: {c['send_text']}; background: {c['send']}; }}
QComboBox#session-combo {{ min-height: 28px; padding: 0 10px; border: none; border-radius: 14px; color: {c['text']};
    background: rgba(255, 255, 255, 215); selection-color: {c['text']}; selection-background-color: rgba(255, 255, 255, 215); }}
QComboBox#session-combo:hover, QComboBox#session-combo:focus {{ background: #ffffff; }}
QComboBox#session-combo::drop-down {{ border: none; width: 22px; }}
QComboBox#session-combo QAbstractItemView, QAbstractItemView#session-list {{ color: {c['text']}; background: #ffffff;
    border: 1px solid {c['rule']}; outline: none; selection-color: {c['send_text']}; selection-background-color: {c['send']}; }}
QAbstractItemView#session-list::item {{ min-height: 28px; padding: 6px 10px; color: {c['text']}; background: #ffffff; }}
QAbstractItemView#session-list::item:hover {{ background: #eef7ef; }}
QAbstractItemView#session-list::item:selected {{ color: {c['send_text']}; background: {c['send']}; }}
QScrollArea#message-scroll {{ background: transparent; border: none; }}
QScrollArea#message-scroll QWidget#qt_scrollarea_viewport {{ background: transparent; }}
QLabel#empty-state {{ color: {c['time']}; font-size: 14px; background: transparent; }}
QFrame#message-bubble {{ padding: 0px; border: none; background: transparent; }}
QLabel#bubble-body {{ color: {c['text']}; font-family: {BODY}; font-size: 14px; background: transparent;
    selection-color: {c['text']}; selection-background-color: #b9d6ff; }}
QLabel#bubble-status {{ color: {c['dim']}; font-size: 10px; background: transparent; }}
QFrame#message-bubble[state="error"] QLabel#bubble-body {{ color: {c['err_text']}; }}
QPushButton#retry-button {{ min-height: 24px; padding: 0 12px; border: none; border-radius: 12px; color: #ffffff;
    background: #c8303a; font-size: 12px; }}
QPushButton#retry-button:hover {{ background: #a8232c; }}
QFrame#chat-composer {{ background: {c['bar']}; border: none; border-top: 1px solid {c['rule']};
    border-bottom-left-radius: {SHELL_RADIUS - 1}px; border-bottom-right-radius: {SHELL_RADIUS - 1}px; }}
QPlainTextEdit#chat-input {{ padding: 6px 12px; border: none; border-radius: 18px; color: {c['text']};
    background: {c['pill']}; font-family: {BODY}; font-size: 14px; selection-color: {c['text']};
    selection-background-color: #b9d6ff; }}
QLabel#composer-hint {{ color: {c['placeholder']}; font-size: 10px; }}
QPushButton#send-button {{ min-height: 36px; max-height: 36px; padding: 0 16px; border: none; border-radius: 18px;
    color: {c['send_text']}; background: {c['send']}; font-family: {BODY}; font-size: 13px; font-weight: 700; }}
QPushButton#send-button:hover {{ background: {c['send_hover']}; }}
QPushButton#send-button:disabled {{ color: {c['placeholder']}; background: #eceef1; }}
QPushButton#send-button[busy="true"] {{ color: {c['err_text']}; background: {c['bubble_err']}; }}
QScrollBar:vertical {{ width: 8px; margin: 4px 2px 4px 0; background: transparent; }}
QScrollBar::handle:vertical {{ min-height: 30px; border-radius: 3px; background: rgba(20, 40, 80, 70); }}
QScrollBar::handle:vertical:hover {{ background: rgba(20, 40, 80, 120); }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


AVATAR_QSS = "background: transparent; border: none; color: transparent;"


# ---------------------------------------------------------------------- 背景：淡蓝天空 + 几朵极淡的云
def sky_image(w: int, h: int, dpr: float = 1.0):
    key = (int(w), int(h), round(float(dpr), 2))
    img = _sky_cache.get(key)
    if img is None:
        if len(_sky_cache) > 6:
            _sky_cache.clear()
        img = _sky_cache[key] = _make_sky(*key)
    return img


def _make_sky(w: int, h: int, dpr: float):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient

    img = QImage(max(1, int(w * dpr)), max(1, int(h * dpr)), QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0.0, QColor(L["sky_top"]))
    g.setColorAt(1.0, QColor(L["sky_bot"]))
    p.fillRect(0, 0, w, h, g)
    rng = random.Random(11)
    p.setPen(Qt.NoPen)
    n = max(3, int(h / 170))
    for i in range(n):                                        # 每朵云由几个压扁的柔和白色圆叠成
        cx = rng.uniform(-0.1, 1.1) * w
        cy = (i + rng.uniform(0.15, 0.85)) / n * h
        width = rng.uniform(0.45, 0.8) * w
        for _ in range(7):
            bx = cx + rng.uniform(-0.5, 0.5) * width
            by = cy + rng.uniform(-0.06, 0.06) * width
            r = rng.uniform(0.16, 0.32) * width
            rg = QRadialGradient(QPointF(bx, by), r)
            rg.setColorAt(0.0, QColor(255, 255, 255, rng.randint(26, 46)))
            rg.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.save()
            p.translate(bx, by)
            p.scale(1.0, 0.42)
            p.translate(-bx, -by)
            p.setBrush(rg)
            p.drawEllipse(QPointF(bx, by), r, r)
            p.restore()
    p.end()
    return img


class _ShellPainter:
    """在聊天窗口的外壳上画天空（标题栏、会话栏、消息区都透出来）。"""
    _cls = None

    @classmethod
    def attach(cls, shell) -> None:
        from PySide6.QtCore import QEvent, QObject, QRectF
        from PySide6.QtGui import QPainter, QPainterPath

        if cls._cls is None:
            class Filter(QObject):
                def eventFilter(self, obj, event):
                    if event.type() == QEvent.Paint and obj.property("lineSkin"):
                        try:
                            p = QPainter(obj)
                            p.setRenderHint(QPainter.Antialiasing, True)
                            path = QPainterPath()
                            path.addRoundedRect(QRectF(obj.rect()), SHELL_RADIUS, SHELL_RADIUS)
                            p.setClipPath(path)
                            p.drawImage(QRectF(obj.rect()), sky_image(obj.width(), obj.height(), obj.devicePixelRatioF()))
                            p.end()
                        except Exception:
                            log.exception("字符画：画 LINE 背景失败")
                    return False

            cls._cls = Filter
        if getattr(shell, "_line_painter", None) is None:
            f = cls._cls(shell)
            shell.installEventFilter(f)
            shell._line_painter = f


# ---------------------------------------------------------------------- 头像：她的笑脸（圆形）
def _circle(src, size: int, bg: str | None = None):
    """把一张图（QImage）居中裁成正方形，再做成圆形头像（圆外透明）。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

    side = min(src.width(), src.height())
    sq = src.copy((src.width() - side) // 2, (src.height() - side) // 2, side, side)
    out = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, size, size))
    p.setClipPath(path)
    if bg:
        p.fillRect(0, 0, size, size, QColor(bg))
    p.drawImage(QRectF(0, 0, size, size), sq)
    p.end()
    return out


def render_drawn_avatar(character: str, size: int = 128):
    """她的“开心”表情，用面部骨架的手绘差分画成一张普通的图（变成字符画之前的样子），做成圆形头像。
    角色没有面部骨架时返回 None。"""
    from PySide6.QtGui import QImage

    from . import face_rig as fr
    from .character import find_character

    rig = find_character(character).pose(None).rig
    if rig is None:
        return None
    img = rig.draw(fr.normalize({"preset": "happy"})).convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    q = QImage(data, img.width, img.height, img.width * 4, QImage.Format_RGBA8888).copy()
    return _circle(q, size)


def render_avatar(character: str, size: int = 128):
    """头像：优先用手绘差分画的笑脸；没有面部骨架的角色，用独立的离线引擎渲染字符画笑脸。"""
    drawn = render_drawn_avatar(character, size)
    if drawn is not None:
        return drawn
    return render_ascii_avatar(character, size)


def render_ascii_avatar(character: str, size: int = 128):
    """用一个独立的离线引擎渲染她的“开心”表情（字符画），裁出脸部做成圆形头像。"""
    from PySide6.QtGui import QColor

    from .character import find_character
    from .engine import Engine, EngineSettings
    from .render import AsciiView

    eng = Engine(find_character(character), EngineSettings(background="none", code_rows=False), seed=7)
    eng.next_blink = 1e9                                     # 不眨眼
    if eng.rigged:
        eng.set_expression({"preset": "happy"}, 60)
    for _ in range(36):
        eng.tick(1 / 30)
    eng.blink_until, eng.blink_hold = 0.0, False
    chars, colors, w, h, off = eng.frame()
    view = AsciiView(cell_h=8, glow=0.35, scanlines=0.0, pad=0, background=int(QColor(L["avatar_bg"]).rgba()))
    try:
        view.set_frame(chars, colors, w, h, off)
        src = view._img
        dpr = src.devicePixelRatio()
        cw, ch = view._cache.cell_w * dpr, view._cache.cell_h * dpr
        fx, fy, fw, fh = eng.face_rect() or (0, 0, w, h)
        side = min(fw * 1.3 * cw, src.width(), src.height())
        cx, cy = (fx + fw / 2) * cw, (fy + fh * 0.52) * ch
        x0 = int(max(0, min(src.width() - side, cx - side / 2)))
        y0 = int(max(0, min(src.height() - side, cy - side / 2)))
        crop = src.copy(x0, y0, int(side), int(side))
        crop.setDevicePixelRatio(1.0)
    finally:
        view.deleteLater()
    return _circle(crop, size, L["avatar_bg"])


def _fallback_avatar(size: int = 128):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    out = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ff8cc0"))
    p.drawEllipse(QRectF(0, 0, size, size))
    f = QFont("Segoe UI")
    f.setPixelSize(int(size * 0.5))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, "D")
    p.end()
    return out


def avatar_image(character: str | None = None):
    """头像原图：先读缓存的 PNG，没有就现渲染一次并存下来；都失败时用粉色圆 + “D”。"""
    from PySide6.QtGui import QImage

    character = character or _character()
    img = _avatar_src.get(character)
    if img is not None:
        return img
    path = _state.get("avatar_path") or ""
    path = path.format(character=character) if path else ""
    if path:
        cached = QImage(path)
        if not cached.isNull():
            _avatar_src[character] = cached
            return cached
    try:
        img = render_avatar(character)
        if path:
            try:
                img.save(path)
            except Exception:
                pass
    except Exception:
        log.exception("字符画：渲染 LINE 头像失败（改用粉色圆形头像）")
        img = _fallback_avatar()
    _avatar_src[character] = img
    return img


def avatar_pixmap(size: int = AVATAR, dpr: float = 1.0):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    character = _character()
    key = (character, size, round(float(dpr), 2))
    pm = _avatar_pm.get(key)
    if pm is None:
        px = max(1, int(round(size * dpr)))
        pm = QPixmap.fromImage(avatar_image(character).scaled(px, px, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        pm.setDevicePixelRatio(dpr)
        _avatar_pm[key] = pm
    return pm


# ---------------------------------------------------------------------- 时间 / 既読 / 分组
def parse_time(value):
    """会话里存的 created_at（UTC 的 ISO 时间）→ 本地时间；解析不了返回 None。"""
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo else value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone() if dt.tzinfo else dt


def stamp(bubble, when=None) -> None:
    """记下这条消息的时间（没给就是现在）。"""
    bubble._line_ts = parse_time(when) if when is not None else datetime.now().astimezone()
    if bubble._line_ts is not None:
        bubble.setToolTip(bubble._line_ts.strftime("%Y-%m-%d %H:%M"))


def _minute(bubble):
    ts = getattr(bubble, "_line_ts", None)
    return ts.strftime("%Y%m%d%H%M") if ts is not None else None


def time_text(bubble) -> str:
    ts = getattr(bubble, "_line_ts", None)
    hm = ts.strftime("%H:%M") if ts is not None else ""
    if getattr(bubble, "role", "") == "user" and bubble.property("lineRead"):
        return f"{READ_MARK}\n{hm}" if hm else READ_MARK
    return hm


def stamp_loaded(win) -> None:
    """会话刚载入：把每条消息存下来的时间对到气泡上（桌宠按顺序给每条消息建一个气泡）。"""
    session = getattr(win, "session", None)
    msgs = list(getattr(session, "messages", None) or [])
    bubbles = list(getattr(win, "_bubbles", []) or [])
    if len(msgs) != len(bubbles):
        return
    for b, m in zip(bubbles, msgs):
        when = getattr(m, "created_at", None)
        if when:
            stamp(b, when)


def _flag(bubble, name: str, value: bool) -> bool:
    if bubble.property(name) is value:
        return False
    bubble.setProperty(name, value)
    return True


def regroup(win) -> None:
    """LINE 的分组：同一个人在同一分钟里连着发的几条，只有第一条带头像和尾巴；
    你的消息后面只要有她的气泡（她已经开始回了），就显示「既読」。"""
    bubbles = [b for b in list(getattr(win, "_bubbles", []) or []) if _alive(b)]
    later_ai = False
    for b in reversed(bubbles):
        if getattr(b, "role", "") == "user":
            if _flag(b, "lineRead", later_ai):
                b.update()
        else:
            later_ai = True
    prev = None
    for b in bubbles:
        first = prev is None or getattr(prev, "role", "") != getattr(b, "role", "") or _minute(prev) != _minute(b)
        if _flag(b, "lineFirst", first) and b.property("lineTheme"):
            _margins(b)
            _set_avatar(b)
            b.update()
        prev = b


def _alive(w) -> bool:
    try:
        w.objectName()
        return True
    except RuntimeError:
        return False


# ---------------------------------------------------------------------- 气泡
def _inner(bubble):
    lay = bubble.layout()
    if lay is None:
        return None
    for i in range(lay.count()):
        sub = lay.itemAt(i).layout()
        if sub is not None:
            return sub
    return None


def chrome(bubble) -> int:
    """气泡里除了正文以外占的宽度（内边距、时间、头像或尾巴）。"""
    side = TAIL_W if getattr(bubble, "role", "") == "user" else AVATAR + GAP
    return 2 * PAD_X + TIME_W + side + 2


def _margins(bubble) -> None:
    lay = bubble.layout()
    if lay is None:
        return
    top = 6 if bubble.property("lineFirst") is not False else 0
    if getattr(bubble, "role", "") == "user":
        lay.setContentsMargins(TIME_W, top, TAIL_W, 0)
    else:
        lay.setContentsMargins(0, top, TIME_W, 0)


def _set_avatar(bubble) -> None:
    from PySide6.QtGui import QPixmap

    av = getattr(bubble, "avatar", None)
    if av is None or getattr(bubble, "role", "") == "user":
        return
    av.setStyleSheet(AVATAR_QSS)
    if bubble.property("lineFirst") is False:                # 连着发的后几条：留出头像的位置，但不画头像
        av.setPixmap(QPixmap())
        av.setText("")
        return
    try:
        av.setPixmap(avatar_pixmap(AVATAR, av.devicePixelRatioF()))
    except Exception:
        log.exception("字符画：设置 LINE 头像失败")


def apply_bubble(bubble, on: bool) -> None:
    """给一个气泡套用 / 还原 LINE 样式（只改布局边距、头像和名字行；气泡本身在 paint_bubble 里画）。"""
    try:
        lay, inner = bubble.layout(), _inner(bubble)
        if lay is None or inner is None:
            return
        user = getattr(bubble, "role", "") == "user"
        saved = getattr(bubble, "_line_saved", None)
        if on:
            if saved is None:
                bubble._line_saved = {"outer": lay.contentsMargins(), "spacing": lay.spacing(),
                                      "inner": inner.contentsMargins(), "inner_spacing": inner.spacing(),
                                      "avatar_text": bubble.avatar.text()}
            bubble.setProperty("lineTheme", True)
            bubble.meta.setVisible(False)                     # 1 对 1 的 LINE 不显示名字
            bubble.avatar.setVisible(not user)                # 自己的消息不显示头像
            lay.setSpacing(GAP)
            inner.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
            inner.setSpacing(2)
            _margins(bubble)
            _set_avatar(bubble)
            sync_status(bubble)
            if getattr(bubble, "_line_ts", None) is None:
                stamp(bubble)
        elif saved is not None:
            from PySide6.QtGui import QPixmap

            del bubble._line_saved
            bubble.setProperty("lineTheme", False)
            lay.setContentsMargins(saved["outer"])
            lay.setSpacing(saved["spacing"])
            inner.setContentsMargins(saved["inner"])
            inner.setSpacing(saved["inner_spacing"])
            bubble.avatar.setPixmap(QPixmap())
            bubble.avatar.setText(saved["avatar_text"])
            bubble.avatar.setVisible(True)
            bubble.meta.setVisible(True)
            status = getattr(bubble, "status_label", None)
            if status is not None and getattr(bubble, "state", "") == "streaming":
                status.setVisible(bool(status.text()))
        bubble.update()
    except RuntimeError:                                       # 气泡已被删除
        pass


def sync_status(bubble) -> None:
    """生成中：LINE 里只显示气泡（“• • •” 或正在长出来的回复），不要气泡里那行“正在生成…”。"""
    status = getattr(bubble, "status_label", None)
    if status is not None and bubble.property("lineTheme") and getattr(bubble, "state", "") == "streaming":
        status.setVisible(False)


def _tail(r, user: bool):
    """LINE 的小尾巴：从气泡上边沿向头像那一侧伸出一个尖，再弯回气泡侧边。"""
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    t = r.top()
    if user:
        x = r.right()
        path.moveTo(x - 14, t)
        path.lineTo(x + 7, t)
        path.quadTo(x + 1, t + 3, x - 1, t + 12)
        path.lineTo(x - 14, t + 12)
    else:
        x = r.left()
        path.moveTo(x + 14, t)
        path.lineTo(x - 7, t)
        path.quadTo(x - 1, t + 3, x + 1, t + 12)
        path.lineTo(x + 14, t + 12)
    path.closeSubpath()
    return path


def bubble_rect(bubble):
    from PySide6.QtCore import QRectF

    inner = _inner(bubble)
    return QRectF(inner.geometry()) if inner is not None else QRectF()


def paint_bubble(bubble, painter) -> None:
    """画气泡（圆角 + 第一条的小尾巴）和外侧的时间 / 既読。正文由气泡里原来的标签显示。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainterPath

    r = bubble_rect(bubble)
    if r.width() < 4 or r.height() < 4:
        return
    user = getattr(bubble, "role", "") == "user"
    state = getattr(bubble, "state", "normal")
    fill = L["bubble_user"] if user else L["bubble_ai"]
    if state == "error":
        fill = L["bubble_err"]
    elif state == "stopped" and not user:
        fill = L["bubble_stop"]
    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    rad = min(17.0, r.height() / 2)
    path = QPainterPath()
    path.addRoundedRect(r, rad, rad)
    if bubble.property("lineFirst") is not False:
        path = path.united(_tail(r, user))
    painter.fillPath(path, QColor(fill))
    text = time_text(bubble)
    if text:
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(10)
        painter.setFont(f)
        painter.setPen(QColor(L["time"]))
        if user:
            box = QRectF(r.left() - TIME_W, r.top(), TIME_W - 5, r.height())
            painter.drawText(box, int(Qt.AlignRight | Qt.AlignBottom), text)
        else:
            box = QRectF(r.right() + 5, r.top(), TIME_W - 5, r.height())
            painter.drawText(box, int(Qt.AlignLeft | Qt.AlignBottom), text)


# ---------------------------------------------------------------------- 窗口：标题栏、会话栏、消息区、输入栏
def fit_input(inp) -> None:
    """LINE 的输入框随内容长高：一行起，最多 INPUT_MAX_LINES 行，再多就在框里滚动。"""
    from PySide6.QtCore import Qt

    try:
        if not inp.property("lineSkin"):
            return
        inp.ensurePolished()
        doc = inp.document()
        lines = max(1, int(round(doc.documentLayout().documentSize().height())))
        lh = inp.fontMetrics().lineSpacing()
        chrome_h = max(0, inp.height() - inp.viewport().height()) + int(doc.documentMargin() * 2)
        slack = 4                                             # 按行滚动的滚动条算可见行数时要留一点余量，否则一行也会出滚动条
        lo = max(INPUT_MIN, lh + chrome_h + slack)
        h = max(lo, min(lines, INPUT_MAX_LINES) * lh + chrome_h + slack)
        if inp.minimumHeight() != h or inp.maximumHeight() != h:
            inp.setFixedHeight(h)
        inp.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if lines > INPUT_MAX_LINES else Qt.ScrollBarAlwaysOff)
    except RuntimeError:
        pass


def _composer(win, on: bool) -> None:
    """输入栏改成 LINE 那样的一行：[ Aa 圆角输入框 ][ 发送 ]（原来是输入框在上、按钮在下）。"""
    from PySide6.QtCore import Qt

    comp = getattr(win, "composer", None)
    inp, send = getattr(comp, "input", None), getattr(comp, "send", None)
    if comp is None or inp is None or send is None or comp.layout() is None:
        return
    vbox, hint = comp.layout(), getattr(comp, "hint", None)
    row = None
    for i in range(vbox.count()):
        sub = vbox.itemAt(i).layout()
        if sub is not None and sub.indexOf(send) >= 0:
            row = sub
    if row is None:
        return
    saved = getattr(comp, "_line_saved", None)
    if on:
        if saved is None:
            item = row.itemAt(row.indexOf(send))
            saved = comp._line_saved = {
                "margins": vbox.contentsMargins(), "spacing": vbox.spacing(), "row_spacing": row.spacing(),
                "min": inp.minimumHeight(), "max": inp.maximumHeight(), "placeholder": inp.placeholderText(),
                "send_min": send.minimumWidth(), "send_align": item.alignment() if item else Qt.Alignment(),
                "vsb": inp.verticalScrollBarPolicy(),
                "hint": bool(hint is not None and hint.isVisibleTo(comp))}
            vbox.removeWidget(inp)
            row.insertWidget(0, inp, 1)
            for i in range(row.count()):
                if row.itemAt(i).spacerItem() is not None:
                    row.setStretch(i, 0)
            row.setAlignment(send, Qt.AlignBottom)
        vbox.setContentsMargins(10, 8, 8, 8)
        row.setSpacing(8)
        if hint is not None:
            hint.hide()
        inp.setPlaceholderText(PLACEHOLDER)
        send.setMinimumWidth(60)
        if not getattr(inp, "_line_fit_connected", False):
            inp.textChanged.connect(lambda i=inp: fit_input(i))
            inp._line_fit_connected = True
        inp.setProperty("lineSkin", True)
        fit_input(inp)
    elif saved is not None:
        del comp._line_saved
        inp.setProperty("lineSkin", False)
        row.removeWidget(inp)
        vbox.insertWidget(0, inp)
        for i in range(row.count()):
            if row.itemAt(i).spacerItem() is not None:
                row.setStretch(i, 1)
        row.setAlignment(send, saved["send_align"])
        vbox.setContentsMargins(saved["margins"])
        vbox.setSpacing(saved["spacing"])
        row.setSpacing(saved["row_spacing"])
        inp.setMinimumHeight(saved["min"])
        inp.setMaximumHeight(saved["max"])
        inp.setPlaceholderText(saved["placeholder"])
        inp.setVerticalScrollBarPolicy(saved["vsb"])
        send.setMinimumWidth(saved["send_min"])
        if hint is not None:
            hint.setVisible(saved["hint"])


def _chrome_widgets(win, on: bool) -> None:
    """标题栏去掉方形头像和副标题（LINE 只显示名字）；消息列表的边距和间距收紧一些。"""
    saved = getattr(win, "_line_saved", None)
    host = getattr(win, "message_host_layout", None)
    labels = [w for w in (getattr(win, "avatar_label", None), getattr(win, "subtitle_label", None)) if w is not None]
    if on:
        if saved is None:
            saved = win._line_saved = {
                "labels": [(w, w.isVisibleTo(w.parentWidget()) if w.parentWidget() else True) for w in labels],
                "host": (host.contentsMargins(), host.spacing()) if host is not None else None}
        for w in labels:
            w.hide()
        if host is not None:
            host.setContentsMargins(10, 12, 10, 14)
            host.setSpacing(4)
    elif saved is not None:
        del win._line_saved
        for w, vis in saved["labels"]:
            w.setVisible(vis)
        if host is not None and saved["host"] is not None:
            host.setContentsMargins(saved["host"][0])
            host.setSpacing(saved["host"][1])


def apply_window(win, on: bool) -> None:
    """给一个聊天窗口套用 / 还原 LINE 皮肤（窗口级的部分；气泡由 apply_bubble 处理）。"""
    shell = getattr(win, "phone_shell", None)
    if on:
        win.setStyleSheet(qss())
        if shell is not None:
            _ShellPainter.attach(shell)
            shell.setProperty("lineSkin", True)
        try:
            win._set_background_surface_transparency(True)      # 消息区透明，透出外壳上画的天空
        except Exception:
            pass
    elif shell is not None and shell.property("lineSkin"):
        shell.setProperty("lineSkin", False)
    win.setProperty("lineSkin", bool(on))
    _chrome_widgets(win, on)
    _composer(win, on)
    if shell is not None:
        shell.update()
