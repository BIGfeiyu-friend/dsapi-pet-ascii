"""字符网格与图层合成。

坐标约定（全系统统一）：
  原点 (0, 0) 在目标区域左上角；x 为列，向右增大；y 为行，向下增大；一个字符占一个单元格。
  超出目标边界的内容一律裁剪丢弃，不会报错，也不会写到别的区域。

单元格内容：
  None        透明：不修改下层（补丁中的 "`"，以及默认情况下的空格）
  CLEAR ("")  清空：主动把该格变成空白，遮住所有下层
  其它字符     一个可见字符；宽字符（中日韩）占两格，第二格为 WIDE_CONT

颜色：0xAARRGGBB 整数。图层按 z 从小到大叠加，上层的非透明格完全取代下层同一格（一格只画一个字）。
"""
from __future__ import annotations

import unicodedata

CLEAR = ""
WIDE_CONT = "\x01"
TRANSPARENT_MARK = "`"
DEFAULT_COLOR = 0xFFD9DEE8

# 允许出现在 AI 生成内容中的非 ASCII 单宽符号（字体普遍支持，且不会撑破格子）
EXTRA_SYMBOLS = set("░▒▓█▀▄▌▐■□▪▫●○◆◇★☆♥♡♪♫✦✧·•°∙…※¤∴∵≈≡±×÷←↑→↓∞")


def char_width(ch: str) -> int:
    if not ch:
        return 0
    o = ord(ch)
    if o < 0x20 or o == 0x7F:
        return 0
    if o < 0x7F:
        return 1
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def sanitize_char(ch: str, allow_wide: bool = False) -> str:
    """把模型给出的单个字符规整为可安全渲染的字符（宽字符默认替换为 ?）。"""
    if not ch:
        return " "
    o = ord(ch)
    if 0x20 <= o < 0x7F or ch in EXTRA_SYMBOLS:
        return ch
    w = char_width(ch)
    if w == 0 or not ch.isprintable():
        return " "
    if w == 2 and not allow_wide:
        return "?"
    return ch


def parse_color(value, default: int | None = None) -> int | None:
    """'#rgb' / '#rrggbb' / '#aarrggbb' / 0xRRGGBB → 0xAARRGGBB。"""
    if value is None:
        return default
    if isinstance(value, int):
        return value if value > 0xFFFFFF else (0xFF000000 | value)
    s = str(value).strip().lstrip("#")
    try:
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) == 6:
            return 0xFF000000 | int(s, 16)
        if len(s) == 8:
            return int(s, 16)
    except ValueError:
        pass
    return default


def color_hex(argb: int) -> str:
    return "#%06x" % (argb & 0xFFFFFF)


def with_alpha(argb: int, a: float) -> int:
    base = (argb >> 24) & 0xFF
    na = max(0, min(255, int(base * a)))
    return (na << 24) | (argb & 0xFFFFFF)


def mix(c1: int, c2: int, t: float) -> int:
    t = max(0.0, min(1.0, t))
    out = 0
    for shift in (24, 16, 8, 0):
        a = (c1 >> shift) & 0xFF
        b = (c2 >> shift) & 0xFF
        out |= int(a + (b - a) * t) << shift
    return out


class Layer:
    """一个矩形字符图层。chars/colors 为行优先的扁平列表。"""

    __slots__ = ("name", "w", "h", "chars", "colors", "x", "y", "z", "visible", "opacity")

    def __init__(self, name: str, w: int, h: int, z: int = 0):
        self.name = name
        self.w = max(0, int(w))
        self.h = max(0, int(h))
        self.chars: list = [None] * (self.w * self.h)
        self.colors: list = [DEFAULT_COLOR] * (self.w * self.h)
        self.x = 0
        self.y = 0
        self.z = z
        self.visible = True
        self.opacity = 1.0

    # ------------------------------------------------------------------ 基础读写
    def clear(self) -> None:
        self.chars = [None] * (self.w * self.h)

    def fill(self, ch, color: int = DEFAULT_COLOR) -> None:
        self.chars = [ch] * (self.w * self.h)
        self.colors = [color] * (self.w * self.h)

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def get(self, x: int, y: int):
        if not self.inside(x, y):
            return None, DEFAULT_COLOR
        i = y * self.w + x
        return self.chars[i], self.colors[i]

    def set(self, x: int, y: int, ch, color: int | None = None) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = y * self.w + x
            self.chars[i] = ch
            if color is not None:
                self.colors[i] = color

    def put_text(self, x: int, y: int, text: str, color: int = DEFAULT_COLOR, allow_wide: bool = True) -> int:
        """写一行文字（宽字符占两格）。返回写完后的 x。"""
        for ch in text:
            ch = sanitize_char(ch, allow_wide=allow_wide)
            w = char_width(ch)
            if w == 2:
                if x + 1 < self.w and x >= 0:
                    self.set(x, y, ch, color)
                    self.set(x + 1, y, WIDE_CONT, color)
                x += 2
            else:
                self.set(x, y, ch, color)
                x += 1
        return x

    def copy(self, name: str | None = None) -> "Layer":
        out = Layer(name or self.name, self.w, self.h, self.z)
        out.chars = list(self.chars)
        out.colors = list(self.colors)
        out.x, out.y, out.visible, out.opacity = self.x, self.y, self.visible, self.opacity
        return out

    def blit(self, src: "Layer", x: int, y: int) -> None:
        """把 src 的非透明格复制到本图层 (x, y) 处（裁剪越界部分）。"""
        for sy in range(src.h):
            ty = y + sy
            if not 0 <= ty < self.h:
                continue
            for sx in range(src.w):
                ch = src.chars[sy * src.w + sx]
                if ch is None:
                    continue
                tx = x + sx
                if 0 <= tx < self.w:
                    i = ty * self.w + tx
                    self.chars[i] = ch
                    self.colors[i] = src.colors[sy * src.w + sx]

    def apply_patch(self, x: int, y: int, rows: list[str], colors: list | None = None,
                    blank: str = "transparent", mask: list[str] | None = None,
                    default_color: int | None = None) -> int:
        """把补丁行写入本图层。返回实际修改的格数。

        rows    : 字符串列表；"`" 永远表示透明（不修改下层）
        blank   : 空格的含义，"transparent"（默认）或 "clear"（清空成空白）
        mask    : 可选，与 rows 同尺寸；'.' 表示该格保持不变，其它字符表示写入（此时空格按清空处理）
        colors  : 可选，与 rows 同尺寸的颜色矩阵（每格为 0xAARRGGBB 或 None 表示用 default_color）
        """
        changed = 0
        for ry, row in enumerate(rows):
            ty = y + ry
            if not 0 <= ty < self.h:
                continue
            mrow = mask[ry] if mask and ry < len(mask) else None
            crow = colors[ry] if colors and ry < len(colors) else None
            for rx, ch in enumerate(row):
                tx = x + rx
                if not 0 <= tx < self.w:
                    continue
                if mrow is not None:
                    m = mrow[rx] if rx < len(mrow) else "."
                    if m == ".":
                        continue
                    cell = CLEAR if ch in (" ", TRANSPARENT_MARK) else sanitize_char(ch)
                else:
                    if ch == TRANSPARENT_MARK:
                        continue
                    if ch == " ":
                        if blank != "clear":
                            continue
                        cell = CLEAR
                    else:
                        cell = sanitize_char(ch)
                        if cell == " ":
                            continue
                i = ty * self.w + tx
                self.chars[i] = cell
                col = crow[rx] if crow is not None and rx < len(crow) else None
                if col is not None:
                    self.colors[i] = col
                elif default_color is not None:
                    self.colors[i] = default_color
                changed += 1
        return changed

    def rows(self, transparent: str = TRANSPARENT_MARK, clear: str = " ") -> list[str]:
        out = []
        for y in range(self.h):
            line = []
            for ch in self.chars[y * self.w:(y + 1) * self.w]:
                if ch is None:
                    line.append(transparent)
                elif ch == CLEAR:
                    line.append(clear)
                elif ch == WIDE_CONT:
                    continue
                else:
                    line.append(ch)
            out.append("".join(line))
        return out

    def occupied(self) -> int:
        return sum(1 for c in self.chars if c is not None and c != CLEAR)


def compose(layers, w: int, h: int):
    """按 z 叠加可见图层，返回 (chars, colors)。空白格的字符为 CLEAR。"""
    chars = [CLEAR] * (w * h)
    colors = [0] * (w * h)
    for L in sorted((l for l in layers if l.visible and l.opacity > 0.0), key=lambda l: l.z):
        ox, oy = int(L.x), int(L.y)
        y0, y1 = max(0, -oy), min(L.h, h - oy)
        x0, x1 = max(0, -ox), min(L.w, w - ox)
        if y0 >= y1 or x0 >= x1:
            continue
        lc, lcol, lw = L.chars, L.colors, L.w
        fade = L.opacity < 0.999
        for ly in range(y0, y1):
            src = ly * lw
            dst = (oy + ly) * w + ox
            for lx in range(x0, x1):
                ch = lc[src + lx]
                if ch is None:
                    continue
                chars[dst + lx] = ch
                colors[dst + lx] = with_alpha(lcol[src + lx], L.opacity) if fade else lcol[src + lx]
    return chars, colors
