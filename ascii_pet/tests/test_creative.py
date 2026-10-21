"""v0.7 创作能力：可创造的组件、可编辑骨架、本地视觉检查、创作记忆与探索、后台视觉评价、自主视觉实验。"""
import json
import random
import threading
import time
import types

import pytest
from conftest import dispose

from pet_ascii import creative
from pet_ascii import face_rig as fr
from pet_ascii import parts as pt
from pet_ascii import visual_check as vc

STAR_EYE = {"slot": "eye", "style": "quirky", "draw": [
    {"t": "ellipse", "c": [0, 0], "r": [0.95, 0.9], "fill": "sclera", "stroke": "ink", "w": 0.9},
    {"t": "star", "c": [0.1, 0.05], "r": 0.75, "n": 6, "fill": "#ff5d8f"}]}
TINY_EYE = {"slot": "eye", "draw": [{"t": "dots", "pts": [[0, 0]], "r": 0.05, "fill": "#f0d8d0"}]}
ZIG = {"slot": "mouth", "mirror": False, "draw": [
    {"t": "line", "pts": [[-1, 0], [-0.6, -0.5], [-0.2, 0.5], [0.2, -0.5], [0.6, 0.5], [1, 0]], "stroke": "ink", "w": 1.0}]}


@pytest.fixture
def rt(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    cfg.chat_link = "inline"
    cfg.vision_min_interval_s = 0.0
    r = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    yield r
    dispose(r)
    fr.CUSTOM, fr.OVERRIDES, fr.PARTS, fr.PARTS_DIGEST, fr.SKELETONS = {}, {}, {}, {}, {}
    creative.SCENES.clear()


def roles(rt):
    return tuple(rt.engine.pose.rig.col)


# ---------------------------------------------------------------------- 组件
def test_component_validation_clamps_and_rejects(rt):
    w = []
    c = pt.clean_component({"slot": "eye", "draw": [
        {"t": "ellipse", "c": [9, -9], "r": [50, 0.001], "fill": "#ABC"},
        {"t": "laser"}, {"t": "line", "pts": [[0, 0]]},
        {"t": "poly", "pts": [[0, 0], [1, 0], [0, 1]], "fill": "rainbow"}] + [{"t": "dots", "pts": [[0, 0]]}] * 40,
        "glyphs": {"rows": ["^_^中", "", ""], "color": "#ff00ff"}}, roles(rt), w)
    e = c["draw"][0]
    assert e["c"] == [2.5, -2.5] and e["r"] == [2.5, 0.02] and e["fill"] == "#aabbcc"
    assert len(c["draw"]) == pt.LIMITS["prims"] - 2                  # 先截断到上限 32 个，其中 2 个无效的被丢掉
    assert c["glyphs"]["rows"] == ["^_^?"]                           # 宽字符被替换，空行去掉
    assert any("未知图元" in x for x in w) and any("点数不够" in x for x in w) and any("颜色" in x for x in w)
    assert pt.clean_component({"slot": "eye", "draw": [{"t": "laser"}]}, roles(rt), []) is None
    assert pt.clean_component("rm -rf /", roles(rt), []) is None


def test_component_renders_mirrored_on_both_eyes(rt):
    rig = rt.engine.pose.rig
    lib = {"星芒眼": pt.clean_component(STAR_EYE, roles(rt))}
    spec = fr.normalize({"parts": {"eye_l": "星芒眼", "eye_r": "星芒眼"}})
    img, layers, info = rig.compose(spec, lib, want_layers=True)
    assert info["used"] == ["星芒眼", "星芒眼"] and not info["missing"]
    bl, br = layers["eye_l"].getbbox(), layers["eye_r"].getbbox()
    nl, nr = rig.nodes["eye_l"], rig.nodes["eye_r"]
    assert bl[0] < nl["cx"] < bl[2] and br[0] < nr["cx"] < br[2]       # 画在各自的眼睛节点上
    # 镜像：星形偏向 +x（脸中线）→ 左眼的重心偏右、右眼的重心偏左
    assert (bl[0] + bl[2]) / 2 >= nl["cx"] - 1 and (br[0] + br[2]) / 2 <= nr["cx"] + 1
    assert rig.visible_diff(fr.neutral(), spec, parts=lib) > 0.04


def test_old_specs_keep_the_original_fast_path(rt, monkeypatch):
    rig = rt.engine.pose.rig
    monkeypatch.setattr(rig, "compose", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走分层绘制")))
    for name in ("happy", "dizzy", "love", "neutral"):
        rig._render_uncached(fr.normalize({"preset": name}))            # 没有骨架字段：仍走原来的 draw
    assert fr.spec_key(fr.normalize({"preset": "happy"}))[-1] != "cand" and len(fr.spec_key(fr.neutral())) == 15   # v0.8 多了歪嘴


def test_glyph_component_overlays_ascii(rt):
    rig = rt.engine.pose.rig
    lib = {"斜线": pt.clean_component({"slot": "cheek", "glyphs": {"rows": ["///"], "color": "#ff5d8f"}}, roles(rt))}
    chars, colors = rig.render(fr.normalize({"parts": {"cheek_l": "斜线"}}), lib)
    assert "///" in "\n".join("".join(r) for r in chars)


def test_rig_fields_normalize_inherit_lerp_and_digest(rt):
    lib = {"锯齿嘴": pt.clean_component(ZIG, roles(rt))}
    fr.PARTS, fr.PARTS_DIGEST = lib, {n: pt.digest(c) for n, c in lib.items()}
    a = fr.normalize({"parts": {"mouth": "锯齿嘴", "bogus": "x", "eye_l": "../../etc"},
                      "nodes": {"mouth": {"dy": 3, "sx": 99, "rot": 500}, "nose": {"dx": 0.1}},
                      "extra": [{"id": "Third Eye", "part": "锯齿嘴"}, {"id": "third", "x": 5, "part": "锯齿嘴"}]})
    assert a["parts"] == {"mouth": "锯齿嘴"}                          # 未知节点、非法名字被丢掉
    assert a["nodes"] == {"mouth": {"dy": 0.5, "sx": 3.0, "rot": 90.0}}  # 钳制；锚点节点不能变换
    assert a["extra"] == [{"id": "third", "x": 1.2, "y": 0.5, "w": 0.08, "h": 0.08, "part": "锯齿嘴"}]
    nxt = fr.normalize({"eyes": {"open": 0.5}}, a)                    # 后续帧不写骨架字段：沿用
    assert nxt["parts"] == a["parts"] and nxt["nodes"] == a["nodes"]
    mid = fr.lerp_spec(fr.neutral(), a, 0.5)
    assert abs(mid["nodes"]["mouth"]["dy"] - 0.25) < 1e-6 and mid["parts"] == {"mouth": "锯齿嘴"}
    k1 = fr.spec_key(a)
    lib["锯齿嘴"] = {**lib["锯齿嘴"], "draw": lib["锯齿嘴"]["draw"] + [{"t": "dots", "pts": [[0, 0]], "r": 0.1, "fill": "ink", "w": 0.7, "a": 1.0}]}
    fr.PARTS_DIGEST = {n: pt.digest(c) for n, c in lib.items()}
    assert fr.spec_key(a) != k1                                       # 改了组件：缓存键跟着变，会重画
    fr.SKELETONS = {"歪脸": {"nodes": {"mouth": {"rot": 20}}}}
    s = fr.normalize({"skeleton": "歪脸", "nodes": {"eye_l": {"dy": 0.02}}})
    assert s["nodes"] == {"mouth": {"rot": 20.0}, "eye_l": {"dy": 0.02}}
    assert "换成了组件「锯齿嘴」" in fr.mirror_text(a) and "下移 50%" in fr.mirror_text(a)


def test_skeleton_ops(rt):
    nodes = rt.engine.pose.rig.nodes
    w = []
    f = pt.apply_ops({}, [{"op": "move", "node": "mouth", "dx": 0.02, "dy": 0.9},
                          {"op": "scale", "node": "eye_l", "s": 1.5}, {"op": "rotate", "node": "eye_r", "deg": 30},
                          {"op": "replace", "node": "brow_l", "part": "none"},
                          {"op": "add", "id": "horn", "x": 0.2, "y": 0.1, "w": 0.05, "h": 0.1, "part": "角"},
                          {"op": "add", "id": "eye_l", "part": "角"}, {"op": "explode"}, {"op": "move", "node": "nowhere"},
                          {"op": "group", "nodes": ["eye_l", "eye_r"], "dy": 0.03, "s": 1.2},
                          {"op": "remove", "node": "horn"}, {"op": "remove", "node": "brow_r"}], nodes, w)
    assert f["nodes"]["mouth"] == {"dx": 0.02, "dy": 0.5}               # 钳制到 ±0.5
    assert f["parts"] == {"brow_l": "none", "brow_r": "none"}
    assert "extra" not in f                                           # horn 加了又删；冒充核心节点的 id 被拒
    assert f["nodes"]["eye_l"]["sx"] == pytest.approx(1.8) and f["nodes"]["eye_r"]["rot"] == 30.0
    assert f["nodes"]["eye_l"]["dx"] < 0 < f["nodes"]["eye_r"]["dx"]    # 一起放大：两眼向外分开
    assert any("未知骨架操作" in x for x in w) and any("nowhere" in x for x in w)
    f2 = pt.apply_ops(f, [{"op": "reset", "node": "mouth"}], nodes)
    assert "mouth" not in f2.get("nodes", {})


def test_transformed_builtin_part_moves_in_render(rt):
    rig = rt.engine.pose.rig
    spec = fr.normalize({"preset": "happy", "nodes": {"mouth": {"dy": 0.08}}})
    _, layers, _ = rig.compose(spec, want_layers=True)
    _, base_layers, _ = rig.compose(fr.normalize({"preset": "happy", "nodes": {"eye_l": {"dx": 0.0001, "sx": 1.01}}}),
                                    want_layers=True)
    assert layers["mouth"].getbbox()[1] > base_layers["mouth"].getbbox()[1] + 10


# ---------------------------------------------------------------------- 本地视觉检查
def test_visual_check_builtin_presets_are_clean(rt):
    rig = rt.engine.pose.rig
    for name in fr.PRESETS:
        r = vc.analyze(rig, fr.normalize({"preset": name}))
        assert not r["errors"], (name, r["errors"])


def test_visual_check_finds_deterministic_problems(rt):
    rig = rt.engine.pose.rig
    lib = {"小点眼": pt.clean_component(TINY_EYE, roles(rt)),
           "第三眼": pt.clean_component({"slot": "accessory", "draw": [{"t": "ellipse", "c": [0, 0], "r": [0.6, 1],
                                                                        "fill": "#2b1d3a"}]}, roles(rt))}
    spec = {"preset": "happy", "parts": {"eye_l": "小点眼", "eye_r": "小点眼", "mouth": "不存在"},
            "nodes": {"mouth": {"dy": -0.45}}, "extra": [{"id": "third", "x": 0.5, "y": 0.08, "w": 0.05, "h": 0.07, "part": "第三眼"}]}
    r = vc.analyze(rig, spec, parts=lib, snapshots=True)
    joined = " ".join(r["errors"])
    assert not r["ok"]
    assert "不存在的组件「不存在」" in joined and "眼睛跑到了嘴的下面" in joined and "几乎看不出来" in joined
    assert any("third" in x and "头发" in x for x in r["warnings"])
    assert r["parts"]["eye_l"]["visible_cells"] < 3
    imgs = r["images"]
    assert all(v[:8] == b"\x89PNG\r\n\x1a\n" for v in imgs.values())
    x0, y0, x1, y1 = r["region_px"]                                   # 截图范围围绕改动的部位，而不是整张脸
    assert (x1 - x0) * (y1 - y0) < (rig.px_box[2] - rig.px_box[0]) * (rig.px_box[3] - rig.px_box[1]) * 0.95


def test_engine_bobs_component_mouth_when_talking(rt):
    lib = {"锯齿嘴": pt.clean_component(ZIG, roles(rt))}
    fr.PARTS, fr.PARTS_DIGEST = lib, {n: pt.digest(c) for n, c in lib.items()}
    rt.engine.set_expression({"parts": {"mouth": "锯齿嘴"}}, 0)
    rt.engine.tick(0.05)
    rt.engine.talk(2.0)
    rt.engine.tick(0.13)
    spec = rt.engine._displayed_spec()
    assert spec["nodes"]["mouth"]["sy"] > 1.0 and spec["mouth"] == rt.engine.current_face_spec()["mouth"]


# ---------------------------------------------------------------------- 创作记忆、探索
def test_choose_target_explores_and_exploits(rt):
    mem = rt.creative
    t = mem.choose_target(random.Random(1), epsilon=0.0)
    assert t["style"] in pt.STYLES and t["explore"]                  # 什么都没试过：探索
    for s in pt.STYLES:
        mem.data["styles"][s] = {"tries": 10, "adopted": 1, "quality": 0.3}
    mem.data["styles"]["abstract"] = {"tries": 10, "adopted": 9, "quality": 0.9}
    picks = [mem.choose_target(random.Random(i), epsilon=0.0)["style"] for i in range(5)]
    assert set(picks) == {"abstract"}                                 # 利用：成功率和质量最高的风格
    rnd = {mem.choose_target(random.Random(i), epsilon=1.0)["style"] for i in range(40)}
    assert len(rnd) >= 4                                              # 随机探索仍会覆盖各种风格
    kinds = {mem.choose_target(random.Random(i))["kind"] for i in range(60)}
    assert {"invent", "skeleton", "scene"} <= kinds


def test_elites_protect_diversity(rt):
    assert rt.custom.add("抽象一号", {"preset": "dizzy", "eye_l": {"open": 1.3, "pupil": "star"}, "eye_r": {"open": 0.2},
                                      "mouth": {"shape": "triangle", "open": 0.9}}, origin="experiment", style="abstract")[1] == "new"
    assert rt.custom.add("自然一号", {"preset": "calm", "eyes": {"open": 0.3, "tilt": -0.8}, "mouth": {"shape": "line"},
                                      "gaze": [0.9, 0.5]}, origin="experiment", style="natural")[1] == "new"
    rt.custom.protected = rt.creative.elites(rt.custom)
    assert set(rt.custom.protected) == {"抽象一号", "自然一号"}
    ok, why = rt.custom.retire("抽象一号", by="evolve")
    assert not ok and "风格里最好的作品" in why
    ok, _ = rt.custom.retire("抽象一号", by="user")
    assert ok


def test_memory_budget_notes_scenes(rt):
    mem = rt.creative
    assert mem.budget_left(1, 1000)[0]
    exp = mem.start({"style": "cute", "kind": "invent"})
    assert not mem.budget_left(1, 1000)[0]
    mem.add_iteration(exp, {"round": 1, "local_errors": ["x"]})
    mem.finish(exp, "abandoned", failure="太碎了", intent="试试")
    for i in range(30):
        mem.add_note(f"心得{i % 20}号：线要粗")
    assert len(mem.data["notes"]) == creative.MAX_NOTES
    ctx = mem.context()
    assert ctx["recent_experiments"][-1]["failure"] == "太碎了" and ctx["recent_experiments"][-1]["last_errors"] == ["x"]
    name, st = mem.add_scene("流星雨", {"mode": 3, "ops": [{"type": "effect", "effect": "rain", "duration_ms": 1200}]}, "abstract", "下流星")
    assert st == "new" and "流星雨" in creative.SCENES
    from pet_ascii.inline import parse_tag

    assert parse_tag("<face happy scene=流星雨 idle=keep after=1>")["scene"] == "流星雨"
    assert "scene" not in parse_tag("<face happy scene=没有这个 idle=keep after=1>")
    assert rt.play_scene("流星雨") and not rt.play_scene("没有这个")


# ---------------------------------------------------------------------- 后台视觉评价
class FakeVisionClient:
    def __init__(self, text=None, error=None, delay=0.0):
        self.text = text or json.dumps({"reads_as": "惊喜", "intent_match": 0.8, "clarity": 0.6, "appeal": 0.9,
                                        "problems": ["嘴太小"], "suggestions": ["线再粗一点"]}, ensure_ascii=False)
        self.error, self.delay, self.calls = error, delay, []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        time.sleep(self.delay)
        if self.error:
            raise RuntimeError(self.error)
        return {"text": self.text, "usage": {"total_tokens": 321}, "model": "fake-vl"}


def _critic(cfg, client, provider=True):
    from pet_ascii.ai.vision import VisionCritic
    from pet_ascii.metrics import Latency

    return VisionCritic(cfg, client_factory=lambda p: client, metrics=Latency(),
                        provider={"model": "fake-vl", "base_url": "x", "api_key": "k", "source": "测试"} if provider else {})


def test_vision_critic_async_budget_and_parse(rt):
    cfg = rt.cfg
    client = FakeVisionClient()
    critic = _critic(cfg, client)
    task = {"images": {"ascii_focus": b"\x89PNG fake", "ascii_face": b"\x89PNG fake"}, "intent": "惊喜", "style": "quirky"}
    done = []
    job = critic.submit(task, on_done=lambda j: done.append(j.result))
    assert job is not None and job.wait(5)["score"] == pytest.approx((0.8 + 0.6 + 0.9) / 3, abs=1e-3)
    time.sleep(0.05)
    assert done and done[0]["problems"] == ["嘴太小"] and critic.budget["requests"] == 1 and critic.budget["tokens"] == 321
    content = client.calls[0][1]["content"]
    assert [c["type"] for c in content] == ["text", "image_url", "image_url"]       # 特写 + 缩略图
    assert "视觉评价（后台）" in critic.metrics.stats()
    cfg.vision_daily_requests = 1
    assert critic.submit(task) is None                                # 每日上限
    critic.stop()


def test_vision_prompt_matches_what_is_shown():
    from pet_ascii.ai import vision

    face = vision.build_messages({"images": {"ascii_focus": b"x"}, "kind": "face"})[0]["content"]
    scene = vision.build_messages({"images": {"ascii_focus": b"x"}, "kind": "scene"})[0]["content"]
    assert "角色的脸" in face and "角色的脸" not in scene and "自由演出" in scene and "角色本身被隐藏" in scene
    assert "这段演出的画面实际看起来是什么" in scene        # 不把抽象演出预设成“一张脸”（实测会让评审硬看出脸来）


def test_vision_critic_cancel_pending_and_unsupported(rt):
    cfg = rt.cfg
    slow = FakeVisionClient(delay=0.6)
    critic = _critic(cfg, slow)
    cfg.vision_max_pending = 2
    j1 = critic.submit({"images": {}})
    j2 = critic.submit({"images": {}})
    assert critic.submit({"images": {}}) is None                      # 排队上限
    j2.cancel()
    j1.wait(3)
    time.sleep(0.8)
    assert j1.result is not None and j2.result is None and len(slow.calls) == 1   # 取消的任务没有发出请求
    critic.stop()
    bad = _critic(cfg, FakeVisionClient(error="HTTP 400: unknown variant `image_url`"))
    job = bad.submit({"images": {}})
    job.wait(3)
    time.sleep(0.05)
    assert job.result is None and not bad.can_submit()[0] and "不支持图片" in bad.can_submit()[1]
    bad.stop()
    none = _critic(cfg, FakeVisionClient(), provider=False)
    assert none.submit({"images": {}}) is None and "没有可用的视觉模型" in none.can_submit()[1]


# ---------------------------------------------------------------------- 自主视觉实验
class Scripted:
    """按顺序返回预设回答的假 DeepSeek，并记下收到的消息。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kw):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else '{"decision":"abandon","reason":"没有更多脚本"}'
        return {"text": text if isinstance(text, str) else json.dumps(text, ensure_ascii=False),
                "usage": {"total_tokens": 1000}, "model": "fake"}


def _ctx(rt, vision=None):
    from pet_ascii.ai.experiment import ExperimentContext

    return ExperimentContext("测试娘", rt.engine.pose.rig, rt.custom, rt.parts, rt.creative, vision=vision,
                             engine_factory=rt._offline_engine, canvas=(rt.engine.W, rt.engine.H), vision_wait_s=5)


def test_experiment_fixes_deterministic_error_then_adopts(rt):
    from pet_ascii.ai import experiment

    design = {"intent": "被夸到眼冒星星", "name": "星星眼花", "emotion": "被夸奖", "components": [{"name": "星芒眼", **TINY_EYE}],
              "spec": {"preset": "happy", "parts": {"eye_l": "星芒眼", "eye_r": "星芒眼"}}, "notes": "眼睛换成星星"}
    review1 = {"decision": "adopt", "reason": "我觉得挺好", "changes": {"components": [{"name": "星芒眼", **STAR_EYE}]},
               "self_note": "眼睛部件要画满节点框"}
    review2 = {"decision": "adopt", "reason": "这次看得清了"}
    client = Scripted([design, review1, review2])
    rep = experiment.run(client, _ctx(rt), {"style": "quirky", "kind": "invent", "reason": "测试"}, max_iters=3, thinking=False)
    assert rep["outcome"] == "adopted" and len(rep["rounds"]) == 2
    assert rep["rounds"][0]["errors"] and rep["rounds"][0].get("overridden")       # 第一轮有确定性错误：不许采用
    assert rt.parts.items["星芒眼"]["status"] == "active" and "星芒眼" in fr.PARTS
    assert rep["adopted"]["expression"] == "星星眼花" and rt.custom.items["星星眼花"]["style"] == "quirky"
    assert rt.custom.items["星星眼花"]["spec"]["parts"] == {"eye_l": "星芒眼", "eye_r": "星芒眼"}
    exp = rt.creative.data["experiments"][-1]
    assert exp["status"] == "adopted" and exp["iterations"][0]["local_errors"] and exp["intent"] == "被夸到眼冒星星"
    assert any(n["text"] == "眼睛部件要画满节点框" for n in rt.creative.data["notes"])
    # 复查时她看到的是本地真实渲染的检查结果
    review_payload = json.loads(client.calls[1][1]["content"])
    assert review_payload["local_check"]["errors"] and "looks" in review_payload and review_payload["round"] == "1/3"
    chars, _ = rt.engine.pose.rig.render(fr.normalize({"preset": "星星眼花"}))
    assert chars                                                        # 正式库里的表情能直接渲染


def test_experiment_abandon_records_failure_and_no_library_change(rt):
    from pet_ascii.ai import experiment

    design = {"intent": "扭曲的不安", "name": "扭扭脸", "skeleton_ops": [{"op": "move", "node": "mouth", "dy": -0.45}],
              "spec": {"preset": "worried"}}
    client = Scripted([design, {"decision": "abandon", "reason": "嘴跑到眼睛上面了，这个方向不对"}])
    rep = experiment.run(client, _ctx(rt), {"style": "abstract", "kind": "skeleton"}, max_iters=3, thinking=False)
    assert rep["outcome"] == "abandoned" and "这个方向不对" in rep["failure"]
    assert "扭扭脸" not in rt.custom.items and not rt.parts.active()
    assert rt.creative.data["styles"]["abstract"] == {"tries": 1, "adopted": 0, "quality": 0.0}


def test_experiment_uses_vision_only_for_high_risk_and_passes_feedback(rt):
    from pet_ascii.ai import experiment

    vclient = FakeVisionClient()
    critic = _critic(rt.cfg, vclient)
    design = {"intent": "锯齿状的咬牙切齿", "name": "咬牙", "components": [{"name": "锯齿嘴", **ZIG}],
              "spec": {"preset": "angry", "parts": {"mouth": "锯齿嘴"}}}
    client = Scripted([design, {"decision": "adopt", "reason": "评审也觉得清楚"}])
    rep = experiment.run(client, _ctx(rt, vision=critic), {"style": "exaggerated", "kind": "invent"}, max_iters=2,
                         thinking=False)
    assert rep["outcome"] == "adopted" and len(vclient.calls) == 1
    payload = json.loads(client.calls[1][1]["content"])
    assert payload["vision"]["reads_as"] == "惊喜"                     # 视觉评价交给她参考
    time.sleep(0.1)
    assert rt.creative.data["experiments"][-1]["iterations"][0]["vision"]["summary"]
    assert rt.parts.items["锯齿嘴"]["vision"]                           # 也记到组件上
    # 只调参数、没有新部件 / 骨架变化：不请视觉模型
    plain = Scripted([{"intent": "淡淡的", "name": "淡淡笑", "spec": {"preset": "smile", "eyes": {"open": 0.5}}},
                      {"decision": "adopt", "reason": "好"}])
    rt.custom.add("眯眼坏笑", {"preset": "grin", "eyes": {"open": 0.4}}, note="使坏")
    rep = experiment.run(plain, _ctx(rt, vision=critic), {"style": "natural", "kind": "remix"}, max_iters=2, thinking=False)
    assert len(vclient.calls) == 1
    critic.stop()
    # 重做旧作：设计请求里带着可重做的部件和表情（以前这里 KeyError 'spec'，remix 每次都直接失败）
    assert rep["outcome"] == "adopted", rep
    pool = json.loads(plain.calls[0][1]["content"])["remix_pool"]
    assert {"name": "锯齿嘴", "kind": "component"}.items() <= pool[0].items() and pool[0]["spec"]["slot"] == "mouth"
    assert any(p["kind"] == "expression" and p["name"] == "眯眼坏笑" and p["spec"] for p in pool)


def test_experiment_scene_free_ascii_mode(rt):
    from pet_ascii.ai import experiment

    rows = [("*" * 30).center(rt.engine.W)] * 8
    scene = {"mode": 3, "intent": "星屑", "ops": [{"type": "full_scene", "frames": [{"rows": rows, "color": "#ffe27a", "ms": 400},
                                                                                 {"rows": rows[::-1], "color": "#ff8cc0", "ms": 400}],
                                                  "hide": ["body", "face"], "then": "restore"},
                                                 {"type": "effect", "effect": "sparkle", "duration_ms": 1200}]}
    client = Scripted([{"intent": "开心到变成星屑", "name": "星屑散开", "scene": scene}, {"decision": "adopt", "reason": "好看"}])
    rep = experiment.run(client, _ctx(rt), {"style": "abstract", "kind": "scene"}, max_iters=2, thinking=False)
    assert rep["outcome"] == "adopted" and rep["adopted"]["scene"] == "星屑散开" and "星屑散开" in creative.SCENES
    payload = json.loads(client.calls[1][1]["content"])
    assert payload["local_check"]["metrics"]["duration_s"] > 0.5 and payload["local_check"]["metrics"]["peak_change"] > 0.05


def test_experiment_cancel(rt):
    from pet_ascii.ai import experiment

    ev = threading.Event()
    ev.set()
    client = Scripted([{"intent": "x", "name": "取消测试", "spec": {"preset": "smile"}}])
    rep = experiment.run(client, _ctx(rt), {"style": "cute", "kind": "invent"}, cancel=ev, thinking=False)
    assert rep["outcome"] == "cancelled" and "取消测试" not in rt.custom.items


def test_runtime_experiment_scheduling_and_latency(rt):
    from PySide6.QtWidgets import QApplication

    calls = []
    rt.experiment_now = lambda *a, **k: calls.append(1) or True
    rt.maybe_experiment()
    assert not calls                                                    # 刚有互动：不做
    rt.sleep.last_activity -= 11 * 60
    rt.maybe_experiment()
    assert calls == [1]
    rt.creative.data["experiments"].append({"time": time.time(), "iterations": [], "style": "cute", "kind": "invent"})
    rt.maybe_experiment()
    assert calls == [1]                                                 # 间隔未到
    rt.cfg.experiments = False
    rt.creative.data["experiments"].clear()
    rt.maybe_experiment()
    assert calls == [1]
    # 内联标签 → 开始播放 的延迟被记录
    rt.note_tag("<face happy>")
    rt.apply_inline_tag("<face happy>")
    assert "内联标签 → 开始播放" in rt.latency.stats()


def test_runtime_experiment_end_to_end_in_background(rt):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.runtime import Runtime

    design = {"intent": "猫一样的狡黠", "name": "猫咪狡黠", "components": [{"name": "猫瞳", **STAR_EYE}],
              "spec": {"preset": "smug", "parts": {"eye_l": "猫瞳", "eye_r": "猫瞳"}}}
    client = Scripted([design, {"decision": "adopt", "reason": "好"}])
    rt.cfg.vision_enabled = False
    assert Runtime.experiment_now(rt, style="cute", kind="invent", client=client)
    assert not Runtime.experiment_now(rt, client=client)                  # 同时只跑一个
    t0 = time.time()
    while rt._experimenting and time.time() - t0 < 30:
        QApplication.processEvents()
        time.sleep(0.02)
    assert rt.last_experiment["outcome"] == "adopted" and "猫咪狡黠" in rt.custom.items
    assert "创作实验（后台）" in rt.latency.stats()
