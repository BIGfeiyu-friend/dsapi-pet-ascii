"""“AI 对话”经典窗口的 ASCII 皮肤：用桌宠自带的窗口类（有桌宠模块时才跑），临时数据目录，虚构对话。"""
import json
import os
import sys

import pytest
from conftest import dispose

from pet_ascii import chat_theme


def test_readable_contrast():
    c = chat_theme.C
    for fg, bg in ((c["text"], c["bubble_ai"]), ("#eaf6ff", c["bubble_user"]), (c["text"], c["bg"]),
                   (c["dim"], c["panel"]), (c["dim"], c["bg"]), (c["pink"], c["bubble_ai"]),
                   (c["cyan"], c["bubble_user"]), (c["amber"], c["bubble_ai"]), (c["text2"], c["bubble_ai"])):
        assert chat_theme.contrast(fg, bg) >= 4.5, (fg, bg)            # WCAG AA
    assert chat_theme.contrast(c["text"], c["bubble_ai"]) >= 12          # 正文远高于 AA


@pytest.fixture
def chat_window(tmp_path, monkeypatch):
    lw = pytest.importorskip("pet.chat.legacy_widgets")
    pc = pytest.importorskip("pet.config")
    from PySide6.QtWidgets import QApplication

    monkeypatch.setenv("APPDATA", str(tmp_path))
    appdir = tmp_path / "dsh-pet-standalone-webm-chat"
    appdir.mkdir()
    (appdir / "config.json").write_text(json.dumps({
        "character": "shenshen", "chat_ui_style": "classic",
        "chat": {"enabled": True, "active_provider": "p", "providers": {"p": {"name": "DeepSeek", "base_url": "http://127.0.0.1:9",
                                                                               "model": "deepseek-v4-pro"}}}}), encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    try:
        cfg = pc.Config()
    except TypeError:
        cfg = pc.Config(pc._default_base(), "")
    if str(tmp_path).lower() not in str(pc._default_base()).lower():
        pytest.skip("桌宠配置目录没有指向临时目录")
    chat_theme.install(enabled=True, name_provider=lambda: "测试娘", tile_path=str(tmp_path / "tile.png"))
    chat_theme.set_enabled(True)
    win = lw.ChatWindow(cfg, "shenshen", pet_window=None, notifier=None, auth_callback=None)
    for role, text in (("assistant", "（歪头）主人回来啦～今天过得怎么样？我刚才一直在数窗外飘过去的云，数到第三十七朵的时候差点睡着了。"),
                       ("user", "有点累")):
        win._add(role, text)
    win.resize(430, 780)
    win.show()
    for _ in range(4):
        app.processEvents()
    yield win, app
    dispose(win)
    # 对话窗口的会话写盘线程（桌宠自带模块；写的是上面的临时 APPDATA）：用桌宠自己给测试留的收尾函数停掉
    reset = getattr(sys.modules.get("pet.chat.session_store"), "reset_writers_for_tests", None)
    if reset is not None:
        reset()
    chat_theme.set_enabled(True)


def test_ascii_skin_applies_and_reverts(chat_window):
    win, app = chat_window
    ai, user = win._bubbles[0], win._bubbles[1]
    assert ai.property("asciiTheme") and not ai.avatar.isVisible() and not ai.meta.isVisible()
    assert "#0b0e14" in win.styleSheet()
    ai.body.ensurePolished()
    assert ai.body.font().pixelSize() == 13                                 # 正文放大到 13px
    vw = win.scroll.viewport().width()
    assert all(b.width() <= vw for b in win._bubbles)                      # 不会撑出横向滚动条
    assert not win.scroll.horizontalScrollBar().isVisible()
    img = ai.grab().toImage()                                               # 上边框画出来了（粉色）
    pink = chat_theme.C["pink"].lstrip("#")
    target = tuple(int(pink[i:i + 2], 16) for i in (0, 2, 4))
    hits = sum(1 for x in range(img.width()) for y in range(0, 14)
               if sum(abs(a - b) for a, b in zip(img.pixelColor(x, y).getRgb()[:3], target)) < 90)
    assert hits > 20
    assert win.title_label.text() == "DeepSeek" and win.avatar_label.text() == "D"     # 标题栏改成 DeepSeek
    chat_theme.set_enabled(False)
    app.processEvents()
    assert not ai.property("asciiTheme") and ai.avatar.isVisibleTo(ai) and "#0b0e14" not in win.styleSheet()
    assert ai.body.minimumWidth() == 0
    assert "AI" in win.title_label.text() and win.title_label.text() != "DeepSeek"   # 关掉皮肤：还原原来的标题
    chat_theme.set_enabled(True)
    app.processEvents()
    assert ai.property("asciiTheme") and "#0b0e14" in win.styleSheet() and win.title_label.text() == "DeepSeek"


def test_streaming_cursor(chat_window):
    win, app = chat_window
    win._add("assistant", "")
    b = win._bubbles[-1]
    b.set_state("streaming")
    b.set_content("从前有一只小鲸鱼")
    assert chat_theme.plain_text(b.body).endswith("▌")
    b.set_content("从前有一只小鲸鱼，晚安。")
    b.set_state("normal")
    assert chat_theme.plain_text(b.body) == "从前有一只小鲸鱼，晚安。"                       # 生成完：光标去掉，正文原样
