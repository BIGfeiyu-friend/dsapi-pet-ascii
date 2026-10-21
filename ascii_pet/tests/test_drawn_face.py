"""手绘表情差分驱动的五官（drawn_face.py + assets/characters/face_layers）。

这里测的是分档切换（EYE_STYLE=layers，v0.7.1，连续变形不可用时的退路）；连续变形见 test_morph_face.py。"""
import pytest

from pet_ascii import face_rig as fr
from pet_ascii.character import find_character
from pet_ascii.drawn_face import eye_state


@pytest.fixture(scope="module")
def rig():
    return find_character("maid_bust").pose("idle").rig


@pytest.fixture(autouse=True)
def stepped(monkeypatch):
    monkeypatch.setattr(fr, "EYE_STYLE", "layers")


def test_assets_loaded_for_all_states(rig):
    df = rig.drawn
    assert df is not None
    for state in ("open", "half", "wide", "closed", "smile"):
        for side in "lr":
            assert df.parts.get((state, f"eye_{side}")) is not None, (state, side)
            assert df.parts.get((state, f"brow_{side}")) is not None, (state, side)
    for state in ("open", "half", "wide"):
        assert df.parts.get((state, "sclera_l")) and df.parts.get((state, "sclera_r"))
    assert set(df.mouths) >= {"dot", "o", "open"}


@pytest.mark.parametrize("preset,state", [("neutral", "open"), ("sleepy", "half"), ("surprised", "wide"), ("happy", "smile"),
                                          ("closed", "closed"), ("shocked", "wide"), ("angry", "half"), ("cat", "smile"),
                                          ("thinking", "open")])
def test_parameters_pick_the_hand_drawn_state(preset, state):
    assert eye_state(fr.normalize({"preset": preset})["eye_l"]) == state


def test_dot_pupil_with_half_closed_eyes_stays_half_open():
    """瞳孔缩成点但眼睛半闭（困、发呆，例如她自创的困倦表情）：用半睁那张，不变成瞪大的眼睛。"""
    sleepy_dot = fr.normalize({"eyes": {"open": 0.4, "pupil": "dot"}})
    assert eye_state(sleepy_dot["eye_l"]) == "half"
    assert eye_state(fr.normalize({"preset": "shocked"})["eye_l"]) == "wide"


def _eye_layer(rig, spec):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", rig.blank.size, (0, 0, 0, 0))
    assert rig.drawn.draw_eye(img, 0, spec["eye_l"], spec["gaze"])
    return img


def _blue_centroid(img):
    px = img.load()
    xs = ys = n = 0
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 200 and b > r + 40 and b > 120:          # 手绘虹膜的浅蓝月牙
                xs += x
                ys += y
                n += 1
    return xs / max(1, n), ys / max(1, n), n


def test_gaze_moves_only_the_hand_drawn_iris(rig):
    base = _blue_centroid(_eye_layer(rig, fr.normalize({})))
    right = _blue_centroid(_eye_layer(rig, fr.normalize({"gaze": [0.6, 0.0]})))
    down = _blue_centroid(_eye_layer(rig, fr.normalize({"gaze": [0.0, 0.6]})))
    assert base[2] > 30 and right[0] > base[0] + 5 and down[1] > base[1] + 3
    # 眼眶线稿不动：比较两张图里纯黑笔触在虹膜外的部分
    from PIL import ImageChops

    a = _eye_layer(rig, fr.normalize({})).getchannel("A")
    b = _eye_layer(rig, fr.normalize({"gaze": [0.6, 0.0]})).getchannel("A")
    assert ImageChops.difference(a, b).point(lambda v: 255 if v > 60 else 0).histogram()[255] < 400


def test_iris_is_clipped_to_the_eye_white(rig):
    from PIL import ImageChops

    df = rig.drawn
    img = _eye_layer(rig, fr.normalize({"preset": "sleepy", "gaze": [0.0, -0.8]}))   # 半睁 + 往上看：虹膜上半部分被眼睑挡住
    sclera = df.parts[("half", "sclera_l")]
    from PIL import Image

    m = Image.new("L", img.size, 0)
    m.paste(sclera["img"].getchannel("A"), (int(sclera["x"]), int(sclera["y"])))
    lines = df.parts[("half", "eye_l")]
    lm = Image.new("L", img.size, 0)
    lm.paste(lines["img"].getchannel("A"), (int(lines["x"]), int(lines["y"])))
    blue = Image.new("L", img.size, 0)
    px, bp = img.load(), blue.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a > 200 and b > r + 40:
                bp[x, y] = 255
    outside = ImageChops.multiply(blue, ImageChops.invert(ImageChops.lighter(m, lm)))
    assert outside.histogram()[255] == 0


def test_tilt_rotates_the_drawn_eye_and_x_falls_back(rig):
    from PIL import ImageChops

    a = _eye_layer(rig, fr.normalize({"preset": "neutral"}))
    b = _eye_layer(rig, fr.normalize({"eyes": {"tilt": -0.8}}))
    assert ImageChops.difference(a.getchannel("A"), b.getchannel("A")).getbbox() is not None
    from PIL import Image

    img = Image.new("RGBA", rig.blank.size)
    assert not rig.drawn.draw_eye(img, 0, fr.normalize({"preset": "dead"})["eye_l"], [0, 0])     # × 叉叉眼：程序画


def test_mouth_rules(rig):
    from PIL import Image

    df = rig.drawn
    img = Image.new("RGBA", rig.blank.size)
    assert df.draw_mouth(img, fr.normalize({"preset": "happy"})["mouth"])          # 笑着张嘴：手绘
    assert not df.draw_mouth(img, fr.normalize({"preset": "angry"})["mouth"])      # 生气撇嘴：不用笑着的嘴
    assert df.draw_mouth(img, fr.normalize({"preset": "surprised"})["mouth"])      # O 形嘴：手绘
    assert df.draw_mouth(img, fr.neutral()["mouth"])                               # 原画的小嘴
    assert not df.draw_mouth(img, fr.normalize({"preset": "dizzy"})["mouth"])      # 波浪嘴还没有手绘


def _mouth_top(rig, spec):
    from PIL import Image

    img = Image.new("RGBA", rig.blank.size, (0, 0, 0, 0))
    assert rig.drawn.draw_mouth(img, spec["mouth"], (spec["eye_l"], spec["eye_r"]))
    return img.getchannel("A").getbbox()


def test_open_mouth_moves_down_when_eyes_are_open(rig):
    """笑着张开的嘴是配笑眼 ^ 画的：咧嘴笑 / 说话（睁着眼）时往下挪，不贴着眼睛；开心（笑眼）时保持原画位置。"""
    df = rig.drawn
    assert 10 <= df.open_drop <= 20
    happy = _mouth_top(rig, fr.normalize({"preset": "happy"}))
    assert happy[1] == int(round(df.mouths["open"]["y"]))                       # 笑眼：原画位置
    grin = fr.normalize({"preset": "grin"})
    g = _mouth_top(rig, grin)
    assert g[1] - happy[1] >= df.open_drop - 2
    eye_bottom = max(df.parts[("open", f"eye_{s}")]["y"] + df.parts[("open", f"eye_{s}")]["img"].height for s in "lr")
    assert g[1] - eye_bottom >= 20                                               # 和下眼睑拉开距离
    blink = fr.normalize({"eyes": {"open": 0.0}}, grin)                          # 咧嘴笑时眨眼：嘴不跳
    assert _mouth_top(rig, blink)[1] == g[1]
    wide = _mouth_top(rig, fr.normalize({"mouth": {"shape": "normal", "open": 1.0, "curve": 0.3}}))
    assert wide[3] - g[3] < 24                                                   # 张得再大也只往下长一半，不压到下巴


def test_style_switch_back_to_procedural(rig, monkeypatch):
    monkeypatch.setattr(fr, "EYE_STYLE", "drawn")
    assert rig.drawn is None
    rig.draw(fr.normalize({"preset": "happy"}))
