"""字符画窗口的“灵动岛”：把原桌宠的灵动岛收进窗口右下角的“◈ 灵动岛”按钮，点击弹出这张卡片。

数据仍然来自桌宠自己的灵动岛逻辑（integration 把 DynamicIsland 的 set_balance_info / set_last_message 等转发过来），
这里只负责显示和按钮（IslandBar 也能贴在窗口底部当一栏用，card=False）：

  第一行  余额 · 峰谷时段提示 ·············· ⟳
  第二行  [显示桌宠] [设置] · 最近一条消息

“刚刚查询过 / 查询中…”这类状态不会盖掉已经查到的余额，只作为余额后面的小字。
"""
from __future__ import annotations

import re
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

MONO = "Cascadia Mono"
BG = QColor(18, 24, 38)
LINE = QColor(59, 71, 92)
FLASH = QColor(255, 140, 192)
COLORS = {"balance": "#3ddc97", "status": "#7d8aa0", "peak": "#f5b84a", "idle": "#5fd4ff", "msg": "#a7b3c2"}
BAR_H = 50

BTN_QSS = """
QToolButton { color: #c3cdd9; background: transparent; border: 1px solid #3b475c; border-radius: 3px;
    padding: 1px 7px; font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace; font-size: 11px; }
QToolButton:hover { color: #ff8cc0; border-color: #ff8cc0; }
QToolButton:pressed { color: #0b0e14; background: #ff8cc0; border-color: #ff8cc0; }
"""
_BALANCE_RE = re.compile(r"[¥￥$]\s*-?\d|\d+(?:\.\d+)?\s*(?:元|CNY|USD)")


def is_balance_text(text: str) -> bool:
    """“余额 ¥23.45”是余额；“查询中…”“刚刚查询过，稍后自动更新”“余额 --”是状态。"""
    return bool(_BALANCE_RE.search(text or ""))


def short_balance(text: str) -> str:
    """栏里只放主金额（“余额 ¥12.34”），括号里的充值 / 赠送明细放到悬停提示里。"""
    text = (text or "").strip()
    for sep in ("（", "("):
        if sep in text and is_balance_text(text.split(sep, 1)[0]):
            return text.split(sep, 1)[0].strip()
    return text


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont(MONO)
    f.setFamilies([MONO, "Consolas", "Microsoft YaHei UI"])
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class ElidedLabel(QLabel):
    """一行放不下时末尾显示省略号（完整内容在悬停提示里）。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(10)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 12 else "")
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full.replace("\n", " "), Qt.ElideRight, max(10, self.width())))


class IslandBar(QWidget):
    def __init__(self, parent, on_chat=None, on_toggle_pet=None, on_settings=None, on_refresh=None,
                 pet_visible=None, pricing_tier=None, radius: int = 10, card: bool = False, show_chat: bool = True):
        super().__init__(parent)
        self.setObjectName("ascii-island-bar")
        self.setFixedHeight(BAR_H + (6 if card else 0))
        self.radius = radius
        self.card = card                # True：作为弹出卡片（四角圆、实线边框）；False：贴在窗口底部的一栏
        self.on_flash = None            # 有新事件时额外通知（例如让灵动岛按钮闪一下）
        self._on_toggle_pet, self._on_settings, self._on_refresh = on_toggle_pet, on_settings, on_refresh
        self._pet_visible = pet_visible or (lambda: False)
        self._pricing_tier = pricing_tier or (lambda: None)
        self.last_balance = ""          # 最近一次查到的余额（常驻显示）
        self.status = ""                # 查询中 / 刚刚查询过 / 查询失败 …
        self.tier_text = ""
        self.balance_time = 0.0
        self._flash_until = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8 if card else 6, 8, 8 if card else 5)
        outer.setSpacing(3)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.balance_label = QLabel("余额 --")
        self.balance_label.setFont(_font(12, bold=True))
        self.balance_label.setStyleSheet(f"color:{COLORS['balance']};")
        self.status_label = ElidedLabel("")
        self.status_label.setFont(_font(10))
        self.status_label.setStyleSheet(f"color:{COLORS['status']};")
        self.tier_label = ElidedLabel("")
        self.tier_label.setFont(_font(10))
        self.tier_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.refresh_btn = self._button("⟳", "立即查询余额", self._refresh_clicked)
        row1.addWidget(self.balance_label)
        row1.addWidget(self.status_label, 1)
        row1.addWidget(self.tier_label, 2)
        row1.addWidget(self.refresh_btn)
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.chat_btn = self._button(">_ 对话", "打开 AI 对话", on_chat)
        self.pet_btn = self._button("显示桌宠", "显示 / 隐藏原来的桌宠", self._toggle_pet_clicked)
        self.settings_btn = self._button("设置", "打开灵动岛设置", on_settings)
        self.msg_label = ElidedLabel("")
        self.msg_label.setFont(_font(10))
        self.msg_label.setStyleSheet(f"color:{COLORS['msg']};")
        for b in (self.chat_btn, self.pet_btn, self.settings_btn):
            row2.addWidget(b)
        self.chat_btn.setVisible(show_chat)
        row2.addWidget(self.msg_label, 1)
        outer.addLayout(row1)
        outer.addLayout(row2)
        self._tick = QTimer(self)           # 峰谷时段提示、按钮文字每半分钟刷新一次
        self._tick.timeout.connect(self.refresh_state)
        self._tick.start(30000)
        self.refresh_state()

    def _button(self, text: str, tip: str, fn) -> QToolButton:
        b = QToolButton(self)
        b.setText(text)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.setStyleSheet(BTN_QSS)
        if fn is not None:
            b.clicked.connect(lambda: fn())
        return b

    # ------------------------------------------------------------------ 数据（由灵动岛转发）
    def set_balance(self, tier_text: str, balance_text: str) -> None:
        if tier_text:
            self.tier_text = str(tier_text)
        text = str(balance_text or "").strip()
        if is_balance_text(text):
            self.last_balance, self.status, self.balance_time = text, "", time.time()
        else:
            self.status = text
        self.refresh_state()

    def set_message(self, text: str) -> None:
        text = str(text or "").strip()
        self.msg_label.set_full_text(f"› {text}" if text else "")

    def flash(self, ms: int = 700) -> None:
        self._flash_until = time.monotonic() + ms / 1000
        self.update()
        QTimer.singleShot(ms + 20, self.update)
        if self.on_flash is not None:
            try:
                self.on_flash(ms)
            except Exception:
                pass

    def refresh_state(self) -> None:
        shown = short_balance(self.last_balance)
        self.balance_label.setText(shown or (self.status if self.status and len(self.status) <= 8 else "余额 --"))
        self.balance_label.setToolTip(self.last_balance if self.last_balance and self.last_balance != shown else "")
        status = self.status if self.last_balance or len(self.status) > 8 else ""
        if self.last_balance and self.balance_time:
            age = time.time() - self.balance_time
            when = "刚刚" if age < 90 else (f"{int(age // 60)} 分钟前" if age < 3600 else f"{int(age // 3600)} 小时前")
            status = f"{status} · {when}" if status else when
        self.status_label.set_full_text(status)
        tier = None
        try:
            tier = self._pricing_tier()
        except Exception:
            pass
        self.tier_label.setStyleSheet(f"color:{COLORS.get(tier or '', COLORS['status'])};")
        self.tier_label.set_full_text(self.tier_text)
        try:
            self.pet_btn.setText("隐藏桌宠" if self._pet_visible() else "显示桌宠")
        except Exception:
            pass

    # ------------------------------------------------------------------ 按钮
    def _refresh_clicked(self) -> None:
        if self._on_refresh is not None:
            self._on_refresh()
        self.flash()

    def _toggle_pet_clicked(self) -> None:
        if self._on_toggle_pet is not None:
            self._on_toggle_pet()
        QTimer.singleShot(50, self.refresh_state)

    # ------------------------------------------------------------------ 外观
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h, r = self.width(), self.height(), self.radius
        edge = FLASH if time.monotonic() < self._flash_until else LINE
        if self.card:                                             # 弹出卡片：四角圆 + 实线边框
            body = QPainterPath()
            body.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), r, r)
            p.fillPath(body, BG)
            p.setPen(QPen(edge, 1))
            p.drawPath(body)
            p.end()
            return
        body = QPainterPath()
        body.addRoundedRect(QRectF(0, -r, w, h + r), r, r)       # 只有下面两个角是圆的，贴合窗口圆角
        clip = QPainterPath()
        clip.addRect(QRectF(0, 0, w, h))
        p.fillPath(body.intersected(clip), BG)
        pen = QPen(edge, 1)
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.drawLine(0, 0, w, 0)
        p.end()


class IslandPopup(QWidget):
    """点击灵动岛按钮弹出的卡片：点别处或再点一下按钮就收起。"""

    def __init__(self, bar: IslandBar):
        super().__init__(None, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.bar = bar
        self.closed_at = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(bar)

    def popup_above(self, anchor, width: int) -> None:
        """在 anchor（按钮的全局矩形）上方弹出，右边与按钮对齐；上方放不下就放到下方。"""
        from PySide6.QtGui import QGuiApplication

        self.setFixedWidth(width)
        self.adjustSize()
        x, y = anchor.right() - width + 1, anchor.top() - self.height() - 6
        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            if y < avail.top():
                y = anchor.bottom() + 6
            x = max(avail.left(), min(x, avail.right() - width))
        self.move(x, y)
        self.bar.refresh_state()
        self.show()

    def hideEvent(self, e):
        self.closed_at = time.monotonic()
        super().hideEvent(e)
