"""把角色视频/图片转换成彩色字符画角色素材（离线工具，只依赖 Pillow + ffmpeg）。

画法沿用 MV《world.search(you)》的字符引擎：亮度 → 字符梯度 " .,:;-~=+*ox%#&@"，
每格取原图平均色并提高饱和度/明度（深色衬底上更清楚）。

对每个姿势额外生成：
  face   面部可编辑区域（自动检测皮肤区域，可手动覆盖）
  plate  去掉五官后的“脸底”（只有皮肤），放在身体层里
  neutral 原画的五官，作为默认面部补丁（透明格用 "`"）
这样 AI 生成的新五官画在脸底上，不会和原画五官叠在一起，头部轮廓也保持不变。
"""
from __future__ import annotations

import colorsys
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from statistics import median

RAMP = " .,:;-~=+*ox%#&@"
CELL_ASPECT = 1.0 / 0.6  # 单元格高宽比（与渲染器一致）
MARK = (255, 0, 255)


def find_ffmpeg() -> str | None:
    env = os.environ.get("PET_ASCII_FFMPEG")
    if env and os.path.exists(env):
        return env
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.join(here, "..", "..", "_internal"), getattr(sys, "_MEIPASS", "")):
        hits = glob.glob(os.path.join(base, "imageio_ffmpeg", "binaries", "ffmpeg*"))
        if hits:
            return hits[0]
    return shutil.which("ffmpeg")


def load_frame(source: str, index: int = 0):
    """返回 RGBA 图像。视频用 libvpx 解码以保留 VP9 透明通道。"""
    from PIL import Image

    if source.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        return Image.open(source).convert("RGBA")
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("找不到 ffmpeg（可设置环境变量 PET_ASCII_FFMPEG）")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "f.png")
        decoder = ["-c:v", "libvpx-vp9"] if source.lower().endswith(".webm") else []
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", *decoder, "-i", source,
               "-vf", "select=eq(n\\,%d)" % int(index), "-vsync", "0", "-frames:v", "1", out]
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return Image.open(out).convert("RGBA").copy()


def foreground_mask(im, thresh: int = 48):
    """前景蒙版：有透明通道就直接用；否则从画面边缘泛洪抠掉白底
    （角色内部的白色如围裙、花边被轮廓包住，会保留下来）。"""
    from PIL import Image, ImageChops, ImageDraw

    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        if lo < 250:
            # 视频编码会在“透明”处留下 1~20 的残余 alpha，清零后外接框才准确
            return alpha.point(lambda v: 0 if v < 32 else v)
    work = im.convert("RGB")
    w, h = work.size
    seeds = [(x, y) for x in range(0, w, 6) for y in (0, h - 1)] + [(x, y) for y in range(0, h, 6) for x in (0, w - 1)]
    for s in seeds:
        px = work.getpixel(s)
        if px != MARK and min(px) > 215:
            ImageDraw.floodfill(work, s, MARK, thresh=thresh)
    diff = ImageChops.difference(work, Image.new("RGB", work.size, MARK)).convert("L")
    return diff.point(lambda v: 255 if v > 10 else 0)


def _boost(r, g, b, v_floor=0.45, s_mul=1.3):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    s = min(1.0, s * s_mul)
    v = min(1.0, v_floor + v * (1 - v_floor) * 1.15)
    rr, gg, bb = colorsys.hsv_to_rgb(h, s, v)
    return int(rr * 255), int(gg * 255), int(bb * 255)


def _is_skin(r, g, b) -> bool:
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return v > 0.78 and 0.05 < s < 0.42 and (h < 0.11 or h > 0.96) and r > g > b - 8


def _components(cells: set, connectivity=4):
    seen, comps = set(), []
    for c in cells:
        if c in seen:
            continue
        stack, comp = [c], []
        seen.add(c)
        while stack:
            x, y = stack.pop()
            comp.append((x, y))
            nbrs = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if connectivity == 8:
                nbrs += ((x + 1, y + 1), (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1))
            for n in nbrs:
                if n in cells and n not in seen:
                    seen.add(n)
                    stack.append(n)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def to_cells(im, mask, box, cols: int, rows: int, gamma: float = 0.9, alpha_min: int = 110):
    """裁剪 + 降采样，返回 raw[(r,g,b,a)] 网格。"""
    rgba = im.convert("RGB").crop(box).convert("RGBA")
    rgba.putalpha(mask.crop(box))
    from PIL import Image

    small = rgba.resize((cols, rows), Image.BOX)
    raw = [[small.getpixel((x, y)) for x in range(cols)] for y in range(rows)]
    return raw


def ramp_norm(raw, alpha_min: int = 110, floor: float = 0.38, v_floor: float = 0.45):
    lums = sorted((0.299 * r + 0.587 * g + 0.114 * b) / 255 for row in raw for (r, g, b, a) in row if a >= alpha_min)
    lo = lums[int(len(lums) * 0.03)] if lums else 0.0
    hi = lums[int(len(lums) * 0.97)] if lums else 1.0
    return lo, max(hi - lo, 0.08), floor, v_floor


def render_cells(raw, gamma: float = 0.9, alpha_min: int = 110, norm=None):
    """raw → (chars, colors)。norm 为 (lo, span, floor[, v_floor])，同一角色的所有渲染（身体、面部）必须共用同一个 norm。

    floor   最暗处至少使用的字符密度（0.38：暗部也是中等密度，适合透明背景的小角色；
            0.1：暗部是稀疏的“空洞”，墨线/黑眼珠才能和中间调的头发区分开，适合整幅场景图）
    v_floor 颜色提亮的下限（同理，场景图用较低的值保留墨色）
    """
    rows, cols = len(raw), len(raw[0])
    nm = tuple(norm or ramp_norm(raw, alpha_min))
    lo, span, floor = nm[:3]
    v_floor = nm[3] if len(nm) > 3 else 0.45
    chars = [[" "] * cols for _ in range(rows)]
    colors = [[None] * cols for _ in range(rows)]
    n = len(RAMP)
    for y in range(rows):
        for x in range(cols):
            r, g, b, a = raw[y][x]
            if a < alpha_min:
                continue
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            # floor：角色内的最暗处也至少用中等密度字符，深色大色块（头发）才不会变成稀疏横线
            t = floor + min(1.0, max(0.0, (lum - lo) / span)) * (1 - floor)
            idx = int(min(n - 1, max(1, round((t ** gamma) * (n - 1)))))
            chars[y][x] = RAMP[idx]
            colors[y][x] = _boost(r, g, b, v_floor=v_floor)
    return chars, colors


# ---------------------------------------------------------------------- 面部骨架（rig）标定
# 标定文件（像素坐标，针对某个姿势的那一帧原图）：
# {"poses": {"idle": {
#     "eyes":  [[[x,y],...多边形], [[x,y],...]],      左右眼（含睫毛）的轮廓，转换时在像素级用皮肤色抹掉
#     "mouth": [[x,y],...],                             原画嘴巴轮廓（同样抹掉）
#     "mouth_center": [x,y], "mouth_size": [列,行],     表情嘴巴的绘制区域（字符格）
#     "cheeks": [[x,y],[x,y]],                          腮红中心
#     "skin_sample": [x0,y0,x1,y1],                     取皮肤色的矩形
#     "blush_sample": [x0,y0,x1,y1]                     可选：取腮红色的矩形
# }}}
# 标定过的姿势称为“骨架姿势”：五官由本地骨架绘制（AI 只决定形状/少量自定义小图）；
# 没有标定的姿势是“原画姿势”：保留原画自带的表情，不叠加五官。

def _median_rgb(im, rect):
    px = [im.getpixel((x, y))[:3] for y in range(rect[1], rect[3]) for x in range(rect[0], rect[2])]
    return tuple(int(median(c[i] for c in px)) for i in range(3))


def blank_features(im, rig: dict):
    """返回 (空白脸图, 皮肤色, 腮红色)：眼睛与嘴巴在像素级被皮肤色覆盖，头发与脸部轮廓保持原样。"""
    from PIL import ImageDraw, ImageFilter

    out = im.copy()
    skin = _median_rgb(im, rig["skin_sample"])
    blush = _median_rgb(im, rig["blush_sample"]) if rig.get("blush_sample") else None
    mask = Image_new("L", im.size)
    d = ImageDraw.Draw(mask)
    for poly in list(rig["eyes"]) + ([rig["mouth"]] if rig.get("mouth") else []):
        pts = [tuple(p) for p in poly]
        d.polygon(pts, fill=255, outline=255)
        d.line(pts + [pts[0]], fill=255, width=2)
    mask = mask.filter(ImageFilter.GaussianBlur(0.8))
    fill = Image_new("RGBA", im.size, skin + (255,))
    out.paste(fill, (0, 0), mask)
    if im.mode == "RGBA":
        out.putalpha(im.getchannel("A"))
    return out, skin, blush


def Image_new(mode, size, color=0):
    from PIL import Image

    return Image.new(mode, size, color)


def _px_to_cell(box, cw, ch, x, y):
    return (x - box[0]) / cw, (y - box[1]) / ch


def build_rig(rig: dict, box, cw, ch, chars_o, colors_o, chars_b, colors_b, skin, blush, pal) -> dict:
    """在字符格坐标里生成骨架：面部区域、眼/嘴/腮红锚点、原画眼睛与嘴巴的小图、颜色。"""
    rows, cols = len(chars_o), len(chars_o[0])

    def rect_of(poly):
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        x0, y0 = _px_to_cell(box, cw, ch, min(xs), min(ys))
        x1, y1 = _px_to_cell(box, cw, ch, max(xs), max(ys))
        return [int(x0), int(y0), int(x1) - int(x0) + 1, int(y1) - int(y0) + 1]

    eyes = [rect_of(p) for p in rig["eyes"]]
    # 两只眼睛统一尺寸（取较大者，居中），保证镜像对称
    ew = max(e[2] for e in eyes)
    eh = max(e[3] for e in eyes)
    eyes = [[e[0] - (ew - e[2]) // 2, e[1] - (eh - e[3]) // 2, ew, eh] for e in eyes]
    mx, my = _px_to_cell(box, cw, ch, *rig["mouth_center"])
    mw, mh = rig.get("mouth_size", [10, 3])
    mouth = [int(round(mx - mw / 2)), int(round(my - mh / 2)), mw, mh]
    cheeks = [[int(round(v)) for v in _px_to_cell(box, cw, ch, *c)] for c in rig.get("cheeks", [])]
    top = min(e[1] for e in eyes) - 2
    x0 = min(min(e[0] for e in eyes), mouth[0]) - 2
    x1 = max(max(e[0] + e[2] for e in eyes), mouth[0] + mouth[2]) + 2
    y1 = max(mouth[1] + mouth[3], max(c[1] for c in cheeks) + 1 if cheeks else 0) + 1
    face = {"x": max(0, x0), "y": max(0, top), "w": min(cols, x1) - max(0, x0), "h": min(rows, y1) - max(0, top)}
    fx, fy = face["x"], face["y"]

    def sprite(rect):
        x, y, w, h = rect
        srows, scols = [], []
        for yy in range(y, y + h):
            r, c = [], []
            for xx in range(x, x + w):
                inside = 0 <= yy < rows and 0 <= xx < cols
                o, b = (chars_o[yy][xx], chars_b[yy][xx]) if inside else (" ", " ")
                co, cb = (colors_o[yy][xx], colors_b[yy][xx]) if inside else (None, None)
                diff = o != b or (co and cb and sum(abs(co[i] - cb[i]) for i in range(3)) > 40)
                if inside and diff and o != " ":
                    r.append(o)
                    c.append(pal.idx(co))
                else:
                    r.append("`")
                    c.append(-1)
            srows.append("".join(r))
            scols.append(c)
        return {"rows": srows, "colors": scols}

    eye_sprites = [sprite(e) for e in eyes]
    mouth_sprite = sprite(mouth)
    # 从原画眼睛里取色：最暗 = 睫毛，最亮 = 高光，中间的蓝色 = 虹膜
    eye_cols = [colors_o[y][x] for (ex, ey, w, h) in eyes for y in range(ey, ey + h) for x in range(ex, ex + w)
                if 0 <= y < rows and 0 <= x < cols and colors_o[y][x] and chars_o[y][x] != chars_b[y][x]]
    lum = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]  # noqa: E731
    eye_cols.sort(key=lum)
    hexc = lambda c: "#%02x%02x%02x" % tuple(c)  # noqa: E731
    lash = eye_cols[len(eye_cols) // 12] if eye_cols else (22, 32, 90)
    iris = eye_cols[len(eye_cols) // 2] if eye_cols else (61, 123, 255)
    light = eye_cols[int(len(eye_cols) * 0.8)] if eye_cols else (120, 200, 255)
    local = lambda r: [r[0] - fx, r[1] - fy, r[2], r[3]]  # noqa: E731
    return {
        "face": face,
        "rig": {
            "eyes": [local(e) for e in eyes],
            "mouth": local(mouth),
            "cheeks": [[c[0] - fx, c[1] - fy] for c in cheeks],
            "colors": {"lash": hexc(lash), "iris": hexc(iris), "iris_light": hexc(light), "highlight": "#ffffff",
                       "skin": hexc(_boost(*skin)), "blush": hexc(_boost(*blush)) if blush else "#ff9ab5",
                       "mouth": "#e0607e", "mouth_dark": "#9c2f4a", "tear": "#8fdcff"},
        },
        "sprites": {"eye_l": eye_sprites[0], "eye_r": eye_sprites[1], "mouth": mouth_sprite},
    }


class _Palette:
    def __init__(self):
        self.colors: list[str] = []
        self.index: dict[str, int] = {}

    def idx(self, rgb) -> int:
        if rgb is None:
            return -1
        q = "#%02x%02x%02x" % (rgb[0] & 0xF8, rgb[1] & 0xF8, rgb[2] & 0xF8)
        if q not in self.index:
            self.index[q] = len(self.colors)
            self.colors.append(q)
        return self.index[q]


def convert(sources: dict[str, tuple[str, int]], rows: int = 56, name: str = "character",
            display_name: str = "", rig: dict | None = None, side_margin: float = 0.16,
            top_margin: float = 0.05, cols: int | None = None, crop: float = 1.0) -> dict:
    """sources: {pose_name: (视频或图片路径, 帧号)}；rig: 标定文件内容（见上）。

    以第一个姿势（通常是待机）的角色高度确定缩放比例和裁剪框，所有姿势共用这一裁剪框，
    因此切换姿势时角色位置、大小保持一致（超出裁剪框的部分如大幅挥手会被裁掉）。
    """
    frames, masks = {}, {}
    anchor = None
    for pose, (src, idx) in sources.items():
        im = load_frame(src, idx)
        m = foreground_mask(im)
        bb = m.getbbox()
        if not bb:
            raise RuntimeError(f"{pose}: 没有检测到前景")
        frames[pose], masks[pose] = im, m
        if anchor is None:
            anchor = bb
    x0, y0, x1, y1 = anchor
    if crop < 1.0:  # 只保留上半身（头肩特写），脸在画面里更大
        y1 = y0 + int((y1 - y0) * crop)
    bh = y1 - y0
    ch = bh * (1 + top_margin) / (rows - 1)
    cw = ch / CELL_ASPECT
    if cols is None:
        cols = int(round((x1 - x0) * (1 + 2 * side_margin) / cw))
    cx = (x0 + x1) / 2
    box = (int(round(cx - cols * cw / 2)), int(round(y0 - bh * top_margin)),
           int(round(cx + cols * cw / 2)), int(round(y0 - bh * top_margin + rows * ch)))
    rig_poses = (rig or {}).get("poses", {})

    pal = _Palette()
    out = {"format": "pet_ascii.character/2", "name": name, "display_name": display_name or name,
           "ramp": RAMP, "cell_aspect": round(CELL_ASPECT, 3), "palette": pal.colors, "poses": {}}
    for pose, im in frames.items():
        raw = to_cells(im, masks[pose], box, cols, rows)
        norm = ramp_norm(raw)
        chars, colors = render_cells(raw, norm=norm)
        entry = {"w": cols, "h": rows, "source": f"{os.path.basename(sources[pose][0])}#{sources[pose][1]}"}
        if pose in rig_poses:
            r = rig_poses[pose]
            blank, skin, blush = blank_features(im, r)
            chars_b, colors_b = render_cells(to_cells(blank, masks[pose], box, cols, rows), norm=norm)
            built = build_rig(r, box, cw, ch, chars, colors, chars_b, colors_b, skin, blush, pal)
            chars, colors = chars_b, colors_b      # 身体层用空白脸
            entry["face"] = built["face"]
            entry["rig"] = built["rig"]
            entry["sprites"] = built["sprites"]
        entry["rows"] = ["".join(r) for r in chars]
        entry["colors"] = [[pal.idx(c) for c in row] for row in colors]
        out["poses"][pose] = entry
    out["default_pose"] = next(iter(sources))
    return out


# ---------------------------------------------------------------------- 场景图（无五官 + 有五官 两张原图）
def convert_scene(rig_path: str, view: str, out_dir: str, name: str | None = None) -> dict:
    """用“无五官版 + 有五官版”两张同构图生成角色：身体 = 无五官版，面部由骨架实时绘制（默认用有五官版的原画像素）。

    两张图分辨率可以不同（会缩放到有五官版的尺寸），构图必须一致。输出：
      <out_dir>/<view>.json                 角色素材
      <out_dir>/<view>_face_blank.png       面部区域（无五官）
      <out_dir>/<view>_face_neutral.png     面部区域（原画五官）
    """
    import math

    from PIL import Image

    with open(rig_path, encoding="utf-8") as f:
        cal = json.load(f)
    src_dir = os.path.dirname(os.path.abspath(rig_path))
    face_img = Image.open(os.path.join(src_dir, cal["image_face"])).convert("RGB")
    blank_img = Image.open(os.path.join(src_dir, cal["image_blank"])).convert("RGB").resize(face_img.size, Image.LANCZOS)
    v = cal["views"][view]
    X0, Y0, X1, Y1 = v["crop"]
    cols = int(v["cols"])
    cw = (X1 - X0) / cols
    ch = cw * CELL_ASPECT
    rows = int(round((Y1 - Y0) / ch))
    box = (X0, Y0, X0 + cols * cw, Y0 + rows * ch)

    def cells(img, b, w, h):
        small = img.convert("RGBA").resize((w, h), Image.BOX, box=b)
        return [[small.getpixel((x, y)) for x in range(w)] for y in range(h)]

    raw_face = cells(face_img, box, cols, rows)
    norm = ramp_norm(raw_face, floor=float(v.get("ink_floor", 0.1)), v_floor=float(v.get("ink_v", 0.22)))
    chars, colors = render_cells(cells(blank_img, box, cols, rows), norm=norm)

    fx0, fy0, fx1, fy1 = cal["face_box"]
    cx0 = max(0, int(math.floor((fx0 - X0) / cw)))
    cy0 = max(0, int(math.floor((fy0 - Y0) / ch)))
    cx1 = min(cols, int(math.ceil((fx1 - X0) / cw)))
    cy1 = min(rows, int(math.ceil((fy1 - Y0) / ch)))
    pb = (X0 + cx0 * cw, Y0 + cy0 * ch, X0 + cx1 * cw, Y0 + cy1 * ch)
    ox, oy = int(math.floor(pb[0])) - 2, int(math.floor(pb[1])) - 2
    crop = (ox, oy, int(math.ceil(pb[2])) + 2, int(math.ceil(pb[3])) + 2)
    os.makedirs(out_dir, exist_ok=True)
    blank_name, neutral_name = f"{view}_face_blank.png", f"{view}_face_neutral.png"
    blank_img.crop(crop).save(os.path.join(out_dir, blank_name))
    face_img.crop(crop).save(os.path.join(out_dir, neutral_name))

    pal = _Palette()
    pose = {
        "w": cols, "h": rows, "source": f"{cal['image_blank']} + {cal['image_face']} ({view})",
        "rows": ["".join(r) for r in chars],
        "colors": [[pal.idx(c) for c in row] for row in colors],
        "face": {"x": cx0, "y": cy0, "w": cx1 - cx0, "h": cy1 - cy0},
        "face_rig": {
            "rig": os.path.relpath(os.path.abspath(rig_path), os.path.abspath(out_dir)).replace("\\", "/"),
            "blank": blank_name, "neutral": neutral_name,
            "cells": [cx1 - cx0, cy1 - cy0],
            "px_box": [pb[0] - ox, pb[1] - oy, pb[2] - ox, pb[3] - oy],
            "crop_origin": [ox, oy],
            "norm": list(norm),
        },
    }
    data = {"format": "pet_ascii.character/2", "name": name or view,
            "display_name": v.get("display_name", name or view), "ramp": RAMP, "cell_aspect": round(CELL_ASPECT, 3),
            "palette": pal.colors, "poses": {"idle": pose}, "default_pose": "idle", "scene": True}
    with open(os.path.join(out_dir, f"{view}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return data


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="把视频帧/图片转换为 pet_ascii 彩色字符画角色")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", default="character")
    ap.add_argument("--display-name", default="")
    ap.add_argument("--rows", type=int, default=56, help="角色高度（行数），决定字符密度")
    ap.add_argument("--base-dir", default="")
    ap.add_argument("--crop", type=float, default=1.0, help="只保留角色上部的比例，如 0.62 = 头肩特写")
    ap.add_argument("--rig", default="", help="面部骨架标定文件（JSON，像素坐标）")
    ap.add_argument("--scene", default="", help="场景模式：两张原图的骨架标定文件（如 assets/source/maid_rig.json）")
    ap.add_argument("--view", default="", help="场景模式下要生成的视图名（标定文件 views 中的键）")
    ap.add_argument("pose", nargs="*", help="姿势=路径[#帧号]，路径可相对 --base-dir")
    a = ap.parse_args(argv)
    if a.scene:
        data = convert_scene(a.scene, a.view, a.out, a.name if a.name != "character" else None)
        p = data["poses"]["idle"]
        print(f"{a.view}: {p['w']}x{p['h']} face={p['face']} rig_cells={p['face_rig']['cells']}")
        return 0
    sources = {}
    for spec in a.pose:
        pose, _, rest = spec.partition("=")
        path, _, idx = rest.partition("#")
        sources[pose] = (os.path.join(a.base_dir, path) if a.base_dir else path, int(idx or 0))
    rig = None
    if a.rig:
        with open(a.rig, encoding="utf-8") as f:
            rig = json.load(f)
    data = convert(sources, rows=a.rows, name=a.name, display_name=a.display_name, crop=a.crop, rig=rig)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    for pose, p in data["poses"].items():
        print(f"{pose}: {p['w']}x{p['h']} face={p.get('face')} rig={p.get('rig', {}).get('eyes')} "
              f"mouth={p.get('rig', {}).get('mouth')} cheeks={p.get('rig', {}).get('cheeks')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
