import json

import pytest

from pet_ascii.protocol import LIMITS, Context, ProtocolError, extract_json, parse_animation

CTX = Context(canvas_w=60, canvas_h=40, face_w=10, face_h=4, poses=("idle", "wave"))


def test_extract_json_tolerates_fences_and_chatter():
    text = '好的！下面是动画：\n```json\n{"mode":2,"ops":[{"type":"effect","effect":"flash"}]}\n```\n希望你喜欢'
    assert extract_json(text)["mode"] == 2
    with pytest.raises(ProtocolError):
        extract_json("完全没有 JSON")


def test_face_patch_is_cropped_and_sanitized():
    data = {"mode": 2, "ops": [{"type": "face_patch", "palette": {"a": "#ff0000"},
                                "frames": [{"rows": ["0123456789ABCDEF", "你好", "x", "y", "z", "TOO MANY ROWS"],
                                            "colors": ["aaaaaaaaaaaaaaaa"], "ms": 99999}]}]}
    anim = parse_animation(data, CTX)
    f = anim.ops[0].frames[0]
    assert len(f.rows) == 4 and len(f.rows[0]) == 10           # 裁剪到面部区域
    assert f.rows[1] == "??"                                    # 宽字符被替换
    assert f.ms == LIMITS["ms_max"]                             # 时长被钳制
    assert f.colors[0][0] == 0xFFFF0000
    assert any("超过区域高度" in w for w in anim.warnings)


def test_unknown_and_invalid_ops_are_dropped_not_fatal():
    data = {"ops": [{"type": "exec", "code": "import os"}, {"type": "effect", "effect": "nuke"},
                    {"type": "parameter_animation", "params": {"jump": 99, "gaze": [5, -5], "evil": 1}}]}
    anim = parse_animation(data, CTX)
    assert [op.type for op in anim.ops] == ["parameter_animation"]
    p = anim.ops[0].params
    assert p["jump"] == 8 and p["gaze_x"] == 1 and p["gaze_y"] == -1
    assert "evil" not in p


def test_no_valid_ops_raises():
    with pytest.raises(ProtocolError):
        parse_animation({"ops": [{"type": "face_patch", "frames": []}]}, CTX)
    with pytest.raises(ProtocolError):
        parse_animation({"mode": 2}, CTX)


def test_frame_sequence_and_delta_normalization():
    data = {"ops": [
        {"type": "frame_sequence", "target": "face", "frames": [{"rows": ["ab"]}, {"delta": [{"x": 1, "y": 0, "text": "Z"}]}]},
        {"type": "frame_sequence", "target": "region", "anchor": "body", "x": 3, "frames": [["..", ".."]]},
        {"type": "delta_update", "changes": [{"x": 0, "y": 0, "text": "^_^", "colors": "aaa"}], "palette": {"a": "#0f0"}},
    ]}
    anim = parse_animation(data, CTX)
    assert [op.type for op in anim.ops] == ["face_patch", "region_patch", "delta_update"]
    assert anim.ops[0].frames[1].delta[0]["text"] == "Z"
    assert anim.ops[2].frames[0].delta[0]["colors"][0] == 0xFF00FF00


def test_limits_on_ops_and_unknown_pose():
    ops = [{"type": "effect", "effect": "flash"}] * (LIMITS["ops"] + 5)
    anim = parse_animation({"ops": ops, "end_state": {"pose": "nonexistent"}}, CTX)
    assert len(anim.ops) == LIMITS["ops"]
    assert "pose" not in anim.end_state
    anim = parse_animation({"ops": [{"type": "parameter_animation", "params": {"pose": "dance"}}] + ops[:1]}, CTX)
    assert "pose" not in anim.ops[0].params if anim.ops[0].type == "parameter_animation" else True


def test_string_input_roundtrip():
    raw = json.dumps({"mode": 3, "think": "I AM 你 GLITCH", "ops": [
        {"type": "full_scene", "rules": [{"effect": "dissolve"}], "then": "restore"}]})
    anim = parse_animation(raw, CTX)
    assert anim.mode == 3 and anim.ops[0].hide == ["body", "face"]
    assert anim.think == "I AM   GLITCH"     # think 只保留 ASCII
