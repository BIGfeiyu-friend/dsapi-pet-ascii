"""本地视觉检查（v0.7）：分析**实际渲染出来的结果**，不是按参数想象画面。

对一个表情规格（可以带自创组件 / 骨架字段）做一次分层渲染，然后用确定性的方法检查：
- 越界：部件画到了脸部区域外（字符画里看不到）、新增节点跑出脸框
- 遮挡：部件大面积画在头发上（不是皮肤）、部件之间互相重叠（眼睛压住嘴等）
- 结构：两眼左右颠倒、眼睛跑到嘴下面、嘴不在下半张脸、眼睛高低差太大
- 可见性：每个部件在字符画上实际改变了多少格（太碎的细节会被字符化抹掉）、和皮肤的颜色对比
- 缺失：引用了不存在的组件、组件什么也没画出来
另外给出“改动区域”（用来决定截图范围）和本地截图：局部特写 + 整张脸缩略图（字符画 + 像素两种）。
全部在本地完成，几十毫秒；不调用任何 API。截图用 PIL 绘制，可以在后台线程里运行。
"""
from __future__ import annotations

import io
import math

from . import face_rig as fr
from . import parts as pt

PAIR_SOFT = {frozenset(("eye_l", "brow_l")), frozenset(("eye_r", "brow_r")), frozenset(("blush", "cheek_l")),
             frozenset(("blush", "cheek_r"))}
HAIR_OK = {"brow_l", "brow_r", "forehead", "sweat"}         # 这些部件本来就画在刘海 / 额头上
SKIN_DIST = 70


def _alpha_mask(layer, thresh: int = 40):
    return layer.getchannel("A").point(lambda v: 255 if v > thresh else 0)


def _area(mask) -> int:
    return mask.histogram()[255]


def _bbox_union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def analyze(rig, spec: dict, parts: dict | None = None, ref: dict | None = None, focus: list | None = None,
            intended_asym: bool = False, snapshots: bool = False) -> dict:
    """检查一个表情规格的实际渲染结果。spec 可以是未规整的；parts 是候选组件表（不给就用正式库）。

    返回 {"ok", "errors", "warnings", "parts":{节点:{...}}, "metrics":{...}, "region_px", "region_cells", "mirror"}，
    snapshots=True 时另带 "images": {"ascii_focus","ascii_face","pixel_focus"}（PNG 字节）。
    """
    from PIL import Image, ImageChops

    spec = fr.normalize(spec) if "eye_l" not in spec else spec
    lib = fr.PARTS if parts is None else {**fr.PARTS, **parts}
    img, layers, info = rig.compose(spec, lib, want_layers=True)
    fields = pt.rig_fields(spec)
    nodes = rig.nodes
    errors, warnings = [], []
    W, H = img.size
    bx0, by0, bx1, by1 = rig.px_box
    face_mask = Image.new("L", (W, H), 0)
    face_mask.paste(255, (int(bx0), int(by0), int(bx1), int(by1)))
    # 皮肤区域：无五官原图里接近肤色的像素
    skin = rig.skin
    blank = rig.blank
    diff = ImageChops.difference(blank, Image.new("RGB", blank.size, skin)).convert("L")
    skin_mask = diff.point(lambda v: 255 if v < SKIN_DIST else 0)

    for m in sorted(set(info["missing"])):
        errors.append(f"引用了不存在的组件「{m}」")

    report_parts = {}
    masks = {}
    for node, layer in layers.items():
        mask = _alpha_mask(layer)
        area = _area(mask)
        masks[node] = mask
        if area == 0:
            if node not in ("blush",):
                comp_name = (fields.get("parts") or {}).get(node) or next(
                    (e["part"] for e in fields.get("extra") or [] if e["id"] == node), None)
                glyph = any(g[0] == node for g in info["glyphs"])
                if comp_name and not glyph:
                    errors.append(f"{node} 的组件「{comp_name}」什么也没画出来")
            continue
        inside = _area(ImageChops.multiply(mask, face_mask)) / area
        on_skin = _area(ImageChops.multiply(mask, skin_mask)) / area
        bbox = mask.getbbox()
        rgb = layer.convert("RGB")
        # 和底下皮肤的颜色对比（只看部件像素）
        d = ImageChops.difference(rgb, blank).convert("L")
        hist = ImageChops.multiply(d, mask).histogram()
        total = sum(i * c for i, c in enumerate(hist))
        contrast = total / area / 1.0 if area else 0.0
        report_parts[node] = {"area": area, "inside": round(inside, 2), "on_skin": round(on_skin, 2),
                              "contrast": round(contrast, 1), "bbox": list(bbox) if bbox else None}
        label = fr.NODE_CN.get(node, node)
        if node == "blush":
            continue
        if inside < 0.2:
            errors.append(f"{label}几乎全画在脸部区域外（{inside:.0%} 在内），字符画里看不到")
        elif inside < 0.7:
            warnings.append(f"{label}有 {1 - inside:.0%} 画到了脸部区域外，会被裁掉")
        if node not in HAIR_OK and on_skin < 0.35:
            warnings.append(f"{label}大部分（{1 - on_skin:.0%}）画在头发或衣服上，不在皮肤上")
        if contrast < 30:
            warnings.append(f"{label}和皮肤的颜色太接近（对比 {contrast:.0f}），字符化后可能看不清")

    # 部件之间的重叠
    names = [n for n in masks if n != "blush" and report_parts.get(n)]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if frozenset((a, b)) in PAIR_SOFT:
                continue
            inter = _area(ImageChops.multiply(masks[a], masks[b]))
            if not inter:
                continue
            ratio = inter / max(1, min(report_parts[a]["area"], report_parts[b]["area"]))
            la, lb = fr.NODE_CN.get(a, a), fr.NODE_CN.get(b, b)
            critical = {a, b} & {"eye_l", "eye_r", "mouth"} and len({a, b} & {"eye_l", "eye_r", "mouth"}) == 2
            if ratio > 0.5 and critical:
                errors.append(f"{la}和{lb}重叠了 {ratio:.0%}")
            elif ratio > 0.3:
                warnings.append(f"{la}和{lb}重叠了 {ratio:.0%}")

    # 结构
    geo = {n: pt.node_geom(nodes, fields, n) for n in ("eye_l", "eye_r", "mouth")}
    face = nodes["face"]
    fh = face["b"] * 2
    fw = face["a"] * 2
    hidden = {n for n, v in (fields.get("parts") or {}).items() if v == "none"}
    el, er, mo = geo["eye_l"], geo["eye_r"], geo["mouth"]
    if el["cx"] > er["cx"] - fw * 0.05:
        errors.append("左右眼的位置颠倒或挤到了一起")
    if "mouth" not in hidden and min(el["cy"], er["cy"]) > mo["cy"] - fh * 0.08:
        errors.append("眼睛跑到了嘴的下面（或和嘴一样高）")
    rel_y = (mo["cy"] - (face["cy"] - face["b"])) / fh
    if "mouth" not in hidden and not 0.45 <= rel_y <= 1.0:
        warnings.append(f"嘴的位置不在下半张脸（在脸框 {rel_y:.0%} 高处）")
    if abs(el["cy"] - er["cy"]) > fh * 0.08 and not intended_asym:
        warnings.append(f"两只眼睛高低差 {abs(el['cy'] - er['cy']) / fh:.0%}（不是有意的不对称就修正）")
    if {"eye_l", "eye_r"} <= hidden and not fields.get("extra"):
        warnings.append("两只眼睛都被隐藏了，又没有新增的部件代替")
    for e in fields.get("extra") or []:
        g = pt.node_geom(nodes, fields, e["id"])
        if not (face["cx"] - face["a"] * 1.1 <= g["cx"] <= face["cx"] + face["a"] * 1.1
                and face["cy"] - face["b"] * 1.1 <= g["cy"] <= face["cy"] + face["b"] * 1.1):
            warnings.append(f"新增节点 {e['id']} 在脸框外")

    # 字符画上的可见性：和“没有五官的脸”相比，每个部件所在的格子里有多少格真的变了
    chars, colors = rig.render(spec, parts=lib if parts is not None else None)
    base = fr.normalize({"parts": {n: "none" for n in pt.CORE_NODES}, "blush": 0.0})
    bchars, bcolors = rig.render(base)

    def cell_changed(x, y):
        if chars[y][x] != bchars[y][x]:
            return True
        k1, k2 = colors[y][x], bcolors[y][x]
        return bool(k1 and k2 and sum(abs(k1[i] - k2[i]) for i in range(3)) > 60)

    fwc, fhc = rig.fw, rig.fh
    changed = [[cell_changed(x, y) for x in range(fwc)] for y in range(fhc)]
    for node, rp in report_parts.items():
        if node == "blush" or not rp.get("bbox"):
            continue
        x0, y0, x1, y1 = rp["bbox"]
        c0 = rig.cell_of_px(x0, y0)
        c1 = rig.cell_of_px(x1, y1)
        cx0, cy0 = max(0, int(c0[0])), max(0, int(c0[1]))
        cx1, cy1 = min(fwc - 1, int(math.ceil(c1[0]))), min(fhc - 1, int(math.ceil(c1[1])))
        vis = sum(1 for y in range(cy0, cy1 + 1) for x in range(cx0, cx1 + 1) if changed[y][x])
        rp["visible_cells"] = vis
        rp["cells"] = [cx0, cy0, cx1, cy1]
        if vis < 3 and rp["inside"] >= 0.2:
            label = fr.NODE_CN.get(node, node)
            (errors if node in pt.CORE_NODES and node.startswith(("eye", "mouth")) and node not in hidden
             and (fields.get("parts") or {}).get(node) not in (None, "builtin") else warnings).append(
                f"{label}在字符画上几乎看不出来（只改变了 {vis} 格），细节太小或太淡")
    for node, comp, geom in info["glyphs"]:
        cx, cy = rig.cell_of_px(geom["cx"], geom["cy"])
        if not (0 <= cx < fwc and 0 <= cy < fhc):
            warnings.append(f"{node} 的字符纹理在脸部区域外")

    # 指标
    metrics = {"parts_used": sorted(set(info["used"]))}
    try:
        metrics["visible_vs_neutral"] = round(rig.visible_diff(fr.neutral(), spec, parts=lib if parts is not None else None), 3)
        if ref is not None:
            metrics["visible_vs_ref"] = round(rig.visible_diff(fr.normalize(ref), spec,
                                                               parts=lib if parts is not None else None), 3)
    except Exception:
        pass
    metrics["changed_cells"] = sum(sum(r) for r in changed)

    # 改动区域：focus 指定的节点，否则是所有换了组件 / 动过 / 新增的节点；都没有就是整张脸
    focus = focus or sorted(set((fields.get("parts") or {}).keys()) | set((fields.get("nodes") or {}).keys())
                            | {e["id"] for e in fields.get("extra") or []})
    box = _bbox_union([report_parts.get(n, {}).get("bbox") for n in focus]) if focus else None
    if box is None:
        box = [bx0, by0, bx1, by1]
    pad = max(rig.cw, rig.ch) * 3
    region = [max(bx0, box[0] - pad), max(by0, box[1] - pad), min(bx1, box[2] + pad), min(by1, box[3] + pad)]
    c0, c1 = rig.cell_of_px(region[0], region[1]), rig.cell_of_px(region[2], region[3])
    region_cells = [max(0, int(c0[0])), max(0, int(c0[1])), min(fwc, int(math.ceil(c1[0]))), min(fhc, int(math.ceil(c1[1])))]

    out = {"ok": not errors, "errors": errors, "warnings": warnings[:10], "parts": report_parts, "metrics": metrics,
           "region_px": [round(v, 1) for v in region], "region_cells": region_cells, "focus": focus,
           "mirror": fr.mirror_text(spec)}
    if snapshots:
        out["images"] = {"ascii_focus": ascii_png(chars, colors, region_cells, cell=(9, 16)),
                         "ascii_face": ascii_png(chars, colors, None, cell=(5, 9)),
                         "pixel_focus": pixel_png(img, region, 320)}
    return out


def summary(report: dict) -> dict:
    """给模型看的紧凑版本（去掉像素级细节和图片）。"""
    parts = {n: {k: v for k, v in p.items() if k in ("visible_cells", "inside", "on_skin", "contrast")}
             for n, p in report.get("parts", {}).items() if n != "blush"}
    return {"ok": report.get("ok"), "errors": report.get("errors", []), "warnings": report.get("warnings", []),
            "metrics": report.get("metrics", {}), "parts": parts, "looks": report.get("mirror", "")}


# ---------------------------------------------------------------------- 截图（PIL，后台线程可用）
_FONT = {}


def _font(size: int):
    from PIL import ImageFont

    if size in _FONT:
        return _FONT[size]
    f = None
    for path in ("C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/cour.ttf", "DejaVuSansMono.ttf"):
        try:
            f = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    _FONT[size] = f or ImageFont.load_default()
    return _FONT[size]


def ascii_png(chars, colors, region_cells=None, cell=(7, 12), bg=(11, 14, 20)) -> bytes:
    """把字符网格画成 PNG（和窗口里看到的字符画一致的配色；用于给视觉模型看、存档）。"""
    from PIL import Image, ImageDraw

    h, w = len(chars), len(chars[0]) if chars else 0
    x0, y0, x1, y1 = region_cells or (0, 0, w, h)
    x1, y1 = min(w, max(x0 + 1, x1)), min(h, max(y0 + 1, y1))
    cw, ch = cell
    img = Image.new("RGB", ((x1 - x0) * cw, (y1 - y0) * ch), bg)
    d = ImageDraw.Draw(img)
    font = _font(ch)
    for y in range(y0, y1):
        for x in range(x0, x1):
            c, k = chars[y][x], colors[y][x]
            if not k:
                continue
            # 和窗口里一样的“同色衬底”：格子底色 = 背景向字符颜色混 30%（字符画的块面感主要来自它）
            plate = tuple(int(bg[i] + (k[i] - bg[i]) * 0.3) for i in range(3))
            d.rectangle([(x - x0) * cw, (y - y0) * ch, (x - x0 + 1) * cw - 1, (y - y0 + 1) * ch - 1], fill=plate)
            if c in (" ", "", None):
                continue
            d.text(((x - x0) * cw, (y - y0) * ch - 1), str(c), fill=tuple(k[:3]), font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def pixel_png(img, region, max_side: int = 320) -> bytes:
    from PIL import Image

    crop = img.crop(tuple(int(round(v)) for v in region))
    s = max_side / max(1, max(crop.size))
    if s != 1:
        crop = crop.resize((max(1, int(crop.width * s)), max(1, int(crop.height * s))), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "PNG", optimize=True)
    return buf.getvalue()
