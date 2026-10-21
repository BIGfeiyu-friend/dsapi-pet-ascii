"""虚拟房间（v1.0）：房间本身（沙盒路径、文件操作、回收站、日志、礼物、快照）。"""
import json

import pytest

from pet_ascii.room import GIFT_DIR, MAX_WRITE_CHARS, Room, RoomError, similarity


@pytest.fixture
def room(tmp_path):
    return Room(tmp_path / "room")


@pytest.mark.parametrize("bad", ["../x.txt", "a/../../x.txt", "C:/Windows/x.txt", "~/x.txt", ".secret.txt",
                                 "con.txt", "a/b/c/d/e.txt", "名" * 41 + ".txt", "a<b.txt", "a/./b.txt", "x. "])
def test_paths_outside_or_invalid_are_rejected(room, bad):
    with pytest.raises(RoomError):
        room.write(bad, "hi")


def test_backslashes_and_leading_slash_stay_inside(room):
    r = room.write("\\画\\猫.txt", "=^.^=")
    assert r["path"] == "画/猫.txt" and (room.space / "画" / "猫.txt").read_text(encoding="utf-8") == "=^.^="


def test_write_append_edit_keep_old_versions_in_trash(room):
    assert room.write("日记", "第一天")["path"] == "日记.txt"            # 没写扩展名：存成 .txt
    with pytest.raises(RoomError, match="已经存在"):
        room.write("日记.txt", "覆盖？")
    room.append("日记.txt", "第二天")
    assert room.read("日记.txt")["text"] == "第一天\n第二天"
    room.edit("日记.txt", "第二天", "第二天，下雨")
    with pytest.raises(RoomError, match="找不到"):
        room.edit("日记.txt", "不存在的句子", "x")
    room.write("日记.txt", "重写", overwrite=True)
    trash = sorted(p.name for p in room.trash.iterdir())
    assert any("edit" in n for n in trash) and any("overwrite" in n for n in trash)
    room.delete("日记.txt")
    assert not (room.space / "日记.txt").exists()
    assert any("delete" in p.name for p in room.trash.iterdir())          # 删除 = 进回收站


def test_only_text_files_can_be_written_but_gifts_can_be_read(room):
    with pytest.raises(RoomError, match=r"\.txt"):
        room.write("脚本.py", "print(1)")
    (room.space / GIFT_DIR / "代码.py").write_text("print('hi')", encoding="utf-8")
    assert "print" in room.read(f"{GIFT_DIR}/代码.py")["text"]
    (room.space / GIFT_DIR / "图.png").write_bytes(b"\x89PNG....")
    with pytest.raises(RoomError, match="看不懂"):
        room.read(f"{GIFT_DIR}/图.png")
    with pytest.raises(RoomError, match="编辑"):
        room.edit(f"{GIFT_DIR}/代码.py", "hi", "yo")


def test_size_limits(room):
    with pytest.raises(RoomError, match="一次最多"):
        room.write("长.txt", "字" * (MAX_WRITE_CHARS + 1))
    room.write("长.md", "一二三四五" * 700)
    part = room.read("长.md", max_chars=1000)
    assert part["more"] == 3500 - 1000 and len(part["text"]) == 1000
    assert room.read("长.md", offset=3000)["more"] == 0


def test_gift_folder_itself_is_protected(room):
    for fn in (lambda: room.delete(GIFT_DIR), lambda: room.rename(GIFT_DIR, "别的"), lambda: room.move(GIFT_DIR, "x")):
        with pytest.raises(RoomError, match=GIFT_DIR):
            fn()


def test_mkdir_move_rename(room):
    room.mkdir("画")
    room.write("猫.txt", "=^.^=")
    assert room.move("猫.txt", "画") == {"path": "猫.txt", "to": "画/猫.txt"}
    assert room.rename("画/猫.txt", "小猫") == {"path": "画/猫.txt", "to": "画/小猫.txt"}   # 不写扩展名就保留原来的
    with pytest.raises(RoomError, match="类型"):
        room.rename("画/小猫.txt", "小猫.exe")
    with pytest.raises(RoomError, match="自己里面"):
        room.move("画", "画")
    assert "小猫.txt" in room.tree_text()


def test_log_ids_continue_after_restart(room, tmp_path):
    a = room.append_log({"type": "enter"})
    b = room.append_log({"type": "step"})
    assert (a["id"], b["id"]) == ("r1", "r2")
    again = Room(tmp_path / "room")
    assert again.append_log({"type": "leave"})["id"] == "r3"
    events, off = again.read_log(0)
    assert [e["type"] for e in events] == ["enter", "step", "leave"]
    assert again.read_log(off)[0] == []
    assert [e["id"] for e in again.tail_log(2)] == ["r2", "r3"]


def test_gifts_go_to_gift_folder_and_inbox(room, tmp_path):
    src = tmp_path / "照片说明.txt"
    src.write_text("这是我们去海边那天", encoding="utf-8")
    ev = room.add_gift(src, note="送你的")
    assert ev["path"] == f"{GIFT_DIR}/照片说明.txt" and (room.space / ev["path"]).exists()
    ev2 = room.add_gift(src)                                        # 同名：不覆盖，另起一个名字
    assert ev2["path"] != ev["path"]
    room.add_gift_text("晚安", "便条")
    events, off = room.read_inbox(0)
    assert [e["type"] for e in events] == ["gift"] * 3 and events[0]["note"] == "送你的"
    assert room.read_inbox(off)[0] == []
    with pytest.raises(RoomError):
        room.add_gift(tmp_path)                                     # 文件夹不行


def test_snapshot_once_per_day(room):
    room.write("a.txt", "x")
    name = room.snapshot_daily()
    assert name.endswith(".zip") and (room.snaps / name).exists()
    assert room.snapshot_daily() == ""


def test_signature_and_tree(room):
    room.write("a.txt", "x")
    sig = room.signature()
    assert "a.txt" in sig and GIFT_DIR in sig
    items = {it["path"]: it for it in room.tree()}
    assert items[GIFT_DIR]["type"] == "dir" and items["a.txt"]["text"]


def test_similarity():
    assert similarity("写日记：今天下雨了", "写日记：今天下雨了") == pytest.approx(1.0)
    assert similarity("写日记：今天下雨了", "写日记：今天又下雨了") > 0.6
    assert similarity("画了一只猫", "整理礼物文件夹") < 0.2
    assert similarity("", "x") == 0.0


def test_tail_log_with_big_lines(room):
    for i in range(30):                                            # 带思考过程的行很长（十几 KB）
        room.append_log({"type": "step", "reasoning": "想" * 6000, "i": i})
    tail = room.tail_log(25)
    assert [e["i"] for e in tail] == list(range(5, 30))
    assert len(room.tail_log(100)) == 30


def test_tree_marks_empty_folders_and_counts_chars(room):
    room.write("日记.md", "今天很安静。")
    text = room.tree_text()
    assert f"{GIFT_DIR}/（空的）" in text and "日记.md（6 字）" in text
    (room.space / GIFT_DIR / "歌.mp3").write_bytes(b"x" * 10)                 # 图片可以看（v1.2），音频还看不懂
    assert "看不懂的格式" in room.tree_text() and f"{GIFT_DIR}/（空的）" not in room.tree_text()
