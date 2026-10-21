"""房间里看图片（v1.2）：DeepSeek 看不了图，给她看字符画 + 颜色概况 + 图里的文字；她自己写下“我觉得这是……”。"""
import json
import random
import subprocess

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii import room_image as ri
from pet_ascii.room import GIFT_DIR


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def two_halves(path, w=200, h=100):
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.fillRect(0, 0, w // 2, h, QColor("#e53935"))
    p.fillRect(w // 2, 0, w - w // 2, h, QColor("#1e5bd8"))
    p.end()
    img.save(str(path))
    return path


def test_color_names(qapp):
    cases = {(0, 0, 0): "黑", (255, 255, 255): "白", (128, 128, 128): "灰", (230, 30, 30): "红",
             (30, 60, 220): "蓝", (40, 170, 60): "绿", (250, 210, 30): "黄", (120, 70, 30): "棕", (140, 60, 200): "紫"}
    for rgb, want in cases.items():
        assert ri.color_name(*rgb)[-1] == want, (rgb, ri.color_name(*rgb))


def test_describe_red_left_blue_right(qapp, tmp_path):
    p = two_halves(tmp_path / "a.png")
    d = ri.describe(p, cols=40, ocr=False)
    assert d["size"] == [200, 100] and d["shape"] == "横图"
    main = dict(d["main"])
    assert 40 <= main.get("红", 0) <= 60 and 40 <= main.get("蓝", 0) <= 60
    rows = d["map"].splitlines()
    assert all(r[0] == "红" and r[-1] == "蓝" for r in rows)
    art = d["ascii"].splitlines()
    assert len(art) == 10 and max(len(x) for x in art) <= 40              # 字约两倍高：40 列 → 10 行
    text = ri.render(d)
    assert "字符画" in text and "颜色地图" in text and "这次没法识别" in text


def test_ocr_cleanup_and_subprocess(monkeypatch, qapp, tmp_path):
    assert ri.tidy_ocr("窗 外 下 了 雨") == "窗外下了雨" and ri.tidy_ocr("Hello DeepSeek") == "Hello DeepSeek"
    from PySide6.QtGui import QImage

    img = QImage(10, 10, QImage.Format_RGB32)
    calls = []

    def fake_run(args, **k):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps({"lang": "zh-Hans-CN", "lines": ["你 好", "Hi"]}).encode(), b"")

    monkeypatch.setattr(ri.subprocess, "run", fake_run)
    monkeypatch.setattr(ri.sys, "platform", "win32")
    assert ri.run_ocr(img) == (["你好", "Hi"], "ok")
    assert calls[0][-2] == str(ri.OCR_SCRIPT)
    monkeypatch.setattr(ri.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 0, b'{"error":"x"}', b""))
    assert ri.run_ocr(img)[1].startswith("unavailable")


@pytest.fixture
def rt(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path / "home"))
    QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    cfg.room_ocr = False
    cfg.room_look_vision = False                       # 单元测试不连网；需要时用替身（见下）
    r = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    r.room.rng = random.Random(1)
    r.room.night = lambda now=None: False
    yield r
    dispose(r)
    fr.CUSTOM = {}


class Scripted:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def complete(self, messages, max_tokens=1800, temperature=0.9, timeout=45.0, extra=None):
        self.calls.append(messages)
        return {"text": json.dumps(self.outputs.pop(0), ensure_ascii=False), "usage": {"total_tokens": 100},
                "finish_reason": "stop"}


def step(action, then="leave", **kw):
    return {"thought": "嗯", "action": {"type": action, **kw}, "then": then}


def test_she_looks_at_a_gift_picture_and_says_what_she_saw(rt, tmp_path):
    life = rt.room
    src = two_halves(tmp_path / "旗子.png")
    ev = life.room.add_gift(src)
    life._take_inbox(life.clock())
    first_prompt = {}
    c = Scripted([step("look", path=ev["path"]),                     # 看完说要离开：也会让她接着看到结果
                  {**step("note", text="谢谢主人的旗子"), "saw": "一面左红右蓝的旗子"}])
    life.enter(force=True, client=c, sync=True)
    assert "图片 200×100，可以 look 看" in c.calls[0][1]["content"]
    second = c.calls[1][1]["content"]
    assert "你看到的" in second and "颜色地图" in second and "红" in second and "saw" in second
    looked = next(it for it in life.memory.short if it["kind"] == "look")
    assert looked["what"] == f"看了图片「{ev['path']}」：一面左红右蓝的旗子"
    types = [e["type"] for e in life.room.tail_log(10)]
    assert "saw" in types and life.state["pending"] == [] and life.state["looks_today"] == 1
    step_ev = next(e for e in life.room.tail_log(10) if e.get("action") == "look")
    assert "颜色地图" in step_ev["look_text"]
    text, _ = life.situation(life.clock(), [])
    assert "你看过，觉得是：一面左红右蓝的旗子" in text                        # 下次进来还记得
    life.is_owner = True
    assert "一面左红右蓝的旗子" in life.chat_recent()                          # 聊天时也记得


def test_read_on_a_picture_is_a_look_and_extra_step_after_last(rt, tmp_path):
    life = rt.room
    rt.cfg.room_max_steps = 1
    two_halves(life.room.space / "画.png")
    c = Scripted([step("read", then="stay", path="画.png"), {**step("nothing"), "saw": "红蓝两半"}])
    life.enter(force=True, client=c, sync=True)
    assert len(c.calls) == 2 and "你看到的" in c.calls[1][1]["content"]       # 步数用完也多给一步看结果
    assert [e.get("action") for e in life.room.tail_log(6) if e["type"] == "step"] == ["look", "nothing"]


def test_daily_cap_and_non_images(rt, tmp_path):
    life = rt.room
    two_halves(life.room.space / "画.png")
    life.room.write("字.txt", "hello")
    life.state["looks_today"] = rt.cfg.room_look_daily
    c = Scripted([step("look", path="画.png"), step("nothing")])
    life.enter(force=True, client=c, sync=True)
    ev = [e for e in life.room.tail_log(6) if e["type"] == "step"][0]
    assert not ev["ok"] and "明天再看" in ev["error"]
    life.state["looks_today"] = 0
    c = Scripted([step("look", path="字.txt"), step("nothing")])
    life.enter(force=True, client=c, sync=True)
    ev = [e for e in life.room.tail_log(6) if e["type"] == "step"][-2]
    assert not ev["ok"] and "read" in ev["error"]


def test_viewer_previews_pictures(rt, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from pet_ascii.room_viewer import RoomViewer

    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    life = rt.room
    two_halves(life.room.space / "画.png")
    v = RoomViewer(life.room.root, poll_ms=60000)
    item = next(v.tree.topLevelItem(i) for i in range(v.tree.topLevelItemCount())
                if v.tree.topLevelItem(i).text(0) == "画.png")
    v._on_tree(item, 0)
    assert "还没看过" in v.preview.toPlainText()
    life.looker.look(life.room.space / "画.png", "画.png", 40, False)
    life.looker.note_saw(life.room.space / "画.png", "画.png", "红蓝旗")
    v._on_tree(item, 0)
    t = v.preview.toPlainText()
    assert "她觉得是：红蓝旗" in t and "颜色地图" in t
    dispose(v)


def test_flash_also_looks_and_its_words_reach_her(rt, tmp_path):
    life = rt.room
    rt.cfg.room_look_vision = True
    calls = []

    def fake_flash(img):
        calls.append((img.width(), img.height()))
        return {"text": "左半边是红色、右半边是蓝色的纯色块，没有别的东西。", "tokens": 300, "model": "deepseek-flash"}

    life.vision_fn = fake_flash
    two_halves(life.room.space / "画.png")
    c = Scripted([step("look", path="画.png"), {**step("nothing"), "saw": "一面红蓝旗"}])
    life.enter(force=True, client=c, sync=True)
    second = c.calls[1][1]["content"]
    assert "副模型替你真的看了一眼原图" in second and "左半边是红色" in second and "字符画" in second
    assert calls == [(200, 100)] and life.state["tokens_today"] == 300 + 200      # 看图 300 + 两次决定各 100
    ev = next(e for e in life.room.tail_log(8) if e.get("action") == "look")
    assert ev["result"]["vision"] is True and ev["result"]["vision_tokens"] == 300
    # 同一张图再看：不再请副模型（缓存），也不再计 token
    c2 = Scripted([step("look", path="画.png"), step("nothing")])
    life.enter(force=True, client=c2, sync=True)
    assert len(calls) == 1 and "左半边是红色" in c2.calls[1][1]["content"]


def test_if_she_does_not_say_memory_keeps_flash_first_sentence(rt, tmp_path):
    life = rt.room
    rt.cfg.room_look_vision = True
    life.vision_fn = lambda img: {"text": "一张红蓝各半的纯色图。没有别的内容。", "tokens": 10}
    two_halves(life.room.space / "画.png")
    c = Scripted([step("look", path="画.png"), step("nothing")])          # 第二步没写 saw
    life.enter(force=True, client=c, sync=True)
    looked = next(it for it in life.memory.short if it["kind"] == "look")
    assert looked["what"] == "看了图片「画.png」：（副模型说）一张红蓝各半的纯色图。"
    assert life.looker.saw_map(life.room) == {}                          # 不当成她自己的看法
    ev = next(e for e in life.room.tail_log(6) if e["type"] == "saw")
    assert ev["hers"] is False


def test_flash_failure_still_gives_the_ascii_view(rt, tmp_path):
    life = rt.room
    rt.cfg.room_look_vision = True

    def broken(img):
        raise RuntimeError("网络错误")

    life.vision_fn = broken
    two_halves(life.room.space / "画.png")
    c = Scripted([step("look", path="画.png"), step("nothing")])
    life.enter(force=True, client=c, sync=True)
    second = c.calls[1][1]["content"]
    assert "副模型这次没看成" in second and "字符画" in second
    calls = []
    life.vision_fn = lambda img: calls.append(1) or {"text": "红蓝两块", "tokens": 50}
    c2 = Scripted([step("look", path="画.png"), step("nothing")])
    life.enter(force=True, client=c2, sync=True)
    assert calls == [1] and "红蓝两块" in c2.calls[1][1]["content"]                  # 上次没看成，这次补上
