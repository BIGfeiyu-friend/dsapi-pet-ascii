"""表情自我迭代：对话素材持久化、版本与回滚、👍/👎 与使用次数、合并/淘汰/恢复、改良内置表情、照镜子、进化动作。"""
import json

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.ai import evolve
from pet_ascii.custom_presets import CustomPresets
from pet_ascii.dialogue_log import DialogueLog

SNICKER = {"preset": "shy", "eyes": {"smile": 0.6, "open": 0.7}, "mouth": {"shape": "cat", "curve": 0.6}, "blush": 0.9}
GLEE = {"preset": "cat", "eyes": {"smile": 0.6}, "mouth": {"shape": "cat", "curve": 0.5}, "blush": 0.9, "gaze": [0.5, 0.3]}
STUBBORN = {"preset": "pout", "gaze": [0.8, 0.0], "blush": 0.9, "eyes": {"pupil": "dot"}}


@pytest.fixture
def lib(tmp_path):
    lib = CustomPresets(tmp_path / "lib.json", max_items=6)
    yield lib
    dispose(lib)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def test_dialogue_log_persists_and_counts(tmp_path):
    d = DialogueLog(tmp_path / "d.json", keep=4)
    for i in range(6):
        d.append({"user": f"u{i}", "pet": f"p{i}", "face": ""})
    assert len(d) == 4 and d.since_evolve == 6 and d[-1]["pet"] == "p5"
    d2 = DialogueLog(tmp_path / "d.json", keep=4)                 # 重启后还在
    assert len(d2) == 4 and d2.since_evolve == 6 and "time" not in d2.snapshot()[0]
    d2.mark_evolved()
    assert DialogueLog(tmp_path / "d.json").since_evolve == 0
    d2.clear()
    assert len(DialogueLog(tmp_path / "d.json")) == 0


def test_revise_keeps_versions_and_rollback(lib):
    lib.add("憋笑", SNICKER)
    v1 = lib.items["憋笑"]["spec"]
    assert lib.revise("憋笑", {"mouth": {"shape": "tongue"}, "eye_r": {"smile": 1.0}}, reason="更俏皮")[0] == "revised"
    v2 = lib.items["憋笑"]["spec"]
    assert v2["mouth"]["shape"] == "tongue" and v2["eye_r"]["smile"] == 1.0
    assert v2["eye_l"] == v1["eye_l"] and v2["blush"] == v1["blush"]     # 没写的字段不变
    assert fr.CUSTOM["憋笑"]["mouth"]["shape"] == "tongue"
    assert lib.revise("憋笑", {"blush": v2["blush"] - 0.01})[0] == "unchanged"
    assert lib.rollback("憋笑") and lib.items["憋笑"]["spec"] == v1
    assert not lib.rollback("憋笑")
    assert lib.revise("不存在", {"blush": 1})[0] == "missing"


def test_feedback_usage_and_builtin_improvement(lib):
    assert lib.revise("sleepy", {"eyes": {"open": 0.15}})[0] == "refused"    # 没被嫌弃的内置表情不能改
    lib.touch("sleepy")
    lib.touch("sleepy")
    assert lib.rate("sleepy", -1, "她打哈欠的样子像在瞪人")
    assert lib.builtin["sleepy"]["uses"] == 2 and lib.feedback_log[-1]["vote"] == "👎"
    orig = fr.normalize({"preset": "sleepy"})
    assert lib.resolve("困倦") == "sleepy"                                   # 模型用中文名称呼内置表情也能找到
    assert lib.revise("困倦", {"eyes": {"open": 0.12, "smile": 0.5}}, reason="更像困")[0] == "revised"
    assert fr.normalize({"preset": "sleepy"})["eye_l"]["open"] == pytest.approx(0.12)
    assert fr.normalize({"preset": "sleepy", "level": 0.5})["eye_l"]["open"] > 0.12    # 强度照常生效
    ctx = lib.evolve_context()
    sl = ctx["builtin_presets"]["sleepy"]
    assert sl["improved"] and sl["dislikes"] == 1 and sl["uses"] == 2 and "looks" in sl["mirror"]
    assert ctx["feedback_log"][0]["context"].startswith("她打哈欠")
    lib.flush()
    CustomPresets(lib.path)                                                # 持久化
    assert fr.normalize({"preset": "sleepy"})["eye_l"]["open"] == pytest.approx(0.12)
    assert lib.rollback("sleepy") and fr.normalize({"preset": "sleepy"}) == orig   # 回滚到原版


def test_merge_retire_restore_and_protection(lib):
    assert lib.add("憋笑", SNICKER)[1] == "new"
    assert lib.add("偷乐", GLEE)[1] == "new"
    assert lib.add("嘴硬", STUBBORN)[1] == "new"
    lib.rate("憋笑", +1)
    assert lib.merge("憋笑", "偷乐")[0] is False                 # 👍 过的不能并掉
    assert lib.retire("憋笑")[0] is False                       # 也不能淘汰
    ok, _ = lib.merge("偷乐", "憋笑", reason="太像了")
    assert ok and "偷乐" not in lib.items and lib.retired["偷乐"]["merged_into"] == "憋笑"
    assert lib.resolve("偷乐") == "憋笑" and fr.is_preset("偷乐")   # 旧名字照样能用
    assert fr.normalize({"preset": "偷乐"}) == fr.normalize({"preset": "憋笑"})
    assert "偷乐" not in lib.names()
    assert lib.retire("嘴硬", "一直没用")[0] and not fr.is_preset("嘴硬")
    assert lib.restore("嘴硬") and fr.is_preset("嘴硬")
    assert lib.retire("happy")[0] is False
    lib.flush()
    lib2 = CustomPresets(lib.path)
    assert lib2.aliases == {"偷乐": "憋笑"} and "偷乐" in lib2.retired


def test_mirror_text_and_visible_diff(lib):
    assert "心形瞳孔" in fr.mirror_text(fr.normalize({"preset": "love"}))
    assert "眯成一条缝" in fr.mirror_text(fr.normalize({"preset": "sleepy"}))
    assert "^" in fr.mirror_text(fr.normalize({"preset": "happy"}))
    assert "看不出来" in fr.mirror_text(fr.normalize({"preset": "angry"}))     # 提醒：眉毛几乎看不见
    from pet_ascii.character import find_character

    rig = find_character("maid_bust").poses["idle"].rig
    a = fr.normalize({"preset": "happy"})
    assert rig.visible_diff(a, a) == 0
    assert rig.visible_diff(a, fr.normalize({"preset": "love"})) > \
        rig.visible_diff(a, fr.normalize({"preset": "happy", "blush": 0.65}))
    m = lib.mirror(fr.normalize({"preset": "happy", "blush": 0.62}), rig=rig)
    assert m["closest"].startswith("开心") and m["visible_diff"].endswith("%")


class FakeDesigner:
    def __init__(self, design, check):
        self.calls, self.design, self.check = [], design, check

    def complete(self, messages, max_tokens=0, temperature=0, timeout=0, extra=None):
        self.calls.append((messages, extra))
        obj = self.check if "照镜子看它们" in messages[0]["content"] else self.design
        return {"text": json.dumps(obj, ensure_ascii=False), "usage": {"total_tokens": 500}, "model": "fake"}


def test_evolve_actions_and_mirror_check(lib):
    for n, s in (("憋笑", SNICKER), ("偷乐", GLEE), ("嘴硬", STUBBORN)):
        lib.add(n, s)
    lib.rate("嘴硬", -1, "看起来像在生气")
    design = {"thoughts": "嘴硬看着像生气；偷乐和憋笑太像；缺一个发呆", "actions": [
        {"op": "revise", "name": "嘴硬", "reason": "更像嘴硬心软", "spec": {"eyes": {"smile": 0.4}, "blush": 1.0}},
        {"op": "merge", "name": "偷乐", "into": "憋笑", "reason": "太像"},
        {"op": "retire", "name": "happy", "reason": "试图淘汰内置"},
        {"op": "create", "name": "发呆", "emotion": "放空", "from": "bored",
         "spec": {"eyes": {"open": 0.5, "pupil": "dot", "highlight": 0.2}}},
        {"op": "create", "name": "多余", "emotion": "超出动作上限", "spec": {}}]}
    check = {"checks": [{"name": "嘴硬", "ok": True},
                        {"name": "发呆", "ok": False, "why": "不够呆", "spec": {"mouth": {"shape": "o", "open": 0.2}}},
                        {"name": "憋笑", "ok": False, "spec": {"blush": 0}}]}
    fake = FakeDesigner(design, check)
    rep = evolve.run(fake, "深深", [{"user": "你是不是想我了", "pet": "才没有", "face": "<face pout>"}], lib, check=True)
    ops = {(r["op"], r["name"]): r["status"] for r in rep["results"]}
    assert ops == {("revise", "嘴硬"): "revised", ("merge", "偷乐"): "merged", ("retire", "happy"): "refused",
                   ("create", "发呆"): "new"}
    req = json.loads(fake.calls[0][0][1]["content"])
    zui = next(c for c in req["custom_presets"] if c["name"] == "嘴硬")
    assert zui["dislikes"] == 1 and "looks" in zui["mirror"] and req["feedback_log"][0]["name"] == "嘴硬"
    assert fake.calls[0][1]["thinking"]["type"] == "enabled" and fake.calls[1][1]["thinking"]["type"] == "disabled"
    designs = json.loads(fake.calls[1][0][1]["content"])["designs"]
    assert {d["name"] for d in designs} == {"嘴硬", "发呆"}
    assert lib.items["发呆"]["spec"]["mouth"]["shape"] == "o"             # 照镜子后修正
    assert lib.items["发呆"]["last_by"] == "mirror" and len(lib.items["发呆"]["history"]) == 1
    assert lib.items["憋笑"]["spec"]["blush"] > 0                         # 复查只能改本次设计的表情
    assert [c["name"] for c in rep["checks"]] == ["嘴硬", "发呆"] and rep["usage"]["total_tokens"] == 1000


@pytest.fixture
def make_rt(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    made = []

    def make():
        cfg = AsciiConfig()
        cfg.character = "maid_bust"
        cfg.chat_link = "inline"
        r = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
        made.append(r)
        return r

    yield make
    dispose(*made)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def test_runtime_rating_persistence_probe_and_lab(make_rt):
    from pet_ascii.ai.mock import MockClient

    rt = make_rt()
    rt.record_user("今天好累")
    rt.apply_inline_tag("<face sad level=0.7>")
    rt.on_chat_reply("（摸摸头）辛苦啦", tag_seen=True)
    rt.record_user("自检", probe=True)                        # 探针：照常演出，但不计入素材
    rt.apply_inline_tag("<face smile>")
    rt.on_chat_reply("（自检回复）", tag_seen=True)
    assert len(rt.dialogue) == 1 and rt.dialogue.since_evolve == 1
    assert "uses" not in rt.custom.builtin.get("smile", {})      # 探针用到的表情不计使用次数
    for _ in range(30):
        rt.engine.tick(0.1)
    assert rt.current_expression() == "smile"
    assert rt.rate_current(-1) == "smile"
    assert rt.custom.builtin["smile"]["dislikes"] == 1 and rt.custom.builtin["sad"]["uses"] == 1
    assert rt.custom.feedback_log[-1]["context"] == "（摸摸头）辛苦啦"
    rt.stop()

    rt2 = make_rt()                                            # 重启：素材、计数、评价都还在
    assert len(rt2.dialogue) == 1 and rt2.dialogue.since_evolve == 1
    assert rt2.custom.builtin["smile"]["dislikes"] == 1
    rep = evolve.run(MockClient(), "x", rt2.dialogue.snapshot(), rt2.custom, rig=rt2.engine.pose.rig, check=True)
    assert any(r["final"] == "憋笑" for r in rep["results"]) and rep["checks"][0]["ok"]
    rt2._on_evolve_done(rep)
    assert rt2.dialogue.since_evolve == 0 and rt2.last_evolve["results"]

    from pet_ascii.lab import Lab
    from pet_ascii.window import AsciiPetWindow

    lab = Lab(rt2)
    lab._refresh_custom()
    assert lab.custom_list.count() >= 1 and "照镜子" in lab.custom_list.item(0).toolTip()
    win = AsciiPetWindow(rt2)
    win._rate(1)
    assert "记下了" in win.title.expr.text()
    rt2.clear_dialogue()
    assert len(rt2.dialogue) == 0
    dispose(win, lab)
