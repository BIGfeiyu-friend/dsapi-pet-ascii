"""桌宠窗口。

window_style:
  framed   （默认）不透明的深色面板 + 标题栏（名字、当前表情、状态灯、按钮）+ 圆角与阴影，看起来像一个独立的小窗口
  floating 透明悬浮（旧样式，字符直接浮在桌面上）
操作：拖动任意位置移动；标题栏的 — 最小化到任务栏（点任务栏图标恢复）；双击标题栏收起/展开；双击画面戳她一下；
Ctrl+滚轮调大小；右键菜单。窗口是普通的顶层窗口：任务栏里有图标（显示她现在的脸），也会出现在 Alt+Tab 里。
"""
from __future__ import annotations

import logging
import random
import time

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QInputDialog, QLabel, QMenu, QSizePolicy, QToolButton,
                               QVBoxLayout, QWidget)

from . import face_rig as fr
from .render import AsciiView, wide_font_family
from .runtime import Runtime

POKES = ["被戳了一下，吓了一跳", "被摸了摸头，很开心", "被点了一下，疑惑地看过来", "被戳脸颊，有点害羞",
         "被打扰了，假装生气地鼓起脸"]

PANEL = QColor(11, 14, 20)
PANEL_ARGB = 0xFF0B0E14
BORDER = QColor(48, 60, 88)
TITLE_BG = QColor(18, 24, 38)
SHADOW = 10
RADIUS = 10
TITLE_H = 28

# 左下角的“AI 对话”入口：像终端里的一个小标签，半透明深底 + 细边框，平时低调，悬停时变粉（与标题栏按钮一致）
CHAT_BTN_QSS = """
QToolButton#ascii-chat-entry {
    color: #c3cdd9; background: rgba(11, 14, 20, 236); border: 1px solid #3b475c; border-radius: 3px;
    padding: 2px 8px; font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace; font-size: 11px;
}
QToolButton#ascii-chat-entry:hover { color: #ff8cc0; border-color: #ff8cc0; background: rgba(11, 14, 20, 246); }
QToolButton#ascii-chat-entry:pressed { color: #0b0e14; border-color: #ff8cc0; background: #ff8cc0; }
"""
CHAT_BTN_MARGIN = 8


class ScrollMenu(QMenu):
    """限高、能用鼠标滚轮上下滑动的菜单（切换表情用）。

    Qt 自带的菜单滚动只在菜单比整块屏幕还高时才启用：限制最大高度的话，多出来的项会被直接裁掉，滚轮也没反应。
    所以这里自己做“窗口”：菜单项多于 max_rows 项、或比 max_frac × 屏幕可用高度还高时，只显示连续的一段菜单项，其余的暂时隐藏；
    滚轮（一格 3 项，触控板的小幅滚动会累积）和方向键移动这一段。外观完全是桌宠菜单原来的样子。
    上下还有内容时，在菜单上下留白处画个小箭头。
    """
    ROWS_PER_NOTCH = 3
    wheel_calls = 0

    def __init__(self, title: str, parent=None, max_frac: float = 0.6, max_rows: int = 20):
        super().__init__(title, parent)
        self.max_frac = max_frac
        self.max_rows = max_rows       # 一次最多显示几项（菜单太长看着累，滚动也更明显）
        self._wheel = 0
        self._top = 0                  # 窗口里第一项的下标
        self._rows = 0                 # 窗口一次显示几项；0 = 不需要滚动，全部显示
        self.aboutToShow.connect(self._setup_window)
        self.aboutToHide.connect(self._show_all)

    def _show_all(self) -> None:
        self.setMinimumWidth(0)
        for a in self.actions():
            a.setVisible(True)
        self._rows = 0

    def _setup_window(self) -> None:
        self._show_all()
        acts = self.actions()
        if not acts:
            return
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        limit = max(240, int((scr.availableGeometry().height() if scr is not None else 800) * self.max_frac))
        full = self.sizeHint()                      # 顺带算好每项的位置
        rects = [self.actionGeometry(a) for a in acts]
        row = max(r.height() for r in rects) or 1
        pad = max(0, min(r.top() for r in rects))
        rows = max(5, min(self.max_rows, (limit - 2 * pad) // row))
        if len(acts) <= rows:
            return
        self._rows = rows
        # 宽度固定成单列的宽度（菜单比屏幕还高时 Qt 会排成好几列，sizeHint 是几列加起来的宽度），滚动时不跟着变
        col_w = max(r.width() for r in rects)
        ncols = len({r.x() for r in rects})
        self.setMinimumWidth(col_w + max(0, full.width() - ncols * col_w))
        cur = next((a for a in acts if a.isCheckable() and a.isChecked()), None)
        idx = acts.index(cur) if cur is not None else 0
        self._top = -1
        self.scroll_to(idx - self._rows // 2)       # 打开时把现在的表情放在中间

    def scroll_to(self, top: int) -> None:
        acts = self.actions()
        if not self._rows:
            return
        top = max(0, min(len(acts) - self._rows, top))
        if top == self._top:
            return
        self._top = top
        self.setUpdatesEnabled(False)
        for i, a in enumerate(acts):
            a.setVisible(top <= i < top + self._rows)
        self.setUpdatesEnabled(True)
        self.update()

    def scroll_state(self) -> tuple[bool, bool]:
        """(上面还有, 下面还有)"""
        if not self._rows:
            return False, False
        return self._top > 0, self._top + self._rows < len(self.actions())

    def wheelEvent(self, e) -> None:
        self.wheel_calls += 1
        if not self._rows:
            return super().wheelEvent(e)
        self._wheel += e.angleDelta().y()
        step = 120 // self.ROWS_PER_NOTCH
        moved = 0
        while abs(self._wheel) >= step:
            sign = 1 if self._wheel > 0 else -1
            self._wheel -= sign * step
            moved -= sign
        if moved:
            self.scroll_to(self._top + moved)
        e.accept()

    def keyPressEvent(self, e) -> None:
        if self._rows and e.key() in (Qt.Key_Down, Qt.Key_Up):
            acts = self.actions()
            cur = self.activeAction()
            if cur in acts:
                i, d = acts.index(cur), (1 if e.key() == Qt.Key_Down else -1)
                j = i + d
                while 0 <= j < len(acts) and (acts[j].isSeparator() or not acts[j].isEnabled()):
                    j += d
                if 0 <= j < len(acts) and not (self._top <= j < self._top + self._rows):
                    self.scroll_to(j if d < 0 else j - self._rows + 1)
                    self.setActiveAction(acts[j])
                    return
        super().keyPressEvent(e)

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        up, down = self.scroll_state()
        if not (up or down):
            return
        vis = [a for a in self.actions() if a.isVisible()]
        if not vis:
            return
        top_pad = self.actionGeometry(vis[0]).top()
        bottom_pad = self.height() - self.actionGeometry(vis[-1]).bottom()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.palette().color(self.foregroundRole()))
        c.setAlpha(150)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cx = self.width() / 2
        for on, pad, y0, d in ((up, top_pad, 0, -1), (down, bottom_pad, self.height(), 1)):
            if on:
                h = max(2.5, min(5.0, pad - 2))     # 画在留白里，不压住菜单项
                y = y0 - d * pad / 2
                path = QPainterPath()
                path.moveTo(cx - h, y - h / 2 * d)
                path.lineTo(cx + h, y - h / 2 * d)
                path.lineTo(cx, y + h / 2 * d)
                path.closeSubpath()
                p.drawPath(path)
        p.end()


class LeftMenus(QObject):
    """右键菜单的每一级都往左展开（字符画窗口在右边，往右展开会挡住动画）。

    菜单开着的期间装一个应用级事件过滤器：某个子菜单即将显示（Show 事件在窗口真正出现之前）时，
    把它挪到父菜单的左边、和父菜单里对应那一项对齐；左边放不下才保持 Qt 默认的右边。
    anchor（全局坐标的 QRect）给出时，顶层菜单也放在 anchor 的左边（独立运行时用字符画窗口当 anchor）。
    菜单关掉后自动卸下。
    """
    OVERLAP = 3

    def __init__(self, top: QMenu, anchor: QRect | None = None):
        super().__init__(top)
        self.top, self.anchor = top, anchor
        self.chain = {top}
        self.placed = []                         # [(菜单标题, 全局 x, 父菜单左边)]，测试 / 自检用
        QApplication.instance().installEventFilter(self)
        top.aboutToHide.connect(lambda: QTimer.singleShot(0, self._remove))

    def _remove(self):
        if self.top.isVisible():
            return
        try:
            QApplication.instance().removeEventFilter(self)
        except RuntimeError:
            pass

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QMenu):
            try:
                if obj is self.top:
                    if self.anchor is not None:
                        self._place_top(obj)
                else:
                    parent, act = self._parent_of(obj)
                    if parent is not None:
                        self._place_left(obj, parent, act)
                        self.chain.add(obj)
            except RuntimeError:                 # 菜单已经被删掉
                pass
        return False

    def _parent_of(self, menu):
        for m in list(self.chain):
            for a in m.actions():
                if a.menu() is menu:
                    return m, a
        return None, None

    @staticmethod
    def _screen_rect(w):
        scr = w.screen() or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def _place_top(self, menu):
        w, h = menu.width() or menu.sizeHint().width(), menu.height() or menu.sizeHint().height()
        scr = self._screen_rect(menu)
        x = self.anchor.left() - w - 4
        if x < scr.left():
            return
        y = max(scr.top(), min(QCursor.pos().y() - 12, scr.bottom() - h))
        menu.move(x, y)

    def _place_left(self, sub, parent, act):
        w, h = sub.width() or sub.sizeHint().width(), sub.height() or sub.sizeHint().height()
        pr = parent.geometry()
        scr = self._screen_rect(parent)
        x = pr.left() - w + self.OVERLAP
        if x < scr.left():                       # 左边放不下：保持 Qt 默认（右边）
            return
        y = sub.y()
        if act is not None:
            y = parent.mapToGlobal(parent.actionGeometry(act).topLeft()).y()
            first = next((a for a in sub.actions() if a.isVisible() and not a.isSeparator()), None)
            if first is not None:
                y -= sub.actionGeometry(first).top()
        y = max(scr.top(), min(y, scr.bottom() - h))
        sub.move(x, y)
        self.placed.append((sub.title(), x, pr.left()))


class TitleBar(QWidget):
    def __init__(self, win: "AsciiPetWindow"):
        super().__init__(win)
        self.win = win
        self.hold_until = 0.0
        self.setFixedHeight(TITLE_H)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 4, 0)
        lay.setSpacing(4)
        font = QFont(wide_font_family())
        font.setPixelSize(12)
        self.dot = QLabel("●")
        self.dot.setFont(font)
        self.name = QLabel(win.rt.pet_name())
        bold = QFont(font)
        bold.setBold(True)
        self.name.setFont(bold)
        self.name.setStyleSheet("color:#d6def0;")
        self.expr = QLabel("")
        self.expr.setFont(font)
        self.expr.setStyleSheet("color:#8d9bb8;")
        # 状态文字（梦话、房间状态……）可能很长：不许它把窗口撑宽（以前撑宽后缩不回来，右边出现黑边），放不下就省略
        self.expr.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.expr.setMinimumWidth(0)
        self._expr_full = ""
        lay.addWidget(self.dot)
        lay.addWidget(self.name)
        lay.addWidget(self.expr, 1)
        self.btns = {}
        for key, text, tip in (("lab", "≡", "开发测试台"), ("pin", "◆", "置顶"),
                               ("min", "—", "最小化（点任务栏里的图标恢复；双击标题栏是收起 / 展开画面）"),
                               ("close", "×", "关闭")):
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.setFixedSize(22, 20)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton{color:#9fb0d0;background:transparent;border:none;border-radius:4px;font-size:13px;}"
                "QToolButton:hover{background:#2a3550;color:#ffffff;}"
                + ("QToolButton:hover{background:#b0384a;}" if key == "close" else ""))
            lay.addWidget(b)
            self.btns[key] = b
        self.btns["lab"].clicked.connect(win.open_lab_safe)
        self.btns["pin"].clicked.connect(win.toggle_pin)
        self.btns["min"].clicked.connect(win.minimize_to_taskbar)
        self.btns["close"].clicked.connect(win.close)
        self.refresh()

    def refresh(self):
        rt = self.win.rt
        if time.monotonic() < self.hold_until:      # 正在显示一条临时提示（例如评价已记下）
            return
        busy = rt.director.busy or getattr(rt, "_evolving", False)
        mock = rt.director.client_desc.startswith("mock")
        color = "#f0b450" if busy else ("#7a869e" if mock else "#5ad07a")
        self.dot.setStyleSheet(f"color:{color};")
        self.dot.setToolTip("生成中" if busy else ("Mock 模式" if mock else "就绪"))
        idle = getattr(rt, "idle_state", None)
        expr = rt.current_expression() if hasattr(rt, "current_expression") else (rt.engine.expression or "")
        text = f"· {fr.display_name(expr)}" if expr and expr != "neutral" else ""
        sleep = getattr(rt, "sleep", None)
        status = sleep.status_text() if sleep is not None else ""
        room = getattr(rt, "room", None)
        if not status and room is not None:          # 在自己的房间里（虚拟房间）
            status = room.status_text()
        if status:                                   # 睡眠中 / 犯困 / 被吵醒 / 在房间里
            text = f"· {status}"
        elif idle:
            text += " · 挂机中"
        elif getattr(rt, "_idle_pending", None):
            eta = rt.idle_eta() if hasattr(rt, "idle_eta") else None
            if eta is not None and eta <= 10:        # 至少要等 1 分钟：只在最后 10 秒提示，免得一直挂着
                text += " · 即将挂机"
        self.set_status(text)
        self.btns["pin"].setText("◆" if rt.cfg.always_on_top else "◇")

    def set_status(self, text: str) -> None:
        """标题栏右边的状态文字；放不下就用“…”省略，鼠标悬停看全文。"""
        self._expr_full = text or ""
        self._elide_status()

    def _elide_status(self) -> None:
        full = self._expr_full
        width = self.expr.width()
        shown = self.expr.fontMetrics().elidedText(full, Qt.ElideRight, width) if width > 0 else full
        if self.expr.text() != shown:
            self.expr.setText(shown)
        self.expr.setToolTip(full if shown != full else "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide_status()


class AsciiPetWindow(QWidget):
    def __init__(self, rt: Runtime, open_lab=None, on_close=None):
        self.rt = rt
        self.framed = rt.cfg.window_style != "floating"
        super().__init__(None, self._flags())
        self.open_lab = open_lab
        self.on_close = on_close
        self.folded = False
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle(f"{rt.pet_name()} · ASCII")
        self.view = AsciiView(self, cell_h=rt.cfg.cell_h, glow=rt.cfg.glow, plate_mode=rt.cfg.plate_mode,
                              scanlines=rt.cfg.scanlines, pad=6 if self.framed else 8,
                              background=PANEL_ARGB if self.framed else None)
        lay = QVBoxLayout(self)
        if self.framed:
            lay.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)
            lay.setSpacing(0)
            self.title = TitleBar(self)
            lay.addWidget(self.title)
        else:
            lay.setContentsMargins(0, 0, 0, 0)
            self.title = None
        lay.addWidget(self.view)
        # 左下角的“AI 对话”入口：随桌宠运行时由 integration 接上（set_chat_entry），独立运行时不显示
        self.open_chat = None
        self.chat_btn = QToolButton(self.view)
        self.chat_btn.setObjectName("ascii-chat-entry")
        self.chat_btn.setText(">_ 对话")
        self.chat_btn.setToolTip("打开 AI 对话（右键菜单里的“AI 对话”也还在）")
        self.chat_btn.setCursor(Qt.PointingHandCursor)
        self.chat_btn.setFocusPolicy(Qt.NoFocus)
        self.chat_btn.setStyleSheet(CHAT_BTN_QSS)
        self.chat_btn.clicked.connect(self._open_chat)
        self.chat_btn.hide()
        # 右下角的“灵动岛”按钮：内容平时收着，鼠标移上去点击才弹出（integration 用 set_island_bar 接上）
        self.island_bar = None
        self.island_popup = None
        self.island_on_open = None      # integration 设置：弹出时调用（顺便查一次余额）
        self.island_btn = QToolButton(self.view)
        self.island_btn.setObjectName("ascii-chat-entry")       # 与“>_ 对话”同一套样式
        self.island_btn.setText("◈ 灵动岛")
        self.island_btn.setToolTip("灵动岛：余额、峰谷时段、最近消息（点击展开）")
        self.island_btn.setCursor(Qt.PointingHandCursor)
        self.island_btn.setFocusPolicy(Qt.NoFocus)
        self.island_btn.setStyleSheet(CHAT_BTN_QSS)
        self.island_btn.clicked.connect(self.toggle_island_popup)
        self.island_btn.hide()
        self.view.installEventFilter(self)
        self._drag: QPoint | None = None
        self._moved = False
        rt.add_view(self.view)
        rt.engine.tick(0.0)
        c, k, w, h, off = rt.engine.frame()
        self.view.set_frame(c, k, w, h, off)
        self.adjustSize()
        if rt.cfg.window_x >= 0 and rt.cfg.window_y >= 0:
            self.move(rt.cfg.window_x, rt.cfg.window_y)
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            self.move(geo.right() - self.width() - 40, geo.bottom() - self.height() - 20)
        if self.title is not None:
            self._title_timer = QTimer(self)
            self._title_timer.timeout.connect(self.title.refresh)
            self._title_timer.start(400)
        # 任务栏图标 = 她现在的脸（表情或睡眠状态变了才重画，几秒看一次）
        self._icon_key = None
        self._icon_timer = QTimer(self)
        self._icon_timer.timeout.connect(self._refresh_icon)
        self._icon_timer.timeout.connect(self.fit_to_content)       # 保底：窗口比内容大就缩回去（几秒看一次）
        self._icon_timer.start(3000)
        QTimer.singleShot(800, lambda: self._refresh_icon(force=True))

    # ------------------------------------------------------------------ AI 对话入口
    def set_chat_entry(self, callback) -> None:
        """接上（或去掉）左下角的“AI 对话”入口。callback() 打开桌宠的对话窗口。"""
        self.open_chat = callback
        self.chat_btn.setVisible(callback is not None)
        self.chat_btn.adjustSize()
        self._place_chat_btn()
        self.chat_btn.raise_()

    # ------------------------------------------------------------------ 灵动岛按钮
    island_toggle = None    # integration 设置：callable() -> (是否已放进窗口, 点击回调)

    def set_island_bar(self, bar) -> None:
        """接上灵动岛：右下角出现“◈ 灵动岛”按钮，点击弹出 bar（卡片）；bar=None 去掉。只有框式窗口才有。"""
        if self.island_popup is not None:
            self.island_popup.hide()
            self.island_popup.deleteLater()
            self.island_popup = None
        self.island_bar = None
        if bar is not None and self.framed:
            from .island_bar import IslandPopup

            self.island_popup = IslandPopup(bar)
            self.island_bar = bar
            bar.on_flash = self._flash_island_btn
        self.island_btn.setVisible(self.island_bar is not None)
        self.island_btn.adjustSize()
        self._place_chat_btn()
        self.island_btn.raise_()

    def toggle_island_popup(self) -> None:
        popup = self.island_popup
        if popup is None:
            return
        # 弹出层开着时点按钮：Qt 会先因“点了外面”把它关掉，再把点击交给按钮。刚关上的就别再打开（= 再点一下收起）
        if popup.isVisible() or time.monotonic() - popup.closed_at < 0.25:
            popup.hide()
            return
        if self.island_on_open is not None:
            try:
                self.island_on_open()
            except Exception:
                logging.getLogger("pet.ascii").exception("字符画：展开灵动岛时查询余额失败")
        anchor = QRect(self.island_btn.mapToGlobal(QPoint(0, 0)), self.island_btn.size())
        popup.popup_above(anchor, max(300, min(460, self.view.width() - 2 * CHAT_BTN_MARGIN)))

    def _flash_island_btn(self, ms: int = 700) -> None:
        """灵动岛有新事件（余额更新、峰谷切换、新消息）：按钮短暂变粉。"""
        self.island_btn.setStyleSheet(CHAT_BTN_QSS + "QToolButton#ascii-chat-entry{color:#ff8cc0;border-color:#ff8cc0;}")
        QTimer.singleShot(ms, lambda: self.island_btn.setStyleSheet(CHAT_BTN_QSS))

    def _keep_on_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = min(max(self.x(), avail.left() - SHADOW), avail.right() - self.width() + SHADOW)
        y = min(max(self.y(), avail.top() - SHADOW), avail.bottom() - self.height() + SHADOW)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _place_chat_btn(self) -> None:
        self.chat_btn.move(CHAT_BTN_MARGIN, self.view.height() - self.chat_btn.height() - CHAT_BTN_MARGIN)
        self.island_btn.move(self.view.width() - self.island_btn.width() - CHAT_BTN_MARGIN,
                             self.view.height() - self.island_btn.height() - CHAT_BTN_MARGIN)

    def _open_chat(self) -> None:
        if self.open_chat is not None:
            try:
                self.open_chat()
            except Exception:
                logging.getLogger("pet.ascii").exception("字符画：打开 AI 对话失败")

    def eventFilter(self, obj, event):
        if obj is self.view and event.type() == QEvent.Resize:     # 字符大小变化等：入口跟着贴在左下角
            self._place_chat_btn()
        return super().eventFilter(obj, event)

    def _flags(self):
        # 普通顶层窗口（以前是 Qt.Tool，任务栏和 Alt+Tab 里都没有它，收起或被挡住后就找不到了）。
        # 无边框窗口默认没有“最小化框 / 系统菜单”样式，加上之后点任务栏图标才能最小化 / 恢复；不会画出系统边框。
        flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint | Qt.WindowSystemMenuHint
        if self.rt.cfg.always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        return flags

    # ------------------------------------------------------------------ 外观
    def paintEvent(self, _e):
        if not self.framed:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        panel = QRectF(self.rect()).adjusted(SHADOW, SHADOW, -SHADOW, -SHADOW)
        for i in range(SHADOW, 0, -2):      # 柔和阴影
            path = QPainterPath()
            path.addRoundedRect(panel.adjusted(-i, -i + 2, i, i + 2), RADIUS + i, RADIUS + i)
            p.fillPath(path, QColor(0, 0, 0, int(36 * (1 - i / SHADOW)) + 4))
        path = QPainterPath()
        path.addRoundedRect(panel, RADIUS, RADIUS)
        p.fillPath(path, PANEL)
        if self.title is not None:
            bar = QPainterPath()
            bar.addRoundedRect(QRectF(panel.left(), panel.top(), panel.width(), TITLE_H + RADIUS), RADIUS, RADIUS)
            clip = QPainterPath()
            clip.addRect(QRectF(panel.left(), panel.top(), panel.width(), TITLE_H))
            p.fillPath(bar.intersected(clip), TITLE_BG)
            p.setPen(QPen(QColor(36, 46, 70), 1))
            p.drawLine(int(panel.left()), int(panel.top() + TITLE_H), int(panel.right()), int(panel.top() + TITLE_H))
        p.setPen(QPen(BORDER, 1))
        p.drawPath(path)
        p.end()

    def toggle_pin(self):
        self.rt.cfg.always_on_top = not self.rt.cfg.always_on_top
        self.rt.cfg.save()
        pos = self.pos()
        self.setWindowFlags(self._flags())
        self.move(pos)
        self.show()
        if self.title:
            self.title.refresh()

    def minimize_to_taskbar(self):
        self.showMinimized()

    def restore_from_taskbar(self):
        """托盘 / 灵动岛的“显示桌宠”等：最小化了就恢复，然后提到最前。"""
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _refresh_icon(self, force: bool = False) -> None:
        rt = self.rt
        key = (rt.current_expression(), rt.sleep.state)
        if key == self._icon_key and not force:
            return
        icon = self._face_icon()
        if icon is not None:
            self._icon_key = key
            self.setWindowIcon(icon)

    def _face_icon(self):
        """从当前画面裁出脸部，做成圆角方形的任务栏图标。"""
        img, face = self.view._img, self.rt.engine.face_rect()
        if img is None or face is None:
            return None
        cw, ch = self.view.cell_w, self.view._cache.cell_h
        dpr = img.devicePixelRatio()
        x, y, w, h = face
        side = max(w * cw, h * ch) * 0.92
        cx, cy = (x + w / 2) * cw, (y + h * 0.5) * ch
        src = QRectF((cx - side / 2) * dpr, (cy - side / 2) * dpr, side * dpr, side * dpr)
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 64, 64), 12, 12)
        p.setClipPath(path)
        p.fillRect(pm.rect(), PANEL)
        p.drawImage(QRectF(0, 0, 64, 64), img, src)
        p.end()
        return QIcon(pm)

    def fit_to_content(self) -> bool:
        """窗口比内容大（以前被标题栏的长文字撑宽过、显示器缩放变化等）就缩回内容大小，免得右边 / 下边出现黑边。
        返回是否缩了。"""
        if self.folded or not self.isVisible() or self.isMinimized():
            return False
        hint = self.sizeHint()
        w, h = min(self.width(), hint.width()), min(self.height(), hint.height())
        if (w, h) == (self.width(), self.height()):
            return False
        self.resize(w, h)
        return True

    def toggle_fold(self):
        self.folded = not self.folded
        self.view.setVisible(not self.folded)
        self.adjustSize()
        self.resize(self.sizeHint())

    def open_lab_safe(self):
        if self.open_lab:
            self.open_lab()

    # ------------------------------------------------------------------ 鼠标
    def mousePressEvent(self, e):
        self.rt.note_activity()                      # 碰一下窗口：推迟入睡（不会吵醒已经睡着的她）
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)
            self._moved = True

    def mouseReleaseEvent(self, e):
        if self._drag is not None and self._moved:
            self.rt.cfg.window_x, self.rt.cfg.window_y = self.x(), self.y()
            self.rt.cfg.save()
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        if self.title is not None and e.position().y() < SHADOW + TITLE_H:
            self.toggle_fold()
            return
        self.rt.poke(random.choice(POKES))          # 醒着：戳她一下；睡着：把她戳醒

    def wheelEvent(self, e):
        self.rt.note_activity()
        if e.modifiers() & Qt.ControlModifier:
            step = 1 if e.angleDelta().y() > 0 else -1
            self.set_cell(self.rt.cfg.cell_h + step)

    SIZE_CELLS = (5, 6, 7, 8, 10, 12)     # 右键「大小」里给的字符高（字宽 = 字高 × 0.6 取整，所以挑了比例好看的几档）

    def size_for_cell(self, h: int) -> tuple[int, int]:
        """字符高为 h 时，字符画窗口的大小（宽 × 高，含标题栏，不含外面的阴影）。"""
        h = max(5, min(24, int(h)))
        cw = max(3, int(round(h * 0.6)))                 # 与 GlyphCache 的字宽一致
        w = self.rt.engine.W * cw + self.view.pad * 2
        hh = self.rt.engine.H * h + self.view.pad * 2 + (TITLE_H if self.title is not None else 0)
        return w, hh

    def fill_size_menu(self, menu: QMenu, avail_height: int | None = None) -> None:
        """右键「大小」（v1.3.7）：按窗口的实际尺寸列出几档，勾上现在这档；点了直接调字符画窗口。
        改的是字符高，画面始终清晰（不是把图片拉伸）；放不进屏幕的档位不列出。"""
        menu.clear()
        cur = int(self.rt.cfg.cell_h)
        if avail_height is None:
            screen = self.screen() or QApplication.primaryScreen()
            avail_height = screen.availableGeometry().height() if screen is not None else 10 ** 6
        avail = avail_height - 2 * SHADOW
        for h in sorted(set(self.SIZE_CELLS) | {cur}):
            w, hh = self.size_for_cell(h)
            if hh > avail and h != cur:
                continue
            act = QAction(f"{w} × {hh} px", menu, checkable=True)
            act.setChecked(h == cur)
            act.triggered.connect(lambda _checked=False, hv=h: self.set_cell(hv))
            menu.addAction(act)

    def set_cell(self, h: int) -> None:
        h = max(5, min(24, h))
        self.rt.cfg.cell_h = h
        self.view.set_cell_height(h)
        self.rt.engine.tick(0.0)
        c, k, w, hh, off = self.rt.engine.frame()
        self.view.set_frame(c, k, w, hh, off)
        self.view.setMinimumSize(self.view.sizeHint())
        self.view.resize(self.view.sizeHint())
        self.adjustSize()
        self.resize(self.sizeHint())
        self.rt.cfg.save()

    # ------------------------------------------------------------------ 菜单
    # 随桌宠运行时，右键弹出的是原桌宠的完整菜单（integration 接管，定位在这个窗口旁边），
    # 字符画自己的选项插在最前面：👍/👎 + “字符画”子菜单。独立运行（--ascii-pet）时用自己的菜单。
    pet_menu = None       # integration 设置：callable(全局坐标) -> bool，成功弹出原桌宠菜单时返回 True
    pet_toggle = None     # integration 设置：callable() -> (菜单文字, 点击回调)，“显示/隐藏原来的桌宠”

    def contextMenuEvent(self, e):
        self.rt.note_activity()
        if self.pet_menu is not None:
            try:
                if self.pet_menu(e.globalPos()):
                    return
            except Exception:
                logging.getLogger("pet.ascii").exception("字符画：弹出原桌宠菜单失败，改用字符画自己的菜单")
        m = QMenu(self)
        for a in self._rating_actions(m):
            m.addAction(a)
        if m.actions():
            m.addSeparator()
        self.fill_ascii_menu(m)
        self.left_menus(m, anchor=self.frameGeometry())          # 独立运行时顶层菜单也放在窗口左边
        m.exec(QCursor.pos())

    def left_menus(self, menu: QMenu, anchor: QRect | None = None) -> LeftMenus:
        """这个右键菜单（含各级子菜单）都往左展开，不挡住字符画。"""
        return LeftMenus(menu, anchor)

    def _rating_actions(self, parent) -> list:
        if not self.rt.engine.rigged or self.rt.sleep.busy:     # 睡脸不参与评价（不是可进化的表情）
            return []
        cur = fr.display_name(self.rt.current_expression())
        like = QAction(f"👍 喜欢现在这个表情（{cur}）", parent)
        like.triggered.connect(lambda: self._rate(1))
        dislike = QAction(f"👎 不喜欢现在这个表情（{cur}）", parent)
        dislike.triggered.connect(lambda: self._rate(-1))
        return [like, dislike]

    def extend_pet_menu(self, menu: QMenu) -> list:
        """把字符画的选项插到原桌宠菜单最前面：👍 / 👎、切换表情（和“字符画”同一层，少点一层）、“字符画”子菜单。
        返回 [(父菜单, 新建的子菜单)]，供套用原菜单的样式。"""
        first = menu.actions()[0] if menu.actions() else None
        for a in self._rating_actions(menu):
            menu.insertAction(first, a)
        pairs = []
        if self.rt.engine.rigged:
            expr = self._expression_menu(menu)
            menu.insertMenu(first, expr)
            pairs.append((menu, expr))
        sub = QMenu("字符画", menu)
        self.fill_ascii_menu(sub, expressions=False)
        menu.insertMenu(first, sub)
        if first is not None:
            menu.insertSeparator(first)
        pairs.append((menu, sub))
        for a in sub.actions():
            if a.menu() is not None:
                pairs.append((sub, a.menu()))
        return pairs

    def _expression_menu(self, parent: QMenu) -> QMenu:
        """切换表情：全部内置表情 + 全部自创表情（以前自创的只列 30 个），列表可以用滚轮上下滑动；
        现在的表情打着勾，打开时直接滚到它那里。"""
        sub = ScrollMenu("切换表情", parent)
        cur = self.rt.current_expression()
        custom = [n for n in self.rt.custom.names(10_000) if n not in fr.PRESETS]
        for group in (list(fr.PRESETS), custom):
            if group is custom and custom:
                sub.addSeparator()
            for name in group:
                a = QAction(fr.display_name(name), sub, checkable=True)
                a.setChecked(name == cur)                 # ScrollMenu 打开时会把打勾的这一项滚到中间
                a.triggered.connect(lambda _=False, n=name: self.rt.apply_preset(n))
                sub.addAction(a)
        return sub

    def fill_ascii_menu(self, m: QMenu, expressions: bool = True) -> None:
        """字符画自己的选项。expressions=False：切换表情已经放在上一层了（原桌宠菜单里和“字符画”同一层）。"""
        m.addAction("说点什么 / 输入表达意图…", self._ask)
        if self.open_lab:
            m.addAction("打开开发测试台", self.open_lab)
        if self.rt.engine.rigged and expressions:
            m.addMenu(self._expression_menu(m))
        m.addSeparator()
        bg = m.addMenu("背景")
        grp = QActionGroup(bg)
        for mode, label in (("none", "无"), ("log", "思考日志"), ("code", "代码流"), ("rain", "字符雨"), ("noise", "星点")):
            a = QAction(label, bg, checkable=True)
            a.setChecked(self.rt.engine.s.background == mode)
            a.triggered.connect(lambda _=False, md=mode: self._set_bg(md))
            grp.addAction(a)
            bg.addAction(a)
        tex = QAction("字符流纹理", m, checkable=True)
        tex.setChecked(self.rt.engine.s.code_rows)
        tex.triggered.connect(self._toggle_tex)
        m.addAction(tex)
        auto = QAction("自主表达（会调用 API）", m, checkable=True)
        auto.setChecked(self.rt.cfg.autonomous)
        auto.triggered.connect(self._toggle_auto)
        m.addAction(auto)
        style = QAction("透明悬浮样式（重启窗口后生效）", m, checkable=True)
        style.setChecked(self.rt.cfg.window_style == "floating")
        style.triggered.connect(self._toggle_style)
        m.addAction(style)
        size = m.addMenu("字符大小")
        for h in (5, 6, 7, 8, 9, 10, 12):
            size.addAction(f"字符高 {h}px", lambda hh=h: self.set_cell(hh))
        m.addAction("恢复默认表情", self._reset_face)
        if self.framed:
            m.addAction("展开画面" if self.folded else "收起画面（只留标题栏，也可双击标题栏）", self.toggle_fold)
            m.addAction("最小化到任务栏", self.minimize_to_taskbar)
        if self.rt.engine.rigged:
            sl = self.rt.sleep
            if sl.sleeping:
                m.addAction("叫醒她", lambda: sl.wake("poke"))
            elif not sl.busy:
                mins = getattr(self.rt.cfg, "sleep_after_min", 30.0)
                m.addAction("现在睡觉" + (f"（{mins:g} 分钟无互动会自己睡）" if self.rt.cfg.sleep_enabled else ""),
                            lambda: sl.fall_asleep())
        m.addSeparator()
        from . import chat_theme

        if chat_theme._state["installed"]:          # 随桌宠运行时：“AI 对话”窗口的风格（LINE / ASCII / 原样）
            sub = m.addMenu("对话框风格（AI 对话窗口）")
            group = QActionGroup(sub)
            for key, label in chat_theme.SKIN_LABELS:
                act = QAction(label, sub, checkable=True)
                act.setChecked(chat_theme.skin() == key)
                act.triggered.connect(lambda _=False, k=key: self._set_chat_theme(k))
                group.addAction(act)
                sub.addAction(act)
        if self.island_toggle is not None and self.framed:
            on, fn = self.island_toggle()
            isl = QAction("灵动岛收进窗口右下角的按钮", m, checkable=True)
            isl.setChecked(on)
            isl.triggered.connect(lambda checked=False: fn(checked))
            m.addAction(isl)
        if self.pet_toggle is not None:
            label, fn = self.pet_toggle()
            m.addAction(label, fn)
        self._spotify_menu(m)
        m.addAction("关闭字符画窗口", self.close)

    # ------------------------------------------------------------------ Spotify（v1.3）
    def _spotify_menu(self, m: QMenu) -> None:
        mus = getattr(self.rt, "music", None)
        if mus is None:
            return
        sub = m.addMenu("Spotify（让她可以放歌）")
        if mus.available():
            st = sub.addAction("已连接，她可以用你的 Spotify 放歌")
            st.setEnabled(False)
            sub.addAction("断开 Spotify", self._spotify_disconnect)
        else:
            st = sub.addAction("还没连接" if self.rt.cfg.spotify_enabled else "已关闭（spotify_enabled）")
            st.setEnabled(False)
            sub.addAction("连接 Spotify…", self._spotify_connect)

    def _spotify_connect(self) -> None:
        from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

        mus = self.rt.music
        uri = mus.redirect_uri()
        QApplication.clipboard().setText(uri)
        text = ("1. 打开 developer.spotify.com/dashboard，登录后 Create app（名字随便）\n"
                f"2. Redirect URIs 填：{uri}（已复制到剪贴板），API 选 Web API，保存\n"
                "3. 把应用的 Client ID 粘贴在下面（Client ID 不是密码）\n\n"
                "点确定后会打开浏览器，在 Spotify 的页面上点“同意”就连好了（需要 Premium）。")
        cid, ok = QInputDialog.getText(self, "连接 Spotify", text, text=self.rt.cfg.spotify_client_id)
        if not ok or not cid.strip():
            return
        mus.set_client_id(cid)
        try:
            self.rt.cfg.save()
        except OSError:
            pass
        box = QMessageBox(self)
        box.setWindowTitle("连接 Spotify")
        box.setText("已经打开浏览器，请在 Spotify 的页面上点“同意”。\n（这个提示会在完成后自动更新）")
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.show()
        self._spotify_box = box

        def run():
            from .spotify import SpotifyError

            try:
                mus.connect()
                msg = "连好了。之后她觉得合适时可以给你放歌（你在听歌时只会排进队列）。"
            except SpotifyError as exc:
                msg = f"没连上：{exc}"
            except Exception as exc:
                msg = f"没连上：{exc}"
            self._spotify_result = msg

        import threading

        self._spotify_result = None
        threading.Thread(target=run, name="pet-ascii-spotify-auth", daemon=True).start()
        self._spotify_poll()

    def _spotify_poll(self) -> None:
        from PySide6.QtCore import QTimer

        if getattr(self, "_spotify_result", None) is None:
            QTimer.singleShot(500, self._spotify_poll)
            return
        box = getattr(self, "_spotify_box", None)
        if box is not None:
            box.setText(self._spotify_result)
            box.show()

    def _spotify_disconnect(self) -> None:
        self.rt.music.disconnect()

    def _reset_face(self) -> None:
        self.rt.sleep.end_sleep()
        self.rt.engine.reset_face()

    def _set_chat_theme(self, name: str) -> None:
        from . import integration

        integration.set_chat_theme(name)

    def _rate(self, vote: int) -> None:
        name = self.rt.rate_current(vote)
        if name and self.title is not None:
            self.title.set_status(f"· {fr.display_name(name)} · {'👍 记下了' if vote > 0 else '👎 记下了，会改进'}")
            self.title.hold_until = time.monotonic() + 2.5

    def _ask(self):
        text, ok = QInputDialog.getText(self, "表达意图", "让她怎么表现？（例如：高兴地打招呼 / 突然感到困惑 / 变成一团乱码）")
        if ok and text.strip():
            self.rt.express(text.strip(), source="user", priority=8, force_new=True)

    def _set_bg(self, mode):
        self.rt.engine.set_background(mode)
        self.rt.cfg.background = mode
        self.rt.cfg.save()

    def _toggle_tex(self, on):
        self.rt.engine.s.code_rows = on
        self.rt.engine._body_dirty = True
        self.rt.cfg.code_rows = on
        self.rt.cfg.save()

    def _toggle_auto(self, on):
        self.rt.cfg.autonomous = on
        self.rt.cfg.save()

    def _toggle_style(self, on):
        self.rt.cfg.window_style = "floating" if on else "framed"
        self.rt.cfg.save()

    def closeEvent(self, e):
        self.rt.remove_view(self.view)
        if self.on_close:
            self.on_close()
        super().closeEvent(e)
