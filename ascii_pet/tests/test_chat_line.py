"""“AI 对话”经典窗口的 LINE 风格皮肤（v1.4.0）：桌宠自带的窗口类（有桌宠模块时才跑），临时数据目录，虚构的临时对话。"""
from datetime import datetime, timedelta, timezone

import pytest
from test_chat_theme import chat_window  # noqa: F401  （同一个夹具：临时 APPDATA 里的经典聊天窗口）

from pet_ascii import chat_line, chat_theme


def test_line_readable_contrast():
    c, con = chat_line.L, chat_theme.contrast
    assert con(c["text"], c["bubble_ai"]) >= 12 and con(c["text"], c["bubble_user"]) >= 11     # 正文
    assert con(c["text"], c["pill"]) >= 12                                                     # 输入框
    for bg in (c["sky_top"], c["sky_bot"]):                                                    # 天空上的字（云只会更亮）
        assert con(c["time"], bg) >= 4.5 and con(c["title"], bg) >= 7
    assert con(c["send_text"], c["send"]) >= 4.5 and con(c["err_text"], c["bubble_err"]) >= 4.5


def _count(img, hex_color, box=None, tol=40):
    h = hex_color.lstrip("#")
    target = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    x0, y0, x1, y1 = box or (0, 0, img.width(), img.height())
    return sum(1 for x in range(x0, x1, 2) for y in range(y0, y1, 2)
               if sum(abs(a - b) for a, b in zip(img.pixelColor(x, y).getRgb()[:3], target)) < tol)


def _settle(app, n=4):
    for _ in range(n):
        app.processEvents()


def test_line_skin_applies_switches_and_reverts(chat_window):
    win, app = chat_window
    chat_theme.set_skin("line")
    _settle(app)
    ai, user = win._bubbles[0], win._bubbles[1]
    assert ai.property("lineTheme") and user.property("lineTheme") and not ai.property("asciiTheme")
    assert ai.avatar.isVisible() and not ai.avatar.pixmap().isNull() and not ai.meta.isVisible()     # 她：圆形头像，无名字行
    assert not user.avatar.isVisible() and not user.meta.isVisible()                                   # 自己：不显示头像
    assert chat_line.L["bubble_user"] in win.styleSheet()
    assert _count(user.grab().toImage(), chat_line.L["bubble_user"]) > 100                            # 绿色气泡
    assert _count(ai.grab().toImage(), "#ffffff", tol=6) > 100                                         # 白色气泡
    shell = win.phone_shell.grab().toImage()
    assert _count(shell, chat_line.L["sky_top"], (20, 10, shell.width() - 20, 40), tol=60) > 50        # 标题栏透出天空
    comp, inp = win.composer, win.input
    assert comp.layout().indexOf(inp) == -1 and inp.placeholderText() == chat_line.PLACEHOLDER          # 输入框和发送键在同一行
    assert not win.avatar_label.isVisible() and not win.subtitle_label.isVisible()
    assert win.title_label.text() == "DeepSeek"
    vw = win.scroll.viewport().width()
    assert all(b.width() <= vw for b in win._bubbles) and not win.scroll.horizontalScrollBar().isVisible()

    chat_theme.set_skin("ascii")                                                                       # → ASCII
    _settle(app)
    assert ai.property("asciiTheme") and not ai.property("lineTheme") and not ai.avatar.isVisible()
    assert "#0b0e14" in win.styleSheet() and chat_line.L["bubble_user"] not in win.styleSheet()
    assert comp.layout().indexOf(inp) == 0 and (inp.minimumHeight(), inp.maximumHeight()) == (82, 150)
    assert inp.placeholderText() != chat_line.PLACEHOLDER and win.avatar_label.isVisible()

    chat_theme.set_skin("original")                                                                    # → 桌宠原样
    _settle(app)
    assert not ai.property("lineTheme") and not ai.property("asciiTheme")
    assert ai.avatar.isVisibleTo(ai) and user.avatar.isVisibleTo(user) and ai.meta.isVisibleTo(ai)
    assert ai.avatar.pixmap().isNull() and ai.avatar.text()                                            # 原来的字母头像
    assert "AI" in win.title_label.text() and win.subtitle_label.isVisible()

    chat_theme.set_skin("line")                                                                        # → 再回到 LINE
    _settle(app)
    assert ai.property("lineTheme") and comp.layout().indexOf(inp) == -1 and not ai.avatar.pixmap().isNull()


def test_line_times_read_mark_and_groups(chat_window):
    from pet.chat.models import ChatMessage

    win, app = chat_window
    chat_theme.set_skin("line")
    base = datetime(2026, 10, 19, 11, 0, tzinfo=timezone.utc)
    conv = [(0, "assistant", "在吗"), (0, "assistant", "我刚写完日记"), (1, "assistant", "主人？"),
            (2, "user", "在的"), (2, "user", "刚到家")]
    win.session.messages[:] = [ChatMessage(r, t, created_at=(base + timedelta(minutes=m)).isoformat()) for m, r, t in conv]
    win._load()
    _settle(app)
    bs = list(win._bubbles)

    def hm(m):
        return (base + timedelta(minutes=m)).astimezone().strftime("%H:%M")

    assert [b.property("lineFirst") for b in bs] == [True, False, True, True, False]   # 同一个人同一分钟连发：只有第一条
    assert not bs[0].avatar.pixmap().isNull() and bs[1].avatar.pixmap().isNull() and bs[1].avatar.isVisible()
    assert chat_line.time_text(bs[0]) == hm(0) and chat_line.time_text(bs[2]) == hm(1)  # 存下来的时间（本地时间）
    assert chat_line.time_text(bs[3]) == hm(2)                                          # 她还没回：没有既読
    b = win._add("assistant", "")
    b.set_state("streaming")
    _settle(app)
    assert b.body.text() == chat_line.TYPING and not b.status_label.isVisible()        # 她在想：只显示 • • •
    assert chat_line.time_text(bs[3]) == f"{chat_line.READ_MARK}\n{hm(2)}"
    assert chat_line.time_text(bs[4]).startswith(chat_line.READ_MARK)
    b.set_content("欢迎回来")
    b.set_state("normal")
    assert b.body.text() == "欢迎回来"
    win._remove_bubble(b)                                                               # 删掉她的回复：既読跟着消失
    _settle(app)
    assert not chat_line.time_text(bs[3]).startswith(chat_line.READ_MARK)


def test_line_input_grows_with_text(chat_window):
    win, app = chat_window
    chat_theme.set_skin("line")
    _settle(app)
    inp = win.input
    h1 = inp.height()
    inp.setPlainText("一\n二\n三")
    _settle(app)
    h3 = inp.height()
    inp.setPlainText("\n".join(str(i) for i in range(12)))
    _settle(app)
    h12 = inp.height()
    lh = inp.fontMetrics().lineSpacing()
    assert h1 < h3 < h12 <= h1 + (chat_line.INPUT_MAX_LINES - 1) * lh + 2                # 最多 5 行高
    assert inp.verticalScrollBar().maximum() > 0                                          # 再多就在框里滚动
    inp.setPlainText("")
    _settle(app)
    assert inp.height() == h1 and inp.verticalScrollBar().maximum() == 0                  # 一行也不出滚动条


def test_line_bubble_widths(chat_window):
    win, app = chat_window
    chat_theme.set_skin("line")
    long = win._add("assistant", "很长的一段话，" * 40)
    short = win._add("assistant", "（悄悄把房间的灯调暗）")
    _settle(app)
    vw = win.scroll.viewport().width()
    assert long.width() <= vw and not win.scroll.horizontalScrollBar().isVisible()
    assert long.body.minimumWidth() > 150                                                  # 长消息撑满可用宽度
    fm = short.body.fontMetrics()
    assert short.body.minimumWidth() >= fm.horizontalAdvance(short.body.text())            # 短消息不会无故折行


def test_line_avatar_is_her_drawn_happy_face(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    img = chat_line.render_avatar("maid_bust", 64)
    assert (img.width(), img.height()) == (64, 64)
    assert img.pixelColor(1, 1).alpha() == 0 and img.pixelColor(32, 32).alpha() == 255   # 圆形：四角透明
    assert len({img.pixelColor(x, y).name() for x in range(8, 56, 3) for y in range(8, 56, 3)}) > 40
    saved = dict(chat_line._state)
    try:
        chat_line._avatar_src.clear()
        chat_line.configure(avatar_path=str(tmp_path / "a_{character}.png"), character=lambda: "maid_bust")
        chat_line.avatar_image()
        assert (tmp_path / "a_maid_bust.png").exists()                                     # 只渲染一次，存下来
    finally:
        chat_line._state.update(saved)
        chat_line._avatar_src.clear()
        chat_line._avatar_pm.clear()


def test_set_chat_theme_accepts_names_and_old_bools():
    from pet_ascii import integration

    try:
        integration.set_chat_theme("line")
        assert chat_theme.skin() == "line" and not chat_theme.is_enabled()
        integration.set_chat_theme(False)
        assert chat_theme.skin() == "original"
        integration.set_chat_theme(True)
        assert chat_theme.skin() == "ascii" and chat_theme.is_enabled()
        chat_theme.set_skin("nonsense")
        assert chat_theme.skin() == "ascii"
    finally:
        chat_theme.set_skin("ascii")


@pytest.mark.parametrize("value", ["2026-10-19T12:17:43.063243+00:00", "2026-10-19T12:17:43Z"])
def test_parse_time_converts_to_local(value):
    dt = chat_line.parse_time(value)
    assert dt is not None and dt.utcoffset() == datetime.now().astimezone().utcoffset()
    assert chat_line.parse_time("not a time") is None
