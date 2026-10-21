"""虚拟房间（v1.0）：生活循环（什么时候进去、一步一步做、主人来了就出来）、无聊与联想、房间记忆的整理。"""
import json
import random

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.room import GIFT_DIR


class Scripted:
    """按顺序返回准备好的输出；输出可以是函数（拿到这次的消息，返回 dict / 文字）。"""

    def __init__(self, outputs, reasoning_tokens=200):
        self.outputs = list(outputs)
        self.calls = []
        self.rtok = reasoning_tokens

    def complete(self, messages, max_tokens=1800, temperature=0.9, timeout=45.0, extra=None):
        self.calls.append({"messages": messages, "extra": extra, "max_tokens": max_tokens})
        o = self.outputs.pop(0)
        if callable(o):
            o = o(messages)
        rt = self.rtok(len(self.calls)) if callable(self.rtok) else self.rtok
        return {"text": o if isinstance(o, str) else json.dumps(o, ensure_ascii=False),
                "usage": {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300,
                          "completion_tokens_details": {"reasoning_tokens": rt}},
                "finish_reason": "stop", "reasoning": "（思考）"}


def step(action, thought="随便想想", then="leave", **kw):
    return {"thought": thought, "feeling": "平静", "intensity": 0.3, "face": "微笑",
            "action": {"type": action, **kw}, "then": then, "remember": False}


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
    r = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    clock = [1_800_000_000.0]                      # 2027-01-15 08:00 左右（本地时间不重要，night() 在测试里单独替换）
    life = r.room
    life.clock = lambda: clock[0]
    life.rng = random.Random(3)
    life.night = lambda now=None: False
    life.autorun = True
    r.clock = clock
    yield r
    dispose(r)
    fr.CUSTOM = {}


def test_runtime_has_room_but_it_only_runs_in_the_pet(rt):
    assert rt.room is not None and rt.room.room.space.exists()
    rt.room.autorun = False
    rt.room.state["next_wake"] = 0
    rt.room.check()                                  # 不在桌宠里（测试台 / 独立窗口）：什么都不做
    assert not rt.room.busy and rt.room.room.tail_log(5) == []


def test_session_read_gift_then_write(rt, tmp_path):
    life = rt.room
    life.room.add_gift_text("今天是你来到这里的第一天。", "欢迎")
    life._take_inbox(rt.clock[0])
    gift = life.state["pending"][0]["path"]
    assert gift.startswith(GIFT_DIR + "/")
    seen = {}

    def second(messages):
        seen["prompt"] = messages[-1]["content"]
        return step("write", "想把这个写下来", path="日记.md", content="主人送了我一张便条。")

    client = Scripted([step("read", "这是什么？", then="stay", path=gift), second])
    assert life.enter(force=True, client=client, sync=True)
    assert not life.busy
    assert (life.room.space / "日记.md").read_text(encoding="utf-8") == "主人送了我一张便条。"
    assert "今天是你来到这里的第一天。" in seen["prompt"]               # 读到的内容进了下一步
    assert "主人送的" in client.calls[0]["messages"][-1]["content"]      # 第一步就看到了新礼物
    assert client.calls[0]["extra"]["thinking"] == {"type": "enabled"}
    types = [e["type"] for e in life.room.tail_log(10)]
    assert types == ["gift", "enter", "step", "step", "leave"]
    assert life.state["pending"] == []                                  # 看过的礼物不再标“新的”
    assert len(life.memory.short) == 2 and life.memory.short[0]["user_related"]
    assert life.last_report["steps"] == 2 and life.last_report["tokens"] == 2600
    assert rt.store.budget.tokens >= 2600 and life.state["tokens_today"] == 2600
    assert life.state["next_wake"] > rt.clock[0] + 60
    assert not rt.engine.think_text.startswith("<ROOM>")                # 出来以后恢复原来的思考行


def test_user_chat_makes_her_leave_after_the_current_step(rt):
    life = rt.room

    def first(messages):
        rt.record_user("在吗？")                                         # 她正在想的时候主人发来消息
        return step("write", "写一点", then="stay", path="a.txt", content="一")

    client = Scripted([first, step("write", path="b.txt", content="二")])
    life.enter(force=True, client=client, sync=True)
    assert (life.room.space / "a.txt").exists() and not (life.room.space / "b.txt").exists()
    assert life.last_report["why"] == "主人来找我了"
    assert life.state["next_wake"] >= rt.clock[0] + rt.cfg.room_after_chat_min * 60


def test_errors_are_shown_to_her_and_she_can_try_again(rt):
    life = rt.room
    seen = {}

    def second(messages):
        seen["prompt"] = messages[-1]["content"]
        return step("nothing")

    client = Scripted([step("write", then="stay", path="../外面.txt", content="x"), second])
    life.enter(force=True, client=client, sync=True)
    ev = [e for e in life.room.tail_log(10) if e["type"] == "step"][0]
    assert not ev["ok"] and "出房间" in ev["error"]
    assert "没成功" in seen["prompt"]
    assert not (life.room.root.parent / "外面.txt").exists()


def test_unparsable_output_is_repaired_once(rt):
    life = rt.room
    client = Scripted(["我想想……", step("note", text="今天也很安静。")])
    life.enter(force=True, client=client, sync=True)
    assert len(client.calls) == 2 and client.calls[1]["extra"] == {"thinking": {"type": "disabled"}}
    notes = [e for e in life.room.tail_log(10) if e.get("action") == "note"]
    assert notes and notes[0]["result"]["text"] == "今天也很安静。"


def test_note_summary_marks_where_it_is_cut():
    from pet_ascii.room_life import _describe

    long = "主人～" + "谢谢你夸我画的表情，" * 8
    assert _describe("note", {"text": long}, {"text": long}, {}) == f"给主人留了便条：{long[:60]}…"   # 截断时加“…”
    assert _describe("note", {"text": "晚安"}, {"text": "晚安"}, {}) == "给主人留了便条：晚安"


def test_mock_client_does_not_enter(rt):
    from pet_ascii.ai.mock import MockClient

    life = rt.room
    life.enter(force=True, client=MockClient(), sync=True)
    assert "Mock" in life.last_report["error"] and not life.busy


def test_due_rules(rt):
    life, sl = rt.room, rt.sleep
    now = rt.clock[0]
    sl.clock = lambda: now
    sl.last_activity = now - 3600
    life.state["next_wake"] = now + 60
    assert life.due(now) == (False, "还没到时间")
    life.state["next_wake"] = now - 1
    assert life.due(now)[0]
    sl.last_activity = now - 30                                          # 主人刚刚还在
    assert not life.due(now)[0]
    sl.last_activity = now - 3600
    sl.state = "asleep"
    life.state["energy"] = 0.3
    assert life.due(now) == (False, "睡着了，精力还不够")
    life.state["energy"] = 0.9
    assert life.due(now)[0]
    life.state["wakes_today"] = rt.cfg.room_daily_wakes
    assert "次数" in life.due(now)[1]
    sl.state = "awake"


def test_night_waits_longer(rt):
    life = rt.room
    gaps = {}
    for night in (False, True):
        life.night = lambda now=None, n=night: n
        life.rng = random.Random(1)
        life.session = {"steps": [], "woke": False}
        life.busy = True
        life._on_done({"steps": 1, "tokens": 0, "why": "做完了"})
        gaps[night] = life.state["next_wake"] - rt.clock[0]
    assert gaps[True] == pytest.approx(gaps[False] * rt.cfg.room_night_factor)


def test_repeating_is_boring_but_similar_reactions_to_similar_things_less_so(rt):
    life = rt.room
    out = {"usage": {}, "reasoning_tokens": 100}
    from pet_ascii.ai.room_ai import parse_decision

    def do(content, stimulus, path="日记.md", action="append", thought="又想写日记了"):
        rt.clock[0] += 3600
        before = life.state["boredom"]
        d = parse_decision(step(action, thought, path=path, content=content, art=content))
        s = life.execute(d, rt.clock[0], stimulus, out, 0, 0)
        return life.state["boredom"] - before, s

    life.state["boredom"] = 0.3
    first, _ = do("今天下雨了，窗外很安静", "")
    rep_plain, s = do("今天下雨了，窗外很安静", "")                       # 什么新鲜事都没有，又写一样的
    assert rep_plain > first and s["echo"] is not None and s["echo"]["sim"] > 0.9
    # 对很相似的事情（又收到一张写着下雨的便条）做出相似的反应：涨得少
    life.memory.short[-1]["stimulus"] = "礼物 便条 今天下雨了"
    rep_similar, _ = do("今天下雨了，窗外很安静", "礼物 便条 今天下雨了")
    assert rep_similar < rep_plain
    novel, s = do("=^.^= 窗边睡觉的猫", "", path="画/猫.txt", action="draw", thought="突然想画画")   # 做点完全不同的：无聊下降
    assert novel < 0 and s["echo"] is None


def test_echo_and_association_show_up_in_what_she_sees(rt):
    life = rt.room
    from pet_ascii.ai.room_ai import parse_decision

    old_t = rt.clock[0] - 3 * 86400
    life.memory.add_short({"id": "r900", "ts": old_t, "kind": "draw", "what": "画了「画/猫.txt」：一只橘猫",
                           "thought": "想画一只猫", "response": "draw 画/猫.txt 橘猫", "stimulus": ""})
    life.room.add_gift_text("一只橘猫在窗台上晒太阳", "照片说明")
    life._take_inbox(rt.clock[0])
    gift = life.state["pending"][0]["path"]
    life.session = {"woke": False}
    text, stim = life.situation(rt.clock[0], [])
    assert "联想" not in text                                           # 只看到文件名：还想不到猫
    read = life.execute(parse_decision(step("read", path=gift, then="stay")), rt.clock[0], stim, {"usage": {}}, 0, 0)
    text, stim = life.situation(rt.clock[0], [read])
    assert "晒太阳" in stim and "联想" in text and "想画一只猫" in text   # 读到橘猫：想起三天前画过的猫
    d = parse_decision(step("draw", "想画一只猫", path="画/猫.txt", art="一只橘猫"))
    s = life.execute(d, rt.clock[0], stim, {"usage": {}}, 0, 1)
    assert s["echo"] and s["echo"]["id"] == "r900"
    text2, _ = life.situation(rt.clock[0], [read, s])
    assert "你注意到" in text2
    life.session = None


def test_outside_changes_and_folder_gifts_are_noticed_but_not_her_own(rt):
    life = rt.room
    life._notice_changes(rt.clock[0])                                     # 第一次：只记下现在的样子
    from pet_ascii.ai.room_ai import parse_decision

    life.execute(parse_decision(step("write", path="我的.txt", content="我写的")), rt.clock[0], "", {"usage": {}}, 0, 0)
    (life.room.space / GIFT_DIR / "直接放的.txt").write_text("hi", encoding="utf-8")
    (life.room.space / "主人放的.txt").write_text("hello", encoding="utf-8")
    life._notice_changes(rt.clock[0])
    pend = life.state["pending"]
    gifts = [p for p in pend if p["kind"] == "gift"]
    changes = [c for p in pend if p["kind"] == "change" for c in p["changes"]]
    assert [g["path"] for g in gifts] == [f"{GIFT_DIR}/直接放的.txt"]
    assert changes == [{"path": "主人放的.txt", "change": "多了"}]       # 她自己写的不算
    text, _ = life.situation(rt.clock[0], [])
    assert "不是你做的" in text


def test_woken_from_sleep_keeps_the_owners_idle_time(rt):
    life, sl = rt.room, rt.sleep
    sl.state = "asleep"
    pre = sl.last_activity = rt.clock[0] - 40 * 60
    life.enter(force=True, client=Scripted([step("rest")]), sync=True)
    assert life.last_report["woke"]
    assert sl.last_activity <= pre + 1                                   # 她自己醒过来不算主人的互动


# ---------------------------------------------------------------------- 记忆
def test_think_signal_relative_to_same_kind(rt):
    mem = rt.room.memory
    assert mem.think_signal("write", 600) == pytest.approx(0.5)          # 样本少：按绝对量
    for n in (300, 320, 280, 310):
        mem.think_signal("write", n)
    assert mem.think_signal("write", 2000) > 0.9
    assert mem.think_signal("write", 50) < 0.4
    assert mem.think_signal("read", 2000) < 0.9                          # 别的动作单独统计


def test_consolidation_fates(rt):
    mem = rt.room.memory
    now = rt.clock[0]
    base = {"ts": now - 30 * 3600, "kind": "write", "what": "x", "thought": "", "feeling": ""}
    mem.add_short({**base, "id": "r1", "what": "第一次画了主人", "think": 0.9, "intensity": 0.9, "novelty": 0.9,
                   "user_related": True, "remember": True})
    mem.add_short({**base, "id": "r2", "what": "整理了文件夹", "think": 0.5, "intensity": 0.4, "novelty": 0.6})
    mem.add_short({**base, "id": "r3", "what": "发呆", "think": 0.1, "intensity": 0.1, "novelty": 0.1})
    mem.long.append({"id": "l9", "ts": now - 86400 * 9, "text": "我喜欢下雨天", "evidence": ["r0"], "weight": 0.6})
    mem.add_short({**base, "id": "r4", "what": "又写了下雨", "think": 0.9, "intensity": 0.8, "novelty": 0.5,
                   "remember": True, "user_related": True})
    j = {"r1": {"importance": 0.9, "long": "我第一次画了主人"}, "r2": {"importance": 0.4, "gist": "整理了文件夹"},
         "r3": {"importance": 0.05}, "r4": {"importance": 0.8, "long": "我又写了下雨", "merge_into": "l9"}}
    rep = mem.apply_consolidation(mem.due(now, 20), j, now)
    assert [x["text"] for x in rep["long"]] == ["我第一次画了主人"]
    assert [x["text"] for x in rep["gist"]] == ["整理了文件夹"]
    assert [x["from"] for x in rep["forgot"]] == ["r3"]
    l9 = next(m for m in mem.long if m["id"] == "l9")
    assert l9["text"] == "我喜欢下雨天" and "r4" in l9["evidence"]      # 合并只加强，不改写
    assert mem.short == [] and mem.counts()["forgotten"] == 1


def test_gists_fade_unless_recalled_and_long_has_a_cap(rt):
    mem = rt.room.memory
    now = rt.clock[0]
    mem.gists.extend([{"id": "g1", "ts": now - 9 * 86400, "text": "常被想起", "from": ["r1"], "until": now - 1,
                       "recalls": 2},
                      {"id": "g2", "ts": now - 9 * 86400, "text": "没人想起", "from": ["r2"], "until": now - 1,
                       "recalls": 0}])
    out = mem.expire(now, long_max=200)
    assert out["faded"] == ["g2"] and len(out["promoted"]) == 1 and mem.long[-1]["text"] == "常被想起"
    for i in range(5):
        mem.long.append({"id": f"lx{i}", "ts": now, "text": f"t{i}", "weight": 0.1 * i, "last": now})
    out = mem.expire(now, long_max=3)
    assert len(mem.long) == 3 and "lx0" in out["dropped_long"]


def test_consolidate_while_asleep_with_the_secondary_model(rt):
    life = rt.room
    now = rt.clock[0]
    life.memory.add_short({"id": "r5", "ts": now - 25 * 3600, "kind": "write", "what": "写了一首小诗",
                           "thought": "想写诗", "feeling": "开心", "think": 0.8, "intensity": 0.7, "novelty": 0.9,
                           "remember": True})
    rt.sleep.state = "awake"
    assert not life.maybe_consolidate(now)                                 # 醒着、不急：不整理
    rt.sleep.state = "asleep"
    judge = Scripted([{"items": [{"id": "r5", "importance": 0.8, "gist": "写了诗", "long": "我写了第一首诗"}]}])
    life.client_factory = lambda: judge
    assert life.maybe_consolidate(now, sync=True)
    assert judge.calls[0]["extra"] is None                                 # 整理不开思考（便宜）
    assert life.memory.long[-1]["text"] == "我写了第一首诗" and life.memory.long[-1]["evidence"] == ["r5"]
    ev = life.room.tail_log(3)[-1]
    assert ev["type"] == "memory" and ev["long"] == ["我写了第一首诗"]
    rt.sleep.state = "awake"


def test_room_memory_is_separate_from_pet_memory(rt, tmp_path):
    life = rt.room
    assert life.memory.path.parent == life.room.root
    assert "memory" not in str(life.room.root.relative_to(tmp_path)).split("/")[:1]


def test_first_visit_uses_last_dialogue_time_for_longing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    from pet_ascii.room_life import _last_chat_guess

    QApplication.instance() or QApplication([])
    now = 1_800_000_000.0
    from datetime import datetime

    t = datetime.fromtimestamp(now - 5 * 3600)
    rt = SimpleNamespace(dialogue=SimpleNamespace(rounds=[{"user": "晚安", "time": t.strftime("%m-%d %H:%M")}]))
    assert abs(_last_chat_guess(rt, now) - (now - 5 * 3600)) < 61
    assert _last_chat_guess(SimpleNamespace(dialogue=SimpleNamespace(rounds=[])), now) == now


# ---------------------------------------------------------------------- 什么时候再来（她自己决定）
def leave_with(rt, next_visit, energy=0.7):
    life = rt.room
    life.state["energy"] = energy
    out = step("write", "写完了", path="a.txt", content="一")
    if next_visit is not None:
        out["next_visit"] = next_visit
    life.enter(force=True, client=Scripted([out]), sync=True)
    return (life.state["next_wake"] - rt.clock[0]) / 60


def test_she_decides_when_to_come_back(rt):
    c = Scripted([step("nothing")])
    rt.room.enter(force=True, client=c, sync=True)
    assert "30~90" in c.calls[0]["messages"][0]["content"] and "next_visit" in c.calls[0]["messages"][0]["content"]
    assert leave_with(rt, 45) == pytest.approx(45)
    assert rt.room.state["next_by"] == "her" and rt.room.state["next_choice"] == 45
    assert rt.room.room.tail_log(1)[0]["next_visit"] == 45
    assert leave_with(rt, 5) == pytest.approx(30)                       # 限制在 30~90 分钟
    assert leave_with(rt, "两小时") == pytest.approx(90)
    assert rt.room.state["next_choice"] == 90


def test_sleep_until_morning_only_at_night(rt):
    from datetime import datetime

    life = rt.room
    life.night = lambda now=None: True
    minutes = leave_with(rt, "morning")
    wake = datetime.fromtimestamp(rt.clock[0] + minutes * 60)
    assert wake.hour == rt.cfg.room_night_end and wake.minute <= 30
    assert life.state["next_choice"] == "morning"
    life.night = lambda now=None: False
    assert leave_with(rt, "morning") == pytest.approx(90)               # 白天说“睡到天亮”：按最长的算


def test_fallback_stays_in_range_when_she_does_not_say(rt):
    life = rt.room
    for e in (0.0, 0.5, 1.0):
        m = leave_with(rt, None, energy=e)
        assert 30 - 1e-6 <= m <= 90 + 1e-6 and life.state["next_by"] == "auto"


def test_after_waking_from_sleep_she_picks_from_a_three_times_longer_range(rt):
    life, sl = rt.room, rt.sleep
    assert rt.cfg.room_asleep_interval_factor == 3.0
    assert life.visit_range(False) == (30, 90) and life.visit_range(True) == (90, 270)

    def visit(asleep, next_visit):
        sl.state = "asleep" if asleep else "awake"
        life.state["energy"] = 0.9
        out = step("nothing")
        if next_visit is not None:
            out["next_visit"] = next_visit
        c = Scripted([out])
        life.enter(force=True, client=c, sync=True)
        sl.state = "awake"
        return c.calls[0]["messages"][0]["content"], (life.state["next_wake"] - rt.clock[0]) / 60

    prompt, minutes = visit(True, 150)
    assert "90~270" in prompt and "倍" not in prompt.split("next_visit：")[1][:80]
    assert minutes == pytest.approx(150)                                # 她选多少就睡多少（没有暗中的倍数）
    assert visit(True, 40)[1] == pytest.approx(90)                      # 范围外：限制在 90~270
    assert visit(True, 500)[1] == pytest.approx(270)
    prompt, minutes = visit(False, 150)                                 # 醒着进的：还是 30~90
    assert "30~90" in prompt and minutes == pytest.approx(90)
    for _ in range(3):                                                  # 她没说：也在放长的范围里按精力定
        assert 90 - 1e-6 <= visit(True, None)[1] <= 270 + 1e-6


def test_factor_one_keeps_the_old_range(rt):
    rt.cfg.room_asleep_interval_factor = 1.0
    assert rt.room.visit_range(True) == (30, 90)


def test_parse_next_visit():
    from pet_ascii.ai.room_ai import parse_next_visit

    assert parse_next_visit(40) == 40.0 and parse_next_visit("50分钟") == 50.0
    assert parse_next_visit("1.5小时") == 90.0 and parse_next_visit("明早再来") == "morning"
    assert parse_next_visit(None) is None and parse_next_visit("随便") is None and parse_next_visit(True) is None
    assert parse_next_visit(-5) is None
    assert parse_next_visit("两小时") == 120.0 and parse_next_visit("四十五分钟") == 45.0
    assert parse_next_visit("一个半小时") == 90.0 and parse_next_visit("半个小时后") == 30.0
    assert parse_next_visit("十分钟") == 10.0


# ---------------------------------------------------------------------- 记得自己读过什么（v1.2.3）
def test_read_marks_replies_and_snippets(rt):
    import os
    import time as _t

    life = rt.room
    rt.clock[0] = _t.time()                                              # 和文件的修改时间对得上
    life.room.add_gift_text("送你一盆小绿萝，记得放在窗边晒太阳。", "便条")
    life._take_inbox(rt.clock[0])
    gift = life.state["pending"][0]["path"]
    c = Scripted([step("read", then="stay", path=gift), step("note", text="谢谢主人！我会好好布置的。")])
    life.enter(force=True, client=c, sync=True)
    looked = next(it for it in life.memory.short if it["kind"] == "read")
    assert looked["what"].endswith("：『送你一盆小绿萝，记得放在窗边晒太阳。』")         # 内容开头记进记忆
    text, _ = life.situation(rt.clock[0], [])
    line = next(l for l in text.splitlines() if gift.split("/", 1)[1] in l)
    assert "读过" in line and "回过便条" in line and "新的" not in line
    # 再读一次：次数；之后主人改了这个文件：标出来
    rt.clock[0] += 60
    life.enter(force=True, client=Scripted([step("read", path=gift), step("nothing")]), sync=True)
    p = life.room.space / gift
    p.write_text("改过了", encoding="utf-8")
    os.utime(p, (rt.clock[0] + 100, rt.clock[0] + 100))
    text, _ = life.situation(rt.clock[0] + 120, [])
    line = next(l for l in text.splitlines() if gift.split("/", 1)[1] in l)
    assert "一共 2 次" in line and "之后被改动过" in line
    # 搬走以后标记跟着走
    rt.clock[0] += 200
    life.enter(force=True, client=Scripted([step("move", path=gift, to="收藏")]), sync=True)
    marks = life.state["read_marks"]
    assert gift not in marks and any(k.startswith("收藏/") for k in marks)


def test_read_marks_are_backfilled_from_the_log(rt):
    from pet_ascii.room_life import RoomLife

    life = rt.room
    life.room.add_gift_text("便条内容", "便条")
    events, _ = life.room.read_inbox(0)
    path = events[0]["path"]
    for ev in ({"type": "enter"}, {"type": "step", "action": "read", "ok": True, "result": {"path": path}},
               {"type": "step", "action": "note", "ok": True, "result": {"text": "谢谢"}}, {"type": "leave"}):
        life.room.append_log(ev)
    life.state.data.pop("read_marks", None)
    life.state.data.pop("read_marks_backfilled", None)
    life.state.save()
    again = RoomLife(rt)
    m = again.state["read_marks"][path]
    assert m["count"] == 1 and m["replied"]["text"] == "谢谢"


def test_all_of_today_stays_in_the_recent_list(rt):
    from datetime import datetime

    life = rt.room
    now = datetime(2027, 1, 15, 22, 0).timestamp()
    yday = datetime(2027, 1, 14, 20, 0).timestamp()
    for i in range(10):
        life.memory.add_short({"id": f"y{i}", "ts": yday + i * 60, "kind": "write", "what": f"昨天的事{i}", "thought": ""})
    for i in range(25):
        life.memory.add_short({"id": f"t{i}", "ts": now - 20 * 3600 + i * 600, "kind": "write", "what": f"今天的事{i:02d}",
                               "thought": f"想法{i:02d}"})
    life.session = {"woke": False}
    text, _ = life.situation(now, [])
    life.session = None
    assert all(f"今天的事{i:02d}" in text for i in range(25))                 # 今天的一件不少
    assert "昨天的事9" in text and "昨天的事4" in text and "昨天的事3" not in text   # 昨天只留最后 6 件
    assert "想法24" in text and "想法17" in text and "想法16" not in text          # 最近 8 件带想法
