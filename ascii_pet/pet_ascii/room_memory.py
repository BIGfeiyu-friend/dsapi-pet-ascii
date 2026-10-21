"""房间记忆（v1.0）：和桌宠本体的记忆完全分开，存在 room/room_memory.json，不会影响副人格。

三层：
  short  短期：每一步（想法 + 动作）都先进这里，带着细节，留一两天
  gists  印象：一两句话的概括，留几天（room_gist_days）后淡忘；期间被想起两次以上会升为长期
  long   长期：真正重要的事，一句话 + 证据（原始日志里的事件编号）；文字一旦写下不会被改写，只会被加强或淡出

整理在她睡着时做（副模型当“评委”给每条打重要度），但去留不由评委一个人决定，而是综合打分：
  评委的重要度、她当时的思考量（相对同类动作的 z 分数）、情绪强度、新奇程度、和主人有关、她自己说想记住、被想起过几次
分数够高 → 长期；中等 → 印象；低 → 忘掉（原始日志里永远还在，只是她不再“记得”）。
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

from .room import similarity

WEIGHTS = {"judge": 0.35, "think": 0.18, "emotion": 0.15, "novelty": 0.08, "user": 0.09, "self": 0.10,
           "recall": 0.05}
LONG_AT = 0.60          # 综合分 ≥ 这个：长期
GIST_AT = 0.32          # ≥ 这个：印象；更低：忘掉
LONG_TEXT_MAX = 80
GIST_TEXT_MAX = 60


class RoomMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = {"version": 1, "short": [], "gists": [], "long": [], "think_stats": {},
                     "last_consolidate": 0.0, "next_gid": 1, "next_lid": 1, "forgotten": 0}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data.update(raw)
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ 存取
    def save(self) -> None:
        with self.lock:
            text = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def short(self) -> list:
        return self.data["short"]

    @property
    def gists(self) -> list:
        return self.data["gists"]

    @property
    def long(self) -> list:
        return self.data["long"]

    def counts(self) -> dict:
        return {"short": len(self.short), "gists": len(self.gists), "long": len(self.long),
                "forgotten": int(self.data.get("forgotten", 0))}

    # ------------------------------------------------------------------ 思考量
    def think_signal(self, kind: str, tokens: int) -> float:
        """这次思考量在同类动作里算多还是少（0~1）。样本少时按绝对量估计。会把这次计入统计。"""
        tokens = max(0, int(tokens or 0))
        with self.lock:
            st = self.data["think_stats"].setdefault(kind, {"n": 0, "mean": 0.0, "m2": 0.0})
            n, mean, m2 = st["n"], st["mean"], st["m2"]
            if n >= 5:
                sd = math.sqrt(m2 / (n - 1)) if n > 1 else 0.0
                z = (tokens - mean) / sd if sd > 1e-6 else 0.0
                sig = (max(-2.0, min(2.0, z)) + 2.0) / 4.0
            else:
                sig = tokens / (tokens + 600.0)
            n += 1
            d = tokens - mean
            mean += d / n
            m2 += d * (tokens - mean)
            st.update(n=n, mean=round(mean, 2), m2=round(m2, 2))
            return round(sig, 3)

    # ------------------------------------------------------------------ 短期
    def add_short(self, item: dict, keep: int = 400) -> None:
        with self.lock:
            item.setdefault("recalls", 0)
            self.short.append(item)
            if len(self.short) > keep:                    # 很久没睡（没整理）时的保护：最老的直接忘掉
                drop = len(self.short) - keep
                del self.short[:drop]
                self.data["forgotten"] = int(self.data.get("forgotten", 0)) + drop

    def recent(self, now: float, hours: float = 48.0, limit: int = 12) -> list:
        with self.lock:
            return [it for it in self.short if now - it["ts"] <= hours * 3600][-limit:]

    def timeline(self, now: float, prev_keep: int = 6, cap: int = 60) -> list:
        """给她看的“最近做过的事”：今天的全部（不会因为条数多被挤掉），加上昨天（48 小时内）还没整理的最后几件。"""
        from datetime import datetime

        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self.lock:
            today = [it for it in self.short if it["ts"] >= midnight]
            prev = [it for it in self.short if now - 48 * 3600 <= it["ts"] < midnight][-prev_keep:]
        return (prev + today)[-cap:]

    def responses(self, now: float, hours: float = 48.0) -> list:
        """最近做过的事（判断“又做了一样的事”用）：短期里的每一步。"""
        return self.recent(now, hours, limit=60)

    # ------------------------------------------------------------------ 联想
    def recall(self, cues, now: float, k: int = 3, min_sim: float = 0.2, exclude: set | None = None,
               touch: bool = True) -> list:
        """和线索相似的过去（短期 / 印象 / 长期都找；每条线索分开比，取最像的）。被想起的会记一次（影响之后的去留）。"""
        cues = [c for c in ([cues] if isinstance(cues, str) else list(cues)) if str(c).strip()]
        if not cues:
            return []
        exclude = exclude or set()
        found = []
        with self.lock:
            for layer, items in (("long", self.long), ("gist", self.gists), ("short", self.short)):
                for it in items:
                    if it["id"] in exclude:
                        continue
                    text = it.get("text") or f"{it.get('what', '')} {it.get('thought', '')}"
                    s = max(similarity(c, text) for c in cues)
                    if layer == "short" and now - it["ts"] < 1800:      # 刚刚发生的不算“联想”
                        continue
                    if s >= min_sim:
                        found.append((s, layer, it))
            found.sort(key=lambda x: -x[0])
            out = []
            for s, layer, it in found[:k]:
                if touch:
                    it["recalls"] = int(it.get("recalls", 0)) + 1
                    it["last_recall"] = round(now, 1)
                out.append({"layer": layer, "sim": round(s, 3), "id": it["id"], "ts": it["ts"],
                            "text": it.get("text") or it.get("what", ""), "thought": it.get("thought", "")})
            return out

    def echo_of(self, response: str, what: str, now: float, min_sim: float, exclude: set | None = None) -> dict | None:
        """刚做的事和记忆里哪件很像（不限时间：短期按动作签名比，印象 / 长期按文字比）。不计“想起”次数。"""
        exclude = exclude or set()
        best, best_it = 0.0, None
        with self.lock:
            for it in self.short:
                if it["id"] in exclude or now - it["ts"] < 60:
                    continue
                s = similarity(response, it.get("response", ""))
                if s > best:
                    best, best_it = s, {"id": it["id"], "ts": it["ts"], "what": it.get("what", ""),
                                        "stimulus": it.get("stimulus", "")}
            for it in self.gists + self.long:
                s = similarity(what, it.get("text", ""))
                if s > best:
                    best, best_it = s, {"id": it["id"], "ts": it["ts"], "what": it.get("text", "")}
        if best_it is None or best < min_sim:
            return None
        return {**best_it, "sim": round(best, 3)}

    def long_context(self, query: str, limit: int = 8) -> list:
        with self.lock:
            scored = []
            for it in self.long:
                rel = similarity(query, it["text"]) if query else 0.0
                scored.append((rel * 2 + it.get("weight", 0.5) + 0.05 * it.get("recalls", 0), it))
            scored.sort(key=lambda x: -x[0])
            return [it for _, it in scored[:limit]]

    def gist_context(self, limit: int = 6) -> list:
        with self.lock:
            return self.gists[-limit:]

    # ------------------------------------------------------------------ 整理（睡着时）
    def due(self, now: float, min_age_h: float) -> list:
        with self.lock:
            return [it for it in self.short if now - it["ts"] >= min_age_h * 3600]

    def score(self, it: dict, judge: float) -> dict:
        parts = {"judge": max(0.0, min(1.0, float(judge))),
                 "think": float(it.get("think", 0.5)),
                 "emotion": max(0.0, min(1.0, float(it.get("intensity", 0.3)))),
                 "novelty": float(it.get("novelty", 0.5)),
                 "user": 1.0 if it.get("user_related") else 0.0,
                 "self": 1.0 if it.get("remember") else 0.0,
                 "recall": min(1.0, int(it.get("recalls", 0)) / 3.0)}
        total = sum(WEIGHTS[k] * v for k, v in parts.items())
        return {"total": round(total, 3), **{k: round(v, 3) for k, v in parts.items()}}

    def apply_consolidation(self, items: list, judgments: dict, now: float, gist_days: float = 7.0,
                            long_max: int = 200) -> dict:
        """items：这次要整理的短期条目；judgments：id → {importance, gist, long, merge_into}。返回报告。"""
        rep = {"long": [], "gist": [], "forgot": [], "merged": []}
        with self.lock:
            ids = {it["id"] for it in items}
            long_ids = {it["id"]: it for it in self.long}
            for it in items:
                j = judgments.get(it["id"]) or {}
                sc = self.score(it, j.get("importance", 0.3))
                total = sc["total"]
                base = {"from": it["id"], "score": total}
                if total >= LONG_AT:
                    target = long_ids.get(str(j.get("merge_into") or ""))
                    if target is not None:                      # 加强已有的长期记忆（不改写文字）
                        target["evidence"] = (target.get("evidence", []) + [it["id"]])[-12:]
                        target["weight"] = round(min(1.0, max(target.get("weight", 0.5), total) + 0.05), 3)
                        target["last"] = round(now, 1)
                        rep["merged"].append({**base, "into": target["id"]})
                    else:
                        text = _clip(j.get("long") or j.get("gist") or it.get("what", ""), LONG_TEXT_MAX)
                        lid = f"l{self.data['next_lid']}"
                        self.data["next_lid"] += 1
                        new = {"id": lid, "ts": it["ts"], "made": round(now, 1), "text": text, "evidence": [it["id"]],
                               "weight": total, "recalls": 0, "last": round(now, 1), "parts": sc}
                        self.long.append(new)
                        long_ids[lid] = new
                        rep["long"].append({**base, "id": lid, "text": text})
                elif total >= GIST_AT:
                    text = _clip(j.get("gist") or it.get("what", ""), GIST_TEXT_MAX)
                    gid = f"g{self.data['next_gid']}"
                    self.data["next_gid"] += 1
                    self.gists.append({"id": gid, "ts": it["ts"], "text": text, "from": [it["id"]],
                                       "until": round(now + gist_days * 86400, 1), "recalls": 0, "score": total})
                    rep["gist"].append({**base, "id": gid, "text": text})
                else:
                    rep["forgot"].append(base)
            self.data["short"] = [it for it in self.short if it["id"] not in ids]
            self.data["forgotten"] = int(self.data.get("forgotten", 0)) + len(rep["forgot"])
            self.data["last_consolidate"] = round(now, 1)
            rep.update(self.expire(now, long_max, lock=False))
        return rep

    def expire(self, now: float, long_max: int = 200, lock: bool = True) -> dict:
        """印象到期：被想起过两次以上的升为长期，其余淡忘；长期超过上限时，最弱的淡出。"""
        out = {"promoted": [], "faded": [], "dropped_long": []}

        def run():
            keep = []
            for g in self.gists:
                if g["until"] > now:
                    keep.append(g)
                elif int(g.get("recalls", 0)) >= 2:
                    lid = f"l{self.data['next_lid']}"
                    self.data["next_lid"] += 1
                    self.long.append({"id": lid, "ts": g["ts"], "made": round(now, 1), "text": g["text"],
                                      "evidence": list(g.get("from", [])), "weight": 0.55, "recalls": g["recalls"],
                                      "last": round(now, 1), "from_gist": g["id"]})
                    out["promoted"].append(lid)
                else:
                    out["faded"].append(g["id"])
            self.data["gists"] = keep
            if len(self.long) > long_max:
                def strength(it):
                    age = (now - it.get("last", it["ts"])) / 86400
                    return it.get("weight", 0.5) * 0.5 ** (age / 60) + 0.05 * it.get("recalls", 0)
                ranked = sorted(self.long, key=strength)
                drop = {it["id"] for it in ranked[:len(self.long) - long_max]}
                out["dropped_long"] = sorted(drop)
                self.data["long"] = [it for it in self.long if it["id"] not in drop]

        if lock:
            with self.lock:
                run()
        else:
            run()
        return out


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"
