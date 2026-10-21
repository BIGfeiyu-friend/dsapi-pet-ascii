"""房间里的她“看”图片（v1.2）。

她用的模型（DeepSeek 官方 API）看不了图片：实测图片会被直接丢掉，开思考时她说“看不到”，不开思考时会编一张不存在的画面。
所以换一种“眼睛”——把图片变成她能读的文字，也正好是她自己的样子（字符画）：
  - 字符画：灰度图缩成几十列的字符（自动拉伸对比度），细节会丢，但构图、明暗、大致形状还在
  - 颜色概况：主要颜色和占比、整体明暗；再加一张“颜色地图”（每个字是那一块的主要颜色）
  - 图里的文字：Windows 自带的文字识别（Windows.Media.Ocr，离线、不联网、不花 token），可能有错字
结果按图片缓存（room/seen.json），她自己看完写下的一句“我觉得这是……”也存在这里。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
RAMP = "@%#*+=-:. "                 # 由暗到亮
MAP_CHARS = "黑白灰红橙黄绿青蓝紫粉棕"
OCR_SCRIPT = Path(__file__).with_name("ocr_win.ps1")
OCR_MAX_SIDE = 2000


def is_image(path) -> bool:
    return Path(str(path)).suffix.lower() in IMG_EXT


def image_size(path) -> tuple[int, int]:
    from PySide6.QtGui import QImageReader

    s = QImageReader(str(path)).size()
    return (s.width(), s.height()) if s.isValid() else (0, 0)


def load(path):
    from PySide6.QtGui import QImage, QImageReader

    r = QImageReader(str(path))
    r.setAutoTransform(True)                        # 照片的 EXIF 方向
    img = r.read()                                  # GIF 只取第一帧
    if img.isNull():
        raise ValueError(f"打不开这张图片（{r.errorString()}）")
    return img.convertToFormat(QImage.Format.Format_RGB32)


# ---------------------------------------------------------------------- 颜色
def color_name(r: int, g: int, b: int) -> str:
    """RGB → 中文颜色名（可能带“深 / 浅”）。名字的最后一个字是颜色地图里用的字。"""
    from PySide6.QtGui import QColor

    h, s, v, _ = QColor(r, g, b).getHsvF()
    if v < 0.16:
        return "黑"
    if s < 0.16:
        if v > 0.86:
            return "白"
        return "深灰" if v < 0.42 else ("浅灰" if v > 0.7 else "灰")
    deg = (h if h >= 0 else 0) * 360
    if deg < 15 or deg >= 345:
        base = "粉" if (s < 0.45 and v > 0.75) else ("棕" if v < 0.45 else "红")
    elif deg < 42:
        base = "棕" if v < 0.6 else ("肤" if s < 0.4 else "橙")
    elif deg < 70:
        base = "棕" if v < 0.45 else "黄"
    elif deg < 165:
        base = "绿"
    elif deg < 195:
        base = "青"
    elif deg < 255:
        base = "蓝"
    elif deg < 290:
        base = "紫"
    else:
        base = "粉" if v > 0.6 else "紫"
    if base == "肤":
        return "浅橙"
    if base in ("棕",):
        return base
    if v < 0.4:
        return "深" + base
    if s < 0.38 and v > 0.8:
        return "浅" + base
    return base


def _scaled(img, cols: int, rows: int):
    from PySide6.QtCore import Qt

    return img.scaled(max(1, cols), max(1, rows), Qt.AspectRatioMode.IgnoreAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)


def ascii_art(img, cols: int = 64, max_rows: int = 34) -> str:
    w, h = img.width(), img.height()
    rows = max(1, round(cols * h / max(1, w) * 0.5))      # 一个字大约是两倍高
    if rows > max_rows:
        rows = max_rows
        cols = max(8, round(rows * w / max(1, h) * 2))
    small = _scaled(img, cols, rows)
    lum = [[small.pixelColor(x, y).lightnessF() for x in range(cols)] for y in range(rows)]
    flat = sorted(v for row in lum for v in row)
    lo = flat[int(len(flat) * 0.02)]
    hi = flat[min(len(flat) - 1, int(len(flat) * 0.98))]
    span = max(0.08, hi - lo)                              # 自动拉伸对比度（几乎纯色的图不硬拉）
    n = len(RAMP) - 1
    lines = []
    for row in lum:
        lines.append("".join(RAMP[min(n, max(0, round((min(1.0, max(0.0, (v - lo) / span))) * n)))] for v in row).rstrip())
    return "\n".join(lines)


def color_overview(img) -> dict:
    w, h = img.width(), img.height()
    cols = 48
    rows = max(1, round(cols * h / max(1, w)))
    if rows > 48:
        rows, cols = 48, max(1, round(48 * w / max(1, h)))
    small = _scaled(img, cols, rows)
    counts: dict = {}
    names = []
    light = 0.0
    for y in range(rows):
        row = []
        for x in range(cols):
            c = small.pixelColor(x, y)
            name = color_name(c.red(), c.green(), c.blue())
            counts[name] = counts.get(name, 0) + 1
            row.append(name[-1])
            light += c.lightnessF()
        names.append(row)
    total = cols * rows
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    main = [(k, round(v * 100 / total)) for k, v in top if v * 100 / total >= 3][:7]
    # 颜色地图：每一块里最多的颜色（按画面比例，最多 12 列 8 行）
    mc = 12 if w >= h else max(4, round(12 * w / max(1, h)))
    mr = max(2, min(8, round(mc * h / max(1, w))))
    grid = []
    for gy in range(mr):
        line = []
        for gx in range(mc):
            tally: dict = {}
            for y in range(gy * rows // mr, max(gy * rows // mr + 1, (gy + 1) * rows // mr)):
                for x in range(gx * cols // mc, max(gx * cols // mc + 1, (gx + 1) * cols // mc)):
                    ch = names[min(rows - 1, y)][min(cols - 1, x)]
                    tally[ch] = tally.get(ch, 0) + 1
            line.append(max(tally.items(), key=lambda kv: kv[1])[0])
        grid.append("".join(line))
    mean = light / total
    bright = "偏暗" if mean < 0.35 else ("偏亮" if mean > 0.65 else "明暗适中")
    return {"main": main, "map": "\n".join(grid), "brightness": bright}


# ---------------------------------------------------------------------- 图里的文字（Windows 自带 OCR）
def run_ocr(img, timeout: float = 30.0) -> tuple[list, str]:
    """→ (文字行, 状态)。状态：ok / none（没认出文字）/ unavailable:原因。"""
    if sys.platform != "win32" or not OCR_SCRIPT.exists():
        return [], "unavailable:不是 Windows"
    from PySide6.QtCore import Qt

    tmpdir = tempfile.mkdtemp(prefix="room_ocr_")
    tmp = os.path.join(tmpdir, "img.png")
    try:
        im = img
        if max(im.width(), im.height()) > OCR_MAX_SIDE:
            im = im.scaled(OCR_MAX_SIDE, OCR_MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        if not im.save(tmp, "PNG"):
            return [], "unavailable:临时文件写不了"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(OCR_SCRIPT), tmp], capture_output=True, timeout=timeout, creationflags=flags)
        out = r.stdout.decode("utf-8", "replace").strip().splitlines()
        data = json.loads(out[-1]) if out else {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return [], f"unavailable:{str(exc)[:80]}"
    finally:
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except OSError:
            pass
    if data.get("error"):
        return [], "unavailable:" + str(data["error"])[:80]
    lines = data.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    lines = [tidy_ocr(x) for x in lines if str(x).strip()][:30]
    return lines, ("ok" if lines else "none")


def tidy_ocr(line: str) -> str:
    """Windows OCR 认中文时字与字之间带空格：去掉中文之间的空格。"""
    s = re.sub(r"(?<=[　-鿿＀-￯])\s+(?=[　-鿿＀-￯])", "", str(line).strip())
    return s[:200]


# ---------------------------------------------------------------------- 能看图的副模型（flash）
VISION_SYSTEM = """你在帮一个看不见图片的朋友看图。她只能读文字，所以请你替她看，客观、具体地描述这张图：
画面里有什么（主体、数量、大概位置）、颜色和光线、是照片 / 插画 / 截图 / 文字图还是别的、图里的文字（原文照抄）、
整体给人的感觉。不要猜拍图的人想表达什么；看不清的地方就说看不清。150~250 字，只输出描述本身。"""
VISION_MAX_SIDE = 1024


def image_data_url(img, max_side: int = VISION_MAX_SIDE) -> str:
    import base64

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt

    im = img
    if max(im.width(), im.height()) > max_side:
        im = im.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    im.save(buf, "JPEG", 88)
    return "data:image/jpeg;base64," + base64.b64encode(bytes(ba)).decode("ascii")


def vision_describe(img, provider: dict, timeout: float = 60.0) -> dict:
    """让能看图的模型（provider：{base_url, api_key, model}）描述这张图。→ {"text", "tokens", "model"}；失败抛异常。"""
    from .ai.client import OpenAICompatClient

    c = OpenAICompatClient(provider["base_url"], provider["api_key"], provider["model"], json_mode=False,
                           disable_thinking=True)
    msgs = [{"role": "system", "content": VISION_SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": "请描述这张图。"},
                                         {"type": "image_url", "image_url": {"url": image_data_url(img)}}]}]
    out = c.complete(msgs, max_tokens=600, temperature=0.3, timeout=timeout)
    u = out.get("usage") or {}
    text = " ".join(str(out.get("text") or "").split())[:600]
    if not text:
        raise ValueError("没有给出描述")
    return {"text": text, "tokens": int(u.get("total_tokens") or 0), "model": out.get("model") or provider["model"],
            "prompt_tokens": int(u.get("prompt_tokens") or 0)}


# ---------------------------------------------------------------------- 组合 + 缓存
def describe(path, cols: int = 64, ocr: bool = True, vision=None) -> dict:
    """vision：可选的 callable(QImage) → {"text","tokens","model"}（能看图的副模型）；None = 不请它看。"""
    img = load(path)
    w, h = img.width(), img.height()
    shape = "横图" if w > h * 1.15 else ("竖图" if h > w * 1.15 else "差不多方的图")
    out = {"size": [w, h], "shape": shape, "ascii": ascii_art(img, cols), **color_overview(img)}
    if ocr:
        out["text"], out["ocr"] = run_ocr(img)
    else:
        out["text"], out["ocr"] = [], "unavailable:关掉了"
    add_vision(out, img, vision)
    return out


def add_vision(out: dict, img, vision) -> None:
    if vision is None:
        return
    try:
        v = vision(img)
        out["vision"], out["vision_model"] = v["text"], v.get("model", "")
        out["vision_tokens_new"] = int(v.get("tokens") or 0)      # 这次新花的（缓存时不再计）
    except Exception as exc:
        out["vision_error"] = str(exc)[:120]


def render(desc: dict) -> str:
    """给她看的文字版图片。"""
    w, h = desc["size"]
    main = "、".join(f"{k} {p}%" for k, p in desc.get("main", []))
    parts = [f"尺寸：{w}×{h}（{desc['shape']}），整体{desc['brightness']}",
             "字符画（越密的字越暗）：", "```", desc["ascii"], "```",
             f"主要颜色：{main or '说不清'}",
             "颜色地图（每个字是那一块的主要颜色）：", "```", desc["map"], "```"]
    if desc.get("vision"):
        parts.append("另外，能看图的副模型替你真的看了一眼原图，它是这么描述的（这是它的话，不是你自己看到的）：\n"
                     + desc["vision"])
    elif desc.get("vision_error"):
        parts.append("（能看图的副模型这次没看成）")
    st = desc.get("ocr", "")
    if desc.get("text"):
        parts.append("图里的文字（自动识别，可能有错字）：\n" + "\n".join(f"  {t}" for t in desc["text"]))
    elif st == "none":
        parts.append("图里的文字：没认出文字")
    else:
        parts.append("图里的文字：这次没法识别")
    return "\n".join(parts)


def file_key(path) -> str:
    p = Path(path)
    st = p.stat()
    return hashlib.sha1(f"{p.name}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()[:16]


class Looker:
    """看过的图片：描述缓存（同一张图不重复识别）和她自己写的一句“我觉得这是……”。只由桌宠进程写。"""

    def __init__(self, path: Path, keep: int = 300):
        self.path = Path(path)
        self.keep = keep
        self.lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        if len(self.data) > self.keep:
            for k in sorted(self.data, key=lambda k: self.data[k].get("ts", 0))[:len(self.data) - self.keep]:
                del self.data[k]
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def look(self, path, rel: str, cols: int = 64, ocr: bool = True, vision=None) -> dict:
        """同一张图不重复识别；副模型的描述也只请一次（上次没看成、或以前没请它看过的，这次补上）。"""
        key = file_key(path)
        with self.lock:
            hit = self.data.get(key)
        if hit and hit.get("desc") and hit.get("cols") == cols:
            desc = dict(hit["desc"])
            desc.pop("vision_tokens_new", None)
            if vision is not None and not desc.get("vision"):
                desc.pop("vision_error", None)
                add_vision(desc, load(path), vision)
        else:
            desc = describe(path, cols, ocr, vision)
        new_tokens = int(desc.pop("vision_tokens_new", 0) or 0)
        with self.lock:
            entry = self.data.setdefault(key, {})
            entry.update(desc={k: v for k, v in desc.items() if k != "vision_error"}, cols=cols, path=rel,
                         ts=round(time.time(), 1))
            entry["looks"] = int(entry.get("looks", 0)) + 1
            self._save()
        if new_tokens:
            desc["vision_tokens_new"] = new_tokens
        return desc

    def note_saw(self, path, rel: str, saw: str) -> None:
        try:
            key = file_key(path)
        except OSError:
            return
        with self.lock:
            entry = self.data.setdefault(key, {"path": rel, "ts": round(time.time(), 1)})
            entry["saw"] = saw
            entry["path"] = rel
            self._save()

    def saw_map(self, room) -> dict:
        """房间里现在的图片 → 她看过时写的那句话（按文件内容认，改名、搬动后也认得）。"""
        out = {}
        with self.lock:
            by_key = {k: v.get("saw") for k, v in self.data.items() if v.get("saw")}
        if not by_key:
            return out
        for p in room.walk():
            if p.is_file() and is_image(p):
                try:
                    s = by_key.get(file_key(p))
                except OSError:
                    continue
                if s:
                    out[room.rel(p)] = s
        return out
