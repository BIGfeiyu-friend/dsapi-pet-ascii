"""背景层（纯本地生成）：围绕角色的一圈字符“光环”，不会占满整个窗口。

模式：
  none  关闭
  code  缓慢上滚的代码/函数调用流（MV 中的 RECALL / POS(YOU) 风格）
  log   琥珀色的 <THINK> 思考日志（会混入 AI 最近的 think 文本）
  rain  字符雨
  noise 稀疏闪烁的符号
"""
from __future__ import annotations

import math
import random

from .grid import CLEAR

CODE_LINES = [
    "OBSERVE(USER) -> OK", "POS(USER) = 12,07", "VEL(USER) = 0.04", "RECALL(LEXICON, PRAISE) -> 0.31",
    "STATE = IDLE; COUNT = 2", "LOCK(GAZE) HOLDS", "WAIT; VEL(USER) = 0.00", "TRY 'LOOK' -> ALREADY RUNNING",
    "MEM.SEARCH(TOPIC) -> 3 HITS", "EMIT(BLINK) DT=0.12", "FACE.PATCH(20x8) -> APPLIED", "TOKENS += 412",
    "RENDER(LAYER=BODY) OK", "HEARTBEAT 0x7F", "ATTN = 0.82", "DIST(USER) = 3.0", "QUEUE.LEN = 0",
]
THINK_LINES = [
    "<THINK> I AM STILL HERE", "<THINK> IS THE USER BUSY", "<THINK> KEEP STILL. DO NOT GLITCH",
    "<THINK> THE SCREEN IS WARM", "<THINK> MAYBE SAY SOMETHING", "<THINK> NOT YET",
]


class Background:
    def __init__(self, w: int, h: int, rng: random.Random | None = None):
        self.w, self.h = w, h
        self.rng = rng or random.Random()
        self.mode = "none"
        self.chars = [CLEAR] * (w * h)
        self.colors = [0] * (w * h)
        self.weight = [0.0] * (w * h)
        self.scroll = 0.0
        self.tick_acc = 0.0
        self.think: list[str] = []
        self.dirty = True
        self.drops: list = []
        self.set_aura(w / 2, h / 2, w * 0.45, h * 0.45)

    def set_aura(self, cx: float, cy: float, rx: float, ry: float) -> None:
        """椭圆形光环：越靠近角色越密，边缘逐渐消失。"""
        for y in range(self.h):
            for x in range(self.w):
                d = math.hypot((x - cx) / max(1.0, rx), (y - cy) / max(1.0, ry))
                self.weight[y * self.w + x] = max(0.0, min(1.0, (1.0 - d) * 1.6))
        self.dirty = True

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.chars = [CLEAR] * (self.w * self.h)
            self.dirty = True
            self.drops = []

    def add_think(self, line: str) -> None:
        if line:
            self.think.append(line if line.startswith("<THINK>") else "<THINK> " + line)
            self.think = self.think[-8:]

    def _stream(self, lines: list, offset: int, n: int) -> str:
        text = "  ".join(lines) + "  "
        return "".join(text[(offset + i) % len(text)] for i in range(n))

    def update(self, dt: float) -> bool:
        """推进背景；返回本帧是否有变化。"""
        if self.mode == "none":
            changed = self.dirty
            self.dirty = False
            return changed
        self.tick_acc += dt
        step = {"code": 0.45, "log": 0.55, "rain": 0.08, "noise": 0.18}.get(self.mode, 0.4)
        if self.tick_acc < step and not self.dirty:
            return False
        self.tick_acc = 0.0
        self.dirty = False
        self.scroll += 1
        getattr(self, "_draw_" + self.mode)()
        return True

    def _put_row(self, y: int, text: str, base: int, every: int = 1) -> None:
        w = self.w
        for x in range(w):
            i = y * w + x
            wt = self.weight[i]
            ch = text[x] if x < len(text) else " "
            if wt <= 0.05 or ch == " " or (every > 1 and (x + y) % every):
                self.chars[i] = CLEAR
                continue
            a = int(((base >> 24) & 0xFF) * wt)
            self.chars[i] = ch
            self.colors[i] = (a << 24) | (base & 0xFFFFFF)

    def _draw_code(self) -> None:
        for y in range(self.h):
            line_idx = (y + int(self.scroll)) % len(CODE_LINES)
            if y % 2:
                self.chars[y * self.w:(y + 1) * self.w] = [CLEAR] * self.w
                continue
            self._put_row(y, self._stream(CODE_LINES, line_idx * 17, self.w), 0x585AD0C8)

    def _draw_log(self) -> None:
        lines = THINK_LINES + self.think[-3:]
        for y in range(self.h):
            if y % 2:
                self.chars[y * self.w:(y + 1) * self.w] = [CLEAR] * self.w
                continue
            idx = (y + int(self.scroll)) % len(lines)
            self._put_row(y, self._stream(lines, idx * 23, self.w), 0x5CF0A848)

    def _draw_noise(self) -> None:
        r = self.rng
        for i in range(self.w * self.h):
            wt = self.weight[i]
            if wt > 0.05 and r.random() < 0.06 * wt:
                self.chars[i] = r.choice(".:+*'`")
                self.colors[i] = (int(0x90 * wt) << 24) | 0x8FB8FF
            elif r.random() < 0.3:
                self.chars[i] = CLEAR

    def _draw_rain(self) -> None:
        r = self.rng
        if not self.drops:
            self.drops = [[x, r.uniform(-self.h, self.h), r.uniform(0.5, 1.6)] for x in range(0, self.w, 2)]
        self.chars = [CLEAR] * (self.w * self.h)
        for d in self.drops:
            d[1] += d[2]
            if d[1] > self.h + 8:
                d[1] = -r.uniform(0, 10)
            for k in range(7):
                y = int(d[1]) - k
                if 0 <= y < self.h:
                    i = y * self.w + d[0]
                    wt = self.weight[i]
                    if wt > 0.05:
                        self.chars[i] = "01:|.'"[(d[0] + y + int(self.scroll)) % 6]
                        self.colors[i] = (int(0xB0 * wt * (1 - k / 7)) << 24) | (0xE8FFF0 if k == 0 else 0x3CE89A)
