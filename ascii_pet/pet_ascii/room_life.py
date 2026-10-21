"""虚拟房间（v1.0）的生活循环：没有任何人发指令，她自己决定什么时候进房间、在里面做什么。

在桌宠进程里运行（主人不用打开“房间”窗口）。每分钟检查一次：
  - 到了下次“想进去”的时间（20~60 分钟，按精力伸缩；夜里间隔 ×3），主人这几分钟没在和她聊天，没有别的后台任务 → 进去
  - 睡着时：精力够才会自己醒来（伸个懒腰）进去，出来以后接着睡；精力不够就继续睡。
    这种时候她能选的“过多久再来”范围 × room_asleep_interval_factor（默认 90~270 分钟，v1.3.5），选多少就睡多少
  - 每次进去最多走几步（读了文件会接着决定下一步；她也可以说“还想待着”）
  - 主人发来消息：做完手上这一步就出来陪主人
每一步：看到房间和自己的状态 → 主模型（开思考）给出想法 + 动作 → 程序检查并执行 → 记进原始日志和短期记忆。
无聊：重复做差不多的事会变无聊；但对很相似的事情做出相似的反应，涨得少（这是她的性格，不是惯性）。
相似的过去会浮现成联想；她做了和以前很像的事时，下一步会“注意到”。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from . import face_rig as fr
from .config import data_dir, pet_app_config
from .persona import acquire_lease, release_lease
from .chat_memory import ChatMemory
from .room import GIFT_DIR, Room, RoomError, similarity
from .room_image import Looker, is_image
from .room_image import render as render_image
from .room_memory import RoomMemory

log = logging.getLogger("pet.ascii")

WEEK = "一二三四五六日"
# 每种动作花多少精力（负数 = 恢复）
COST = {"write": 0.07, "append": 0.05, "edit": 0.04, "draw": 0.08, "read": 0.02, "look": 0.03, "music": 0.01, "rename": 0.01, "move": 0.01,
        "mkdir": 0.01, "delete": 0.01, "note": 0.03, "rest": -0.06, "sleep": 0.0, "nothing": 0.0}
VERB = {"write": "写了", "append": "接着写了", "edit": "改了", "draw": "画了", "read": "看了", "look": "看了图片", "music": "放歌", "rename": "改名",
        "move": "搬动", "mkdir": "新建文件夹", "delete": "扔进回收站", "note": "给主人留了便条", "rest": "歇了一会儿",
        "sleep": "回去睡觉", "nothing": "就这样待着"}
THINK = {"write": "write", "append": "append", "edit": "edit", "draw": "draw", "read": "read", "look": "look", "music": "play", "rename": "rename",
         "move": "move", "mkdir": "mkdir", "delete": "trash", "note": "note", "rest": "rest", "sleep": "sleep",
         "nothing": "idle"}
BORED_BASE = 0.03       # 每做一件事的基础无聊
BORED_REPEAT = 0.30     # 重复（且不是因为相似的事情）带来的无聊
BORED_NOVEL = 0.08      # 做了新鲜事，无聊下降
ECHO_AT = 0.55          # 和以前某次做的事相似到这个程度：她会“注意到”


class RoomState:
    """room_state.json：精力 / 无聊 / 上次和主人说话的时间 / 日程 / 用量。只由桌宠进程写，“房间”窗口只读。"""

    def __init__(self, path: Path, now: float):
        self.path = Path(path)
        self.data = {"version": 1, "energy": 0.75, "boredom": 0.2, "t": now, "last_user_chat": now,
                     "day": _day(now), "wakes_today": 0, "tokens_today": 0, "total_wakes": 0, "total_tokens": 0,
                     "next_wake": 0.0, "last_wake": 0.0, "inbox_offset": 0, "pending": [], "signature": None,
                     "inside": False, "status": "", "heartbeat": 0.0, "pid": 0, "echo": None, "first": True,
                     "read_marks": {}, "chat_seen_ts": 0.0}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data.update(raw)
        except (OSError, ValueError):
            pass
        self.data["inside"] = False                          # 上次退出时在房间里：重启后算已经出来了

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def evolve(self, now: float, asleep: bool) -> None:
        """随时间变化：精力慢慢恢复（睡着恢复得快），无聊慢慢消退（半衰期 8 小时），新的一天无聊少一些。"""
        d = self.data
        dt = max(0.0, min(now - float(d.get("t") or now), 3 * 86400))
        d["energy"] = round(min(1.0, d["energy"] + dt / 3600 * (0.20 if asleep else 0.10)), 4)
        d["boredom"] = round(d["boredom"] * 0.5 ** (dt / (8 * 3600)), 4)
        d["t"] = now
        today = _day(now)
        if d.get("day") != today:
            d["day"], d["wakes_today"], d["tokens_today"], d["looks_today"] = today, 0, 0, 0
            d["boredom"] = round(d["boredom"] * 0.6, 4)

    def longing(self, now: float) -> float:
        h = max(0.0, now - float(self.data.get("last_user_chat") or now)) / 3600
        return round(1 - 0.5 ** (h / 8), 3)


class RoomLife(QObject):
    step_signal = Signal(object)        # 一步做完（工作线程 → 界面线程：换表情、更新标题）
    done_signal = Signal(object)        # 这次从房间出来
    memory_signal = Signal(object)      # 一次记忆整理完成

    def __init__(self, rt, root: Path | None = None, autorun: bool = False, clock=time.time,
                 rng: random.Random | None = None):
        super().__init__()
        self.rt = rt
        self.cfg = rt.cfg
        self.room = Room(root)
        self.clock = clock
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        had_state = (self.room.root / "room_state.json").exists()
        self.state = RoomState(self.room.root / "room_state.json", clock())
        self.memory = RoomMemory(self.room.root / "room_memory.json")
        seen = float(self.state.data.get("chat_seen_ts") or 0.0)    # 聊天里已经给她看过的房间近况到哪儿
        if not seen and had_state:                                   # v1.3.8 之前的状态：上次聊天时她已经看过当时的近况
            seen = float(self.state.data.get("last_user_chat") or 0.0)
        self._chat_seen_ts = seen
        self.autorun = autorun
        self.busy = False
        self.consolidating = False
        self.cancel = threading.Event()
        self.session: dict | None = None
        self.last_report: dict | None = None
        self.last_memory: dict | None = None
        self.client_factory = None                    # 测试用：替换模型客户端
        self._own_paths: set = set()                  # 她自己改过的路径（比较房间变化时不算“别人做的”）
        self.looker = Looker(self.room.root / "seen.json")   # 看过的图片（v1.2）：转成文字的样子、她自己觉得是什么
        self.vision_fn = None                         # 测试用：替换“能看图的副模型”
        self.is_owner = False                         # 多开时只有一个实例负责房间；其它实例给聊天看房间时从磁盘读
        # 聊天的记忆（只读桌宠记忆库），房间里的她也记得和你聊过什么（v1.1）
        self.chat_memory = ChatMemory(str(pet_app_config().get("character") or ""),
                                      recent=int(getattr(self.cfg, "room_chat_recent", 12)))
        self._disk_view = (None, None, None)          # (修改时间, RoomMemory, RoomState)：非负责实例的只读视图
        self.lease = self.room.root / "room.lease"
        self.owner_lease = self.room.root / "room.owner"
        self.step_signal.connect(self._on_step, Qt.QueuedConnection)
        self.done_signal.connect(self._on_done, Qt.QueuedConnection)
        self.memory_signal.connect(self._on_memory_done, Qt.QueuedConnection)
        with self.lock:
            if "notes" not in self.state.data:            # v1.1 之前留的便条：从原始日志里补一次原文
                self.state.data["notes"] = [{"ts": e["ts"], "text": (e.get("result") or {}).get("text", "")}
                                            for e in self.room.tail_log(400)
                                            if e.get("type") == "step" and e.get("action") == "note" and e.get("ok")][-5:]
            if not self.state.data.get("read_marks_backfilled"):  # v1.2.3 之前读过的：从原始日志里补一次“读过 / 回过”
                self._backfill_read_marks()
            if not self.state["next_wake"]:
                self.state["next_wake"] = clock() + self.rng.uniform(4, 9) * 60     # 第一次：很快就会去看看
                self.state["last_user_chat"] = _last_chat_guess(rt, clock())
            self.state.save()

    # ------------------------------------------------------------------ 小工具
    @property
    def sleep(self):
        return self.rt.sleep

    def asleep(self) -> bool:
        return self.sleep.state == "asleep"

    def night(self, now: float | None = None) -> bool:
        h = datetime.fromtimestamp(now or self.clock()).hour
        a, b = int(self.cfg.room_night_start), int(self.cfg.room_night_end)
        return (a <= h < b) if a <= b else (h >= a or h < b)

    def status_text(self) -> str:
        if not self.busy or self.session is None:
            return ""
        return "在房间里" + (f"：{self.session['doing']}" if self.session.get("doing") else "")

    def _save(self) -> None:
        with self.lock:
            try:
                self.state.save()
            except OSError:
                log.exception("字符画·房间：保存状态失败")

    def _log(self, ev: dict) -> dict:
        try:
            return self.room.append_log(ev)
        except OSError:
            log.exception("字符画·房间：写日志失败")
            return {"id": "", **ev}

    # ------------------------------------------------------------------ 主人
    def on_user(self, chat: bool = False, interrupt: bool = True) -> None:
        """主人有互动（chat=True：发来消息）。interrupt：在房间里的话，做完手上这一步就出来（拖窗口、开菜单不算）。"""
        now = self.clock()
        with self.lock:
            if chat:
                self.state["last_user_chat"] = now
            if self.busy and interrupt:
                self.cancel.set()
                if self.session is not None:
                    self.session["user"] = True
            # 主人在：之后至少隔 room_after_chat_min 分钟再进去
            self.state["next_wake"] = max(self.state["next_wake"], now + self.cfg.room_after_chat_min * 60)

    # ------------------------------------------------------------------ 每分钟
    def shutdown(self) -> None:
        """桌宠退出：记下状态，放开租约（在房间里的话，下次启动算已经出来了）。"""
        with self.lock:
            self.state["inside"] = False
            self.state["status"] = ""
            self.state["heartbeat"] = 0.0
        self._save()
        if self.autorun:
            release_lease(self.owner_lease)
        if self.busy:
            self.cancel.set()
            release_lease(self.lease)

    def check(self) -> None:
        if not (self.cfg.room_enabled and self.autorun):
            return
        try:
            self._check()
        except Exception:
            log.exception("字符画·房间：定时检查出错")

    def _check(self) -> None:
        now = self.clock()
        if not acquire_lease(self.owner_lease, ttl=300):      # 开了好几个桌宠：只有一个负责房间
            self.is_owner = False
            return
        self.is_owner = True
        with self.lock:
            self.state.evolve(now, self.sleep.sleeping)
            self.state["heartbeat"] = now
            self.state["pid"] = os.getpid()
            self.state["pet_state"] = self.sleep.state            # 给“房间”窗口看：醒着 / 睡着
        self._take_inbox(now)
        if not self.busy:                                     # 她正在房间里改东西时不比较（出来以后再看）
            self._notice_changes(now)
        try:
            snap = self.room.snapshot_daily(keep=int(self.cfg.room_snapshot_keep))
            if snap:
                self._log({"type": "snapshot", "name": snap})
        except OSError:
            log.exception("字符画·房间：快照失败")
        with self.lock:
            exp = self.memory.expire(now, int(self.cfg.room_long_max))
            if exp["promoted"] or exp["faded"] or exp["dropped_long"]:
                self.memory.save()
        self.maybe_consolidate(now)
        if self.autorun:
            ok, why = self.due(now)
            if ok:
                self.enter()
            elif why == "睡着了，精力还不够":
                with self.lock:                          # 精力不够：过一阵再看
                    self.state["next_wake"] = now + self.rng.uniform(20, 40) * 60
        self._save()

    def _take_inbox(self, now: float) -> None:
        events, off = self.room.read_inbox(int(self.state["inbox_offset"]))
        with self.lock:
            for ev in events:
                if ev.get("type") == "gift":
                    self._gift(now, ev.get("path", ""), ev.get("note", ""), ev.get("size", 0), "window")
            self.state["inbox_offset"] = off

    def _gift(self, now: float, path: str, note: str, size: int, via: str) -> None:
        """收到一件礼物（从“房间”窗口送来的，或主人直接放进礼物文件夹的）。同一个文件只算一次。"""
        seen = self.state.data.setdefault("gifts_seen", [])
        if not path or path in seen:
            return
        seen.append(path)
        del seen[:-300]
        logged = self._log({"type": "gift", "path": path, "name": path.split("/", 1)[-1], "note": note,
                            "size": size, "via": via})
        self.state["pending"] = (self.state["pending"] + [{"kind": "gift", "path": path, "note": note,
                                                           "id": logged["id"], "ts": now}])[-20:]
        self.state["boredom"] = round(max(0.0, self.state["boredom"] - 0.25), 4)     # 收到礼物：不那么无聊了
        # 几分钟后会去看看（主人正在聊天时还是等聊完）
        self.state["next_wake"] = min(self.state["next_wake"], now + self.rng.uniform(2, 6) * 60)

    def _notice_changes(self, now: float) -> None:
        """不是她做的变化（主人在资源管理器里改了、加了、删了东西）：记下来，下次进房间时她会看到。
        直接放进礼物文件夹的新文件算礼物。"""
        sig = self.room.signature()
        with self.lock:
            old = self.state["signature"]
            self.state["signature"] = sig
            own, self._own_paths = self._own_paths, set()
            if old is None:
                return

            def mine(path):
                return any(path == p or path.startswith(p + "/") for p in own)

            changes = []
            for path, v in sig.items():
                if mine(path):
                    continue
                if path not in old:
                    if path.startswith(GIFT_DIR + "/") and v[0] >= 0:
                        self._gift(now, path, "", v[0], "folder")
                    else:
                        changes.append({"path": path, "change": "多了"})
                elif old[path] != v and v[0] >= 0:
                    changes.append({"path": path, "change": "被改动过"})
            for path in old:
                if path not in sig and not mine(path):
                    changes.append({"path": path, "change": "不见了"})
            if changes:
                ev = self._log({"type": "change", "changes": changes[:30]})
                self.state["pending"] = (self.state["pending"] + [{"kind": "change", "changes": changes[:30],
                                                                   "id": ev["id"], "ts": now}])[-20:]

    def visit_range(self, woke: bool) -> tuple[float, float]:
        """她能选的“过多少分钟再来”范围（分钟）。睡着时醒来进的（出来后接着睡）：最小、最大都 × room_asleep_interval_factor。"""
        lo, hi = float(self.cfg.room_interval_min), float(self.cfg.room_interval_max)
        if woke:
            f = max(1.0, float(getattr(self.cfg, "room_asleep_interval_factor", 1.0) or 1.0))
            lo, hi = lo * f, hi * f
        return lo, hi

    def due(self, now: float | None = None) -> tuple[bool, str]:
        now = now or self.clock()
        rt, cfg, st = self.rt, self.cfg, self.state
        if self.busy or self.consolidating:
            return False, "已经在房间里"
        if now < st["next_wake"]:
            return False, "还没到时间"
        if any(getattr(rt, a, False) for a in ("_experimenting", "_evolving", "_reflecting")) or rt.director.busy:
            return False, "别的后台任务在跑"
        if self.sleep.state in ("falling", "waking"):
            return False, "正在入睡 / 被吵醒"
        if not self.sleep.sleeping and self.sleep.idle_seconds() < cfg.room_after_chat_min * 60:
            return False, "主人刚刚还在"
        if getattr(rt, "_idle_pending", None) is not None:
            return False, "说完话还没进入挂机"
        if st["wakes_today"] >= cfg.room_daily_wakes:
            return False, "今天进房间的次数用完了"
        if st["tokens_today"] >= cfg.room_daily_tokens:
            return False, "今天房间的 token 预算用完了"
        ok, why = rt.store.budget.check(cfg)
        if not ok:
            return False, why
        if self.sleep.sleeping and st["energy"] < cfg.room_wake_energy:
            return False, "睡着了，精力还不够"
        return True, ""

    # ------------------------------------------------------------------ 进房间
    def enter(self, force: bool = False, client=None, sync: bool = False) -> bool:
        """进房间（force：测试台 / 自检，不看时间和精力；sync：在当前线程跑完，测试用）。"""
        if self.busy:
            return False
        if not acquire_lease(self.lease, ttl=900):
            return False
        now = self.clock()
        woke = self.sleep.sleeping
        self.busy = True
        self.cancel.clear()
        self.session = {"start": now, "woke": woke, "pre_activity": self.sleep.last_activity, "user": False,
                        "doing": "", "steps": [], "tokens": 0, "saved_think": self.rt.engine.think_text,
                        "night": self.night(now)}
        with self.lock:
            self.state["inside"] = True
            self.state["status"] = "进房间"
            self.state["last_wake"] = now
            self.state["wakes_today"] += 1
            self.state["total_wakes"] += 1
        self._save()
        if woke and not sync:                         # 睡着时：自己醒过来（伸个懒腰），醒好了再进去
            self.sleep.wake("room", "stretch")
            self.sleep.after_wake(lambda: self._begin(client, sync))
        else:
            if woke:
                self.sleep.end_sleep()
            self._begin(client, sync)
        return True

    def _begin(self, client, sync: bool) -> None:
        s = self.session
        if s is None:
            return
        s["saved_think"] = self.rt.engine.think_text
        how = "睡了一会儿，自己醒了" if s["woke"] else "醒着，主人这会儿不在"
        with self.lock:
            st = self.state
            ev = self._log({"type": "enter", "how": how, "energy": st["energy"], "boredom": st["boredom"],
                            "longing": self.state.longing(s["start"]), "night": s["night"]})
        s["enter_id"] = ev["id"]
        self.rt.engine.set_think("<ROOM> enter();")
        self.rt._notify()
        log.info("字符画·房间：进房间（%s，精力 %.2f，无聊 %.2f）", how, self.state["energy"], self.state["boredom"])
        if sync:
            self._work(client, self._on_step, self._on_done)
        else:
            threading.Thread(target=self._work, args=(client, self.step_signal.emit, self.done_signal.emit),
                             name="pet-ascii-room", daemon=True).start()

    def _client(self, client=None):
        if client is not None:
            return client
        if self.client_factory is not None:
            return self.client_factory()
        from .ai.client import make_client

        c, _ = make_client(self.cfg)
        model = self.cfg.room_model or ((pet_main_model()) or self.cfg.model)
        if hasattr(c, "model"):
            c.model = c.name = model
        if hasattr(c, "disable_thinking"):
            c.disable_thinking = not self.cfg.room_thinking
        return c

    def _work(self, client, emit_step, emit_done) -> None:
        from .ai import room_ai

        s = self.session
        rep = {"steps": 0, "tokens": 0, "why": "", "error": ""}
        try:
            c = self._client(client)
            if type(c).__name__ == "MockClient":
                raise RuntimeError("没有可用的 API Key（Mock 模式不进房间）")
            history: list = []
            chat_mem = self._chat_memory_text()            # 这次进房间期间不变（几步之间前缀缓存命中）
            steps_max = max(1, int(self.cfg.room_max_steps))
            for i in range(steps_max + 1):
                if self.cancel.is_set():
                    rep["why"] = "主人来找我了"
                    break
                # 步数用完了；只有最后一步是在读 / 看东西时多给一步，让她看到读到、看到的内容
                if i >= steps_max and not (history and history[-1]["type"] in ("read", "look") and history[-1].get("ok")):
                    rep["why"] = rep["why"] or "这次就到这里"
                    break
                now = self.clock()
                situation, stimulus = self.situation(now, history)
                msgs = room_ai.decision_messages(self._canon(), self.rt.pet_name(), situation, self._faces(),
                                                 self.cfg.room_observer_known, chat_mem,
                                                 self.visit_range(bool((self.session or {}).get("woke"))),
                                                 music=self._music_ok())
                try:
                    out = room_ai.complete_json(c, msgs, self.cfg.room_max_tokens, self.cfg.room_timeout_s,
                                                self.cfg.room_thinking, self.cfg.room_reasoning_effort,
                                                need=("action", "thought"))
                except Exception as exc:
                    used = room_ai.total_tokens(getattr(exc, "usage", {}) or {})
                    rep["tokens"] += used
                    raise
                tokens = room_ai.total_tokens(out["usage"])
                rep["tokens"] += tokens
                rep["billable"] = rep.get("billable", 0) + room_ai.billable_tokens(out["usage"])
                d = room_ai.parse_decision(out["obj"])
                prev = history[-1] if history else None
                if prev is not None and prev["type"] == "look" and prev.get("ok") and not prev.get("saw"):
                    if d.get("saw"):
                        self._record_saw(prev, d["saw"])           # 她自己写下的“我觉得图里是……”
                    elif prev.get("vision_brief"):                 # 她没写：记忆里先记副模型的第一句（注明是它说的）
                        self._record_saw(prev, f"（副模型说）{prev['vision_brief']}", hers=False)
                step = self.execute(d, now, stimulus, out, tokens, i)
                if step.get("extra_tokens"):                      # 副模型看图的 token 也算进这次
                    rep["tokens"] += step["extra_tokens"]
                    rep["billable"] = rep.get("billable", 0) + step["extra_tokens"]
                if d.get("next_visit") is not None:            # 她说的“过多久再来”（以最后说的为准）
                    rep["next_visit"] = d["next_visit"]
                rep["steps"] += 1
                history.append(step)
                emit_step(step)
                if d["type"] in ("sleep", "rest", "nothing"):
                    rep["why"] = {"sleep": "想睡了", "rest": "歇一会儿", "nothing": "待够了"}[d["type"]]
                    break
                if d["type"] not in ("read", "look") and d["then"] != "stay":
                    rep["why"] = "做完了"
                    break
                if i >= steps_max:
                    rep["why"] = "这次就到这里"
                    break
                with self.lock:
                    if self.state["energy"] < 0.08:
                        rep["why"] = "累了"
                        break
                    if self.state["tokens_today"] >= self.cfg.room_daily_tokens:
                        rep["why"] = "今天的预算用完了"
                        break
            else:
                rep["why"] = rep["why"] or "这次就到这里"
        except Exception as exc:
            log.exception("字符画·房间：在房间里出错")
            rep["error"] = str(exc)[:200]
        emit_done(rep)

    # ------------------------------------------------------------------ 她看到的
    def situation(self, now: float, history: list) -> tuple[str, str]:
        """组装她这一步看到的一切。返回 (给模型的文字, 刺激摘要)。刺激 = 新发生的事（礼物、变化、刚读到的内容）。"""
        st, s = self.state, self.session or {}
        dt = datetime.fromtimestamp(now)
        part = _part_of_day(dt.hour)
        lines = [f"现在：{dt:%Y-%m-%d} 周{WEEK[dt.weekday()]} {dt:%H:%M}（{part}）"]
        if st["first"]:
            lines.append("这是你第一次来到这个房间。")
        if not history:
            lines.append("你是怎么来的：" + ("睡了一会儿，自己醒了过来" if s.get("woke") else "醒着，主人这会儿不在身边"))
        with self.lock:
            energy, boredom, longing = st["energy"], st["boredom"], st.longing(now)
            since = now - max(float(st["last_user_chat"]), self._last_chat_ts())
            pending = list(st["pending"])
        lines.append(f"你的状态：精力 {energy:.2f}（{_energy_word(energy)}）  无聊 {boredom:.2f}（{_bored_word(boredom)}）  "
                     f"想念主人 {longing:.2f}（上次和主人说话：{_ago(since)}）")
        marks = {}
        stim = []
        news = []
        for p in pending:
            if p["kind"] == "gift":
                marks[p["path"]] = "← 主人送的，新的！还没看过"
                news.append(f"主人往礼物文件夹里放了「{p['path'].split('/', 1)[-1]}」" +
                            (f"（附言：{p['note']}）" if p.get("note") else ""))
                stim.append(f"礼物 {p['path']} {p.get('note', '')}")
            elif p["kind"] == "change":
                for ch in p["changes"][:8]:
                    marks.setdefault(ch["path"], f"← {ch['change']}（不是你做的）")
                    stim.append(f"变化：{ch['path']} {ch['change']}")
                news.append("房间里有些变化不是你做的：" + "、".join(f"「{c['path']}」{c['change']}" for c in p["changes"][:8]))
        try:
            seen = self.looker.saw_map(self.room)
        except Exception:
            seen = {}
        for path, m in self._read_marks_text(now).items():         # 读过 / 看过 / 回过便条（新礼物、别人改动的标记优先）
            marks.setdefault(path, m)
        lines.append("\n房间里有：\n" + self.room.tree_text(mark=marks, seen=seen))
        if news:
            lines.append("\n自上次以来：\n" + "\n".join(f"- {n}" for n in news))
        with self.lock:
            recent = self.memory.timeline(now)
            gists = self.memory.gist_context(6)
        if recent:
            detail_from = len(recent) - 8                                  # 最近 8 件带上当时的想法，更早的只留一行
            lines.append("\n你最近在房间里做过的事（短期记忆：今天的全部，和昨天最后几件）：\n" + "\n".join(
                f"- {_when(it['ts'], now)} {_short(it['what'], 90 if i >= detail_from else 60)}"
                + (f"——当时想：{_short(it['thought'], 60)}" if it.get("thought") and i >= detail_from else "")
                for i, it in enumerate(recent)))
        if gists:
            lines.append("\n这几天的印象：\n" + "\n".join(f"- {_when(g['ts'], now)} {g['text']}" for g in gists))
        # 刺激：新发生的事 + 这次刚读到的内容
        for h in history[-2:]:
            if h["type"] == "read" and h.get("ok"):
                stim.append(f"{h['args'].get('path', '')} {h.get('read_text', '')[:300]}")
            if h["type"] == "look" and h.get("ok"):
                stim.append(f"{h.get('look_path', '')} {h.get('saw', '')} {h.get('look_hint', '')}")
        stimulus = " ".join(stim)
        # 联想的线索：新发生的事（每件分开比）；没有新鲜事时，就是她最近在做的事
        cues = stim or [it.get("what", "") for it in recent[-2:]]
        query = " ".join(cues)
        with self.lock:
            longs = self.memory.long_context(query, 8)
            exclude = {it["id"] for it in recent[-3:]} | {h.get("id") for h in history}
            assoc = self.memory.recall(cues, now, k=3, exclude=exclude, touch=not history) if query.strip() else []
        if longs:
            lines.append("\n你记得的（长期记忆）：\n" + "\n".join(f"- {_when(m['ts'], now)} {m['text']}" for m in longs))
        if assoc:
            lines.append("\n联想（和现在有点像的过去，浮现在脑海里）：\n" + "\n".join(
                f"- {_when(a['ts'], now)} {a['text']}" + (f"（当时想：{a['thought']}）" if a.get("thought") else "")
                for a in assoc))
        echo = st["echo"] if not history else None
        if echo:
            lines.append(f"\n你注意到：上次在房间里，你{echo['what']}，和 {_when(echo['past_ts'], now)} 那次"
                         f"「{echo['past_what']}」很像。")
        if history:
            lines.append("\n这次进来已经做了：")
            for h in history:
                lines.append(f"- {h['what']}" + ("" if h.get("ok") else f"（没成功：{h.get('error', '')}）"))
                if h["type"] == "read" and h.get("ok"):
                    more = f"\n（后面还有 {h['read_more']} 字，想接着看可以 read 并写 offset:{h['read_next']}）" \
                        if h.get("read_more") else ""
                    lines.append(f"  内容：\n{h.get('read_text', '')}{more}")
                if h["type"] == "look" and h.get("ok"):
                    lines.append("  你看到的（图片转成字符画、颜色和文字以后的样子；细节会丢，识别也可能有错）：\n"
                                 + h.get("look_text", "")
                                 + ("" if h.get("saw") else "\n  看完以后，在 saw 里用一句话写下你觉得图里是什么。"))
                if h.get("echo"):
                    e = h["echo"]
                    lines.append(f"  （你注意到：这和 {_when(e['past_ts'], now)} 那次「{e['past_what']}」很像）")
        mus = getattr(self.rt, "music", None)
        if self._music_ok():
            info = [x for x in (mus.now_text(), mus.last_text(), mus.liked_text(6)) if x]
            ok, why, left = mus.quota()
            info.append(f"现在还能放 {left} 首" if ok else why)
            lines.append("\nSpotify：" + "。".join(info))
        lines.append("\n想做什么都可以，也可以什么都不做。只输出 JSON。")
        return "\n".join(lines), stimulus

    # ------------------------------------------------------------------ 双向打通（v1.1）
    def _chat_memory_text(self) -> str:
        if not getattr(self.cfg, "room_bridge_from_chat", True):
            return ""
        try:
            return self.chat_memory.text(self.rt.pet_name(), self.clock())
        except Exception:
            log.exception("字符画·房间：读取聊天的记忆失败（这次房间里不带）")
            return ""

    def _last_chat_ts(self) -> float:
        if not getattr(self.cfg, "room_bridge_from_chat", True):
            return 0.0
        try:
            return float(self.chat_memory.snapshot().get("last_ts") or 0.0)
        except Exception:
            return 0.0

    def _view(self) -> tuple:
        """(RoomMemory, RoomState)：负责房间的实例用内存里的；其它实例从磁盘读（文件变了才重读）。"""
        if self.is_owner or not self.autorun:
            return self.memory, self.state
        try:
            mt = (self.memory.path.stat().st_mtime, self.state.path.stat().st_mtime)
        except OSError:
            return self.memory, self.state
        if self._disk_view[0] != mt:
            self._disk_view = (mt, RoomMemory(self.memory.path), RoomState(self.state.path, self.clock()))
        return self._disk_view[1], self._disk_view[2]

    def chat_block(self) -> str:
        """聊天请求的系统提示里附上的“房间里的记忆”：长期记忆 + 这几天的印象。只在整理记忆后变化，不打断前缀缓存。"""
        mem, _st = self._view()
        lines = [ROOM_BLOCK_HEAD.replace("OBSERVER", ROOM_OBSERVER if self.cfg.room_observer_known else "")]
        with self.lock:
            longs = sorted(mem.long, key=lambda m: -(m.get("weight", 0.5) + 0.05 * m.get("recalls", 0)))[:8]
            gists = list(mem.gists)[-5:]
        for m in sorted(longs, key=lambda m: m["ts"]):
            lines.append(f"· {_md(m['ts'])} {m['text']}")
        for g in gists:
            lines.append(f"· {_md(g['ts'])}（印象）{g['text']}")
        if len(lines) == 1:
            lines.append("· （还没有整理出长期的记忆）")
        return "\n".join(lines)

    def chat_recent(self, force: bool = False) -> str:
        """聊天请求里本轮消息末尾附上的“房间近况”：最近在房间里做的事、留给主人的便条、还没看的礼物、房间里有什么。

        v1.3.8：只附她上次在聊天里看到之后房间里的新事，同一批内容不每轮重复附（附上去的近况不进会话记录，
        以前每轮都附，她会把同一件事一说再说）。没有新事就什么都不附。force=True（主人在问房间里的事）时全部附上。"""
        mem, st = self._view()
        now = self.clock()
        seen = float(getattr(self, "_chat_seen_ts", 0.0) or 0.0)
        out = []
        s = self.session
        stamps = []
        if self.busy and s is not None:
            started = float(s.get("start") or now)
            stamps.append(started)
            if force or started > seen:
                doing = s.get("doing") or "刚进去"
                out.append(f"你刚才正在自己的房间里（{doing}），主人来找你，你就出来了。")
        with self.lock:
            recent = [it for it in mem.short if now - it["ts"] <= 36 * 3600][-6:]
            pending = [p for p in st["pending"] if p["kind"] == "gift"]
            notes = [n for n in st.data.get("notes", []) if now - n["ts"] <= 7 * 86400][-2:]
        stamps += [it["ts"] for it in recent] + [float(p.get("ts") or 0) for p in pending] + [n["ts"] for n in notes]
        if not force:
            recent = [it for it in recent if it["ts"] > seen]
            pending = [p for p in pending if float(p.get("ts") or now) > seen]
            notes = [n for n in notes if n["ts"] > seen]
        if not (out or recent or pending or notes):
            return ""
        self._mark_chat_seen(max([seen] + stamps))
        if recent:
            out.append("最近在房间里：" + "；".join(
                f"{_when(it['ts'], now)} {it['what']}" + (f"（当时想：{_short(it.get('thought', ''), 40)}）"
                                                          if it.get("thought") else "")
                for it in recent))
        if notes:
            out.append("你留给主人的便条（原文）：" + "；".join(f"{_when(n['ts'], now)}「{_short(n['text'], 200)}」"
                                                         for n in notes))
        if pending:
            out.append("主人送进礼物文件夹、你还没看的：" + "、".join(f"「{p['path'].split('/', 1)[-1]}」" for p in pending[:4]))
        try:
            top = [it["path"] + ("/" if it["type"] == "dir" else "") for it in self.room.tree() if it["depth"] == 0]
        except OSError:
            top = []
        if top:
            out.append("房间里现在有：" + "、".join(top[:12]))
        head = "你的房间近况" if force else "你上次和主人聊天之后，房间里新发生的事"
        return f"\n〔{head}（你自己的经历，不是主人这句话的内容；自然地记得就好，不用每次都提）：" + " ".join(out) + "〕"

    def _mark_chat_seen(self, ts: float) -> None:
        """记下“聊天里已经给她看过的房间近况”到哪个时间点（负责房间的实例顺便存进 room_state.json，重启后不重复）。"""
        self._chat_seen_ts = float(ts)
        if self.is_owner or not self.autorun:
            with self.lock:
                self.state["chat_seen_ts"] = float(ts)
            self._save()

    def _canon(self) -> str:
        p = getattr(self.rt, "persona", None)
        text = ""
        if p is not None:
            text = (p.data.get("canon") or {}).get("text") or ""
        if not text:
            text = (pet_app_config().get("chat") or {}).get("default_system_prompt") or ""
        return text

    def _faces(self) -> list:
        names = [fr.display_name(k) for k in fr.PRESETS if k not in ("closed", "dead")]
        try:
            names += list(self.rt.custom.names(12))
        except Exception:
            pass
        return names

    # ------------------------------------------------------------------ 执行
    def execute(self, d: dict, now: float, stimulus: str, out: dict, tokens: int, index: int) -> dict:
        """检查并执行一个动作，记日志、短期记忆，更新精力 / 无聊。可在工作线程里调用。"""
        typ, a = d["type"], d["args"]
        if typ == "read" and is_image(str(a.get("path", ""))):     # 用 read 看图片：当作 look
            typ = d["type"] = "look"
        step = {"type": typ, "args": a, "ok": True, "error": "", "thought": d["thought"], "feeling": d["feeling"],
                "face": d["face"], "index": index}
        result = {}
        try:
            result = self._do(typ, a, step)
        except RoomError as exc:
            step["ok"], step["error"] = False, str(exc)
        except OSError as exc:
            step["ok"], step["error"] = False, f"文件操作失败：{exc.strerror or exc}"
        step["what"] = _describe(typ, a, result, step)
        response = f"{typ} {a.get('path', '')} {_content_of(typ, a)[:200]} {d['thought'][:80]}"
        with self.lock:
            past = self.memory.responses(now)
            # 最像的过去那次（同样像的几次里，取当时情况和现在最像的那次：对相似的事做出相似的反应，不算单纯重复）
            best, best_s, best_it = 0.0, 0.0, None
            scored = [(similarity(response, it.get("response", "")), it) for it in past]
            top = max((r for r, _ in scored), default=0.0)
            for r, it in scored:
                if r < top - 0.05 or r <= 0:
                    continue
                si = similarity(stimulus, it.get("stimulus", "")) if stimulus and it.get("stimulus") else 0.0
                if best_it is None or (si, r) > (best_s, best):
                    best, best_s, best_it = r, si, it
            delta = BORED_BASE + BORED_REPEAT * best * (1 - best_s) - BORED_NOVEL * (1 - best)
            if typ in ("read", "look") and step["ok"] and _gift_pending(self.state, a.get("path", "")):
                delta -= 0.05
            st = self.state
            st["boredom"] = round(max(0.0, min(1.0, st["boredom"] + delta)), 4)
            if step["ok"] or typ in ("rest", "sleep", "nothing"):
                st["energy"] = round(max(0.0, min(1.0, st["energy"] - COST.get(typ, 0.02))), 4)
            if typ in ("read", "look") and step["ok"]:      # 看过的礼物 / 变化不再标“新的”
                path = result.get("path", "")
                st["pending"] = [p for p in st["pending"] if not (p["kind"] == "gift" and p["path"] == path)]
            if index == 0:
                st["pending"] = [p for p in st["pending"] if p["kind"] == "gift"]   # 变化看过一次就够了
            billable = room_ai_billable(out.get("usage") or {}, tokens)
            st["tokens_today"] += billable                   # 房间的日预算只算计费 token（前缀缓存命中几乎不花钱）
            st["total_tokens"] += tokens
            if step["ok"]:
                self._update_read_marks(typ, a, result, now)
            if typ == "note" and step["ok"]:                 # 留给主人的便条：原文留几张，聊天时她记得自己写了什么
                st.data["notes"] = (st.data.get("notes", []) + [{"ts": round(now, 1), "text": result.get("text", "")}])[-5:]
            st["first"] = False
            echo = None
            if best >= ECHO_AT and best_it is not None:
                echo = {"id": best_it["id"], "past_ts": best_it["ts"], "past_what": best_it.get("what", ""),
                        "sim": round(best, 3), "situation_sim": round(best_s, 3), "what": step["what"]}
            else:                                          # 更早的、还记得的（几天前的短期、印象、长期）
                old = self.memory.echo_of(response, step["what"], now, ECHO_AT,
                                          exclude={h.get("id") for h in (self.session or {}).get("steps", [])})
                if old is not None:
                    si = similarity(stimulus, old.get("stimulus", "")) if stimulus and old.get("stimulus") else 0.0
                    echo = {"id": old["id"], "past_ts": old["ts"], "past_what": old["what"], "sim": old["sim"],
                            "situation_sim": round(si, 3), "what": step["what"]}
            step["echo"] = echo
            think = self.memory.think_signal(typ, out.get("reasoning_tokens", 0))
            novelty = round(1 - best, 3)
        ev = self._log({"type": "step", "step": index + 1, "thought": d["thought"], "feeling": d["feeling"],
                        "intensity": d["intensity"], "face": d["face"], "action": typ, "args": _log_args(a),
                        "ok": step["ok"], "error": step["error"], "what": step["what"], "result": result,
                        "then": d["then"], "remember": d["remember"], "tokens": tokens,
                        "reasoning_tokens": out.get("reasoning_tokens", 0), "reasoning": out.get("reasoning", "")[:4000],
                        **({"look_text": step["look_text"]} if step.get("look_text") else {}),
                        "cache_hit": int((out.get("usage") or {}).get("prompt_cache_hit_tokens") or 0),
                        "boredom_delta": round(delta, 3), "boredom": self.state["boredom"],
                        "energy": self.state["energy"], "echo": echo and {k: echo[k] for k in ("id", "sim", "situation_sim")},
                        **({"unknown_action": d["unknown_action"]} if d.get("unknown_action") else {})})
        step["id"] = ev["id"]
        # 和主人有关：给主人的便条、礼物（看礼物、回应礼物）、主人动过的东西。只是在想法里提到主人不算（她常常想到主人）
        user_related = typ == "note" or GIFT_DIR in str(a.get("path", "")) or GIFT_DIR in stimulus or \
            "变化：" in stimulus
        with self.lock:
            self.memory.add_short({"id": ev["id"], "ts": round(now, 1), "kind": typ, "what": step["what"],
                                   "thought": d["thought"], "feeling": d["feeling"], "intensity": d["intensity"],
                                   "face": d["face"], "reasoning_tokens": out.get("reasoning_tokens", 0),
                                   "think": think, "novelty": novelty, "remember": d["remember"],
                                   "user_related": bool(user_related), "response": response[:400],
                                   "stimulus": stimulus[:400], "ok": step["ok"]})
            self.memory.save()
            self.state["echo"] = echo
            if step["ok"] and typ not in ("read", "note", "rest", "sleep", "nothing"):
                self._own_paths.update(p for p in (result.get("path"), result.get("to"), a.get("path")) if p)
        self._save()
        return step

    def _do(self, typ: str, a: dict, step: dict) -> dict:
        room = self.room
        if typ in ("write", "draw"):
            content = a.get("art") if typ == "draw" else a.get("content")
            if typ == "draw" and not a.get("art"):
                content = a.get("content")
            return room.write(a.get("path", ""), content or "", overwrite=bool(a.get("overwrite")))
        if typ == "append":
            return room.append(a.get("path", ""), a.get("content", ""))
        if typ == "edit":
            return room.edit(a.get("path", ""), a.get("old", ""), a.get("new", ""))
        if typ == "read":
            r = room.read(a.get("path", ""), offset=int(a.get("offset") or 0) if str(a.get("offset") or "0").isdigit() else 0)
            step["read_text"] = r["text"]
            step["read_more"] = r["more"]
            step["read_next"] = r["offset"] + len(r["text"])
            return {"path": r["path"], "chars": r["chars"], "shown": len(r["text"]), "more": r["more"], "offset": r["offset"]}
        if typ == "look":
            return self._look(a, step)
        if typ == "music":
            return self._music(a)
        if typ == "rename":
            return room.rename(a.get("path", ""), a.get("new_name", ""))
        if typ == "move":
            return room.move(a.get("path", ""), a.get("to", ""))
        if typ == "mkdir":
            return room.mkdir(a.get("path", ""))
        if typ == "delete":
            return room.delete(a.get("path", ""))
        if typ == "note":
            text = " ".join(str(a.get("text") or a.get("content") or "").split())[:500]
            if not text:
                raise RoomError("便条是空的")
            return {"text": text}
        return {}

    def _music_ok(self) -> bool:
        mus = getattr(self.rt, "music", None)
        try:
            return bool(mus is not None and mus.available())
        except Exception:
            return False

    def _music(self, a: dict) -> dict:
        """在房间里用主人的 Spotify 放一首歌（v1.3）。"""
        from .spotify import SpotifyError

        if not self._music_ok():
            raise RoomError("现在连不上主人的 Spotify")
        try:
            r = self.rt.music.request(a.get("query") or a.get("song") or a.get("name") or "", "room")
        except SpotifyError as exc:
            raise RoomError(str(exc))
        return {k: r[k] for k in ("query", "name", "artists", "mode", "device") if k in r}

    def _look(self, a: dict, step: dict) -> dict:
        """看一张图片（v1.2）：DeepSeek 看不了图，给她看图片转成的字符画、颜色概况和图里的文字。"""
        cfg = self.cfg
        if not getattr(cfg, "room_look", True):
            raise RoomError("看图片这件事现在关掉了")
        if int(self.state.data.get("looks_today", 0)) >= int(cfg.room_look_daily):
            raise RoomError(f"今天已经看了 {int(cfg.room_look_daily)} 次图片，眼睛累了，明天再看吧")
        p = self.room.resolve(a.get("path", ""), kind="file")
        if not is_image(p):
            raise RoomError("这不是图片（文字文件用 read 看）")
        rel = self.room.rel(p)
        try:
            desc = self.looker.look(p, rel, int(cfg.room_look_cols), bool(cfg.room_ocr), self._vision_fn())
        except ValueError as exc:
            raise RoomError(str(exc))
        vt = int(desc.pop("vision_tokens_new", 0) or 0)              # 副模型看图这次花的 token
        with self.lock:
            self.state["looks_today"] = int(self.state.data.get("looks_today", 0)) + 1
            if vt:
                self.state["tokens_today"] += vt
                self.state["total_tokens"] += vt
        step["extra_tokens"] = vt
        step["look_text"] = render_image(desc)
        step["vision_brief"] = _first_sentence(desc.get("vision", ""), 70)
        step["look_path"] = rel
        step["look_hint"] = " ".join((desc.get("text") or [])[:5]) + " " + \
            " ".join(c for c, _ in desc.get("main", [])[:3])
        return {"path": rel, "size": desc["size"], "colors": [c for c, _ in desc.get("main", [])[:3]],
                "text_lines": len(desc.get("text") or []), "ocr": desc.get("ocr", ""),
                "vision": bool(desc.get("vision")), "vision_tokens": vt,
                **({"vision_error": desc["vision_error"]} if desc.get("vision_error") else {})}

    def _vision_fn(self):
        """能看图的副模型（字符画设置 vision_model，默认 deepseek-flash）；关掉了或没配好就返回 None（只给字符画）。"""
        if not getattr(self.cfg, "room_look_vision", True):
            return None
        if self.vision_fn is not None:                  # 测试替身
            return self.vision_fn
        try:
            from . import room_image
            from .ai.vision import resolve_provider

            prov = resolve_provider(self.cfg)
        except Exception:
            log.exception("字符画·房间：找不到能看图的模型")
            return None
        if not prov:
            return None
        timeout = float(getattr(self.cfg, "vision_timeout_s", 45.0)) + 15
        return lambda img: room_image.vision_describe(img, prov, timeout)

    def _update_read_marks(self, typ: str, a: dict, result: dict, now: float) -> None:
        """读过 / 看过哪些文件（什么时候、几次）、读过的礼物有没有回过便条；改名、搬动、删除时标记跟着走。调用时已持有 lock。"""
        marks = self.state.data.setdefault("read_marks", {})
        s = self.session if self.session is not None else {}
        if typ in ("read", "look"):
            path = result.get("path", "")
            if path:
                m = marks.get(path) or {}
                m.update(ts=round(now, 1), count=int(m.get("count", 0)) + 1, kind=typ)
                marks[path] = m
                if path.startswith(GIFT_DIR + "/"):
                    s.setdefault("read_gifts", []).append(path)
        elif typ == "note":
            for path in s.get("read_gifts", []):                    # 这次读过的礼物：留了便条就算回过
                if path in marks and not marks[path].get("replied"):
                    marks[path]["replied"] = {"ts": round(now, 1), "text": str(result.get("text", ""))[:40]}
        elif typ in ("rename", "move"):
            old, new = result.get("path", ""), result.get("to", "")
            for p in [p for p in marks if p == old or p.startswith(old + "/")]:
                marks[new + p[len(old):]] = marks.pop(p)
        elif typ == "delete":
            old = result.get("path", "")
            for p in [p for p in marks if p == old or p.startswith(old + "/")]:
                marks.pop(p, None)
        if len(marks) > 400:
            for p in sorted(marks, key=lambda k: marks[k].get("ts", 0))[:len(marks) - 400]:
                del marks[p]

    def _backfill_read_marks(self) -> None:
        saved, self.session = self.session, None
        try:
            for e in self.room.tail_log(5000):
                if e.get("type") == "enter":
                    self.session = {}
                elif e.get("type") == "leave":
                    self.session = None
                elif e.get("type") == "step" and e.get("ok"):
                    if self.session is None:
                        self.session = {}
                    self._update_read_marks(e.get("action", ""), e.get("args") or {}, e.get("result") or {},
                                            float(e.get("ts") or 0))
        except Exception:
            log.exception("字符画·房间：从日志补“读过”标记失败")
        finally:
            self.session = saved
            self.state.data["read_marks_backfilled"] = True

    def _read_marks_text(self, now: float) -> dict:
        """房间清单里每个读过的文件后面的标记：“← 你今天 15:49 读过，今天 15:49 回过便条”。"""
        out = {}
        with self.lock:
            marks = dict(self.state.data.get("read_marks") or {})
        for path, m in marks.items():
            p = self.room.space / path
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            verb = "看过" if m.get("kind") == "look" else "读过"
            n = int(m.get("count", 1))
            text = f"← 你{_when(m['ts'], now)} {verb}" + (f"（一共 {n} 次）" if n > 1 else "")
            if mtime > m["ts"] + 2:
                text += "，之后被改动过"
            r = m.get("replied")
            if r:
                text += f"，{_when(r['ts'], now)} 回过便条"
            out[path] = text
        return out

    def _record_saw(self, prev: dict, saw: str, hers: bool = True) -> None:
        """她看完图片写下的一句“我觉得这是……”：记进那一步的短期记忆、图片缓存和原始日志。
        hers=False：她没写，记的是副模型描述的第一句（只进短期记忆和日志，不当成她的看法存进图片缓存）。"""
        path = prev.get("look_path") or str(prev["args"].get("path", ""))
        prev["saw"] = saw
        if hers:
            try:
                self.looker.note_saw(self.room.resolve(path, kind="file"), path, saw)
            except (RoomError, OSError):
                pass
        with self.lock:
            for it in reversed(self.memory.short):
                if it["id"] == prev.get("id"):
                    it["what"] = f"{it['what']}：{saw}"[:160]
                    it["stimulus"] = f"{it.get('stimulus', '')} {saw}"[:400]
                    break
            self.memory.save()
        self._log({"type": "saw", "of": prev.get("id"), "path": path, "text": saw, "hers": hers})

    # ------------------------------------------------------------------ 界面线程
    def _on_step(self, step: dict) -> None:
        s = self.session
        if s is None:
            return
        s["steps"].append(step)
        s["doing"] = step["what"][:30]
        with self.lock:
            self.state["status"] = step["what"][:60]
        if s.get("user"):                     # 主人来了：脸交给对话，不再演房间里的表情
            return
        path = str(step["args"].get("path") or "")
        arg = path.rsplit("/", 1)[-1] if path and path.isascii() else ""
        self.rt.engine.set_think(f"<ROOM> {THINK.get(step['type'], 'idle')}({arg});")
        self._show_face(step.get("face") or "")
        self.rt._notify()

    def _show_face(self, face: str) -> None:
        if not face or not self.rt.engine.rigged or self.sleep.busy:
            return
        try:
            from .inline import tag_to_animation

            key = self.rt.custom.resolve(face)
            if key in fr.PRESETS or key in fr.CUSTOM:
                self.rt.apply_json(tag_to_animation({"preset": key}))
        except Exception:
            log.exception("字符画·房间：换表情失败")

    def _on_done(self, rep: dict) -> None:
        s = self.session or {}
        now = self.clock()
        self.busy = False
        release_lease(self.lease)
        rep = {**rep, "time": time.strftime("%H:%M:%S"), "woke": s.get("woke", False)}
        last = (s.get("steps") or [{}])[-1].get("type", "")
        with self.lock:
            st = self.state
            st["inside"] = False
            st["status"] = ""
            # 睡着时醒来进的：按给她看的那个（放长的）范围；主人来了、她也没说什么时候再来：她不会接着睡，按平常的范围
            woke = bool(s.get("woke")) and not (s.get("user") and rep.get("next_visit") is None)
            base, by = self._next_delay(now, rep.get("next_visit"), last, st["energy"], woke)
            if rep.get("error"):
                base, by = max(base, 30 * 60), "auto"
            st["next_wake"] = now + base
            st["next_by"] = by
            st["next_choice"] = ("morning" if rep.get("next_visit") == "morning" else round(base / 60)) \
                if by == "her" else None
        self._log({"type": "leave", "why": rep.get("error") and f"出错了：{rep['error']}" or rep.get("why", ""),
                   "steps": rep.get("steps", 0), "tokens": rep.get("tokens", 0), "next_wake": round(now + base, 1),
                   "next_visit": rep.get("next_visit"), "next_by": by})
        tokens = int(rep.get("tokens") or 0)
        if tokens and hasattr(self.rt, "store"):
            self.rt.store.budget.add(tokens)
            try:
                self.rt.store.save(force=True)
            except Exception:
                pass
        if s.get("saved_think") and self.rt.engine.think_text.startswith("<ROOM>"):
            self.rt.engine.set_think(s["saved_think"])
        self._save()
        self.last_report = rep
        self.session = None
        log.info("字符画·房间：出来了（%s，%d 步，%d token）%s", rep.get("why", ""), rep.get("steps", 0), tokens,
                 f" 错误：{rep['error']}" if rep.get("error") else "")
        # 自己醒来进的房间、主人没来：出来以后接着睡（入睡计时按主人最后一次互动算，不因为她自己醒过而重新计）
        if (s.get("woke") or last == "sleep") and not s.get("user"):
            self.sleep.last_activity = min(self.sleep.last_activity, s.get("pre_activity", self.sleep.last_activity))

            def back_to_sleep():
                if not self.busy and self.sleep.state == "awake" and \
                        self.sleep.idle_seconds() >= self.cfg.room_after_chat_min * 60:
                    self.sleep.fall_asleep()

            QTimer.singleShot(4000, back_to_sleep)
        self.rt._notify()

    def _next_delay(self, now: float, choice, last: str, energy: float, woke: bool = False) -> tuple:
        """下次什么时候再来（秒）。她自己说了就照她说的（限制在 visit_range 里：平常 room_interval_min~max 分钟，
        睡着时醒来进的 × room_asleep_interval_factor；深夜可以说“睡到天亮”）；
        没说（例如主人来了她就出来了）才按精力在这个范围里随便定一个，夜里再乘 room_night_factor。返回 (秒, "her"/"auto")。"""
        lo, hi = self.visit_range(woke)
        if choice == "morning":
            if self.night(now):
                return _until_hour(now, int(self.cfg.room_night_end)) + self.rng.uniform(0, 30) * 60, "her"
            return hi * 60, "her"
        if isinstance(choice, (int, float)) and choice > 0:
            return max(lo, min(hi, float(choice))) * 60, "her"
        delay = self.rng.uniform(lo, hi) * 60 * (1.5 - 0.9 * energy)        # 精力好：来得勤一点
        if last == "sleep":
            delay *= 2.0
        elif last == "rest":
            delay *= 1.3
        delay = max(lo * 60, min(hi * 60, delay))
        if self.night(now):
            delay *= self.cfg.room_night_factor
        return delay, "auto"

    # ------------------------------------------------------------------ 整理记忆（睡着时）
    def maybe_consolidate(self, now: float | None = None, force: bool = False, client=None, sync: bool = False) -> bool:
        now = now or self.clock()
        cfg = self.cfg
        if self.consolidating or self.busy:
            return False
        with self.lock:
            due = self.memory.due(now, 0 if force else cfg.room_short_hours)
            last = float(self.memory.data.get("last_consolidate") or 0)
        if not due:
            return False
        if not force:
            overdue = any(now - it["ts"] > 48 * 3600 for it in due)
            if now - last < 6 * 3600 and not overdue:
                return False
            if not (self.asleep() or (overdue and self.sleep.idle_seconds() > 600)):
                return False
            if any(getattr(self.rt, a, False) for a in ("_experimenting", "_evolving", "_reflecting")):
                return False
            ok, _ = self.rt.store.budget.check(cfg)
            if not ok:
                return False
        due = due[:40]
        self.consolidating = True
        name = self.rt.pet_name()
        with self.lock:
            longs = list(self.memory.long)

        def work():
            from .ai import room_ai

            res = {"items": due, "judgments": {}, "tokens": 0, "error": ""}
            try:
                c = client
                if c is None:
                    if self.client_factory is not None:
                        c = self.client_factory()
                    else:
                        from .ai.client import make_client

                        c, _ = make_client(cfg)
                        if cfg.room_memory_model and hasattr(c, "model"):
                            c.model = c.name = cfg.room_memory_model
                if type(c).__name__ == "MockClient":
                    raise RuntimeError("没有可用的 API Key")
                out = room_ai.complete_json(c, room_ai.judge_messages(name, due, longs), 3000, 90.0, False, "low",
                                            need=("items",))
                res["judgments"] = room_ai.parse_judgments(out["obj"])
                res["tokens"] = room_ai.total_tokens(out["usage"])
            except Exception as exc:
                log.exception("字符画·房间：整理记忆出错")
                res["error"] = str(exc)[:200]
                res["tokens"] = room_ai.total_tokens(getattr(exc, "usage", {}) or {})
            (self._on_memory_done if sync else self.memory_signal.emit)(res)

        if sync:
            work()
        else:
            threading.Thread(target=work, name="pet-ascii-room-memory", daemon=True).start()
        return True

    def _on_memory_done(self, res: dict) -> None:
        self.consolidating = False
        now = self.clock()
        if res.get("error"):
            rep = {"error": res["error"]}
            with self.lock:                                    # 失败了：过几个小时再试（不会因此忘掉任何东西）
                self.memory.data["last_consolidate"] = round(now - 3 * 3600, 1)
                self.memory.save()
        else:
            with self.lock:
                rep = self.memory.apply_consolidation(res["items"], res["judgments"], now,
                                                      float(self.cfg.room_gist_days), int(self.cfg.room_long_max))
                self.memory.save()
        tokens = int(res.get("tokens") or 0)
        if tokens and hasattr(self.rt, "store"):
            self.rt.store.budget.add(tokens)
            with self.lock:
                self.state["tokens_today"] += tokens
                self.state["total_tokens"] += tokens
        rep["tokens"] = tokens
        rep["time"] = time.strftime("%H:%M:%S")
        self.last_memory = rep
        self._log({"type": "memory", "error": rep.get("error", ""), "tokens": tokens,
                   "long": [x.get("text") for x in rep.get("long", [])], "gist": len(rep.get("gist", [])),
                   "forgot": len(rep.get("forgot", [])), "merged": len(rep.get("merged", [])),
                   "promoted": len(rep.get("promoted", [])), "faded": len(rep.get("faded", []))})
        self._save()
        log.info("字符画·房间：整理记忆 %s", rep.get("error") or
                 f"长期 +{len(rep.get('long', []))}、印象 +{len(rep.get('gist', []))}、忘掉 {len(rep.get('forgot', []))}、"
                 f"加强 {len(rep.get('merged', []))}（{tokens} token）")


ROOM_BLOCK_MARK = "【房间里的记忆】"
ROOM_BLOCK_HEAD = (ROOM_BLOCK_MARK + "你有一个属于自己的小房间（一个只有你能改动的文件夹），没人理你的时候你会自己进去"
                   "写写画画、整理东西、发呆；OBSERVER主人偶尔会往礼物文件夹里放东西。"
                   "以下是你在房间里的长期记忆和这几天的印象，和聊天的记忆一样都是你自己的经历，自然地记得就好：")
ROOM_OBSERVER = "主人知道这个房间，偶尔会透过“房间”窗口看看；"


def room_ai_billable(usage: dict, total: int) -> int:
    hit = usage.get("prompt_cache_hit_tokens") if isinstance(usage, dict) else 0
    return max(0, int(total) - int(hit or 0))


_ROOM_QUESTION = re.compile(r"房间|日记|便条|礼物|小屋|在干嘛|在干什么|干嘛呢|做了什么|在做什么|忙什么|忙啥")


def asks_about_room(text: str) -> bool:
    """主人这句是不是在问她房间里的事（这时房间近况全部附上，不只附新的）。"""
    return bool(_ROOM_QUESTION.search(str(text or "")))


def _until_hour(now: float, hour: int) -> float:
    """从 now 到下一个 hour 点整还有多少秒。"""
    from datetime import timedelta

    d = datetime.fromtimestamp(now)
    target = d.replace(hour=max(0, min(23, hour)), minute=0, second=0, microsecond=0)
    if target <= d:
        target += timedelta(days=1)
    return (target - d).total_seconds()


def _first_sentence(text: str, n: int) -> str:
    import re as _re

    s = _re.split(r"(?<=[。！？!?])", " ".join(str(text or "").split()), maxsplit=1)[0]
    return s if len(s) <= n else s[:n - 1] + "…"


def _md(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d.month}月{d.day}日"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


# ---------------------------------------------------------------------- 描述
def _last_chat_guess(rt, now: float) -> float:
    """第一次建房间时，“上次和主人说话”取字符画对话素材里最后一轮的时间（不读桌宠记忆）；没有就算现在。"""
    try:
        last = list(rt.dialogue.rounds)[-1].get("time", "")
        t = datetime.strptime(f"{datetime.fromtimestamp(now).year}-{last}", "%Y-%m-%d %H:%M").timestamp()
        if t > now + 60:                                   # 跨年：是去年的
            t = datetime.strptime(f"{datetime.fromtimestamp(now).year - 1}-{last}", "%Y-%m-%d %H:%M").timestamp()
        return min(t, now)
    except Exception:
        return now


def pet_main_model() -> str:
    """桌宠聊天设置里的主模型（她平时说话用的那个）。"""
    chat = pet_app_config().get("chat") or {}
    prov = (chat.get("providers") or {}).get(chat.get("active_provider") or "") or {}
    return str(prov.get("model") or "")


def _content_of(typ: str, a: dict) -> str:
    if typ == "draw":
        return str(a.get("art") or a.get("content") or "")
    if typ == "edit":
        return f"{a.get('old', '')} → {a.get('new', '')}"
    if typ == "note":
        return str(a.get("text") or a.get("content") or "")
    return str(a.get("content") or "")


def _describe(typ: str, a: dict, result: dict, step: dict) -> str:
    path = result.get("path") or str(a.get("path") or "")
    verb = VERB.get(typ, typ)
    if typ in ("write", "draw", "append"):
        body = " ".join(_content_of(typ, a).split())
        head = f"{verb}「{path}」" + (f"（{len(_content_of(typ, a))} 字）" if body else "")
        return head + (f"：{body[:40]}…" if len(body) > 40 else (f"：{body}" if body else ""))
    if typ == "edit":
        return f"{verb}「{path}」里的一处"
    if typ == "read":
        if not step.get("ok"):
            return f"{verb}「{path}」"
        head = " ".join(str(step.get("read_text", "")).split())
        frm = f"，从第 {result.get('offset', 0) + 1} 字接着看" if result.get("offset") else ""
        return f"{verb}「{path}」（{result.get('chars', 0)} 字{frm}）" + (f"：『{head[:40]}{'…' if len(head) > 40 else ''}』"
                                                                       if head else "")
    if typ == "look":
        return f"{verb}「{path}」"
    if typ == "music":
        if not step.get("ok"):
            return f"想用 Spotify 放「{a.get('query') or a.get('song') or ''}」"
        how = "加进了主人的播放队列" if result.get("mode") == "queued" else "直接播放"
        return f"用 Spotify 放了《{result.get('name', '')}》- {result.get('artists', '')}（{how}）"
    if typ == "rename":
        return f"把「{path}」改名为「{result.get('to') or a.get('new_name', '')}」"
    if typ == "move":
        return f"把「{path}」搬到「{result.get('to') or a.get('to', '')}」"
    if typ in ("mkdir", "delete"):
        return f"{verb}「{path}」"
    if typ == "note":                         # 摘要只留前 60 字，截断时加“…”（全文在 result.text 和便条里）
        body = " ".join(str(result.get("text") or _content_of(typ, a)).split())
        return f"{verb}：{body[:60]}…" if len(body) > 60 else f"{verb}：{body}"
    return verb


def _log_args(a: dict) -> dict:
    out = {}
    for k, v in a.items():
        v = v if isinstance(v, (int, float, bool)) else str(v)
        out[k] = v[:6000] if isinstance(v, str) else v
    return out


def _gift_pending(state: RoomState, path: str) -> bool:
    return any(p["kind"] == "gift" and p["path"] == path for p in state["pending"])


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _part_of_day(h: int) -> str:
    return ("深夜" if h < 5 else "清晨" if h < 8 else "上午" if h < 11 else "中午" if h < 13 else "下午" if h < 17
            else "傍晚" if h < 19 else "晚上" if h < 23 else "深夜")


def _energy_word(e: float) -> str:
    return "很累" if e < 0.25 else "有点累" if e < 0.5 else "还行" if e < 0.75 else "精神不错"


def _bored_word(b: float) -> str:
    return "不无聊" if b < 0.2 else "有一点无聊" if b < 0.45 else "挺无聊的" if b < 0.7 else "无聊透了"


def _ago(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 120:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"{sec / 3600:.0f} 小时前"
    return f"{sec / 86400:.0f} 天前"


def _when(ts: float, now: float) -> str:
    a, b = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    if a.date() == b.date():
        return f"今天 {a:%H:%M}"
    if (b.date() - a.date()).days == 1:
        return f"昨天 {a:%H:%M}"
    return f"{a:%m-%d %H:%M}"
