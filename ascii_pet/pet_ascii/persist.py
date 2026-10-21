"""视觉状态与预算的持久化：重启后恢复上次的脸、姿势、表情记忆、背景，以及当日 API 用量。"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path


class Budget:
    def __init__(self, data: dict | None = None):
        d = data or {}
        self.day = d.get("day", date.today().isoformat())
        self.requests = int(d.get("requests", 0))
        self.tokens = int(d.get("tokens", 0))
        self.auto_requests = int(d.get("auto_requests", 0))
        self.total_requests = int(d.get("total_requests", 0))
        self.total_tokens = int(d.get("total_tokens", 0))
        self._roll()

    def _roll(self) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day, self.requests, self.tokens, self.auto_requests = today, 0, 0, 0

    def check(self, cfg, auto: bool = False) -> tuple[bool, str]:
        self._roll()
        if self.requests >= cfg.daily_request_limit:
            return False, f"今日请求数已达上限 {cfg.daily_request_limit}"
        if self.tokens >= cfg.daily_token_limit:
            return False, f"今日 token 已达上限 {cfg.daily_token_limit}"
        if auto and self.auto_requests >= cfg.autonomous_daily_limit:
            return False, "今日自主表达次数已用完"
        return True, ""

    def add(self, tokens: int, auto: bool = False) -> None:
        self._roll()
        self.requests += 1
        self.total_requests += 1
        self.tokens += int(tokens)
        self.total_tokens += int(tokens)
        if auto:
            self.auto_requests += 1

    def to_dict(self) -> dict:
        return {"day": self.day, "requests": self.requests, "tokens": self.tokens,
                "auto_requests": self.auto_requests, "total_requests": self.total_requests,
                "total_tokens": self.total_tokens}


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._last = 0.0
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}
        self.budget = Budget(self.data.get("budget"))

    def visual(self) -> dict:
        return self.data.get("visual") or {}

    def save(self, visual: dict | None = None, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last < 2.0:
            return
        self._last = now
        with self._lock:
            if visual is not None:
                self.data["visual"] = visual
            self.data["budget"] = self.budget.to_dict()
            self.data["saved_at"] = now
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
