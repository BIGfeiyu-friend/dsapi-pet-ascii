import json
import threading
import time

import pytest
from conftest import dispose

from pet_ascii.ai.client import ClientError
from pet_ascii.ai.director import Director
from pet_ascii.cache import AnimCache
from pet_ascii.character import builtin_character
from pet_ascii.config import AsciiConfig, data_dir
from pet_ascii.engine import Engine, EngineSettings
from pet_ascii.persist import StateStore

GOOD = {"mode": 2, "intent": "ok", "expression": "开心", "ops": [{"type": "effect", "effect": "flash"}]}
_made: list = []      # setup() 建的调度器：每个测试结束时停掉后台线程


@pytest.fixture(autouse=True)
def _stop_directors():
    yield
    dispose(*_made)
    _made.clear()


class FakeClient:
    name = "fake"

    def __init__(self, outputs, delay=0.0):
        self.outputs = list(outputs)
        self.calls = []
        self.delay = delay

    def complete(self, messages, max_tokens=0, temperature=0, timeout=0):
        self.calls.append(messages)
        time.sleep(self.delay)
        out = self.outputs.pop(0) if self.outputs else json.dumps(GOOD)
        if isinstance(out, Exception):
            raise out
        return {"text": out, "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                "elapsed": self.delay, "model": "fake"}


def setup(outputs, delay=0.0, **cfgkw):
    cfg = AsciiConfig()
    cfg.min_interval_s = 0
    cfg.reuse_probability = 0.0
    for k, v in cfgkw.items():
        setattr(cfg, k, v)
    eng = Engine(builtin_character(), EngineSettings(), seed=3)
    tag = str(time.time_ns())
    store = StateStore(data_dir() / f"state_{tag}.json")
    cache = AnimCache(data_dir() / f"cache_{tag}")
    got = []
    ev = threading.Event()

    def deliver(res):
        got.append(res)
        ev.set()

    client = FakeClient(outputs, delay)
    d = Director(cfg, store, cache, lambda: (eng.visual_state(), eng.context()), deliver, client, "fake")
    _made.append(d)
    return d, client, got, ev, store, cache


def wait(got, n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and len(got) < n:
        time.sleep(0.02)
    return got


def test_success_is_delivered_cached_and_budgeted():
    d, client, got, ev, store, cache = setup([json.dumps(GOOD)])
    d.request("高兴地打招呼")
    wait(got)
    assert got and got[0].anim and not got[0].fallback
    assert store.budget.requests == 1 and store.budget.tokens == 150
    assert cache.find("高兴地打招呼")


def test_invalid_output_is_repaired_with_feedback():
    d, client, got, *_ = setup(["这不是 JSON", json.dumps(GOOD)])
    d.request("困惑")
    wait(got)
    assert got[0].anim and got[0].attempts == 2 and not got[0].fallback
    assert "无法通过校验" in client.calls[1][-1]["content"]


def test_double_failure_falls_back_locally():
    d, client, got, *_ = setup(["bad", "still bad"])
    d.request("高兴")
    wait(got)
    assert got[0].fallback and got[0].anim is not None
    assert "校验失败" in got[0].error


def test_network_error_falls_back():
    d, client, got, *_ = setup([ClientError("HTTP 401: bad key", 401, retryable=False)])
    d.request("难过")
    wait(got)
    assert got[0].fallback and "401" in got[0].error


def test_dedupe_and_cancel_stale():
    d, client, got, *_ = setup([json.dumps(GOOD)] * 5, delay=0.3)
    first = d.request("看看周围", priority=5)
    assert d.request("看看周围！", priority=5) is None          # 去重
    time.sleep(0.1)
    d.request("突然很开心", priority=6)                         # 取消正在进行的旧请求
    wait(got, 1, timeout=3)
    time.sleep(0.5)
    assert first.cancelled
    assert [r.request.intent for r in got] == ["突然很开心"]


def test_budget_exhausted_uses_cache_or_local():
    d, client, got, store, *_ = setup([], daily_request_limit=0)
    d.request("高兴地跳起来")
    wait(got)
    assert got[0].fallback and not client.calls


def test_mode_restriction_strips_full_scene():
    out = json.dumps({"mode": 3, "ops": [{"type": "full_scene", "rules": [{"effect": "dissolve"}]},
                                         {"type": "effect", "effect": "sparkle"}]})
    d, client, got, *_ = setup([out])
    d.request("轻轻笑一下", modes=(1, 2))
    wait(got)
    ops = [op.type for op in got[0].anim.ops]
    assert ops == ["effect"] and got[0].anim.mode == 2
