"""她放歌（v1.3）：给她用主人 Spotify 放歌的权限——用不用、什么时候用、放哪首，都由她自己决定。

规矩（程序强制）：
  - 主人正在听歌时只排进播放队列，不打断（spotify_interrupt；默认 queue_if_playing：没在听时才直接播放）
  - 每小时、每天最多放几首（spotify_max_per_hour / spotify_max_per_day），聊天和房间合计
  - 只会搜歌、播放、加入队列；不删不改歌单、不调音量
  - 每一次都记进“房间”窗口的时间线（房间里放的记在那一步，聊天里放的单独记一条）
主人现在在听什么（spotify_share_now_playing）：每 30 秒在后台看一次，聊天和房间里都给她看。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from .spotify import SpotifyAuth, SpotifyClient, SpotifyError, launch_desktop_app, redirect_uri

log = logging.getLogger("pet.ascii")

POLL_STEP_S = 0.5            # 发完播放指令后多久读一次播放状态
POLL_MAX_S = 8.0             # 最多等多久才判定“没放起来”（v1.4.2，以前只等 1.5 秒）


def _brief(pb) -> str:
    """播放状态的简短写法（写日志用）：在不在放、哪首、哪台设备、设备在不在用。"""
    if not pb:
        return "（没有播放状态）"
    uri = str(pb.get("uri") or "")
    return (f"{'在放' if pb.get('is_playing') else '没在放'} {uri.split(':')[-1] if uri else '（空，没有歌）'}"
            f" @{pb.get('device') or '?'}{'' if pb.get('device_active') else '（设备没在用）'}")

_ASK_RE = re.compile(r"spotify|放首|放一首|来首|来一首|放歌|播歌|听歌|点歌|"
                     r"(放|播|来|听|挑|选|换|点)[^，。！？!?\n]{0,8}(歌|曲子|音乐)", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[\s\-—–_·・,，.。、'\"“”‘’《》「」()（）!！?？&]+", "", str(text or "")).lower()


_LIKED_RE = re.compile(r"点赞|喜欢的歌|红心|收藏|我喜欢|liked|my likes", re.I)


def asks_for_liked(text: str) -> bool:
    """主人是不是说了“从我点赞 / 喜欢 / 收藏的歌里挑”。"""
    return bool(_LIKED_RE.search(str(text or "")))


def asks_for_music(text: str) -> bool:
    """主人这句话是不是在请她放歌（只用来决定提醒得直接一点、没放成时告诉她）。"""
    return bool(_ASK_RE.search(str(text or "")))


class Music:
    def __init__(self, cfg, path: Path, log_event=None, client=None, auth=None, clock=time.time,
                 launcher=launch_desktop_app, wait=time.sleep):
        self.cfg = cfg
        self.path = Path(path)
        self.log_event = log_event
        self.auth = auth or SpotifyAuth(cfg.spotify_client_id, int(cfg.spotify_redirect_port))
        self.client = client or SpotifyClient(self.auth)
        self.clock = clock
        self.launcher = launcher
        self.wait = wait
        self.lock = threading.RLock()
        self.now: dict | None = None
        self.now_t = 0.0
        self._polling = False
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("history", [])

    # ------------------------------------------------------------------ 状态
    def available(self) -> bool:
        return bool(getattr(self.cfg, "spotify_enabled", True) and self.auth.connected)

    def redirect_uri(self) -> str:
        return redirect_uri(int(self.cfg.spotify_redirect_port))

    def set_client_id(self, client_id: str) -> None:
        self.cfg.spotify_client_id = (client_id or "").strip()
        self.auth = SpotifyAuth(self.cfg.spotify_client_id, int(self.cfg.spotify_redirect_port), store=self.auth.store)
        self.client = SpotifyClient(self.auth)

    def connect(self, open_browser=None) -> None:
        """授权（阻塞，后台线程里调用）。"""
        self.auth.authorize(open_browser=open_browser)
        self.refresh_now()

    def disconnect(self) -> None:
        self.auth.disconnect()
        with self.lock:
            self.now, self.now_t = None, 0.0

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ 主人在听什么
    def refresh_now(self) -> None:
        if not (self.available() and getattr(self.cfg, "spotify_share_now_playing", True)):
            return
        try:
            pb = self.client.playback()
        except SpotifyError as exc:
            log.info("字符画·Spotify：读取播放状态失败（%s）", exc)
            return
        with self.lock:
            self.now, self.now_t = pb, self.clock()

    def poll_async(self) -> None:
        """每 30 秒由运行时调用：在后台线程里看一眼主人在听什么。"""
        if self._polling or not self.available() or not getattr(self.cfg, "spotify_share_now_playing", True):
            return
        self._polling = True

        def run():
            try:
                self.refresh_now()
                liked = self.data.get("liked") or {}
                if self.clock() - float(liked.get("ts") or 0) > self.LIKED_REFRESH_S:
                    self.refresh_liked()
            finally:
                self._polling = False

        threading.Thread(target=run, name="pet-ascii-spotify", daemon=True).start()

    # ------------------------------------------------------------------ 你点赞的歌
    LIKED_MAX = 1000
    LIKED_REFRESH_S = 6 * 3600

    def refresh_liked(self) -> None:
        """读你点赞的歌（最多 1000 首），存在 music.json；没有权限时记下来（提示重新连接）。"""
        items, total, err = [], 0, ""
        try:
            offset = 0
            while offset < self.LIKED_MAX:
                page, total = self.client.saved_tracks(50, offset)
                items += page
                offset += 50
                if not page or offset >= total:
                    break
        except SpotifyError as exc:
            err = exc.reason or str(exc)
            log.info("字符画·Spotify：读取点赞的歌失败（%s）", exc)
        with self.lock:
            if items or not err:
                self.data["liked"] = {"ts": round(self.clock(), 1), "total": total, "items": items[:self.LIKED_MAX]}
            else:
                old = self.data.get("liked") or {}
                self.data["liked"] = {**old, "ts": round(self.clock(), 1), "error": err}
            try:
                self._save()
            except OSError:
                pass

    def liked_items(self) -> list:
        return list((self.data.get("liked") or {}).get("items") or [])

    def liked_sample(self, k: int = 12) -> list:
        import random

        items = self.liked_items()
        return random.sample(items, min(k, len(items))) if items else []

    def match_liked(self, query: str) -> dict | None:
        """她写的“歌名 歌手”是不是你点赞的某一首（是的话就放那一首，不另外搜）。"""
        q = _norm(query)
        if not q:
            return None
        best = None
        for it in self.liked_items():
            name = _norm(it["name"])
            if not name or name not in q:
                continue
            arts = [_norm(a) for a in str(it.get("artists", "")).split("、") if a]
            rest = q.replace(name, "", 1)
            if not rest or any(a and (a in rest or rest in a) for a in arts):
                if best is None or len(name) > len(_norm(best["name"])):
                    best = it
        return best

    def liked_text(self, k: int) -> str:
        sample = self.liked_sample(k)
        if not sample:
            err = (self.data.get("liked") or {}).get("error")
            return "（还看不到主人点赞的歌：需要主人重新连接一次 Spotify）" if err == "SCOPE" else ""
        total = (self.data.get("liked") or {}).get("total") or len(self.liked_items())
        songs = "、".join(f"《{s['name']}》- {s['artists']}" for s in sample)
        return f"主人点赞的歌（一共 {total} 首，这次随机看到这些）：{songs}"

    def now_text(self) -> str:
        if not getattr(self.cfg, "spotify_share_now_playing", True):
            return ""
        with self.lock:
            pb, t = self.now, self.now_t
        if not t or self.clock() - t > 180:
            return "主人现在在听什么：不知道（暂时没看到）"
        if not pb or not pb.get("name"):
            return "主人现在没在用 Spotify 听歌"
        state = "正在播放" if pb.get("is_playing") else "暂停着"
        who = f" - {pb['artists']}" if pb.get("artists") else ""
        return f"主人的 Spotify {state}：《{pb['name']}》{who}"

    def last_text(self, within_s: float = 3600) -> str:
        last = self.data.get("last")
        if not last or self.clock() - last.get("ts", 0) > within_s:
            return ""
        ago = int((self.clock() - last["ts"]) // 60)
        when = "刚刚" if ago < 1 else f"{ago} 分钟前"
        if last.get("missed"):
            return f"{when}{last['error']}"
        if last.get("ok"):
            how = "加进了播放队列" if last.get("mode") == "queued" else "已经在放了"
            src = "，是主人点赞的歌" if last.get("liked") else ""
            return f"你{when}放了《{last['name']}》- {last['artists']}（{how}{src}）"
        return f"你{when}想放「{last.get('query', '')}」，没成功：{last.get('error', '')}"

    def quota(self) -> tuple[bool, str, int]:
        now = self.clock()
        hist = [t for t in self.data.get("history", []) if now - t < 86400]
        hour = [t for t in hist if now - t < 3600]
        ph, pd = int(self.cfg.spotify_max_per_hour), int(self.cfg.spotify_max_per_day)
        if len(hist) >= pd:
            return False, f"今天已经放了 {pd} 首，明天再放吧", 0
        if len(hour) >= ph:
            return False, f"这一小时已经放了 {ph} 首，过一会儿再放吧", 0
        return True, "", min(ph - len(hour), pd - len(hist))

    def note_missed(self, user_text: str) -> None:
        """主人请她放歌，她回复里却没有写 music=（只是嘴上说了）：记下来，下一轮告诉她。"""
        with self.lock:
            self.data["last"] = {"ok": False, "missed": True, "query": "", "ts": round(self.clock(), 1),
                                 "error": "主人请你放歌，但你的标签里没有写 music=\"歌名 歌手\"，所以没有真的放出来"}
            try:
                self._save()
            except OSError:
                pass
        log.info("字符画·Spotify：主人请她放歌，但回复里没有 music=（没有真的放）")

    def memo(self, asked: bool = False, liked_only: bool = False) -> str:
        """聊天时本轮消息末尾附的一句（不改系统提示）。asked：主人这句是在请她放歌；liked_only：主人要她从点赞的歌里挑。"""
        if not self.available():
            return ""
        parts = [p for p in (self.now_text(), self.last_text()) if p]
        if asked and self.quota()[0]:
            liked = self.liked_text(16 if liked_only else 12)
            if liked and liked_only:
                parts.append(liked + "。主人要你从点赞的歌里挑：只能从上面这些里选一首，照抄歌名和歌手")
            elif liked:
                parts.append(liked + "（想放其中一首就照抄歌名和歌手；别的歌也可以）")
            parts.append("主人这句是在请你放歌：要放就自己挑一首具体的、真实存在的歌，在标签里写 music=\"歌名 歌手\"——"
                         "只在嘴上说不会真的放出来；没看到的歌不要说成是主人点赞的")
        ok, why, left = self.quota()
        if ok:
            playing = bool(self.now and self.now.get("is_playing"))
            rule = "主人正在听歌，会排进播放队列，不打断" if playing and self.cfg.spotify_interrupt != "play" else \
                ("只会排进播放队列" if self.cfg.spotify_interrupt == "queue_only" else "")
            parts.append("想给主人放首歌时，可以在开头的表情标签里加 music=\"歌名 歌手\"（不是必须的，只在你真的想放的时候）"
                         + (f"；{rule}" if rule else "") + f"；现在还能放 {left} 首")
        else:
            parts.append(f"放歌：{why}")
        return "\n〔Spotify：" + "。".join(parts) + "〕"

    # ------------------------------------------------------------------ 放歌
    def request(self, query: str, source: str) -> dict:
        """搜歌并播放 / 加入队列。成功返回结果；失败抛 SpotifyError（原因是说给她听的中文）。"""
        query = re.sub(r"\s+", " ", str(query or "").strip().strip("\"'“”「」《》"))[:80]
        with self.lock:
            try:
                if not query:
                    raise SpotifyError("没说要放哪首")
                if not self.available():
                    raise SpotifyError("还没有连接 Spotify")
                ok, why, _left = self.quota()
                if not ok:
                    raise SpotifyError(why)
                t = self.match_liked(query)                       # 是你点赞的那首：就放那一首
                from_liked = t is not None
                if t is None:
                    tracks = self.client.search_track(query, 5)
                    if not tracks:
                        raise SpotifyError(f"Spotify 上没搜到「{query}」")
                    t = tracks[0]
                pb = None
                try:
                    pb = self.client.playback()
                except SpotifyError:
                    pb = None
                policy = getattr(self.cfg, "spotify_interrupt", "queue_if_playing")
                mode = "queue" if policy == "queue_only" or (policy != "play" and pb and pb.get("is_playing")) else "play"
                device = self._run(mode, t["uri"], pb)
                res = {"ok": True, "query": query, "name": t["name"], "artists": t["artists"], "uri": t["uri"],
                       "mode": "queued" if mode == "queue" else "played", "source": source, "device": device,
                       "liked": from_liked, "ts": round(self.clock(), 1)}
                self.data["history"] = [x for x in self.data["history"] if self.clock() - x < 86400] + [self.clock()]
                if mode == "play":
                    self.now = {"is_playing": True, "name": t["name"], "artists": t["artists"], "device": device}
                    self.now_t = self.clock()
            except SpotifyError as exc:
                res = {"ok": False, "query": query, "error": str(exc), "source": source, "ts": round(self.clock(), 1)}
            self.data["last"] = res
            try:
                self._save()
            except OSError:
                pass
        if self.log_event is not None and source != "room":     # 房间里放的记在那一步里，不重复记
            try:
                self.log_event({"type": "music", **{k: v for k, v in res.items() if k != "ts"}})
            except Exception:
                pass
        log.info("字符画·Spotify：%s %s", source, res if res.get("ok") else f"没放成：{res.get('error')}")
        if not res["ok"]:
            raise SpotifyError(res["error"])
        return res

    def _run(self, mode: str, uri: str, pb: dict | None) -> str:
        """真正去播放 / 排队，并核对真的生效了。返回设备名；没生效抛 SpotifyError。

        排队：要有正在用的设备；排完读一下队列，确认在里面。
        播放：先把播放交给要用的设备（后台的客户端第一次收到“播放”经常没反应），再播放；
              按“专辑 + 从这首开始”放（部分 Windows 桌面客户端单给一首歌会接受却放不出来，还会被弄成空白）；
              之后每 0.5 秒读一次播放状态，最多等 8 秒（v1.4.2：以前只等 1.5 秒，睡眠唤醒后闲了很久的客户端
              第一次反应慢，就被误判成没放、再用单曲写法重试，反而把它弄空白了）；
              没放起来就重新把播放交给这台设备、仍按专辑再放一次（查不到专辑时才用单曲写法），还不行就如实失败。
              每一步都写进日志。"""
        if mode == "queue" and pb and pb.get("device_active"):
            self.client.queue(uri, pb.get("device_id", ""))
            self.wait(1.0)
            try:
                if uri not in self.client.queue_uris():
                    raise SpotifyError("加入播放队列的指令发出去了，但队列里没有这首")
            except SpotifyError as exc:
                if exc.status:                                    # 读不了队列：不当成失败
                    log.info("字符画·Spotify：核对队列失败（%s）", exc)
                else:
                    raise
            return pb.get("device", "")
        target = self._target_device(pb)
        name = target.get("name", "") or "电脑"
        active = bool(pb and pb.get("device_active") and pb.get("device_id") == target["id"])
        log.info("字符画·Spotify：要放 %s，设备「%s」（%s），播放前的状态 %s", uri, name, "正在用" if active else "没在用",
                 _brief(pb))
        if not active:
            self.client.transfer(target["id"], play=False)          # 唤醒：把播放交给这台设备
            log.info("字符画·Spotify：已把播放交给「%s」", name)
            self.wait(1.0)
        album_of = getattr(self.client, "album_of", None)
        album = album_of(uri) if album_of else ""
        log.info("字符画·Spotify：%s", f"按专辑 {album} 从这首开始放" if album else "查不到这首所在的专辑，按单曲放")
        for attempt in range(2):
            if attempt == 1:                                      # 重试：重新唤醒，仍用同样的写法（不退回单曲）
                self.client.transfer(target["id"], play=False)
                log.info("字符画·Spotify：重试前重新把播放交给「%s」", name)
                self.wait(1.0)
            if album:
                self.client.play(uri, target["id"], context=album)
            else:
                self.client.play(uri, target["id"])
            ok, waited, trace = self._wait_playing(uri)
            if ok:
                log.info("字符画·Spotify：第 %d 次播放指令后约 %.1f 秒放起来了", attempt + 1, waited)
                return target.get("name", "")
            log.info("字符画·Spotify：第 %d 次播放指令已接受，但等了约 %.1f 秒还没在放；状态变化 %s", attempt + 1, waited,
                     trace)
        raise SpotifyError(f"Spotify 收到了播放指令，但「{name}」上的客户端没有真的开始播放")

    def _wait_playing(self, uri: str) -> tuple[bool, float, list]:
        """发完播放指令后每 POLL_STEP_S 秒读一次播放状态，最多 POLL_MAX_S 秒。
        返回 (是否在放这首, 大约等了多少秒, 状态变化记录)。读状态出错不算失败，记下来接着等。"""
        trace, last, waited = [], None, 0.0
        t0 = time.monotonic()
        for _ in range(int(round(POLL_MAX_S / POLL_STEP_S))):
            self.wait(POLL_STEP_S)
            waited += POLL_STEP_S
            try:
                now = self.client.playback()
                brief = _brief(now)
            except SpotifyError as exc:
                now, brief = None, f"读状态出错：{exc}"
            if brief != last:                                      # 只记变化，日志不会太长
                trace.append(f"{waited:.1f}s {brief}")
                last = brief
            if now and now.get("uri") == uri and now.get("is_playing"):
                return True, max(waited, time.monotonic() - t0), trace
        return False, max(waited, time.monotonic() - t0), trace

    def _target_device(self, pb: dict | None) -> dict:
        """要在哪台设备上放：正在用的那台；没有就找电脑上的客户端（没开就打开它）。"""
        if pb and pb.get("device_id"):
            return {"id": pb["device_id"], "name": pb.get("device", "")}
        devices = self.client.devices()
        if not devices and self.launcher():
            for _ in range(4):                                       # 刚打开的客户端要几秒才出现在设备列表里
                self.wait(2.5)
                devices = self.client.devices()
                if devices:
                    break
        if not devices:
            raise SpotifyError("找不到可以播放的设备（电脑上的 Spotify 没有打开）")
        def rank(d):                                             # 正在用的 > 电脑上的客户端 > 网页播放器 > 其它
            name, typ = str(d.get("name", "")), str(d.get("type", "")).lower()
            return (0 if d.get("is_active") else 1, 0 if typ == "computer" and not name.lower().startswith("web player")
                    else (1 if typ == "computer" else 2))

        pick = sorted(devices, key=rank)[0]
        return {"id": pick.get("id", ""), "name": pick.get("name", "")}
