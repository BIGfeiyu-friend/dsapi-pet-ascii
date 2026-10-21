"""手绘差分的连续变形（morph_face.py，v0.8）：原像素沿笔画脊柱搬运，眼睛 / 眉毛 / 嘴连续可调。"""
import os
import time

import pytest

from pet_ascii import face_rig as fr
from pet_ascii import morph_face as mf
from pet_ascii.character import find_character
from pet_ascii.drawn_face import eye_state

FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "characters", "face_layers")


@pytest.fixture(scope="module")
def face():
    f = mf.MorphFace.load(FOLDER)
    assert f is not None
    return f


@pytest.fixture(scope="module")
def rig():
    return find_character("maid_bust").pose("idle").rig


def _iou(a, b, thr=100):
    from PIL import ImageChops

    A = a.getchannel("A").point(lambda v: 255 if v > thr else 0)
    B = b.getchannel("A").point(lambda v: 255 if v > thr else 0)
    inter = ImageChops.multiply(A, B).histogram()[255]
    union = ImageChops.lighter(A, B).histogram()[255]
    return inter / max(1, union)


def test_precomputed_strokes_load_fast_and_complete(face):
    assert os.path.exists(os.path.join(FOLDER, "strokes.json"))
    t = time.perf_counter()
    again = mf.MorphFace.load(FOLDER)
    assert time.perf_counter() - t < 1.0                              # 读预计算，不现算
    for f in (face, again):
        assert all(e is not None and set(e.keys) >= set(mf.STATES) for e in f.eyes)
        assert all(b is not None for b in f.brows) and f.mouth is not None and f.closed is not None


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("state,open_,smile", [("wide", 1.3, 0), ("open", 1.0, 0), ("half", 0.55, 0), ("closed", 0.0, 0),
                                               ("smile", 1.0, 1.0)])
def test_key_drawings_are_reproduced(face, side, state, open_, smile):
    """参数正好落在某张手绘上时，画出来的就是那张手绘（原像素，不是重画）。"""
    import json

    er = face.eyes[side]
    lines, _, _ = er.layers(open_, smile)
    m = json.load(open(os.path.join(FOLDER, "manifest.json"), encoding="utf-8"))
    ref = mf._on_canvas(FOLDER, m["states"][state][f"eye_{er.side}"], er.box)
    assert _iou(lines, ref) > 0.9, (state, er.side)


def test_right_eye_spines_start_at_inner_corner(face):
    """右眼的外眼角在右边：上眼睑脊柱从左（内眼角）走到右（外眼角）再往下，和左眼镜像对应（眼褶连在一起时也不例外）。"""
    for st in mf.STATES:
        pts = face.eyes[1].keys[st]["upper"]["pts"]
        corner = pts[mf.K_ARC - 1]
        assert corner[0] >= max(p[0] for p in pts) - 3 and pts[0][0] < corner[0] - 20, st
        lpts = face.eyes[0].keys[st]["upper"]["pts"]
        assert lpts[mf.K_ARC - 1][0] <= min(p[0] for p in lpts) + 3, st


def test_openness_is_continuous(face):
    """睁眼程度从 1.3 扫到 0：每一档都和上一档不同（不再只有 3 档），眼白越来越少。"""
    from PIL import ImageChops

    er = face.eyes[0]
    prev, areas, distinct = None, [], 0
    for i in range(14):
        lines, mask, _ = er.layers(max(0.0, 1.3 - i * 0.1), 0.0)
        areas.append(mask.point(lambda v: 255 if v > 128 else 0).histogram()[255])
        if prev is not None and ImageChops.difference(prev.getchannel("A"), lines.getchannel("A")).getbbox():
            distinct += 1
        prev = lines
    assert distinct >= 12
    assert areas[-1] == 0 and areas[0] > areas[6] > areas[10]


def test_handover_has_no_long_ghosting():
    """形状连续混合，像素只在交接点附近很窄一段交叉淡化。"""
    assert mf.pixel_weights({"open": 0.75, "smile": 0.25}) == {"open": 1.0}
    w = mf.pixel_weights({"open": 0.52, "smile": 0.48})
    assert set(w) == {"open", "smile"} and abs(sum(w.values()) - 1) < 1e-6
    assert mf.pixel_weights({"half": 0.61, "closed": 0.39}) == {"half": 1.0}


def test_new_eye_controls_change_the_drawing(face):
    from PIL import ImageChops

    er = face.eyes[0]
    base, _, _ = er.layers(0.8, 0.0)
    for kw in ({"droop": 0.8}, {"droop": -0.8}, {"lower": 0.8}):
        other, _, _ = er.layers(0.8, 0.0, kw.get("droop", 0.0), kw.get("lower", 0.0))
        assert ImageChops.difference(base.getchannel("A"), other.getchannel("A")).getbbox() is not None, kw


def test_mouth_closed_open_and_teeth(face):
    mo, cm = face.mouth, face.closed
    hs = []
    for k in (0.3, 0.6, 1.0, 1.4):
        bb = mo.layer(1.0, k, 0.3).getchannel("A").getbbox()
        hs.append(bb[3] - bb[1])
    assert hs == sorted(hs) and hs[-1] > hs[0] + 10                   # 越张越大
    closed = cm.layer(1.0, 0.0)
    smile, frown = cm.layer(1.0, 0.8), cm.layer(1.0, -0.8)
    assert closed.getbbox() and smile.getbbox() and frown.getbbox()
    plain = mo.layer(1.0, 0.8, 0.5)
    teeth = mo.layer(1.0, 0.8, 0.5, inner="teeth")
    whites = lambda im: sum(1 for c in im.getdata() if c[3] > 200 and min(c[:3]) > 235)  # noqa: E731
    assert whites(teeth) > whites(plain) + 20


def test_spec_has_new_parameters():
    s = fr.normalize({"eyes": {"droop": 3, "lower": -1}, "mouth": {"shape": "normal", "skew": 0.5}})
    assert s["eye_l"]["droop"] == 1.0 and s["eye_l"]["lower"] == 0.0 and s["mouth"]["skew"] == 0.5
    assert fr.describe(s)["mouth"]["skew"] == 0.5
    a, b = fr.neutral(), fr.normalize({"eyes": {"droop": 1.0}})
    assert fr.lerp_spec(a, b, 0.5)["eye_l"]["droop"] == pytest.approx(0.5)
    assert fr.spec_key(a) != fr.spec_key(b)
    assert fr.normalize({"preset": "sleepy"})["eye_l"]["droop"] > 0
    assert "耷拉" in fr.mirror_text(fr.normalize({"eyes": {"droop": 0.8}}))
    from pet_ascii.inline import parse_tag

    t = parse_tag("<face new=无奈 from=bored droop=0.7 l.lower=0.5 mskew=-0.6>")
    spec = t["new"]["spec"]
    assert spec["eyes"]["droop"] == 0.7 and spec["eye_l"]["lower"] == 0.5 and spec["mouth"]["skew"] == -0.6


def test_rig_uses_morph_and_is_fast_when_cached(rig):
    assert fr.EYE_STYLE == "morph" and rig.drawn.morph is not None
    specs = [fr.normalize({"eyes": {"open": o / 10}}) for o in range(0, 13, 3)] + [fr.normalize({"preset": p}) for p in
                                                                                   ("happy", "grin", "sleepy", "smug")]
    for s in specs:
        rig.draw(s)
    t = time.perf_counter()
    for s in specs:
        rig.draw(s)
    assert (time.perf_counter() - t) / len(specs) < 0.03               # 五官层命中缓存后每张脸几毫秒
    assert eye_state(specs[0]["eye_l"]) == "closed"


def test_blink_passes_through_half_closed():
    from pet_ascii.engine import Engine, EngineSettings

    eng = Engine(find_character("maid_bust"), EngineSettings(idle_blink=False, idle_glance=False, code_rows=False), seed=1)
    eng.tick(0.05)
    eng.blink_start, eng.blink_until = eng.t, eng.t + 0.13
    seen = []
    for _ in range(14):
        eng.t += 0.01
        seen.append(eng._displayed_spec()["eye_l"]["open"])
    assert min(seen) == pytest.approx(0.05) and any(0.2 < v < 0.95 for v in seen)
