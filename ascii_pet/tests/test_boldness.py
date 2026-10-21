"""变化幅度：在字符画上衡量每次修改/新建，太小就自动放大，放大后仍不够的修改不采用并请模型重做。"""
import json

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.ai import evolve
from pet_ascii.custom_presets import CustomPresets

SNICKER = {"preset": "shy", "eyes": {"smile": 0.6, "open": 0.7}, "mouth": {"shape": "cat", "curve": 0.6}, "blush": 0.9}


@pytest.fixture(scope="module")
def rig():
    from pet_ascii.character import find_character

    return find_character("maid_bust").poses["idle"].rig


@pytest.fixture
def lib(tmp_path, rig):
    lib = CustomPresets(tmp_path / "lib.json")
    lib.rig, lib.min_change, lib.max_gain = rig, 0.10, 3.0
    yield lib
    dispose(lib)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def test_amplify_pushes_small_changes_further(lib, rig):
    base = fr.normalize({"preset": "happy"})
    target = fr.normalize({"preset": "happy", "eyes": {"smile": 0.8, "open": 1.05}, "gaze": [0.15, 0.0]})
    raw = rig.visible_diff(base, target)
    spec, diff, gain = lib.amplify(base, target)
    assert gain > 1 and diff > raw
    assert spec["eye_l"]["smile"] < target["eye_l"]["smile"]          # 沿着原来的方向推得更远
    big = fr.normalize({"preset": "love"})
    assert lib.amplify(base, big)[2] == 1.0                           # 本来就够大的不动
    lib.rig = None
    assert lib.amplify(base, target) == (target, None, 1.0)           # 没有骨架：原样返回


def test_revise_too_small_is_rejected_and_bold_one_recorded(lib):
    lib.add("憋笑", SNICKER)
    before = lib.items["憋笑"]["spec"]
    status, detail = lib.revise("憋笑", {"blush": 1.0, "sweat": 0.7})   # 只改脸红和汗：字符画上几乎看不出
    assert status == "too_small" and "没有采用" in detail
    assert lib.items["憋笑"]["spec"] == before and not lib.items["憋笑"]["history"]
    status, detail = lib.revise("憋笑", {"eyes": {"open": 0.5, "tilt": 0.5}, "gaze": [0.6, 0.2]}, reason="更狡黠")
    assert status == "revised" and "变化" in detail
    assert lib.change_of("憋笑")["diff"] >= 0.075


def test_create_is_pushed_away_from_nearest_or_merged(lib, rig):
    name, status = lib.add("偷看", {"preset": "happy", "eyes": {"smile": 0.6, "open": 1.0}, "gaze": [0.3, 0.1]})
    assert status == "new"
    ch = lib.change_of("偷看")
    assert ch["diff"] >= rig.visible_diff(lib._spec_of(ch["vs"]), fr.normalize(
        {"preset": "happy", "eyes": {"smile": 0.6, "open": 1.0}, "gaze": [0.3, 0.1]}))
    # 只在泪光/汗上不同、放大后仍几乎看不出来：并入最像的表情，不新增
    name, status = lib.add("微汗", {"preset": "happy", "eyes": {"smile": 1.0, "tear": 0.4}, "sweat": 0.5})
    assert status == "merged" and "微汗" not in lib.items


class FakeDesigner:
    def __init__(self, design, check):
        self.calls, self.design, self.check = [], design, check

    def complete(self, messages, max_tokens=0, temperature=0, timeout=0, extra=None):
        self.calls.append(messages)
        obj = self.check if "照镜子看它们" in messages[0]["content"] else self.design
        return {"text": json.dumps(obj, ensure_ascii=False), "usage": {"total_tokens": 100}, "model": "fake"}


def test_evolve_asks_for_bolder_redo(lib, rig):
    lib.add("憋笑", SNICKER)
    design = {"thoughts": "憋笑的脸红可以再多一点", "actions": [
        {"op": "revise", "name": "憋笑", "reason": "更害羞", "spec": {"blush": 1.0, "sweat": 0.5}}]}
    check = {"checks": [{"name": "憋笑", "ok": False, "why": "要改眼睛才看得出",
                         "spec": {"eye_l": {"smile": 1.0}, "eye_r": {"open": 0.4, "tilt": 0.6}, "gaze": [-0.7, 0.3]}}]}
    fake = FakeDesigner(design, check)
    rep = evolve.run(fake, "深深", [{"user": "嗨", "pet": "嗨", "face": "<face 憋笑>"}], lib, rig=rig, check=True)
    assert rep["results"][0]["status"] == "too_small"
    assert "10%" in fake.calls[0][0]["content"] and "10%" in fake.calls[1][0]["content"]
    designs = json.loads(fake.calls[1][1]["content"])["designs"]
    assert designs[0]["name"] == "憋笑" and "没有采用" in designs[0]["problem"]
    assert rep["checks"][0]["status"] == "revised" and rep["checks"][0]["change"]
    assert lib.items["憋笑"]["last_by"] == "mirror" and lib.change_of("憋笑")["diff"] >= 0.075
