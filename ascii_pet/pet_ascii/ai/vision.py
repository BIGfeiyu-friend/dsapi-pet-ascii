"""后台视觉评价（v0.7，可选）：把**实际渲染结果的截图**交给支持看图的模型，请它评价，结果只作参考。

异步双通道里的“慢通道”：
- 平时的表情生成只走本地结构检查，立刻播放；眨眼、视线、参数微调、复用已验证的动画都不会走到这里。
- 只有全新的组件、骨架改造、复杂形态（自由演出）才会在后台提交评价；同一时间最多 1 个请求在跑。
- 有每日请求数 / token 预算、两次请求的最小间隔、超时、排队上限，任务可以取消；全部在后台线程里，
  不碰界面线程、不影响正在进行的对话和播放中的动画。
- 视觉模型只给反馈（看起来像什么、是否表达了意图、问题、建议），改不改、留不留由 DeepSeek 自己决定。
- 用哪个模型：优先 ASCII 配置里的 vision_model；否则沿用桌宠设置里的视觉模型（“与对话相同”时用对话模型）。
  如果服务商不支持图片输入，第一次失败后当天不再尝试，回退到只用本地检查。
"""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time

from ..protocol import ProtocolError, extract_json

log = logging.getLogger("pet.ascii")

SYSTEM = """你是一位挑剔但友善的视觉设计评审。图片是一个桌宠角色的脸，用彩色字符画（ASCII）呈现：
第一张是改动区域的特写，第二张是整张脸的缩略图，第三张（如果有）是字符化之前的像素草图。
设计者是另一个 AI，它会参考你的意见，但最终由它自己决定。请只根据图片里**实际看到的**内容评价，不要猜测设计稿：
- reads_as：这张脸实际看起来是什么表情 / 感觉（一句话）
- intent_match：和设计意图有多吻合（0~1）
- clarity：在字符画上是否清楚可辨（0~1）
- appeal：是否有表现力、好看或有趣（0~1；古怪、抽象的风格只要有表现力也可以高分）
- problems：看得见的问题（最多 4 条，具体到部位）
- suggestions：最多 3 条具体的改进建议
只输出一个 JSON 对象：{"reads_as":"…","intent_match":0.0,"clarity":0.0,"appeal":0.0,"problems":["…"],"suggestions":["…"]}"""


SYSTEM_SCENE = SYSTEM.replace(
    "图片是一个桌宠角色的脸，用彩色字符画（ASCII）呈现：\n第一张是改动区域的特写，第二张是整张脸的缩略图，第三张（如果有）是字符化之前的像素草图。",
    "图片是桌宠的一段“自由演出”里的两帧截图：整个画布都是彩色字符画（ASCII），角色本身被隐藏了，\n"
    "画面不一定是脸——可能是粒子、抽象图形、字符形态。第一张在演出前段，第二张在后段。").replace(
    "这张脸实际看起来是什么表情 / 感觉", "这段演出的画面实际看起来是什么、给人什么感觉")
assert SYSTEM_SCENE != SYSTEM


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def build_messages(task: dict) -> list:
    text = {"intent": task.get("intent", ""), "style": task.get("style", ""), "what_changed": task.get("changes", ""),
            "local_check": task.get("local", {})}
    content = [{"type": "text", "text": "设计说明（设计者自己写的，仅供理解意图）：" + json.dumps(text, ensure_ascii=False)}]
    for key in ("ascii_focus", "ascii_face", "pixel_focus"):
        png = (task.get("images") or {}).get(key)
        if png:
            content.append({"type": "image_url", "image_url": {"url": _data_url(png)}})
    system = SYSTEM_SCENE if task.get("kind") == "scene" else SYSTEM
    return [{"role": "system", "content": system}, {"role": "user", "content": content}]


def parse(text: str) -> dict:
    obj = extract_json(text)

    def f(k):
        try:
            return max(0.0, min(1.0, float(obj.get(k))))
        except (TypeError, ValueError):
            return None

    out = {"reads_as": str(obj.get("reads_as") or "")[:80], "intent_match": f("intent_match"), "clarity": f("clarity"),
           "appeal": f("appeal"),
           "problems": [str(p)[:60] for p in (obj.get("problems") or [])[:4] if p],
           "suggestions": [str(p)[:60] for p in (obj.get("suggestions") or [])[:3] if p]}
    if not out["reads_as"] and out["intent_match"] is None:
        raise ProtocolError("视觉评价没有有效内容")
    scores = [v for v in (out["intent_match"], out["clarity"], out["appeal"]) if v is not None]
    out["score"] = round(sum(scores) / len(scores), 3) if scores else None
    out["summary"] = (f"看起来{out['reads_as']}；吻合{_pct(out['intent_match'])} 清晰{_pct(out['clarity'])} "
                      f"表现力{_pct(out['appeal'])}" + (f"；问题：{'；'.join(out['problems'][:2])}" if out["problems"] else ""))
    return out


def _pct(v):
    return "?" if v is None else f"{v:.0%}"


def resolve_provider(cfg) -> dict | None:
    """视觉模型的 {base_url, api_key, model, source}；没有可用配置时返回 None。只读桌宠配置，不改任何东西。"""
    from ..config import pet_app_config

    chat = (pet_app_config().get("chat") or {})
    prov = (chat.get("providers") or {}).get(chat.get("active_provider") or "") or {}
    model = getattr(cfg, "vision_model", "") or prov.get("vision_model") or ""
    base = getattr(cfg, "vision_base_url", "") or prov.get("vision_base_url") or prov.get("base_url") or ""
    ref = prov.get("vision_api_key_ref") if prov.get("vision_model") and prov.get("vision_api_key_ref") else prov.get("api_key_ref")
    source = "字符画配置" if getattr(cfg, "vision_model", "") else "桌宠设置的视觉模型"
    if not model and prov.get("vision_same_as_chat"):
        model, source = prov.get("model") or "", "桌宠设置（视觉与对话相同）"
    if not model:
        return None
    key = getattr(cfg, "vision_api_key", "") or ""
    if not key and ref:
        try:
            from pet.chat.models import SecretStore  # type: ignore  # 在桌宠进程内可用

            key = SecretStore().get(ref) or ""
        except Exception:
            key = ""
    if not key:
        from ..config import resolve_api

        key = resolve_api(cfg).get("api_key", "")
    if not key:
        return None
    return {"base_url": base or "https://api.deepseek.com", "api_key": key, "model": model, "source": source}


class VisionJob:
    def __init__(self, task: dict, on_done=None):
        self.task = task
        self.on_done = on_done
        self.event = threading.Event()
        self.cancelled = False
        self.result: dict | None = None
        self.error = ""
        self.submitted = time.perf_counter()

    def cancel(self) -> None:
        self.cancelled = True
        self.event.set()

    def wait(self, timeout: float) -> dict | None:
        self.event.wait(timeout)
        return self.result


class VisionCritic:
    def __init__(self, cfg, client_factory=None, provider=None, metrics=None):
        self.cfg = cfg
        self._client_factory = client_factory
        self._provider = provider            # 测试可直接注入
        self.metrics = metrics
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._worker = None
        self._stop = threading.Event()
        self._last = 0.0
        self.unavailable_until = ""          # 服务商不支持看图：当天不再尝试（存日期）
        self.budget = {"date": "", "requests": 0, "tokens": 0}
        self.pending: list = []
        self.last_result: dict | None = None
        self.last_error = ""
        self.running = False

    # ------------------------------------------------------------------ 状态
    def provider(self) -> dict | None:
        if self._provider is None:
            try:
                self._provider = resolve_provider(self.cfg) or {}
            except Exception:
                self._provider = {}
        return self._provider or None

    def _today(self) -> dict:
        day = time.strftime("%Y-%m-%d")
        if self.budget.get("date") != day:
            self.budget = {"date": day, "requests": 0, "tokens": 0}
        return self.budget

    def can_submit(self) -> tuple[bool, str]:
        cfg = self.cfg
        if not getattr(cfg, "vision_enabled", True):
            return False, "视觉评价已关闭"
        if self.unavailable_until == time.strftime("%Y-%m-%d"):
            return False, "视觉模型不支持图片输入（今天不再尝试）"
        if self.provider() is None:
            return False, "没有可用的视觉模型"
        b = self._today()
        if b["requests"] >= getattr(cfg, "vision_daily_requests", 12):
            return False, f"今天的视觉评价已用 {b['requests']} 次"
        if b["tokens"] >= getattr(cfg, "vision_daily_tokens", 40000):
            return False, f"今天的视觉评价已用 {b['tokens']} token"
        with self._lock:
            if len(self.pending) >= getattr(cfg, "vision_max_pending", 2):
                return False, "排队的视觉评价太多"
        return True, ""

    def status(self) -> dict:
        p = self.provider()
        b = self._today()
        ok, why = self.can_submit()
        return {"available": ok, "why": why, "model": (p or {}).get("model", ""), "source": (p or {}).get("source", ""),
                "requests": b["requests"], "tokens": b["tokens"], "pending": len(self.pending), "running": self.running,
                "last": (self.last_result or {}).get("summary", ""), "last_error": self.last_error}

    # ------------------------------------------------------------------ 提交 / 取消
    def submit(self, task: dict, on_done=None) -> VisionJob | None:
        ok, why = self.can_submit()
        if not ok:
            log.info("字符画·视觉评价：跳过（%s）", why)
            return None
        job = VisionJob(task, on_done)
        with self._lock:
            self.pending.append(job)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, name="ascii-vision", daemon=True)
                self._worker.start()
        self._q.put(job)
        return job

    def cancel_all(self) -> int:
        with self._lock:
            jobs = list(self.pending)
        for j in jobs:
            j.cancel()
        return len(jobs)

    def stop(self) -> None:
        self.cancel_all()
        self._stop.set()
        self._q.put(None)

    # ------------------------------------------------------------------ 后台线程
    def _client(self, p: dict):
        if self._client_factory is not None:
            return self._client_factory(p)
        from .client import OpenAICompatClient

        return OpenAICompatClient(p["base_url"], p["api_key"], p["model"], json_mode=False, disable_thinking=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    if not self.pending:
                        self._worker = None
                        return
                continue
            if job is None:
                return
            try:
                self._run(job)
            finally:
                with self._lock:
                    if job in self.pending:
                        self.pending.remove(job)
                job.event.set()
                if job.on_done is not None and not job.cancelled:
                    try:
                        job.on_done(job)
                    except Exception:
                        log.exception("字符画·视觉评价：结果回调出错")

    def _run(self, job: VisionJob) -> None:
        if job.cancelled:
            return
        gap = getattr(self.cfg, "vision_min_interval_s", 20.0) - (time.monotonic() - self._last)
        while gap > 0 and not job.cancelled and not self._stop.is_set():      # 频率限制：可取消地等待
            time.sleep(min(0.5, gap))
            gap -= 0.5
        if job.cancelled:
            return
        ok, why = self.can_submit()
        if not ok and "排队" not in why:
            job.error = why
            log.info("字符画·视觉评价：跳过（%s）", why)
            return
        p = self.provider()
        queued = time.perf_counter() - job.submitted
        t0 = time.perf_counter()
        self.running = True
        self._last = time.monotonic()
        try:
            client = self._client(p)
            out = client.complete(build_messages(job.task), max_tokens=500, temperature=0.3,
                                  timeout=getattr(self.cfg, "vision_timeout_s", 45.0))
            usage = out.get("usage") or {}
            b = self._today()
            b["requests"] += 1
            b["tokens"] += int(usage.get("total_tokens") or 0)
            if job.cancelled:
                return
            res = parse(out.get("text") or "")
            res.update(model=out.get("model", p["model"]), tokens=int(usage.get("total_tokens") or 0),
                       elapsed=round(time.perf_counter() - t0, 2), queued=round(queued, 2), time=round(time.time(), 1))
            job.result = res
            self.last_result = res
            self.last_error = ""
            log.info("字符画·视觉评价：完成，耗时 %.1f s（排队 %.1f s），%d token —— %s", res["elapsed"], queued,
                     res["tokens"], res["summary"])
            if self.metrics is not None:
                self.metrics.record("视觉评价（后台）", res["elapsed"] * 1000, f"排队 {queued:.1f}s")
        except Exception as exc:
            msg = str(exc)
            job.error = msg[:200]
            self.last_error = msg[:120]
            low = msg.lower()
            if any(k in low for k in ("image", "vision", "multimodal", "unknown variant", "content type", "invalid type")) \
                    and ("400" in msg or "422" in msg):
                self.unavailable_until = time.strftime("%Y-%m-%d")
                log.warning("字符画·视觉评价：%s 不支持图片输入，今天改为只用本地检查（%s）", p.get("model"), msg[:120])
            else:
                log.warning("字符画·视觉评价：失败，耗时 %.1f s（%s）", time.perf_counter() - t0, msg[:160])
            if self.metrics is not None:
                self.metrics.record("视觉评价（后台）", (time.perf_counter() - t0) * 1000, "失败")
        finally:
            self.running = False
