"""更快的 OpenAI 兼容流式请求（替换 OpenAICompatibleProvider.stream）。

相比原实现：
  1. 长连接复用（HTTP/1.1 keep-alive），省掉每次的 TCP + TLS 握手；支持系统/环境代理（CONNECT 隧道）
  2. SSL 上下文缓存，不再每次重新加载 certifi 证书包
  3. read1() 增量读取：原实现 read(4096) 在分块传输下会攒满 4KB（约 15~20 个 token）才返回，首字明显变慢
  4. DeepSeek 默认开启思考模式（effort=high），而本应用丢弃思维链——白白等待。这里默认显式关闭
  5. 记录首字延迟、总耗时、上下文缓存命中 token，写入桌宠日志
"""
from __future__ import annotations

import base64
import http.client
import json
import logging
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("pet_memory.net")

_settings = None  # MemorySettings，由 bootstrap 注入
_orig_stream = None  # 原始 stream，用于不支持的代理类型时回退
_providers = None  # pet.chat.providers 模块

_pool_lock = threading.Lock()
_pool: dict[tuple, list[tuple[http.client.HTTPConnection, float]]] = {}
_ctx_cache: dict[bool, ssl.SSLContext] = {}
_proxy_cache: tuple[float, dict] = (0.0, {})

_RETRYABLE = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.BadStatusLine,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    ssl.SSLEOFError,
)

MSG_CONNECT_FAILED = "网络连接失败："
MSG_REQUEST_FAILED = "网络请求失败："
MSG_BAD_JSON = "Provider 返回了无效 JSON"
MSG_PROVIDER_ERROR = "Provider 返回错误"


def configure(settings, providers_module, orig_stream) -> None:
    global _settings, _providers, _orig_stream
    _settings = settings
    _providers = providers_module
    _orig_stream = orig_stream


# ---------------------------------------------------------------------- 基础设施
def ssl_context(verify: bool) -> ssl.SSLContext:
    verify = bool(verify)
    ctx = _ctx_cache.get(verify)
    if ctx is None:
        if _providers is not None:
            ctx = _providers._make_ssl_context(verify)
        else:
            ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
        _ctx_cache[verify] = ctx
    return ctx


def _proxies() -> dict:
    global _proxy_cache
    now = time.monotonic()
    if now - _proxy_cache[0] > 30:
        try:
            _proxy_cache = (now, urllib.request.getproxies())
        except Exception:
            _proxy_cache = (now, {})
    return _proxy_cache[1]


def _proxy_for(scheme: str, host: str):
    """返回 None（直连）、(host, port, auth_header) 或 'unsupported'。"""
    proxies = _proxies()
    p = proxies.get(scheme)
    if not p:
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except Exception:
        pass
    u = urllib.parse.urlsplit(p if "://" in p else "http://" + p)
    if u.scheme not in ("http",) or not u.hostname:
        return "unsupported"
    auth = None
    if u.username:
        cred = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
        auth = "Basic " + base64.b64encode(cred.encode()).decode()
    return (u.hostname, u.port or 80, auth)


def _target(endpoint: str, verify: bool):
    u = urllib.parse.urlsplit(endpoint)
    scheme = (u.scheme or "https").lower()
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    proxy = _proxy_for(scheme, host)
    key = (scheme, host, port, bool(verify), proxy if isinstance(proxy, tuple) else None)
    return key, path, proxy


def _new_conn(key, timeout: float) -> http.client.HTTPConnection:
    scheme, host, port, verify, proxy = key
    if scheme == "https":
        if proxy:
            conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout, context=ssl_context(verify))
            conn.set_tunnel(host, port, headers={"Proxy-Authorization": proxy[2]} if proxy[2] else None)
        else:
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl_context(verify))
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    return conn


def _acquire(key, timeout: float):
    idle_max = getattr(_settings, "keepalive_idle_seconds", 50.0) if _settings else 50.0
    now = time.monotonic()
    with _pool_lock:
        lst = _pool.get(key) or []
        while lst:
            conn, last = lst.pop()
            if conn.sock is not None and now - last < idle_max:
                try:
                    conn.sock.settimeout(timeout)
                except OSError:
                    _close_quiet(conn)
                    continue
                return conn, True
            _close_quiet(conn)
    return _new_conn(key, timeout), False


def _release(key, conn) -> None:
    if conn.sock is None:
        return
    with _pool_lock:
        lst = _pool.setdefault(key, [])
        lst.append((conn, time.monotonic()))
        while len(lst) > 2:
            _close_quiet(lst.pop(0)[0])


def _close_quiet(conn) -> None:
    try:
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()
    except Exception:
        pass


def _open(key, path: str, body: bytes, headers: dict, timeout: float):
    """发送请求并取得响应头。复用的连接若已被服务端关闭，自动换新连接重试一次。"""
    last_exc = None
    for attempt in range(2):
        conn, reused = _acquire(key, timeout)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            return conn, resp, reused
        except _RETRYABLE as exc:
            _close_quiet(conn)
            last_exc = exc
            if reused and attempt == 0:
                continue
            raise
        except BaseException:
            _close_quiet(conn)
            raise
    raise last_exc  # pragma: no cover


def is_deepseek(host: str) -> bool:
    return (host or "").lower().endswith("deepseek.com")


def apply_extras(payload: dict, host: str, streaming: bool) -> None:
    s = _settings
    if is_deepseek(host):
        mode = (getattr(s, "deepseek_thinking", "disabled") or "disabled").lower() if s else "disabled"
        if mode in ("disabled", "enabled"):
            payload["thinking"] = {"type": mode}
        if streaming:
            payload["stream_options"] = {"include_usage": True}
    if s is not None and s.extra_payload:
        payload.update(s.extra_payload)


def _headers(api_key: str, accept: str) -> dict:
    extra = {"Content-Type": "application/json", "Accept": accept}
    if _providers is not None:
        headers = _providers.build_browser_headers(extra)
    else:
        headers = dict(extra)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _provider_error(message: str, status=None):
    if _providers is not None:
        return _providers.ProviderError(message, status) if status is not None else _providers.ProviderError(message)
    return RuntimeError(message)


def _connect_error(exc: BaseException):
    hint = ""
    try:
        if _providers is not None and _providers._is_cert_verify_error(exc):
            hint = _providers._CERT_HINT
    except Exception:
        pass
    return _provider_error(f"{MSG_CONNECT_FAILED}{exc}{hint}")


def _http_error(resp):
    try:
        detail = resp.read(2048).decode("utf-8", "replace")
    except Exception:
        detail = ""
    if _providers is not None:
        return _provider_error(_providers._safe_error_detail(detail), resp.status)
    return RuntimeError(f"HTTP {resp.status}: {detail[:300]}")


# ---------------------------------------------------------------------- SSE
class _SSE:
    def __init__(self):
        self.buf = b""
        self.done = False
        self.usage = None

    def feed(self, chunk: bytes):
        self.buf += chunk
        if len(self.buf) > 1024 * 1024:
            raise _provider_error("SSE 缓冲超过 1MB 上限（服务端未发送事件分隔符）")
        out = []
        while True:
            i_n = self.buf.find(b"\n\n")
            i_rn = self.buf.find(b"\r\n\r\n")
            cands = [(i, m) for i, m in ((i_n, 2), (i_rn, 4)) if i >= 0]
            if not cands:
                break
            i, mlen = min(cands)
            event, self.buf = self.buf[:i], self.buf[i + mlen:]
            data = []
            for line in event.replace(b"\r\n", b"\n").split(b"\n"):
                if not line or line.startswith(b":"):
                    continue
                if line.startswith(b"data:"):
                    data.append(line[5:].lstrip().decode("utf-8", "replace"))
            if not data:
                continue
            text = "\n".join(data).strip()
            if text == "[DONE]":
                self.done = True
                break
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                raise _provider_error(MSG_BAD_JSON)
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise _provider_error(f"{MSG_PROVIDER_ERROR}：{msg}" if msg else MSG_PROVIDER_ERROR)
            if obj.get("usage"):
                self.usage = obj["usage"]
            choices = obj.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                content = delta.get("content") if isinstance(delta, dict) else None
                if content:
                    out.append(str(content))
        return out


class _Handle:
    """放进 response_holder，供“停止生成”按钮关闭连接。"""

    def __init__(self, conn, resp):
        self.conn = conn
        self.resp = resp
        self.closed = False

    def close(self):
        self.closed = True
        _close_quiet(self.conn)
        try:
            self.resp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------- 对外：流式
def stream(self, messages, config, cancel_event, response_holder=None):
    endpoint = _providers.normalize_chat_endpoint(config.base_url, config.chat_path)
    key, path, proxy = _target(endpoint, config.verify_ssl)
    if proxy == "unsupported" or (proxy and key[0] != "https"):
        yield from _orig_stream(self, messages, config, cancel_event, response_holder)
        return

    payload = {
        "model": config.model,
        "messages": messages,
        "stream": True,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    apply_extras(payload, key[1], streaming=True)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _headers(config.api_key, "text/event-stream")
    note_provider(config)

    t0 = time.perf_counter()
    try:
        conn, resp, reused = _open(key, path, body, headers, float(config.timeout or 60))
    except Exception as exc:
        if cancel_event.is_set():
            return
        raise _connect_error(exc) from exc
    t_head = time.perf_counter()
    if resp.status >= 400:
        err = _http_error(resp)
        _close_quiet(conn)
        raise err

    handle = _Handle(conn, resp)
    if response_holder is not None:
        response_holder.append(handle)

    parser = _SSE()
    t_first = None
    n_chars = 0
    finished = False
    try:
        while not cancel_event.is_set():
            try:
                chunk = resp.read1(16384)
            except Exception as exc:
                if cancel_event.is_set() or handle.closed:
                    return
                raise _provider_error(f"{MSG_REQUEST_FAILED}{exc}") from exc
            if not chunk:
                finished = True
                break
            for delta in parser.feed(chunk):
                if cancel_event.is_set():
                    return
                if t_first is None:
                    t_first = time.perf_counter()
                n_chars += len(delta)
                yield delta
            if parser.done:
                finished = True
                break
    finally:
        reusable = False
        if finished and not cancel_event.is_set() and not handle.closed:
            reusable = _drain(conn, resp)
        if reusable:
            _release(key, conn)
        else:
            _close_quiet(conn)
        if getattr(_settings, "log_timing", True):
            _log_timing(config, reused, t0, t_head, t_first, n_chars, parser.usage, cancel_event.is_set())


def _drain(conn, resp) -> bool:
    try:
        if conn.sock is not None:
            conn.sock.settimeout(3)
        while resp.read1(65536):
            pass
        return not resp.will_close and conn.sock is not None
    except Exception:
        return False


def _log_timing(config, reused, t0, t_head, t_first, n_chars, usage, cancelled) -> None:
    end = time.perf_counter()
    u = usage or {}
    log.info(
        "聊天请求 model=%s 复用连接=%s 响应头=%.2fs 首字=%s 总耗时=%.2fs 字数=%d 输入token=%s 缓存命中=%s 输出token=%s%s",
        config.model,
        "是" if reused else "否",
        t_head - t0,
        f"{t_first - t0:.2f}s" if t_first else "-",
        end - t0,
        n_chars,
        u.get("prompt_tokens", "-"),
        u.get("prompt_cache_hit_tokens", "-"),
        u.get("completion_tokens", "-"),
        " (已取消)" if cancelled else "",
    )


# ---------------------------------------------------------------------- 对外：非流式 JSON（后台整理用）
_last_provider = {}
_last_provider_lock = threading.Lock()


def note_provider(config) -> None:
    """记住最近一次聊天用的服务配置（含已解析的 key），供后台记忆整理复用。"""
    if not getattr(config, "api_key", ""):
        return
    with _last_provider_lock:
        _last_provider.update(
            base_url=config.base_url,
            chat_path=config.chat_path,
            model=config.model,
            api_key=config.api_key,
            verify_ssl=bool(config.verify_ssl),
            timeout=float(config.timeout or 60),
        )


def last_provider() -> dict:
    with _last_provider_lock:
        return dict(_last_provider)


def complete_json(provider: dict, messages: list, max_tokens: int = 1800, temperature: float = 0.2, timeout: float = 120.0):
    endpoint = (_providers.normalize_chat_endpoint(provider.get("base_url"), provider.get("chat_path"))
                if _providers is not None else provider["base_url"].rstrip("/") + "/chat/completions")
    verify = bool(provider.get("verify_ssl", True))
    key, path, proxy = _target(endpoint, verify)
    payload = {
        "model": provider.get("model"),
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if is_deepseek(key[1]):
        payload["response_format"] = {"type": "json_object"}
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = _headers(provider.get("api_key", ""), "application/json")
    if proxy == "unsupported" or (proxy and key[0] != "https"):
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context(verify)) as r:
            raw = r.read()
    else:
        conn, resp, _ = _open(key, path, body, headers, timeout)
        try:
            if resp.status >= 400:
                raise _http_error(resp)
            raw = resp.read()
            if not resp.will_close:
                _release(key, conn)
                conn = None
        finally:
            if conn is not None:
                _close_quiet(conn)
    obj = json.loads(raw.decode("utf-8", "replace"))
    content = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return parse_json_loose(content), obj.get("usage") or {}


def parse_json_loose(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


# ---------------------------------------------------------------------- 预热
def prewarm(base_url: str, chat_path: str = "", verify: bool = True) -> None:
    def run():
        try:
            endpoint = (_providers.normalize_chat_endpoint(base_url, chat_path) if _providers is not None else base_url)
            key, _path, proxy = _target(endpoint, verify)
            if proxy == "unsupported" or (proxy and key[0] != "https"):
                return
            with _pool_lock:
                lst = _pool.get(key) or []
                fresh = [c for c, t in lst if c.sock is not None and time.monotonic() - t < 20]
            if fresh:
                return
            conn = _new_conn(key, 15)
            t0 = time.perf_counter()
            conn.connect()
            _release(key, conn)
            log.info("已预热到 %s 的连接（%.2fs）", key[1], time.perf_counter() - t0)
        except Exception as exc:
            log.debug("预热连接失败: %s", exc)

    threading.Thread(target=run, name="pet-memory-prewarm", daemon=True).start()
