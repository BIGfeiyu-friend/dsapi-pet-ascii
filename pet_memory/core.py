"""记忆读路径（主线程，毫秒级，不调用任何模型）与写路径入口。

请求结构（为 DeepSeek 前缀缓存优化，越靠前越稳定）：
  [system]  人设提示词 + 记忆快照（核心事实 / 我的笔记 / 前情提要）   ← 仅在窗口推进或事实变化后刷新
  [L1 原文]  从 window_start 开始的会话原文                           ← 起点分块推进，不逐条滑动
  [user]    相关记忆检索结果 + 当前时间 + 用户本轮消息                ← 每轮变化，放在最后
DeepSeek 的聊天模板会把所有 system 消息提到最前面，所以动态内容只能放进最后一条 user 消息。
"""
from __future__ import annotations

import hashlib
import logging
import math
import queue
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from . import textindex as ti
from .settings import MemorySettings, app_data_dir, memory_dir, read_app_config
from .store import MemoryStore, content_text, parse_ts

log = logging.getLogger("pet.memory")

CATEGORY_ORDER = ["profile", "relationship", "preference", "commitment", "event", "world", "other"]
WEEKDAYS = "一二三四五六日"

NOTES_TEMPLATE = """<!--
在这里写下你希望桌宠永远记住的内容（世界观设定、你的称呼、禁忌话题等），
每次聊天都会放进系统提示词。HTML 注释（像这段）不会被发送。修改后下一次发消息即生效。
-->
"""


def _fmt_day(ts: float | None) -> str:
    if not ts:
        return "某天"
    d = datetime.fromtimestamp(ts)
    now = datetime.now()
    if d.year != now.year:
        return f"{d.year}年{d.month}月{d.day}日"
    return f"{d.month}月{d.day}日"


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: max(1, n - 1)] + "…"


def _gap_text(seconds: float) -> str:
    if seconds >= 86400 * 2:
        return f"{int(seconds // 86400)} 天"
    if seconds >= 86400:
        return "1 天多"
    return f"{int(seconds // 3600)} 小时"


class MemoryManager:
    def __init__(self, settings: MemorySettings):
        self.s = settings
        self.dir = memory_dir()
        self.store = MemoryStore(self.dir / "memory.db")
        self.notes_path = self.dir / "notes.md"
        if not self.notes_path.exists():
            try:
                self.notes_path.write_text(NOTES_TEMPLATE, encoding="utf-8")
            except OSError:
                pass
        self._msg_session: dict[str, str] = {}
        self._session_seen: dict[str, int] = {}
        self._last_saved: dict[str, tuple[str, str]] = {}
        self._pending_blocks: OrderedDict[int, tuple[list, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._ingest_q: queue.Queue = queue.Queue()
        self._started = False
        self._aliases_cache: tuple[float, dict] = (0.0, {})
        self._notes_cache: tuple[float, str] = (-1.0, "")
        from .worker import ConsolidationWorker

        self.worker = ConsolidationWorker(self)

    # ------------------------------------------------------------------ 线程
    def ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._ingest_loop, name="pet-memory-ingest", daemon=True).start()
        self.worker.start()

    def _ingest_loop(self) -> None:
        try:
            self._backfill()
        except Exception:
            log.exception("记忆：历史会话回填失败")
        while True:
            snap = self._ingest_q.get()
            batch = {snap["session_id"]: snap}
            try:
                while True:
                    s2 = self._ingest_q.get_nowait()
                    batch[s2["session_id"]] = s2
            except queue.Empty:
                pass
            for s in batch.values():
                try:
                    if s.get("_deleted"):
                        self.store.mark_session_deleted(s["session_id"], self.s.forget_deleted_sessions)
                    else:
                        self.store.ingest_session(s)
                except Exception:
                    log.exception("记忆：写入会话失败")
            self.worker.poke()

    def _backfill(self) -> None:
        import json

        root = app_data_dir()
        n_sessions = n_new = 0
        for path in sorted(root.glob("sessions*/*/*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                msgs = raw.get("messages") or []
                sid = str(raw.get("session_id") or path.stem)
                last_mid = msgs[-1].get("message_id") if msgs else None
                if self.store.session_synced(sid, len(msgs), last_mid):
                    continue
                snap = {
                    "session_id": sid,
                    "character_id": str(raw.get("character_id") or path.parent.name),
                    "title": raw.get("custom_title") or raw.get("title") or "",
                    "created_at": raw.get("created_at"),
                    "messages": [
                        (m.get("message_id"), m.get("role", ""), m.get("content", ""), m.get("created_at"))
                        for m in msgs
                        if isinstance(m, dict)
                    ],
                }
                n_new += self.store.ingest_session(snap)
                n_sessions += 1
            except Exception as exc:
                log.debug("记忆：跳过会话文件 %s: %s", path, exc)
        st = self.store.stats()
        log.info(
            "记忆库就绪：%s（新回填 %d 个会话 / %d 条消息；共 %d 条消息、%d 段情节、%d 条事实，待整理 %d 条）",
            self.dir, n_sessions, n_new, st["messages"], st["episodes"], st["facts"], st["pending"],
        )

    # ------------------------------------------------------------------ 写路径（主线程钩子）
    def on_session_saved(self, session) -> None:
        sid = str(session.session_id)
        cid = str(session.character_id or "")
        msgs = list(session.messages)
        tuples = [(m.message_id, m.role, m.content, m.created_at) for m in msgs]
        seen = self._session_seen.get(sid, 0)
        start = 0 if seen == 0 or seen > len(tuples) else max(0, seen - 5)
        for t in tuples[start:]:
            if t[0]:
                self._msg_session[t[0]] = sid
        self._session_seen[sid] = len(tuples)
        for t in reversed(tuples):
            if t[1] == "user":
                self._last_saved[cid] = (sid, content_text(t[2]).strip())
                break
        title = ""
        try:
            title = session.title or ""
        except Exception:
            pass
        self._ingest_q.put(
            {"session_id": sid, "character_id": cid, "title": title, "created_at": getattr(session, "created_at", None),
             "messages": tuples}
        )
        self.ensure_started()

    def on_session_deleted(self, session) -> None:
        self._ingest_q.put({"session_id": str(session.session_id), "_deleted": True})
        self.ensure_started()

    # ------------------------------------------------------------------ 读路径
    def _resolve_session(self, cid: str, history: list, user_text: str) -> str | None:
        for m in reversed(history[-4:]):
            sid = self._msg_session.get(getattr(m, "message_id", None) or "")
            if sid:
                return sid
        for m in reversed(history[-4:]):
            mid = getattr(m, "message_id", None)
            if mid:
                r = self.store.conn().execute("SELECT session_id FROM messages WHERE message_id=?", (mid,)).fetchone()
                if r:
                    return r[0]
        last = self._last_saved.get(cid)
        if last and last[1] == (user_text or "").strip():
            return last[0]
        return None

    def build(self, builder, settings, character_id, history, user_text, role_prompt):
        t0 = time.perf_counter()
        self.ensure_started()
        cid = str(character_id or "")
        history = list(history or [])
        system = builder.effective_system_prompt(settings, character_id, role_prompt)
        sid = self._resolve_session(cid, history, user_text)

        l1_max = self.s.l1_max_messages or max(4, int(getattr(settings, "history_message_limit", 40) or 40))
        l1_keep = min(self.s.l1_keep_messages or max(2, l1_max // 2), l1_max)
        char_limit = max(2000, int(getattr(settings, "history_char_limit", 24000) or 24000))

        ws = 0
        ws_id = None
        n = len(history)
        if sid and n:
            ids = [getattr(m, "message_id", "") for m in history]
            row = self.store.session_row(sid)
            if row is not None and row["window_start_id"]:
                try:
                    ws = ids.index(row["window_start_id"])
                except ValueError:
                    ws = 0
            if n - ws > l1_max:
                summarized = self.store.summarized_ids(sid)
                u = 0
                while u < n and ids[u] in summarized:
                    u += 1
                limit = min(n - l1_keep, u)
                new_ws = max(ws, limit)
                # 尽量让窗口从用户消息开始
                back = new_ws
                while back > ws and back < n and history[back].role != "user":
                    back -= 1
                if back > ws and history[back].role == "user":
                    new_ws = back
                if new_ws != ws:
                    ws = new_ws
                    self.store.set_window_start(sid, cid, ids[ws] if ws < n else None)
                    log.info("记忆：会话窗口推进到第 %d 条（共 %d 条，之前的内容已整理为情节记忆）", ws, n)
            ws_id = ids[ws] if ws < n else None

        window = history[ws:]
        total_chars = sum(len(content_text(m.content)) for m in window)
        if len(window) > l1_max * 2 or total_chars > char_limit:
            # 后台整理落后（如网络不可用）时退回原始的滑动裁剪，保证请求不会无限变长
            window = builder.trim_history(window, l1_max, char_limit)

        win_first_seq = None
        if sid and window:
            win_first_seq = self.store.seq_of(getattr(window[0], "message_id", ""))
            if win_first_seq is None:
                win_first_seq = ws

        snapshot, snap_fact_ids, snap_ep_ids = self._snapshot(cid, sid, ws_id, win_first_seq)
        sys_content = system if not snapshot else (system + "\n\n" + snapshot if system else snapshot)

        recall = self._recall(cid, sid, user_text, window, win_first_seq, snap_fact_ids, snap_ep_ids)
        user_clean = (user_text or "").strip()
        prefix = recall
        if self.s.include_time:
            now = datetime.now()
            tline = f"（现在是 {now.year}年{now.month}月{now.day}日 周{WEEKDAYS[now.weekday()]} {now:%H:%M}"
            last_ts = None
            for m in reversed(window or history):
                last_ts = parse_ts(getattr(m, "created_at", None))
                if last_ts:
                    break
            if last_ts and time.time() - last_ts > 3 * 3600:
                tline += f"，距离上一次聊天已过去 {_gap_text(time.time() - last_ts)}"
            tline += "）"
            prefix = (prefix + "\n" + tline) if prefix else tline
        user_content = f"{prefix}\n\n{user_clean}" if prefix else user_clean

        messages = [{"role": "system", "content": sys_content}]
        messages.extend({"role": m.role, "content": m.content} for m in window)
        messages.append({"role": "user", "content": user_content})
        if prefix:
            with self._lock:
                self._pending_blocks[id(messages)] = (messages, prefix)
                while len(self._pending_blocks) > 8:
                    self._pending_blocks.popitem(last=False)
        log.debug(
            "记忆：构建请求 %.1fms，窗口 %d/%d 条，快照 %d 字，检索 %d 字",
            (time.perf_counter() - t0) * 1000, len(window), n, len(snapshot), len(recall),
        )
        return messages

    def fix_multimodal(self, messages) -> None:
        """带图片的消息会被聊天窗口改写成 list 内容，这里把记忆前缀补回去。"""
        with self._lock:
            entry = self._pending_blocks.pop(id(messages), None)
        if not entry or entry[0] is not messages or not messages:
            return
        last = messages[-1]
        if isinstance(last, dict) and isinstance(last.get("content"), list):
            last["content"] = [{"type": "text", "text": entry[1]}] + list(last["content"])

    # ------------------------------------------------------------------ 快照（系统提示中的记忆）
    def _pet_name(self, cid: str) -> str:
        now = time.monotonic()
        if now - self._aliases_cache[0] > 60:
            self._aliases_cache = (now, dict(read_app_config().get("character_aliases") or {}))
        return str(self._aliases_cache[1].get(cid) or "") or "我"

    def _notes(self) -> str:
        try:
            mtime = self.notes_path.stat().st_mtime
        except OSError:
            return ""
        if mtime != self._notes_cache[0]:
            try:
                text = self.notes_path.read_text(encoding="utf-8-sig")
            except OSError:
                text = ""
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
            self._notes_cache = (mtime, text)
        return self._notes_cache[1]

    def _snapshot(self, cid, sid, ws_id, win_first_seq):
        notes = self._notes()
        key = f"{ws_id or ''}|{hashlib.md5(notes.encode()).hexdigest()[:8]}"
        row = self.store.session_row(sid) if sid else None
        if row is not None and row["snapshot"] is not None and row["snapshot_key"] == key:
            snap_at = row["snapshot_at"] or 0
            age = time.time() - snap_at
            cached = _strip_ids(row["snapshot"])
            fresh = age < self.s.snapshot_refresh_hours * 3600
            if fresh and self.store.memory_version(cid) > snap_at:
                # 有新整理出的记忆：空快照立即刷新；否则最多每 20 分钟刷新一次，避免频繁打断前缀缓存
                fresh = bool(cached) and age < 1200
            if fresh:
                fact_ids, ep_ids = _decode_ids(row["snapshot"])
                return cached, fact_ids, ep_ids
        text, fact_ids, ep_ids = self._compose_snapshot(cid, sid, win_first_seq, notes)
        if sid:
            try:
                self.store.set_snapshot(sid, cid, _encode_ids(text, fact_ids, ep_ids), key)
            except Exception:
                log.debug("记忆：快照保存失败", exc_info=True)
        return text, fact_ids, ep_ids

    def _compose_snapshot(self, cid, sid, win_first_seq, notes):
        parts = []
        fact_ids: list[int] = []
        ep_ids: list[int] = []
        name = self._pet_name(cid)

        facts = self.store.active_facts(cid, 300)
        chosen, used = [], 0
        for f in facts:  # 已按重要度降序
            line = f"· {f['content']}"
            if used + len(line) > self.s.core_fact_chars:
                continue
            chosen.append(f)
            used += len(line) + 1
        if chosen:
            order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
            chosen.sort(key=lambda f: (order.get(f["category"] or "other", len(order)), f["id"]))
            fact_ids = [f["id"] for f in chosen]
            parts.append(
                "【长期记忆】以下是你对用户以及你们之间关系的长期记忆，由过往对话整理而来。"
                "请像真正记得一样自然地运用，不要逐条复述；若与用户最新的说法冲突，以最新说法为准。\n"
                + "\n".join(f"· {f['content']}" for f in chosen)
            )
        if notes:
            parts.append("【设定与笔记】\n" + notes)

        k = max(0, self.s.recap_episodes)
        if k:
            eps = self.store.recent_episodes(cid, k, exclude_session=sid, exclude_from_seq=win_first_seq)
            if eps:
                per = max(80, self.s.recap_chars // max(1, len(eps)))
                lines = []
                for e in reversed(eps):
                    ep_ids.append(e["id"])
                    head = f"{_fmt_day(e['end_ts'])}「{e['title']}」" if e["title"] else _fmt_day(e["end_ts"])
                    lines.append(f"· {head}{_clip(e['summary'], per)}")
                parts.append("【前情提要】更早的对话里发生过（按时间先后）：\n" + "\n".join(lines))
        if not parts:
            return "", [], []
        if name and name != "我":
            parts[0] = parts[0].replace("你对用户", f"你（{name}）对用户", 1)
        return "\n\n".join(parts), fact_ids, ep_ids

    # ------------------------------------------------------------------ 动态检索
    def _recall(self, cid, sid, user_text, window, win_first_seq, snap_fact_ids, snap_ep_ids) -> str:
        text = (user_text or "").strip()
        q_text = text
        if len(text) < 8 and window:
            for m in reversed(window):
                if m.role == "assistant":
                    q_text = text + " " + content_text(m.content)[:200]
                    break
        qt = ti.query_tokens(q_text)
        fq = ti.fts_query(qt)
        if not fq:
            return ""
        min_ov = ti.min_overlap(qt)
        tw = max(1, ti.total_weight(qt))
        now = time.time()
        budget = self.s.recall_chars
        lines: list[str] = []

        def score(ov, importance, ts):
            age_days = max(0.0, (now - (ts or now)) / 86400)
            return min(1.0, ov / tw) * 0.6 + (importance or 0.3) * 0.25 + math.exp(-age_days / 30) * 0.15

        # L3 事实
        fact_hits = []
        hit_ids = [r["rowid"] for r in self.store.search("facts", fq, 40)]
        skip = set(snap_fact_ids)
        for f in self.store.rows_by_ids("facts", [i for i in hit_ids if i not in skip]):
            if f["character_id"] != cid or f["status"] != "active":
                continue
            ov = ti.overlap(qt, f["fkey"].replace(".", " ") + " " + f["content"])
            if ov >= min_ov:
                fact_hits.append((score(ov, f["importance"], f["updated_at"]), f))
        fact_hits.sort(key=lambda x: -x[0])

        # L2 情节
        ep_hits = []
        hit_ids = [r["rowid"] for r in self.store.search("episodes", fq, 40)]
        skip = set(snap_ep_ids)
        for e in self.store.rows_by_ids("episodes", [i for i in hit_ids if i not in skip]):
            if e["character_id"] != cid:
                continue
            if sid and e["session_id"] == sid and win_first_seq is not None and (e["last_seq"] or 0) >= win_first_seq:
                continue  # 原文还在窗口里
            ov = ti.overlap(qt, " ".join([e["title"] or "", e["summary"] or "", e["keywords"] or "", e["details"] or ""]))
            if ov >= min_ov:
                ep_hits.append((score(ov, e["importance"], e["end_ts"]), e))
        ep_hits.sort(key=lambda x: -x[0])

        # L4 原始消息：只在“还记得/上次…”或没有其他命中时才查
        raw_hits = []
        if self.s.raw_recall_max > 0 and (ti.has_recall_cue(text) or (not fact_hits and not ep_hits and len(qt) >= 3)):
            window_ids = {getattr(m, "message_id", "") for m in window}
            hit_ids = [r["rowid"] for r in self.store.search("messages", fq, 60)]
            for m in self.store.rows_by_ids("messages", hit_ids):
                if m["character_id"] != cid or m["message_id"] in window_ids:
                    continue
                ov = ti.overlap(qt, m["content"])
                if ov >= 3 or (ov >= 2 and ti.has_recall_cue(text)):
                    raw_hits.append((score(ov, 0.4, m["ts"]), m))
            raw_hits.sort(key=lambda x: -x[0])

        used_facts = []
        for _, f in fact_hits[:5]:
            line = f"· {f['content']}"
            if len(line) > budget:
                break
            lines.append(line)
            used_facts.append(f["id"])
            budget -= len(line)
        for _, e in ep_hits[:3]:
            head = f"{_fmt_day(e['end_ts'])}「{e['title']}」" if e["title"] else _fmt_day(e["end_ts"])
            line = f"· {head}{_clip(e['summary'], min(260, budget))}"
            if len(line) > budget or budget < 60:
                break
            lines.append(line)
            budget -= len(line)
        name = self._pet_name(cid)
        for _, m in raw_hits[: self.s.raw_recall_max]:
            who = "用户" if m["role"] == "user" else (name if name != "我" else "我")
            line = f"· {_fmt_day(m['ts'])} {who}说过：“{_clip(m['content'], min(160, budget))}”"
            if len(line) > budget or budget < 40:
                break
            lines.append(line)
            budget -= len(line)
        if used_facts:
            self.store.touch_facts(used_facts)
        if not lines:
            return ""
        return "【相关记忆】（根据当前话题自动想起的往事，仅供参考，自然地用上即可，不要提及“记忆检索”）\n" + "\n".join(lines)


# 快照里附带用到的事实/情节 id，供检索去重（存库时附加在末尾，使用时剥离）
_ID_MARK = "\n\u0000ids:"


def _encode_ids(text: str, fact_ids, ep_ids) -> str:
    return f"{text}{_ID_MARK}{','.join(map(str, fact_ids))};{','.join(map(str, ep_ids))}"


def _decode_ids(stored: str):
    if _ID_MARK not in stored:
        return [], []
    tail = stored.rsplit(_ID_MARK, 1)[1]
    f, _, e = tail.partition(";")
    return [int(x) for x in f.split(",") if x], [int(x) for x in e.split(",") if x]


def _strip_ids(stored: str) -> str:
    return stored.rsplit(_ID_MARK, 1)[0] if _ID_MARK in stored else stored
