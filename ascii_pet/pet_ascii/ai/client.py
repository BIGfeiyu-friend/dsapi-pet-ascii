"""模型客户端（OpenAI 兼容 Chat Completions）。只依赖标准库，便于以后替换服务商。

统一接口：complete(messages, max_tokens, temperature, timeout) -> {"text", "usage", "elapsed", "model", "finish_reason"}
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request


class ClientError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, chat_path: str = "/chat/completions",
                 json_mode: bool = True, disable_thinking: bool = True):
        base = (base_url or "https://api.deepseek.com").rstrip("/")
        path = chat_path if chat_path.startswith("/") else "/" + chat_path
        if base.endswith("/chat/completions"):
            self.endpoint = base
        elif path == "/chat/completions" and not base.rstrip("/").endswith("/v1") and "deepseek.com" not in base:
            self.endpoint = base + "/v1" + path
        else:
            self.endpoint = base + path
        self.api_key = api_key
        self.model = model
        self.json_mode = json_mode
        self.disable_thinking = disable_thinking
        self.name = model
        self._ctx = _ssl_context()
        self.is_deepseek = "deepseek.com" in self.endpoint

    def complete(self, messages: list, max_tokens: int = 1800, temperature: float = 0.9, timeout: float = 45.0,
                 extra: dict | None = None) -> dict:
        payload = {"model": self.model, "messages": messages, "max_tokens": int(max_tokens),
                   "temperature": float(temperature), "stream": False}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.disable_thinking and self.is_deepseek:
            payload["thinking"] = {"type": "disabled"}
        if extra:
            if not self.is_deepseek:
                extra = {k: v for k, v in extra.items() if k not in ("thinking", "reasoning_effort")}
            payload.update(extra)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read(1500).decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except Exception:
                pass
            raise ClientError(f"HTTP {e.code}: {str(detail)[:300]}", e.code, retryable=e.code in (429, 500, 502, 503, 504))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ClientError(f"网络错误: {getattr(e, 'reason', e)}")
        elapsed = time.perf_counter() - t0
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
            choice = obj["choices"][0]
            text = choice["message"].get("content") or ""
        except Exception:
            raise ClientError("响应格式异常: " + raw[:200].decode("utf-8", "replace"))
        # finish_reason=length：思考 + 正文超过了 max_tokens（正文可能为空或被截断），排查解析失败时要看
        return {"text": text, "usage": obj.get("usage") or {}, "elapsed": elapsed, "model": obj.get("model", self.model),
                "finish_reason": choice.get("finish_reason") or "",
                "reasoning": choice["message"].get("reasoning_content") or ""}     # 思考模式下的思考过程（房间日志里给主人看）


def make_client(cfg, api: dict | None = None):
    """按配置创建客户端；没有 Key 或 provider=mock 时返回 Mock。"""
    from .mock import MockClient
    from ..config import resolve_api

    if cfg.provider == "mock":
        return MockClient(), "mock（配置指定）"
    api = api or resolve_api(cfg)
    if not api["api_key"]:
        return MockClient(), "mock（未找到 API Key）"
    return OpenAICompatClient(api["base_url"], api["api_key"], cfg.model, cfg.chat_path, cfg.json_mode,
                              cfg.disable_thinking), f"{cfg.model} @ {api['base_url']}（Key 来自 {api['source']}）"
