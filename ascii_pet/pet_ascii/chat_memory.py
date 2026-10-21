"""聊天的记忆 → 房间（v1.1）：让房间里的她也记得和你聊过什么。

只读：SQLite 只读模式（mode=ro）+ query_only 打开桌宠的记忆库，每次用完就关；notes.md 只读文件。绝不写入。
内容和聊天时她系统提示里的“记忆快照”同源：
  长期记忆（记忆模块整理出的事实，按重要度，约 1200 字）
  设定与笔记（memory/notes.md，你写给她的）
  前情提要（最近几段情节的标题和摘要）
  最近的聊天原文（最后若干条；识屏、文件解读、粘贴的长文本只留一句“已省略”，不带进房间）
记忆库没变（文件修改时间不变）时直接用缓存，不重复查询。
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .persona_evidence import classify, memory_db_path, quotable

CATEGORY_ORDER = ["profile", "relationship", "preference", "commitment", "event", "world", "other"]
FACT_CHARS = 1200
RECAP_EPISODES = 3
RECAP_CHARS = 900
MSG_CHARS = 160


class ChatMemory:
    def __init__(self, character_id: str = "", db_path: Path | None = None, recent: int = 12):
        self.character_id = character_id
        self.db_path = Path(db_path) if db_path else None
        self.recent = recent
        self._lock = threading.Lock()
        self._cache = (None, None)          # (签名, 结果)

    def _path(self) -> Path:
        return self.db_path or memory_db_path()

    def _signature(self, p: Path) -> tuple:
        sig = []
        for q in (p, p.with_name(p.name + "-wal"), p.parent / "notes.md"):
            try:
                st = q.stat()
                sig.append((st.st_mtime, st.st_size))
            except OSError:
                sig.append(None)
        return tuple(sig) + (self.character_id, self.recent)

    def snapshot(self) -> dict:
        """{"facts": [...], "notes": str, "episodes": [...], "recent": [...], "last_ts": float}；读不到就是空的。"""
        p = self._path()
        sig = self._signature(p)
        with self._lock:
            if self._cache[0] == sig:
                return self._cache[1]
        out = {"facts": [], "notes": "", "episodes": [], "recent": [], "last_ts": 0.0}
        if p.exists():
            try:
                out.update(self._read(p))
            except sqlite3.Error:
                pass
        try:
            text = (p.parent / "notes.md").read_text(encoding="utf-8-sig")
            out["notes"] = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()[:1500]
        except OSError:
            pass
        with self._lock:
            self._cache = (sig, out)
        return out

    def _read(self, p: Path) -> dict:
        uri = "file:" + str(p).replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=1")
            cid = self.character_id
            where, args = ("character_id=? AND ", (cid,)) if cid else ("", ())
            facts = con.execute(f"SELECT id, category, content, importance FROM facts WHERE {where}status='active'"
                                " ORDER BY importance DESC, updated_at DESC LIMIT 300", args).fetchall()
            chosen, used = [], 0
            for f in facts:
                n = len(f["content"]) + 3
                if used + n > FACT_CHARS:
                    continue
                chosen.append(f)
                used += n
            order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
            chosen.sort(key=lambda f: (order.get(f["category"] or "other", len(order)), f["id"]))
            eps = con.execute(f"SELECT end_ts, title, summary FROM episodes WHERE {where}1=1"
                              " ORDER BY end_ts DESC, id DESC LIMIT ?", args + (RECAP_EPISODES,)).fetchall()
            per = max(80, RECAP_CHARS // max(1, len(eps))) if eps else 0
            rows = con.execute(f"SELECT role, content, ts FROM messages WHERE {where}deleted=0"
                               " ORDER BY ts DESC, id DESC LIMIT ?", args + (self.recent,)).fetchall()
        finally:
            con.close()
        recent = []
        prev_trust = None
        for r in reversed(rows):
            trust, why = classify(r["role"], r["content"], prev_trust)
            if r["role"] == "user":
                prev_trust = trust
            text = quotable(r["role"], r["content"])
            if trust == "untrusted":
                text = f"（{why}，内容不带进房间）"
            recent.append({"role": r["role"], "text": _clip(text, MSG_CHARS), "ts": float(r["ts"] or 0)})
        return {"facts": [f["content"] for f in chosen],
                "episodes": [{"ts": float(e["end_ts"] or 0), "title": e["title"] or "",
                              "summary": _clip(e["summary"] or "", per)} for e in reversed(eps)],
                "recent": recent, "last_ts": max((m["ts"] for m in recent), default=0.0)}

    def text(self, name: str = "", now: float | None = None) -> str:
        """给房间里的她看的“聊天的记忆”（放在系统提示里，聊天没变时前缀缓存命中）。空的就返回空字符串。"""
        s = self.snapshot()
        now = now or time.time()
        parts = []
        if s["facts"]:
            parts.append("【长期记忆】你对主人以及你们之间关系的长期记忆，由过往的聊天整理而来：\n" +
                         "\n".join(f"· {f}" for f in s["facts"]))
        if s["notes"]:
            parts.append("【设定与笔记】主人写给你、希望你一直记得的：\n" + s["notes"])
        if s["episodes"]:
            parts.append("【前情提要】更早的聊天里发生过（按时间先后）：\n" + "\n".join(
                f"· {_day(e['ts'])}" + (f"「{e['title']}」" if e["title"] else "") + e["summary"] for e in s["episodes"]))
        if s["recent"]:
            who = {"user": "主人", "assistant": "你"}
            lines = [f"{_stamp(m['ts'], now)} {who.get(m['role'], m['role'])}：{m['text']}" for m in s["recent"]
                     if m["role"] in who]
            parts.append("【最近的聊天】最后几句（原话）：\n" + "\n".join(lines))
        return "\n\n".join(parts)


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:max(1, n - 1)] + "…"


def _day(ts: float) -> str:
    if not ts:
        return "某天"
    d = datetime.fromtimestamp(ts)
    return f"{d.month}月{d.day}日"


def _stamp(ts: float, now: float) -> str:
    if not ts:
        return ""
    a, b = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    if a.date() == b.date():
        return f"今天 {a:%H:%M}"
    if (b.date() - a.date()).days == 1:
        return f"昨天 {a:%H:%M}"
    return f"{a.month}月{a.day}日 {a:%H:%M}"
