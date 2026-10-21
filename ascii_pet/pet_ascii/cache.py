"""动画缓存：每段成功生成的动画按意图、表情、模式保存到本地，可回放、可在相似场景复用。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

_CJK = re.compile(r"[㐀-鿿]+|[A-Za-z0-9]+")


def tokens(text: str) -> set:
    out = set()
    for run in _CJK.findall(text or ""):
        if run.isascii():
            out.add(run.lower())
        else:
            out.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return out


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class AnimCache:
    def __init__(self, root: Path, max_items: int = 600):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.max_items = max_items
        self._lock = threading.Lock()
        try:
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            self.index = []

    def _flush(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.index, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def save(self, intent: str, raw: dict, meta: dict | None = None) -> dict:
        meta = meta or {}
        entry = {
            "id": uuid.uuid4().hex[:12],
            "intent": intent[:120],
            "expression": str(raw.get("expression") or "")[:60],
            "mode": raw.get("mode", 2),
            "source": meta.get("source", ""),
            "model": meta.get("model", ""),
            "tokens": meta.get("tokens", 0),
            "face": meta.get("face", ""),
            "created": time.time(),
            "uses": 0,
        }
        with self._lock:
            (self.root / f"{entry['id']}.json").write_text(
                json.dumps({"intent": intent, "raw": raw, "meta": meta}, ensure_ascii=False), encoding="utf-8")
            self.index.append(entry)
            while len(self.index) > self.max_items:
                old = self.index.pop(0)
                try:
                    (self.root / f"{old['id']}.json").unlink()
                except OSError:
                    pass
            self._flush()
        return entry

    def load(self, entry_id: str) -> dict | None:
        try:
            return json.loads((self.root / f"{entry_id}.json").read_text(encoding="utf-8"))["raw"]
        except Exception:
            return None

    def find(self, intent: str, face_key: str = "", threshold: float = 0.35):
        best, score = None, 0.0
        with self._lock:
            for e in self.index:
                if face_key and e.get("face") and e["face"] != face_key:
                    continue
                s = similarity(intent, e["intent"])
                if s > score:
                    best, score = e, s
        return (best, score) if best and score >= threshold else None

    def mark_used(self, entry_id: str) -> None:
        with self._lock:
            for e in self.index:
                if e["id"] == entry_id:
                    e["uses"] = e.get("uses", 0) + 1
            try:
                self._flush()
            except OSError:
                pass

    def recent(self, n: int = 50) -> list:
        with self._lock:
            return list(reversed(self.index[-n:]))

    def delete(self, entry_id: str) -> None:
        with self._lock:
            self.index = [e for e in self.index if e["id"] != entry_id]
            try:
                (self.root / f"{entry_id}.json").unlink()
            except OSError:
                pass
            self._flush()
