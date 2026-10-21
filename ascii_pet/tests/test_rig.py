import pytest

from pet_ascii import face_rig as fr
from pet_ascii.ai import mock
from pet_ascii.character import find_character
from pet_ascii.engine import Engine, EngineSettings
from pet_ascii.inline import TagFilter, guess_preset, parse_tag, tag_to_animation
from pet_ascii.protocol import parse_animation


@pytest.fixture(scope="module")
def maid():
    ch = find_character("maid_bust")
    assert ch.poses["idle"].rig_info is not None, "maid_bust 素材缺失面部骨架"
    return ch


def make(ch):
    eng = Engine(ch, EngineSettings(idle_blink=False, idle_glance=False, code_rows=False), seed=1)
    return eng


def frame(eng):
    c, k, w, h, _ = eng.frame()
    return c, w, h


# ---------------------------------------------------------------------- 规格
def test_normalize_clamps_and_layers_presets():
    s = fr.normalize({"preset": "happy", "blush": 9, "mouth": {"open": -3, "shape": "nope"}, "eyes": {"pupil": "laser"}})
    assert s["blush"] == 1.0
    assert s["mouth"]["open"] == 0.0 and s["mouth"]["shape"] == "normal"
    assert s["eye_l"]["smile"] == 1.0 and s["eye_l"]["pupil"] == "art"
    n = fr.neutral()
    assert fr.describe(n) == {}
    half = fr.normalize({"preset": "surprised", "level": 0.5})
    assert 1.0 < half["eye_l"]["open"] < fr.normalize({"preset": "surprised"})["eye_l"]["open"]


def test_incremental_frames_keep_previous_values():
    a = fr.normalize({"preset": "sad"})
    b = fr.normalize({"mouth": {"curve": 0.5}}, a)
    assert b["eye_l"] == a["eye_l"] and b["mouth"]["curve"] == 0.5


def test_every_preset_renders_inside_face_box(maid):
    rig = maid.poses["idle"].rig
    for name in fr.PRESETS:
        chars, colors = rig.render(fr.normalize({"preset": name}))
        assert len(chars) == rig.fh and all(len(r) == rig.fw for r in chars), name
        assert all(c is not None for row in colors for c in row), name


def _neutral_diff(rig):
    from PIL import ImageChops

    a = rig.draw(fr.neutral())
    diff = ImageChops.difference(a.convert("RGB"), rig.art.convert("RGB")).convert("L")
    out = []
    for part in list(rig.cal["eyes"]) + [rig.cal["mouth"]]:
        x0, y0 = rig.p(*part["region"][:2])
        x1, y1 = rig.p(*part["region"][2:])
        hist = diff.crop((x0, y0, x1, y1)).histogram()
        out.append(sum(i * c for i, c in enumerate(hist)) / max(1, sum(hist)))
    return out


def test_neutral_uses_original_art_pixels(maid, monkeypatch):
    """程序绘制（没有手绘差分时的退路）：平静表情直接贴原画的五官像素。"""
    rig = maid.poses["idle"].rig
    monkeypatch.setattr(fr, "EYE_STYLE", "drawn")
    # 在眼睛/嘴巴区域内应与原画几乎一致（区域外两张原图本身的头发排线略有不同，不比较）
    for mean in _neutral_diff(rig):
        assert mean < 12, mean


def test_neutral_with_hand_drawn_layers_matches_original(maid):
    """手绘差分（默认）：平静表情由画师的分层线稿 + 眼白 + 虹膜拼成，和原画很接近（线条细节略有出入）。"""
    rig = maid.poses["idle"].rig
    assert rig.drawn is not None
    for mean in _neutral_diff(rig):
        assert mean < 25, mean


# ---------------------------------------------------------------------- 引擎
def test_face_op_only_changes_face_rect(maid):
    eng = make(maid)
    before, w, h = frame(eng)
    fx, fy, fw, fh = eng.face_rect()
    eng.apply(parse_animation({"ops": [{"type": "face", "frames": [{"preset": "laugh", "ms": 300}], "blend_ms": 0}]},
                              eng.context()))
    for _ in range(10):
        eng.tick(0.05)
    after, _, _ = frame(eng)
    changed_inside = 0
    for y in range(h):
        for x in range(w):
            i = y * w + x
            inside = fx <= x < fx + fw and fy <= y < fy + fh
            if not inside:
                assert after[i] == before[i], (x, y)
            elif after[i] != before[i]:
                changed_inside += 1
    assert changed_inside > 20


def test_expression_track_blends_and_holds(maid):
    eng = make(maid)
    eng.apply(parse_animation({"ops": [{"type": "face", "blend_ms": 400, "frames": [
        {"preset": "surprised", "ms": 500}, {"preset": "happy", "ms": 500}]}]}, eng.context()))
    eng.tick(0.1)
    mid = eng.current_face_spec()["eye_l"]["open"]
    assert 1.0 < mid < fr.normalize({"preset": "surprised"})["eye_l"]["open"]
    for _ in range(30):
        eng.tick(0.05)
    assert eng.expr_track is None
    assert eng.face_spec["eye_l"]["smile"] == 1.0          # hold=last
    assert eng.current is None


def test_blink_and_talk_modify_displayed_spec_only(maid):
    eng = make(maid)
    eng.set_expression({"preset": "smile"}, 0)
    eng.tick(0.05)
    eng.blink_until = eng.t + 0.2
    eng.talk(0.5)
    d = eng._displayed_spec()
    assert d["eye_l"]["open"] < 0.1 and d["mouth"]["open"] >= 0.15
    assert eng.face_spec["eye_l"]["open"] == 1.0 or eng.current_face_spec()["eye_l"]["open"] == 1.0
    frame(eng)


def test_rig_state_roundtrip(maid):
    eng = make(maid)
    eng.set_expression({"preset": "pout"}, 0)
    for _ in range(5):
        eng.tick(0.05)
    st = eng.export_state()
    eng2 = make(maid)
    eng2.import_state(st)
    assert fr.spec_key(eng2.face_spec) == fr.spec_key(eng.face_spec)


def test_visual_state_is_small_for_rig(maid):
    import json

    eng = make(maid)
    st = eng.visual_state()
    assert st["face_region"]["rig"] is True
    assert len(json.dumps(st, ensure_ascii=False)) < 900


def test_mock_generates_valid_rig_animations(maid):
    eng = make(maid)
    for intent in ("高兴地打招呼", "突然感到困惑", "变成一团乱码", "害羞地说谢谢"):
        anim = parse_animation(mock.generate(intent, eng.visual_state(), seed=3), eng.context())
        assert any(op.type == "face" for op in anim.ops), intent
        eng.apply(anim)
        for _ in range(20):
            eng.tick(0.1)
            frame(eng)


# ---------------------------------------------------------------------- 内联标签
def test_tag_filter_strips_tag_split_across_chunks():
    f = TagFilter()
    out = "".join(f.feed(c) for c in ["<fa", "ce happy le", "vel=0.8 fx=hearts>", "\n（笑）你好", "呀"])
    out += f.flush()
    assert out == "（笑）你好呀"
    assert f.pop_tags() == ["<face happy level=0.8 fx=hearts>"]


def test_tag_filter_passes_normal_text_and_lone_brackets():
    f = TagFilter()
    out = "".join(f.feed(c) for c in ["a < b 并且", " <font> 不是标签", "<"]) + f.flush()
    assert out == "a < b 并且 <font> 不是标签<"
    assert f.pop_tags() == []


def test_tag_filter_gives_up_on_unclosed_garbage():
    f = TagFilter()
    out = f.feed("<face " + "x" * 400) + f.flush()
    assert out.startswith("<face ") and not f.pop_tags()


def test_parse_tag_and_animation(maid):
    t = parse_tag("<face shy level=0.7 blush=0.9 look=left act=nod fx=hearts+sweat then=smile>")
    assert t["preset"] == "shy" and t["look"] == "left" and t["act"] == "nod" and t["fx"] == ["hearts", "sweat"]
    assert parse_tag("<face banana>")["preset"] == "neutral"
    eng = make(maid)
    anim = parse_animation(tag_to_animation(t), eng.context())
    kinds = [op.type for op in anim.ops]
    assert kinds[0] == "face" and "effect" in kinds and "parameter_animation" in kinds
    assert anim.ops[0].frames[0].spec["sweat"] == 0.8
    eng.apply(anim)
    for _ in range(50):
        eng.tick(0.1)
    assert eng.face_spec["eye_l"]["smile"] == pytest.approx(fr.normalize({"preset": "smile"})["eye_l"]["smile"])


def test_guess_preset_fallback():
    assert guess_preset("哈哈哈太好笑了") == "laugh"
    assert guess_preset("呜呜我好难过") in ("cry", "sad")
    assert guess_preset("今天天气不错。") == "smile"
