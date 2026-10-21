"""睡眠模式：无互动 30 分钟后入睡，睡眠动画即时编排（不重复播片），在对话里发消息会随机播放一个吵醒动画。"""
import random

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii import sleep as slp
from pet_ascii.protocol import LIMITS, parse_animation


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
    cfg.chat_link = "inline"
    r = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    clock = [1000.0]
    r.sleep.clock = lambda: clock[0]
    r.sleep.rng = random.Random(7)
    r.sleep.activity()
    r.clock = clock
    r.anims = []                                     # 睡眠控制器发出的每段动画（原始 JSON）
    orig = r.apply_json

    def apply_json(d):
        r.anims.append(d)
        return orig(d)

    r.apply_json = apply_json
    yield r
    dispose(r)
    fr.CUSTOM = {}


def run(rt, sec, dt=0.1):
    for _ in range(int(sec / dt)):
        rt.engine.tick(dt)
        rt.sleep.tick()


def asleep(rt, variant=None):
    rt.sleep.fall_asleep(variant)
    run(rt, 14)
    assert rt.sleep.state == "asleep"


def test_falls_asleep_after_30_idle_minutes(rt):
    sl = rt.sleep
    rt.clock[0] += 29 * 60
    assert not sl.check() and sl.state == "awake"
    rt.record_user("还在吗")                          # 有互动：重新计时
    rt.clock[0] += 29 * 60
    assert not sl.check()
    rt.clock[0] += 2 * 60
    assert sl.check() and sl.state == "falling" and sl.status_text() == "犯困…"
    run(rt, 14)
    assert sl.state == "asleep" and rt.current_expression() == slp.SLEEP_EXPR
    assert sl.night is not None and sl.zzz is not None
    spec = rt.engine.face_spec
    assert spec["eye_l"]["open"] < 0.16 and spec["eye_r"]["open"] < 0.16      # 闭着眼


def test_sleep_can_be_disabled_and_threshold_configured(rt):
    rt.cfg.sleep_enabled = False
    rt.clock[0] += 3 * 3600
    assert not rt.sleep.check()
    rt.cfg.sleep_enabled = True
    rt.cfg.sleep_after_min = 5
    rt.sleep.activity()
    rt.clock[0] += 4 * 60
    assert not rt.sleep.check()
    rt.clock[0] += 2 * 60
    assert rt.sleep.check()


def test_drag_and_menu_postpone_sleep_but_do_not_wake(rt):
    rt.clock[0] += 29 * 60
    rt.note_activity()
    rt.clock[0] += 29 * 60
    assert not rt.sleep.check()
    asleep(rt)
    rt.note_activity()
    assert rt.sleep.state == "asleep"


def test_sleep_beats_are_composed_not_looped(rt):
    asleep(rt)
    run(rt, 600)
    hist = rt.sleep.history
    assert rt.sleep.beats >= 40
    assert len({h["mood"] for h in hist}) >= 2                # 梦境情绪会换
    assert len({h["breaths"] for h in hist}) >= 2             # 每拍的呼吸数不同
    assert len({h["ms"] for h in hist}) == len(hist)          # 每拍时长都不同（没有两段一模一样）
    beats = [a for a in rt.anims if a["intent"] == "睡眠"]
    keys = {str([(f["eyes"]["open"], f["mouth"]["open"], f["blush"], f["ms"]) for op in a["ops"] if op["type"] == "face"
                 for f in op["frames"]]) for a in beats}
    assert len(keys) == len(beats)


def test_sleep_events_happen_over_time(rt):
    seen = set()
    for seed in range(6):
        rt.sleep.end_sleep()
        rt.sleep.rng = random.Random(seed)
        asleep(rt)
        run(rt, 900, dt=0.2)
        for h in rt.sleep.history:
            seen.update(h["events"])
        seen.update(e for e in ("talk", "bubble", "sparkle", "drool", "sigh", "twitch")
                    if any(e in h["events"] for h in rt.sleep.history))
    assert {"talk", "sigh"} <= seen and len(seen) >= 4


def test_every_generated_animation_is_valid(rt):
    ctx = rt.engine.context()
    for seed in range(8):
        rt.sleep.rng = random.Random(seed)
        for variant in ("yawn", "nod", "goodnight"):
            rt.sleep.end_sleep()
            rt.sleep.last_fall = ""
            asleep(rt, variant)
            run(rt, 120, dt=0.2)
            rt.sleep.wake("chat", ("startle", "groggy", "grumpy", "drool", "glitch", "stretch")[(seed + len(variant)) % 6])
            run(rt, 8)
            assert rt.sleep.state == "awake"
    assert len(rt.anims) > 100
    for a in rt.anims:
        anim = parse_animation(a, ctx)
        assert not anim.warnings, (a["intent"], anim.warnings)
        assert len(a["ops"]) <= LIMITS["ops"]
        assert max(op.get("delay_ms", 0) for op in a["ops"]) <= LIMITS["duration_max"]
        assert all(f["ms"] <= LIMITS["ms_max"] for op in a["ops"] if op["type"] == "face" for f in op["frames"])


def test_chat_message_wakes_her_with_random_reaction(rt):
    variants = []
    for _ in range(8):
        asleep(rt)
        rt.record_user("醒醒")
        assert rt.sleep.state == "waking" and rt.sleep.status_text() == "被吵醒了"
        variants.append(rt.sleep.last_wake)
        run(rt, 8)
        assert rt.sleep.state == "awake" and rt.sleep.night is None and rt.sleep.zzz is None
        assert rt.engine.think_text == slp.AWAKE_THINK
        assert rt.current_expression() == slp.WAKE_END[variants[-1]]   # 醒来后按最后停住的表情算（不是“被吵醒”）
    assert all(a != b for a, b in zip(variants, variants[1:]))     # 不会连续两次一样
    assert len(set(variants)) >= 4


def test_reply_tag_waits_for_wake_animation(rt):
    asleep(rt)
    rt.record_user("在吗")
    rt.apply_inline_tag("<face happy level=0.8 idle=calm after=3>")
    assert rt.sleep.pending_tags and rt.current_expression() == "被吵醒"
    rt.on_chat_reply("在的在的！刚刚……没睡着哦。", tag_seen=True)
    assert not rt.idle_timer.isActive()                         # 回复的收尾也等吵醒动画播完
    run(rt, 8)
    assert rt.sleep.state == "awake" and rt.current_expression() == "happy"
    assert rt.idle_timer.isActive() and rt.last_idle["decision"] == "calm"


def test_reply_tag_while_asleep_wakes_her(rt):
    asleep(rt)
    rt.apply_inline_tag("<face smile>")                          # 例如别的入口触发的回复
    assert rt.sleep.state == "waking"
    run(rt, 8)
    assert rt.current_expression() == "smile"


def test_no_autonomous_expressions_while_asleep(rt, monkeypatch):
    calls = []
    monkeypatch.setattr(rt.director, "autonomous_tick", lambda **k: calls.append(k))
    asleep(rt)
    rt._auto()
    assert not calls
    assert rt.express("突然很开心", source="auto") is None
    from pet_ascii.ai.director import GenRequest, GenResult

    before = rt.engine.current
    late = parse_animation({"ops": [{"type": "face", "frames": [{"preset": "happy"}]}]}, rt.engine.context())
    rt._on_result(GenResult(request=GenRequest("突然很开心"), anim=late))    # 生成结果晚到：不盖掉睡脸
    assert rt.engine.current is before and rt.sleep.state == "asleep"
    rt.talk_signal.emit(2.0)                                     # 别处的说话信号：睡着时不张嘴


def test_poke_wakes_and_preset_ends_sleep(rt):
    asleep(rt)
    rt.poke("被戳了一下")
    assert rt.sleep.state == "waking" and rt.sleep.last_wake in ("startle", "groggy", "grumpy", "glitch")
    run(rt, 8)
    asleep(rt)
    rt.apply_preset("surprised")                                 # 菜单里手动换表情：直接醒
    assert rt.sleep.state == "awake" and rt.engine.expression == "surprised" and rt.sleep.night is None


def test_explicit_intent_runs_after_waking(rt, monkeypatch):
    got = []
    monkeypatch.setattr(rt.director, "request", lambda *a, **k: got.append(a[0]) or object())
    asleep(rt)
    assert rt.express("高兴地打招呼", source="user", priority=8) is None
    assert rt.sleep.state == "waking" and not got
    run(rt, 8)
    assert got == ["高兴地打招呼"]


def test_model_is_told_once_that_she_was_woken(rt):
    from pet_ascii.integration import WOKEN_MEMO, prepare_inline_messages

    asleep(rt)
    rt.record_user("早上好")
    msgs = [{"role": "user", "content": "早上好"}]
    prepare_inline_messages(rt, msgs)
    assert msgs[-1]["content"].endswith(WOKEN_MEMO)
    run(rt, 8)
    rt.record_user("再说一句")
    msgs = [{"role": "user", "content": "再说一句"}]
    prepare_inline_messages(rt, msgs)
    assert WOKEN_MEMO not in msgs[-1]["content"]


def test_periodic_save_while_asleep_stores_awake_look(rt):
    asleep(rt)
    run(rt, 20)
    st = rt._export_state()                                      # 每秒存一次的状态：强行结束进程时留下的就是它
    assert st["expression"] == "neutral" and not st["think"].startswith(("<SLEEP>", "<DREAM>"))
    assert st["face_spec"]["eye_l"]["open"] > 0.5
    assert rt.sleep.state == "asleep" and rt.engine.face_spec["eye_l"]["open"] < 0.16   # 画面上照样睡着


def test_quit_while_asleep_saves_awake_face(rt):
    asleep(rt)
    rt.stop()
    st = rt.engine.export_state()
    assert st["expression"] not in (slp.SLEEP_EXPR, "被吵醒")
    assert rt.engine.face_spec["eye_l"]["open"] > 0.5
    assert rt.sleep.state == "awake"
