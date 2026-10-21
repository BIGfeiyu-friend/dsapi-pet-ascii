"""本地特效引擎：AI 只给出特效名和参数，逐帧计算全部在本地完成。

两类特效：
  绘制型 draw(cells, ctx)            往稀疏字典 cells[(x, y)] = (ch, color) 里写字符
  后处理型 post(chars, colors, W, H)  对合成后的整帧做变换（闪光、乱码、色调、渐隐、扫描线）
另外 px_offset() 可返回像素级位移（抖动）。坐标均为画布坐标。
"""
from __future__ import annotations

import math
import random

from .grid import CLEAR, WIDE_CONT, char_width, mix, with_alpha

GLITCH_CHARS = "#%&@$01<>/\\|=+*?![]{}"
WARM = 0xFFFFD27A
PINK = 0xFFFF7EB6
CYAN = 0xFF6EF3FF


class Effect:
    hides: tuple = ()

    def __init__(self, duration: float, params: dict | None = None, rng: random.Random | None = None):
        self.duration = max(0.05, duration)
        self.t = 0.0
        self.p = params or {}
        self.rng = rng or random.Random()

    @property
    def done(self) -> bool:
        return self.t >= self.duration

    @property
    def k(self) -> float:
        return min(1.0, self.t / self.duration)

    def envelope(self, attack: float = 0.15, release: float = 0.3) -> float:
        k = self.k
        if k < attack:
            return k / attack
        if k > 1 - release:
            return max(0.0, (1 - k) / release)
        return 1.0

    def update(self, dt: float) -> None:
        self.t += dt

    def draw(self, cells: dict, ctx: dict) -> None:
        pass

    def post(self, chars: list, colors: list, w: int, h: int) -> None:
        pass

    def px_offset(self) -> tuple[float, float]:
        return 0.0, 0.0


def _col(p, key="color", default=0xFFFFFFFF):
    v = p.get(key)
    return v if v is not None else default


class Particles(Effect):
    def __init__(self, duration, params, rng, origin, spread_rect=None):
        super().__init__(duration, params, rng)
        chars = params.get("chars") or "*+.o'`"
        n = int(params.get("count", 24))
        speed = 6.0 * float(params.get("speed", 1.0))
        direction = params.get("direction", "out")
        self.color = _col(params, "color", WARM)
        self.color2 = params.get("color2") or self.color
        self.ps = []
        ox, oy = origin
        for _ in range(n):
            if direction == "up":
                a = -math.pi / 2 + rng.uniform(-0.7, 0.7)
            elif direction == "down":
                a = math.pi / 2 + rng.uniform(-0.7, 0.7)
            elif direction == "left":
                a = math.pi + rng.uniform(-0.6, 0.6)
            elif direction == "right":
                a = rng.uniform(-0.6, 0.6)
            else:
                a = rng.uniform(0, 2 * math.pi)
            v = speed * rng.uniform(0.4, 1.3)
            sx, sy = ox, oy
            if direction == "in" and spread_rect:
                x0, y0, w, h = spread_rect
                sx, sy = rng.uniform(x0 - 6, x0 + w + 6), rng.uniform(y0 - 4, y0 + h + 4)
                a = math.atan2(oy - sy, (ox - sx) / 2)
            self.ps.append([sx, sy, math.cos(a) * v * 2.0, math.sin(a) * v, rng.choice(chars),
                            rng.uniform(0.6, 1.0) * self.duration, rng.random()])
        self.gravity = 7.0 if direction in ("down", "out") else 0.0

    def update(self, dt):
        super().update(dt)
        for p in self.ps:
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            p[3] += self.gravity * dt

    def draw(self, cells, ctx):
        for x, y, _vx, _vy, ch, life, mixk in self.ps:
            if self.t > life:
                continue
            fade = 1.0 - self.t / life
            col = with_alpha(mix(self.color, self.color2, mixk), max(0.15, fade))
            cells[(int(round(x)), int(round(y)))] = (ch, col)


class Sparkle(Effect):
    def __init__(self, duration, params, rng, rect):
        super().__init__(duration, params, rng)
        self.rect = rect
        self.chars = params.get("chars") or "*+.✦·"
        self.color = _col(params, "color", 0xFFFFF4B0)
        self.rate = 6 + 30 * float(params.get("intensity", 0.6))
        self.stars = []

    def update(self, dt):
        super().update(dt)
        x0, y0, w, h = self.rect
        n = self.rng.random() < self.rate * dt * self.envelope()
        for _ in range(int(self.rate * dt) + (1 if n else 0)):
            self.stars.append([self.rng.randint(x0 - 4, x0 + w + 3), self.rng.randint(y0 - 3, y0 + h + 2), self.t,
                               self.rng.uniform(0.2, 0.6), self.rng.choice(self.chars)])
        self.stars = [s for s in self.stars if self.t - s[2] < s[3]]

    def draw(self, cells, ctx):
        for x, y, born, life, ch in self.stars:
            age = (self.t - born) / life
            cells[(x, y)] = (ch if age < 0.6 else ".", with_alpha(self.color, 1 - age * 0.8))


class Symbols(Effect):
    """头顶冒出的符号：? ! ♪ ♥ … 逐个升起并左右轻摆。"""

    def __init__(self, duration, params, rng, origin):
        super().__init__(duration, params, rng)
        self.text = params.get("chars") or params.get("text") or "?"
        self.color = _col(params, "color", 0xFFFFE066)
        self.origin = origin
        self.items = []
        self.next_spawn = 0.0
        self.interval = max(0.15, 0.55 / float(params.get("speed", 1.0)))

    def update(self, dt):
        super().update(dt)
        if self.t >= self.next_spawn and self.k < 0.75:
            self.next_spawn = self.t + self.interval
            ch = self.text[len(self.items) % len(self.text)]
            self.items.append([self.origin[0] + self.rng.randint(-5, 5), self.origin[1], self.t, ch])

    def draw(self, cells, ctx):
        for x, y, born, ch in self.items:
            age = self.t - born
            yy = int(round(y - age * 4.0))
            xx = int(round(x + math.sin(age * 5 + x) * 1.2))
            if age < 1.6:
                cells[(xx, yy)] = (ch, with_alpha(self.color, max(0.2, 1 - age / 1.6)))


class Bubble(Effect):
    """对话气泡（支持中文，宽字符占两格）。"""

    def __init__(self, duration, params, rng, anchor):
        super().__init__(duration, params, rng)
        self.text = params.get("text") or "..."
        self.color = _col(params, "color", 0xFFF2F6FF)
        self.anchor = anchor
        self.lines = self._wrap(self.text, 26)

    @staticmethod
    def _wrap(text, width):
        lines, cur, cw = [], "", 0
        for ch in text:
            w = char_width(ch) or 1
            if cw + w > width or ch == "\n":
                lines.append(cur)
                cur, cw = "", 0
                if ch == "\n":
                    continue
            cur += ch
            cw += w
        if cur:
            lines.append(cur)
        return lines[:4]

    def draw(self, cells, ctx):
        k = self.k
        shown = len(self.text) if k > 0.25 else int(len(self.text) * k / 0.25)
        width = max(sum(char_width(c) or 1 for c in line) for line in self.lines) if self.lines else 3
        ax, ay = self.anchor
        x0 = max(0, min(ctx["W"] - width - 4, ax - width // 2 - 2))
        y0 = max(0, ay - len(self.lines) - 3)
        a = self.envelope(0.05, 0.12)
        col = with_alpha(self.color, a)
        dim = with_alpha(0xFF8FA4C8, a)
        cells[(x0, y0)] = (".", dim)
        cells[(x0 + width + 3, y0)] = (".", dim)
        for x in range(x0 + 1, x0 + width + 3):
            cells[(x, y0)] = ("-", dim)
            cells[(x, y0 + len(self.lines) + 1)] = ("-", dim)
        cells[(x0, y0 + len(self.lines) + 1)] = ("'", dim)
        cells[(x0 + width + 3, y0 + len(self.lines) + 1)] = ("'", dim)
        left = shown
        for i, line in enumerate(self.lines):
            y = y0 + 1 + i
            cells[(x0, y)] = ("|", dim)
            cells[(x0 + width + 3, y)] = ("|", dim)
            for x in range(x0 + 1, x0 + width + 3):
                cells[(x, y)] = (" ", with_alpha(0xFF0C1424, a))
            x = x0 + 2
            for ch in line:
                if left <= 0:
                    break
                left -= 1
                cells[(x, y)] = (ch, col)
                if char_width(ch) == 2:
                    cells[(x + 1, y)] = (WIDE_CONT, col)
                    x += 2
                else:
                    x += 1
        tail_x = max(x0 + 2, min(x0 + width + 1, ax))
        cells[(tail_x, y0 + len(self.lines) + 2)] = ("v", dim)


class Rain(Effect):
    def __init__(self, duration, params, rng, w, h):
        super().__init__(duration, params, rng)
        self.chars = params.get("chars") or "01|:.<>/\\"
        self.color = _col(params, "color", 0xFF52FFA8)
        self.drops = [[x, rng.uniform(-h, 0), rng.uniform(8, 22) * params.get("speed", 1.0), rng.randint(3, 9)]
                      for x in range(0, w, 2) if rng.random() < 0.35 + 0.6 * params.get("intensity", 0.6)]
        self.h = h

    def update(self, dt):
        super().update(dt)
        for d in self.drops:
            d[1] += d[2] * dt
            if d[1] - d[3] > self.h:
                d[1] = -self.rng.uniform(0, 8)

    def draw(self, cells, ctx):
        a = self.envelope()
        for x, y, _v, n in self.drops:
            for i in range(n):
                yy = int(y) - i
                if 0 <= yy < self.h:
                    ch = self.chars[(x * 7 + yy * 3 + int(self.t * 10)) % len(self.chars)]
                    cells[(x, yy)] = (ch, with_alpha(self.color if i else 0xFFE8FFF2, a * (1 - i / n)))


class Ripple(Effect):
    def __init__(self, duration, params, rng, origin):
        super().__init__(duration, params, rng)
        self.origin = origin
        self.chars = params.get("chars") or "o.:"
        self.color = _col(params, "color", CYAN)
        self.maxr = 10 + 30 * params.get("intensity", 0.6)

    def draw(self, cells, ctx):
        r = self.k * self.maxr
        ox, oy = self.origin
        a = 1 - self.k
        steps = max(12, int(r * 6))
        for i in range(steps):
            th = 2 * math.pi * i / steps
            x = int(round(ox + math.cos(th) * r * 2.0))
            y = int(round(oy + math.sin(th) * r))
            cells[(x, y)] = (self.chars[i % len(self.chars)], with_alpha(self.color, a))


class TextFx(Effect):
    def __init__(self, duration, params, rng, pos):
        super().__init__(duration, params, rng)
        self.text = params.get("text") or ""
        self.pos = pos
        self.color = _col(params, "color", 0xFFB8FFD8)

    def draw(self, cells, ctx):
        n = int(len(self.text) * min(1.0, self.k / 0.4))
        x, y = self.pos
        a = self.envelope(0.02, 0.2)
        for ch in self.text[:n]:
            cells[(x, y)] = (ch, with_alpha(self.color, a))
            if char_width(ch) == 2:
                cells[(x + 1, y)] = (WIDE_CONT, 0)
                x += 2
            else:
                x += 1


class Dissolve(Effect):
    """角色解体成字符粒子，飞散、悬停后重新聚合。期间隐藏身体和脸。"""
    hides = ("body", "face")

    def __init__(self, duration, params, rng, source_cells, center):
        super().__init__(duration, params, rng)
        self.cells = []
        cx, cy = center
        power = 0.6 + params.get("intensity", 0.6)
        for (x, y), (ch, col) in source_cells:
            dx, dy = x - cx, (y - cy) * 2
            d = math.hypot(dx, dy) or 1.0
            ang = math.atan2(dy, dx) + rng.uniform(-0.5, 0.5)
            dist = rng.uniform(6, 26) * power
            self.cells.append((x, y, ch, col, math.cos(ang) * dist * 1.6, math.sin(ang) * dist * 0.6,
                               rng.uniform(0.0, 0.15)))
        self.glyphs = params.get("chars") or GLITCH_CHARS

    def draw(self, cells, ctx):
        k = self.k
        for x, y, ch, col, vx, vy, delay in self.cells:
            kk = min(1.0, max(0.0, (k - delay) / (1 - delay)))
            f = math.sin(math.pi * kk) ** 0.7
            xx = int(round(x + vx * f))
            yy = int(round(y + vy * f + math.sin(kk * 9 + x) * f))
            g = ch if f < 0.25 or self.rng.random() > 0.3 else self.rng.choice(self.glyphs)
            cells[(xx, yy)] = (g, with_alpha(col, 1 - 0.5 * f))


# ---------------------------------------------------------------------- 后处理
class Glitch(Effect):
    def __init__(self, duration, params, rng):
        super().__init__(duration, params, rng)
        self.intensity = params.get("intensity", 0.6)
        self.chars = params.get("chars") or GLITCH_CHARS

    def post(self, chars, colors, w, h):
        amt = self.intensity * self.envelope(0.05, 0.4)
        r = self.rng
        for _ in range(int(h * amt * 0.5)):
            y = r.randrange(h)
            shift = r.choice((-3, -2, -1, 1, 2, 3))
            row = slice(y * w, (y + 1) * w)
            rc, rk = chars[row], colors[row]
            chars[row] = rc[-shift:] + rc[:-shift]
            colors[row] = rk[-shift:] + rk[:-shift]
        for _ in range(int(w * h * 0.03 * amt)):
            i = r.randrange(w * h)
            if chars[i] not in (CLEAR, WIDE_CONT) and char_width(chars[i]) != 2:
                chars[i] = r.choice(self.chars)
                colors[i] = r.choice((PINK, CYAN, 0xFFFFFFFF))


class Flash(Effect):
    def __init__(self, duration, params, rng):
        super().__init__(duration, params, rng)
        self.color = _col(params, "color", 0xFFFFFFFF)
        self.amount = 0.4 + 0.6 * params.get("intensity", 0.6)

    def post(self, chars, colors, w, h):
        k = self.amount * (1 - self.k) ** 2
        if k < 0.02:
            return
        c = self.color
        for i, ch in enumerate(chars):
            if ch != CLEAR:
                colors[i] = mix(colors[i], c | 0xFF000000, k) & 0x00FFFFFF | (colors[i] & 0xFF000000)


class ColorShift(Effect):
    def __init__(self, duration, params, rng):
        super().__init__(duration, params, rng)
        self.color = _col(params, "color", PINK)
        self.amount = params.get("intensity", 0.6) * 0.8

    def post(self, chars, colors, w, h):
        k = self.amount * self.envelope(0.2, 0.3)
        if k < 0.02:
            return
        for i, ch in enumerate(chars):
            if ch != CLEAR:
                colors[i] = mix(colors[i], self.color, k) & 0x00FFFFFF | (colors[i] & 0xFF000000)


class Fade(Effect):
    def __init__(self, duration, params, rng):
        super().__init__(duration, params, rng)
        self.depth = 0.3 + 0.7 * params.get("intensity", 0.6)
        self.direction = params.get("direction", "out")

    def post(self, chars, colors, w, h):
        if self.direction == "in":
            a = self.k
        else:
            a = 1 - self.depth * math.sin(math.pi * self.k)
        for i, ch in enumerate(chars):
            if ch != CLEAR:
                colors[i] = with_alpha(colors[i], a)


class Scanline(Effect):
    def __init__(self, duration, params, rng):
        super().__init__(duration, params, rng)
        self.color = _col(params, "color", 0xFFB8FFE8)

    def post(self, chars, colors, w, h):
        y0 = int(self.k * (h + 6)) - 3
        for y in range(max(0, y0 - 2), min(h, y0 + 1)):
            k = 0.7 if y == y0 else 0.35
            for i in range(y * w, (y + 1) * w):
                if chars[i] != CLEAR:
                    colors[i] = mix(colors[i], self.color, k)


class Shake(Effect):
    def __init__(self, duration, params, rng):
        super().__init__(duration, params, rng)
        self.amp = 1.5 + 5 * params.get("intensity", 0.6)

    def px_offset(self):
        a = self.amp * (1 - self.k)
        return self.rng.uniform(-a, a), self.rng.uniform(-a * 0.5, a * 0.5)


def make_effect(spec: dict, duration: float, rng: random.Random, geo: dict, source_cells=None) -> Effect | None:
    """根据协议里的特效规格和当前几何信息实例化特效。geo 由引擎提供各锚点位置。"""
    name = spec["name"]
    origin = geo["origin"]
    if name == "particles":
        return Particles(duration, spec, rng, origin, geo.get("rect"))
    if name == "sparkle":
        return Sparkle(duration, spec, rng, geo.get("rect") or (origin[0] - 4, origin[1] - 2, 8, 4))
    if name == "symbols":
        return Symbols(duration, spec, rng, geo["head"])
    if name == "bubble":
        return Bubble(duration, spec, rng, geo["head"])
    if name == "rain":
        return Rain(duration, spec, rng, geo["W"], geo["H"])
    if name == "ripple":
        return Ripple(duration, spec, rng, origin)
    if name == "text":
        return TextFx(duration, spec, rng, origin)
    if name == "dissolve":
        return Dissolve(duration, spec, rng, source_cells or [], geo["center"])
    if name == "glitch":
        return Glitch(duration, spec, rng)
    if name == "flash":
        return Flash(duration, spec, rng)
    if name == "color_shift":
        return ColorShift(duration, spec, rng)
    if name == "fade":
        return Fade(duration, spec, rng)
    if name == "scanline":
        return Scanline(duration, spec, rng)
    if name == "shake":
        return Shake(duration, spec, rng)
    return None


# ---------------------------------------------------------------------- 睡眠（由睡眠控制器直接创建，不经过协议）
SLEEP_Z = 0xFFC8D0FF
SLEEP_BUBBLE = 0xFFBFEFFF


class SleepZ(Effect):
    """睡着时飘起的 z Z Ｚ：随每次呼气冒出一串，一个比一个大、沿右上方排开，然后慢慢飘走、变淡。

    emit() 由睡眠控制器在呼气时调用，所以节奏跟着呼吸走，不是固定循环。大号的 Z 用字母拼成
    （字符画风格，格子窄高，所以斜笔两格宽）。glow 记录本帧画出的格子，夜色后处理会跳过它们（夜里 Z 仍然是亮的）。
    """

    ARTS = (("z",), ("Z",),
            ("ZZZZ", "  Z ", " Z  ", "ZZZZ"),
            ("ZZZZZZ", "   ZZ ", "  ZZ  ", " ZZ   ", "ZZZZZZ"))
    OFFSETS = ((0, 0), (2, -2), (4, -4), (5, -5))     # 第几个 → 相对起点的位置（右上方依次排开；坐标是图案左下角）

    def __init__(self, origin, rng=None):
        super().__init__(1e9, {}, rng)
        self.origin = origin
        self.items: list = []           # [出生时间, x0, y0, 相位, 寿命, 大小档]
        self.pending: list = []         # [何时出生, 大小档]
        self.glow: set = set()

    def emit(self, n: int = 3, big: bool = False, gap: float = 0.45) -> None:
        t = self.t
        n = max(1, n)
        for i in range(n):
            t += gap * self.rng.uniform(0.75, 1.3)
            tier = 3 if big and i == n - 1 else min(i, 2)
            self.pending.append([t, tier])

    def stop(self, fade: float = 2.5) -> None:
        self.pending.clear()
        self.duration = self.t + fade

    def update(self, dt):
        super().update(dt)
        for p in [p for p in self.pending if p[0] <= self.t]:
            self.pending.remove(p)
            ox, oy = self.origin
            dx, dy = self.OFFSETS[p[1]]
            self.items.append([self.t, ox + dx + self.rng.uniform(-0.6, 0.6), oy + dy + self.rng.uniform(-0.4, 0.4),
                               self.rng.uniform(0, 6.3), self.rng.uniform(2.6, 3.6) + 0.3 * p[1], p[1]])
        self.items = [it for it in self.items if self.t - it[0] < it[4]]

    def draw(self, cells, ctx):
        self.glow = set()
        fade_out = 1.0 if self.duration > 1e8 else max(0.0, (self.duration - self.t) / 2.5)
        for born, x0, y0, ph, life, tier in self.items:
            a = self.t - born
            k = a / life
            x = int(round(x0 + a * 0.75 + math.sin(a * 1.5 + ph) * 0.7))
            y = int(round(y0 - a * 0.6))
            alpha = max(0.0, min(1.0, a / 0.3)) * (1.0 - max(0.0, k - 0.55) / 0.45) * fade_out
            if alpha <= 0.03:
                continue
            art = (".",) if a < 0.12 and tier < 2 else self.ARTS[tier]
            col = with_alpha(SLEEP_Z, alpha)
            top = y - (len(art) - 1)
            for j, row in enumerate(art):
                for i, ch in enumerate(row):
                    if ch != " ":
                        cells[(x + i, top + j)] = (ch, col)
                        self.glow.add((x + i, top + j))


class NoseBubble(Effect):
    """鼻涕泡：随吸气鼓起、呼气缩小，吹到最大会“啵”地破掉（溅出几个点）。"""

    SHAPES = ((".",), ("o",), ("O",), (" _ ", "(_)"), (" .-. ", "(   )", " `-' "), (" .--. ", "(    )", " `--' "))

    def __init__(self, origin, rng=None, max_size: int = 4):
        super().__init__(1e9, {}, rng)
        self.origin = origin
        self.size = 0.0                 # 0~max_size
        self.target = 0.0
        self.max_size = max_size
        self.popped_at = -1.0
        self.glow: set = set()

    def breathe(self, inhale: bool) -> None:
        if self.popped_at >= 0:
            return
        if inhale:
            self.target = min(self.max_size, self.target + self.rng.uniform(0.7, 1.3))
        else:
            self.target = max(0.0, self.target - self.rng.uniform(0.2, 0.6))

    def pop(self) -> None:
        if self.popped_at < 0:
            self.popped_at = self.t
            self.duration = self.t + 0.9

    def stop(self, fade: float = 0.3) -> None:
        self.duration = min(self.duration, self.t + fade)

    def update(self, dt):
        super().update(dt)
        self.size += (self.target - self.size) * min(1.0, dt * 1.8)
        if self.popped_at < 0 and self.size >= self.max_size - 0.25:
            self.pop()

    def draw(self, cells, ctx):
        self.glow = set()
        ox, oy = self.origin
        if self.popped_at >= 0:                         # 破掉：几个点向外溅开
            a = self.t - self.popped_at
            cx, cy = ox + self.max_size * 0.5, oy - 1
            for dx, dy in ((-2, -1), (2, -1), (-3, 0), (3, 0), (0, -2), (1, 1), (-1, 1), (2, -2)):
                x, y = int(round(cx + dx * (0.8 + a * 3.0))), int(round(cy + dy * (0.6 + a * 1.6)))
                cells[(x, y)] = ("·" if a > 0.35 else "*", with_alpha(SLEEP_BUBBLE, max(0.0, 1 - a / 0.9)))
                self.glow.add((x, y))
            return
        shape = self.SHAPES[max(0, min(len(self.SHAPES) - 1, int(round(self.size))))]
        h = len(shape)
        for j, row in enumerate(shape):
            for i, ch in enumerate(row):
                if ch == " ":
                    continue
                x, y = ox + i, oy - (h - 1) + j
                cells[(x, y)] = (ch, with_alpha(SLEEP_BUBBLE, 0.9))
                self.glow.add((x, y))


class NightMood(Effect):
    """入夜：整幅画面慢慢压暗、偏蓝，并随呼吸极轻地明暗起伏；Z 和鼻涕泡（glow）不受影响。"""

    def __init__(self, rng=None, dim: float = 0.28, blue: float = 0.3, fade_in: float = 6.0):
        super().__init__(1e9, {}, rng)
        self.dim, self.blue, self.fade_in = dim, blue, fade_in
        self.breath = 0.0               # 0 呼气 ~ 1 吸气
        self.breath_target = 0.0
        self.glow_sources: list = []
        self.glow_fn = None             # 可选：返回本帧特效画到的格子（梦话气泡、星星、口水也保持亮）
        self.fade_from = None

    def set_breath(self, inhale: bool) -> None:
        self.breath_target = 1.0 if inhale else 0.0

    def stop(self, fade: float = 0.8) -> None:
        self.fade_from = (self.t, fade, self._level())
        self.duration = self.t + fade

    def _level(self) -> float:
        if self.fade_from is not None:
            t0, fade, lv = self.fade_from
            return lv * max(0.0, 1 - (self.t - t0) / fade)
        return min(1.0, self.t / self.fade_in)

    def update(self, dt):
        super().update(dt)
        self.breath += (self.breath_target - self.breath) * min(1.0, dt * 1.2)

    def post(self, chars, colors, w, h):
        lv = self._level()
        if lv < 0.01:
            return
        skip = set()
        if self.glow_fn is not None:
            for x, y in self.glow_fn():
                if 0 <= x < w and 0 <= y < h:
                    skip.add(y * w + x)
        for src in self.glow_sources:
            for x, y in getattr(src, "glow", ()):
                if 0 <= x < w and 0 <= y < h:
                    skip.add(y * w + x)
        dark = 1.0 - lv * (self.dim - 0.06 * self.breath)       # 吸气时亮一点点
        blue = lv * self.blue
        # 只做乘法（压暗 + 红绿压得多、蓝压得少 → 偏蓝的夜色），暗处不会被抬亮发灰
        kr, kg, kb = dark * (1 - 0.55 * blue), dark * (1 - 0.3 * blue), min(1.0, dark * (1 + 0.1 * blue))
        for i, ch in enumerate(chars):
            if ch == CLEAR or i in skip:
                continue
            c = colors[i]
            r, g, b = int(((c >> 16) & 255) * kr), int(((c >> 8) & 255) * kg), int((c & 255) * kb)
            colors[i] = (c & 0xFF000000) | (r << 16) | (g << 8) | b
