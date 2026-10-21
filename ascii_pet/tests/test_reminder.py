"""真实对话请求里的表情提醒：会话历史里的回复都已剥掉标签，模型会照着省略标签；本轮用户消息末尾提醒一句。"""
import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii import integration
from pet_ascii.inline import TURN_REMINDER


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
    yield r
    dispose(r)
    fr.CUSTOM = {}
    fr.OVERRIDES = {}


def chat():
    return [{"role": "system", "content": "人设"}, {"role": "user", "content": "早"},
            {"role": "assistant", "content": "（揉眼睛）早呀"}, {"role": "user", "content": "我回来啦"}]


def test_reminder_goes_to_last_user_message_only(rt):
    msgs = chat()
    integration.prepare_inline_messages(rt, msgs)
    assert msgs[-1]["content"] == "我回来啦" + TURN_REMINDER
    assert [m["content"] for m in msgs[:3]] == ["人设", "早", "（揉眼睛）早呀"]     # 历史与系统提示不动（前缀缓存不受影响）


def test_memo_then_reminder_and_switch_off(rt):
    rt.custom.add("气笑", {"preset": "smile", "eyes": {"smile": 0.5}, "mouth": {"shape": "wavy"}, "sweat": 0.4, "gaze": [0.8, 0.4]})
    msgs = chat()
    integration.prepare_inline_messages(rt, msgs)
    content = msgs[-1]["content"]
    assert "气笑" in content and content.index("气笑") < content.index(TURN_REMINDER.strip())
    rt.cfg.inline_reminder = False
    msgs = chat()
    integration.prepare_inline_messages(rt, msgs)
    assert TURN_REMINDER not in msgs[-1]["content"] and "气笑" in msgs[-1]["content"]


def test_multimodal_last_message_untouched(rt):
    msgs = chat()
    msgs[-1]["content"] = [{"type": "image_url", "image_url": {"url": "data:,"}}]
    integration.prepare_inline_messages(rt, msgs)
    assert isinstance(msgs[-1]["content"], list) and len(msgs[-1]["content"]) == 1
