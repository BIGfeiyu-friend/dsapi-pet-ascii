"""请求调度：把表达意图变成动画，且不阻塞界面。

- 后台线程执行 API 请求；界面线程只收结果（由调用方负责切回 GUI 线程）
- 同义请求去重；新请求（优先级 ≥ 旧请求）会取消排队中及正在进行中的过时请求（其结果被丢弃）
- 两次 API 调用之间保持最小间隔；每日请求数 / token 预算
- 命中相似缓存时按概率复用旧动画，但始终保留生成新动画的能力（force_new 可强制生成）
- 输出不合规：带着校验错误让模型修正重试；仍失败或网络错误：本地程序化生成兜底
- 可选自主表达：空闲时偶尔自己决定做个小动作（默认关闭，受冷却与每日次数限制）
"""
from __future__ import annotations

import random
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..protocol import Animation, Context, ProtocolError, extract_json, parse_animation
from . import mock, prompt
from .client import ClientError, make_client

MODE3_HINTS = ("乱码", "解体", "变成", "崩", "爆炸", "碎", "故障", "glitch", "融化", "消失", "全屏", "变身", "分解", "化成")
MODE1_HINTS = ("眨眼", "看向", "看左", "看右", "往左", "往右", "跳", "点头", "摇头", "转身", "移动", "靠近", "后退", "晃", "抬头", "低头")

AUTO_INTENTS = [
    "没人理我的时候，自己发会儿呆，做个细微的小表情",
    "偷偷观察一下用户在忙什么",
    "无聊地哼起歌来",
    "伸个懒腰，打个小哈欠",
    "突然想到一件开心的事",
    "有点困了，眼皮打架",
    "对着屏幕做个俏皮的鬼脸",
    "根据现在的时间（{time}）自然地做一个小动作",
    "即兴在身边用字符画一个小小的图案",
    "眼睛四处张望，好奇地看看周围",
]


@dataclass
class GenRequest:
    intent: str
    source: str = "user"
    priority: int = 5
    allowed: tuple = (1, 2)
    suggested: int = 2
    context: str = ""
    intensity: float = 0.6
    force_new: bool = False
    state: dict = field(default_factory=dict)       # 发起请求时（GUI 线程）拍下的视觉状态快照
    ctx: Context | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: float = field(default_factory=time.time)
    cancelled: bool = False


@dataclass
class GenResult:
    request: GenRequest
    anim: Animation | None = None
    raw_text: str = ""
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0
    model: str = ""
    cached: bool = False
    fallback: bool = False
    error: str = ""
    attempts: int = 0
    cache_id: str = ""

    def summary(self) -> dict:
        u = self.usage or {}
        return {
            "time": time.strftime("%H:%M:%S", time.localtime(self.request.created)),
            "intent": self.request.intent, "source": self.request.source,
            "mode": self.anim.mode if self.anim else None,
            "ops": [op.type for op in self.anim.ops] if self.anim else [],
            "model": self.model, "elapsed": round(self.elapsed, 2),
            "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
            "cache_hit_tokens": u.get("prompt_cache_hit_tokens"),
            "cached": self.cached, "fallback": self.fallback, "error": self.error, "attempts": self.attempts,
            "warnings": self.anim.warnings if self.anim else [], "cache_id": self.cache_id,
        }


def choose_modes(intent: str, source: str, allow_mode3: bool) -> tuple[tuple, int]:
    low = intent.lower()
    if allow_mode3 and any(k in low for k in MODE3_HINTS):
        return (1, 2, 3), 3
    if any(k in intent for k in MODE1_HINTS) and len(intent) <= 12:
        return (1, 2), 1
    if source in ("user", "lab") and allow_mode3:
        return (1, 2, 3), 2
    return (1, 2), 2


def restrict_modes(anim: Animation, allowed: tuple) -> Animation:
    """剔除本次请求不允许的操作（例如普通对话中不允许全画布演出）。"""
    before = len(anim.ops)
    if 3 not in allowed:
        anim.ops = [op for op in anim.ops if op.type != "full_scene"
                    and not (op.type == "effect" and op.effect.get("name") == "dissolve")]
    if 2 not in allowed and 3 not in allowed:
        anim.ops = [op for op in anim.ops if op.type in ("parameter_animation", "effect", "face")]
    if not anim.ops:
        raise ProtocolError(f"只允许 mode {list(allowed)}，但输出中没有可用的操作")
    if len(anim.ops) != before or anim.mode not in allowed:
        anim.warnings.append(f"按允许的模式 {list(allowed)} 过滤了 {before - len(anim.ops)} 个操作")
        anim.mode = min(anim.mode, max(allowed))
    return anim


def _key(intent: str) -> str:
    return re.sub(r"[\W_]+", "", intent.lower())[:48]


class Director:
    def __init__(self, cfg, store, cache, get_state: Callable[[], tuple[dict, Context]],
                 deliver: Callable[[GenResult], None], client=None, client_desc: str = ""):
        self.cfg = cfg
        self.store = store
        self.cache = cache
        self.get_state = get_state
        self.deliver = deliver
        if client is None:
            client, client_desc = make_client(cfg)
        self.client = client
        self.client_desc = client_desc
        self.rng = random.Random()
        self._cv = threading.Condition()
        self._queue: list[GenRequest] = []
        self.inflight: GenRequest | None = None
        self._recent: dict[str, float] = {}
        self._last_api = 0.0
        self.history: deque = deque(maxlen=60)
        self.preset_hook = None      # 输出里带 new_presets 时调用（先入库，后面的表情帧才能引用新名字）
        self.last: GenResult | None = None
        self.status = "就绪"
        self.next_auto = time.time() + self.rng.uniform(cfg.autonomous_min_s, cfg.autonomous_max_s)
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="pet-ascii-director", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ 对外
    def set_client(self, client, desc: str) -> None:
        self.client, self.client_desc = client, desc

    def request(self, intent: str, source: str = "user", priority: int = 5, context: str = "",
                intensity: float = 0.6, force_new: bool = False, modes: tuple | None = None) -> GenRequest | None:
        intent = (intent or "").strip()
        if not intent:
            return None
        k = _key(intent) + "|" + source
        now = time.time()
        if not force_new and now - self._recent.get(k, 0) < self.cfg.dedupe_window_s:
            self.status = f"已忽略重复请求：{intent[:20]}"
            return None
        self._recent = {kk: v for kk, v in self._recent.items() if now - v < 300}
        self._recent[k] = now
        allowed, suggested = choose_modes(intent, source, self.cfg.allow_mode3)
        if modes:
            allowed = tuple(m for m in modes if m in (1, 2, 3)) or allowed
            suggested = suggested if suggested in allowed else allowed[-1]
        state, ctx = self.get_state()
        req = GenRequest(intent, source, priority, allowed, suggested, context, intensity, force_new, state, ctx)
        with self._cv:
            self._queue = [q for q in self._queue if q.priority > priority]
            if self.inflight is not None and priority >= self.inflight.priority:
                self.inflight.cancelled = True
            self._queue.append(req)
            self._queue.sort(key=lambda r: (-r.priority, r.created))
            self._cv.notify()
        return req

    def cancel_all(self) -> None:
        with self._cv:
            self._queue.clear()
            if self.inflight:
                self.inflight.cancelled = True

    def stop(self, timeout: float = 2.0) -> bool:
        """结束后台线程（测试里用；桌宠里调度器跟进程同生共死，不调用）。返回线程是否已经退出。"""
        with self._cv:
            self._stop = True
            self._queue.clear()
            if self.inflight:
                self.inflight.cancelled = True
            self._cv.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def busy(self) -> bool:
        return self.inflight is not None or bool(self._queue)

    def autonomous_tick(self, idle: bool) -> GenRequest | None:
        if not self.cfg.autonomous or not idle or self.busy:
            return None
        now = time.time()
        if now < self.next_auto:
            return None
        self.next_auto = now + self.rng.uniform(self.cfg.autonomous_min_s, self.cfg.autonomous_max_s)
        ok, _ = self.store.budget.check(self.cfg, auto=True)
        if not ok:
            return None
        intent = self.rng.choice(AUTO_INTENTS).format(time=time.strftime("%H:%M"))
        return self.request(intent, source="auto", priority=1, intensity=0.35, modes=(1, 2))

    # ------------------------------------------------------------------ 后台线程
    def _loop(self) -> None:
        while not self._stop:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                req = self._queue.pop(0)
                self.inflight = req
            try:
                res = self._process(req)
                if res is not None:
                    self.last = res
                    self.history.append(res)
                    if not req.cancelled:
                        self.deliver(res)
                    else:
                        self.status = f"已丢弃过时结果：{req.intent[:20]}"
            except Exception as exc:  # 调度器永不崩溃
                self.status = f"内部错误：{exc}"
            finally:
                with self._cv:
                    self.inflight = None

    def _parse(self, text: str, ctx: Context) -> Animation:
        return parse_animation(text, ctx)

    def _fallback(self, req: GenRequest, state: dict, ctx: Context, error: str, res: GenResult) -> GenResult:
        hit = self.cache.find(req.intent, self._face_key(state), threshold=0.3)
        if hit:
            raw = self.cache.load(hit[0]["id"])
            if raw:
                try:
                    res.anim = restrict_modes(parse_animation(raw, ctx), req.allowed)
                    res.cached, res.cache_id = True, hit[0]["id"]
                    self.cache.mark_used(hit[0]["id"])
                except ProtocolError:
                    res.anim = None
        if res.anim is None:
            obj = mock.generate(req.intent, state, req.allowed)
            res.anim = parse_animation(obj, ctx)
            res.model = (res.model or "") + "+本地兜底"
        res.fallback = True
        res.error = error
        self.status = f"已回退：{error[:40]}"
        return res

    @staticmethod
    def _face_key(state: dict) -> str:
        fr = state.get("face_region") or {}
        return f"{state.get('pose')}:{fr.get('w')}x{fr.get('h')}"

    def _process(self, req: GenRequest) -> GenResult | None:
        if req.cancelled:
            return None
        state, ctx = req.state, req.ctx or Context()
        res = GenResult(req, model=getattr(self.client, "name", "?"))
        face_key = self._face_key(state)
        budget_ok, budget_msg = self.store.budget.check(self.cfg, auto=req.source == "auto")
        if not req.force_new:
            hit = self.cache.find(req.intent, face_key)
            if hit and (self.rng.random() < self.cfg.reuse_probability or not budget_ok):
                raw = self.cache.load(hit[0]["id"])
                if raw:
                    try:
                        res.anim = restrict_modes(parse_animation(raw, ctx), req.allowed)
                        res.cached, res.cache_id = True, hit[0]["id"]
                        res.raw_text = "(复用缓存动画 %s，相似度 %.2f)" % (hit[0]["id"], hit[1])
                        self.cache.mark_used(hit[0]["id"])
                        self.status = f"复用缓存：{req.intent[:20]}"
                        return res
                    except ProtocolError:
                        pass
        if not budget_ok:
            return self._fallback(req, state, ctx, budget_msg, res)
        wait = self._last_api + self.cfg.min_interval_s - time.time()
        while wait > 0 and not req.cancelled:
            time.sleep(min(0.1, wait))
            wait = self._last_api + self.cfg.min_interval_s - time.time()
        if req.cancelled:
            return None
        messages = prompt.build_messages(req.intent, state, req.allowed, req.suggested, req.context,
                                         req.intensity, req.source)
        last_err = ""
        total_tokens = 0
        for attempt in range(1 + max(0, self.cfg.retries)):
            res.attempts = attempt + 1
            self.status = f"生成中（第 {attempt + 1} 次）：{req.intent[:20]}"
            self._last_api = time.time()
            try:
                out = self.client.complete(messages, self.cfg.max_tokens, self.cfg.temperature, self.cfg.timeout)
            except ClientError as exc:
                last_err = str(exc)
                if exc.retryable and attempt < self.cfg.retries and not req.cancelled:
                    time.sleep(1.5)
                    continue
                break
            res.raw_text = out["text"]
            res.elapsed += out["elapsed"]
            res.model = out.get("model", res.model)
            u = out.get("usage") or {}
            res.usage = u
            total_tokens += int(u.get("total_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)) or 0)
            if self.preset_hook is not None:
                try:
                    obj = extract_json(out["text"])
                    if isinstance(obj.get("new_presets"), list) and obj["new_presets"]:
                        self.preset_hook(obj["new_presets"])
                except Exception:
                    pass
            try:
                res.anim = restrict_modes(parse_animation(out["text"], ctx), req.allowed)
                break
            except ProtocolError as exc:
                last_err = f"输出校验失败：{exc}"
                if attempt < self.cfg.retries and not req.cancelled:
                    messages = prompt.repair_messages(messages, out["text"], str(exc))
                    continue
                break
        if getattr(self.client, "name", "") != "mock":
            self.store.budget.add(total_tokens, auto=req.source == "auto")
            self.store.save()
        if res.anim is None:
            return self._fallback(req, state, ctx, last_err or "未知错误", res)
        entry = self.cache.save(req.intent, res.anim.raw, {
            "source": req.source, "model": res.model, "tokens": total_tokens, "face": face_key})
        res.cache_id = entry["id"]
        self.status = f"完成：{req.intent[:20]}（{res.elapsed:.1f}s）"
        return res
