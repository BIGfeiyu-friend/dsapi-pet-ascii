from pet_ascii.character import builtin_character, find_character
from pet_ascii.engine import Engine, EngineSettings
from pet_ascii.grid import CLEAR
from pet_ascii.protocol import parse_animation


def make(char=None):
    eng = Engine(char or builtin_character(), EngineSettings(idle_blink=False, idle_glance=False, code_rows=False),
                 seed=1)
    eng.params["bob"] = 0
    return eng


def frame(eng):
    c, k, w, h, _ = eng.frame()
    return c, k, w, h


def run(eng, seconds, dt=0.05):
    for _ in range(int(seconds / dt)):
        eng.tick(dt)


def test_face_patch_changes_only_face_region():
    eng = make()
    before, _, w, h = frame(eng)
    fx, fy, fw, fh = eng.face_rect()
    rows = ["X" * fw] * fh
    eng.apply(parse_animation({"ops": [{"type": "face_patch", "frames": [{"rows": rows, "ms": 200}]}]}, eng.context()))
    eng.tick(0.01)
    after, _, _, _ = frame(eng)
    for y in range(h):
        for x in range(w):
            inside = fx <= x < fx + fw and fy <= y < fy + fh
            if inside:
                assert after[y * w + x] == "X"
            else:
                assert after[y * w + x] == before[y * w + x], (x, y)


def test_hold_last_persists_and_neutral_restores():
    eng = make()
    fw, fh = eng.face.w, eng.face.h
    eng.apply(parse_animation({"ops": [{"type": "face_patch", "hold": "last",
                                        "frames": [{"rows": ["A" * fw] * fh, "ms": 100}, {"rows": ["B" * fw] * fh, "ms": 100}]}]},
                              eng.context()))
    run(eng, 0.5)
    assert eng.face_track is None and eng.face.get(0, 0)[0] == "B"
    eng.apply(parse_animation({"ops": [{"type": "face_patch", "hold": "neutral", "frames": [{"rows": ["C" * fw] * fh, "ms": 100}]}]},
                              eng.context()))
    run(eng, 0.4)
    assert eng.face.get(0, 0)[0] == eng.pose.neutral.get(0, 0)[0]


def test_delta_update_touches_only_given_cells():
    eng = make()
    snap = list(eng.face.chars)
    eng.apply(parse_animation({"ops": [{"type": "delta_update", "layer": "face",
                                        "changes": [{"x": 1, "y": 0, "text": "QQ"}]}]}, eng.context()))
    run(eng, 0.5)
    changed = [i for i, (a, b) in enumerate(zip(snap, eng.face.chars)) if a != b]
    assert changed == [1, 2]
    assert eng.face.chars[1] == "Q"


def test_full_scene_hides_body_then_restores():
    eng = make()
    base, _, w, h = frame(eng)
    scene_rows = ["S" * 4]
    eng.apply(parse_animation({"mode": 3, "ops": [{"type": "full_scene", "hide": ["body", "face"], "then": "restore",
                                                    "duration_ms": 400, "frames": [{"rows": scene_rows, "ms": 400}]}]},
                              eng.context()))
    eng.tick(0.05)
    during, _, _, _ = frame(eng)
    assert during[:4] == ["S"] * 4
    assert sum(1 for c in during if c not in (CLEAR,)) == 4      # 身体与脸被隐藏
    run(eng, 0.6)
    after, _, _, _ = frame(eng)
    assert after == base


def test_dissolve_effect_hides_then_reassembles():
    eng = make()
    base, _, w, h = frame(eng)
    eng.apply(parse_animation({"mode": 3, "ops": [{"type": "effect", "effect": "dissolve", "duration_ms": 600}]}, eng.context()))
    run(eng, 0.3)
    mid, _, _, _ = frame(eng)
    assert mid != base
    run(eng, 0.5)
    after, _, _, _ = frame(eng)
    assert after == base


def test_region_patch_restore_leaves_no_residue():
    eng = make()
    base, _, _, _ = frame(eng)
    eng.apply(parse_animation({"ops": [{"type": "region_patch", "anchor": "canvas", "x": 0, "y": 0, "hold": "restore",
                                        "frames": [{"rows": ["RRR"], "ms": 200}]}]}, eng.context()))
    eng.tick(0.05)
    mid, _, _, _ = frame(eng)
    assert mid[:3] == ["R", "R", "R"]
    run(eng, 0.4)
    assert frame(eng)[0] == base


def test_interrupted_animation_leaves_consistent_state():
    eng = make()
    fw, fh = eng.face.w, eng.face.h
    loop = {"ops": [{"type": "face_patch", "loop": "loop", "frames": [{"rows": ["L" * fw] * fh, "ms": 100}]},
                    {"type": "parameter_animation", "duration_ms": 2000, "params": {"jump": 5, "shake": 3}}],
            "expression": "looping"}
    eng.apply(parse_animation(loop, eng.context()))
    run(eng, 0.3)
    eng.apply(parse_animation({"ops": [{"type": "face_patch", "frames": [{"rows": ["N" * fw] * fh, "ms": 100}]}],
                               "expression": "new"}, eng.context()))
    run(eng, 3.0)
    assert eng.face.get(0, 0)[0] == "N"
    assert eng.recent[-2:] == ["looping", "new"]
    assert eng.jump is None


def test_state_export_import_roundtrip():
    eng = make()
    fw, fh = eng.face.w, eng.face.h
    eng.apply(parse_animation({"ops": [{"type": "face_patch", "palette": {"a": "#ff00aa"},
                                        "frames": [{"rows": ["@" * fw] * fh, "colors": ["a" * fw] * fh, "ms": 80}]}],
                               "expression": "smug"}, eng.context()))
    run(eng, 0.3)
    st = eng.export_state()
    eng2 = make()
    eng2.import_state(st)
    assert eng2.face.chars == eng.face.chars
    assert eng2.face.colors[0] & 0xF0F0F0 == 0xF000A0
    assert eng2.expression == "smug" and eng2.recent == ["smug"]


def test_real_character_visual_state_is_compact():
    eng = Engine(find_character("shenshen"), seed=2)
    st = eng.visual_state()
    assert st["face_region"]["w"] >= 10 and len(st["face"]["rows"]) == st["face_region"]["h"]
    import json
    assert len(json.dumps(st, ensure_ascii=False)) < 3000
