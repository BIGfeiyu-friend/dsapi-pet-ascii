"""表情进化用的对话素材：最近若干轮（用户的话、她的回复、当时的表情标签）。

存在字符画数据目录的 dialogue.json 里，重启不丢；与桌宠的聊天记录、永久记忆完全无关，
不读取也不修改它们。测试台「自创表情」页可以一键清空。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path


class DialogueLog:
    def __init__(self, path: Path, keep: int = 16):
        self.path = Path(path)
        self.rounds: deque = deque(maxlen=max(4, keep))
        self.since_evolve = 0                 # 上次表情进化之后新增的轮数（跨重启累计）
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for r in data.get("rounds") or []:
                if isinstance(r, dict):
                    self.rounds.append(r)
            self.since_evolve = int(data.get("since_evolve") or 0)
        except Exception:
            pass

    def _save(self) -> None:
        data = {"rounds": list(self.rounds), "since_evolve": self.since_evolve}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def append(self, rnd: dict) -> None:
        with self._lock:
            self.rounds.append({**rnd, "time": time.strftime("%m-%d %H:%M")})
            self.since_evolve += 1
            self._save()

    def mark_evolved(self) -> None:
        with self._lock:
            self.since_evolve = 0
            self._save()

    def clear(self) -> None:
        with self._lock:
            self.rounds.clear()
            self.since_evolve = 0
            self._save()

    def snapshot(self) -> list:
        with self._lock:
            return [{k: v for k, v in r.items() if k != "time"} for r in self.rounds]

    def __len__(self) -> int:
        return len(self.rounds)

    def __iter__(self):
        return iter(list(self.rounds))

    def __getitem__(self, i):
        return self.rounds[i]
