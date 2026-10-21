"""Spotify（v1.3）：给她放歌的权限。授权（PKCE + 本机回调）、搜歌、播放 / 排队的规矩、没有设备时的处理、次数上限、聊天和房间里的入口。"""
import json
import random
import socket
import threading
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest
from conftest import dispose

from pet_ascii import face_rig as fr
from pet_ascii.music import Music
from pet_ascii.spotify import SpotifyAuth, SpotifyClient, SpotifyError


class MemStore:
    def __init__(self):
        self.d = {}

    def get(self, cid):
        return self.d.get(cid, "")

    def set(self, cid, tok):
        self.d[cid] = tok

    def delete(self, cid):
        self.d.pop(cid, None)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_pkce_authorize_with_local_callback():
    store, posts = MemStore(), []

    def post(url, data):
        posts.append(data)
        return {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600}

    auth = SpotifyAuth("cid123", free_port(), store=store, post=post)

    def browser(url):                                        # 模拟主人在浏览器里点了“同意”
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and "user-modify-playback-state" in q["scope"][0]
        cb = f"{q['redirect_uri'][0]}?code=CODE9&state={q['state'][0]}"
        threading.Thread(target=lambda: urllib.request.urlopen(cb, timeout=5).read(), daemon=True).start()

    auth.authorize(open_browser=browser, timeout=10)
    assert store.get("cid123") == "R1" and auth.connected and auth.access_token() == "A1"
    assert posts[0]["code"] == "CODE9" and posts[0]["code_verifier"] and "client_secret" not in posts[0]


def test_wrong_state_is_rejected():
    auth = SpotifyAuth("cid", free_port(), store=MemStore(), post=lambda u, d: {"access_token": "x"})

    def browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        cb = f"{q['redirect_uri'][0]}?code=C&state=WRONG"
        threading.Thread(target=lambda: urllib.request.urlopen(cb, timeout=5).read(), daemon=True).start()

    with pytest.raises(SpotifyError, match="对不上"):
        auth.authorize(open_browser=browser, timeout=10)


def test_token_refresh_and_401_retry():
    store = MemStore()
    store.set("cid", "R0")
    clock = [1000.0]
    tokens = iter(["A1", "A2"])
    auth = SpotifyAuth("cid", 8765, store=store, post=lambda u, d: {"access_token": next(tokens), "expires_in": 3600},
                       clock=lambda: clock[0])
    calls = []

    def http(method, url, token, body=None):
        calls.append(token)
        return (401, {}) if token == "A1" else (200, {"tracks": {"items": []}})

    SpotifyClient(auth, http=http).search_track("x")
    assert calls == ["A1", "A2"]                                            # 过期的令牌被拒：刷新一次再试


class FakeAPI:
    """假的 Spotify：记下所有调用。dead=True：播放指令被接受，但客户端不真的开始放（就是这次遇到的情况）。"""

    def __init__(self, playing=None, devices=None, results=None, liked=None):
        self.playing = playing
        self.devs = devices if devices is not None else [{"id": "pc", "name": "我的电脑", "type": "Computer"}]
        self.results = results if results is not None else [{"name": "晴天", "artists": "周杰伦", "album": "叶惠美",
                                                             "uri": "spotify:track:1"}]
        self.liked = liked or []
        self.calls = []
        self.q = []
        self.no_device = False
        self.dead = 0                     # 还有几次播放指令“接受了但没放”（-1 = 一直不放）
        self.contexts = []                # 每次播放带的 context（专辑）；单曲播放记空串
        self.dead_without_context = False  # 像部分 Windows 桌面客户端：单给一首歌不放，按专辑放才放

    def search_track(self, q, limit=5):
        self.calls.append(("search", q))
        return list(self.results)

    def playback(self):
        return self.playing

    def devices(self):
        self.calls.append(("devices",))
        return list(self.devs)

    def transfer(self, device_id, play=False):
        self.calls.append(("transfer", device_id))

    def album_of(self, uri):
        return "spotify:album:A" if uri.startswith("spotify:track:") else ""

    def play(self, uri, device_id="", context=""):
        if self.no_device and not device_id:
            raise SpotifyError("没有正在使用的 Spotify 设备", 404, "NO_ACTIVE_DEVICE")
        self.calls.append(("play", uri, device_id))
        self.contexts.append(context)
        if self.dead or (self.dead_without_context and not context):
            self.dead -= 1 if self.dead > 0 else 0
            self.playing = {"is_playing": False, "uri": "", "name": "", "device": "我的电脑", "device_id": device_id,
                            "device_active": True}
            return
        self.playing = {"is_playing": True, "uri": uri, "name": "?", "artists": "?", "device": "我的电脑",
                        "device_id": device_id or "pc", "device_active": True}

    def queue(self, uri, device_id=""):
        self.calls.append(("queue", uri, device_id))
        self.q.append(uri)

    def queue_uris(self):
        return list(self.q)

    def saved_tracks(self, limit=50, offset=0):
        if self.liked == "noscope":
            raise SpotifyError("还没有读取点赞歌曲的权限", 403, "SCOPE")
        return self.liked[offset:offset + limit], len(self.liked)


def make_music(tmp_path, api, **cfg_over):
    cfg = SimpleNamespace(spotify_enabled=True, spotify_client_id="cid", spotify_redirect_port=8765,
                          spotify_interrupt="queue_if_playing", spotify_share_now_playing=True,
                          spotify_max_per_hour=3, spotify_max_per_day=12)
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    store = MemStore()
    store.set("cid", "R")
    clock = [10_000.0]
    events = []
    m = Music(cfg, tmp_path / "music.json", log_event=events.append, client=api,
              auth=SpotifyAuth("cid", 8765, store=store), clock=lambda: clock[0], launcher=lambda: False,
              wait=lambda s: None)
    return m, clock, events


def test_queue_while_listening_play_when_not(tmp_path):
    api = FakeAPI(playing={"is_playing": True, "name": "别的歌", "artists": "某人", "device": "电脑", "device_id": "pc",
                           "device_active": True, "uri": "spotify:track:other"})
    m, clock, events = make_music(tmp_path, api)
    r = m.request("晴天 周杰伦", "chat")
    assert r["mode"] == "queued" and ("queue", "spotify:track:1", "pc") in api.calls
    assert events[0]["type"] == "music" and events[0]["ok"] and events[0]["name"] == "晴天"
    api.playing = {"is_playing": False, "name": "别的歌", "artists": "某人", "device": "电脑", "device_id": "pc",
                   "device_active": True, "uri": "spotify:track:other"}
    assert m.request("晴天", "chat")["mode"] == "played"
    m2, _, _ = make_music(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, FakeAPI(), spotify_interrupt="queue_only")
    assert m2.request("晴天", "room")["mode"] == "queued"


def test_no_active_device_uses_the_computer(tmp_path):
    api = FakeAPI(playing=None, devices=[{"id": "web", "name": "Web Player (Microsoft Edge)", "type": "Computer"},
                                        {"id": "phone", "name": "手机", "type": "Smartphone"},
                                        {"id": "pc", "name": "我的电脑", "type": "Computer"}])
    api.no_device = True
    m, _, _ = make_music(tmp_path, api)
    r = m.request("晴天", "room")
    assert r["mode"] == "played" and r["device"] == "我的电脑" and ("play", "spotify:track:1", "pc") in api.calls
    api2 = FakeAPI(playing=None, devices=[])
    api2.no_device = True
    m2, _, events = make_music(tmp_path, api2)
    with pytest.raises(SpotifyError, match="没有打开"):
        m2.request("晴天", "chat")
    assert events[-1]["ok"] is False and "没有打开" in m2.last_text()


def test_hourly_and_daily_limits_and_room_not_double_logged(tmp_path):
    m, clock, events = make_music(tmp_path, FakeAPI(), spotify_max_per_hour=2, spotify_max_per_day=3)
    m.request("a", "room")
    m.request("b", "chat")
    with pytest.raises(SpotifyError, match="这一小时"):
        m.request("c", "chat")
    clock[0] += 3700
    m.request("d", "chat")
    with pytest.raises(SpotifyError, match="今天已经放了 3 首"):
        m.request("e", "chat")
    assert [e["source"] for e in events if e.get("ok")] == ["chat", "chat"]          # 房间里放的不在这里重复记


def test_not_found_and_memo(tmp_path):
    api = FakeAPI(results=[])
    m, clock, _ = make_music(tmp_path, api)
    with pytest.raises(SpotifyError, match="没搜到"):
        m.request("不存在的歌", "chat")
    m.now, m.now_t = {"is_playing": True, "name": "夜曲", "artists": "周杰伦"}, clock[0]
    memo = m.memo()
    assert "正在播放：《夜曲》 - 周杰伦" in memo and "music=" in memo and "不打断" in memo and "没成功" in memo
    m.cfg.spotify_share_now_playing = False
    assert "夜曲" not in m.memo()


def test_face_tag_carries_music_with_spaces():
    from pet_ascii.inline import parse_tag

    t = parse_tag('<face happy music="晴天 周杰伦" idle=keep after=3>')
    assert t["preset"] == "happy" and t["music"] == "晴天 周杰伦" and t["idle"] == "keep"
    t = parse_tag("<face smile music=“七里香 周杰伦”>")
    assert t["music"] == "七里香 周杰伦" and t["preset"] == "smile"
    assert "music" not in parse_tag("<face smile>")


@pytest.fixture
def rt(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from pet_ascii.ai.mock import MockClient
    from pet_ascii.config import AsciiConfig
    from pet_ascii.runtime import Runtime

    monkeypatch.setenv("PET_ASCII_HOME", str(tmp_path / "home"))
    QApplication.instance() or QApplication([])
    cfg = AsciiConfig()
    cfg.character = "maid_bust"
    r = Runtime(cfg, client=MockClient(), client_desc="mock（测试）", start_timer=False)
    m, clock, events = make_music(tmp_path, FakeAPI())
    m.log_event = r._music_log
    r.music = m
    r.room.rng = random.Random(1)
    r.room.night = lambda now=None: False
    yield r
    dispose(r)
    fr.CUSTOM = {}


def test_chat_tag_plays_in_background(rt, monkeypatch):
    import time as _t

    seen = []
    monkeypatch.setattr(rt.music, "request", lambda q, s: seen.append((q, s, threading.current_thread().name)))
    rt.apply_inline_tag('<face smile music="晴天 周杰伦">')
    deadline = _t.time() + 3
    while not seen and _t.time() < deadline:
        _t.sleep(0.02)
    assert seen == [("晴天 周杰伦", "chat", "pet-ascii-music")]                  # 在后台线程里放，不卡界面


def test_music_memo_goes_before_the_face_reminder(rt):
    from pet_ascii.inline import TURN_REMINDER
    from pet_ascii.integration import prepare_inline_messages, prepare_music_memo

    rt.cfg.chat_link = "inline"
    msgs = [{"role": "system", "content": "人设"}, {"role": "user", "content": "放首歌吧"}]
    prepare_music_memo(rt, msgs)
    prepare_inline_messages(rt, msgs)
    last = msgs[-1]["content"]
    assert "〔Spotify：" in last and last.endswith(TURN_REMINDER)


class Scripted:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def complete(self, messages, max_tokens=1800, temperature=0.9, timeout=45.0, extra=None):
        self.calls.append(messages)
        return {"text": json.dumps(self.outputs.pop(0), ensure_ascii=False), "usage": {"total_tokens": 100},
                "finish_reason": "stop"}


def test_room_music_action(rt):
    life = rt.room
    c = Scripted([{"thought": "想听点歌", "action": {"type": "music", "query": "晴天 周杰伦"}}])
    life.enter(force=True, client=c, sync=True)
    sys_prompt, situation = c.calls[0][0]["content"], c.calls[0][1]["content"]
    assert "music  {\"query\"" in sys_prompt and "Spotify：" in situation
    ev = next(e for e in life.room.tail_log(5) if e["type"] == "step")
    assert ev["ok"] and ev["what"] == "用 Spotify 放了《晴天》- 周杰伦（直接播放）"
    assert not any(e["type"] == "music" for e in life.room.tail_log(5))          # 房间里放的只记在那一步
    rt.music.auth.disconnect()                                                     # 没连上：她看不到这个动作
    c2 = Scripted([{"thought": "嗯", "action": {"type": "nothing"}}])
    life.enter(force=True, client=c2, sync=True)
    assert "music  {" not in c2.calls[0][0]["content"] and "Spotify：" not in c2.calls[0][1]["content"]


# ---------------------------------------------------------------------- v1.3.1：她说要放却没真的放
def test_music_only_and_closing_tags(rt, monkeypatch):
    from pet_ascii.inline import parse_tag

    assert parse_tag('<face music="晴天 周杰伦">') == {"music": "晴天 周杰伦", "music_only": True}
    t = parse_tag('<face idle=smile after=10 music="七里香 周杰伦">')
    assert t["idle_only"] and t["music"] == "七里香 周杰伦"
    seen = []
    monkeypatch.setattr(rt, "play_music", lambda q, s: seen.append(q) or True)
    rt.apply_inline_tag("<face happy>")
    expr = rt.engine.expression
    rt.apply_inline_tag('<face music="晴天 周杰伦">')                  # 只放歌：表情不变
    assert rt.engine.expression == expr
    rt.apply_inline_tag('<face idle=smile after=10 music="七里香 周杰伦">')
    assert seen == ["晴天 周杰伦", "七里香 周杰伦"] and rt._idle_plan["idle"] == "smile"


def test_asked_but_not_played_is_told_next_turn(rt, monkeypatch):
    from pet_ascii.music import asks_for_music

    assert asks_for_music("现在能放歌了吧？你自己挑一首想听的来试试？")
    assert asks_for_music("来首周杰伦") and asks_for_music("换一首歌吧") and not asks_for_music("今天好累")
    rt.cfg.chat_link = "inline"
    rt.record_user("你自己挑一首歌放吧")
    assert rt._music_asked
    memo = rt.music.memo(asked=True)
    assert "主人这句是在请你放歌" in memo and "只在嘴上说不会真的放出来" in memo
    rt.apply_inline_tag("<face happy idle=smile after=10>")                # 只说不放
    rt.on_chat_reply("嗯……这首你听过吗？", tag_seen=True)
    assert "没有真的放出来" in rt.music.memo()
    rt.record_user("那就放吧")
    monkeypatch.setattr(rt, "play_music", lambda q, s: True)
    rt.apply_inline_tag('<face happy idle=smile after=10 music="晴天 周杰伦">')
    rt.music.data["last"] = {"ok": True, "name": "晴天", "artists": "周杰伦", "mode": "played", "ts": rt.music.clock()}
    rt.on_chat_reply("给你放《晴天》！", tag_seen=True)
    assert "没有真的放出来" not in rt.music.memo()


def test_system_prompt_documents_music_only_when_connected(rt):
    from pet_ascii.inline import INSTRUCTION, MUSIC_INSTRUCTION
    from pet_ascii.integration import prepare_system_instructions

    msgs = [{"role": "system", "content": "人设"}, {"role": "user", "content": "嗨"}]
    prepare_system_instructions(rt, msgs)
    prepare_system_instructions(rt, msgs)                                     # 不重复加
    sys_ = msgs[0]["content"]
    assert sys_.count(INSTRUCTION) == 1 and sys_.count(MUSIC_INSTRUCTION) == 1
    rt.music.auth.disconnect()
    msgs = [{"role": "system", "content": "人设"}, {"role": "user", "content": "嗨"}]
    prepare_system_instructions(rt, msgs)
    assert INSTRUCTION in msgs[0]["content"] and MUSIC_INSTRUCTION not in msgs[0]["content"]



# ---------------------------------------------------------------------- v1.3.2：确认真的在放；点赞的歌
def test_accepted_but_silent_is_retried_then_reported(tmp_path):
    api = FakeAPI(playing={"is_playing": False, "uri": "", "device": "DESKTOP-PC", "device_id": "pc", "device_active": False})
    api.dead = 1                                                              # 第一次没反应，第二次放出来了
    m, _, _ = make_music(tmp_path, api)
    r = m.request("晴天", "chat")
    assert r["mode"] == "played"
    assert ("transfer", "pc") in api.calls and [c for c in api.calls if c[0] == "play"] == [("play", "spotify:track:1", "pc")] * 2
    api2 = FakeAPI(playing=None)
    api2.dead = -1                                                            # 一直不放：如实失败
    m2, _, events = make_music(tmp_path, api2)
    with pytest.raises(SpotifyError, match="没有真的开始播放"):
        m2.request("晴天", "chat")
    assert events[-1]["ok"] is False and "没有真的开始播放" in m2.last_text()


def test_plays_with_album_context_first(tmp_path):
    api = FakeAPI(playing=None)
    api.dead_without_context = True                                           # 单给一首歌：接受了但不放
    m, _, _ = make_music(tmp_path, api)
    assert m.request("晴天", "chat")["mode"] == "played"
    assert api.contexts == ["spotify:album:A"] and api.playing["uri"] == "spotify:track:1"
    api2 = FakeAPI(playing=None)
    api2.dead = 1                                                             # 按专辑也没放：仍按专辑再试一次（v1.4.2）
    m2, _, _ = make_music(tmp_path, api2)
    assert m2.request("晴天", "chat")["mode"] == "played" and api2.contexts == ["spotify:album:A", "spotify:album:A"]


class SlowAPI(FakeAPI):
    """客户端反应慢：播放指令接受后，要读到第 slow+1 次播放状态才真的在放（睡眠唤醒后闲了很久的那种）。"""

    def __init__(self, slow, **kw):
        super().__init__(**kw)
        self.slow, self.reads, self.pending, self.countdown, self.woken = slow, 0, None, 0, False

    def play(self, uri, device_id="", context=""):
        super().play(uri, device_id, context)
        if self.playing and self.playing.get("is_playing"):
            if not self.woken:                                    # 只慢一次：醒过来以后（重试时）接着倒数，不重新计时
                self.woken, self.countdown = True, self.slow
            self.pending = self.playing
            self.playing = {"is_playing": False, "uri": "", "device": "我的电脑", "device_id": device_id,
                            "device_active": True}

    def playback(self):
        self.reads += 1
        if self.pending is not None:
            if self.countdown <= 0:
                self.playing, self.pending = self.pending, None
            else:
                self.countdown -= 1
        return self.playing


def test_slow_client_gets_up_to_8_seconds_before_a_retry(tmp_path):
    from pet_ascii import music as mu

    api = SlowAPI(9, playing=None)                                            # 约 5 秒才放起来：以前 1.5 秒就判失败
    m, _, _ = make_music(tmp_path, api)
    assert m.request("晴天", "chat")["mode"] == "played"
    assert [c for c in api.calls if c[0] == "play"] == [("play", "spotify:track:1", "pc")]   # 没有重试
    assert api.contexts == ["spotify:album:A"] and api.reads == 1 + 10                 # 放之前读 1 次 + 等的时候读 10 次
    late = SlowAPI(int(mu.POLL_MAX_S / mu.POLL_STEP_S) + 2, playing=None)    # 8 秒还没放：才重试，第二次放起来
    m2, _, _ = make_music(tmp_path, late)
    assert m2.request("晴天", "chat")["mode"] == "played" and late.contexts == ["spotify:album:A", "spotify:album:A"]


def test_retry_never_falls_back_to_a_bare_track_and_is_logged(tmp_path, caplog):
    import logging

    api = FakeAPI(playing=None)
    api.dead = -1                                                             # 一直不放
    m, _, _ = make_music(tmp_path, api)
    with caplog.at_level(logging.INFO, logger="pet.ascii"), pytest.raises(SpotifyError, match="没有真的开始播放"):
        m.request("晴天", "chat")
    assert api.contexts == ["spotify:album:A", "spotify:album:A"]              # 重试也不退回单曲写法
    assert [c for c in api.calls if c[0] == "transfer"] == [("transfer", "pc")] * 2   # 开始前、重试前各唤醒一次
    text = caplog.text
    assert "按专辑 spotify:album:A 从这首开始放" in text and "重试前重新把播放交给" in text
    assert "等了约 8.0 秒还没在放" in text and "状态变化" in text and "（空，没有歌）" in text
    solo = FakeAPI(playing=None)
    solo.album_of = lambda uri: ""                                            # 查不到专辑：才用单曲写法
    solo.dead = 1
    m2, _, _ = make_music(tmp_path, solo)
    assert m2.request("晴天", "chat")["mode"] == "played" and solo.contexts == ["", ""]


def test_album_of_and_play_body():
    sent = []

    def http(method, url, token, body=None, timeout=15.0):
        sent.append((method, url, body))
        if "/tracks/" in url:
            return 200, {"album": {"uri": "spotify:album:ALB"}}
        return 204, {}

    auth = SimpleNamespace(access_token=lambda force_refresh=False: "T")
    c = SpotifyClient(auth, http=http)
    assert c.album_of("spotify:track:XYZ") == "spotify:album:ALB" and c.album_of("spotify:local:a:b:c:1") == ""
    c.play("spotify:track:XYZ", "dev", context="spotify:album:ALB")
    c.play("spotify:track:XYZ", "dev")
    plays = [b for m_, u, b in sent if m_ == "PUT"]
    assert plays == [{"context_uri": "spotify:album:ALB", "offset": {"uri": "spotify:track:XYZ"}},
                     {"uris": ["spotify:track:XYZ"]}]


def test_queue_is_checked(tmp_path):
    playing = {"is_playing": True, "uri": "spotify:track:x", "device": "DESKTOP-PC", "device_id": "pc", "device_active": True}
    api = FakeAPI(playing=dict(playing))
    m, _, _ = make_music(tmp_path, api)
    assert m.request("晴天", "chat")["mode"] == "queued"
    api.queue_uris = lambda: []                                               # 队列里没有：不能说排上了
    with pytest.raises(SpotifyError, match="队列里没有"):
        m.request("晴天", "chat")


def test_liked_songs_are_seen_and_played_exactly(tmp_path):
    liked = [{"name": f"歌{i}", "artists": "某人", "uri": f"spotify:track:L{i}"} for i in range(70)] + \
        [{"name": "晴天", "artists": "周杰伦", "uri": "spotify:track:LIKED"}]
    api = FakeAPI(liked=liked)
    m, _, _ = make_music(tmp_path, api)
    m.refresh_liked()
    assert len(m.liked_items()) == 71                                         # 分页读完
    assert m.match_liked("晴天 周杰伦")["uri"] == "spotify:track:LIKED"
    assert m.match_liked("晴天") is not None and m.match_liked("七里香 周杰伦") is None
    r = m.request("《晴天》- 周杰伦", "chat")
    assert r["uri"] == "spotify:track:LIKED" and r["liked"] and not any(c[0] == "search" for c in api.calls)
    memo = m.memo(asked=True)
    assert "主人点赞的歌（一共 71 首" in memo and "没看到的歌不要说成是主人点赞的" in memo
    assert "是主人点赞的歌" in m.last_text()


def test_from_liked_only_when_asked(tmp_path):
    from pet_ascii.music import asks_for_liked

    assert asks_for_liked("从我的点赞里放一首") and asks_for_liked("放首我喜欢的歌") and not asks_for_liked("随便放首歌")
    liked = [{"name": f"歌{i}", "artists": "某人", "uri": f"spotify:track:L{i}"} for i in range(30)]
    m, _, _ = make_music(tmp_path, FakeAPI(liked=liked))
    m.refresh_liked()
    memo = m.memo(asked=True, liked_only=True)
    assert "只能从上面这些里选一首" in memo and "别的歌也可以" not in memo and memo.count("》- 某人") == 16


def test_no_library_permission_says_reconnect(tmp_path):
    m, _, _ = make_music(tmp_path, FakeAPI(liked="noscope"))
    m.refresh_liked()
    assert "需要主人重新连接一次 Spotify" in m.memo(asked=True)
