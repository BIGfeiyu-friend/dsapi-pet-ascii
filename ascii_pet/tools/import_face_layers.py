"""把画师（用户本人）画的表情差分 PSD 导入成面部骨架用的分层素材。

输入：Procreate / Photoshop 导出的 PSD（和原图同构图，可以是不同分辨率），以及图层映射表
      assets/source/face_layers_map.json（哪个图层是哪个状态的眼眶线稿 / 眼白 / 虹膜 / 高光 / 嘴）。
输出：assets/characters/face_layers/ 下按状态、左右眼拆好的 PNG + manifest.json，坐标统一换算到标定图
      （maid_face.jpg，2000×1410）的像素坐标，所有角色视图（半身 / 特写）共用。

处理：
- 按映射表取图层（用自己写的 PSD 读取器，PIL 自带的读不了 Procreate 的多图层）
- 偏移校正：虹膜层如果被挪开了，和“带虹膜的线稿”参考层做模板对齐，自动移回眼睛上
- 缩放到标定图分辨率（预乘 alpha 再缩放，线条边缘不会发黑或发绿）
- 每张线稿按连通区域拆成：左眼 / 右眼、左眉 / 右眉、嘴（各自能单独变换、替换）
- 记录虹膜中心、眼白范围等几何信息

用法：python tools/import_face_layers.py <差分.psd> [映射表.json] [输出目录]
"""
from __future__ import annotations

import heapq
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from psd_layers import read as read_psd  # noqa: E402

CAL_W, CAL_H = 2000, 1410
REGION = (800, 300, 1130, 620)          # 输出范围（标定图坐标）：双眼、眉毛、嘴


def _canvas(layer, size):
    from PIL import Image

    c = Image.new("RGBA", size, (0, 0, 0, 0))
    if layer.image is not None:
        c.paste(layer.image, (layer.left, layer.top), layer.image)
    return c


def _to_cal(img, sx, sy, shift=(0, 0)):
    """PSD 坐标的整幅图层 → 标定图坐标下的 REGION 区域（预乘 alpha 缩放）。"""
    from PIL import Image

    x0, y0, x1, y1 = REGION
    box = ((x0) * sx + shift[0], (y0) * sy + shift[1], (x1) * sx + shift[0], (y1) * sy + shift[1])
    out = img.convert("RGBa").resize((x1 - x0, y1 - y0), Image.LANCZOS, box=box)
    return out.convert("RGBA")


def _dark_mask(img, thr=100):
    a = img.getchannel("A")
    g = img.convert("L")
    from PIL import ImageChops

    return ImageChops.multiply(a.point(lambda v: 255 if v > 80 else 0), g.point(lambda v: 255 if v < thr else 0))


def _align(moving, reference, guess, radius=24):
    """在 guess 附近搜索平移，使 moving 的深色像素与 reference 重合最多。返回 (dx, dy)（PSD 像素）。"""
    from PIL import ImageChops

    ref = _dark_mask(reference)
    mov = _dark_mask(moving)
    bb = ref.getbbox()
    best = (-1, guess)
    for step in (4, 1):
        cx, cy = best[1] if best[0] >= 0 else guess
        rng = radius if step == 4 else 4
        for dx in range(int(cx) - rng, int(cx) + rng + 1, step):
            for dy in range(int(cy) - rng, int(cy) + rng + 1, step):
                m = mov.crop((bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy))
                score = ImageChops.multiply(m, ref.crop(bb)).histogram()[255]
                if score > best[0]:
                    best = (score, (dx, dy))
    return best[1]


def _components(alpha, thr=40):
    """alpha 通道的 8 连通区域：[(像素集合, bbox)]。纯 Python（素材只有几百像素见方）。"""
    w, h = alpha.size
    px = alpha.load()
    seen = bytearray(w * h)
    comps = []
    for y in range(h):
        for x in range(w):
            if seen[y * w + x] or px[x, y] <= thr:
                continue
            stack = [(x, y)]
            seen[y * w + x] = 1
            pts = []
            while stack:
                cx, cy = stack.pop()
                pts.append((cx, cy))
                for nx in (cx - 1, cx, cx + 1):
                    for ny in (cy - 1, cy, cy + 1):
                        if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and px[nx, ny] > thr:
                            seen[ny * w + nx] = 1
                            stack.append((nx, ny))
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            comps.append((pts, (min(xs), min(ys), max(xs) + 1, max(ys) + 1)))
    return comps


def _mask_from(points, size):
    from PIL import Image

    m = Image.new("L", size, 0)
    mp = m.load()
    for x, y in points:
        mp[x, y] = 255
    return m


def split_lines(img, cal, mid_x, has_mouth=True):
    """线稿 → {"eye_l","eye_r","brow_l","brow_r","mouth"} 图层（REGION 坐标）。

    按连通区域归类：嘴 = 位于眼睛下方很远的；眉 = 在本侧上眼睑上方、隔开一段距离的；其余都归眼睛
    （双眼皮线、睫毛离上眼睑很近，仍算眼睛）。抗锯齿的半透明边按最近的实心像素归类。
    """
    from PIL import Image, ImageChops, ImageFilter

    x0, y0 = REGION[0], REGION[1]
    size = img.size
    alpha = img.getchannel("A")
    comps = _components(alpha, 60)
    eyes_y = (cal["eyes"][0]["center"][1] + cal["eyes"][1]["center"][1]) / 2 - y0
    mouth_y = cal["mouth"]["center"][1] - y0
    groups = {k: [] for k in ("eye_l", "eye_r", "brow_l", "brow_r", "mouth")}

    def mean_y(c):
        return sum(p[1] for p in c[0]) / len(c[0])

    rest = []
    for c in comps:
        cy = (c[1][1] + c[1][3]) / 2
        if has_mouth and cy > (eyes_y + mouth_y) / 2 + 10:
            groups["mouth"].append(c[0])
        else:
            rest.append(c)
    # 每一侧：最大的连通区域是上眼睑；眉毛 = 足够大的笔画里平均高度最高的那一笔（且明显高于上眼睑）。
    # 斜着的眉毛外端可能比上眼睑顶端还低，所以按平均高度判断；双眼皮线虽然也在上面，但比眉毛低，不会被选中。
    for side in ("l", "r"):
        mine = [c for c in rest if (((c[1][0] + c[1][2]) / 2 < mid_x) == (side == "l"))]
        if not mine:
            continue
        lid = max(mine, key=lambda c: len(c[0]))
        lid_h = lid[1][3] - lid[1][1]
        cands = [c for c in mine if c is not lid and len(c[0]) >= max(80, 0.12 * len(lid[0]))]
        brow = min(cands, key=mean_y) if cands else None
        if brow is not None and mean_y(brow) >= mean_y(lid) - 0.3 * lid_h:
            brow = None
        for c in mine:
            if brow is not None and (c is brow or (len(c[0]) < len(brow[0]) and c is not lid
                                                   and mean_y(c) < (mean_y(brow) + lid[1][1]) / 2)):
                groups[f"brow_{side}"].append(c[0])
            else:
                groups[f"eye_{side}"].append(c[0])
    out = {}
    for k, lst in groups.items():
        if not lst:
            continue
        core = _mask_from([p for pts in lst for p in pts], size)
        grown = core.filter(ImageFilter.MaxFilter(5))           # 带上抗锯齿的半透明边
        m = ImageChops.multiply(alpha, grown)
        part = Image.new("RGBA", size, (0, 0, 0, 0))
        part.paste(img, (0, 0), m)
        out[k] = part
    return out


_STEPS = ((-1, -1, 1.4142), (0, -1, 1.0), (1, -1, 1.4142), (-1, 0, 1.0), (1, 0, 1.0), (-1, 1, 1.4142), (0, 1, 1.0),
          (1, 1, 1.4142))


def _geodesic(mask, w, h, seeds):
    """在 mask 内从 seeds 出发沿笔画走的距离（8 邻接，斜走算 √2）；mask 外、不连通处为 inf。"""
    dist = [math.inf] * (w * h)
    pq = []
    for s in seeds:
        dist[s] = 0.0
        pq.append((0.0, s))
    heapq.heapify(pq)
    while pq:
        d, i = heapq.heappop(pq)
        if d > dist[i]:
            continue
        x, y = i % w, i // w
        for dx, dy, c in _STEPS:
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if mask[j] and d + c < dist[j]:
                    dist[j] = d + c
                    heapq.heappush(pq, (d + c, j))
    return dist


def trim_end(img, side: str, trim: float, taper: float = 6):
    """沿笔画擦掉一头（side=left/right 指主笔画靠左还是靠右的那一头）。

    从那头的笔尖起，沿着笔画走 trim 像素以内的全部擦掉；再往里 taper 像素做收笔：只留笔画中心，越往里越宽，
    所以新的末端仍是尖尖的笔锋，不是一刀切平。竖着、斜着走的笔画都适用；分叉出去的睫毛只要不在擦掉的范围内就保留。
    """
    from PIL import Image

    bb = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if not bb or trim <= 0:
        return img
    x0, y0 = max(0, bb[0] - 3), max(0, bb[1] - 3)
    sub = img.crop((x0, y0, min(img.width, bb[2] + 3), min(img.height, bb[3] + 3)))
    w, h = sub.size
    alpha = sub.getchannel("A").tobytes()
    mask = [v > 60 for v in alpha]
    # 主笔画 = 最大的连通区域
    seen = [False] * (w * h)
    main = []
    for s in range(w * h):
        if mask[s] and not seen[s]:
            seen[s] = True
            stack, comp = [s], []
            while stack:
                i = stack.pop()
                comp.append(i)
                x, y = i % w, i // w
                for dx, dy, _ in _STEPS:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h and mask[ny * w + nx] and not seen[ny * w + nx]:
                        seen[ny * w + nx] = True
                        stack.append(ny * w + nx)
            if len(comp) > len(main):
                main = comp
    # 笔尖：从另一头出发沿笔画走得最远、又在要擦的这一侧的点
    other = max(main, key=lambda i: i % w) if side == "left" else min(main, key=lambda i: i % w)
    d_other = _geodesic(mask, w, h, [other])
    xs = [i % w for i in main]
    cx = (min(xs) + max(xs)) / 2
    tip = max((i for i in main if (i % w < cx) == (side == "left")), key=lambda i: d_other[i])
    d = _geodesic(mask, w, h, [tip])

    # 沿笔画每 1 像素一段：段里的像素横跨笔画，像素数 ≈ 那里的笔画宽度，平均位置 ≈ 笔画中心线
    bands = {}
    for i in main:
        if d[i] < trim + taper + 2:
            bands.setdefault(int(d[i]), []).append(i)
    centre = {k: (sum(i % w for i in v) / len(v), sum(i // w for i in v) / len(v)) for k, v in bands.items()}
    k_end = int(trim + taper)
    widths = [len(bands[k]) for k in (k_end - 1, k_end, k_end + 1) if k in bands]
    end_half = max(1.0, sum(widths) / len(widths) / 2) if widths else 2.0    # 收笔结束处原来的半宽
    factor = {}
    for i in main:
        if d[i] >= trim + taper:
            continue
        if d[i] < trim - 1:
            factor[i] = 0.0
            continue
        s = max(0.0, d[i] - trim)                  # 离新笔尖多远
        allow = 0.5 + (end_half - 0.5) * s / taper  # 新笔尖 1 像素宽，到 taper 处恢复原宽度
        cx, cy = centre[int(d[i])]
        off = math.hypot(i % w - cx, i // w - cy)
        factor[i] = min(1.0, max(0.0, allow - off + 0.5)) * min(1.0, max(0.0, d[i] - trim + 1))
    if not factor:
        return img
    # 主笔画外的半透明边缘、碎点：跟随 3 像素内最近的主笔画像素
    main_set = set(main)
    fx = [i % w for i in factor]
    fy = [i // w for i in factor]
    out_a = bytearray(alpha)
    for i, f in factor.items():
        out_a[i] = int(alpha[i] * f + 0.5)
    for y in range(max(0, min(fy) - 3), min(h, max(fy) + 4)):
        for x in range(max(0, min(fx) - 3), min(w, max(fx) + 4)):
            i = y * w + x
            if alpha[i] == 0 or i in main_set:
                continue
            near, nd = None, 99.0
            for yy in range(max(0, y - 3), min(h, y + 4)):
                for xx in range(max(0, x - 3), min(w, x + 4)):
                    j = yy * w + xx
                    if j in main_set:
                        dd = math.hypot(xx - x, yy - y)
                        if dd < nd:
                            near, nd = j, dd
            if near is not None and near in factor:
                out_a[i] = int(alpha[i] * factor[near] + 0.5)
    sub.putalpha(Image.frombytes("L", (w, h), bytes(out_a)))
    out = img.copy()
    out.paste(sub, (x0, y0))
    return out


def main():
    from PIL import Image, ImageChops

    psd = sys.argv[1]
    mpath = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "assets", "source", "face_layers_map.json")
    outdir = sys.argv[3] if len(sys.argv) > 3 else os.path.join(ROOT, "assets", "characters", "face_layers")
    mp = json.load(open(mpath, encoding="utf-8"))
    cal = json.load(open(os.path.join(ROOT, "assets", "source", "maid_rig.json"), encoding="utf-8"))
    W, H, layers = read_psd(psd)
    sx, sy = W / CAL_W, H / CAL_H
    print(f"PSD {W}x{H}，相对标定图缩放 {sx:.4f} × {sy:.4f}")
    for role, spec in mp["layers"].items():
        L = layers[spec["index"]]
        if spec.get("name") and L.name != spec["name"]:
            raise SystemExit(f"映射表里 {role} 的图层 #{spec['index']} 叫「{L.name}」，不是「{spec['name']}」：PSD 改过的话请更新映射表")
    full = {role: _canvas(layers[spec["index"]], (W, H)) for role, spec in mp["layers"].items()}
    # 偏移校正：被挪开的虹膜组和参考线稿对齐
    shifts = {}
    for group in mp.get("align", []):
        guess = tuple(group["guess"])
        d = _align(full[group["moving"]], full[group["reference"]], guess)
        for role in group["roles"]:
            shifts[role] = d
        print(f"对齐：{group['roles']} 偏移 {d}（PSD 像素）")
    cal_img = {role: _to_cal(img, sx, sy, shifts.get(role, (0, 0))) for role, img in full.items()}
    os.makedirs(outdir, exist_ok=True)
    mid_x = (cal["eyes"][0]["center"][0] + cal["eyes"][1]["center"][0]) / 2 - REGION[0]
    manifest = {"source": os.path.basename(psd), "cal_size": [CAL_W, CAL_H], "region": list(REGION), "states": {},
                "iris": {}, "mouths": {}}

    def save(name, img):
        bb = img.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
        if not bb:
            return None
        crop = img.crop(bb)
        crop.save(os.path.join(outdir, name + ".png"), optimize=True)
        return {"file": name + ".png", "origin": [REGION[0] + bb[0], REGION[1] + bb[1]], "size": list(crop.size)}

    def halves(img):
        w, h = img.size
        lm = Image.new("L", (w, h), 0)
        lm.paste(255, (0, 0, int(mid_x), h))
        rm = ImageChops.invert(lm)
        out = {}
        for side, m in (("l", lm), ("r", rm)):
            part = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            part.paste(img, (0, 0), ImageChops.multiply(img.getchannel("A"), m))
            out[side] = part
        return out

    for state, roles in mp["states"].items():
        st = {}
        parts = split_lines(cal_img[roles["lines"]], cal, mid_x, has_mouth=roles.get("mouth_in_lines", True))
        for k, adj in (mp.get("adjust") or {}).items():          # 微调：例如笑眼左右长度不对称时擦掉一端
            if ":" not in k:
                continue
            st_name, part_name = k.split(":", 1)
            if st_name == state and part_name in parts and adj.get("trim"):
                parts[part_name] = trim_end(parts[part_name], adj.get("side", "left"), float(adj["trim"]),
                                            float(adj.get("taper", 6)))
                print(f"微调：{state} {part_name} 擦掉{'左' if adj.get('side', 'left') == 'left' else '右'}端 {adj['trim']} 像素")
        for k, img in parts.items():
            if k == "mouth":
                continue
            st[k] = save(f"{state}_{k}", img)
        if "sclera" in roles:
            for side, img in halves(cal_img[roles["sclera"]]).items():
                st[f"sclera_{side}"] = save(f"{state}_sclera_{side}", img)
        if roles.get("iris"):
            st["iris"] = roles["iris"]
        if "mouth" in parts and roles.get("mouth_name"):
            mouth = parts["mouth"]
            if roles.get("mouth_fill"):
                base = cal_img[roles["mouth_fill"]].copy()
                base.alpha_composite(mouth)
                mouth = base
            manifest["mouths"][roles["mouth_name"]] = save(f"mouth_{roles['mouth_name']}", mouth)
        manifest["states"][state] = st
        print(f"状态 {state}：{sorted(k for k, v in st.items() if v)}")
    for iris, roles in mp["iris"].items():
        for side in ("l", "r"):
            body = Image.new("RGBA", cal_img[roles["dark"]].size, (0, 0, 0, 0))
            for r in (roles["blue"], roles["dark"]):
                body.alpha_composite(halves(cal_img[r])[side])
            hl = halves(cal_img[roles["highlight"]])[side]
            ent = {"body": save(f"iris_{iris}_{side}", body), "highlight": save(f"iris_{iris}_{side}_hl", hl)}
            dm = _dark_mask(body, 140).point(lambda v: 255 if v else 0)
            a = body.getchannel("A").point(lambda v: 255 if v > 80 else 0)
            bb = a.getbbox()
            ent["center"] = [REGION[0] + (bb[0] + bb[2]) / 2, REGION[1] + (bb[1] + bb[3]) / 2]
            ent["radius"] = [(bb[2] - bb[0]) / 2, (bb[3] - bb[1]) / 2]
            del dm
            manifest["iris"].setdefault(iris, {})[side] = ent
    for name, roles in mp.get("extra_mouths", {}).items():
        img = cal_img[roles["fill"]].copy()
        if roles.get("lines_from"):
            lines = split_lines(cal_img[roles["lines_from"]], cal, mid_x).get("mouth")
            if lines is not None:
                img.alpha_composite(lines)
        manifest["mouths"][name] = save(f"mouth_{name}", img)
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print("写入", outdir)
    try:                                       # 连续变形用的笔画脊柱（v0.8）：跟着差分一起重新提取
        from build_face_strokes import run as build_strokes

        build_strokes(outdir)
    except Exception as exc:                   # noqa: BLE001
        print("笔画脊柱提取失败（运行时会现算）：", exc)


if __name__ == "__main__":
    main()
