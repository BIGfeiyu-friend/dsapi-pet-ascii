"""虚拟房间（v1.0）：主人的“房间”窗口（独立进程，单向玻璃：只读 + 送礼物）。"""
import json
import time

import pytest
from conftest import dispose

from pet_ascii.room import GIFT_DIR, Room


@pytest.fixture
def viewer(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    room = Room(tmp_path / "room")
    room.write("日记.md", "今天很安静。")
    room.append_log({"type": "enter", "how": "醒着", "energy": 0.7, "boredom": 0.2, "longing": 0.4})
    room.append_log({"type": "step", "thought": "想写点东西<b>", "feeling": "平静", "action": "write",
                     "args": {"path": "日记.md", "content": "今天很安静。"}, "ok": True, "what": "写了「日记.md」",
                     "reasoning": "先想想写什么", "reasoning_tokens": 321, "echo": {"id": "r1", "sim": 0.8,
                                                                                 "situation_sim": 0.1}})
    room.append_log({"type": "step", "thought": "留个便条", "action": "note", "args": {"text": "晚安"}, "ok": True,
                     "what": "给主人留了便条：晚安", "result": {"text": "晚安"}})
    (room.root / "room_state.json").write_text(json.dumps({"energy": 0.6, "boredom": 0.3, "heartbeat": time.time(),
                                                           "inside": True, "status": "写了「日记.md」",
                                                           "last_user_chat": time.time() - 7200}), encoding="utf-8")
    from pet_ascii.room_viewer import RoomViewer

    v = RoomViewer(room.root, poll_ms=60000)
    yield v
    dispose(v)


def test_viewer_shows_state_timeline_notes_and_tree(viewer):
    assert "在房间里" in viewer.head.text() and "精力" in viewer.bars.text()
    tl = viewer.timeline.toPlainText()
    assert "想写点东西<b>" in tl and "似曾相识" in tl and "思考 321 tok" in tl     # HTML 被转义，不会被当成标签
    assert "晚安" in viewer.notes.toPlainText() and "（1）" in viewer.tabs.tabText(1)
    names = [viewer.tree.topLevelItem(i).text(0) for i in range(viewer.tree.topLevelItemCount())]
    assert GIFT_DIR + "/" in names and "日记.md" in names


def test_viewer_details_hide_reasoning_until_asked(viewer):
    from PySide6.QtCore import QUrl

    assert not viewer.show_reasoning.isChecked()                           # 思考过程默认收起
    viewer._on_anchor(QUrl("ev:r2"))
    text = viewer.preview.toPlainText()
    assert "今天很安静。" in text and "先想想写什么" not in text and "已收起（321 tok" in text
    viewer.show_reasoning.setChecked(True)                                 # 勾上：正在看的详情立即刷新
    assert "先想想写什么" in viewer.preview.toPlainText()
    viewer._on_anchor(QUrl("ev:r3"))                                       # 之后点开别的也显示（这条没有思考过程）
    assert "已收起" not in viewer.preview.toPlainText()
    viewer.show_reasoning.setChecked(False)
    viewer._on_anchor(QUrl("ev:r2"))
    assert "先想想写什么" not in viewer.preview.toPlainText()


def test_note_details_show_the_full_text():
    from pet_ascii.room_viewer import describe_event

    full = "主人～" + "谢谢你夸我画的表情，" * 8 + "下次见面给你看更厉害的！"
    e = {"id": "r9", "type": "step", "action": "note", "ok": True, "what": "给主人留了便条：" + full[:60],
         "result": {"text": full}}
    assert f"结果：给主人留了便条：{full}" in describe_event(e)                # 不再只显示前 60 字


def test_viewer_gifts_are_the_only_thing_it_writes(viewer, tmp_path):
    root = viewer.room.root
    before = {p.name: p.stat().st_mtime for p in root.iterdir() if p.is_file()}
    src = tmp_path / "照片.txt"
    src.write_text("海边", encoding="utf-8")
    viewer._gift([str(src)])
    viewer.room.add_gift_text("早点睡", "便条")
    events, _ = viewer.room.read_inbox(0)
    assert [e["path"].split("/")[0] for e in events] == [GIFT_DIR, GIFT_DIR]
    after = {p.name: p.stat().st_mtime for p in root.iterdir() if p.is_file()}
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == {"inbox.jsonl"}                                    # 状态、日志、记忆都没动


def test_offline_pet_is_shown(viewer):
    st = viewer.room.root / "room_state.json"
    st.write_text(json.dumps({"heartbeat": 0}), encoding="utf-8")
    viewer.refresh()
    assert "没在运行" in viewer.head.text()


def test_icon_renders():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from pet_ascii.room_viewer import make_icon

    img = make_icon(64)
    assert not img.isNull() and img.width() == 64
