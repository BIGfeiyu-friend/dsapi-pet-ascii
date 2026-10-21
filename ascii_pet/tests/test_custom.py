import json
import time

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.ai import evolve
from pet_ascii.custom_presets import CustomPresets, spec_distance
from pet_ascii.inline import custom_memo, parse_tag, tag_to_animation
from pet_ascii.protocol import parse_animation


@pytest.fixture
def lib(tmp_path):
    lib = CustomPresets(tmp_path / "custom.json", max_items=5)
    yield lib
    dispose(lib)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


SNICKER = {"preset": "shy", "eyes": {"smile": 0.6, "open": 0.7}, "mouth": {"shape": "cat", "curve": 0.6}, "blush": 0.9}


def test_add_use_and_persist(lib, tmp_path):
    name, status = lib.add("憋笑", SNICKER)
    assert (name, status) == ("憋笑", "new")
    assert fr.is_preset("憋笑")
    s = fr.normalize({"preset": "憋笑", "level": 1.0})
    assert s["mouth"]["shape"] == "cat" and s["blush"] == 0.9
    half = fr.normalize({"preset": "憋笑", "level": 0.5})
    assert half["blush"] < 0.9
    lib.flush()
    lib2 = CustomPresets(tmp_path / "custom.json")
    assert "憋笑" in lib2.items and lib2.items["憋笑"]["origin"] == "chat"


def test_near_duplicates_merge_and_invalid_names(lib):
    lib.add("憋笑", SNICKER)
    name, status = lib.add("偷笑", {**SNICKER, "blush": 0.85})
    assert (name, status) == ("憋笑", "merged")
    name, status = lib.add("心动二号", {"preset": "love"})          # 与内置预设几乎一样
    assert (name, status) == ("love", "merged")
    name, status = lib.add("happy", {"preset": "angry", "eyes": {"pupil": "star"}})  # 不能与内置重名
    assert status == "invalid" and name in fr.PRESETS
    name, status = lib.add("非常非常非常长的一个表情名字", SNICKER)
    assert status == "invalid"


def test_update_rename_delete_and_cap(lib):
    lib.add("憋笑", SNICKER)
    assert lib.add("憋笑", {**SNICKER, "sweat": 0.8})[1] == "updated"
    assert fr.CUSTOM["憋笑"]["sweat"] == 0.8
    assert lib.rename("憋笑", "忍笑") and "忍笑" in fr.CUSTOM and "憋笑" not in lib.items
    assert lib.resolve("憋笑") == "忍笑" and fr.is_preset("憋笑")       # 模型学会的旧名字照样能用
    lib.delete("忍笑")
    assert not lib.items
    shapes = ["wavy", "cat", "pout", "o", "triangle", "teeth", "tongue"]
    for i, sh in enumerate(shapes):
        lib.add(f"测试{i}", {"preset": "dizzy", "mouth": {"shape": sh, "open": 0.9}, "eyes": {"pupil": "heart", "tilt": -1 + i * 0.3}})
    assert len(lib.items) <= 5


def test_spec_distance_orders_sensibly():
    a = fr.normalize({"preset": "happy"})
    assert spec_distance(a, a) == 0
    assert spec_distance(a, fr.normalize({"preset": "laugh"})) < spec_distance(a, fr.normalize({"preset": "angry"}))


def test_inline_new_tag_parsing():
    t = parse_tag("<face new=憋笑 from=shy smile=0.6 open=0.7 mouth=cat curve=0.6 blush=0.9 look=left r.smile=1 act=nod>")
    assert t["new"]["name"] == "憋笑"
    spec = t["new"]["spec"]
    assert spec["preset"] == "shy" and spec["eyes"] == {"smile": 0.6, "open": 0.7}
    assert spec["eye_r"] == {"smile": 1.0} and spec["mouth"] == {"shape": "cat", "curve": 0.6}
    assert spec["gaze"] == [-0.8, 0.0] and spec["blush"] == 0.9 and t["act"] == "nod"
    bad = parse_tag("<face new=x from=nope open=99 pupil=laser mouth=fangs>")["new"]["spec"]
    assert bad["eyes"] == {"open": 1.3} and "preset" not in bad and "mouth" not in bad


def test_custom_preset_usable_in_tags_and_protocol(lib):
    lib.add("憋笑", SNICKER)
    t = parse_tag("<face 憋笑 level=0.8>")
    assert t["preset"] == "憋笑"
    anim = parse_animation(tag_to_animation(t), None)
    assert anim.ops[0].frames[0].spec["preset"] == "憋笑"
    assert "憋笑" in custom_memo(lib.names())


def test_evolve_with_fake_model(lib):
    class Fake:
        name = "fake"

        def __init__(self):
            self.messages = None

        def complete(self, messages, max_tokens=0, temperature=0, timeout=0, extra=None):
            self.messages, self.extra = messages, extra
            out = {"thoughts": "她常常嘴硬心软", "presets": [
                {"name": "嘴硬", "emotion": "嘴上不服气其实很开心", "from": "pout",
                 "spec": {"eyes": {"smile": 0.4}, "mouth": {"shape": "wavy", "curve": 0.3}, "blush": 0.8, "gaze": [0.7, 0]}},
                {"name": "抄袭开心", "emotion": "就是开心", "from": "happy", "spec": {}}]}
            return {"text": "好的：\n" + json.dumps(out, ensure_ascii=False), "usage": {"total_tokens": 900}, "model": "fake"}

    fake = Fake()
    dialogue = [{"user": "你是不是想我了", "pet": "（扭过头）才、才没有！", "face": "<face pout>"}]
    rep = evolve.run(fake, "深深", dialogue, lib)
    assert fake.extra and fake.extra["thinking"]["type"] == "enabled"
    req = json.loads(fake.messages[1]["content"])
    assert req["recent_dialogue"] == dialogue and "happy" in req["builtin_presets"]
    statuses = {r["name"]: r["status"] for r in rep["results"]}
    assert statuses == {"嘴硬": "new", "抄袭开心": "merged"}
    assert lib.items["嘴硬"]["origin"] == "evolve" and lib.items["嘴硬"]["note"].startswith("嘴上")


def test_runtime_inline_creation_and_window(lib, tmp_path, monkeypatch, disposer):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime
    from pet_ascii.window import AsciiPetWindow

    app = QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    rt = disposer(Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False))
    rt.custom.items.clear()
    rt.custom._publish()
    rt.record_user("你今天开心吗")
    rt.apply_inline_tag("<face new=小雀跃 from=happy smile=0.9 mouth=tongue mopen=0.5 blush=0.7 fx=sparkle>")
    assert rt.last_created["status"] == "new" and "小雀跃" in rt.custom.items
    rt.on_chat_reply("（蹦了一下）开心呀！", tag_seen=True)
    assert rt.custom.items["小雀跃"]["context"].startswith("（蹦了一下）")
    assert rt.dialogue[-1]["face"].startswith("<face new=小雀跃")
    for _ in range(10):
        rt.engine.tick(0.1)
    assert rt.engine.face_spec["mouth"]["shape"] == "tongue"
    # 生成器输出里的 new_presets 先入库
    rt._on_generator_presets([{"name": "发呆", "from": "bored", "spec": {"eyes": {"open": 0.4, "pupil": "dot"}}}])
    assert "发呆" in rt.custom.items
    win = disposer(AsciiPetWindow(rt))
    win.show()
    app.processEvents()
    assert win.framed and win.view.background is not None and win.title is not None
    h = win.height()
    win.toggle_fold()
    app.processEvents()
    assert win.height() < h
