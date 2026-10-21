"""运行时：把引擎、渲染视图、AI 调度、缓存、持久化和定时器组装在一起（桌宠窗口与实验室共用）。"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal

from .ai.client import make_client
from .ai.director import Director, GenResult
from .ai.mock import MockClient
from .cache import AnimCache
from .character import find_character
from .config import AsciiConfig, data_dir, pet_app_config
from . import face_rig as fr
from .creative import CreativeMemory
from .custom_presets import CustomPresets
from .dialogue_log import DialogueLog
from .engine import Engine, EngineSettings
from .metrics import Latency
from .parts import PartsLibrary
from .persist import StateStore
from .persona import Persona, acquire_lease, release_lease
from .sleep import AWAKE_THINK, SLEEP_EXPR, SleepController
from .protocol import Animation, parse_animation

log = logging.getLogger("pet.ascii")

STAGE_RE = re.compile(r"[（(]([^（）()]{1,80})[）)]")


def intent_from_reply(text: str) -> tuple[str, str]:
    """从对话回复中提炼表达意图：优先用括号里的神态描写，其次用第一句话。返回 (意图, 上下文)。"""
    text = (text or "").strip()
    stages = STAGE_RE.findall(text)
    speech = STAGE_RE.sub("", text).strip()
    first = re.split(r"[。！？!?\n]", speech, maxsplit=1)[0][:50] if speech else ""
    if stages:
        intent = "；".join(s.strip() for s in stages[:2])
        if first:
            intent += f"；同时说：“{first}”"
    else:
        intent = f"一边说“{first}”一边自然地做出相应的表情" if first else "安静地看着用户"
    return intent[:120], text[:400]


class Runtime(QObject):
    result_ready = Signal(object)
    changed = Signal()
    inline_tag = Signal(str)        # 对话流中剥离出的 <face ...> 标签（可从任意线程发射）
    talk_signal = Signal(float)     # 正在说话（流式输出中），参数为持续秒数
    wake_signal = Signal(str)       # 吵醒（对话请求可能在工作线程里组装，动画要回到界面线程再播）
    experiment_done = Signal(object)  # 一次创作实验完成（报告字典）
    evolve_done = Signal(object)    # 一次表情进化完成（报告字典）
    persona_done = Signal(object)   # 一次副人格夜间反思完成（后台结果，回到界面线程落地）

    def __init__(self, cfg: AsciiConfig | None = None, client=None, client_desc: str = "", start_timer: bool = True):
        super().__init__()
        self.cfg = cfg or AsciiConfig.load()
        self.latency = Latency()                       # 动画响应延迟、后台任务耗时（日志 + 测试台）
        # 创作（v0.7）：正式组件库与骨架方案、创作记忆（实验记录 / 候选 / 心得 / 演出），都在字符画数据目录
        self.parts = PartsLibrary(data_dir() / "components.json")
        self.creative = CreativeMemory(data_dir() / "experiments.json")
        self.custom = CustomPresets(data_dir() / "custom_presets.json", self.cfg.custom_presets_max)
        self.custom.extra_context = self._creative_context
        self.custom.protected = self.creative.elites(self.custom)
        self.char = find_character(self.cfg.character)
        self.engine = Engine(self.char, EngineSettings(background=self.cfg.background, code_rows=self.cfg.code_rows))
        self.store = StateStore(data_dir() / "state.json")
        self.engine.import_state(self.store.visual())
        if self.engine.expression in (SLEEP_EXPR, "被吵醒"):   # 上次退出时正在睡：醒着启动
            self.engine.reset_face()
            self.engine.expression = "neutral"
        if self.engine.think_text.startswith(("<SLEEP>", "<DREAM>", "<ROOM>")):
            self.engine.think_text = AWAKE_THINK
        if self.engine.rigged:            # 变化幅度：在字符画上衡量，不够就自动放大
            self.custom.rig = self.engine.pose.rig
            self.custom.min_change = max(0.0, min(0.5, self.cfg.evolve_min_change))
            self.custom.max_gain = max(1.0, min(5.0, self.cfg.evolve_max_gain))
        self.cache = AnimCache(data_dir() / "anim_cache")
        self.views: list = []
        self.listeners: list = []
        self.director = Director(self.cfg, self.store, self.cache, self._state_for_ai, self.result_ready.emit,
                                 client, client_desc)
        self.result_ready.connect(self._on_result, Qt.QueuedConnection)
        self.director.preset_hook = self._on_generator_presets
        # 最近对话（字符画数据目录 dialogue.json，跨重启累计，供表情进化参考；与桌宠记忆无关）
        self.dialogue = DialogueLog(data_dir() / "dialogue.json", self.cfg.dialogue_keep)
        self._pending_user = ""
        self._pending_probe = False
        self._reply_tag = ""
        self._created_this_reply = ""
        self.last_created = None
        self.last_evolve = None
        self._evolving = False
        self.evolve_done.connect(self._on_evolve_done, Qt.QueuedConnection)
        self.evolve_timer = QTimer(self)
        self.evolve_timer.timeout.connect(self.maybe_evolve)
        self.inline_tag.connect(self.apply_inline_tag, Qt.QueuedConnection)
        self.talk_signal.connect(lambda sec: self.sleep.state == "awake" and self.engine.talk(sec), Qt.QueuedConnection)
        self.last_inline = None          # 最近一次内联表情（供测试台显示）
        self.inline_count = 0
        # 说完之后：模型用收尾标签 <face idle=表情 after=秒> 决定是否、何时换表情进入挂机
        self._idle_plan = None           # 本轮回复里的决定（回复结束时才排期）
        self._idle_pending = None        # 已排期、尚未执行的切换
        self._reply_face_seen = False
        self.idle_state = None           # 挂机中：{"preset", "since", "keep"}
        self.last_idle = None            # 最近一次决定（供测试台、自检显示）
        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.timeout.connect(self._enter_idle)
        self._last = time.perf_counter()
        self._fps_t = time.perf_counter()
        self.fps = 0.0
        self._frames = 0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.auto_timer = QTimer(self)
        self.auto_timer.timeout.connect(self._auto)
        # 睡眠：无互动超过 sleep_after_min 分钟后入睡，对话发消息会吵醒
        self.sleep = SleepController(self)
        self.sleep_timer = QTimer(self)
        self.sleep_timer.timeout.connect(self.sleep.check)
        self.wake_signal.connect(self.sleep.wake, Qt.QueuedConnection)
        # 创作实验（后台）与视觉评价（后台、可选）
        from .ai.vision import VisionCritic

        self.vision = VisionCritic(self.cfg, metrics=self.latency)
        self._experimenting = False
        self._exp_cancel = None
        self.last_experiment = None
        self.experiment_done.connect(self._on_experiment_done, Qt.QueuedConnection)
        self.evolve_timer.timeout.connect(self.maybe_experiment)
        # 副人格（v0.9）：睡着时回顾最近的对话（只读桌宠记忆库的原始对话），条目与候选池存在字符画数据目录 persona.json
        self.persona = Persona(data_dir() / "persona.json")
        self._reflecting = False
        self.last_reflect = None
        self.persona_done.connect(self._on_persona_done, Qt.QueuedConnection)
        self.evolve_timer.timeout.connect(self.maybe_reflect)
        # 虚拟房间（v1.0）：她自己的文件夹（字符画数据目录 room/）。生活循环每分钟检查一次，只在桌宠里自动运行（autorun）
        self.room = None
        try:
            from .room_life import RoomLife

            self.room = RoomLife(self)
            self.evolve_timer.timeout.connect(self.room.check)
        except Exception:
            log.exception("字符画·房间：初始化失败（不影响其它功能）")
        # Spotify（v1.3）：她放歌的权限；每 30 秒在后台看一眼主人在听什么
        self.music = None
        try:
            from .music import Music

            self.music = Music(self.cfg, data_dir() / "music.json", log_event=self._music_log)
        except Exception:
            log.exception("字符画·Spotify：初始化失败（不影响其它功能）")
        self.music_timer = QTimer(self)
        self.music_timer.timeout.connect(lambda: self.music is not None and self.music.poll_async())
        self._tag_t0: dict = {}
        if start_timer:
            self.start()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._last = time.perf_counter()
        self.timer.start(max(15, int(1000 / max(5, self.cfg.fps))))
        self.auto_timer.start(15000)
        self.evolve_timer.start(60000)
        self.music_timer.start(30000)
        self.sleep_timer.start(15000)

    def stop(self) -> None:
        self.cancel_experiment()
        self.vision.stop()
        self.timer.stop()
        self.auto_timer.stop()
        self.evolve_timer.stop()
        self.idle_timer.stop()
        self.sleep_timer.stop()
        self.music_timer.stop()
        if self.sleep.state != "awake":       # 不把闭着眼的脸存下来
            self.sleep.end_sleep()
            self.engine.reset_face()
            self.engine.expression = "neutral"
        self.store.save(self._export_state(), force=True)
        self.custom.flush()                # 表情库是后台合并写盘的：退出前把没写的写完
        self.persona.flush()
        if self.room is not None:
            self.room.shutdown()

    def add_view(self, view) -> None:
        if view not in self.views:
            self.views.append(view)

    def remove_view(self, view) -> None:
        if view in self.views:
            self.views.remove(view)

    def use_mock(self, on: bool) -> str:
        if on:
            self.director.set_client(MockClient(), "mock（手动开启）")
        else:
            c, d = make_client(self.cfg)
            self.director.set_client(c, d)
        return self.director.client_desc

    # ------------------------------------------------------------------ 帧循环
    def _tick(self) -> None:
        try:
            self._tick_once()
        except Exception as exc:
            # 桌宠是无控制台的窗口程序：帧循环里的异常会被悄悄吞掉、画面停住却没有任何记录。记进日志（同一种错误一分钟记一次）
            key = repr(exc)[:160]
            now = time.monotonic()
            errs = self.__dict__.setdefault("_tick_errors", {})
            if now - errs.get(key, -1e9) > 60:
                errs[key] = now
                log.exception("字符画：帧循环出错（画面可能停住）")

    def _tick_once(self) -> None:
        now = time.perf_counter()
        dt = now - self._last
        self._last = now
        self.engine.tick(dt)
        self.sleep.tick()
        if self.views:
            chars, colors, w, h, off = self.engine.frame()
            for v in self.views:
                v.set_frame(chars, colors, w, h, off)
        self._frames += 1
        if now - self._fps_t >= 1.0:
            self.fps = self._frames / (now - self._fps_t)
            self._frames, self._fps_t = 0, now
            self.store.save(self._export_state())

    def _export_state(self) -> dict:
        st = self.engine.export_state()
        if self.sleep.busy:       # 睡着时存“醒着的样子”：直接关机 / 结束进程后，下次启动不会闭着眼、满屏梦话
            from . import face_rig as fr

            st = dict(st, expression="neutral", think=self.sleep.saved_think)
            if "face_spec" in st:
                st["face_spec"] = fr.neutral()
        return st

    def _auto(self) -> None:
        if self.sleep.busy:                   # 睡着 / 正在被吵醒：不做自主表达
            return
        self.director.autonomous_tick(idle=self.engine.current is None)

    # ------------------------------------------------------------------ AI
    def _state_for_ai(self):
        st = self.engine.visual_state()
        if self.engine.rigged:
            st["custom_presets"] = self.custom.names(15)
        return st, self.engine.context()

    def pet_name(self) -> str:
        pc = pet_app_config()
        return str((pc.get("character_aliases") or {}).get(pc.get("character") or "") or self.char.display_name)

    def _on_generator_presets(self, items: list) -> None:
        """独立生成模式（chat_link=ai / 测试台 / 自主表达）输出里的 new_presets：先入库再解析动画。"""
        if not self.cfg.custom_presets:
            return
        for it in items[:3]:
            if isinstance(it, dict) and isinstance(it.get("spec"), dict):
                spec = dict(it["spec"])
                if it.get("from"):
                    spec.setdefault("preset", it["from"])
                final, status = self.custom.add(str(it.get("name") or ""), spec, origin="generator",
                                                note=str(it.get("emotion") or ""))
                log.info("字符画：生成器自创表情「%s」→「%s」（%s）", it.get("name"), final, status)

    def _on_result(self, res: GenResult) -> None:
        if res.anim is not None and not self.sleep.busy:     # 睡着时到达的生成结果不演，免得把睡脸盖掉
            self.engine.apply(res.anim)
            try:
                kind = "缓存回放 → 开始播放" if res.cached else ("本地兜底 → 开始播放" if res.fallback else "API 表情 → 开始播放")
                self.latency.record(kind, (time.time() - res.request.created) * 1000,
                                    res.request.intent[:16] + (f"，API {res.elapsed:.1f}s" if res.elapsed else ""))
            except Exception:
                pass
        for cb in list(self.listeners):
            try:
                cb(res)
            except Exception:
                pass
        self.changed.emit()

    def express(self, intent: str, source: str = "user", priority: int = 5, context: str = "",
                force_new: bool = False, modes: tuple | None = None, intensity: float = 0.6):
        if source == "user":
            self.sleep.activity()
            self._room_user(interrupt=True)
        if self.sleep.busy:                   # 睡着时：用户的操作会吵醒她，其它（自主表达等）忽略
            if source == "user":
                if self.sleep.sleeping:
                    self.sleep.wake("poke")
                if priority >= 8:             # 明确输入的表达意图：醒来后再演；双击戳醒本身就是反应
                    self.sleep.after_wake(lambda: self.director.request(intent, source, priority, context, intensity,
                                                                        force_new, modes))
            return None
        return self.director.request(intent, source, priority, context, intensity, force_new, modes)

    def poke(self, intent: str) -> None:
        """在窗口上双击：醒着就戳她一下（一次表达），睡着就把她吵醒。"""
        self.express(intent, source="user", priority=7)

    def note_activity(self) -> None:
        """拖动窗口、打开菜单等：算作有互动（推迟入睡），但不会吵醒已经睡着的她。"""
        self.sleep.activity()
        self._room_user(interrupt=False)

    def _room_user(self, chat: bool = False, interrupt: bool = True) -> None:
        """主人有互动：推迟她下次进房间；chat / 直接冲着她来的操作会让她做完手上这一步就从房间出来。"""
        if self.room is not None:
            try:
                self.room.on_user(chat=chat, interrupt=interrupt)
            except Exception:
                log.exception("字符画·房间：处理主人互动出错")

    def apply_json(self, data) -> Animation:
        anim = parse_animation(data, self.engine.context())
        self.engine.apply(anim)
        return anim

    def replay(self, cache_id: str) -> Animation | None:
        raw = self.cache.load(cache_id)
        if raw is None:
            return None
        self.cache.mark_used(cache_id)
        return self.apply_json(raw)

    def apply_inline_tag(self, tag_text: str) -> None:
        """主模型回复开头的表情标签 → 本地骨架直接演出（不调用 API）。"""
        from .inline import parse_tag, tag_to_animation

        tag = parse_tag(tag_text)
        if not tag:
            return
        if tag.get("music") and not self._pending_probe:     # 放歌（开头、收尾、单独的标签都可以）
            self._music_this_reply = True
            self.play_music(tag["music"], "chat")
        if tag.get("music_only"):                             # 只放歌：表情不变
            return
        self.sleep.activity()
        if self.sleep.intercept_tag(tag_text):    # 睡着 / 正在被吵醒：吵醒动画播完再演这个表情
            return
        if tag.get("idle_only"):
            # 收尾标签：只记下“说完之后”的决定，回复结束时再排期，不改变当前表情
            self._idle_plan = {k: v for k, v in tag.items() if k != "preset"}
            self._idle_plan["tag"] = tag_text
            self._notify()
            return
        if not self._reply_face_seen:
            self.cancel_idle("新的回复开始")
            self._reply_face_seen = True
        if "idle" in tag:                 # 开头标签里也可以直接写 idle= after=
            self._idle_plan = {"idle": tag.pop("idle"), "after": tag.pop("after"), "tag": tag_text}
        if "new" in tag:
            new = tag.pop("new")
            if self.cfg.custom_presets and self.engine.rigged:
                final, status = self.custom.add(new["name"], new["spec"], origin="chat")
                tag["preset"] = final
                self.last_created = {"name": new["name"], "final": final, "status": status, "time": time.strftime("%H:%M:%S")}
                self._created_this_reply = final if status in ("new", "updated") else ""
                log.info("字符画：对话中自创表情「%s」→「%s」（%s）", new["name"], final, status)
            else:
                tag["inline_spec"] = new["spec"]
                tag["preset"] = new["spec"].get("preset", "neutral")
        if not self._pending_probe:              # 内置与自创表情都计使用次数（供表情进化参考；自检探针不计）
            self.custom.touch(tag["preset"])
        self._reply_tag = tag_text
        self.last_inline = {"tag": tag_text, "parsed": tag, "time": time.strftime("%H:%M:%S")}
        self.inline_count += 1
        self.apply_json(tag_to_animation(tag))
        used = fr.CUSTOM.get(tag["preset"], {})
        if used and (used.get("parts") or used.get("extra")):
            from .parts import used_parts

            self.parts.touch(used_parts(used))
        if tag.get("scene"):                    # 标签里点名的特别演出（她自己创作过的）
            self.play_scene(tag["scene"])
        t0 = self._tag_t0.pop(tag_text, None)
        if t0 is not None:
            self.latency.record("内联标签 → 开始播放", (time.perf_counter() - t0) * 1000, tag["preset"])
        for cb in list(self.listeners):
            try:
                cb(None)
            except Exception:
                pass

    def play_music(self, query: str, source: str) -> bool:
        """她在聊天里想给主人放歌（表情标签里的 music=）：后台线程里搜歌、播放或排队，不卡界面。"""
        if self.music is None or not self.music.available():
            return False

        def run():
            from .spotify import SpotifyError

            try:
                self.music.request(query, source)
            except SpotifyError:
                pass                                   # 原因已记进日志和下一轮的备忘（她会知道没放成）
            except Exception:
                log.exception("字符画·Spotify：放歌出错")

        threading.Thread(target=run, name="pet-ascii-music", daemon=True).start()
        return True

    def _music_log(self, ev: dict) -> None:
        if self.room is not None:
            self.room._log(ev)

    def note_tag(self, tag_text: str) -> None:
        """对话流里剥出一个表情标签（可在工作线程里调用）：记下时间，算“标签 → 开始播放”的延迟。"""
        if len(self._tag_t0) > 20:
            self._tag_t0.clear()
        self._tag_t0[tag_text] = time.perf_counter()

    def record_user(self, text: str, probe: bool = False) -> None:
        """用户发来一条消息。probe=True 表示自检探针：照常演出，但不计入表情进化的对话素材。"""
        self._pending_user = (text or "").strip()
        self._pending_probe = probe
        from .music import asks_for_music

        self._music_asked = (not probe) and asks_for_music(self._pending_user)   # 主人这句是在请她放歌吗
        from .music import asks_for_liked

        self._music_liked_only = self._music_asked and asks_for_liked(self._pending_user)
        self._music_this_reply = False
        self.cancel_idle("用户发来新消息")
        self.sleep.activity()
        self._room_user(chat=not probe)
        if self.sleep.sleeping:                   # 睡着时发消息：吵醒她（随机一种反应）
            self.sleep.woken_by_chat = True       # 同步记下，组装这条请求时就能告诉模型
            if QThread.currentThread() == self.thread():
                self.sleep.wake("chat")
            else:
                self.wake_signal.emit("chat")

    # ------------------------------------------------------------------ 评价（👍/👎）
    def current_expression(self) -> str:
        """现在脸上的表情：正在播放的动画和直接切换（菜单、测试台、本地兜底）里，以较晚发生的为准。"""
        eng = self.engine
        cur = eng.current
        if cur is not None and cur.expression and eng.current_t0 >= eng.expression_t:
            return cur.expression
        return eng.expression or "neutral"

    def rate_current(self, vote: int) -> str | None:
        """给当前表情点 👍(+1) / 👎(-1)。记入表情库，供表情进化参考；返回被评价的表情名。"""
        self.sleep.activity()
        self._room_user()
        name = self.custom.resolve(self.current_expression())
        spec = fr.CUSTOM.get(name) or {}
        if spec.get("parts") or spec.get("extra"):          # 评价也记到这个表情用到的自创部件上
            from .parts import used_parts

            self.parts.rate(used_parts(spec), vote)
        last = self.dialogue[-1].get("pet", "") if len(self.dialogue) else ""
        if not self.custom.rate(name, vote, context=last):
            return None
        log.info("字符画：用户给表情「%s」%s", name, "👍" if vote > 0 else "👎")
        fx = ({"effect": "sparkle", "chars": "*+✦", "color": "#ffe27a", "duration_ms": 900} if vote > 0 else
              {"effect": "symbols", "chars": "?", "color": "#9fe8ff", "duration_ms": 900})
        self.apply_json({"mode": 1, "intent": "收到评价", "expression": name, "priority": 3,
                         "ops": [{"type": "effect", **fx}], "end_state": {"face": "hold"}})
        self._notify()
        return name

    def clear_dialogue(self) -> None:
        self.dialogue.clear()
        self._notify()

    # ------------------------------------------------------------------ 说完之后 / 挂机
    def _notify(self) -> None:
        for cb in list(self.listeners):
            try:
                cb(None)
            except Exception:
                pass

    def _schedule_idle(self, plan: dict) -> None:
        """回复说完后排期挂机：至少等 idle_min_delay_s（默认 1 分钟），再加上模型自己决定的 after 秒。
        idle=keep 也一样要等到那时才算进入挂机，只是不换表情。"""
        self.idle_timer.stop()
        now = time.strftime("%H:%M:%S")
        delay = max(0.0, float(getattr(self.cfg, "idle_min_delay_s", 60.0))) + float(plan["after"])
        self.last_idle = {"decision": plan["idle"], "after": plan["after"], "delay": delay,
                          "tag": plan.get("tag", ""), "time": now, "fired": None}
        self._idle_pending = plan
        self._idle_due = time.monotonic() + delay
        self.idle_timer.start(int(delay * 1000))
        if plan["idle"] == "keep":
            log.info("字符画：说完 %.0f 秒后保持当前表情进入挂机（1 分钟起步 + 她决定的 %.0f 秒）", delay, plan["after"])
        else:
            log.info("字符画：说完 %.0f 秒后切换到「%s」进入挂机（1 分钟起步 + 她决定的 %.0f 秒）", delay, plan["idle"],
                     plan["after"])
        self._notify()

    def idle_eta(self) -> float | None:
        """离挂机切换还有几秒；没有排期时返回 None。"""
        if self._idle_pending is None:
            return None
        return max(0.0, getattr(self, "_idle_due", 0.0) - time.monotonic())

    def _enter_idle(self) -> None:
        plan, self._idle_pending = self._idle_pending, None
        if not plan or self.sleep.busy:
            return
        if plan["idle"] == "keep":                  # 保持当前表情，只是进入挂机
            self.idle_state = {"preset": self.engine.expression, "since": time.time(), "keep": True}
            if self.last_idle is not None:
                self.last_idle["fired"] = time.strftime("%H:%M:%S")
            log.info("字符画：保持当前表情「%s」，进入挂机", self.engine.expression)
            self._notify()
            return
        from .inline import idle_animation

        if not plan.get("probe"):
            self.custom.touch(plan["idle"])
        self.apply_json(idle_animation(plan))
        self.idle_state = {"preset": plan["idle"], "since": time.time(), "keep": False}
        if self.last_idle is not None:
            self.last_idle["fired"] = time.strftime("%H:%M:%S")
        log.info("字符画：已切换到「%s」，进入挂机", plan["idle"])
        self._notify()

    def cancel_idle(self, reason: str = "") -> None:
        """对话继续了：取消还没执行的挂机切换，并退出挂机状态。"""
        if self._idle_pending is not None:
            log.info("字符画：取消挂机切换「%s」（%s）", self._idle_pending["idle"], reason)
            if self.last_idle is not None:
                self.last_idle["cancelled"] = reason
        self.idle_timer.stop()
        self._idle_pending = None
        self.idle_state = None

    # ------------------------------------------------------------------ 表情进化
    def _evolve_state(self) -> dict:
        st = self.store.data.setdefault("evolve", {"last": 0.0, "day": "", "count": 0, "replies_at_last": 0})
        if st.get("day") != date.today().isoformat():
            st.update(day=date.today().isoformat(), count=0)
        return st

    def maybe_evolve(self) -> None:
        cfg = self.cfg
        if not (cfg.evolve and cfg.custom_presets and self.engine.rigged) or self._evolving:
            return
        st = self._evolve_state()
        if self.dialogue.since_evolve < cfg.evolve_min_replies or st["count"] >= cfg.evolve_daily_limit:
            return
        if time.time() - st.get("last", 0) < cfg.evolve_interval_min * 60:
            return
        self.evolve_now()

    def evolve_now(self, client=None) -> bool:
        """立即在后台线程执行一次表情进化。没有对话素材时返回 False。"""
        if self._evolving or not len(self.dialogue):
            return False
        ok, why = self.store.budget.check(self.cfg)
        if not ok:
            self.last_evolve = {"error": why, "time": time.strftime("%H:%M:%S")}
            return False
        self._evolving = True
        dialogue = self.dialogue.snapshot()
        name = self.pet_name()
        rig = self.engine.pose.rig if self.engine.rigged else None      # 照镜子：在字符画上比对可见差异
        check = self.cfg.evolve_mirror_check

        def work():
            from .ai import evolve

            try:
                c = client
                if c is None:
                    c, _ = make_client(self.cfg)
                    if hasattr(c, "disable_thinking"):
                        c.disable_thinking = not self.cfg.evolve_thinking
                rep_ = evolve.run(c, name, dialogue, self.custom, thinking=self.cfg.evolve_thinking, rig=rig, check=check)
            except Exception as exc:
                rep_ = {"error": str(exc)[:200]}
            self.evolve_done.emit(rep_)

        threading.Thread(target=work, name="pet-ascii-evolve", daemon=True).start()
        return True

    def _on_evolve_done(self, rep: dict) -> None:
        self._evolving = False
        rep["time"] = time.strftime("%H:%M:%S")
        self.last_evolve = rep
        st = self._evolve_state()
        st["last"] = time.time()
        st["count"] = st.get("count", 0) + 1
        if not rep.get("error"):
            self.dialogue.mark_evolved()
        u = rep.get("usage") or {}
        tokens = int(u.get("total_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)) or 0)
        if tokens:
            self.store.budget.add(tokens)
        self.store.save(force=True)
        if rep.get("error"):
            log.warning("字符画：表情进化失败（%.1fs，%s token）%s", rep.get("elapsed", 0), tokens, rep["error"])
        else:
            log.info("字符画：表情进化完成（%.1fs，%s token%s）%s → %s；照镜子 %s", rep.get("elapsed", 0), tokens,
                     f"，首次输出无法解析（{rep['repaired']}），补问后成功" if rep.get("repaired") else "",
                     rep.get("thoughts", ""), [(r.get("op", "create"), r["final"], r["status"]) for r in rep.get("results", [])],
                     [(c["name"], "ok" if c["ok"] else c.get("status", "修正")) for c in rep.get("checks", [])])
        self._notify()

    # ------------------------------------------------------------------ 副人格（v0.9，后台）
    def maybe_reflect(self) -> None:
        """每分钟检查一次：睡着一会儿之后、今天还没回顾过，就在后台做一次夜间反思。冻结或关闭时什么都不做。"""
        cfg = self.cfg
        p = self.persona
        try:
            p.flush()                                     # 聊天时记下的正典（可能来自对话线程）在这里落盘
            if cfg.persona_mode == "off" or p.frozen or self._reflecting:
                return
            p.maintenance(cfg)                            # 每天一次：衰减、试用淡出、候选过期（不调用 API）
            p.flush()
            if self.sleep.state != "asleep" or self._experimenting or self._evolving:
                return
            if self.engine.t - self.sleep.asleep_since < cfg.persona_after_sleep_min * 60:
                return
            if p.due(cfg)[0]:
                self.reflect_now()
        except Exception:
            log.exception("字符画·副人格：定时检查出错")

    def reflect_now(self, client=None, force: bool = False) -> bool:
        """在后台线程做一次夜间反思（force：测试台“立即反思”，不看睡没睡、今天回顾过没有、新消息够不够）。"""
        cfg = self.cfg
        if self._reflecting:
            return False
        why = ""
        if cfg.persona_mode == "off":
            why = "副人格已关闭"
        elif self.persona.frozen:
            why = "副人格已冻结"
        else:
            ok, why = self.store.budget.check(cfg)
            why = "" if ok else why
            if not why and self.persona.tokens_left(cfg.persona_daily_tokens) <= 0:
                why = f"今天副人格的 token 预算（{cfg.persona_daily_tokens}）用完了"
        lease = data_dir() / "persona.lease"
        if not why and not acquire_lease(lease):
            why = "另一个桌宠实例正在反思"
        if why:
            self.last_reflect = {"error": why, "time": time.strftime("%H:%M:%S")}
            self._notify()
            return False
        if not self.persona.data["canon"].get("hash"):     # 重启后还没聊过天：先用桌宠设置里的人设，聊天时再以实际请求为准
            text = (pet_app_config().get("chat") or {}).get("default_system_prompt") or ""
            if text:
                self.persona.note_canon(text)
        self._reflecting = True
        snap = self.persona.snapshot()
        name = self.pet_name()
        if self.sleep.state == "asleep":
            self.engine.set_think("<DREAM> recall(today); self.reflect();")
        log.info("字符画·副人格：开始夜间反思（%s）", cfg.persona_mode)

        def work():
            from .ai import persona_ai

            try:
                c, rc = client, client
                if c is None:
                    c, _ = make_client(cfg)
                    if hasattr(c, "disable_thinking"):
                        c.disable_thinking = not cfg.persona_thinking
                    rc, _ = make_client(cfg)
                    if hasattr(rc, "disable_thinking"):
                        rc.disable_thinking = not cfg.persona_review_thinking
                    if cfg.persona_review_model and hasattr(rc, "model"):
                        rc.model = rc.name = cfg.persona_review_model
                res = persona_ai.run_night(
                    c, snap, name=name, mode=cfg.persona_mode, review_client=rc, thinking=cfg.persona_thinking,
                    review_thinking=cfg.persona_review_thinking, effort=cfg.persona_reasoning_effort,
                    min_user=0 if force else cfg.persona_min_user_messages, force=force,
                    max_entries=cfg.persona_max_entries, max_chars=cfg.persona_max_chars)
            except Exception as exc:
                log.exception("字符画·副人格：夜间反思出错")
                res = {"error": str(exc)[:200]}
            self.persona_done.emit(res)

        threading.Thread(target=work, name="pet-ascii-persona", daemon=True).start()
        self._notify()
        return True

    def _on_persona_done(self, res: dict) -> None:
        self._reflecting = False
        release_lease(data_dir() / "persona.lease")
        try:
            rep = self.persona.apply_night(res, self.cfg)
        except Exception as exc:
            log.exception("字符画·副人格：落地夜间反思结果出错")
            rep = {"error": f"落地结果出错：{exc}"[:200]}
        u = res.get("usage") or {}
        tokens = int(u.get("total_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)) or 0)
        if tokens:
            self.store.budget.add(tokens)
            self.persona.add_tokens(tokens)
            self.persona.flush()
            self.store.save(force=True)
        rep["tokens"] = tokens
        rep["elapsed"] = res.get("elapsed", 0)
        rep["time_str"] = time.strftime("%H:%M:%S")
        self.last_reflect = rep
        if self.sleep.state == "asleep" and self.engine.think_text.startswith("<DREAM> recall"):
            self.engine.set_think("<SLEEP> zzz... zzz...")
        if rep.get("error"):
            log.warning("字符画·副人格：夜间反思失败（%.1fs，%s token）%s", rep["elapsed"], tokens, rep["error"])
        else:
            log.info("字符画·副人格：夜间反思完成（%s，%.1fs，%s token，读了 %s 条消息）%s%s → 提议 %s；决定 %s",
                     rep.get("phase"), rep["elapsed"], tokens, rep.get("messages", 0),
                     rep.get("skipped") or "", rep.get("thoughts", ""),
                     [(x.get("op"), x.get("result")) for x in rep.get("proposals", [])], rep.get("decisions"))
        self._notify()

    # ------------------------------------------------------------------ 创作实验（v0.7，后台）
    def _creative_context(self) -> dict:
        """附加给表情进化的上下文：可以用的自创部件、她自己的创作心得。"""
        out = {"components": self.parts.summary(12)}
        notes = [n["text"] for n in self.creative.data.get("notes", [])[-6:]]
        if notes:
            out["creative_notes"] = notes
        return out

    def experiment_status(self) -> str:
        if self._experimenting:
            return "创作实验进行中…"
        ok, why = self.creative.budget_left(self.cfg.experiment_daily_limit, self.cfg.experiment_daily_tokens)
        return "空闲" if ok else why

    def maybe_experiment(self) -> None:
        cfg = self.cfg
        if not (cfg.experiments and self.engine.rigged) or self._experimenting or self._evolving or self.director.busy:
            return
        if not (self.sleep.sleeping or self.sleep.idle_seconds() >= cfg.experiment_idle_min * 60):
            return                                   # 只在没人理她的时候（或睡着做梦时）做实验
        if time.time() - self.creative.last_run_time() < cfg.experiment_interval_min * 60:
            return
        if not self.creative.budget_left(cfg.experiment_daily_limit, cfg.experiment_daily_tokens)[0]:
            return
        if not self.store.budget.check(cfg)[0]:
            return
        self.experiment_now()

    def experiment_now(self, style: str | None = None, kind: str | None = None, client=None) -> bool:
        """立即在后台线程开始一次创作实验。正在进行时返回 False。"""
        if self._experimenting or not self.engine.rigged:
            return False
        from .ai.experiment import ExperimentContext

        cfg = self.cfg
        target = self.creative.choose_target(store=self.custom, parts_lib=self.parts, style=style, kind=kind)
        self._experimenting = True
        cancel = threading.Event()
        self._exp_cancel = cancel
        ctx = ExperimentContext(self.pet_name(), self.engine.pose.rig, self.custom, self.parts, self.creative,
                                vision=self.vision if cfg.vision_enabled else None, engine_factory=self._offline_engine,
                                canvas=(self.engine.W, self.engine.H), metrics=self.latency,
                                vision_wait_s=cfg.experiment_vision_wait_s)
        log.info("字符画·创作实验：开始（风格 %s，%s；%s）", target["style"], target["kind"], target["reason"])
        if self.sleep.state == "asleep":
            self.engine.set_think("<DREAM> experiment.start(style=" + target["style"] + ");")

        def work():
            from .ai import experiment

            try:
                c = client
                if c is None:
                    c, _ = make_client(cfg)
                    if hasattr(c, "disable_thinking"):
                        c.disable_thinking = not cfg.experiment_thinking
                rep_ = experiment.run(c, ctx, target, max_iters=cfg.experiment_max_iters, thinking=cfg.experiment_thinking,
                                      cancel=cancel)
            except Exception as exc:
                log.exception("字符画·创作实验：出错")
                rep_ = {"outcome": "error", "failure": str(exc)[:200], "target": target, "usage": {}}
            self.experiment_done.emit(rep_)

        threading.Thread(target=work, name="pet-ascii-experiment", daemon=True).start()
        self._notify()
        return True

    def cancel_experiment(self) -> None:
        if self._exp_cancel is not None:
            self._exp_cancel.set()
        self.vision.cancel_all()

    def _offline_engine(self) -> Engine:
        """离线引擎（后台线程渲染自由演出用；每次新建，和界面上的引擎完全独立）。"""
        return Engine(find_character(self.cfg.character), EngineSettings(background="none", code_rows=False))

    def _on_experiment_done(self, rep: dict) -> None:
        self._experimenting = False
        self._exp_cancel = None
        rep["time"] = time.strftime("%H:%M:%S")
        self.last_experiment = rep
        u = rep.get("usage") or {}
        tokens = int(u.get("total_tokens") or 0)
        if tokens:
            self.store.budget.add(tokens)
            self.store.save(force=True)
        self.custom.protected = self.creative.elites(self.custom)
        t = rep.get("target") or {}
        log.info("字符画·创作实验：%s（风格 %s / %s，%s 轮，%.1fs，%d token）%s%s", rep.get("outcome"), t.get("style"),
                 t.get("kind"), len(rep.get("rounds") or []), rep.get("elapsed", 0) or 0, tokens,
                 rep.get("name") or "", f"：{rep['failure']}" if rep.get("failure") else "")
        ad = rep.get("adopted") or {}
        if ad.get("expression") and ad.get("expression_status") in ("new", "updated") and self.sleep.state == "awake" \
                and not self.director.busy and self._idle_pending is None:
            self.engine.set_expression({"preset": ad["expression"]}, 500)      # 醒着的话，顺便给你看看新作品
        self._notify()

    def play_scene(self, name: str) -> bool:
        """播放她创作过的自由演出（脱离骨架的全画布字符画）。"""
        from . import creative

        anim = creative.SCENES.get(name)
        if not anim or self.sleep.busy:
            return False
        try:
            self.apply_json(anim)
            self.creative.touch_scene(name)
            return True
        except Exception:
            log.exception("字符画：播放演出「%s」失败", name)
            return False

    def apply_preset(self, preset: str, blend_ms: int = 220) -> None:
        """手动切换表情（右键菜单、测试台）：也算一次互动，退出挂机。"""
        self.cancel_idle("手动切换表情")
        self.sleep.end_sleep()                    # 睡着时手动换表情：直接醒（不播吵醒动画）
        self.sleep.activity()
        self._room_user()
        self.engine.set_expression({"preset": preset}, blend_ms)

    def on_chat_reply(self, text: str, tag_seen: bool = False) -> None:
        """桌宠对话系统每完成一次回复时调用。

        chat_link:
          inline  主模型在回复开头输出表情标签（已在流中处理）；若这次没有标签，用本地关键词兜底
          ai      另起一次表情生成请求（字符画专用模型，可自由创作，较慢、额外计费）
          local   只用本地关键词猜表情
          off     不联动
        """
        self.sleep.activity()
        if self.sleep.busy:                       # 还在被吵醒：等吵醒动画播完再处理这次回复
            if self.sleep.sleeping:
                self.sleep.wake("reply")
            self.sleep.after_wake(lambda: self.on_chat_reply(text, tag_seen))
            return
        if getattr(self, "_music_asked", False) and not getattr(self, "_music_this_reply", False) \
                and self.music is not None and self.music.available():
            self.music.note_missed(self._pending_user)        # 主人请她放歌、她没写 music=：下一轮告诉她
        self._music_asked = False
        mode = self.cfg.chat_link
        plan, self._idle_plan = self._idle_plan, None
        if plan:
            plan["probe"] = self._pending_probe
        face_seen, self._reply_face_seen = self._reply_face_seen, False
        if text:
            face = self._reply_tag
            if plan and plan.get("tag") and plan["tag"] != face:
                face = (face + " … " + plan["tag"]).strip(" …")
            if not self._pending_probe:
                self.dialogue.append({"user": self._pending_user[:160], "pet": text[:200], "face": face[:200]})
            if self._created_this_reply:
                self.custom.set_context(self._created_this_reply, text)
            self._pending_user, self._reply_tag, self._created_this_reply = "", "", ""
            self._pending_probe = False
        if mode == "off" or not text:
            return
        if plan and mode == "inline":
            self._schedule_idle(plan)
        if mode == "inline" and tag_seen and (face_seen or not plan):
            return
        if mode in ("inline", "local"):
            from .inline import guess_preset

            self.engine.set_expression({"preset": guess_preset(text)}, 260)
            return
        intent, ctx = intent_from_reply(text)
        self.express(intent, source="chat", priority=6, context=ctx, modes=(1, 2))
