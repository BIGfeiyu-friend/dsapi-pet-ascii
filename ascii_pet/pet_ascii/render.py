"""Qt 字符渲染器：把合成好的字符网格画到窗口上。

- 每个字符单独绘制在自己的格子里，因此字体宽度、全角/半角都不会造成错位
- 字形图片按 (字符, 颜色) 缓存（颜色量化），带可选外发光
- 只重绘与上一帧不同的格子；整体漂浮/抖动通过像素偏移实现，不重绘
- 可选深色“衬底”：只在有字符的格子下面铺半透明暗色，在任何桌面上都能看清（参考图的 CRT 质感）
"""
from __future__ import annotations

import os
from collections import OrderedDict

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .grid import CLEAR, WIDE_CONT, char_width

FONT_FILES = ("C:/Windows/Fonts/OCRAEXT.TTF",)
FONT_FALLBACKS = ("OCR A Extended", "OCR A", "Consolas", "Cascadia Mono", "Courier New")
WIDE_FONT_FILES = ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc")
SYMBOL_FONT_FILES = ("C:/Windows/Fonts/seguisym.ttf", "C:/Windows/Fonts/consola.ttf")
_loaded_family: str | None = None
_wide_family: str | None = None


def _family_from_files(files, fallback: str) -> str:
    for path in files:
        if os.path.exists(path):
            fid = QFontDatabase.addApplicationFont(path)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                return fams[0]
    return fallback


_symbol_family: str | None = None


def symbol_font_family() -> str:
    """非 ASCII 单宽符号（★♥✦░▒ 等）用 Segoe UI Symbol 绘制，避免 OCR-A 缺字显示成方框。"""
    global _symbol_family
    if _symbol_family is None:
        _symbol_family = _family_from_files(SYMBOL_FONT_FILES, "Segoe UI Symbol")
    return _symbol_family


def wide_font_family() -> str:
    """中文（宽字符）字体：直接从文件加载，离屏/无系统字体库时也能正确显示。"""
    global _wide_family
    if _wide_family is None:
        fam = None
        for path in WIDE_FONT_FILES:
            if os.path.exists(path):
                fid = QFontDatabase.addApplicationFont(path)
                fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
                if fams:
                    fam = fams[0]
                    break
        _wide_family = fam or "Microsoft YaHei UI"
    return _wide_family


def ui_font_family() -> str:
    global _loaded_family
    if _loaded_family is None:
        fam = None
        for path in FONT_FILES:
            if os.path.exists(path):
                fid = QFontDatabase.addApplicationFont(path)
                fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
                if fams:
                    fam = fams[0]
                    break
        if fam is None:
            installed = set(QFontDatabase.families())
            fam = next((f for f in FONT_FALLBACKS if f in installed), "Monospace")
        _loaded_family = fam
    return _loaded_family


class GlyphCache:
    def __init__(self, cell_h: int, dpr: float = 1.0, glow: float = 0.35, family: str | None = None):
        self.cell_h = max(6, int(cell_h))
        self.cell_w = max(3, int(round(self.cell_h * 0.6)))
        self.dpr = max(1.0, float(dpr))
        self.glow = max(0.0, min(1.0, glow))
        self.font = QFont(family or ui_font_family())
        self.font.setPixelSize(max(4, int(round(self.cell_h * 0.95))))
        self.font.setStyleStrategy(QFont.PreferAntialias)
        self.wide_font = QFont(wide_font_family())
        self.symbol_font = QFont(symbol_font_family())
        self.symbol_font.setPixelSize(max(4, int(round(self.cell_h * 0.8))))
        self.wide_font.setPixelSize(max(4, int(round(self.cell_h * 0.9))))
        self._cache: OrderedDict = OrderedDict()
        self.max_items = 24000

    @staticmethod
    def quantize(argb: int) -> int:
        # 每通道 5 bit，透明度 4 bit：既限制缓存规模，又看不出色阶
        a = (argb >> 24) & 0xF0
        return (a << 24) | (argb & 0xF8F8F8)

    def glyph(self, ch: str, argb: int) -> QImage:
        key = (ch, self.quantize(argb))
        img = self._cache.get(key)
        if img is not None:
            self._cache.move_to_end(key)
            return img
        wide = char_width(ch) == 2
        w = self.cell_w * (2 if wide else 1)
        h = self.cell_h
        img = QImage(int(w * self.dpr), int(h * self.dpr), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(self.dpr)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(self.wide_font if wide else (self.symbol_font if ord(ch) > 0x7E else self.font))
        a = ((key[1] >> 24) & 0xFF) | 0x0F
        col = QColor((key[1] >> 16) & 0xFF, (key[1] >> 8) & 0xFF, key[1] & 0xFF, a)
        rect = QRectF(0, 0, w, h)
        if self.glow > 0 and a > 40:
            halo = QColor(col)
            halo.setAlpha(int(a * 0.28 * self.glow))
            p.setPen(QPen(halo))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                p.drawText(rect.translated(dx, dy), Qt.AlignCenter, ch)
        p.setPen(QPen(col))
        p.drawText(rect, Qt.AlignCenter, ch)
        p.end()
        self._cache[key] = img
        if len(self._cache) > self.max_items:
            self._cache.popitem(last=False)
        return img


class AsciiView(QWidget):
    """显示字符网格的控件。透明背景时可直接作为桌宠窗口的内容。"""

    def __init__(self, parent=None, cell_h: int = 12, glow: float = 0.35, backplate: int = 0x8C05080F,
                 background: int | None = None, pad: int = 6, scanlines: float = 0.0,
                 plate_mode: str = "tint", tint: float = 0.30):
        super().__init__(parent)
        self.cell_h = cell_h
        self.glow = glow
        self.backplate = backplate
        # 衬底：none 不画；solid 统一暗色；tint 用字符自身颜色压暗（形成参考图那种彩色字符块）
        self.plate_mode = plate_mode
        self.tint = tint
        self.background = background
        self.pad = pad
        self.scanlines = scanlines
        self.cols = 0
        self.rows = 0
        self.offset = QPointF(0, 0)
        self._img: QImage | None = None
        self._prev_chars: list = []
        self._prev_colors: list = []
        self._cache = GlyphCache(cell_h, self.devicePixelRatioF(), glow)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    # ------------------------------------------------------------------ 尺寸
    @property
    def cell_w(self) -> int:
        return self._cache.cell_w

    def grid_pixel_size(self) -> QSize:
        return QSize(self.cols * self._cache.cell_w, self.rows * self._cache.cell_h)

    def sizeHint(self) -> QSize:
        s = self.grid_pixel_size()
        return QSize(s.width() + self.pad * 2, s.height() + self.pad * 2)

    def set_cell_height(self, cell_h: int) -> None:
        cell_h = max(6, min(48, int(cell_h)))
        if cell_h == self.cell_h and self._cache.dpr == self.devicePixelRatioF():
            return
        self.cell_h = cell_h
        self._cache = GlyphCache(cell_h, self.devicePixelRatioF(), self.glow)
        self._img = None
        self.updateGeometry()

    def set_style(self, glow: float | None = None, backplate: int | None = None, scanlines: float | None = None,
                  plate_mode: str | None = None, tint: float | None = None) -> None:
        if glow is not None and glow != self.glow:
            self.glow = glow
            self._cache = GlyphCache(self.cell_h, self.devicePixelRatioF(), glow)
        if backplate is not None:
            self.backplate = backplate
        if plate_mode is not None:
            self.plate_mode = plate_mode
        if tint is not None:
            self.tint = tint
        if scanlines is not None:
            self.scanlines = scanlines
        self._img = None

    def cell_at(self, pos) -> tuple[int, int]:
        x = int((pos.x() - self.pad - self.offset.x()) // self._cache.cell_w)
        y = int((pos.y() - self.pad - self.offset.y()) // self._cache.cell_h)
        return x, y

    # ------------------------------------------------------------------ 绘制
    def set_frame(self, chars: list, colors: list, cols: int, rows: int, offset: tuple[float, float] = (0, 0)) -> None:
        dpr = self.devicePixelRatioF()
        if dpr != self._cache.dpr:
            self._cache = GlyphCache(self.cell_h, dpr, self.glow)
            self._img = None
        resized = cols != self.cols or rows != self.rows
        if resized:
            self.cols, self.rows = cols, rows
            self._img = None
            self.updateGeometry()
        cw, chh = self._cache.cell_w, self._cache.cell_h
        if self._img is None:
            self._img = QImage(int(cols * cw * dpr), int(rows * chh * dpr), QImage.Format_ARGB32_Premultiplied)
            self._img.setDevicePixelRatio(dpr)
            self._img.fill(Qt.transparent)
            self._prev_chars = [CLEAR] * (cols * rows)
            self._prev_colors = [0] * (cols * rows)
            force = True
        else:
            force = False
        prev_c, prev_k = self._prev_chars, self._prev_colors
        changed = []
        n = cols * rows
        for i in range(n):
            if force or chars[i] != prev_c[i] or colors[i] != prev_k[i]:
                changed.append(i)
        if changed:
            p = QPainter(self._img)
            mode = self.plate_mode
            solid = QColor.fromRgba(self.backplate) if (mode == "solid" and self.backplate) else None
            base = QColor.fromRgba(self.backplate) if self.backplate else QColor(0, 0, 0, 160)
            ba, br, bg_, bb = base.alpha(), base.red(), base.green(), base.blue()
            k = self.tint
            for i in changed:
                y, x = divmod(i, cols)
                ch = chars[i]
                rect = QRect(x * cw, y * chh, cw, chh)
                p.setCompositionMode(QPainter.CompositionMode_Source)
                if ch == CLEAR or ch is None:
                    p.fillRect(rect, Qt.transparent)
                    continue
                if mode == "tint":
                    c = colors[i]
                    ca = ((c >> 24) & 0xFF) / 255.0
                    plate = QColor(int(br + (((c >> 16) & 0xFF) - br) * k), int(bg_ + (((c >> 8) & 0xFF) - bg_) * k),
                                   int(bb + ((c & 0xFF) - bb) * k), int(min(255, ba + 40) * ca))
                else:
                    plate = solid
                p.fillRect(rect, plate if plate is not None else Qt.transparent)
                if ch == WIDE_CONT or ch == " ":
                    continue
                p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                if char_width(ch) == 2:
                    if x + 1 < cols and chars[i + 1] == WIDE_CONT:
                        if plate is not None:
                            p.setCompositionMode(QPainter.CompositionMode_Source)
                            p.fillRect(QRect((x + 1) * cw, y * chh, cw, chh), plate)
                            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
                        p.drawImage(x * cw, y * chh, self._cache.glyph(ch, colors[i]))
                    continue
                p.drawImage(x * cw, y * chh, self._cache.glyph(ch, colors[i]))
            p.end()
        self._prev_chars = list(chars)
        self._prev_colors = list(colors)
        if resized or force:
            self.setMinimumSize(self.sizeHint())  # 不允许布局把画面挤小（会裁掉字符）
        new_off = QPointF(*offset)
        if changed or new_off != self.offset or resized:
            self.offset = new_off
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        if self.background is not None:
            p.fillRect(self.rect(), QColor.fromRgba(self.background))
        if self._img is not None:
            p.drawImage(QPointF(self.pad + self.offset.x(), self.pad + self.offset.y()), self._img)
            if self.scanlines > 0:
                pen = QPen(QColor(0, 0, 0, int(255 * self.scanlines)))
                pen.setWidth(1)
                p.setPen(pen)
                top = int(self.pad + self.offset.y())
                gs = self.grid_pixel_size()
                left = int(self.pad + self.offset.x())
                for yy in range(top, top + gs.height(), 3):
                    p.drawLine(left, yy, left + gs.width(), yy)
        p.end()

    def snapshot(self) -> QImage:
        img = QImage(self.size() * self.devicePixelRatioF(), QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(self.devicePixelRatioF())
        img.fill(Qt.transparent)
        self.render(img)
        return img
