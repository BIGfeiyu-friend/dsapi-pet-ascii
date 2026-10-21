"""自创表情库：DeepSeek 在对话中即兴创造、或在后台“表情进化”时设计和迭代的表情预设。

- 存储：<数据目录>/custom_presets.json（与桌宠的聊天记录、记忆完全分开）
- 每个预设保存规整后的完整表情规格，所以无论模型给的参数多离谱，渲染出来的五官位置都正确
- 去重：与已有预设（内置或自创）参数几乎相同时，不新增，直接沿用已有名字
- 迭代：表情进化可以修改（保留版本，可回滚）、合并（旧名字变成别名，照样能用）、淘汰（可恢复）自创表情；
  被用户 👎 多于 👍 的内置表情也可以被“改良”（随时恢复原版）
- 反馈：用户的 👍/👎 与每个表情（含内置）的使用次数都记在这里，供表情进化参考
- 上限：超过上限时淘汰最少使用、最久未用的自创表情（进入“已淘汰”，可恢复）
- 自创表情在 face_rig 中与内置预设同样可用（{"preset": "憋笑"}）
"""
from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time
import weakref
from pathlib import Path

from . import face_rig as fr

NAME_RE = re.compile(r"^[\w㐀-鿿぀-ヿ·\-]{1,12}$")
MERGE_DISTANCE = 0.35
UNCHANGED_DISTANCE = 0.05
MAX_HISTORY = 8
MAX_RETIRED = 40
MAX_FEEDBACK_LOG = 40
CN_TO_KEY = {cn: key for key, cn in fr.PRESET_CN.items()}      # 模型有时用中文名称呼内置表情
# 变化幅度：放大后达到目标的这个比例就采用（只改眼睛的大改动通常在 8~11% 之间，再严就会把大胆的修改也拒掉）
ACCEPT_RATIO = 0.75
SAVE_DELAY = 0.5          # 结构变化后多久在后台写盘（秒）
USAGE_SAVE_DELAY = 10.0   # 使用次数攒多久写一次（秒）


def _compact(v):
    """表情参数保留 3 位小数（远超过字符画能分辨的精度），文件小、序列化快。"""
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, dict):
        return {k: _compact(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_compact(x) for x in v]
    return v


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def spec_distance(a: dict, b: dict) -> float:
    """两个（规整后的）表情规格之间的差异：连续参数差的绝对值之和 + 离散参数不同计 1。"""
    d = 0.0
    for side in ("eye_l", "eye_r"):
        ea, eb = a[side], b[side]
        for k in ("open", "smile", "tilt", "iris", "tear"):
            d += abs(ea[k] - eb[k]) * 0.5
        d += 0.5 if ea["pupil"] != eb["pupil"] else 0.0
    d += (abs(a["gaze"][0] - b["gaze"][0]) + abs(a["gaze"][1] - b["gaze"][1])) * 0.3
    d += abs(a["brows"]["y"] - b["brows"]["y"]) * 0.4 + abs(a["brows"]["angle"] - b["brows"]["angle"]) * 0.4
    ma, mb = a["mouth"], b["mouth"]
    shape_a = "normal" if ma["shape"] == "art" else ma["shape"]
    shape_b = "normal" if mb["shape"] == "art" else mb["shape"]
    d += 1.0 if shape_a != shape_b else 0.0
    d += abs(ma["open"] - mb["open"]) + abs(ma["width"] - mb["width"]) * 0.5 + abs(ma["curve"] - mb["curve"]) * 0.6
    d += abs(a["blush"] - b["blush"]) * 0.4 + abs(a["sweat"] - b["sweat"]) * 0.4 + abs(a["gloom"] - b["gloom"]) * 0.4
    if "parts" in a or "parts" in b or "nodes" in a or "nodes" in b or "extra" in a or "extra" in b:
        d += _rig_distance(a, b)
    return d


def _rig_distance(a: dict, b: dict) -> float:
    """骨架字段的差异：换了不同的组件每处计 1.5，新增节点不同计 1，节点变换按幅度计。"""
    pa, pb = a.get("parts") or {}, b.get("parts") or {}
    d = 1.5 * sum(1 for n in set(pa) | set(pb) if pa.get(n, "builtin") != pb.get(n, "builtin"))
    ea = {(e["id"], e["part"]) for e in a.get("extra") or []}
    eb = {(e["id"], e["part"]) for e in b.get("extra") or []}
    d += 1.0 * len(ea ^ eb)
    na, nb = a.get("nodes") or {}, b.get("nodes") or {}
    for n in set(na) | set(nb):
        ta, tb = na.get(n, {}), nb.get(n, {})
        for k, dflt, w in (("dx", 0, 4), ("dy", 0, 4), ("sx", 1, 1), ("sy", 1, 1), ("rot", 0, 0.02)):
            d += abs(ta.get(k, dflt) - tb.get(k, dflt)) * w
    return d


def apply_patch(full: dict, patch: dict) -> dict:
    """在一份完整规格上叠加“只写要改的字段”的修改（eyes 同时改两只眼）。返回规整后的新规格。"""
    out = json.loads(json.dumps(full))
    if isinstance(patch.get("eyes"), dict):
        for side in ("eye_l", "eye_r"):
            out[side].update(patch["eyes"])
    for side in ("eye_l", "eye_r"):
        if isinstance(patch.get(side), dict):
            out[side].update(patch[side])
    for part in ("brows", "mouth"):
        if isinstance(patch.get(part), dict):
            out[part].update(patch[part])
    if isinstance(patch.get("gaze"), (list, tuple)) and len(patch["gaze"]) >= 2:
        out["gaze"] = list(patch["gaze"][:2])
    for k in ("blush", "sweat", "gloom"):
        if k in patch:
            out[k] = patch[k]
    for k in ("parts", "nodes", "extra"):          # 骨架字段整组替换（normalize 里会校验）
        if k in patch:
            out[k] = patch[k]
    return fr.normalize_full(out)


class CustomPresets:
    # 变化幅度：由运行时设置。有面部骨架时，每次修改/新建在字符画上至少要改变 min_change 比例的格子，
    # 不够就把变化自动放大（最多 max_gain 倍）；放大后仍不够的修改不采用。
    rig = None
    min_change = 0.0
    max_gain = 3.0

    def __init__(self, path: Path, max_items: int = 60):
        self.path = Path(path)
        self.max_items = max_items
        self._lock = threading.RLock()
        self.protected: dict[str, str] = {}      # 各风格的代表作（名字 → 风格）：进化不淘汰、不合并（避免收敛成一种风格）
        self.extra_context = None                # callable() -> dict：附加给表情进化的上下文
        self.items: dict[str, dict] = {}
        self.retired: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.builtin: dict[str, dict] = {}        # 内置表情：uses / likes / dislikes / override / history
        self.feedback_log: list[dict] = []
        # 保存状态（见“保存”一节）
        self._io = threading.Lock()
        self._sched = threading.Condition()
        self._due: float | None = None
        self._writer: threading.Thread | None = None
        self._closed = False                     # close() 之后：后台线程写完手头的就退出，不再空等一分钟
        self._frags: dict = {"presets": {}, "retired": {}}
        self._dirty: set = set()
        self._dirty_all = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for name, it in (data.get("presets") or {}).items():
                if isinstance(it, dict) and isinstance(it.get("spec"), dict):
                    it["spec"] = _compact(fr.normalize_full(it["spec"]))
                    self.items[name] = it
            for name, it in (data.get("retired") or {}).items():
                if isinstance(it, dict) and isinstance(it.get("spec"), dict):
                    it["spec"] = _compact(fr.normalize_full(it["spec"]))
                    self.retired[name] = it
            self.aliases = {a: t for a, t in (data.get("aliases") or {}).items() if isinstance(t, str)}
            for name, b in (data.get("builtin") or {}).items():
                if name in fr.PRESETS and isinstance(b, dict):
                    if isinstance(b.get("override"), dict):
                        b["override"] = _compact(fr.normalize_full(b["override"]))
                    self.builtin[name] = b
            self.feedback_log = [f for f in (data.get("feedback_log") or []) if isinstance(f, dict)]
        except Exception:
            pass
        self._publish()
        ref = weakref.ref(self)
        atexit.register(lambda: ref() is not None and ref().flush())     # 正常退出时写完没保存的改动

    # ------------------------------------------------------------------ 内部
    def _publish(self) -> None:
        """同步到 face_rig 的注册表（normalize 会查它们）。整体替换，其他线程不会读到半更新的表。"""
        fr.OVERRIDES = {n: b["override"] for n, b in self.builtin.items() if isinstance(b.get("override"), dict)}
        custom = {n: it["spec"] for n, it in self.items.items()}
        for alias, target in self.aliases.items():       # 被合并掉的旧名字照样能用
            if alias not in custom:
                if target in custom:
                    custom[alias] = custom[target]
                elif target in fr.PRESETS:
                    custom[alias] = fr.normalize({"preset": target})
        fr.CUSTOM = custom

    # ------------------------------------------------------------------ 保存（后台、合并、增量）
    # 保存不在调用者的线程里做：先记下“哪些条目变了”，由后台线程合并写盘。
    # - 新建/修改/淘汰/评价等：SAVE_DELAY 秒内写盘；使用次数（每次回复都会变）攒 USAGE_SAVE_DELAY 秒写一次
    # - 只重新序列化变了的条目，其余用缓存的 JSON 片段拼起来：JSON 序列化会占住 GIL，
    #   放到后台线程也会卡住界面，所以关键是每次要序列化的东西少
    # - 退出时（Runtime.stop / 进程正常退出）把还没写的写完；被强制结束最多丢最近 10 秒的使用次数
    def _mark(self, name: str) -> None:
        if name in self.items:
            self._dirty.add(("presets", name))

    def _commit(self) -> None:
        """结构变化（新建、修改、淘汰、合并、改名…）：全部条目下次重新序列化，并尽快在后台写盘。"""
        self._dirty_all = True
        self._request_save(SAVE_DELAY)
        self._publish()

    def _request_save(self, delay: float) -> None:
        due = time.monotonic() + delay
        with self._sched:
            if self._due is None or due < self._due:
                self._due = due
                self._sched.notify_all()
            if self._writer is None:
                self._writer = threading.Thread(target=self._writer_loop, name="pet-ascii-presets-save", daemon=True)
                self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            with self._sched:
                while True:
                    if self._due is None:
                        if not self._closed:
                            self._sched.wait(timeout=60)
                        if self._due is None:          # 一分钟没有新的保存请求（或已 close）：线程退出，下次再开
                            self._writer = None
                            return
                        continue
                    wait = self._due - time.monotonic()
                    if wait <= 0:
                        break
                    self._sched.wait(timeout=wait)
                self._due = None
            self._write()

    def _render(self) -> list:
        """要写进文件的一串片段（不拼成一个大字符串：几 MB 的字符串反复复制本身就会卡住界面）。"""
        def frag(name, it):                    # 缓存的是完整的 "名字":{...} 片段
            return _dump(name) + ":" + _dump(it)

        with self._lock:
            if self._dirty_all:
                self._frags = {"presets": {n: frag(n, it) for n, it in self.items.items()},
                               "retired": {n: frag(n, it) for n, it in self.retired.items()}}
                self._dirty_all = False
            else:
                for sec, name in self._dirty:
                    src = self.items if sec == "presets" else self.retired
                    if name in src:
                        self._frags[sec][name] = frag(name, src[name])
            self._dirty.clear()
            pieces = ['{"version":2']
            for sec, src in (("presets", self.items), ("retired", self.retired)):
                frags = self._frags[sec]
                pieces.append("," + _dump(sec) + ":{")
                for i, (n, it) in enumerate(src.items()):
                    if i:
                        pieces.append(",")
                    pieces.append(frags.get(n) or frags.setdefault(n, frag(n, it)))
                pieces.append("}")
            small = _dump({"aliases": self.aliases, "builtin": self.builtin, "feedback_log": self.feedback_log})
            pieces.append("," + small[1:])
            return pieces

    def _write(self) -> None:
        with self._io:                         # 后台线程与 flush() 不会同时写
            try:
                pieces = self._render()
                tmp = self.path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    for p in pieces:           # 逐段写：每段之间解释器都可以切回界面线程
                        fh.write(p)
                os.replace(tmp, self.path)
            except OSError:
                with self._lock:
                    self._dirty_all = True
                self._request_save(5.0)        # 文件被占用等：过一会儿整份重写

    def flush(self) -> None:
        """把还没写盘的改动立刻写完（退出时、测试里用）。"""
        with self._sched:
            pending, self._due = self._due is not None, None
        if pending:
            self._write()
        else:
            with self._io:                     # 等后台线程正在进行的写入结束
                pass

    def close(self, timeout: float = 2.0) -> bool:
        """写完没保存的改动并让后台写盘线程立刻退出（测试里用；之后再改动照样会开线程写盘）。返回线程是否已经退出。"""
        self.flush()
        with self._sched:
            self._closed = True
            self._sched.notify_all()
            t = self._writer
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        return t is None or not t.is_alive()

    def _nearest(self, spec: dict, exclude: str = "") -> tuple[str, float]:
        best, dist = "", 1e9
        for name in fr.PRESETS:
            if name == exclude:
                continue
            d = spec_distance(spec, fr.normalize({"preset": name}))
            if d < dist:
                best, dist = name, d
        for name, it in self.items.items():
            if name == exclude:
                continue
            d = spec_distance(spec, it["spec"])
            if d < dist:
                best, dist = name, d
        return best, dist

    @staticmethod
    def _push_history(rec: dict, spec: dict, by: str, reason: str = "") -> None:
        hist = rec.setdefault("history", [])
        hist.append({"spec": _compact(spec) if spec else spec, "time": round(time.time(), 1), "by": by,
                     "reason": reason[:60]})
        del hist[:-MAX_HISTORY]

    def _retire_item(self, name: str, reason: str, by: str, merged_into: str = "") -> None:
        it = self.items.pop(name)
        it.update(retired=time.time(), retire_reason=reason[:60], retired_by=by)
        if merged_into:
            it["merged_into"] = merged_into
        self.retired[name] = it
        while len(self.retired) > MAX_RETIRED:
            del self.retired[min(self.retired, key=lambda n: self.retired[n].get("retired", 0))]

    def resolve(self, name: str) -> str:
        """别名 / 内置表情的中文名 → 现在的名字。"""
        seen = 0
        while name in self.aliases and name not in self.items and seen < 5:
            name, seen = self.aliases[name], seen + 1
        if name not in self.items and name not in fr.PRESETS:
            name = CN_TO_KEY.get(name, name)
        return name

    def _spec_of(self, name: str) -> dict:
        return self.items[name]["spec"] if name in self.items else fr.normalize({"preset": name})

    def amplify(self, base: dict, target: dict) -> tuple[dict, float | None, float]:
        """把 target 相对 base 的变化放大，直到在字符画上的可见差异 ≥ min_change（最多 max_gain 倍）。

        返回 (规格, 可见差异, 放大倍数)。没有骨架或没设门槛时原样返回，差异为 None。
        """
        if self.rig is None or self.min_change <= 0:
            return target, None, 1.0
        try:
            best = (target, self.rig.visible_diff(base, target), 1.0)
            k = 1.0
            while best[1] < self.min_change and k < self.max_gain:
                k = min(self.max_gain, k + 0.5)
                cand = fr.normalize_full(fr.lerp_spec(base, target, k))      # t > 1：沿着变化方向外推
                d = self.rig.visible_diff(base, cand)
                if d > best[1]:
                    best = (cand, d, k)
            return best
        except Exception:
            return target, None, 1.0

    def _check_change(self, old: dict, new: dict) -> tuple[dict | None, str, dict]:
        """修改前后的幅度检查：太小就放大；放大后仍不够返回 (None, 说明, 幅度)。"""
        new, diff, gain = self.amplify(old, new)
        change = {"diff": diff, "gain": gain}
        if diff is not None and diff < self.min_change * ACCEPT_RATIO:
            return None, (f"在字符画上只改变了 {diff:.0%} 的格子（目标 {self.min_change:.0%}），"
                          "放大后仍然太小，没有采用"), change
        note = "" if diff is None else f"（变化 {diff:.0%}" + (f"，自动放大 ×{gain:g}）" if gain > 1 else "）")
        return new, note, change

    def change_of(self, name: str) -> dict:
        rec = self.items.get(name) or self.builtin.get(name) or {}
        return rec.get("last_change") or {}

    def liked(self, name: str) -> bool:
        rec = self.items.get(name) or self.builtin.get(name) or {}
        return rec.get("likes", 0) > rec.get("dislikes", 0)

    # ------------------------------------------------------------------ 创建
    @staticmethod
    def valid_name(name: str) -> bool:
        return bool(name) and bool(NAME_RE.match(name)) and name not in fr.PRESETS

    def add(self, name: str, raw_spec: dict, origin: str = "chat", note: str = "", context: str = "",
            style: str = "") -> tuple[str, str]:
        """新增或更新一个自创表情。返回 (实际使用的名字, 状态)。

        状态：new 新建 / updated 覆盖同名 / merged 与已有表情几乎相同，沿用已有名字 / invalid 名字不合法（沿用最接近的）
        """
        name = (name or "").strip()
        spec = fr.normalize(raw_spec)
        with self._lock:
            if not self.valid_name(name):
                nearest, _ = self._nearest(spec)
                return nearest, "invalid"
            if name in self.items:
                it = self.items[name]
                self._push_history(it, it["spec"], it.get("origin", origin))
                it.update(spec=_compact(spec), updated=round(time.time(), 1), note=note or it.get("note", ""))
                it["revisions"] = it.get("revisions", 0) + 1
                status = "updated"
            else:
                nearest, dist = self._nearest(spec)
                if dist < MERGE_DISTANCE:
                    if nearest in self.items:
                        self.items[nearest]["uses"] = self.items[nearest].get("uses", 0) + 1
                        self._commit()
                    return nearest, "merged"
                # 和最像的已有表情拉开差距：不够明显就把不同之处放大
                spec, diff, gain = self.amplify(self._spec_of(nearest), spec)
                spec = _compact(spec)
                if diff is not None and diff < self.min_change / 3:     # 放大后仍几乎看不出区别：不新增
                    if nearest in self.items:
                        self.items[nearest]["uses"] = self.items[nearest].get("uses", 0) + 1
                        self._commit()
                    return nearest, "merged"
                self.retired.pop(name, None)      # 同名的旧表情已淘汰：新的取而代之
                self.aliases.pop(name, None)
                self.items[name] = {"spec": spec, "origin": origin, "note": note[:60], "context": context[:80],
                                    **({"style": style} if style else {}),
                                    "created": round(time.time(), 1), "updated": round(time.time(), 1), "uses": 0, "revisions": 0,
                                    "nearest_builtin": nearest, "likes": 0, "dislikes": 0, "history": [],
                                    "last_change": _compact({"diff": diff, "gain": gain, "vs": nearest})}
                status = "new"
                self._evict()
            self._commit()
            return name, status

    def _evict(self) -> None:
        while len(self.items) > self.max_items:
            victim = min(self.items, key=lambda n: (self.liked(n), n in self.protected, self.items[n].get("uses", 0),
                                                    self.items[n].get("updated", 0)))
            self._retire_item(victim, "超出表情库上限", "system")

    # ------------------------------------------------------------------ 迭代（表情进化）
    def revise(self, name: str, patch: dict, by: str = "evolve", reason: str = "") -> tuple[str, str]:
        """修改已有表情（只写要改的字段）。返回 (状态, 说明)：revised / unchanged / refused / missing。"""
        with self._lock:
            name = self.resolve(name)
            if name in self.items:
                it = self.items[name]
                base = fr.normalize({"preset": patch["from"]}) if fr.is_preset(patch.get("from")) else it["spec"]
                new = apply_patch(base, patch)
                if spec_distance(new, it["spec"]) < UNCHANGED_DISTANCE:
                    return "unchanged", "改动太小，看不出区别"
                new, note, change = self._check_change(it["spec"], new)
                if new is None:
                    return "too_small", note
                self._push_history(it, it["spec"], it.get("last_by", it.get("origin", "")), reason)
                it.update(spec=_compact(new), updated=round(time.time(), 1), last_by=by, last_change=_compact(change))
                it["revisions"] = it.get("revisions", 0) + 1
                self._commit()
                return "revised", reason + note
            if name in fr.PRESETS:
                b = self.builtin.setdefault(name, {})
                if b.get("dislikes", 0) <= b.get("likes", 0):
                    return "refused", "内置表情只有在用户 👎 多于 👍 时才能改良"
                cur = fr.normalize({"preset": name})
                new = apply_patch(cur, patch)
                if spec_distance(new, cur) < UNCHANGED_DISTANCE:
                    return "unchanged", "改动太小，看不出区别"
                new, note, change = self._check_change(cur, new)
                if new is None:
                    return "too_small", note
                self._push_history(b, b.get("override"), b.get("last_by", "original"), reason)
                b.update(override=_compact(new), updated=round(time.time(), 1), last_by=by, last_change=_compact(change))
                self._commit()
                return "revised", reason + note
            return "missing", "没有这个表情"

    def rollback(self, name: str) -> bool:
        """回到上一个版本；内置表情回到最早时恢复原版。"""
        with self._lock:
            name = self.resolve(name)
            if name in self.items:
                hist = self.items[name].get("history") or []
                if not hist:
                    return False
                prev = hist.pop()
                self.items[name].update(spec=_compact(fr.normalize_full(prev["spec"])), updated=round(time.time(), 1),
                                        last_by="rollback")
                self._commit()
                return True
            b = self.builtin.get(name)
            if b and b.get("override") is not None:
                hist = b.get("history") or []
                prev = hist.pop() if hist else {"spec": None}
                b["override"] = _compact(fr.normalize_full(prev["spec"])) if prev.get("spec") else None
                b["last_by"] = "rollback"
                self._commit()
                return True
            return False

    def reset_builtin(self, name: str) -> bool:
        with self._lock:
            b = self.builtin.get(name)
            if not b or b.get("override") is None:
                return False
            b["override"], b["history"] = None, []
            self._commit()
            return True

    def retire(self, name: str, reason: str = "", by: str = "evolve") -> tuple[bool, str]:
        with self._lock:
            name = self.resolve(name)
            if name not in self.items:
                return False, "只能淘汰自创表情"
            if self.liked(name):
                return False, "用户 👍 过的表情不能淘汰"
            if name in self.protected and by != "user":
                return False, f"它是「{self.protected[name]}」风格里最好的作品，为了保留不同风格不淘汰"
            self._retire_item(name, reason, by)
            self._commit()
            return True, reason

    def merge(self, name: str, into: str, reason: str = "", by: str = "evolve") -> tuple[bool, str]:
        """把自创表情 name 并入 into（自创或内置）：name 进入已淘汰，旧名字变成别名，使用次数与评价并过去。"""
        with self._lock:
            name, into = self.resolve(name), self.resolve(into)
            if name not in self.items or name == into:
                return False, "只能把一个自创表情并入另一个表情"
            if into not in self.items and into not in fr.PRESETS:
                return False, "目标表情不存在"
            if self.liked(name):
                return False, "用户 👍 过的表情不能被并掉"
            if name in self.protected and by != "user":
                return False, f"它是「{self.protected[name]}」风格里最好的作品，为了保留不同风格不合并"
            src = self.items[name]
            dst = self.items.get(into) or self.builtin.setdefault(into, {})
            for k in ("uses", "likes", "dislikes"):
                dst[k] = dst.get(k, 0) + src.get(k, 0)
            self._retire_item(name, reason, by, merged_into=into)
            self.aliases[name] = into
            self._commit()
            return True, reason

    def restore(self, name: str) -> bool:
        with self._lock:
            if name not in self.retired or name in self.items:
                return False
            it = self.retired.pop(name)
            for k in ("retired", "retire_reason", "retired_by", "merged_into"):
                it.pop(k, None)
            self.items[name] = it
            self.aliases.pop(name, None)
            self._commit()
            return True

    # ------------------------------------------------------------------ 使用与反馈
    def touch(self, name: str) -> None:
        with self._lock:
            name = self.resolve(name)
            rec = self.items.get(name) if name in self.items else (
                self.builtin.setdefault(name, {}) if name in fr.PRESETS else None)
            if rec is None:
                return
            rec["uses"] = rec.get("uses", 0) + 1
            rec["last_used"] = round(time.time(), 1)
            self._mark(name)
        self._request_save(USAGE_SAVE_DELAY)      # 每次回复都会走到这里：只记下变化，攒一会儿在后台写

    def rate(self, name: str, vote: int, context: str = "") -> bool:
        """用户对某个表情的评价：vote=+1 👍 / -1 👎。"""
        with self._lock:
            name = self.resolve(name)
            rec = self.items.get(name) if name in self.items else (
                self.builtin.setdefault(name, {}) if name in fr.PRESETS else None)
            if rec is None or vote == 0:
                return False
            key = "likes" if vote > 0 else "dislikes"
            rec[key] = rec.get(key, 0) + 1
            self.feedback_log.append({"time": round(time.time(), 1), "name": name, "vote": "👍" if vote > 0 else "👎",
                                      "context": context[:80]})
            del self.feedback_log[:-MAX_FEEDBACK_LOG]
            self._mark(name)
        self._request_save(SAVE_DELAY)
        return True

    def set_context(self, name: str, context: str) -> None:
        with self._lock:
            if name not in self.items or self.items[name].get("context"):
                return
            self.items[name]["context"] = context[:80]
            self._mark(name)
        self._request_save(SAVE_DELAY)

    # ------------------------------------------------------------------ 手动管理
    def delete(self, name: str) -> None:
        with self._lock:
            if self.items.pop(name, None) is not None or self.retired.pop(name, None) is not None:
                self.aliases = {a: t for a, t in self.aliases.items() if a != name and t != name}
                self._commit()

    def rename(self, old: str, new: str) -> bool:
        with self._lock:
            if old not in self.items or not self.valid_name(new) or new in self.items:
                return False
            self.items[new] = self.items.pop(old)
            self.aliases = {a: (new if t == old else t) for a, t in self.aliases.items()}
            self.aliases[old] = new                      # 模型之前学会的旧名字照样能用
            self.aliases.pop(new, None)
            self._commit()
            return True

    # ------------------------------------------------------------------ 查询
    def names(self, limit: int = 20) -> list[str]:
        """按最近使用/创建排序的名字列表（给模型看的备忘）。"""
        with self._lock:
            order = sorted(self.items, key=lambda n: -max(self.items[n].get("last_used", 0), self.items[n].get("updated", 0)))
            return order[:limit]

    def describe(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return [{"name": n, "note": self.items[n].get("note", ""), "spec": fr.describe(self.items[n]["spec"])}
                    for n in self.names(limit)]

    def mirror(self, spec: dict, exclude: str = "", rig=None) -> dict:
        """照镜子：实际画出来的样子、最像哪个已有表情、（有骨架时）和它在字符画上的可见差异。"""
        out = {"looks": fr.mirror_text(spec)}
        nearest, _ = self._nearest(spec, exclude=exclude)
        if nearest:
            out["closest"] = fr.display_name(nearest) + ("" if nearest in self.items else f"（内置 {nearest}）")
            if rig is not None:
                try:
                    other = self.items[nearest]["spec"] if nearest in self.items else fr.normalize({"preset": nearest})
                    out["visible_diff"] = f"{rig.visible_diff(spec, other) * 100:.0f}%"
                except Exception:
                    pass
        return out

    def evolve_context(self, rig=None, limit: int = 30) -> dict:
        """交给表情进化的完整情况：内置/自创表情、使用次数、评价、版本、照镜子。"""
        now = time.time()

        def days(t):
            return round((now - t) / 86400, 1) if t else None

        with self._lock:
            builtin = {}
            for n in fr.PRESETS:
                b = self.builtin.get(n, {})
                d = {"cn": fr.PRESET_CN.get(n, n), "spec": fr.describe(fr.normalize({"preset": n}))}
                for k in ("uses", "likes", "dislikes"):
                    if b.get(k):
                        d[k] = b[k]
                if b.get("override") is not None:
                    d["improved"] = True
                if b.get("dislikes", 0) > b.get("likes", 0):      # 被嫌弃的内置表情：让她照镜子看看哪里不对
                    d["mirror"] = self.mirror(fr.normalize({"preset": n}), exclude=n, rig=rig)
                builtin[n] = d
            custom = []
            order = sorted(self.items, key=lambda n: -self.items[n].get("uses", 0))[:limit]
            for n in order:
                it = self.items[n]
                custom.append({"name": n, "emotion": it.get("note", ""), "origin": it.get("origin", ""),
                               **({"style": it["style"]} if it.get("style") else {}),
                               **({"elite": True} if n in self.protected else {}),
                               "uses": it.get("uses", 0), "days_since_used": days(it.get("last_used")),
                               "age_days": days(it.get("created")), "likes": it.get("likes", 0),
                               "dislikes": it.get("dislikes", 0), "versions": len(it.get("history") or []) + 1,
                               "last_context": it.get("context", ""), "spec": fr.describe(it["spec"]),
                               "mirror": self.mirror(it["spec"], exclude=n, rig=rig)})
            feedback = [{"name": f["name"], "vote": f["vote"], "context": f.get("context", ""),
                         "days_ago": days(f.get("time"))} for f in self.feedback_log[-12:]]
        out = {"builtin_presets": builtin, "custom_presets": custom, "feedback_log": feedback}
        if self.extra_context is not None:          # 组件库、创作偏好等（runtime 接上）
            try:
                out.update(self.extra_context())
            except Exception:
                pass
        return out

    def snapshot(self) -> list[dict]:
        """测试台列表：在用的自创表情、改良过的内置表情、已淘汰的表情。"""
        with self._lock:
            rows = [{"name": n, "kind": "custom", **{k: v for k, v in it.items() if k not in ("spec", "history")},
                     "versions": len(it.get("history") or []) + 1, "spec": fr.describe(it["spec"]),
                     "looks": fr.mirror_text(it["spec"])}
                    for n, it in sorted(self.items.items(), key=lambda kv: -kv[1].get("created", 0))]
            rows += [{"name": n, "kind": "builtin", "note": "改良过的内置表情", "origin": b.get("last_by", "evolve"),
                      "uses": b.get("uses", 0), "likes": b.get("likes", 0), "dislikes": b.get("dislikes", 0),
                      "versions": len(b.get("history") or []) + 1, "created": b.get("updated", 0),
                      "spec": fr.describe(b["override"]), "looks": fr.mirror_text(b["override"])}
                     for n, b in self.builtin.items() if b.get("override") is not None]
            rows += [{"name": n, "kind": "retired", **{k: v for k, v in it.items() if k not in ("spec", "history")},
                      "versions": len(it.get("history") or []) + 1, "spec": fr.describe(it["spec"]),
                      "looks": fr.mirror_text(it["spec"])}
                     for n, it in sorted(self.retired.items(), key=lambda kv: -kv[1].get("retired", 0))]
            return rows
