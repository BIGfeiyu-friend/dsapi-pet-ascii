"""表情库保存：后台合并写盘、只重新序列化变了的条目、紧凑格式、退出前写完。"""
import json
import time

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.custom_presets import CustomPresets

SNICKER = {"preset": "shy", "eyes": {"smile": 0.6, "open": 0.7}, "mouth": {"shape": "cat", "curve": 0.6}, "blush": 0.9}
GLEE = {"preset": "cat", "eyes": {"smile": 0.6}, "mouth": {"shape": "cat", "curve": 0.5}, "blush": 0.9, "gaze": [0.5, 0.3]}


@pytest.fixture
def lib(tmp_path):
    lib = CustomPresets(tmp_path / "lib.json")
    yield lib
    dispose(lib)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def wait_file(path, cond, timeout=4.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if cond(json.loads(path.read_text(encoding="utf-8"))):
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    return False


def test_structural_change_saved_in_background_soon(lib):
    lib.add("憋笑", SNICKER)
    assert wait_file(lib.path, lambda d: "憋笑" in d["presets"])           # 不用 flush，半秒左右后台写好


def test_usage_counts_are_batched_and_cheap(lib):
    lib.add("憋笑", SNICKER)
    lib.flush()
    mtime = lib.path.stat().st_mtime_ns
    t0 = time.perf_counter()
    for _ in range(200):
        lib.touch("憋笑")
        lib.touch("happy")
    per_call_ms = (time.perf_counter() - t0) * 1000 / 400
    assert per_call_ms < 2                                                 # 调用方几乎不花时间
    time.sleep(0.8)
    assert lib.path.stat().st_mtime_ns == mtime                            # 使用次数要攒一会儿才写
    lib.flush()
    data = json.loads(lib.path.read_text(encoding="utf-8"))
    assert data["presets"]["憋笑"]["uses"] == 200 and data["builtin"]["happy"]["uses"] == 200


def test_incremental_saves_stay_consistent(lib):
    lib.add("憋笑", SNICKER)
    lib.add("偷乐", GLEE)
    lib.flush()                                     # 全量
    lib.touch("偷乐")
    lib.rate("憋笑", +1, "好可爱")
    lib.flush()                                     # 只重新序列化两条
    lib.retire("偷乐", "测试")                      # 结构变化：全量
    lib.touch("憋笑")
    lib.flush()
    lib.rename("憋笑", "忍笑")
    lib.flush()
    fresh = CustomPresets(lib.path)
    assert set(fresh.items) == {"忍笑"} and set(fresh.retired) == {"偷乐"}
    assert fresh.items["忍笑"]["likes"] == 1 and fresh.items["忍笑"]["uses"] == 1
    assert fresh.retired["偷乐"]["uses"] == 1 and fresh.aliases == {"憋笑": "忍笑"}
    assert fresh.feedback_log[-1]["context"] == "好可爱"
    assert fresh.items["忍笑"]["spec"] == lib.items["忍笑"]["spec"]


def test_compact_file_and_old_format_still_loads(lib, tmp_path):
    lib.add("憋笑", SNICKER)
    lib.revise("憋笑", {"eyes": {"open": 0.4333333333}, "gaze": [0.123456789, 0.5]})
    lib.flush()
    text = lib.path.read_text(encoding="utf-8")
    assert "\n" not in text and ": " not in text                           # 不缩进、不加空格
    assert "0.4333333333" not in text and "0.123456789" not in text        # 参数保留 3 位小数
    old = tmp_path / "old.json"                                             # 以前带缩进的旧文件照样能读
    old.write_text(json.dumps(json.loads(text), ensure_ascii=False, indent=1), encoding="utf-8")
    assert "憋笑" in CustomPresets(old).items


def test_runtime_stop_flushes(tmp_path, monkeypatch, disposer):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    rt = disposer(Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False))
    rt.apply_inline_tag("<face happy>")
    rt.apply_inline_tag("<face happy>")
    rt.stop()                                                             # 退出：没写的使用次数立刻写完
    data = json.loads((tmp_path / "custom_presets.json").read_text(encoding="utf-8"))
    assert data["builtin"]["happy"]["uses"] == 2
    fr.CUSTOM = {}
    fr.OVERRIDES = {}
