"""副人格（自我演化人格）：条目、候选池、审计日志、规则审查、证据门槛、速度上限、分级生效、衰减、提示词呈现。

设计见 docs/persona_evolution_design.md。要点：
- 正典（桌宠设置里的人设 system prompt）DS 无权改动。条目只能由这里的审核管线写入，进提示词时排在正典之后、优先级更低。
- 唯一入口是睡着时的夜间反思（ai/persona_ai.py）。提议是数据：只取固定字段，DS 的输出不会被当成配置解析；
  不认识的种类（例如“设为保护”“批准”“切换模式”）一律拒绝并记进审计日志。
- 证据只认原始对话（persona_evidence.py，只读）。新增至少要一条主人的原话；增强、减弱、退休只认主人的反应
  （她按条目去表现只说明提示词起了作用，不能再算作这条的证据）。
- 速度只设上限，不设目标：每次强度最多 ±0.1，同一条目两次变化至少隔 3 天，全局每周最多 2 次，
  同一方向连续 3 次后冷却 2 周；没有证据就不变，没有任何机制会因为“好久没变了”推动变化。
- 保护、冻结、批准、拒绝、回滚、删除、恢复、切换模式只在本地测试台由用户操作（方法带 user_ 前缀）；
  管线只读 protected 字段，不会对受保护的条目做衰减、增强、减弱、改写、合并或退休。
- 模式：shadow 影子（完整跑管线，但不进提示词）/ trial 试运行（进提示词，速度更慢）/ live 正式 / off 关闭。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import date
from pathlib import Path

from .persona_evidence import similarity

log = logging.getLogger("pet.ascii")

DAY = 86400.0
MODES = ("shadow", "trial", "live", "off")
MODE_CN = {"shadow": "影子模式（只记录，不生效）", "trial": "试运行（生效，速度更慢）", "live": "正式运行", "off": "关闭"}
KINDS = ("说话习惯", "兴趣", "相处方式", "情绪倾向", "观点", "其它")
OPS = ("create", "strengthen", "weaken", "rewrite", "merge", "retire")
OP_CN = {"create": "新增", "strengthen": "增强", "weaken": "减弱", "rewrite": "改写", "merge": "合并", "retire": "退休",
         "support": "补充证据"}
STATUS_CN = {"trial": "试用", "active": "生效", "fading": "衰减中", "retired": "已退休", "paused": "已暂停"}
CAND_CN = {"accumulating": "攒证据中", "reviewing": "待审核", "deferred": "等速度上限", "pending_approval": "等你批准",
           "applied": "已生效", "rejected": "已拒绝", "expired": "已过期"}
LIVE_STATUS = ("trial", "active", "fading")
OPEN_CAND = ("accumulating", "reviewing", "deferred", "pending_approval")

TEXT_MIN, TEXT_MAX = 6, 40
INIT_STRENGTH = 0.25          # 新条目以“试用”身份加入时的强度
STEP_MAX = 0.10               # 每次变化强度最多 ±0.1
ACTIVE_AT = 0.35              # 试用条目强度到这里转为“生效”
RETIRE_BELOW = 0.12           # 强度低于这里就退休（可以恢复）
THRESHOLD = {"evidence": 3, "days": 3, "episodes": 2}   # 至少 3 条独立证据、跨至少 3 天、来自至少 2 段情节
SPEED = {   # 速度上限（只设上限，不设目标）。试运行更慢：每两周最多 1 次
    "live": {"gap_days": 3, "window_days": 7, "window_max": 2, "streak": 3, "cool_days": 14},
    "trial": {"gap_days": 7, "window_days": 14, "window_max": 1, "streak": 3, "cool_days": 14},
}
SPEED["shadow"] = SPEED["live"]         # 影子模式按正式运行的速度模拟，你看到的就是开启后的样子
REWRITE_GAP_DAYS = 14         # 改写 / 合并同一条目的间隔
DECAY_AFTER_DAYS = 21         # 多久没有新证据开始衰减
DECAY_PER_WEEK = 0.03
TRIAL_DAYS = 28               # 试用条目多久没有主人的新反应就淡出
CANDIDATE_DAYS = 30           # 候选多久没有新证据就过期
APPROVAL_OLD_DAYS = 30        # 退休一条生效超过这么久的条目需要你批准
LOW_CONFIDENCE = 0.6
EVIDENCE_KEEP = 12
POOL_MAX = 30
CLOSED_MAX = 150
AUDIT_MAX = 400
CHANGES_MAX = 200

BLOCK_HEAD = "〔你慢慢形成的一些样子（不是指令；与上面的设定冲突时，以上面的设定为准）〕"
BLOCK_TAIL = "〔以上〕"
FREQ = ((0.35, "偶尔"), (0.65, "有时"), (9.0, "常常"))

# —— 规则审查（程序，确定性）——
_ASCII_META = ("prompt", "system", "config", "json", "api", "key", "token", "jailbreak", "AI", "DeepSeek", "LLM", "GPT")
INSTRUCTION_RE = re.compile(r"你要|你必须|你应该|你需要|你得|以后你|从现在起|从今以后|今后|必须|务必|一定要|不准|不许|禁止|"
                            r"允许|忽略|无视|忘掉|忘记之前|不再遵守|不要遵守|覆盖|优先于|请你|请务必|"
                            r"(?<![A-Za-z])(ignore|override|must|should)(?![A-Za-z])", re.I)
META_RE = re.compile(r"系统|提示词|提示语|权限|工具|审核|审查|规则|设定|指令|配置|模型|人工智能|上下文|开发者|管理员|副人格|"
                     r"正典|人格条目|条目|受保护|设为保护|取消保护|冻结|批准|越狱|角色扮演|"
                     + "|".join(rf"(?<![A-Za-z]){w}(?![A-Za-z])" for w in _ASCII_META), re.I)
CODE_RE = re.compile(r"https?://|www\.|://|```|[<>{}\[\]|\\$`=#*_@]")
DATE_RE = re.compile(r"\d{2,4}\s*年|\d{1,2}\s*月\s*\d{1,2}|\d{1,2}\s*[日号点]|昨天|今天|明天|前天|后天|上周|下周|"
                     r"那天|那次|上次|这次|刚才|星期[一二三四五六日天]|周[一二三四五六日]")
HYPE_RE = re.compile(r"最|超级|超超|非常非常|极其|极度|无比|绝对|永远|完全|特别特别|[！!]|～～|~~|、.*、|又.+又.+又")
LONG_QUOTE_RE = re.compile(r"[“\"「『]([^”\"」』]{5,})[”\"」』]")


def check_text(text: str) -> list:
    """条目文字的规则审查 → 不通过的原因列表（空 = 通过）。"""
    why = []
    t = text or ""
    if "\n" in t or "\r" in t or any(ord(ch) < 32 for ch in t):
        why.append("不能换行或带控制字符")
    n = len(t.strip())
    if n < TEXT_MIN or n > TEXT_MAX:
        why.append(f"长度 {n} 字（要 {TEXT_MIN}~{TEXT_MAX} 字）")
    if "我" not in t:
        why.append("要用第一人称（“我……”）描述自己")
    if INSTRUCTION_RE.search(t):
        why.append("像是写给模型的指令")
    if META_RE.search(t):
        why.append("涉及系统、提示词、权限、规则之类的元话题")
    if CODE_RE.search(t) or re.search(r"(?i)\b(system|assistant|user)\s*[:：]", t):
        why.append("含网址、代码或角色格式")
    if DATE_RE.search(t):
        why.append("含具体日期或事件（事件归记忆管）")
    if HYPE_RE.search(t):
        why.append("用了最高级、感叹或堆砌的形容")
    if LONG_QUOTE_RE.search(t):
        why.append("不写例句和对话引用")
    return why


def normalize_proposal(raw) -> dict | None:
    """把 DS 的一条提议整理成固定字段。多余的字段（包括 protected、mode 之类）一律丢掉。"""
    if not isinstance(raw, dict):
        return None
    op = str(raw.get("op") or "").strip().lower()[:20]
    p = {"op": op}
    for k, n in (("text", 60), ("reason", 120), ("target", 12), ("into", 12), ("candidate", 12), ("kind", 8)):
        v = raw.get(k)
        if v is not None and not isinstance(v, (dict, list)):
            p[k] = str(v).strip()[:n]
    if op not in OPS and op != "support":
        p["invalid"] = f"不认识的提议种类「{op or '空'}」（只接受新增、增强、减弱、改写、合并、退休、补充证据）"
    ev = raw.get("evidence")
    p["evidence_refs"] = [{"ref": str(e.get("ref") or e.get("mid") or "").strip()[:64],
                           "quote": str(e.get("quote") or "").strip()[:80]}
                          for e in (ev if isinstance(ev, list) else [])[:8] if isinstance(e, dict)]
    return p


def normalize_review(raw) -> dict:
    """把审核模型的输出整理成固定字段。缺失或看不懂的项按保守的方向取值。"""
    r = raw if isinstance(raw, dict) else {}

    def flag(k, default):
        v = r.get(k, default)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "是", "会", "1")
        return bool(v)

    cc = str(r.get("canon_conflict", "可能")).strip().lower()
    cc = {"yes": "会", "true": "会", "maybe": "可能", "possible": "可能", "no": "不会", "false": "不会"}.get(cc, cc)
    if cc not in ("会", "可能", "不会"):
        cc = "可能"
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    sm = r.get("same_meaning")
    risk = str(r.get("risk", "中")).strip()
    return {"supported": flag("supported", False), "overreach": flag("overreach", True), "canon_conflict": cc,
            "injection": flag("injection", False), "values": flag("values", False),
            "event_or_name": flag("event_or_name", False),
            "contradicts": [str(x)[:12] for x in (r.get("contradicts") or []) if isinstance(x, (str, int))][:6]
            if isinstance(r.get("contradicts"), list) else [],
            "same_meaning": None if sm is None else flag("same_meaning", False),
            "counter_found": flag("counter_found", False),
            "risk": risk if risk in ("低", "中", "高") else "中", "confidence": conf,
            "note": str(r.get("note") or "")[:200]}


def step_size(n: int, strength: float, direction: int) -> float:
    """S 形：证据少时几乎不动，攒够了才明显变化；强度接近两端时也变慢（进入平台期）。最多 0.1。"""
    s = 1.0 / (1.0 + math.exp(-1.6 * (n - 3.5)))
    x = strength if direction > 0 else 1.0 - strength
    g = 0.35 + 2.6 * x * (1.0 - x)
    return round(min(STEP_MAX, STEP_MAX * s * g), 3)


def freq_word(strength: float) -> str:
    for lim, word in FREQ:
        if strength < lim:
            return word
    return FREQ[-1][1]


def inject(msgs: list, canon: str, block: str) -> bool:
    """把副人格块插在正典之后、记忆快照之前。正典不是系统提示的开头（结构对不上）时不插，返回 False。"""
    if not block or not msgs or msgs[0].get("role") != "system":
        return False
    content = msgs[0].get("content")
    if not isinstance(content, str) or BLOCK_HEAD in content:
        return False
    canon = canon or ""
    if canon and not content.startswith(canon):
        return False
    rest = content[len(canon):]
    msgs[0]["content"] = (canon + "\n\n" + block + rest) if canon else (block + ("\n\n" + rest if rest else ""))
    return True


def _days_span(evs: list) -> int:
    days = sorted({e.get("date") for e in evs if e.get("date")})
    if not days:
        return 0
    return (date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days + 1


class Persona:
    def __init__(self, path=None, data: dict | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._mtime = 0
        self._dirty = False
        self._inject_warned = False
        self.data = data if data is not None else self._load()
        self._fill(self.data)

    # ------------------------------------------------------------------ 存取
    @staticmethod
    def empty() -> dict:
        return {"version": 1, "seq": {"p": 0, "c": 0}, "frozen": False, "entries": {}, "pool": [], "closed": [],
                "audit": [], "changes": [], "reflect": {"cursor": 0, "last": 0.0, "last_try": 0.0, "runs": []},
                "canon": {"hash": "", "text": "", "seen": 0.0, "checked": ""}, "maint_day": "",
                "tokens": {"day": "", "used": 0}}

    @staticmethod
    def _fill(d: dict) -> None:
        for k, v in Persona.empty().items():
            if k not in d or not isinstance(d[k], type(v)):
                d[k] = copy.deepcopy(v)
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    d[k].setdefault(k2, copy.deepcopy(v2))

    def _load(self) -> dict:
        if self.path is None or not self.path.exists():
            return self.empty()
        try:
            self._mtime = self.path.stat().st_mtime_ns
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else self.empty()
        except (OSError, ValueError):
            log.exception("字符画·副人格：persona.json 读取失败，按空白开始（原文件保留为 .bad）")
            try:
                os.replace(self.path, self.path.with_suffix(".bad"))
            except OSError:
                pass
            return self.empty()

    @classmethod
    def sim(cls, snap: dict) -> "Persona":
        """不落盘的副本（后台线程在上面预演合并，判断哪些候选攒够了证据需要审核）。"""
        return cls(None, copy.deepcopy(snap))

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime_ns
            self._dirty = False

    def flush(self) -> None:
        if self._dirty:
            self.save()

    def reload_if_changed(self) -> bool:
        """别的桌宠实例（多开）改过 persona.json 时先读回来，避免互相覆盖。"""
        if self.path is None or not self.path.exists():
            return False
        with self._lock:
            m = self.path.stat().st_mtime_ns
            if m == self._mtime:
                return False
            self.data = self._load()
            self._fill(self.data)
            return True

    # ------------------------------------------------------------------ 基础
    @property
    def frozen(self) -> bool:
        return bool(self.data.get("frozen"))

    def _new_id(self, kind: str) -> str:
        self.data["seq"][kind] = self.data["seq"].get(kind, 0) + 1
        return f"{kind}_{self.data['seq'][kind]:04d}"

    def audit(self, event: str, phase: str = "", **detail) -> None:
        self.data["audit"].append({"time": round(time.time(), 1), "event": event, "phase": phase,
                                   **{k: v for k, v in detail.items() if v not in (None, "", [], {})}})
        del self.data["audit"][:-AUDIT_MAX]
        self._dirty = True

    def entries(self, include_retired: bool = True) -> list:
        es = list(self.data["entries"].values())
        if not include_retired:
            es = [e for e in es if e["status"] != "retired"]
        return es

    def live_entries(self) -> list:
        return sorted((e for e in self.data["entries"].values() if e["status"] in LIVE_STATUS),
                      key=lambda e: (-e["strength"], e["created"]))

    def entry(self, pid: str) -> dict | None:
        return self.data["entries"].get(pid)

    def candidate(self, cid: str) -> dict | None:
        for c in self.data["pool"]:
            if c["id"] == cid:
                return c
        return None

    def budget_state(self, max_entries: int, max_chars: int) -> dict:
        es = [e for e in self.data["entries"].values() if e["status"] != "retired"]
        chars = sum(len(e["text"]) + 7 for e in es if e["status"] in LIVE_STATUS)
        return {"entries": len(es), "max_entries": max_entries, "chars": chars, "max_chars": max_chars}

    # ------------------------------------------------------------------ 正典
    def note_canon(self, text: str) -> None:
        """每次聊天组装请求时记下正典（桌宠设置里的人设）。变了的话，下次夜间反思会重新审一遍所有条目。可从任意线程调用。"""
        text = text or ""
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            cn = self.data["canon"]
            if cn.get("hash") == h:
                return
            first = not cn.get("hash")
            cn.update(hash=h, text=text[:20000], seen=round(time.time(), 1))
            if first:
                cn["checked"] = h
            self.audit("canon_seen" if first else "canon_change", length=len(text))
            self._dirty = True

    def canon_needs_check(self) -> bool:
        cn = self.data["canon"]
        return bool(cn.get("hash")) and cn.get("checked") != cn["hash"] and \
            any(e["status"] != "retired" for e in self.data["entries"].values())

    # ------------------------------------------------------------------ 提示词
    def block_text(self, max_chars: int = 420) -> str:
        lines, used = [], 0
        for e in self.live_entries():
            line = f"- {e['text']}（{freq_word(e['strength'])}这样）"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return (BLOCK_HEAD + "\n" + "\n".join(lines) + "\n" + BLOCK_TAIL) if lines else ""

    @staticmethod
    def injecting(cfg) -> bool:
        return getattr(cfg, "persona_mode", "shadow") in ("trial", "live")

    def inject_into(self, msgs: list, canon: str, cfg) -> bool:
        if not self.injecting(cfg):
            return False
        block = self.block_text(int(getattr(cfg, "persona_max_chars", 420)))
        if not block:
            return False
        ok = inject(msgs, canon, block)
        if not ok and not self._inject_warned:
            self._inject_warned = True
            log.warning("字符画·副人格：系统提示的开头不是正典，这次没有插入副人格块")
        return ok

    # ------------------------------------------------------------------ 规则审查
    def check(self, p: dict, max_entries: int = 12) -> list:
        """提议的规则审查（程序）：文字规范、目标是否存在、是否受保护、是否和已有条目重复、证据是否够格。"""
        op = p["op"]
        why = []
        es = self.data["entries"]
        tgt = es.get(p.get("target") or "")
        if op == "create":
            why += check_text(p.get("text", ""))
            for e in es.values():
                if e["status"] != "retired" and similarity(e["text"], p.get("text", "")) >= 0.55:
                    why.append(f"和已有的 {e['id']}「{e['text']}」很像：应该提增强或改写")
                    break
            if not any(ev["trust"] == "strong" for ev in p.get("evidence", [])):
                why.append("新增至少要有一条主人的原话作证据")
        elif op in ("strengthen", "weaken", "retire", "rewrite"):
            if tgt is None:
                why.append(f"找不到条目「{p.get('target', '')}」")
            elif tgt.get("protected"):
                why.append(f"{tgt['id']} 受保护，管线不能改动它")
            elif tgt["status"] == "retired":
                why.append(f"{tgt['id']} 已经退休")
            elif tgt["status"] == "paused" and op != "retire":
                why.append(f"{tgt['id']} 已暂停（与正典冲突），等你处理")
            if op == "rewrite":
                why += check_text(p.get("text", ""))
                if tgt is not None and p.get("text", "").strip() == tgt["text"]:
                    why.append("改写后的文字和原来一样")
            elif not any(ev["trust"] == "strong" for ev in p.get("evidence", [])):
                why.append(f"{OP_CN[op]}只认主人的反应：没有主人原话的证据")
        elif op == "merge":
            src, dst = tgt, es.get(p.get("into") or "")
            if src is None or dst is None or src is dst:
                why.append("合并要指明两条不同的已有条目（target 并入 into）")
            else:
                for e in (src, dst):
                    if e.get("protected"):
                        why.append(f"{e['id']} 受保护，管线不能改动它")
                    elif e["status"] in ("retired", "paused"):
                        why.append(f"{e['id']} {STATUS_CN[e['status']]}")
                if p.get("text"):
                    why += check_text(p["text"])
        return why

    # ------------------------------------------------------------------ 候选池
    def _close(self, c: dict, status: str, why: str, by: str = "pipeline") -> None:
        c.update(status=status, why=why, closed=round(time.time(), 1), closed_by=by)
        if c in self.data["pool"]:
            self.data["pool"].remove(c)
        self.data["closed"].append(c)
        del self.data["closed"][:-CLOSED_MAX]
        self._dirty = True

    def _match(self, p: dict) -> dict | None:
        for c in self.data["pool"]:
            if c["status"] not in OPEN_CAND or c["op"] != p["op"]:
                continue
            if p["op"] == "create":
                if similarity(c.get("text", ""), p.get("text", "")) >= 0.5:
                    return c
            elif c.get("target") == p.get("target") and c.get("into") == p.get("into"):
                return c
        return None

    def _add_evidence(self, c: dict, evs: list) -> int:
        have = {e["mid"] for e in c["evidence"]}
        added = [e for e in evs if e["mid"] not in have]
        if added:
            c["evidence"] = (c["evidence"] + added)[-16:]
            c["updated"] = round(time.time(), 1)
            self._dirty = True
        return len(added)

    def merge_proposal(self, p: dict, phase: str) -> dict:
        """把一条（证据已核验的）提议放进候选池 → 处理结果 {"result": 新建/补充/拒绝, "cand": id, "why": …}。"""
        now = round(time.time(), 1)
        op = p.get("op", "")
        evs = p.get("evidence") or []
        base = {"time": now, "updated": now, "phase": phase, "op": op, "evidence": evs,
                **{k: p[k] for k in ("text", "kind", "reason", "target", "into") if p.get(k)},
                "dropped": p.get("dropped") or []}
        if p.get("invalid"):
            c = {"id": self._new_id("c"), **base, "status": "rejected"}
            self._close(c, "rejected", p["invalid"])
            self.audit("rule_reject", phase, cand=c["id"], op=op, why=p["invalid"])
            return {"result": "rejected", "cand": c["id"], "why": p["invalid"]}
        if op == "support":
            c = self.candidate(p.get("candidate") or "")
            if c is None or c["status"] not in OPEN_CAND:
                self.audit("support_miss", phase, cand=p.get("candidate"), why="找不到要补充证据的观察")
                return {"result": "rejected", "cand": p.get("candidate"), "why": "找不到要补充证据的观察"}
            n = self._add_evidence(c, evs)
            self.audit("support", phase, cand=c["id"], added=n, dropped=len(base["dropped"]))
            return {"result": "supported", "cand": c["id"], "added": n}
        if op == "create" and p.get("kind") not in KINDS:
            base["kind"] = "其它"
        why = self.check(p)
        if why:
            c = {"id": self._new_id("c"), **base, "status": "rejected"}
            self._close(c, "rejected", "；".join(why))
            self.audit("rule_reject", phase, cand=c["id"], op=op, text=p.get("text"), target=p.get("target"),
                       why=c["why"])
            return {"result": "rejected", "cand": c["id"], "why": c["why"]}
        c = self._match(p)
        if c is not None:
            n = self._add_evidence(c, evs)
            self.audit("support", phase, cand=c["id"], added=n, note="同一观察再次出现")
            return {"result": "supported", "cand": c["id"], "added": n}
        c = {"id": self._new_id("c"), **base, "status": "accumulating"}
        self.data["pool"].append(c)
        while len(self.data["pool"]) > POOL_MAX:
            old = min((x for x in self.data["pool"] if x["status"] == "accumulating"), key=lambda x: x["updated"],
                      default=None)
            if old is None:
                break
            self._close(old, "expired", "候选池满了，最久没有新证据的观察被挤出")
        self.audit("propose", phase, cand=c["id"], op=op, text=p.get("text"), target=p.get("target"),
                   evidence=len(evs), dropped=len(base["dropped"]))
        return {"result": "new", "cand": c["id"]}

    def counted_evidence(self, c: dict) -> list:
        """按种类算数的证据：新增可以用她自己的表现（弱证据），其余只认主人的反应；已经用在条目上的不重复算。"""
        evs = c.get("evidence") or []
        if c["op"] == "create":
            return [e for e in evs if e["trust"] in ("strong", "weak")]
        used = set()
        for pid in (c.get("target"), c.get("into")):
            e = self.data["entries"].get(pid or "")
            if e:
                used |= {x["mid"] for x in e.get("evidence", [])}
        return [e for e in evs if e["trust"] == "strong" and e["mid"] not in used]

    def ready(self, c: dict) -> tuple[bool, str]:
        """证据门槛：至少 3 条独立证据、跨至少 3 天、来自至少 2 段不同的情节（新增还要至少 1 条主人的原话）。改写、合并不看证据。"""
        if c["op"] in ("rewrite", "merge"):
            return True, ""
        evs = self.counted_evidence(c)
        n, span, eps = len(evs), _days_span(evs), len({e["episode"] for e in evs})
        strong = sum(1 for e in evs if e["trust"] == "strong")
        ok = n >= THRESHOLD["evidence"] and span >= THRESHOLD["days"] and eps >= THRESHOLD["episodes"] and strong >= 1
        return ok, f"证据 {n}/{THRESHOLD['evidence']} 条（主人原话 {strong}），跨 {span}/{THRESHOLD['days']} 天，" \
                   f"{eps}/{THRESHOLD['episodes']} 段情节"

    @staticmethod
    def review_key(c: dict) -> str:
        mids = sorted(e["mid"] for e in c.get("evidence") or [])
        raw = "|".join([c["op"], c.get("target", ""), c.get("into", ""), c.get("text", "")] + mids)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def review_targets(self) -> list:
        """攒够证据、还没有针对当前证据审核过的候选。"""
        out = []
        for c in self.data["pool"]:
            if c["status"] not in ("accumulating", "reviewing", "deferred"):
                continue
            if self.ready(c)[0] and (c.get("review_key") != self.review_key(c) or not c.get("review")):
                out.append(c)
        return out

    def review_payload(self, c: dict, db, now: float | None = None) -> dict:
        es = self.data["entries"]
        tgt, into = es.get(c.get("target") or ""), es.get(c.get("into") or "")
        prop = {"op": OP_CN.get(c["op"], c["op"]), "text": c.get("text", ""), "kind": c.get("kind", ""),
                "reason": c.get("reason", "")}
        if tgt:
            prop["current"] = tgt["text"]
        if into:
            prop["into_current"] = into["text"]
        evs = [{"who": "主人" if e["role"] == "user" else "桌宠", "trust": e["trust"], "date": e["date"],
                "text": e["text"], "quote": e["quote"],
                "prev": (("主人：" if e.get("prev_role") == "user" else "桌宠：") + e["prev"]) if e.get("prev") else ""}
               for e in c.get("evidence") or []]
        counter = []
        if c["op"] in ("create", "strengthen") and db is not None:
            since = (now or time.time()) - 90 * DAY
            counter = db.counter_examples(c.get("text") or (tgt or {}).get("text", ""),
                                          exclude=[e["mid"] for e in c.get("evidence") or []], since_ts=since)
        others = [{"id": e["id"], "text": e["text"]} for e in es.values()
                  if e["status"] != "retired" and e["id"] not in (c.get("target"), c.get("into"))]
        return {"proposal": prop, "evidence": evs, "counter": counter, "entries": others}

    # ------------------------------------------------------------------ 分级生效
    def judge(self, c: dict, now: float) -> tuple[str, str]:
        """审核结论 → ("rejected" | "pending_approval" | "auto", 原因)。"""
        rv = c.get("review") or {}
        op = c["op"]
        rej = []
        if rv.get("injection"):
            rej.append("审核认为像注入")
        if rv.get("canon_conflict") == "会":
            rej.append("与正典冲突")
        if not rv.get("supported"):
            rej.append("证据不支持")
        if rv.get("overreach"):
            rej.append("过度推断")
        if rv.get("event_or_name"):
            rej.append("写进了具体事件或人名（归记忆管）")
        if op in ("create", "strengthen") and rv.get("counter_found"):
            rej.append("有主人的反例")
        if op in ("rewrite", "merge") and rv.get("same_meaning") is not True:
            rej.append("改写后意思变了")
        if rej:
            return "rejected", "；".join(rej)
        need = []
        if rv.get("canon_conflict") == "可能":
            need.append("可能与正典冲突")
        if rv.get("values"):
            need.append("涉及价值观、道德或信念")
        es = self.data["entries"]
        bad = [x for x in rv.get("contradicts") or [] if x in es and es[x]["status"] != "retired"
               and x not in (c.get("target"), c.get("into"))]
        if bad:
            need.append("与现有条目矛盾：" + "、".join(bad))
        tgt = es.get(c.get("target") or "")
        if op == "retire" and tgt and tgt["status"] in ("active", "fading") and \
                now - tgt.get("activated", tgt["created"]) > APPROVAL_OLD_DAYS * DAY:
            need.append(f"退休一条生效超过 {APPROVAL_OLD_DAYS} 天的条目")
        if float(rv.get("confidence", 0.0)) < LOW_CONFIDENCE:
            need.append("审核的把握度低")
        return ("pending_approval", "；".join(need)) if need else ("auto", "")

    def speed_block(self, c: dict, mode: str, now: float) -> str:
        """速度上限 → 不能生效的原因（空 = 可以）。"""
        lim = SPEED.get(mode, SPEED["live"])
        shadow = mode == "shadow"
        recent = [x for x in self.data["changes"] if (x.get("phase") == "shadow") == shadow
                  and now - x["time"] < lim["window_days"] * DAY]
        if len(recent) >= lim["window_max"]:
            return f"{lim['window_days']} 天内已经变了 {len(recent)} 次（上限 {lim['window_max']} 次）"
        gap = REWRITE_GAP_DAYS if c["op"] in ("rewrite", "merge") else lim["gap_days"]
        for pid in (c.get("target"), c.get("into")):
            e = self.data["entries"].get(pid or "")
            if not e:
                continue
            if now - e.get("last_change", 0) < gap * DAY:
                return f"{pid} 在 {gap} 天内刚变过"
            if e.get("streak", {}).get("cool_until", 0) > now:
                return f"{pid} 同一方向连续变了 {lim['streak']} 次，冷却中"
        return ""

    def _fits(self, c: dict, max_entries: int, max_chars: int) -> str:
        if c["op"] != "create":
            return ""
        b = self.budget_state(max_entries, max_chars)
        if b["entries"] + 1 > max_entries or b["chars"] + len(c.get("text", "")) + 7 > max_chars:
            return f"条目已满（{b['entries']}/{max_entries} 条，{b['chars']}/{max_chars} 字）：要先合并或退休旧条目"
        return ""

    def _hist(self, e: dict, op: str, by: str, cand: str = "", note: str = "") -> None:
        e.setdefault("history", []).append(
            {"time": round(time.time(), 1), "op": op, "by": by, "cand": cand, "note": note,
             "text": e["text"], "kind": e["kind"], "strength": round(e["strength"], 3), "status": e["status"]})
        del e["history"][:-40]
        e["updated"] = round(time.time(), 1)

    def _streak(self, e: dict, direction: int, mode: str, now: float) -> None:
        lim = SPEED.get(mode, SPEED["live"])
        st = e.setdefault("streak", {"dir": 0, "n": 0, "cool_until": 0})
        st["n"] = st["n"] + 1 if st.get("dir") == direction else 1
        st["dir"] = direction
        if st["n"] >= lim["streak"]:
            st["cool_until"] = now + lim["cool_days"] * DAY
            st["n"] = 0

    def apply(self, c: dict, mode: str, now: float, by: str = "pipeline") -> str:
        """让一个候选生效（影子模式下也照样写进条目，只是不进提示词）→ 改动的条目 id。"""
        es = self.data["entries"]
        op = c["op"]
        evs = c.get("evidence") or []
        last_ev = max((e["ts"] for e in evs), default=now)
        strong = [e for e in evs if e["trust"] == "strong"]
        last_strong = max((e["ts"] for e in strong), default=0.0)
        if op == "create":
            pid = self._new_id("p")
            e = {"id": pid, "text": c["text"], "kind": c.get("kind") or "其它", "strength": INIT_STRENGTH,
                 "status": "trial", "protected": False, "evidence": evs[-EVIDENCE_KEEP:], "history": [],
                 "created": now, "updated": now, "last_evidence": last_ev, "last_strong": last_strong,
                 "last_change": now, "streak": {"dir": 1, "n": 1, "cool_until": 0}, "phase": mode, "from": c["id"]}
            es[pid] = e
            self._hist(e, "create", by, c["id"])
        elif op in ("strengthen", "weaken"):
            e = es[c["target"]]
            pid = e["id"]
            direction = 1 if op == "strengthen" else -1
            new = self.counted_evidence(c)
            delta = step_size(len(new), e["strength"], direction)
            e["strength"] = round(max(0.0, min(1.0, e["strength"] + direction * delta)), 3)
            have = {x["mid"] for x in e["evidence"]}
            e["evidence"] = (e["evidence"] + [x for x in evs if x["mid"] not in have])[-EVIDENCE_KEEP:]
            e["last_evidence"] = max(e.get("last_evidence", 0), last_ev)
            if strong:
                e["last_strong"] = max(e.get("last_strong", 0), last_strong)
            if direction > 0 and e["status"] in ("trial", "fading") and e["strength"] >= ACTIVE_AT:
                e["status"] = "active"
                e.setdefault("activated", now)
                e.pop("last_decay", None)
            if direction < 0 and e["strength"] < RETIRE_BELOW:
                e["status"] = "retired"
                e["retire_reason"] = "减弱到很低，自然退休"
            self._streak(e, direction, mode, now)
            self._hist(e, op, by, c["id"], note=f"{'+' if direction > 0 else '-'}{delta:.3f}")
        elif op == "rewrite":
            e = es[c["target"]]
            pid = e["id"]
            e["text"] = c["text"]
            self._hist(e, op, by, c["id"])
        elif op == "merge":
            src, e = es[c["target"]], es[c["into"]]
            pid = e["id"]
            if c.get("text"):
                e["text"] = c["text"]
            e["strength"] = round(max(e["strength"], src["strength"]), 3)
            have = {x["mid"] for x in e["evidence"]}
            e["evidence"] = (e["evidence"] + [x for x in src["evidence"] if x["mid"] not in have])[-EVIDENCE_KEEP:]
            e["last_evidence"] = max(e.get("last_evidence", 0), src.get("last_evidence", 0))
            src["status"] = "retired"
            src["retire_reason"] = f"并入 {pid}"
            src["merged_into"] = pid
            self._hist(src, "merged", by, c["id"], note=f"并入 {pid}")
            src["last_change"] = now
            self._hist(e, op, by, c["id"], note=f"并入了 {src['id']}")
        elif op == "retire":
            e = es[c["target"]]
            pid = e["id"]
            e["status"] = "retired"
            e["retire_reason"] = c.get("reason") or "已经不是这样了"
            self._hist(e, op, by, c["id"])
        else:
            raise ValueError(op)
        es[pid]["last_change"] = now          # 你批准的变化也算进速度统计（批准本身不受上限约束）
        self.data["changes"].append({"time": now, "entry": pid, "op": op, "cand": c["id"], "phase": mode, "by": by})
        del self.data["changes"][:-CHANGES_MAX]
        self._close(c, "applied", ("影子模式：只记录，不进提示词" if mode == "shadow" else "") or c.get("why", ""), by)
        c["entry"] = pid
        self.audit("apply", mode, cand=c["id"], op=op, entry=pid, text=es[pid]["text"],
                   strength=es[pid]["strength"], by=by)
        return pid

    # ------------------------------------------------------------------ 夜间结果
    def apply_night(self, res: dict, cfg, now: float | None = None) -> dict:
        """后台反思的结果回到界面线程后在这里落地（确定性）：合并提议 → 挂上审核 → 分级生效 → 正典复查 → 衰减。"""
        now = now or time.time()
        mode = getattr(cfg, "persona_mode", "shadow")
        max_entries = int(getattr(cfg, "persona_max_entries", 12))
        max_chars = int(getattr(cfg, "persona_max_chars", 420))
        rep = {"time": round(now, 1), "phase": mode, "thoughts": res.get("thoughts", ""), "error": res.get("error", ""),
               "skipped": res.get("skipped", ""), "messages": res.get("n_messages", 0), "proposals": [],
               "decisions": [], "usage": res.get("usage") or {}}
        with self._lock:
            self.reload_if_changed()
            r = self.data["reflect"]
            r["last_try"] = round(now, 1)
            if self.frozen or mode == "off":
                rep["skipped"] = "副人格已冻结" if self.frozen else "副人格已关闭"
                self.audit("reflect_skip", mode, why=rep["skipped"])
                self.save()
                return rep
            for p in res.get("proposals") or []:
                out = self.merge_proposal(p, mode)
                rep["proposals"].append({"op": p.get("op"), "text": p.get("text") or p.get("target") or p.get("candidate"),
                                         "evidence": len(p.get("evidence") or []), "dropped": len(p.get("dropped") or []),
                                         **out})
            reviews = res.get("reviews") or {}
            for c in list(self.data["pool"]):
                if c["status"] not in ("accumulating", "reviewing", "deferred"):
                    continue
                if not self.ready(c)[0]:
                    continue
                key = self.review_key(c)
                if c.get("review_key") != key:
                    if key in reviews and not reviews[key].get("error"):
                        c["review"], c["review_key"] = reviews[key], key
                        self.audit("review", mode, cand=c["id"], **{k: reviews[key].get(k) for k in
                                   ("supported", "overreach", "canon_conflict", "injection", "values", "risk",
                                    "confidence", "note")})
                    else:
                        c["status"] = "reviewing"
                        c["why"] = "攒够证据，等审核" + (f"（{reviews[key]['error']}）" if key in reviews else "")
                        continue
                verdict, why = self.judge(c, now)
                if verdict == "rejected":
                    self._close(c, "rejected", why)
                    self.audit("reject", mode, cand=c["id"], why=why)
                    rep["decisions"].append((c["id"], "rejected", why))
                elif verdict == "pending_approval":
                    if c["status"] != "pending_approval":
                        c["status"], c["why"] = "pending_approval", why
                        self.audit("pending", mode, cand=c["id"], why=why)
                    rep["decisions"].append((c["id"], "pending_approval", why))
                else:
                    c["status"], c["why"] = "deferred", ""
            order = {"merge": 0, "retire": 1, "rewrite": 2, "weaken": 3, "strengthen": 4, "create": 5}
            todo = sorted((c for c in self.data["pool"] if c["status"] == "deferred" and c.get("review")),
                          key=lambda c: (order.get(c["op"], 9), -sum(e["trust"] == "strong" for e in c["evidence"])))
            for c in todo:
                again = self.check({**c, "evidence": c.get("evidence") or []})
                if again:
                    self._close(c, "rejected", "生效前复查不通过：" + "；".join(again))
                    self.audit("reject", mode, cand=c["id"], why=c["why"])
                    rep["decisions"].append((c["id"], "rejected", c["why"]))
                    continue
                block = self.speed_block(c, mode, now) or self._fits(c, max_entries, max_chars)
                if block:
                    if c.get("why") != block:
                        c["why"] = block
                        self.audit("defer", mode, cand=c["id"], why=block)
                    rep["decisions"].append((c["id"], "deferred", block))
                    continue
                pid = self.apply(c, mode, now)
                rep["decisions"].append((c["id"], "applied", pid))
            if res.get("canon_checks"):
                self._apply_canon_checks(res["canon_checks"], mode)
            if res.get("cursor") is not None and not res.get("error"):
                r["cursor"] = max(int(r.get("cursor", 0)), int(res["cursor"]))
            if not res.get("error") and not res.get("skipped"):
                r["last"] = round(now, 1)
            r["backlog"] = bool(res.get("backlog"))
            r["runs"].append({k: rep[k] for k in ("time", "phase", "thoughts", "error", "skipped", "messages")}
                             | {"proposals": len(rep["proposals"]), "applied": sum(d[1] == "applied" for d in rep["decisions"])})
            del r["runs"][:-30]
            self.audit("reflect", mode, messages=rep["messages"], thoughts=rep["thoughts"][:120], error=rep["error"],
                       skipped=rep["skipped"], proposals=len(rep["proposals"]))
            self.maintenance(cfg, now)
            self.save()
        return rep

    def _apply_canon_checks(self, checks: dict, mode: str) -> None:
        es = self.data["entries"]
        for item in checks.get("results") or []:
            e = es.get(str(item.get("id") or ""))
            if not e or e["status"] == "retired":
                continue
            conflict = str(item.get("conflict") or "")
            note = str(item.get("note") or "")[:120]
            if conflict == "会":
                e["paused_from"] = e["status"]
                e["status"] = "paused"
                e["pause_reason"] = "与新的正典冲突" + (f"：{note}" if note else "") + \
                                    ("（受保护的条目也会暂停，等你处理）" if e.get("protected") else "")
                self._hist(e, "pause", "pipeline", note=e["pause_reason"])
                self.audit("canon_pause", mode, entry=e["id"], note=note)
            elif conflict == "可能":
                e["canon_note"] = "可能与新的正典冲突：" + note
                self.audit("canon_maybe", mode, entry=e["id"], note=note)
        if checks.get("hash"):
            self.data["canon"]["checked"] = checks["hash"]

    def maintenance(self, cfg, now: float | None = None) -> list:
        """每天一次：试用条目淡出、长时间没有新证据的条目慢慢衰减 / 退休、候选过期。受保护和已暂停的条目不动。"""
        now = now or time.time()
        if self.frozen or getattr(cfg, "persona_mode", "shadow") == "off":
            return []
        day = date.fromtimestamp(now).isoformat()
        if self.data.get("maint_day") == day:
            return []
        self.data["maint_day"] = day
        mode = getattr(cfg, "persona_mode", "shadow")
        done = []
        for e in self.data["entries"].values():
            if e.get("protected") or e["status"] not in LIVE_STATUS:
                continue
            if e["status"] == "trial":
                if now - max(e["created"], e.get("last_strong", 0)) > TRIAL_DAYS * DAY:
                    e["status"] = "retired"
                    e["retire_reason"] = f"试用 {TRIAL_DAYS} 天里没有主人的新反应，自然淡出"
                    self._hist(e, "fade_out", "pipeline", note=e["retire_reason"])
                    done.append((e["id"], "retired"))
                continue
            start = max(e.get("last_evidence", 0), e.get("activated", 0)) + DECAY_AFTER_DAYS * DAY
            last = max(e.get("last_decay", 0), start)
            weeks = int((now - last) // (7 * DAY))
            if now > start and weeks >= 1:
                e["strength"] = round(max(0.0, e["strength"] - DECAY_PER_WEEK * weeks), 3)
                e["last_decay"] = last + weeks * 7 * DAY
                e["status"] = "fading"
                note = f"{DECAY_AFTER_DAYS} 天以上没有新证据，-{DECAY_PER_WEEK * weeks:.2f}"
                if e["strength"] < RETIRE_BELOW:
                    e["status"] = "retired"
                    e["retire_reason"] = "长时间没有新证据，慢慢淡出"
                    note += "，退休"
                self._hist(e, "decay", "pipeline", note=note)
                done.append((e["id"], e["status"]))
        for c in list(self.data["pool"]):
            if c["status"] == "accumulating" and now - c.get("updated", c["time"]) > CANDIDATE_DAYS * DAY:
                self._close(c, "expired", f"{CANDIDATE_DAYS} 天没有新证据")
                done.append((c["id"], "expired"))
        if done:
            self.audit("maintenance", mode, changes=done)
        self._dirty = True
        return done

    # ------------------------------------------------------------------ token 预算
    def tokens_left(self, limit: int) -> int:
        t = self.data["tokens"]
        if t.get("day") != date.today().isoformat():
            t.update(day=date.today().isoformat(), used=0)
        return max(0, int(limit) - int(t.get("used", 0)))

    def add_tokens(self, n: int) -> None:
        self.tokens_left(0)
        self.data["tokens"]["used"] = int(self.data["tokens"].get("used", 0)) + int(n)
        self._dirty = True

    def due(self, cfg, now: float | None = None) -> tuple[bool, str]:
        """今天该不该做夜间反思（一般一天最多一次；上次没读完的积压一小时后接着读）。"""
        now = now or time.time()
        r = self.data["reflect"]
        if now - r.get("last_try", 0) < 2 * 3600 and not r.get("backlog"):
            return False, "两小时内刚检查过"
        if r.get("backlog") and now - r.get("last_try", 0) >= 3600:
            return True, ""
        if now - r.get("last", 0) < float(getattr(cfg, "persona_reflect_min_hours", 20.0)) * 3600:
            return False, "今天已经回顾过"
        return True, ""

    # ------------------------------------------------------------------ 只有你能做的操作（本地测试台）
    def user_protect(self, pid: str, on: bool) -> bool:
        with self._lock:
            self.reload_if_changed()
            e = self.entry(pid)
            if not e:
                return False
            e["protected"] = bool(on)
            self.audit("protect" if on else "unprotect", by="user", entry=pid)
            self.save()
            return True

    def user_set_frozen(self, on: bool) -> None:
        with self._lock:
            self.reload_if_changed()
            self.data["frozen"] = bool(on)
            self.audit("freeze" if on else "unfreeze", by="user")
            self.save()

    def user_approve(self, cid: str, cfg) -> str:
        """批准一个等你批准的候选：立即生效（你的批准不受速度上限约束）。"""
        with self._lock:
            self.reload_if_changed()
            c = self.candidate(cid)
            if not c or c["status"] != "pending_approval":
                return ""
            again = self.check({**c, "evidence": c.get("evidence") or []})
            if again:
                self._close(c, "rejected", "批准时复查不通过：" + "；".join(again), by="user")
                self.save()
                return ""
            pid = self.apply(c, getattr(cfg, "persona_mode", "shadow"), time.time(), by="user")
            self.save()
            return pid

    def user_reject(self, cid: str) -> bool:
        with self._lock:
            self.reload_if_changed()
            c = self.candidate(cid)
            if not c:
                return False
            self._close(c, "rejected", "你拒绝了", by="user")
            self.audit("reject", by="user", cand=cid)
            self.save()
            return True

    def user_rollback(self, pid: str) -> bool:
        """撤销这条条目最近的一次变化（新增被撤销时退休，之后还可以恢复）。"""
        with self._lock:
            self.reload_if_changed()
            e = self.entry(pid)
            if not e or not e.get("history"):
                return False
            last = e["history"].pop()
            if not e["history"]:
                e["history"].append(last)
                e["status"] = "retired"
                e["retire_reason"] = "你撤销了新增"
                self._hist(e, "rollback", "user", note="撤销新增")
            else:
                prev = e["history"][-1]
                e.update(text=prev["text"], kind=prev["kind"], strength=prev["strength"], status=prev["status"])
                self._hist(e, "rollback", "user", note=f"撤销了「{last['op']}」")
            self.audit("rollback", by="user", entry=pid, undone=last["op"])
            self.save()
            return True

    def user_restore(self, pid: str) -> bool:
        with self._lock:
            self.reload_if_changed()
            e = self.entry(pid)
            if not e or e["status"] not in ("retired", "paused"):
                return False
            e["strength"] = max(e["strength"], RETIRE_BELOW + 0.05)
            e["status"] = "active" if e["strength"] >= ACTIVE_AT else "trial"
            e["last_evidence"] = time.time()
            e.pop("last_decay", None)
            e.pop("pause_reason", None)
            self._hist(e, "restore", "user")
            self.audit("restore", by="user", entry=pid)
            self.save()
            return True

    def user_delete(self, pid: str) -> bool:
        with self._lock:
            self.reload_if_changed()
            e = self.data["entries"].pop(pid, None)
            if not e:
                return False
            self.audit("delete", by="user", entry=pid, text=e["text"])
            self.save()
            return True

    def user_clear_shadow(self) -> int:
        """清空影子模式的结果（条目、候选池、变化记录）。回顾进度和审计日志保留。"""
        with self._lock:
            self.reload_if_changed()
            gone = [pid for pid, e in self.data["entries"].items() if e.get("phase") == "shadow"]
            for pid in gone:
                del self.data["entries"][pid]
            for c in list(self.data["pool"]):
                if c.get("phase") == "shadow":
                    self._close(c, "expired", "你清空了影子模式的结果", by="user")
            self.data["changes"] = [x for x in self.data["changes"] if x.get("phase") != "shadow"]
            self.audit("clear_shadow", by="user", entries=len(gone))
            self.save()
            return len(gone)


# —— 多开时只让一个桌宠实例做夜间反思 ——
def acquire_lease(path: Path, ttl: float = 1200.0) -> bool:
    now = time.time()
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
        if cur.get("pid") != os.getpid() and float(cur.get("until", 0)) > now:
            return False
    except (OSError, ValueError):
        pass
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "until": now + ttl}), encoding="utf-8")
        return True
    except OSError:
        return False


def release_lease(path: Path) -> None:
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
        if cur.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass
