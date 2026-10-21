"""Spotify（v1.3）：授权、搜歌、播放 / 加入队列、主人现在在听什么。只用 Spotify 官方 Web API。

授权用 PKCE（不需要、也不保存 Client Secret）：
  1. 主人在 developer.spotify.com 建一个自己的应用，回调地址填 http://127.0.0.1:8765/callback，把 Client ID 填进字符画设置
  2. 第一次连接时打开浏览器，主人在 Spotify 的页面上点“同意”；本机 127.0.0.1:8765 接住回调，换到令牌
  3. 刷新令牌存在 Windows 凭据管理器（keyring），不写明文文件；访问令牌只在内存里，过期自动刷新
权限只要四项：读播放状态、控制播放（播放 / 加入队列）、读正在播放的歌、读你点赞的歌（v1.3.2）。不读歌单、不改歌单、不碰音量。
2026 年 2 月起：开发者模式的应用要求应用主人是 Premium；搜索每次最多 10 条。
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing user-library-read"
KEYRING_SERVICE = "dsh-pet-ascii-spotify"


class SpotifyError(RuntimeError):
    """说给她 / 主人听的原因（中文）。"""

    def __init__(self, message: str, status: int = 0, reason: str = ""):
        super().__init__(message)
        self.status = status
        self.reason = reason


def redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/callback"


# ---------------------------------------------------------------------- 令牌存放
class KeyringStore:
    """刷新令牌放在 Windows 凭据管理器里（keyring）。"""

    def get(self, client_id: str) -> str:
        try:
            import keyring

            return keyring.get_password(KEYRING_SERVICE, client_id) or ""
        except Exception:
            return ""

    def set(self, client_id: str, token: str) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, client_id, token)

    def delete(self, client_id: str) -> None:
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, client_id)
        except Exception:
            pass


def _post_form(url: str, data: dict, timeout: float = 20.0) -> dict:
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace")
        raise SpotifyError(f"Spotify 授权出错（HTTP {e.code}）：{detail[:200]}", e.code)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise SpotifyError(f"连不上 Spotify：{getattr(e, 'reason', e)}")


# ---------------------------------------------------------------------- 授权（PKCE）
class SpotifyAuth:
    def __init__(self, client_id: str, port: int = 8765, store=None, post=_post_form, clock=time.time):
        self.client_id = (client_id or "").strip()
        self.port = int(port)
        self.store = store or KeyringStore()
        self._post = post
        self.clock = clock
        self._lock = threading.Lock()
        self._access = ""
        self._expires = 0.0

    @property
    def connected(self) -> bool:
        return bool(self.client_id and self.store.get(self.client_id))

    def disconnect(self) -> None:
        with self._lock:
            self._access, self._expires = "", 0.0
        if self.client_id:
            self.store.delete(self.client_id)

    def authorize(self, open_browser=None, timeout: float = 240.0) -> None:
        """打开浏览器让主人点“同意”，在本机接住回调、换到令牌。阻塞；在后台线程里调用。失败抛 SpotifyError。"""
        if not self.client_id:
            raise SpotifyError("还没有填 Spotify 应用的 Client ID")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri(self.port),
                  "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state}
        url = AUTH_URL + "?" + urllib.parse.urlencode(params)
        got: dict = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if urllib.parse.urlparse(self.path).path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                got.update({k: v[0] for k, v in q.items()})
                ok = got.get("state") == state and "code" in got
                msg = "授权完成，可以关掉这个页面了。" if ok else "授权没有完成，可以关掉这个页面。"
                page = f"<!doctype html><meta charset=utf-8><title>Spotify</title><body style='font-family:sans-serif;padding:40px'>{msg}</body>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page.encode("utf-8"))
                done.set()

            def log_message(self, *a):
                pass

        try:
            srv = socketserver.TCPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            raise SpotifyError(f"本机端口 {self.port} 被占用了，没法接收授权（{exc}）")
        srv.timeout = 0.5
        try:
            if open_browser is None:
                import webbrowser

                open_browser = webbrowser.open
            open_browser(url)
            deadline = time.monotonic() + timeout
            while not done.is_set() and time.monotonic() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        if not done.is_set():
            raise SpotifyError("等了太久，没有收到授权（浏览器里没有点“同意”？）")
        if got.get("error"):
            raise SpotifyError(f"授权被拒绝：{got['error']}")
        if got.get("state") != state or not got.get("code"):
            raise SpotifyError("授权回来的信息对不上，没有保存")
        tok = self._post(TOKEN_URL, {"grant_type": "authorization_code", "code": got["code"],
                                     "redirect_uri": redirect_uri(self.port), "client_id": self.client_id,
                                     "code_verifier": verifier})
        self._take(tok)

    def _take(self, tok: dict) -> None:
        if not tok.get("access_token"):
            raise SpotifyError("Spotify 没有给出令牌")
        with self._lock:
            self._access = tok["access_token"]
            self._expires = self.clock() + float(tok.get("expires_in") or 3600) - 60
        if tok.get("refresh_token"):
            self.store.set(self.client_id, tok["refresh_token"])

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if self._access and not force_refresh and self.clock() < self._expires:
                return self._access
        refresh = self.store.get(self.client_id) if self.client_id else ""
        if not refresh:
            raise SpotifyError("还没有连接 Spotify")
        tok = self._post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": refresh,
                                     "client_id": self.client_id})
        self._take(tok)
        return self._access


# ---------------------------------------------------------------------- Web API
def _http(method: str, url: str, token: str, body: dict | None = None, timeout: float = 15.0) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else (b"" if method in ("PUT", "POST") else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if not raw.strip():
                return r.status, {}
            try:
                return r.status, json.loads(raw.decode("utf-8"))
            except ValueError:                                   # 有的接口成功时回的是纯文本
                return r.status, {"raw": raw.decode("utf-8", "replace")[:300]}
    except urllib.error.HTTPError as e:
        raw = e.read(2000)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            obj = {"error": {"message": raw.decode("utf-8", "replace")[:200]}}
        return e.code, obj
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise SpotifyError(f"连不上 Spotify：{getattr(e, 'reason', e)}")


class SpotifyClient:
    def __init__(self, auth: SpotifyAuth, http=_http):
        self.auth = auth
        self._http = http

    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
        url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        status, obj = self._http(method, url, self.auth.access_token(), body)
        if status == 401:                                           # 令牌过期：刷新一次再试
            status, obj = self._http(method, url, self.auth.access_token(force_refresh=True), body)
        return status, obj

    @staticmethod
    def _fail(status: int, obj: dict, what: str) -> SpotifyError:
        err = (obj or {}).get("error") or {}
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        reason = err.get("reason", "") if isinstance(err, dict) else ""
        if status == 403 and ("premium" in msg.lower() or reason == "PREMIUM_REQUIRED"):
            return SpotifyError("这个账号不是 Premium，Spotify 不允许程序控制播放", status, reason)
        if status == 404 and (reason == "NO_ACTIVE_DEVICE" or "device" in msg.lower()):
            return SpotifyError("没有正在使用的 Spotify 设备", status, "NO_ACTIVE_DEVICE")
        if status == 429:
            return SpotifyError("Spotify 说请求太频繁了，等一会儿再试", status, reason)
        return SpotifyError(f"{what}没成功（HTTP {status}：{msg[:120]}）", status, reason)

    def search_track(self, query: str, limit: int = 5) -> list:
        status, obj = self._req("GET", "/search", {"q": query, "type": "track", "limit": max(1, min(10, limit))})
        if status != 200:
            raise self._fail(status, obj, "搜歌")
        out = []
        for t in ((obj.get("tracks") or {}).get("items") or []):
            if not t or not t.get("uri"):
                continue
            out.append({"name": t.get("name", ""), "artists": "、".join(a.get("name", "") for a in t.get("artists") or []),
                        "album": (t.get("album") or {}).get("name", ""), "uri": t["uri"]})
        return out

    def playback(self) -> dict | None:
        """正在播放的状态；没有在用的设备时返回 None。"""
        status, obj = self._req("GET", "/me/player")
        if status == 204 or not obj:
            return None
        if status != 200:
            raise self._fail(status, obj, "读取播放状态")
        item = obj.get("item") or {}
        return {"is_playing": bool(obj.get("is_playing")), "device": (obj.get("device") or {}).get("name", ""),
                "device_id": (obj.get("device") or {}).get("id", ""), "name": item.get("name", ""),
                "uri": item.get("uri", ""), "device_active": bool((obj.get("device") or {}).get("is_active")),
                "artists": "、".join(a.get("name", "") for a in item.get("artists") or []),
                "type": obj.get("currently_playing_type", "")}

    def devices(self) -> list:
        status, obj = self._req("GET", "/me/player/devices")
        if status != 200:
            raise self._fail(status, obj, "读取设备")
        return obj.get("devices") or []

    def play(self, uri: str, device_id: str = "", context: str = "") -> None:
        """播放这首。context（专辑等）：按“这张专辑、从这首开始”放——有的电脑客户端只认这种，单给一首歌（uris）会被接受却放不出来。"""
        body = {"context_uri": context, "offset": {"uri": uri}} if context else {"uris": [uri]}
        status, obj = self._req("PUT", "/me/player/play", {"device_id": device_id} if device_id else None, body)
        if status not in (200, 202, 204):
            raise self._fail(status, obj, "播放")

    def album_of(self, uri: str) -> str:
        """这首歌所在专辑的 uri；查不到返回空（不抛错）。"""
        if not str(uri).startswith("spotify:track:"):
            return ""
        try:
            status, obj = self._req("GET", "/tracks/" + uri.split(":")[-1])
        except Exception:                                         # noqa: BLE001
            return ""
        return str(((obj or {}).get("album") or {}).get("uri", "")) if status == 200 else ""

    def transfer(self, device_id: str, play: bool = False) -> None:
        """把播放交给这个设备（唤醒后台的客户端）。"""
        status, obj = self._req("PUT", "/me/player", None, {"device_ids": [device_id], "play": bool(play)})
        if status not in (200, 202, 204):
            raise self._fail(status, obj, "切换设备")

    def queue_uris(self) -> list:
        status, obj = self._req("GET", "/me/player/queue")
        if status != 200:
            raise self._fail(status, obj, "读取播放队列")
        return [t.get("uri", "") for t in (obj.get("queue") or []) if t]

    def saved_tracks(self, limit: int = 50, offset: int = 0) -> tuple[list, int]:
        """你点赞的歌（Liked Songs）一页。→ (歌, 总数)。没有 user-library-read 权限时抛 SpotifyError（reason=SCOPE）。"""
        status, obj = self._req("GET", "/me/tracks", {"limit": max(1, min(50, limit)), "offset": max(0, offset)})
        if status == 403 and "scope" in str((obj.get("error") or {}).get("message", "")).lower():
            raise SpotifyError("还没有读取点赞歌曲的权限（需要重新连接一次 Spotify）", 403, "SCOPE")
        if status != 200:
            raise self._fail(status, obj, "读取点赞的歌")
        out = []
        for it in obj.get("items") or []:
            t = (it or {}).get("track") or {}
            if t.get("uri"):
                out.append({"name": t.get("name", ""), "artists": "、".join(a.get("name", "") for a in t.get("artists") or []),
                            "uri": t["uri"]})
        return out, int(obj.get("total") or len(out))

    def queue(self, uri: str, device_id: str = "") -> None:
        params = {"uri": uri, **({"device_id": device_id} if device_id else {})}
        status, obj = self._req("POST", "/me/player/queue", params)
        if status not in (200, 202, 204):
            raise self._fail(status, obj, "加入队列")


def launch_desktop_app() -> bool:
    """没有正在使用的设备时，打开电脑上的 Spotify 客户端（装了的话）。"""
    import sys

    if sys.platform == "darwin":                     # macOS：用系统的 open 打开 spotify: 链接
        import subprocess

        try:
            subprocess.Popen(["open", "spotify:"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False
    try:
        os.startfile("spotify:")                     # type: ignore[attr-defined]
        return True
    except (OSError, AttributeError):
        return False
