"""表情进化的容错：模型输出为空、被截断、夹在说明文字里时，记下原因并不开思考补问一次；仍失败就清楚地报错。"""
import json
import logging

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.ai import evolve
from pet_ascii.custom_presets import CustomPresets

DIALOGUE = [{"user": "你是不是想我了", "pet": "（扭过头）才、才没有！", "face": "<face pout>"}]
GOOD = {"thoughts": "她常常嘴硬心软", "actions": [
    {"op": "create", "name": "嘴硬", "emotion": "嘴上不服气其实很开心", "from": "pout",
     "spec": {"eyes": {"smile": 0.4}, "mouth": {"shape": "wavy", "curve": 0.3}, "blush": 0.8, "gaze": [0.7, 0]}},
    {"op": "retire", "name": "没有这个", "reason": "测试"}]}
GOOD_TEXT = json.dumps(GOOD, ensure_ascii=False)


@pytest.fixture
def lib(tmp_path):
    lib = CustomPresets(tmp_path / "lib.json", max_items=6)
    yield lib
    dispose(lib)
    fr.CUSTOM = {}


class Scripted:
    """按顺序返回预先写好的输出（text, finish_reason, usage），记下每次请求。"""

    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def complete(self, messages, max_tokens=0, temperature=0, timeout=0, extra=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens, "extra": extra})
        text, finish, usage = self.outs.pop(0)
        return {"text": text, "finish_reason": finish, "usage": usage, "model": "fake"}


def _usage(prompt, completion, think=None):
    u = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if think is not None:
        u["completion_tokens_details"] = {"reasoning_tokens": think}
    return u


def test_empty_output_retries_without_thinking(lib, caplog):
    # 实测的那种失败：思考把 max_tokens 用完，正文为空
    fake = Scripted(("", "length", _usage(4800, 8000, think=8000)), (GOOD_TEXT, "stop", _usage(4900, 600)))
    with caplog.at_level(logging.WARNING, logger="pet.ascii"):
        rep = evolve.run(fake, "深深", DIALOGUE, lib)
    assert "error" not in rep and "长度上限" in rep["repaired"]
    assert {r["name"]: r["status"] for r in rep["results"]} == {"嘴硬": "new", "没有这个": "refused"}
    assert rep["usage"]["total_tokens"] == 4800 + 8000 + 4900 + 600            # 两次请求都计入
    first, retry = fake.calls
    assert first["max_tokens"] == evolve.DESIGN_MAX_TOKENS >= 8000 and first["extra"]["thinking"]["type"] == "enabled"
    assert retry["extra"] == {"thinking": {"type": "disabled"}}
    msgs = retry["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]                       # 不附空的 assistant 消息
    assert msgs[1]["content"].startswith(first["messages"][1]["content"]) and "只输出一个 JSON 对象" in msgs[1]["content"]
    log = caplog.text
    assert "finish_reason=length" in log and "输出8000（思考8000）" in log and "原文0字" in log


def test_truncated_json_is_not_taken_as_no_change(lib, caplog):
    # 被截断时第一个动作是完整的 {…}：以前会被当成“没有 actions”，悄悄地什么都不做
    cut = GOOD_TEXT[:GOOD_TEXT.index('{"op": "retire"') + 12]
    assert evolve.extract_json(cut).get("op") == "create"
    fake = Scripted((cut, "length", _usage(4800, 8000)), (GOOD_TEXT, "stop", _usage(5000, 600)))
    with caplog.at_level(logging.WARNING, logger="pet.ascii"):
        rep = evolve.run(fake, "深深", DIALOGUE, lib)
    assert "截断" in rep["repaired"] and rep["results"][0]["status"] == "new" and "嘴硬" in lib.items
    msgs = fake.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"] and msgs[2]["content"] == cut
    assert "只输出一个 JSON 对象" in msgs[3]["content"] and "截断" in msgs[3]["content"]
    assert "finish_reason=length" in caplog.text and f"原文{len(cut)}字" in caplog.text


@pytest.mark.parametrize("text", [
    "好的，我仔细看了对话。\n```json\n" + GOOD_TEXT + "\n```\n以上就是这次的修改，{ 有问题再告诉我。",
    "思考：先看 {op} 的格式，再决定 { 要不要改。\n结果：" + GOOD_TEXT + "\n（完）",
])
def test_prose_wrapped_json_parses_first_time(lib, text):
    fake = Scripted((text, "stop", _usage(4800, 900)))
    rep = evolve.run(fake, "深深", DIALOGUE, lib)
    assert len(fake.calls) == 1 and "repaired" not in rep
    assert rep["thoughts"] == "她常常嘴硬心软" and rep["results"][0]["status"] == "new"


def test_still_unparseable_fails_with_clear_log(lib, caplog):
    prose = "这次我觉得表情库已经挺好的了，" * 40                                   # 600 字的纯文字，没有 JSON
    fake = Scripted(("", "length", _usage(4800, 8000, think=8000)), (prose, "stop", _usage(4900, 300)))
    with caplog.at_level(logging.WARNING, logger="pet.ascii"):
        rep = evolve.run(fake, "深深", DIALOGUE, lib)
    assert set(rep) == {"error", "usage", "elapsed"} and not lib.items
    assert "长度上限" in rep["error"] and "补问后仍失败" in rep["error"] and "JSON" in rep["error"]
    assert rep["usage"]["total_tokens"] == 4800 + 8000 + 4900 + 300               # 失败也记下花掉的 token
    line = next(r.getMessage() for r in caplog.records if "补问后仍无法解析" in r.getMessage())
    snippet = line[line.index("「") + 1:line.rindex("」")]
    assert "finish_reason=stop" in line and "原文600字" in line and len(snippet) <= 200 and "…" in snippet


def test_check_truncated_reports_error_not_silence(lib):
    lib.add("憋笑", {"preset": "shy", "eyes": {"smile": 0.6, "open": 0.7}, "blush": 0.9})
    design = json.dumps({"thoughts": "", "actions": [
        {"op": "create", "name": "发呆", "emotion": "放空", "from": "bored", "spec": {"eyes": {"open": 0.5, "pupil": "dot"}}}]},
        ensure_ascii=False)
    check = '{"checks":[{"name":"发呆","ok":true},{"name":"憋'                      # 截断：只剩一个完整的条目
    fake = Scripted((design, "stop", _usage(10, 10)), (check, "length", _usage(10, 1200)))
    rep = evolve.run(fake, "深深", DIALOGUE, lib, check=True)
    assert rep["results"][0]["status"] == "new" and rep["checks"] == [] and "checks" in rep["check_error"]


def test_runtime_logs_failure_and_counts_tokens(tmp_path, monkeypatch, caplog):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path))
    QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    cfg.chat_link = "inline"
    rt = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    try:
        rt.record_user("今天好累")
        rt.apply_inline_tag("<face sad level=0.7>")
        rt.on_chat_reply("（摸摸头）辛苦啦", tag_seen=True)
        assert rt.dialogue.since_evolve == 1
        before = rt.store.budget.tokens
        fake = Scripted(("", "length", _usage(4800, 8000)), ("还是没有 JSON", "stop", _usage(4900, 30)))
        with caplog.at_level(logging.INFO, logger="pet.ascii"):
            rt._on_evolve_done(evolve.run(fake, "深深", rt.dialogue.snapshot(), rt.custom))
        assert rt.store.budget.tokens - before == 4800 + 8000 + 4900 + 30
        assert rt.dialogue.since_evolve == 1                                      # 失败不清零，下次还会再试
        assert any("表情进化失败（" in r.getMessage() and "17730 token" in r.getMessage() for r in caplog.records)

        caplog.clear()
        fake = Scripted(("", "length", _usage(4800, 8000)), (GOOD_TEXT, "stop", _usage(4900, 600)))
        with caplog.at_level(logging.INFO, logger="pet.ascii"):
            rt._on_evolve_done(evolve.run(fake, "深深", rt.dialogue.snapshot(), rt.custom))
        assert any("补问后成功" in r.getMessage() for r in caplog.records) and rt.dialogue.since_evolve == 0
    finally:
        dispose(rt)
        fr.CUSTOM = {}
        fr.OVERRIDES = {}
