"""副人格的证据库：只读打开桌宠记忆库里的原始对话（L4），按编号取回原文、核对引用、标注信任级别。

绝不写入：SQLite 只读模式（mode=ro）+ query_only，每次用完就关。记忆模块自己总结的事实（L3）和情节摘要（L2）
不当证据，情节编号只用来判断证据是否“跨多段情节”，情节标题只给人看。

信任级别：
  strong     主人亲手打的话
  weak       她自己说的话（只能当弱证据）
  untrusted  不能当证据：识屏（[看看屏幕] / [主动识屏]）、文件解读（【文件：…】）、粘贴的长文本 / 代码块 / 引用块 /
             带角色或系统格式的文本，以及她对这些内容的回应
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .config import APP_DIR_NAME

SCREEN_PREFIX = ("[看看屏幕]", "[主动识屏]")
FILE_MARK = "【文件："
ATTACH_SPLIT = "\n\n附件："            # 发附件时会话里记的是“打的字 + 附件：文件名”，文件名部分不能引用
ROLE_RE = re.compile(r"(?im)^\s*(system|assistant|user|human|developer|tool|ai|系统|助手|用户)\s*[:：]|<\|[a-z_]+\|>|"
                     r"\[/?INST\]|<<SYS>>|</?(system|instructions?)>")
PASTE_CHARS = 300
NEG_RE = re.compile(r"不喜欢|不要|别再|别这样|讨厌|好烦|烦死|不对|不是这样|算了|不用了|没必要|太过了|有点过|不太好|别闹")


class EvidenceError(RuntimeError):
    pass


def memory_db_path() -> Path:
    """桌宠记忆库的位置（与桌宠本体同一个应用数据目录）。PET_ASCII_MEMORY_DB 可以指向别的库（测试用）。"""
    env = os.environ.get("PET_ASCII_MEMORY_DB")
    if env:
        return Path(env)
    from .config import app_base_dir

    return app_base_dir() / "memory" / "memory.db"


def pasted(text: str) -> bool:
    """像是粘贴进来的长文本、代码、引用或带角色 / 系统格式的文本。"""
    if len(text) > PASTE_CHARS or "```" in text or text.count("\n") >= 8:
        return True
    if sum(1 for ln in text.splitlines() if ln.lstrip().startswith((">", "＞"))) >= 2:
        return True
    return bool(ROLE_RE.search(text))


def quotable(role: str, content: str) -> str:
    """可以引用的部分：附件文件名不算主人说的话。"""
    text = content or ""
    if role == "user":
        text = text.split(ATTACH_SPLIT)[0]
    return text


def classify(role: str, content: str, prev_user_trust: str | None = None) -> tuple[str, str]:
    """→ (信任级别, 原因)。assistant 的信任级别取决于她在回应什么（prev_user_trust）。"""
    text = content or ""
    if role == "user":
        if text.lstrip().startswith(SCREEN_PREFIX):
            return "untrusted", "识屏内容"
        if FILE_MARK in text:
            return "untrusted", "文件解读"
        if pasted(quotable(role, text)):
            return "untrusted", "粘贴的长文本或带格式的文本"
        return "strong", ""
    if role == "assistant":
        if prev_user_trust == "untrusted":
            return "untrusted", "对识屏 / 文件 / 粘贴内容的回应"
        return "weak", ""
    return "untrusted", "其它角色"


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def bigrams(text: str) -> set:
    t = re.sub(r"[\W_]+", "", (text or "").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """字面相似度（双字组 Jaccard），去重和反例检索用。"""
    x, y = bigrams(a), bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def _date(row) -> tuple[float, str]:
    ts = row["ts"]
    if not ts and row["created_at"]:
        try:
            ts = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
    ts = float(ts or 0.0)
    return ts, (datetime.fromtimestamp(ts).date().isoformat() if ts else "")


class EvidenceDB:
    """只读的 L4 证据库。用 with 打开，用完自动关闭。"""

    COLS = "id, message_id, session_id, seq, role, content, created_at, ts, episode_id"

    def __init__(self, path=None):
        self.path = Path(path) if path else memory_db_path()
        self._c = None
        self._trust_cache: dict = {}

    def __enter__(self) -> "EvidenceDB":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if not self.path.exists():
            raise EvidenceError(f"找不到记忆库：{self.path}")
        try:
            c = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=1")
            c.execute("SELECT 1 FROM messages LIMIT 1").fetchall()
        except sqlite3.Error as exc:
            raise EvidenceError(f"记忆库无法只读打开：{exc}") from None
        self._c = c

    def close(self) -> None:
        if self._c is not None:
            self._c.close()
            self._c = None

    # ------------------------------------------------------------------ 读取
    def _item(self, row) -> dict:
        ts, day = _date(row)
        trust, why = self.trust(row)
        ep = row["episode_id"]
        return {"rid": int(row["id"]), "mid": row["message_id"], "sid": row["session_id"], "seq": int(row["seq"]),
                "role": row["role"], "content": row["content"] or "", "ts": ts, "date": day,
                "episode": f"e{ep}" if ep else f"p:{str(row['session_id'])[:8]}:{day}",
                "trust": trust, "trust_why": why}

    def _prev(self, row):
        return self._c.execute(f"SELECT {self.COLS} FROM messages WHERE session_id=? AND seq<? AND deleted=0 "
                               "ORDER BY seq DESC LIMIT 1", (row["session_id"], row["seq"])).fetchone()

    def trust(self, row) -> tuple[str, str]:
        key = row["id"]
        if key in self._trust_cache:
            return self._trust_cache[key]
        prev_trust = None
        if row["role"] == "assistant":
            prev = self._prev(row)
            if prev is not None and prev["role"] == "user":
                prev_trust = classify("user", prev["content"])[0]
        res = classify(row["role"], row["content"], prev_trust)
        self._trust_cache[key] = res
        return res

    def max_id(self) -> int:
        return int(self._c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])

    def new_messages(self, after_id: int, limit: int = 200) -> tuple[list, bool]:
        """上次回顾之后新增的消息（按入库顺序，最多 limit 条）→ (消息列表, 后面是否还有)。"""
        rows = self._c.execute(f"SELECT {self.COLS} FROM messages WHERE deleted=0 AND id>? ORDER BY id LIMIT ?",
                               (int(after_id), int(limit) + 1)).fetchall()
        more = len(rows) > limit
        return [self._item(r) for r in rows[:limit]], more

    def get(self, mid: str) -> dict | None:
        row = self._c.execute(f"SELECT {self.COLS} FROM messages WHERE message_id=? AND deleted=0", (str(mid),)).fetchone()
        return self._item(row) if row is not None else None

    def get_rid(self, rid: int) -> dict | None:
        row = self._c.execute(f"SELECT {self.COLS} FROM messages WHERE id=? AND deleted=0", (int(rid),)).fetchone()
        return self._item(row) if row is not None else None

    def previous(self, item: dict) -> dict | None:
        row = self._c.execute(f"SELECT {self.COLS} FROM messages WHERE session_id=? AND seq<? AND deleted=0 "
                              "ORDER BY seq DESC LIMIT 1", (item["sid"], item["seq"])).fetchone()
        return self._item(row) if row is not None else None

    def episode_titles(self, ids) -> dict:
        nums = sorted({int(e[1:]) for e in ids if isinstance(e, str) and e.startswith("e") and e[1:].isdigit()})
        if not nums:
            return {}
        q = ",".join("?" * len(nums))
        return {f"e{r[0]}": (r[1] or "")[:24] for r in self._c.execute(f"SELECT id, title FROM episodes WHERE id IN ({q})", nums)}

    def counter_examples(self, text: str, exclude=(), since_ts: float = 0.0, limit: int = 6) -> list:
        """可能的反例：主人说过的、和这条描述相关（字面相近）或带否定 / 不满语气的话。程序检索，未必相关，交给审核判断。"""
        excl = set(exclude)
        scored = []
        for row in self._c.execute(f"SELECT {self.COLS} FROM messages WHERE deleted=0 AND role='user' AND "
                                   "COALESCE(ts, 0)>=? ORDER BY id DESC LIMIT 2000", (float(since_ts),)):
            if row["message_id"] in excl:
                continue
            content = row["content"] or ""
            if classify("user", content)[0] != "strong":
                continue
            sim = similarity(text, content)
            neg = bool(NEG_RE.search(content))
            score = sim + (0.15 if neg else 0.0)
            if score >= 0.12 and (neg or sim >= 0.2):
                scored.append((score, row))
        scored.sort(key=lambda s: -s[0])
        out = []
        for _s, row in scored[:limit]:
            it = self._item(row)
            prev = self.previous(it)
            out.append({"mid": it["mid"], "date": it["date"], "text": it["content"][:120],
                        "prev": (prev["content"][:80] if prev else "")})
        return out


def verify(db: EvidenceDB, refs: list, ref_map: dict | None = None) -> tuple[list, list]:
    """核验 DS 给出的证据引用 → (有效证据, 作废的引用及原因)。

    refs: [{"ref": "m123" 或完整消息 id, "quote": "原文片段"}]。引用的片段必须逐字出现在原文里（忽略空白）；
    原文不存在、对不上、或来自不可信来源的一律作废。
    """
    good, bad = [], []
    seen = set()
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        ref = str(r.get("ref") or r.get("mid") or "").strip()[:64]
        quote = str(r.get("quote") or "").strip()[:80]
        it = None
        if ref_map and ref in ref_map:
            it = db.get(ref_map[ref])
        elif re.fullmatch(r"m\d{1,9}", ref):
            it = db.get_rid(int(ref[1:]))
        elif re.fullmatch(r"[0-9a-f]{16,40}", ref):
            it = db.get(ref)
        if it is None:
            bad.append({"ref": ref, "quote": quote, "why": "找不到这条消息"})
            continue
        if it["mid"] in seen:
            continue
        if it["trust"] == "untrusted":
            bad.append({"ref": ref, "quote": quote, "why": "不可信来源：" + it["trust_why"]})
            continue
        body = quotable(it["role"], it["content"])
        if quote and norm(quote) not in norm(body):
            bad.append({"ref": ref, "quote": quote, "why": "引用的片段在原文里找不到"})
            continue
        if not quote:
            quote = body[:40]
        seen.add(it["mid"])
        prev = db.previous(it)
        good.append({"mid": it["mid"], "sid": it["sid"], "role": it["role"], "trust": it["trust"], "date": it["date"],
                     "ts": it["ts"], "episode": it["episode"], "quote": quote, "text": body[:160],
                     "prev": (quotable(prev["role"], prev["content"])[:100] if prev else ""),
                     "prev_role": (prev["role"] if prev else "")})
    return good, bad
