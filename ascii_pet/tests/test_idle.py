"""说完之后：模型用收尾标签决定是否、何时换表情进入挂机。"""
import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.inline import INSTRUCTION, TagFilter, idle_animation, parse_tag
from pet_ascii.protocol import parse_animation


def test_parse_closing_tag():
    t = parse_tag("<face idle=sleepy after=6 fx=zzz look=down>")
    assert t["idle_only"] and t["idle"] == "sleepy" and t["after"] == 6.0
    assert t["fx"] == ["zzz"] and t["look"] == "down"
    assert parse_tag("<face idle=Bored after=30>")["after"] == 30.0          # after：1 分钟之后再等的秒数
    assert parse_tag("<face idle=Bored after=999>")["after"] == 240.0        # 最多再等 4 分钟
    assert parse_tag("<face idle=smile after=-3>")["after"] == 0.0
    assert parse_tag("<face idle=smile>")["after"] == 30.0                   # 没写 after 用默认
    assert parse_tag("<face idle=keep>")["idle"] == "keep"
    assert "idle" not in parse_tag("<face idle=banana after=3>")             # 不认识的表情：不做决定
    opening = parse_tag("<face happy level=0.8 idle=calm after=5>")
    assert opening["preset"] == "happy" and not opening.get("idle_only") and opening["idle"] == "calm"
    assert "idle=" in INSTRUCTION and "after=" in INSTRUCTION and "1 分钟" in INSTRUCTION


def test_filter_strips_closing_tag_and_blank_before_it():
    f = TagFilter()
    chunks = ["<face smile>", "好呀，去吧。\n", "\n<face idle=sl", "eepy after=5>", "\n"]
    out = "".join(f.feed(c) for c in chunks) + f.flush()
    assert out == "好呀，去吧。"
    assert f.pop_tags() == ["<face smile>", "<face idle=sleepy after=5>"]
    f = TagFilter()                                   # 没有标签时，段落间的空行原样保留
    out = "".join(f.feed(c) for c in ["第一段\n", "\n第二段 ", "完"]) + f.flush()
    assert out == "第一段\n\n第二段 完"


def test_idle_animation_valid():
    plan = parse_tag("<face idle=sleepy after=6 fx=zzz act=sway>")
    anim = parse_animation(idle_animation(plan), None)
    assert anim.ops[0].type == "face" and anim.ops[0].frames[0].spec["preset"] == "sleepy"
    assert any(op.type == "effect" for op in anim.ops)


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
    yield r
    dispose(r)
    fr.CUSTOM = {}


def settle(rt, n=20):
    for _ in range(n):
        rt.engine.tick(0.1)


def test_runtime_schedules_and_enters_idle(rt):
    rt.record_user("我去写作业啦")
    rt.apply_inline_tag("<face smile level=0.7>")
    rt.apply_inline_tag("<face idle=sleepy after=6 fx=zzz>")
    settle(rt)
    assert rt.engine.face_spec["eye_l"]["smile"] > 0           # 收尾标签不改变当前表情
    assert not rt.idle_timer.isActive()                         # 回复结束前不排期
    rt.on_chat_reply("去吧去吧，我在这儿等你～", tag_seen=True)
    assert rt.idle_timer.isActive() and rt.idle_timer.interval() == (60 + 6) * 1000   # 1 分钟起步 + 她决定的 6 秒
    assert 60 < rt.idle_eta() <= 66
    assert rt.last_idle["decision"] == "sleepy" and rt.idle_state is None
    assert "idle=sleepy" in rt.dialogue[-1]["face"]
    rt._enter_idle()                                            # 计时到点
    settle(rt, 40)                                              # zzz 特效 2.6s 结束后整段演出才算完成
    assert rt.idle_state["preset"] == "sleepy" and rt.last_idle["fired"]
    assert rt.engine.expression == "sleepy"
    sleepy = fr.normalize({"preset": "sleepy"})
    assert abs(rt.engine.face_spec["eye_l"]["open"] - sleepy["eye_l"]["open"]) < 0.05
    rt.record_user("我回来了")                                   # 用户回来：退出挂机
    assert rt.idle_state is None


def test_new_message_cancels_pending_switch(rt):
    rt.apply_inline_tag("<face happy>")
    rt.apply_inline_tag("<face idle=bored after=8>")
    rt.on_chat_reply("好的！", tag_seen=True)
    assert rt.idle_timer.isActive()
    rt.record_user("等等，还有一件事")
    assert not rt.idle_timer.isActive() and rt.last_idle["cancelled"]
    # 下一轮回复的开头标签也会取消上一轮还没执行的切换
    rt.on_chat_reply("", tag_seen=False)
    rt.apply_inline_tag("<face idle=calm after=3>")
    rt.on_chat_reply("嗯嗯", tag_seen=True)
    assert rt.idle_timer.isActive()
    rt.apply_inline_tag("<face surprised>")
    assert not rt.idle_timer.isActive()


def test_keep_and_no_decision(rt):
    rt.apply_inline_tag("<face shy blush=0.8>")
    rt.apply_inline_tag("<face idle=keep after=0>")
    rt.on_chat_reply("才、才没有呢", tag_seen=True)
    assert rt.idle_timer.isActive() and rt.idle_timer.interval() == 60 * 1000   # keep 也要等 1 分钟才算挂机
    assert rt.idle_state is None
    rt._enter_idle()                                            # 计时到点：不换表情，只进入挂机
    assert rt.idle_state["keep"] and rt.last_idle["fired"]
    rt.apply_inline_tag("<face smile>")                         # 不写收尾标签：保持，不排期
    rt.on_chat_reply("嗯！", tag_seen=True)
    assert not rt.idle_timer.isActive() and rt.idle_state is None


def test_closing_tag_alone_still_gets_local_face(rt):
    rt.apply_inline_tag("<face idle=calm after=2>")              # 忘了写开头标签
    rt.on_chat_reply("哈哈哈，笑死我了", tag_seen=True)
    settle(rt, 5)
    assert rt.idle_timer.isActive()
    assert rt.engine.current_face_spec()["mouth"]["open"] > 0    # 本地关键词兜底（laugh）仍然生效


def test_manual_switch_ends_idle_and_gets_rated(rt):
    """挂机中手动切换表情：退出挂机，标题和 👍/👎 都以手动切换的表情为准。"""
    rt.custom.add("气笑", {"preset": "smile", "eyes": {"smile": 0.5}, "mouth": {"shape": "wavy"}, "sweat": 0.4,
                          "gaze": [0.8, 0.4]})
    rt.apply_inline_tag("<face surprised idle=smile after=2>")
    rt.on_chat_reply("诶！", tag_seen=True)
    rt._enter_idle()
    settle(rt, 40)
    assert rt.idle_state and rt.current_expression() == "smile"
    rt.apply_preset("气笑")
    assert rt.idle_state is None and rt.current_expression() == "气笑"
    assert rt.rate_current(1) == "气笑" and rt.custom.items["气笑"]["likes"] == 1
    assert "smile" not in rt.custom.builtin or not rt.custom.builtin["smile"].get("likes")
    rt.apply_inline_tag("<face happy>")                     # 之后开始的新动画又成为当前表情
    assert rt.current_expression() == "happy"


def test_minimum_delay_is_configurable(rt):
    rt.cfg.idle_min_delay_s = 120
    rt.apply_inline_tag("<face smile idle=calm after=10>")
    rt.on_chat_reply("好呀", tag_seen=True)
    assert rt.idle_timer.interval() == 130 * 1000 and rt.last_idle["delay"] == 130
