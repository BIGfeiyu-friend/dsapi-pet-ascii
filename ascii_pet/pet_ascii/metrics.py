"""耗时记录（v0.7）：分别记录“动画响应延迟”和“后台任务耗时”，写日志，并在测试台里显示统计。

- 内联标签 → 开始播放：对话流里剥出表情标签，到本地引擎开始演出（不调用 API，通常几毫秒）
- API 表情 → 开始播放：发起生成请求到结果开始演出（含排队和 API 往返）
- 视觉评价（后台）、创作实验（后台）：后台任务从开始到结束；它们不占界面线程，不影响上面两项
"""
from __future__ import annotations

import logging
import threading
from collections import deque

log = logging.getLogger("pet.ascii")


class Latency:
    def __init__(self, keep: int = 100):
        self._lock = threading.Lock()
        self.data: dict = {}
        self.keep = keep

    def record(self, kind: str, ms: float, note: str = "", quiet: bool = False) -> None:
        with self._lock:
            self.data.setdefault(kind, deque(maxlen=self.keep)).append(float(ms))
        if not quiet:
            if ms >= 1000:
                log.info("字符画·耗时：%s %.2f s%s", kind, ms / 1000, f"（{note}）" if note else "")
            else:
                log.info("字符画·耗时：%s %.1f ms%s", kind, ms, f"（{note}）" if note else "")

    def stats(self) -> dict:
        with self._lock:
            out = {}
            for kind, q in self.data.items():
                v = sorted(q)
                if not v:
                    continue
                out[kind] = {"n": len(v), "avg": sum(v) / len(v), "p50": v[len(v) // 2],
                             "p95": v[min(len(v) - 1, int(len(v) * 0.95))], "max": v[-1], "last": q[-1]}
            return out

    def text(self) -> str:
        parts = []
        for kind, s in self.stats().items():
            unit = (lambda x: f"{x / 1000:.1f}s") if s["p50"] >= 1000 else (lambda x: f"{x:.0f}ms")
            parts.append(f"{kind}：中位 {unit(s['p50'])} / p95 {unit(s['p95'])}（{s['n']} 次）")
        return "；".join(parts) or "暂无记录"
