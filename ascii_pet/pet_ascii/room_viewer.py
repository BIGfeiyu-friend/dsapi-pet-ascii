"""主人的“房间”窗口（v1.0）：一扇单向玻璃。独立进程（exe --ascii-room，桌面快捷方式“DS娘的房间”），和桌宠本体分开。

只读：她的状态、房间里的文件、原始日志（想法 / 动作 / 思考过程）、房间记忆。
唯一会写的：送礼物（文件复制进「礼物」文件夹 + 一行 inbox.jsonl），由桌宠进程里的她下次检查时收下。
桌宠没在运行时也能打开：可以看房间、送礼物，她要等桌宠运行后才会看到。
"""
from __future__ import annotations

import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTextEdit,
                               QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .config import pet_app_config
from .room import GIFT_DIR, READ_EXT, Room, RoomError, room_root
from .room_image import IMG_EXT

MONO = '"Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace'
BODY = '"Microsoft YaHei UI", "Segoe UI", sans-serif'
C = {"bg": "#0b0e14", "panel": "#0f141c", "line": "#2a3344", "line2": "#3b475c", "text": "#e6edf3",
     "text2": "#a7b3c2", "dim": "#7d8aa0", "pink": "#ff8cc0", "cyan": "#5fd4ff", "amber": "#f5b84a",
     "red": "#ff7a7a", "green": "#3ddc97", "violet": "#b9a3ff"}
ICON = {"write": "✎", "append": "✎", "edit": "✎", "draw": "▦", "read": "◉", "look": "◎", "music": "♪", "rename": "⇄", "move": "⇄",
        "mkdir": "▣", "delete": "⌫", "note": "✉", "rest": "～", "sleep": "zZ", "nothing": "·"}
APP_ID = "DSH.PetAscii.Room"
SERVER = "dsh-pet-ascii-room-viewer"


def pet_name() -> str:
    pc = pet_app_config()
    return str((pc.get("character_aliases") or {}).get(pc.get("character") or "") or "DS娘")


def qss() -> str:
    c = C
    return f"""
QWidget#room {{ background: {c['bg']}; color: {c['text']}; font-family: {BODY}; font-size: 13px; }}
QLabel#head {{ color: {c['text']}; font-family: {MONO}; font-size: 13px; }}
QLabel#bars {{ color: {c['text2']}; font-family: {MONO}; font-size: 12px; }}
QLabel#hint {{ color: {c['dim']}; font-family: {MONO}; font-size: 11px; }}
QTreeWidget, QPlainTextEdit, QTextEdit, QTextBrowser {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line']};
    border-radius: 3px; selection-background-color: {c['pink']}; selection-color: {c['bg']}; }}
QTreeWidget {{ font-family: {MONO}; font-size: 12px; outline: none; }}
QTreeWidget::item {{ padding: 2px 0; }}
QTreeWidget::item:hover {{ color: {c['pink']}; }}
QHeaderView::section {{ background: {c['panel']}; color: {c['dim']}; border: none; font-family: {MONO}; }}
QTextEdit#preview {{ font-family: {MONO}; font-size: 12px; }}
QTabWidget::pane {{ border: 1px solid {c['line']}; border-radius: 3px; top: -1px; }}
QTabBar::tab {{ background: {c['panel']}; color: {c['text2']}; border: 1px solid {c['line']}; padding: 4px 12px;
    font-family: {MONO}; font-size: 12px; margin-right: 2px; }}
QTabBar::tab:selected {{ color: {c['bg']}; background: {c['pink']}; border-color: {c['pink']}; }}
QPushButton {{ min-height: 28px; padding: 0 12px; border: 1px solid {c['line2']}; border-radius: 3px; color: {c['text2']};
    background: {c['panel']}; font-family: {MONO}; font-size: 12px; }}
QPushButton:hover {{ border-color: {c['pink']}; color: {c['pink']}; }}
QCheckBox {{ color: {c['text2']}; font-family: {MONO}; font-size: 12px; spacing: 6px; }}
QCheckBox:hover {{ color: {c['pink']}; }}
QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {c['line2']}; border-radius: 2px; background: {c['panel']}; }}
QCheckBox::indicator:hover {{ border-color: {c['pink']}; }}
QCheckBox::indicator:checked {{ background: {c['pink']}; border-color: {c['pink']}; }}
QSplitter::handle {{ background: {c['bg']}; }}
QScrollBar:vertical {{ background: {c['panel']}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {c['line2']}; min-height: 24px; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QDialog {{ background: {c['bg']}; color: {c['text']}; }}
QLineEdit {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line2']}; padding: 4px; }}
"""


def make_icon(size: int = 256) -> QImage:
    """房间的图标：深色底上一个字符画小屋（和桌宠本体的图标区分开）。"""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QColor(C["bg"]))
    p.setPen(QColor(C["pink"]))
    r = size * 0.08
    p.drawRoundedRect(int(size * 0.04), int(size * 0.04), int(size * 0.92), int(size * 0.92), r, r)
    # 不用字体（打包环境、没有字体时也一样）：用线条画一个“字符画”风格的小屋，屋顶 / \、墙 | |、窗 []、门
    from PySide6.QtCore import QLineF, QRectF
    from PySide6.QtGui import QPen

    u = size / 16.0
    pen = QPen(QColor(C["pink"]), max(2.0, u * 0.9))
    pen.setCapStyle(Qt.SquareCap)
    p.setPen(pen)
    p.drawLine(QLineF(3 * u, 7.5 * u, 8 * u, 2.8 * u))          # /
    p.drawLine(QLineF(8 * u, 2.8 * u, 13 * u, 7.5 * u))         # \
    pen.setColor(QColor(C["cyan"]))
    p.setPen(pen)
    p.drawLine(QLineF(4.2 * u, 7.2 * u, 4.2 * u, 13 * u))       # |
    p.drawLine(QLineF(11.8 * u, 7.2 * u, 11.8 * u, 13 * u))     # |
    p.drawLine(QLineF(3.2 * u, 13 * u, 12.8 * u, 13 * u))       # _
    p.setBrush(Qt.NoBrush)
    p.drawRect(QRectF(5.6 * u, 8.4 * u, 2.4 * u, 2.0 * u))      # [] 窗
    p.setBrush(QColor(C["amber"]))
    p.setPen(Qt.NoPen)
    p.drawRect(QRectF(9.0 * u, 9.6 * u, 1.6 * u, 3.4 * u))      # 门（亮着灯）
    p.end()
    return img


class GiftNoteDialog(QDialog):
    def __init__(self, parent, name: str):
        super().__init__(parent)
        self.setWindowTitle(f"写张便条给{name}")
        self.resize(420, 300)
        lay = QVBoxLayout(self)
        tip = QLabel(f"便条会放进她的「{GIFT_DIR}」文件夹。她下次进房间时会看到（会不会看、怎么回应，由她自己决定）。")
        tip.setWordWrap(True)
        tip.setObjectName("hint")
        self.title = QLineEdit()
        self.title.setPlaceholderText("标题（可以不写）")
        self.body = QPlainTextEdit()
        lay.addWidget(tip)
        lay.addWidget(self.title)
        lay.addWidget(self.body, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("放进礼物文件夹")
        bb.button(QDialogButtonBox.Cancel).setText("算了")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class RoomViewer(QWidget):
    def __init__(self, root: Path | None = None, poll_ms: int = 2000):
        super().__init__()
        self.setObjectName("room")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.room = Room(root or room_root())
        self.name = pet_name()
        self.setWindowTitle(f"{self.name}的房间")
        self.setWindowIcon(QIcon(QPixmap.fromImage(make_icon(64))))
        self.setStyleSheet(qss())
        self.setAcceptDrops(True)
        self.resize(980, 720)
        self.log_offset = 0
        self.events: list = []
        self.by_id: dict = {}
        self._detail = None                    # 预览区正在显示的那条日志（切换“显示思考过程”时重画）
        self._sig = None
        self._mtimes: dict = {}
        self._build()
        self._load_initial_log()
        self.refresh(force=True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(poll_ms)

    # ------------------------------------------------------------------ 界面
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)
        self.head = QLabel()
        self.head.setObjectName("head")
        self.head.setTextFormat(Qt.RichText)
        self.bars = QLabel()
        self.bars.setObjectName("bars")
        self.bars.setTextFormat(Qt.RichText)
        lay.addWidget(self.head)
        lay.addWidget(self.bars)
        split = QSplitter(Qt.Vertical)
        top = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["房间", "大小"])
        self.tree.setColumnWidth(0, 230)
        self.tree.itemClicked.connect(self._on_tree)
        self.preview = QTextEdit()             # 文字，也能放图片（预览礼物里的图）
        self.preview.setObjectName("preview")
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("点左边的文件看内容；点时间线里的 [详情] 看她当时的想法"
                                        "（思考过程默认收起，勾选下面的「显示思考过程」才显示）")
        top.addWidget(self.tree)
        top.addWidget(self.preview)
        top.setSizes([300, 660])
        self.tabs = QTabWidget()
        self.timeline = self._browser()
        self.notes = self._browser()
        self.memview = self._browser()
        self.tabs.addTab(self.timeline, "时间线")
        self.tabs.addTab(self.notes, "她的便条")
        self.tabs.addTab(self.memview, "房间记忆")
        split.addWidget(top)
        split.addWidget(self.tabs)
        split.setSizes([330, 330])
        lay.addWidget(split, 1)
        row = QHBoxLayout()
        b1 = QPushButton("◆ 送礼物（文件）…")
        b1.clicked.connect(self.gift_files)
        b2 = QPushButton("✉ 写张便条给她")
        b2.clicked.connect(self.gift_note)
        b3 = QPushButton("▣ 打开房间文件夹")
        b3.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.room.space))))
        for b in (b1, b2, b3):
            row.addWidget(b)
        row.addStretch(1)
        self.show_reasoning = QCheckBox("显示思考过程")      # 默认收起：模型的思考很长，也不是她说出来的话
        self.show_reasoning.setToolTip("[详情] 里是否显示模型的思考过程（每次打开窗口都默认收起）")
        self.show_reasoning.toggled.connect(self._redraw_detail)
        row.addWidget(self.show_reasoning)
        hint = QLabel("单向玻璃：她知道你偶尔会看，但你在不在看不会打扰她。也可以把文件直接拖进这个窗口送给她。")
        hint.setObjectName("hint")
        lay.addLayout(row)
        lay.addWidget(hint)

    def _browser(self) -> QTextBrowser:
        b = QTextBrowser()
        b.setOpenLinks(False)
        b.anchorClicked.connect(self._on_anchor)
        return b

    # ------------------------------------------------------------------ 读取
    def _read_json(self, name: str) -> dict:
        try:
            return json.loads((self.room.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _load_initial_log(self) -> None:
        self.events = self.room.tail_log(400)
        try:
            self.log_offset = self.room.log_path.stat().st_size
        except OSError:
            self.log_offset = 0
        self.by_id = {e.get("id"): e for e in self.events}
        self._render_timeline()

    def refresh(self, force: bool = False) -> None:
        new, off = self.room.read_log(self.log_offset)
        if off < self.log_offset:                          # 日志被换掉了：重新读
            self._load_initial_log()
        elif new:
            self.log_offset = off
            self.events = (self.events + new)[-600:]
            for e in new:
                self.by_id[e.get("id")] = e
            self._render_timeline()
        self._render_state()
        sig = self.room.signature()
        if force or sig != self._sig:
            self._sig = sig
            self._render_tree()
        mt = _mtime(self.room.root / "room_memory.json")
        if force or mt != self._mtimes.get("mem"):
            self._mtimes["mem"] = mt
            self._render_memory()

    # ------------------------------------------------------------------ 渲染
    def _render_state(self) -> None:
        st = self._read_json("room_state.json")
        now = time.time()
        alive = now - float(st.get("heartbeat") or 0) < 180
        c = C
        if not alive:
            where = f"<span style='color:{c['dim']}'>× 桌宠没在运行：{self.name}现在不会进房间（礼物可以先放着，她以后会看到）</span>"
        elif st.get("inside"):
            where = f"<span style='color:{c['pink']}'>● 在房间里</span>" + \
                (f"<span style='color:{c['text2']}'>：{_e(st.get('status', ''))}</span>" if st.get("status") else "")
        elif st.get("pet_state") in ("asleep", "falling"):
            where = f"<span style='color:{c['violet']}'>◌ 睡着了</span>"
        else:
            where = f"<span style='color:{c['green']}'>○ 在外面</span>"
        nw = float(st.get("next_wake") or 0)
        nxt = ""
        if alive and not st.get("inside") and nw:
            choice = st.get("next_choice")
            who = ("她说睡到天亮再来" if choice == "morning" else f"她自己说 {float(choice):g} 分钟后再来") \
                if st.get("next_by") == "her" and choice else "她没说，按精力定的"
            nxt = f"　下次大概 {datetime.fromtimestamp(nw):%H:%M} 会想进来（{who}；主人在聊天、精力不够会推迟）" \
                if nw > now else "　随时可能进来"
        self.head.setText(f"<b style='color:{c['pink']}'>┌─[ {self.name}的房间 ]</b>　{where}"
                          f"<span style='color:{c['dim']}'>{nxt}</span>")
        if not st:
            self.bars.setText(f"<span style='color:{c['dim']}'>（她还没来过）</span>")
            return
        longing = _longing(st, now)
        mem = self._read_json("room_memory.json")
        self.bars.setText(
            f"精力 {_bar(st.get('energy', 0), c['green'])}　无聊 {_bar(st.get('boredom', 0), c['amber'])}　"
            f"想念你 {_bar(longing, c['pink'])}　"
            f"<span style='color:{c['dim']}'>今天进来 {st.get('wakes_today', 0)} 次 · {int(st.get('tokens_today', 0)):,} token（不含缓存命中）"
            f" · 记忆 短期 {len(mem.get('short', []))} / 印象 {len(mem.get('gists', []))} / 长期 {len(mem.get('long', []))}</span>")

    def _render_tree(self) -> None:
        sel = self.tree.currentItem().data(0, Qt.UserRole) if self.tree.currentItem() else None
        self.tree.clear()
        nodes = {"": self.tree.invisibleRootItem()}
        for it in self.room.tree():
            parent_path = it["path"].rsplit("/", 1)[0] if "/" in it["path"] else ""
            parent = nodes.get(parent_path, self.tree.invisibleRootItem())
            name = it["path"].rsplit("/", 1)[-1]
            node = QTreeWidgetItem(parent, [name + ("/" if it["type"] == "dir" else ""),
                                            "" if it["type"] == "dir" else _size(it.get("size", 0))])
            node.setData(0, Qt.UserRole, it["path"])
            if it["type"] == "dir":
                nodes[it["path"]] = node
                node.setForeground(0, QColor(C["cyan"] if it["path"] == GIFT_DIR else C["amber"]))
            elif it.get("gift"):
                node.setForeground(0, QColor(C["cyan"]))
            if it["path"] == sel:
                self.tree.setCurrentItem(node)
        self.tree.expandAll()

    def _render_timeline(self) -> None:
        c = C
        rows = []
        for e in self.events[-400:]:
            t = _t(e.get("ts"))
            typ = e.get("type")
            if typ == "enter":
                rows.append(f"<div style='color:{c['dim']};margin-top:8px'>──── {t} 进房间（{_e(e.get('how', ''))}）"
                            f"精力 {e.get('energy', 0):.2f} · 无聊 {e.get('boredom', 0):.2f} · 想念 {e.get('longing', 0):.2f} ────</div>")
            elif typ == "step":
                icon = ICON.get(e.get("action"), "·")
                feel = f"（{_e(e.get('feeling', ''))}）" if e.get("feeling") else ""
                rows.append(f"<div><span style='color:{c['dim']}'>{t}</span> <span style='color:{c['pink']}'>◇ "
                            f"{_e(e.get('thought', ''))}</span><span style='color:{c['dim']}'>{feel}</span></div>")
                ok = e.get("ok", True)
                what = _e(e.get("what", ""))
                err = f" <span style='color:{c['red']}'>✗ 没成功：{_e(e.get('error', ''))}</span>" if not ok else ""
                echo = f" <span style='color:{c['violet']}'>↺ 似曾相识</span>" if e.get("echo") else ""
                rtok = e.get("reasoning_tokens", 0)
                rows.append(f"<div style='margin-left:46px'><span style='color:{c['text']}'>{icon} {what}</span>{err}{echo}"
                            f" <a href='ev:{e.get('id')}' style='color:{c['cyan']}'>[详情]</a>"
                            f"<span style='color:{c['dim']}'> 思考 {rtok} tok</span></div>")
            elif typ == "leave":
                why = _e(e.get("why", ""))
                rows.append(f"<div style='color:{c['dim']}'>──── {t} 出来了（{why}，{e.get('steps', 0)} 步，"
                            f"{int(e.get('tokens', 0)):,} token）────</div>")
            elif typ == "gift":
                via = "（直接放进了文件夹）" if e.get("via") == "folder" else ""
                rows.append(f"<div style='color:{c['cyan']}'>{t} ◆ 你送了「{_e(e.get('name', ''))}」{via}"
                            + (f"：{_e(e.get('note', ''))}" if e.get("note") else "") + "</div>")
            elif typ == "change":
                ch = "、".join(f"「{_e(x['path'])}」{x['change']}" for x in e.get("changes", [])[:6])
                rows.append(f"<div style='color:{c['amber']}'>{t} △ 房间里有变化（不是她做的）：{ch}</div>")
            elif typ == "music":                       # 聊天时放的歌（房间里放的记在那一步）
                if e.get("ok"):
                    how = "加进了你的播放队列" if e.get("mode") == "queued" else "直接播放"
                    rows.append(f"<div style='color:{c['green']}'>{t} ♪ 聊天时给你放了《{_e(e.get('name', ''))}》"
                                f"- {_e(e.get('artists', ''))}（{how}）</div>")
                else:
                    rows.append(f"<div style='color:{c['dim']}'>{t} ♪ 聊天时想放「{_e(e.get('query', ''))}」，没放成："
                                f"{_e(e.get('error', ''))}</div>")
            elif typ == "saw":
                who = "她觉得这张图是" if e.get("hers", True) else "她没说，记下的是副模型的话"
                rows.append(f"<div style='margin-left:46px;color:{c['violet']}'>◎ {who}：{_e(e.get('text', ''))}</div>")
            elif typ == "memory":
                if e.get("error"):
                    rows.append(f"<div style='color:{c['dim']}'>{t} ☾ 整理记忆没成功：{_e(e['error'])}</div>")
                else:
                    longs = "；".join(_e(x) for x in e.get("long", [])[:3])
                    rows.append(f"<div style='color:{c['violet']}'>{t} ☾ 睡着时整理了记忆：长期 +{len(e.get('long', []))}"
                                f"{'（' + longs + '）' if longs else ''}、印象 +{e.get('gist', 0)}、忘掉 {e.get('forgot', 0)}、"
                                f"加强 {e.get('merged', 0)}</div>")
        if not rows:
            rows.append(f"<div style='color:{c['dim']}'>还没有发生任何事。她会在桌宠运行时自己决定什么时候进来。</div>")
        bar = self.timeline.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.timeline.setHtml(f"<div style='font-family:{BODY};font-size:13px;color:{c['text']}'>" + "".join(rows) + "</div>")
        if at_end:
            bar.setValue(bar.maximum())
        notes = [e for e in self.events if e.get("type") == "step" and e.get("action") == "note" and e.get("ok", True)]
        self.notes.setHtml("".join(
            f"<div style='margin:6px 0'><span style='color:{c['dim']}'>{_t(e.get('ts'), True)}</span><br>"
            f"<span style='color:{c['pink']};font-size:14px'>✉ {_e((e.get('result') or {}).get('text') or e.get('args', {}).get('text', ''))}</span></div>"
            for e in reversed(notes)) or f"<div style='color:{c['dim']}'>她还没有给你留过便条。</div>")
        self.tabs.setTabText(1, f"她的便条（{len(notes)}）" if notes else "她的便条")

    def _render_memory(self) -> None:
        c = C
        mem = self._read_json("room_memory.json")
        parts = [f"<div style='color:{c['dim']}'>这是她在房间里的记忆，和桌宠本体的记忆完全分开，也不会影响她的性格。"
                 f"睡着时会整理：重要的留进长期，其余留个印象几天后淡忘，琐碎的忘掉（原始日志里永远都在）。</div>"]
        longs = mem.get("long", [])
        parts.append(f"<h4 style='color:{c['pink']}'>长期（{len(longs)}）</h4>")
        for m in reversed(longs):
            ev = " ".join(f"<a href='ev:{x}' style='color:{c['cyan']}'>{x}</a>" for x in m.get("evidence", [])[:6])
            parts.append(f"<div>· <span style='color:{c['dim']}'>{_t(m.get('ts'), True)}</span> {_e(m.get('text', ''))}"
                         f" <span style='color:{c['dim']}'>（分 {m.get('weight', 0):.2f}，想起 {m.get('recalls', 0)} 次，证据 {ev}）</span></div>")
        gists = mem.get("gists", [])
        parts.append(f"<h4 style='color:{c['amber']}'>印象（{len(gists)}，几天后淡忘）</h4>")
        for g in reversed(gists):
            parts.append(f"<div>· <span style='color:{c['dim']}'>{_t(g.get('ts'), True)}</span> {_e(g.get('text', ''))}"
                         f" <span style='color:{c['dim']}'>（到 {_t(g.get('until'), True)}）</span></div>")
        short = mem.get("short", [])
        parts.append(f"<h4 style='color:{c['text2']}'>短期（{len(short)}，还没整理）</h4>")
        for s in reversed(short[-30:]):
            parts.append(f"<div>· <span style='color:{c['dim']}'>{_t(s.get('ts'), True)}</span> {_e(s.get('what', ''))}"
                         f" <a href='ev:{s.get('id')}' style='color:{c['cyan']}'>[详情]</a></div>")
        parts.append(f"<div style='color:{c['dim']};margin-top:8px'>已经忘掉的：{int(mem.get('forgotten', 0))} 件</div>")
        self.memview.setHtml(f"<div style='font-family:{BODY};font-size:13px;color:{c['text']}'>" + "".join(parts) + "</div>")

    # ------------------------------------------------------------------ 交互
    def _on_tree(self, item, _col) -> None:
        rel = item.data(0, Qt.UserRole)
        if not rel:
            return
        self._detail = None
        p = self.room.space / rel
        if p.is_dir():
            n = sum(1 for _ in p.iterdir())
            self.preview.setPlainText(f"{rel}/\n\n（文件夹，{n} 项）")
            return
        if p.suffix.lower() in IMG_EXT:
            self._show_image(p, rel)
            return
        if p.suffix.lower() not in READ_EXT:
            self.preview.setPlainText(f"{rel}\n\n（{_size(p.stat().st_size)}，不是文字文件，她只能看到名字和大小）")
            return
        try:
            r = self.room.read(rel, max_chars=60000)
            more = f"\n\n……（后面还有 {r['more']} 字）" if r["more"] else ""
            self.preview.setPlainText(r["text"] + more)
        except (RoomError, OSError) as exc:
            self.preview.setPlainText(f"打不开：{exc}")

    def _show_image(self, p: Path, rel: str) -> None:
        """预览图片：上面是原图（缩到预览框宽度），下面是她“看到”的样子（看过的话）。"""
        from PySide6.QtGui import QTextCursor, QTextImageFormat

        pix = QPixmap(str(p))
        self.preview.clear()
        if pix.isNull():
            self.preview.setPlainText(f"{rel}\n\n（打不开这张图片）")
            return
        w = max(120, self.preview.viewport().width() - 24)
        pix = pix.scaledToWidth(min(w, pix.width()), Qt.SmoothTransformation)
        doc = self.preview.document()
        doc.addResource(3, QUrl("img://preview"), pix.toImage())          # 3 = QTextDocument.ImageResource
        cur = self.preview.textCursor()
        cur.insertText(f"{rel}\n")
        fmt = QTextImageFormat()
        fmt.setName("img://preview")
        cur.insertImage(fmt)
        seen = _seen_entry(self.room.root, p)
        if seen:
            cur.insertText("\n\n" + (f"她觉得是：{seen['saw']}\n\n" if seen.get("saw") else "") +
                           "她“看到”的样子：\n" + _render_seen(seen))
        else:
            cur.insertText("\n\n（她还没看过这张图）")
        self.preview.moveCursor(QTextCursor.Start)

    def _on_anchor(self, url: QUrl) -> None:
        s = url.toString()
        if not s.startswith("ev:"):
            return
        e = self.by_id.get(s[3:])
        if e is None:
            e = next((x for x in self.room.tail_log(2000) if x.get("id") == s[3:]), None)
        if e is None:
            self._detail = None
            self.preview.setPlainText(f"{s[3:]}：日志里找不到（太早了，界面只读最近的部分；完整的在 room_log.jsonl）")
            return
        self._detail = e
        self._redraw_detail()

    def _redraw_detail(self, *_args) -> None:
        if self._detail is not None:
            self.preview.setPlainText(describe_event(self._detail, self.show_reasoning.isChecked()))

    def gift_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, f"送给{self.name}的礼物", str(Path.home()))
        self._gift(files)

    def gift_note(self) -> None:
        d = GiftNoteDialog(self, self.name)
        d.setStyleSheet(qss())
        if d.exec() != QDialog.Accepted:
            return
        try:
            ev = self.room.add_gift_text(d.body.toPlainText(), d.title.text())
            self._gifted([ev["path"]])
        except (RoomError, OSError) as exc:
            QMessageBox.warning(self, "没送出去", str(exc))

    def _gift(self, files: list) -> None:
        done, errors = [], []
        for f in files:
            try:
                done.append(self.room.add_gift(f)["path"])
            except (RoomError, OSError) as exc:
                errors.append(f"{Path(f).name}：{exc}")
        if done:
            self._gifted(done)
        if errors:
            QMessageBox.warning(self, "有的没送出去", "\n".join(errors))

    def _gifted(self, paths: list) -> None:
        st = self._read_json("room_state.json")
        alive = time.time() - float(st.get("heartbeat") or 0) < 180
        tail = "她通常几分钟内会进来看看（你在和她聊天时会等聊完）。" if alive else "桌宠现在没在运行，等它运行后她会看到。"
        QMessageBox.information(self, "放进礼物文件夹了", "、".join(p.split("/", 1)[-1] for p in paths) + "\n\n" + tail)
        self.refresh(force=True)

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        files = [f for f in files if os.path.isfile(f)]
        if files:
            self._gift(files)


def describe_event(e: dict, show_reasoning: bool = False) -> str:
    """一条日志的详情。show_reasoning：是否附上模型的思考过程（默认收起，只说有多少）。"""
    lines = [f"{e.get('id')} · {_t(e.get('ts'), True)} · {e.get('type')}"]
    if e.get("type") == "step":
        what = e.get("what", "")
        if e.get("action") == "note" and e.get("ok", True):      # 便条：日志里显示全文（“结果”摘要只留了前 60 字）
            full = " ".join(str((e.get("result") or {}).get("text") or (e.get("args") or {}).get("text") or "").split())
            if full:
                what = f"{what.split('：', 1)[0] or '给主人留了便条'}：{full}"
        lines += [f"想法：{e.get('thought', '')}", f"心情：{e.get('feeling', '')}（{e.get('intensity', 0)}）  表情：{e.get('face', '')}",
                  f"动作：{e.get('action')}  {'成功' if e.get('ok', True) else '没成功：' + e.get('error', '')}",
                  f"结果：{what}", f"接下来：{'还想待着' if e.get('then') == 'stay' else '离开'}"
                  + ("　（她说想记住这件事）" if e.get("remember") else ""),
                  f"无聊 {e.get('boredom_delta', 0):+.3f} → {e.get('boredom', 0):.2f}　精力 → {e.get('energy', 0):.2f}"
                  f"　token {e.get('tokens', 0)}（思考 {e.get('reasoning_tokens', 0)}）"]
        if e.get("echo"):
            lines.append(f"似曾相识：和 {e['echo'].get('id')} 很像（动作相似 {e['echo'].get('sim')}，当时的情况相似 "
                         f"{e['echo'].get('situation_sim')}）")
        args = e.get("args") or {}
        body = args.get("content") or args.get("art") or args.get("text") or ""
        if args.get("old") or args.get("new"):
            body = f"原文：{args.get('old', '')}\n改成：{args.get('new', '')}"
        if body:
            lines += ["", "——内容——", str(body)]
        if e.get("look_text"):
            lines += ["", "——她“看到”的样子（图片转成字符画、颜色和文字）——", e["look_text"]]
        if e.get("reasoning"):
            if show_reasoning:
                lines += ["", "——思考过程（模型的思考，不是她说出来的话）——", e["reasoning"]]
            else:
                lines += ["", f"——思考过程已收起（{e.get('reasoning_tokens', 0)} tok，勾选下面的「显示思考过程」查看）——"]
    else:
        lines.append(json.dumps({k: v for k, v in e.items() if k not in ("id", "ts", "type")}, ensure_ascii=False, indent=1))
    return "\n".join(lines)


# ---------------------------------------------------------------------- 小工具
def _seen_entry(root: Path, p: Path) -> dict | None:
    try:
        from .room_image import file_key

        data = json.loads((root / "seen.json").read_text(encoding="utf-8"))
        return data.get(file_key(p))
    except (OSError, ValueError, AttributeError):
        return None


def _render_seen(entry: dict) -> str:
    try:
        from .room_image import render

        return render(entry["desc"]) if entry.get("desc") else ""
    except Exception:
        return ""


def _e(s) -> str:
    return html.escape(str(s or ""))


def _t(ts, full: bool = False) -> str:
    try:
        d = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ""
    return d.strftime("%m-%d %H:%M" if full or d.date() != datetime.now().date() else "%H:%M")


def _bar(v, color: str, n: int = 10) -> str:
    try:
        v = max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        v = 0.0
    k = int(round(v * n))
    return f"[<span style='color:{color}'>{'█' * k}</span><span style='color:{C['line2']}'>{'░' * (n - k)}</span>] {v:.2f}"


def _longing(st: dict, now: float) -> float:
    h = max(0.0, now - float(st.get("last_user_chat") or now)) / 3600
    return 1 - 0.5 ** (h / 8)


def _size(n: int) -> str:
    return f"{n}B" if n < 1024 else (f"{n / 1024:.1f}KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f}MB")


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def main(argv=None) -> int:
    argv = list(argv or sys.argv)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)   # 任务栏上和桌宠分开
        except Exception:
            pass
    app = QApplication.instance() or QApplication(argv)
    if "--write-icon" in argv:
        i = argv.index("--write-icon")
        return 0 if make_icon(256).save(argv[i + 1], "ICO") else 1
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(SERVER)
    if sock.waitForConnected(300):                     # 已经开着一个：把它叫到前面就好
        sock.write(b"raise")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    win = RoomViewer()
    server = QLocalServer()
    QLocalServer.removeServer(SERVER)
    server.listen(SERVER)

    def on_conn():
        c = server.nextPendingConnection()
        if c is not None:
            c.readyRead.connect(c.readAll)
        win.showNormal()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(on_conn)
    win.show()
    selftest = os.environ.get("PET_ASCII_ROOM_VIEWER_SELFTEST")
    if selftest:                                   # 自检：截图后退出
        def snap():
            win.grab().save(os.path.join(selftest, "room_viewer.png"))
            app.quit()

        QTimer.singleShot(int(os.environ.get("PET_ASCII_SELFTEST_MS", "2500")), snap)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
