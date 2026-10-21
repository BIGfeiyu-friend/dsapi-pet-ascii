"""创作记忆与探索策略（v0.7）。<数据目录>/experiments.json，和桌宠的聊天记录、记忆完全分开。

记什么（不只是使用次数和 👍/👎）：
- 每次创作实验：目标风格、类型、创作意图、每一轮的设计变化、本地检查的实际结果（错误 / 警告 / 指标）、
  视觉模型的评价（如果有）、她自己的决定和理由、最终结果（采用 / 放弃）以及失败原因、花了多少 token 和时间
- 她自己的创作偏好：每次实验后她可以写一句“学到了什么 / 我喜欢什么”（最多保留 16 条，去重）
- 用户偏好：按风格汇总的 👍/👎（来自表情库和组件库）
- 自由演出（scene）：脱离骨架的字符画演出（协议 JSON，已校验），可以在对话里用 scene=名字 调用

候选设计只存在实验记录里；通过本地检查、由她决定采用之后才登记进正式的组件库 / 表情库。

探索与利用：每个风格（自然、夸张、抽象、古怪、可爱、极简）记录尝试 / 采用次数和质量，选目标时用 UCB
（成功率 + 质量 + 用户偏好 + 探索加成），另有一定概率完全随机，冷门的风格和类型会被优先尝试；
各风格里最好的作品被标成“代表作”，表情进化不会淘汰或合并它们，避免整个表情库收敛成一种风格。
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from pathlib import Path

from .parts import STYLE_CN, STYLES

KINDS = ("invent", "skeleton", "remix", "scene")
KIND_CN = {"invent": "发明新部件", "skeleton": "改造骨架", "remix": "重做旧作", "scene": "自由演出"}
KIND_WEIGHT = {"invent": 0.45, "skeleton": 0.2, "remix": 0.15, "scene": 0.2}
MAX_EXPERIMENTS = 60
MAX_NOTES = 16
MAX_VISION = 40
MAX_SCENES = 24

# 已采用的自由演出：名字 → 协议 JSON（对话标签 scene= 与自主演出用）
SCENES: dict = {}


class CreativeMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = {"experiments": [], "notes": [], "styles": {}, "kinds": {}, "vision": [], "scenes": {},
                     "budget": {}}
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                self.data.update({k: v for k, v in obj.items() if k in self.data})
        except Exception:
            pass
        self._publish()

    # ------------------------------------------------------------------ 存储
    def _publish(self) -> None:
        global SCENES
        SCENES = {n: s["anim"] for n, s in self.data["scenes"].items() if s.get("status", "active") == "active"}
        import sys

        sys.modules[__name__].SCENES = SCENES

    def save(self) -> None:
        with self._lock:
            text = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------------ 预算（每日）
    def _today(self) -> dict:
        b = self.data["budget"]
        day = time.strftime("%Y-%m-%d")
        if b.get("date") != day:
            b.clear()
            b.update(date=day, runs=0, tokens=0)
        return b

    def budget_left(self, daily_runs: int, daily_tokens: int) -> tuple[bool, str]:
        with self._lock:
            b = self._today()
            if b["runs"] >= daily_runs:
                return False, f"今天已经做了 {b['runs']} 次创作实验（上限 {daily_runs}）"
            if b["tokens"] >= daily_tokens:
                return False, f"今天的创作实验已用 {b['tokens']} token（上限 {daily_tokens}）"
            return True, ""

    def spend(self, tokens: int) -> None:
        with self._lock:
            b = self._today()
            b["tokens"] += int(tokens or 0)

    def last_run_time(self) -> float:
        exps = self.data["experiments"]
        return exps[-1]["time"] if exps else 0.0

    # ------------------------------------------------------------------ 实验记录
    def start(self, target: dict) -> dict:
        with self._lock:
            b = self._today()
            b["runs"] += 1
            exp = {"id": f"x{int(time.time() * 1000) % 10 ** 10}", "time": round(time.time(), 1),
                   "style": target["style"], "kind": target["kind"], "explore": target.get("explore", False),
                   "why": target.get("reason", ""), "status": "running", "iterations": []}
            self.data["experiments"].append(exp)
            del self.data["experiments"][:-MAX_EXPERIMENTS]
            self.save()
            return exp

    def add_iteration(self, exp: dict, entry: dict) -> None:
        with self._lock:
            exp["iterations"].append(entry)
            self.save()

    def finish(self, exp: dict, outcome: str, name: str = "", failure: str = "", intent: str = "",
               usage: dict | None = None, elapsed: float = 0.0, quality: float | None = None, adopted: dict | None = None) -> None:
        with self._lock:
            exp.update(status=outcome, name=name, failure=failure[:120], intent=intent[:100],
                       tokens=int((usage or {}).get("total_tokens", 0)), elapsed=round(elapsed, 1))
            if quality is not None:
                exp["quality"] = round(quality, 3)
            if adopted:
                exp["adopted"] = adopted
            for key, table in ((exp["style"], "styles"), (exp["kind"], "kinds")):
                st = self.data[table].setdefault(key, {"tries": 0, "adopted": 0, "quality": 0.0})
                st["tries"] += 1
                if outcome == "adopted":
                    st["adopted"] += 1
                    q = quality if quality is not None else 0.6
                    st["quality"] = round(st["quality"] + (q - st["quality"]) / st["adopted"], 3)
            self.save()

    def add_note(self, note: str) -> None:
        note = (note or "").strip()[:80]
        if len(note) < 4:
            return
        with self._lock:
            notes = [n for n in self.data["notes"] if n["text"] != note]
            notes.append({"text": note, "time": round(time.time(), 1)})
            self.data["notes"] = notes[-MAX_NOTES:]
            self.save()

    def add_vision(self, entry: dict) -> None:
        with self._lock:
            self.data["vision"].append(entry)
            del self.data["vision"][:-MAX_VISION]
            self.save()

    def attach_vision(self, exp_id: str, iteration: int, result: dict) -> None:
        """异步到达的视觉评价写回对应的实验记录（实验可能已经结束）。"""
        with self._lock:
            for exp in reversed(self.data["experiments"]):
                if exp["id"] == exp_id and 0 <= iteration < len(exp["iterations"]):
                    exp["iterations"][iteration]["vision"] = result
                    break
            self.save()

    # ------------------------------------------------------------------ 自由演出
    def add_scene(self, name: str, anim: dict, style: str, intent: str) -> tuple[str, str]:
        from .parts import valid_name

        if not valid_name(name):
            return name, "invalid"
        with self._lock:
            status = "revised" if name in self.data["scenes"] else "new"
            self.data["scenes"][name] = {"anim": anim, "style": style, "intent": intent[:80], "status": "active",
                                         "created": round(time.time(), 1), "uses": 0}
            live = [n for n, s in self.data["scenes"].items() if s.get("status", "active") == "active"]
            if len(live) > MAX_SCENES:
                victim = min(live, key=lambda n: (self.data["scenes"][n].get("uses", 0), self.data["scenes"][n]["created"]))
                self.data["scenes"][victim]["status"] = "retired"
            self._publish()
            self.save()
            return name, status

    def retire_scene(self, name: str) -> bool:
        with self._lock:
            s = self.data["scenes"].get(name)
            if not s or s.get("status") == "retired":
                return False
            s["status"] = "retired"
            self._publish()
            self.save()
            return True

    def touch_scene(self, name: str) -> None:
        with self._lock:
            if name in self.data["scenes"]:
                self.data["scenes"][name]["uses"] = self.data["scenes"][name].get("uses", 0) + 1

    # ------------------------------------------------------------------ 偏好与代表作
    @staticmethod
    def user_style_votes(store, parts_lib) -> dict:
        """按风格汇总用户的 👍/👎（表情库里带风格的表情 + 组件库）。"""
        out = {s: {"likes": 0, "dislikes": 0, "count": 0} for s in STYLES}
        rows = []
        for it in getattr(store, "items", {}).values():
            if it.get("style") in out:
                rows.append((it["style"], it))
        for it in getattr(parts_lib, "items", {}).values():
            if it.get("style") in out and it.get("status") == "active":
                rows.append((it["style"], it))
        for s, it in rows:
            out[s]["likes"] += it.get("likes", 0)
            out[s]["dislikes"] += it.get("dislikes", 0)
            out[s]["count"] += 1
        return out

    @staticmethod
    def elites(store) -> dict:
        """各风格的代表作：👍 多、用得多、质量高的那个（表情名 → 风格）。"""
        best = {}
        for n, it in getattr(store, "items", {}).items():
            s = it.get("style")
            if s not in STYLES:
                continue
            score = it.get("likes", 0) * 3 - it.get("dislikes", 0) * 3 + min(it.get("uses", 0), 20) * 0.2 + \
                it.get("quality", 0.5)
            if s not in best or score > best[s][1]:
                best[s] = (n, score)
        return {n: s for s, (n, _) in best.items()}

    # ------------------------------------------------------------------ 选目标（探索 / 利用）
    def choose_target(self, rng: random.Random | None = None, store=None, parts_lib=None, style: str | None = None,
                      kind: str | None = None, epsilon: float = 0.25) -> dict:
        rng = rng or random.Random()
        votes = self.user_style_votes(store, parts_lib) if store is not None else {}
        with self._lock:
            stats = self.data["styles"]
            total = sum(v.get("tries", 0) for v in stats.values())
            explore = False
            reason = ""
            if style not in STYLES:
                if rng.random() < epsilon:
                    style = rng.choice(STYLES)
                    explore, reason = True, "随机探索"
                else:
                    def score(s):
                        st = stats.get(s, {})
                        t, a = st.get("tries", 0), st.get("adopted", 0)
                        v = votes.get(s, {})
                        pref = math.tanh((v.get("likes", 0) - v.get("dislikes", 0)) / 3.0)
                        return ((a + 1) / (t + 2) + 0.3 * st.get("quality", 0.0) + 0.2 * pref
                                + 0.6 * math.sqrt(math.log(total + 2) / (t + 1)))
                    ranked = sorted(STYLES, key=score, reverse=True)
                    style = ranked[0]
                    t = stats.get(style, {}).get("tries", 0)
                    explore = t < 2
                    reason = "很少尝试的风格（探索）" if explore else "成功率 / 质量 / 你的喜好综合最高（利用）"
            else:
                reason = "指定风格"
            if kind not in KINDS:
                kst = self.data["kinds"]
                has_parts = bool(parts_lib is not None and parts_lib.active())
                weights = []
                for k in KINDS:
                    w = KIND_WEIGHT[k]
                    if k == "remix" and not has_parts and not getattr(store, "items", None):
                        w = 0.0
                    w *= 1.0 + 0.8 / (1 + kst.get(k, {}).get("tries", 0))       # 冷门的类型多试试
                    weights.append(w)
                kind = rng.choices(KINDS, weights=weights)[0]
            return {"style": style, "kind": kind, "explore": explore, "reason": reason}

    # ------------------------------------------------------------------ 给模型的上下文
    def context(self, limit: int = 8) -> dict:
        with self._lock:
            recent = []
            for e in self.data["experiments"][-limit:]:
                last = e["iterations"][-1] if e["iterations"] else {}
                row = {"style": e["style"], "kind": e["kind"], "intent": e.get("intent", ""), "result": e["status"],
                       "name": e.get("name", "")}
                if e.get("failure"):
                    row["failure"] = e["failure"]
                if last.get("local_errors"):
                    row["last_errors"] = last["local_errors"][:3]
                v = last.get("vision") or {}
                if v.get("summary"):
                    row["vision"] = v["summary"][:100]
                recent.append(row)
            styles = {s: {"tries": v.get("tries", 0), "adopted": v.get("adopted", 0), "quality": v.get("quality", 0)}
                      for s, v in self.data["styles"].items()}
            return {"recent_experiments": recent, "my_notes": [n["text"] for n in self.data["notes"][-10:]],
                    "style_stats": styles,
                    "scenes": [{"name": n, "style": s.get("style"), "intent": s.get("intent", "")}
                               for n, s in self.data["scenes"].items() if s.get("status", "active") == "active"][:10]}

    def snapshot(self, limit: int = 30) -> list:
        with self._lock:
            return json.loads(json.dumps(self.data["experiments"][-limit:]))
