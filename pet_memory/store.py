"""SQLite 记忆库：L4 原始消息、L2 情节、L3 事实、会话窗口状态。

多个桌宠进程可能同时打开同一个库：WAL + busy_timeout，后台整理用租约保证只有一个进程在跑。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

from . import textindex as ti

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  message_id TEXT UNIQUE NOT NULL,
  session_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  chash INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  ts REAL,
  deleted INTEGER NOT NULL DEFAULT 0,
  episode_id INTEGER,
  ingested_at REAL
);
CREATE INDEX IF NOT EXISTS ix_msg_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_msg_pending ON messages(episode_id, deleted, session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(tok, tokenize='unicode61 remove_diacritics 0');

CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY,
  character_id TEXT,
  title TEXT,
  created_at TEXT,
  last_ts REAL,
  window_start_id TEXT,
  snapshot TEXT,
  snapshot_key TEXT,
  snapshot_at REAL,
  deleted INTEGER NOT NULL DEFAULT 0,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS episodes(
  id INTEGER PRIMARY KEY,
  character_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  first_message_id TEXT,
  last_message_id TEXT,
  first_seq INTEGER,
  last_seq INTEGER,
  start_ts REAL,
  end_ts REAL,
  msg_count INTEGER,
  title TEXT,
  summary TEXT,
  details TEXT,
  keywords TEXT,
  importance REAL,
  created_at REAL,
  model TEXT
);
CREATE INDEX IF NOT EXISTS ix_ep_char ON episodes(character_id, end_ts);
CREATE INDEX IF NOT EXISTS ix_ep_session ON episodes(session_id, last_seq);
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(tok, tokenize='unicode61 remove_diacritics 0');

CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY,
  character_id TEXT NOT NULL,
  fkey TEXT NOT NULL,
  category TEXT,
  content TEXT NOT NULL,
  importance REAL,
  status TEXT NOT NULL DEFAULT 'active',
  source_episode INTEGER,
  supersedes INTEGER,
  created_at REAL,
  updated_at REAL,
  hits INTEGER NOT NULL DEFAULT 0,
  last_hit REAL
);
CREATE INDEX IF NOT EXISTS ix_fact_key ON facts(character_id, fkey, status);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(tok, tokenize='unicode61 remove_diacritics 0');

CREATE TABLE IF NOT EXISTS lease(id INTEGER PRIMARY KEY CHECK (id = 1), owner TEXT, expires REAL);
"""


def parse_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def chash(text: str) -> int:
    return zlib.crc32(text.encode("utf-8", "replace"))


def content_text(content) -> str:
    """ChatMessage.content 可能是字符串，也可能是多模态 list。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
            elif isinstance(p, dict):
                parts.append("[图片]")
        return "\n".join(parts)
    return "" if content is None else str(content)


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        c = self.conn()
        c.executescript(_SCHEMA)
        c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        r = c.execute("SELECT value FROM meta WHERE key='tok_version'").fetchone()
        if r is None or r[0] != str(ti.TOK_VERSION):
            self._reindex()

    def _reindex(self) -> None:
        """分词规则变化后重建全文索引（源数据都在主表里）。"""
        with self.tx() as c:
            for fts in ("messages_fts", "episodes_fts", "facts_fts"):
                c.execute(f"DELETE FROM {fts}")
            for r in c.execute("SELECT id, content FROM messages").fetchall():
                c.execute("INSERT INTO messages_fts(rowid, tok) VALUES(?, ?)", (r[0], ti.index_text(r[1])))
            for e in c.execute("SELECT id, title, summary, keywords, details FROM episodes").fetchall():
                blob = " ".join(str(x or "") for x in (e[1], e[2], e[3], e[4]))
                c.execute("INSERT INTO episodes_fts(rowid, tok) VALUES(?, ?)", (e[0], ti.index_text(blob)))
            for f in c.execute("SELECT id, fkey, content FROM facts WHERE status='active'").fetchall():
                c.execute("INSERT INTO facts_fts(rowid, tok) VALUES(?, ?)",
                          (f[0], ti.index_text(f[1].replace(".", " ") + " " + f[2])))
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('tok_version', ?)", (str(ti.TOK_VERSION),))

    # ------------------------------------------------------------------ 连接/事务
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=15000")
            self._local.conn = c
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    # ------------------------------------------------------------------ L4 写入
    def ingest_session(self, snap: dict) -> int:
        """把会话当前完整消息列表同步进库。返回新增消息数。"""
        sid = snap["session_id"]
        cid = snap["character_id"] or ""
        msgs = snap["messages"]
        now = time.time()
        added = 0
        with self.tx() as c:
            existing = {
                r["message_id"]: r
                for r in c.execute(
                    "SELECT id, message_id, seq, chash, deleted FROM messages WHERE session_id=?", (sid,)
                )
            }
            present = set()
            last_ts = None
            for seq, (mid, role, content, created) in enumerate(msgs):
                text = content_text(content)
                mid = str(mid or f"{sid}:{seq}:{chash(text)}")
                present.add(mid)
                h = chash(text)
                ts = parse_ts(created)
                if ts:
                    last_ts = max(last_ts or 0, ts)
                row = existing.get(mid)
                if row is None:
                    other = c.execute("SELECT id, session_id FROM messages WHERE message_id=?", (mid,)).fetchone()
                    if other is not None:
                        # 同一消息被复制到别的会话（如分叉/导入）：保持首个来源，不重复入库
                        continue
                    cur = c.execute(
                        "INSERT INTO messages(message_id, session_id, character_id, seq, role, content, chash,"
                        " created_at, ts, deleted, ingested_at) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                        (mid, sid, cid, seq, str(role), text, h, created, ts, now),
                    )
                    c.execute("INSERT INTO messages_fts(rowid, tok) VALUES(?, ?)", (cur.lastrowid, ti.index_text(text)))
                    added += 1
                else:
                    if row["chash"] != h:
                        c.execute(
                            "UPDATE messages SET content=?, chash=?, seq=?, deleted=0 WHERE id=?",
                            (text, h, seq, row["id"]),
                        )
                        c.execute("DELETE FROM messages_fts WHERE rowid=?", (row["id"],))
                        c.execute("INSERT INTO messages_fts(rowid, tok) VALUES(?, ?)", (row["id"], ti.index_text(text)))
                    elif row["seq"] != seq or row["deleted"]:
                        c.execute("UPDATE messages SET seq=?, deleted=0 WHERE id=?", (seq, row["id"]))
            gone = [r["id"] for m, r in existing.items() if m not in present and not r["deleted"]]
            for rid in gone:
                c.execute("UPDATE messages SET deleted=1 WHERE id=?", (rid,))
            c.execute(
                "INSERT INTO sessions(session_id, character_id, title, created_at, last_ts, updated_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET"
                " character_id=excluded.character_id, title=excluded.title,"
                " last_ts=COALESCE(MAX(COALESCE(sessions.last_ts,0), COALESCE(excluded.last_ts,0)), excluded.last_ts),"
                " deleted=0, updated_at=excluded.updated_at",
                (sid, cid, snap.get("title") or "", snap.get("created_at"), last_ts, now),
            )
        return added

    def session_synced(self, sid: str, count: int, last_mid: str | None) -> bool:
        c = self.conn()
        n = c.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND deleted=0", (sid,)).fetchone()[0]
        if n != count:
            return False
        if count == 0:
            return True
        r = c.execute("SELECT 1 FROM messages WHERE message_id=? AND session_id=? AND deleted=0", (last_mid, sid)).fetchone()
        return r is not None

    def mark_session_deleted(self, sid: str, forget: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET deleted=1, updated_at=? WHERE session_id=?", (time.time(), sid))
            if forget:
                ids = [r[0] for r in c.execute("SELECT id FROM messages WHERE session_id=?", (sid,))]
                for rid in ids:
                    c.execute("DELETE FROM messages_fts WHERE rowid=?", (rid,))
                c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                eps = [r[0] for r in c.execute("SELECT id FROM episodes WHERE session_id=?", (sid,))]
                for eid in eps:
                    c.execute("DELETE FROM episodes_fts WHERE rowid=?", (eid,))
                c.execute("DELETE FROM episodes WHERE session_id=?", (sid,))
                c.execute("DELETE FROM sessions WHERE session_id=?", (sid,))

    # ------------------------------------------------------------------ 会话窗口状态
    def session_row(self, sid: str):
        return self.conn().execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()

    def set_window_start(self, sid: str, cid: str, mid: str | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sessions(session_id, character_id, window_start_id, updated_at) VALUES(?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET window_start_id=excluded.window_start_id,"
                " updated_at=excluded.updated_at",
                (sid, cid, mid, time.time()),
            )

    def set_snapshot(self, sid: str, cid: str, text: str, key: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO sessions(session_id, character_id, snapshot, snapshot_key, snapshot_at, updated_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET snapshot=excluded.snapshot,"
                " snapshot_key=excluded.snapshot_key, snapshot_at=excluded.snapshot_at",
                (sid, cid, text, key, time.time(), time.time()),
            )

    def summarized_ids(self, sid: str) -> set[str]:
        return {
            r[0]
            for r in self.conn().execute(
                "SELECT message_id FROM messages WHERE session_id=? AND episode_id IS NOT NULL", (sid,)
            )
        }

    def seq_of(self, mid: str) -> int | None:
        r = self.conn().execute("SELECT seq FROM messages WHERE message_id=?", (mid,)).fetchone()
        return None if r is None else r[0]

    # ------------------------------------------------------------------ 后台整理：待处理消息
    def pending_sessions(self):
        return self.conn().execute(
            "SELECT session_id, character_id, COUNT(*) AS n, MAX(ts) AS last_ts FROM messages"
            " WHERE episode_id IS NULL AND deleted=0 AND role IN ('user','assistant')"
            " GROUP BY session_id ORDER BY MIN(ts)"
        ).fetchall()

    def pending_messages(self, sid: str, limit: int):
        return self.conn().execute(
            "SELECT id, message_id, seq, role, content, ts FROM messages"
            " WHERE session_id=? AND episode_id IS NULL AND deleted=0 AND role IN ('user','assistant')"
            " ORDER BY seq LIMIT ?",
            (sid, limit),
        ).fetchall()

    def try_lease(self, owner: str, ttl: float = 90.0) -> bool:
        now = time.time()
        with self.tx() as c:
            r = c.execute("SELECT owner, expires FROM lease WHERE id=1").fetchone()
            if r is None or r["expires"] < now or r["owner"] == owner:
                c.execute(
                    "INSERT INTO lease(id, owner, expires) VALUES(1,?,?)"
                    " ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, expires=excluded.expires",
                    (owner, now + ttl),
                )
                return True
        return False

    def release_lease(self, owner: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM lease WHERE id=1 AND owner=?", (owner,))

    # ------------------------------------------------------------------ L2 / L3 写入
    def save_episode(self, cid: str, sid: str, rows, data: dict, fact_ops: list, model: str) -> int:
        now = time.time()
        ts_values = [r["ts"] for r in rows if r["ts"]]
        details = {
            "events": data.get("events") or [],
            "relationship": data.get("relationship") or "",
            "commitments": data.get("commitments") or [],
            "open_items": data.get("open_items") or [],
        }
        keywords = " ".join(str(k) for k in (data.get("keywords") or []))
        title = str(data.get("title") or "").strip()[:40]
        summary = str(data.get("summary") or "").strip()
        try:
            importance = max(0.0, min(1.0, float(data.get("importance", 0.3))))
        except Exception:
            importance = 0.3
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO episodes(character_id, session_id, first_message_id, last_message_id, first_seq, last_seq,"
                " start_ts, end_ts, msg_count, title, summary, details, keywords, importance, created_at, model)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, sid, rows[0]["message_id"], rows[-1]["message_id"], rows[0]["seq"], rows[-1]["seq"],
                    min(ts_values) if ts_values else now, max(ts_values) if ts_values else now, len(rows),
                    title, summary, json.dumps(details, ensure_ascii=False), keywords, importance, now, model,
                ),
            )
            eid = cur.lastrowid
            blob = " ".join([title, summary, keywords] + [str(x) for v in details.values() for x in (v if isinstance(v, list) else [v])])
            c.execute("INSERT INTO episodes_fts(rowid, tok) VALUES(?, ?)", (eid, ti.index_text(blob)))
            c.executemany("UPDATE messages SET episode_id=? WHERE id=?", [(eid, r["id"]) for r in rows])
            for op in fact_ops:
                self._apply_fact(c, cid, eid, op, now)
        return eid

    def _apply_fact(self, c, cid: str, eid: int, op: dict, now: float) -> None:
        if not isinstance(op, dict):
            return
        key = str(op.get("key") or "").strip()[:80]
        content = str(op.get("content") or "").strip()[:300]
        action = str(op.get("op") or "add").strip().lower()
        if not key:
            return
        cur = c.execute(
            "SELECT id, content FROM facts WHERE character_id=? AND fkey=? AND status='active'", (cid, key)
        ).fetchone()
        if action == "delete":
            if cur is not None:
                c.execute("UPDATE facts SET status='deleted', updated_at=? WHERE id=?", (now, cur["id"]))
            return
        if not content:
            return
        if cur is not None and cur["content"] == content:
            return
        try:
            importance = max(0.0, min(1.0, float(op.get("importance", 0.5))))
        except Exception:
            importance = 0.5
        category = str(op.get("category") or "other").strip()[:20]
        supersedes = None
        if cur is not None:
            c.execute("UPDATE facts SET status='superseded', updated_at=? WHERE id=?", (now, cur["id"]))
            supersedes = cur["id"]
        new = c.execute(
            "INSERT INTO facts(character_id, fkey, category, content, importance, status, source_episode, supersedes,"
            " created_at, updated_at) VALUES(?,?,?,?,?,'active',?,?,?,?)",
            (cid, key, category, content, importance, eid, supersedes, now, now),
        )
        c.execute("INSERT INTO facts_fts(rowid, tok) VALUES(?, ?)", (new.lastrowid, ti.index_text(key.replace(".", " ") + " " + content)))

    # ------------------------------------------------------------------ 读取
    def active_facts(self, cid: str, limit: int = 200):
        return self.conn().execute(
            "SELECT id, fkey, category, content, importance, updated_at FROM facts"
            " WHERE character_id=? AND status='active' ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (cid, limit),
        ).fetchall()

    def recent_episodes(self, cid: str, limit: int, exclude_session: str | None = None, exclude_from_seq: int | None = None):
        rows = self.conn().execute(
            "SELECT * FROM episodes WHERE character_id=? ORDER BY end_ts DESC, id DESC LIMIT ?",
            (cid, limit + 12),
        ).fetchall()
        out = []
        for r in rows:
            if exclude_session and r["session_id"] == exclude_session and exclude_from_seq is not None:
                if r["last_seq"] is not None and r["last_seq"] >= exclude_from_seq:
                    continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def memory_version(self, cid: str) -> float:
        r = self.conn().execute(
            "SELECT MAX(x) FROM (SELECT MAX(updated_at) AS x FROM facts WHERE character_id=?"
            " UNION ALL SELECT MAX(created_at) FROM episodes WHERE character_id=?)",
            (cid, cid),
        ).fetchone()
        return float(r[0] or 0) if r else 0.0

    def search(self, table: str, q: str, limit: int):
        fts = f"{table}_fts"
        try:
            return self.conn().execute(
                f"SELECT rowid, bm25({fts}) AS score FROM {fts} WHERE {fts} MATCH ? ORDER BY score LIMIT ?",
                (q, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def rows_by_ids(self, table: str, ids: list[int]):
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.conn().execute(f"SELECT * FROM {table} WHERE id IN ({marks})", ids).fetchall()

    def touch_facts(self, ids: list[int]) -> None:
        if not ids:
            return
        try:
            with self.tx() as c:
                c.executemany("UPDATE facts SET hits=hits+1, last_hit=? WHERE id=?", [(time.time(), i) for i in ids])
        except sqlite3.OperationalError:
            pass

    def stats(self) -> dict:
        c = self.conn()
        return {
            "messages": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "episodes": c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0],
            "facts": c.execute("SELECT COUNT(*) FROM facts WHERE status='active'").fetchone()[0],
            "pending": c.execute(
                "SELECT COUNT(*) FROM messages WHERE episode_id IS NULL AND deleted=0 AND role IN ('user','assistant')"
            ).fetchone()[0],
        }
