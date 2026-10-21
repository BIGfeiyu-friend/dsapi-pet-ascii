from pet_ascii.ai import mock
from pet_ascii.cache import AnimCache, similarity
from pet_ascii.config import AsciiConfig, data_dir
from pet_ascii.persist import Budget, StateStore
from pet_ascii.runtime import intent_from_reply


def test_cache_save_find_load(tmp_path):
    c = AnimCache(tmp_path / "c")
    raw = mock.generate("高兴地打招呼", {"face_region": {"w": 20, "h": 8}}, seed=1)
    e = c.save("高兴地打招呼", raw, {"face": "idle:20x8"})
    assert c.load(e["id"]) == raw
    assert c.find("开心地打招呼呀", face_key="idle:20x8")
    assert c.find("开心地打招呼呀", face_key="idle:30x9") is None     # 面部尺寸不同不复用
    assert similarity("变成一团乱码", "完全不相关的话") < 0.2
    c2 = AnimCache(tmp_path / "c")
    assert c2.recent(1)[0]["id"] == e["id"]


def test_budget_limits_and_day_roll():
    cfg = AsciiConfig()
    cfg.daily_request_limit, cfg.daily_token_limit = 2, 1000
    b = Budget()
    b.add(400)
    assert b.check(cfg)[0]
    b.add(700)
    ok, why = b.check(cfg)
    assert not ok and "上限" in why
    b.day = "2000-01-01"
    assert b.check(cfg)[0] and b.requests == 0 and b.total_requests == 2


def test_state_store_roundtrip():
    p = data_dir() / "state_rt.json"
    s = StateStore(p)
    s.budget.add(123)
    s.save({"pose": "idle", "expression": "开心"}, force=True)
    s2 = StateStore(p)
    assert s2.visual()["expression"] == "开心" and s2.budget.tokens == 123


def test_intent_from_reply_prefers_stage_directions():
    intent, ctx = intent_from_reply("（听到窗外的雨声，把脸贴到玻璃上看了看）呜哇，下雨了！要不要一起听雨声")
    assert intent.startswith("听到") and "呜哇" in intent
    intent2, _ = intent_from_reply("今天天气真好。我们出去玩吧")
    assert "今天天气真好" in intent2
