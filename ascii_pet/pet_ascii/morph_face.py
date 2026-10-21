"""手绘差分的连续变形（v0.8）：眼睛、眉毛、嘴不再只能在几张差分之间切换，而是连续可调，且始终是画师的原像素。

做法（“沿笔画脊柱搬运”）：
  - 每张差分里的每一笔（上眼睑、下眼睑、眼褶、眉毛、嘴的上唇线和下轮廓）提取一条脊柱（中心线）。
  - 参数改变时只改脊柱的走向：目标脊柱 = 各张差分里这一笔的脊柱按权重混合，再叠加新参数的变形
    （外眼角下垂 / 上挑、下眼睑上抬、眉毛平移旋转、嘴宽 / 张开 / 嘴角 / 歪嘴）。
  - 原像素按“沿脊柱走了多远 u、离脊柱多远 v”搬到目标脊柱上同样的位置：v 不变 → 线条粗细、毛边、笔锋、睫毛分叉都是原画的。
    用 PIL 的 MESH 变换实现，只在网格角点上算映射，逐像素采样由 PIL 完成。
  - 像素主要取权重最大的那张差分，只在交接点附近很窄的一段交叉淡化（像动画的原画张交接），不会长时间重影。
  - 眼白用手绘的眼白逐列重排：上沿跟着上眼睑、下沿跟着下眼睑（平涂的眼白纵向伸缩看不出来）；
    闭眼、笑眼那两张没有下眼睑，给它们一条“虚拟下眼睑”（闭合线的内侧一段），下眼睑合拢到那里、被上眼睑盖住后才消失。
  - 嘴：上唇线 + 下轮廓连成一个闭合环，用同一条环形脊柱搬运（嘴角处无缝）；嘴里的颜色在两条线之间重新填。

素材：tools/import_face_layers.py 导入的 face_layers/（manifest.json + 各状态的 PNG）。
提取脊柱比较慢（约 1 秒），由 tools/build_face_strokes.py 预先算好存成 face_layers/strokes.json + strokes/*.png；
运行时只读取。没有预计算文件时在进程里现算（并在日志里提示）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from collections import OrderedDict, deque

log = logging.getLogger("pet.ascii")

N8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
STATES = ("open", "half", "wide", "closed", "smile")
OPEN_KEYS = [("closed", 0.0), ("half", 0.55), ("open", 1.0), ("wide", 1.3)]
K_ARC, K_TAIL, K_LINE, K_BROW = 48, 16, 40, 32
K_MTOP, K_MBOT = 32, 48
LOWER_SPAN = 0.55          # 虚拟下眼睑：闭合线从内眼角起的这一段
HANDOVER = 0.2             # 两张差分权重相差小于这个值时才交叉淡化
FORMAT = 3


# ====================================================================== 几何
def frames(pts):
    """每个点的单位切向量和法向量（法向量 = 切向量转 90°）。退化段沿用相邻的方向。"""
    n = len(pts)
    T = []
    for i in range(n):
        a, b = pts[max(0, i - 1)], pts[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        T.append((dx / L, dy / L) if L > 1e-6 else None)
    last = next((t for t in T if t is not None), (1.0, 0.0))
    for i in range(n):
        if T[i] is None:
            T[i] = last
        last = T[i]
    return T, [(-ty, tx) for tx, ty in T]


def arc_samples(pts, k):
    """按弧长均匀取 k 个点。"""
    if k <= 0:
        return []
    if len(pts) == 1:
        return [tuple(pts[0])] * k
    segs = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    total = sum(segs)
    if total < 1e-6:
        return [tuple(pts[0])] * k
    out = []
    i, acc = 0, 0.0
    for j in range(k):
        target = total * (j / (k - 1) if k > 1 else 0.0)
        while i < len(segs) - 1 and acc + segs[i] < target - 1e-9:
            acc += segs[i]
            i += 1
        s = segs[i]
        f = 0.0 if s == 0 else min(1.0, max(0.0, (target - acc) / s))
        a, b = pts[i], pts[i + 1]
        out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def rotate(pts, pivot, deg):
    """屏幕坐标（y 向下）里绕 pivot 转 deg 度：正角度 = 顺时针。"""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    px, py = pivot
    return [(px + (x - px) * c - (y - py) * s, py + (x - px) * s + (y - py) * c) for x, y in pts]


def y_at(pts, x):
    """折线在横坐标 x 处的纵坐标（按 x 排序后线性插值，超出范围取端点）。"""
    q = sorted(pts)
    if x <= q[0][0]:
        return q[0][1]
    if x >= q[-1][0]:
        return q[-1][1]
    for (x0, y0), (x1, y1) in zip(q, q[1:]):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return q[-1][1]


class _Grid:
    """脊柱采样点的空间分桶：查最近的点只看附近几个桶（比每次扫全部点快很多），太远的角点直接判定无效。"""

    def __init__(self, pts, size=8):
        self.size = size
        self.b = {}
        for i, (x, y) in enumerate(pts):
            self.b.setdefault((int(x // size), int(y // size)), []).append(i)

    def near(self, x, y, r):
        s = self.size
        bx, by = int(x // s), int(y // s)
        k = int(r // s) + 1
        out = []
        for gy in range(by - k, by + k + 1):
            for gx in range(bx - k, bx + k + 1):
                out.extend(self.b.get((gx, gy), ()))
        return out


def _project(q, pts, T, grid, r, closed=False, allow=None):
    """点 q 投影到折线：返回 (段下标 i, 段内比例 f, 法向距离 v, 超出两端的切向距离 s)；附近没有脊柱时返回 None。
    allow：只允许投影到这些点（嘴合上时上唇线和下轮廓叠在一起，各自只认自己那一段，免得像素去对面找）。"""
    qx, qy = q
    cand = grid.near(qx, qy, r)
    if allow is not None:
        cand = [i for i in cand if i in allow]
    if not cand:
        return None
    n = len(pts)
    j = min(cand, key=lambda i: (pts[i][0] - qx) ** 2 + (pts[i][1] - qy) ** 2)
    best = None
    for i in (j - 1, j):
        if closed:
            i %= n
        elif i < 0 or i >= n - 1:
            continue
        if allow is not None and i not in allow:
            continue
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        f = 0.0 if L2 < 1e-9 else ((qx - a[0]) * dx + (qy - a[1]) * dy) / L2
        fc = min(1.0, max(0.0, f))
        px, py = a[0] + dx * fc, a[1] + dy * fc
        d2 = (qx - px) ** 2 + (qy - py) ** 2
        if best is None or d2 < best[0]:
            best = (d2, i, fc, f)
    if best is None:
        return None
    _, i, fc, f = best
    a, b = pts[i], pts[(i + 1) % n]
    px, py = a[0] + (b[0] - a[0]) * fc, a[1] + (b[1] - a[1]) * fc
    t0, t1 = T[i], T[(i + 1) % n]
    tx, ty = t0[0] * (1 - fc) + t1[0] * fc, t0[1] * (1 - fc) + t1[1] * fc
    L = math.hypot(tx, ty) or 1.0
    tx, ty = tx / L, ty / L
    v = -(qx - px) * ty + (qy - py) * tx
    s = 0.0
    if f < 0 or f > 1:
        # 投影落在线段端点外：脊柱的两端，或折线拐角外侧的楔形区域（嘴角、外眼角）。保留沿切向的偏移，
        # 否则这一片格子都会映射到拐角那一点，把那里的笔画拉成一根多余的线
        s = (qx - px) * tx + (qy - py) * ty
    return i, fc, v, s


def transport(img, src, dst, reach, size, cell=3, closed=False, allow=None):
    """把 img 里沿脊柱 src 的笔画搬到脊柱 dst 上（点数相同、一一对应）。reach = 笔画像素离脊柱的最远距离。"""
    from PIL import Image

    keep = [0] + [i for i in range(1, len(src))
                  if math.hypot(src[i][0] - src[i - 1][0], src[i][1] - src[i - 1][1]) > 0.3]
    if len(keep) < 2:
        return Image.new("RGBA", size, (0, 0, 0, 0))
    if allow is not None:
        allow = {k for k, i in enumerate(keep) if i in allow}
    src = [src[i] for i in keep]
    dst = [dst[i] for i in keep]
    Ts, _ = frames(src)
    Td, _ = frames(dst)
    grid = _Grid(dst)
    R = reach + cell
    xs = [p[0] for p in dst]
    ys = [p[1] for p in dst]
    pad = R + 2
    x0, y0 = max(0, int(min(xs) - pad)), max(0, int(min(ys) - pad))
    x1, y1 = min(size[0], int(max(xs) + pad) + 1), min(size[1], int(max(ys) + pad) + 1)
    n = len(src)
    smax = min(reach, 6) + cell
    cache = {}

    def src_of(gx, gy):
        key = (gx, gy)
        hit = cache.get(key)
        if hit is None:
            pr = _project((gx, gy), dst, Td, grid, R + 4, closed, allow)
            if pr is None:
                hit = (0.0, 0.0, False)
            else:
                i, f, v, s = pr
                a, b = src[i], src[(i + 1) % n]
                px, py = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
                t0, t1 = Ts[i], Ts[(i + 1) % n]
                tx, ty = t0[0] * (1 - f) + t1[0] * f, t0[1] * (1 - f) + t1[1] * f
                L = math.hypot(tx, ty) or 1.0
                tx, ty = tx / L, ty / L
                hit = (px - ty * v + tx * s, py + tx * v + ty * s, abs(v) <= R and abs(s) <= smax)
            cache[key] = hit
        return hit

    mesh = []
    lim = cell * 4
    for gy in range(y0, y1, cell):
        for gx in range(x0, x1, cell):
            c = (src_of(gx, gy), src_of(gx, gy + cell), src_of(gx + cell, gy + cell), src_of(gx + cell, gy))
            if not (c[0][2] or c[1][2] or c[2][2] or c[3][2]):
                continue
            if max(math.hypot(c[0][0] - c[2][0], c[0][1] - c[2][1]), math.hypot(c[1][0] - c[3][0], c[1][1] - c[3][1])) > lim:
                continue                       # 投影跳段处的格子会被拉成细长条，把笔画采样成一串点：跳过
            mesh.append(((gx, gy, gx + cell, gy + cell), (c[0][0], c[0][1], c[1][0], c[1][1], c[2][0], c[2][1],
                                                          c[3][0], c[3][1])))
    if not mesh:
        return Image.new("RGBA", size, (0, 0, 0, 0))
    return img.convert("RGBa").transform(size, Image.MESH, mesh, resample=Image.BILINEAR).convert("RGBA")


def reach_of(img, pts, closed=False):
    """笔画像素离脊柱的最远距离（决定搬运时覆盖多宽）。取 99% 分位，不让个别杂点把范围撑大。"""
    w, h = img.size
    a = img.getchannel("A").tobytes()
    T, _ = frames(pts)
    grid = _Grid(pts)
    ds = []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if a[y * w + x] > 10:
                pr = _project((x, y), pts, T, grid, 40, closed)
                if pr is not None:
                    ds.append(max(abs(pr[2]), abs(pr[3])))
    if not ds:
        return 3.0
    ds.sort()
    return ds[min(len(ds) - 1, int(len(ds) * 0.99))] + 2


# ====================================================================== 提取脊柱
def _mask(img, thr):
    w, h = img.size
    a = img.getchannel("A").tobytes()
    return [[a[y * w + x] > thr for x in range(w)] for y in range(h)], w, h


def _components(mask, w, h):
    seen = [[False] * w for _ in range(h)]
    comps = []
    for y in range(h):
        for x in range(w):
            if mask[y][x] and not seen[y][x]:
                q, comp = [(x, y)], []
                seen[y][x] = True
                while q:
                    cx, cy = q.pop()
                    comp.append((cx, cy))
                    for dx, dy in N8:
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            q.append((nx, ny))
                comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def _thin(mask, w, h):
    """Zhang-Suen 细化 → 单像素骨架。"""
    m = [row[:] for row in mask]
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            kill = []
            for y in range(1, h - 1):
                for x in range(1, w - 1):
                    if not m[y][x]:
                        continue
                    p = (m[y - 1][x], m[y - 1][x + 1], m[y][x + 1], m[y + 1][x + 1], m[y + 1][x], m[y + 1][x - 1],
                         m[y][x - 1], m[y - 1][x - 1])
                    b = sum(p)
                    if b < 2 or b > 6:
                        continue
                    if sum(1 for i in range(8) if not p[i] and p[(i + 1) % 8]) != 1:
                        continue
                    if step == 0 and (p[0] and p[2] and p[4] or p[2] and p[4] and p[6]):
                        continue
                    if step == 1 and (p[0] and p[2] and p[6] or p[0] and p[4] and p[6]):
                        continue
                    kill.append((x, y))
            for x, y in kill:
                m[y][x] = False
            changed = changed or bool(kill)
    return m


def _longest_path(skel, w, h):
    pts = [(x, y) for y in range(h) for x in range(w) if skel[y][x]]
    if not pts:
        return []
    S = set(pts)

    def bfs(src):
        prev = {src: None}
        q = deque([src])
        last = src
        while q:
            c = q.popleft()
            last = c
            for dx, dy in N8:
                nb = (c[0] + dx, c[1] + dy)
                if nb in S and nb not in prev:
                    prev[nb] = c
                    q.append(nb)
        return last, prev

    a, _ = bfs(pts[0])
    b, prev = bfs(a)
    path, c = [], b
    while c is not None:
        path.append(c)
        c = prev[c]
    return path


def _smooth(pts, k=2):
    n = len(pts)
    out = []
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        out.append((sum(p[0] for p in pts[lo:hi]) / (hi - lo), sum(p[1] for p in pts[lo:hi]) / (hi - lo)))
    return out


def _path_from(skel, w, h, near):
    """从离 near 最近的骨架端点出发，沿骨架走到最远的那一点。"""
    pts = [(x, y) for y in range(h) for x in range(w) if skel[y][x]]
    if not pts:
        return []
    S = set(pts)
    ends = [p for p in pts if sum((p[0] + dx, p[1] + dy) in S for dx, dy in N8) == 1] or pts
    src = min(ends, key=lambda p: (p[0] - near[0]) ** 2 + (p[1] - near[1]) ** 2)
    prev = {src: None}
    q = deque([src])
    last = src
    while q:
        c = q.popleft()
        last = c
        for dx, dy in N8:
            nb = (c[0] + dx, c[1] + dy)
            if nb in S and nb not in prev:
                prev[nb] = c
                q.append(nb)
    path, c = [], last
    while c is not None:
        path.append(c)
        c = prev[c]
    return path[::-1]


def spine_of(img, start="right", thr=70, lid=False):
    """一笔（RGBA）→ 脊柱点列（从 start 那一端走向另一端）。

    lid=True（上眼睑）：眼褶有时和上眼睑连在一起，最长路径会从眼褶的尽头开始。先把笔画腐蚀两圈（细的眼褶消失、
    粗的眼睑还在），取眼睑核心在 start 一侧的最远点当起点，再沿骨架走到最远处（外眼角往下的那一段）。"""
    mask, w, h = _mask(img, thr)
    comps = _components(mask, w, h)
    if not comps:
        return []
    core = [[False] * w for _ in range(h)]
    for x, y in comps[0]:
        core[y][x] = True
    path = None
    if lid:
        er = core
        for _ in range(2):
            er = [[er[y][x] and all(er[y + dy][x + dx] for dx, dy in N8 if 0 <= y + dy < h and 0 <= x + dx < w)
                   for x in range(w)] for y in range(h)]
        pts = [(x, y) for y in range(h) for x in range(w) if er[y][x]]
        if pts:
            near = max(pts, key=lambda p: p[0]) if start == "right" else min(pts, key=lambda p: p[0])
            path = _path_from(_thin(core, w, h), w, h, near)
    if not path:
        path = _longest_path(_thin(core, w, h), w, h)
    if len(path) < 2:
        return [tuple(map(float, path[0]))] if path else []
    if not lid and (start == "right") == (path[0][0] < path[-1][0]):
        path.reverse()
    return _smooth(arc_samples(path, max(4, int(len(path) / 1.5))), 2)


def split_strokes(img, thr=60, min_px=25):
    """连通块拆开（每块连同 2 像素内的半透明边缘），按大小排序：[(RGBA, (x0,y0,x1,y1), 像素数)]。"""
    from PIL import Image

    w, h = img.size
    a = img.getchannel("A").tobytes()
    px = img.load()
    mask = [[a[y * w + x] > thr for x in range(w)] for y in range(h)]
    comps = _components(mask, w, h)
    owner = {}
    for k, c in enumerate(comps):
        for x, y in c:
            owner[(x, y)] = k
    extra = [[] for _ in comps]
    for y in range(h):
        for x in range(w):
            if 0 < a[y * w + x] <= thr:
                for r in (1, 2):
                    hit = next((owner[(x + dx, y + dy)] for dy in range(-r, r + 1) for dx in range(-r, r + 1)
                                if (x + dx, y + dy) in owner), None)
                    if hit is not None:
                        extra[hit].append((x, y))
                        break
    out = []
    for k, c in enumerate(comps):
        if len(c) < min_px:
            continue
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        p = im.load()
        for x, y in c + extra[k]:
            p[x, y] = px[x, y]
        xs = [x for x, _ in c]
        ys = [y for _, y in c]
        out.append((im, (min(xs), min(ys), max(xs), max(ys)), len(c)))
    return out


def adopt_leftovers(full, roles: dict):
    """原画里没被分到任何一笔的像素（很淡的排线、零碎笔触）：归给脊柱离得最近的那一笔。roles: 名字 → {"img", "pts"}。"""
    from PIL import ImageChops

    names = [r for r, v in roles.items() if v.get("img") is not None and v.get("pts")]
    if not names:
        return
    w, h = full.size
    fa = full.getchannel("A").tobytes()
    union = None
    for r in names:
        a = roles[r]["img"].getchannel("A")
        union = a if union is None else ImageChops.lighter(union, a)
    ua = union.tobytes()
    src = full.load()
    imgs = {r: roles[r]["img"].copy() for r in names}
    pix = {r: imgs[r].load() for r in names}
    for y in range(h):
        for x in range(w):
            i = y * w + x
            if fa[i] > 6 and ua[i] < 6:
                best = min(names, key=lambda r: min((px - x) ** 2 + (py - y) ** 2 for px, py in roles[r]["pts"]))
                pix[best][x, y] = src[x, y]
    for r in names:
        roles[r]["img"] = imgs[r]


# ====================================================================== 关键形权重
def eye_weights(open_=1.0, smile=0.0):
    o = max(0.0, min(1.3, open_))
    w = {}
    for (a, oa), (b, ob) in zip(OPEN_KEYS, OPEN_KEYS[1:]):
        if oa <= o <= ob:
            t = (o - oa) / (ob - oa)
            w[a], w[b] = 1 - t, t
            break
    s = max(0.0, min(1.0, smile))
    w = {k: v * (1 - s) for k, v in w.items() if v * (1 - s) > 1e-4}
    if s > 1e-4:
        w["smile"] = w.get("smile", 0.0) + s
    return w


def pixel_weights(w: dict) -> dict:
    """像素取哪张差分：权重最大的那张；和第二名相差小于 HANDOVER 时才交叉淡化（交接只占很短一段，不会长时间重影）。"""
    items = sorted(w.items(), key=lambda kv: -kv[1])
    if len(items) == 1:
        return {items[0][0]: 1.0}
    (k1, w1), (k2, w2) = items[0], items[1]
    d = w1 - w2
    if d >= HANDOVER:
        return {k1: 1.0}
    t = 0.5 + d / (2 * HANDOVER)
    t = t * t * (3 - 2 * t)
    return {k1: t, k2: 1 - t}


def _blend_pts(keys: dict, role: str, w: dict):
    have = {k: v for k, v in w.items() if role in keys[k]}
    tot = sum(have.values())
    if tot < 1e-6:
        return None
    n = len(keys[next(iter(have))][role]["pts"])
    return [(sum(keys[k][role]["pts"][i][0] * v for k, v in have.items()) / tot,
             sum(keys[k][role]["pts"][i][1] * v for k, v in have.items()) / tot) for i in range(n)]


class _LRU:
    def __init__(self, n):
        self.n = n
        self.d = OrderedDict()

    def get(self, k):
        v = self.d.get(k)
        if v is not None:
            self.d.move_to_end(k)
        return v

    def put(self, k, v):
        self.d[k] = v
        if len(self.d) > self.n:
            self.d.popitem(last=False)


def _add(acc, layer, f):
    from PIL import ImageChops

    lay = layer.convert("RGBa").point(lambda v: int(v * f + 0.5)) if f < 0.999 else layer.convert("RGBa")
    return lay if acc is None else ImageChops.add(acc, lay)


# ====================================================================== 眼睛
class EyeRig:
    """一只眼。side = 'l'（画面左边那只，外眼角在左）或 'r'（外眼角在右）。坐标：本眼画布（标定图坐标减去 box 左上角）。"""

    def __init__(self, side: str, box, keys: dict, iris: dict, sclera_col):
        self.side, self.box, self.keys, self.iris, self.sclera_col = side, tuple(box), keys, iris, tuple(sclera_col)
        self.size = (box[2] - box[0], box[3] - box[1])
        self.sgn = 1 if side == "l" else -1            # 外眼角方向：l 在左（x 小），r 在右
        self._cache = _LRU(160)

    # ---------------------------------------------------------------- 目标脊柱
    def targets(self, w, droop=0.0, lower=0.0):
        up = _blend_pts(self.keys, "upper", w)
        lo = _blend_pts(self.keys, "lower", w)
        cr = _blend_pts(self.keys, "crease", w)
        if abs(droop) > 1e-3:                           # 上眼睑绕内眼角转：正 = 外眼角下垂
            deg = droop * 12.0 * self.sgn * -1
            up = rotate(up, up[0], deg)
            if cr:
                cr = rotate(cr, up[0], deg * 0.6)
        if lower > 1e-3 and lo:                         # 下眼睑往上抬（中间抬得多，两端少）
            n = len(lo)
            lift = lower * 14.0
            lo = [(x, y - lift * math.sin(math.pi * (i + 0.5) / n) ** 0.7) for i, (x, y) in enumerate(lo)]
        return {"upper": up, "lower": lo, "crease": cr}

    # ---------------------------------------------------------------- 绘制
    def layers(self, open_=1.0, smile=0.0, droop=0.0, lower=0.0):
        """→ (线稿 RGBA, 眼白 L, 小虹膜权重)。按量化后的参数缓存。"""
        q = lambda v, s: round(v / s) * s  # noqa: E731
        key = (q(open_, 0.05), q(smile, 0.05), q(droop, 0.1), q(lower, 0.1))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = self._render(*key)
        self._cache.put(key, out)
        return out

    def _render(self, open_, smile, droop, lower):
        from PIL import Image

        w = {k: v for k, v in eye_weights(open_, smile).items() if k in self.keys}
        if not w:
            w = {next(iter(self.keys)): 1.0}
        tg = self.targets(w, droop, lower)
        acc = None
        for role in ("upper", "lower", "crease"):
            pts = tg[role]
            if pts is None:
                continue
            real = {k: v for k, v in w.items() if self.keys[k].get(role) and self.keys[k][role].get("img") is not None}
            if not real:
                continue
            fade = min(1.0, sum(real.values()) / 0.35)  # 只有部分差分有的笔画：快合到没有它的那张时才淡出（已被盖住）
            for k, f in pixel_weights(real).items():
                kr = self.keys[k][role]
                if f * fade < 0.004:
                    continue
                acc = _add(acc, transport(kr["img"], kr["pts"], pts, kr["reach"], self.size), f * fade)
        lines = acc.convert("RGBA") if acc is not None else Image.new("RGBA", self.size, (0, 0, 0, 0))
        mask = self._sclera(w, tg)
        return lines, mask, w.get("wide", 0.0) / max(1e-6, sum(w.values()))

    def _sclera(self, w, tg):
        """有眼白的差分：把手绘眼白逐列重排 —— 上沿跟着上眼睑的位移，下沿跟着下眼睑的位移。"""
        from PIL import Image, ImageChops

        have = {k: v for k, v in w.items() if self.keys[k].get("sclera") is not None}
        if sum(have.values()) < 1e-4:
            have = {"half": 1.0}
        up_t = tg["upper"][:K_ARC]
        lo_t = tg["lower"]
        acc = None
        for k, f in pixel_weights(have).items():
            kk = self.keys[k]
            a = kk["sclera"]
            bb = a.getbbox()
            if bb is None:
                continue
            up_k = kk["upper"]["pts"][:K_ARC]
            lo_k = kk["lower"]["pts"]
            col = a.load()
            mesh = []
            for x in range(bb[0], bb[2]):
                ys = [y for y in range(bb[1], bb[3]) if col[x, y] > 8]
                if not ys:
                    continue
                top_k, bot_k = ys[0], ys[-1] + 1
                top_t = top_k + (y_at(up_t, x) - y_at(up_k, x))
                bot_t = bot_k + (y_at(lo_t, x) - y_at(lo_k, x))
                if bot_t - top_t < 0.5:
                    continue
                mesh.append(((x, int(math.floor(top_t)), x + 1, int(math.ceil(bot_t))),
                             (x, top_k, x, bot_k, x + 1, bot_k, x + 1, top_k)))
            if not mesh:
                continue
            m = a.transform(self.size, Image.MESH, mesh, resample=Image.BILINEAR)
            if f < 0.999:
                m = m.point(lambda v, f=f: int(v * f + 0.5))
            acc = m if acc is None else ImageChops.add(acc, m)
        return acc if acc is not None else Image.new("L", self.size, 0)


# ====================================================================== 眉毛
class BrowRig:
    def __init__(self, side: str, box, keys: dict, inner_sign: int):
        self.side, self.box, self.keys = side, tuple(box), keys
        self.size = (box[2] - box[0], box[3] - box[1])
        self.inner = inner_sign
        self._cache = _LRU(96)

    def layer(self, open_, smile, dy, angle):
        q = lambda v, s: round(v / s) * s  # noqa: E731
        key = (q(open_, 0.05), q(smile, 0.05), round(dy), q(angle, 0.1))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        from PIL import Image

        w = eye_weights(key[0], key[1])
        have = {k: v for k, v in w.items() if self.keys.get(k)}
        if not have:
            have = {"open": 1.0}
        tot = sum(have.values())
        n = len(self.keys[next(iter(have))]["pts"])
        pts = [(sum(self.keys[k]["pts"][i][0] * v for k, v in have.items()) / tot,
                sum(self.keys[k]["pts"][i][1] * v for k, v in have.items()) / tot + key[2]) for i in range(n)]
        if abs(key[3]) > 1e-3:
            cx = sum(p[0] for p in pts) / n
            cy = sum(p[1] for p in pts) / n
            pts = rotate(pts, (cx, cy), key[3] * 14.0 * self.inner)   # 正 = 眉头（内侧）往下压，生气的八字反过来
        acc = None
        for k, f in pixel_weights(have).items():
            kb = self.keys[k]
            acc = _add(acc, transport(kb["img"], kb["pts"], pts, kb["reach"], self.size), f)
        out = acc.convert("RGBA") if acc is not None else Image.new("RGBA", self.size, (0, 0, 0, 0))
        self._cache.put(key, out)
        return out


# ====================================================================== 嘴
class MouthRig:
    """画师那张“笑着张开的嘴”：上唇线（嘴角到嘴角）+ 下轮廓（嘴角兜到底再回另一个嘴角），连成闭合环一起搬运。"""

    def __init__(self, box, top, bot, fill_col, top_y):
        self.box = tuple(box)
        self.size = (box[2] - box[0], box[3] - box[1])
        self.top, self.bot = top, bot                    # {"img", "pts"(左→右 / 右→左)}
        self.fill = tuple(fill_col)
        self.loop = top["pts"] + bot["pts"]              # 上唇左→右，接下轮廓右→左，闭合
        self.r_top = reach_of(top["img"], self.loop, closed=True)
        self.r_bot = reach_of(bot["img"], self.loop, closed=True)
        xs = [p[0] for p in top["pts"]]
        self.cx = (min(xs) + max(xs)) / 2
        self.half = max(4.0, (max(xs) - min(xs)) / 2)
        self.top_y = top_y                               # 原画里上唇线的高度（本画布坐标）
        self._cache = _LRU(96)

    def targets(self, width, open_, curve, skew):
        cx, half = self.cx, self.half
        tp = self.top["pts"]

        def lift(x):
            u = (x - cx) / half
            return -curve * 7.0 * u * u - skew * 5.0 * u

        top = [(cx + (x - cx) * width, y + lift(x)) for x, y in tp]
        bot = []
        for x, y in self.bot["pts"]:
            d = y - y_at(tp, x)
            bot.append((cx + (x - cx) * width, y_at(tp, x) + lift(x) + d * open_))
        return top, bot

    def layer(self, width=1.0, open_=1.0, curve=0.0, skew=0.0, inner=""):
        q = lambda v, s: round(v / s) * s  # noqa: E731
        key = (q(width, 0.05), q(open_, 0.05), q(curve, 0.1), q(skew, 0.1), inner)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = self._render(*key)
        self._cache.put(key, out)
        return out

    def _render(self, width, open_, curve, skew, inner):
        from PIL import Image, ImageDraw

        top_t, bot_t = self.targets(width, open_, curve, skew)
        loop_t = top_t + bot_t
        size = self.size
        out = Image.new("RGBA", size, (0, 0, 0, 0))
        if open_ > 0.04:
            ss = 3
            m = Image.new("L", (size[0] * ss, size[1] * ss), 0)
            ImageDraw.Draw(m).polygon([(x * ss, y * ss) for x, y in loop_t], fill=255)
            m = m.resize(size, Image.BOX)
            fill = Image.new("RGBA", size, self.fill + (255,))
            if inner in ("teeth", "tongue"):
                d = ImageDraw.Draw(fill)
                h = max(y for _, y in bot_t) - min(y for _, y in top_t)
                if inner == "teeth":                     # 上排牙：沿上唇线下方一条白带
                    band = [(x, y + 0.5) for x, y in top_t] + [(x, y + max(2.0, h * 0.28)) for x, y in top_t[::-1]]
                    d.polygon(band, fill=(252, 250, 246, 255))
                else:                                    # 舌头：嘴里下半部分一块深一点的粉
                    ys = [y for _, y in bot_t]
                    cy = max(ys) - h * 0.28
                    rx = abs(top_t[-1][0] - top_t[0][0]) * 0.3
                    d.ellipse([self.cx - rx, cy - h * 0.22, self.cx + rx, cy + h * 0.3], fill=(214, 96, 110, 255))
            fill.putalpha(m)
            out.alpha_composite(fill)
        n, nt = len(self.loop), len(self.top["pts"])
        top_allow = set(range(0, nt + 3)) | set(range(n - 3, n))      # 上唇线 + 两个嘴角外各 3 个点（嘴角处无缝）
        bot_allow = set(range(nt - 3, n)) | {0, 1, 2}
        lines = transport(self.top["img"], self.loop, loop_t, self.r_top, size, closed=True, allow=top_allow)
        fade = min(1.0, open_ / 0.15)                    # 合上时下轮廓贴在上唇线上，淡掉（不然线会变粗）
        acc = _add(None, lines, 1.0)
        if fade > 0.01:
            acc = _add(acc, transport(self.bot["img"], self.loop, loop_t, self.r_bot, size, closed=True, allow=bot_allow),
                       fade)
        out.alpha_composite(acc.convert("RGBA"))
        return out


class ClosedMouthRig:
    """合着的嘴：画师在半睁那张里画的小嘴（一笔，左端带个往下的小钩）。
    把这一笔拉直（保留一半原来的弯度和小钩），再按嘴角弯成笑 / 撇嘴、按嘴宽伸缩、按歪嘴倾斜 —— 仍是原像素。"""

    KEEP = 0.5               # 保留原笔画多少弯度（小钩是画风的一部分）

    def __init__(self, box, stroke):
        self.box = tuple(box)
        self.size = (box[2] - box[0], box[3] - box[1])
        self.img, self.pts = stroke["img"], stroke["pts"]
        self.reach = reach_of(self.img, self.pts)
        p0, p1 = self.pts[0], self.pts[-1]
        self.L = max(4.0, math.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        self.center = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
        e1 = ((p1[0] - p0[0]) / self.L, (p1[1] - p0[1]) / self.L)
        e2 = (-e1[1], e1[0])
        self.uv = [(((x - p0[0]) * e1[0] + (y - p0[1]) * e1[1]) / self.L, (x - p0[0]) * e2[0] + (y - p0[1]) * e2[1])
                   for x, y in self.pts]
        self._cache = _LRU(64)

    def layer(self, width=1.0, curve=0.0, skew=0.0):
        q = lambda v, s: round(v / s) * s  # noqa: E731
        key = (q(width, 0.05), q(curve, 0.1), q(skew, 0.1))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        wl, c, sk = key
        L2 = self.L * 1.25 * wl
        A = max(2.5, L2 * 0.2)
        cx, cy = self.center
        pts = []
        for u, d in self.uv:
            t = 2 * u - 1
            pts.append((cx + (u - 0.5) * L2, cy + d * self.KEEP - c * A * (t * t - 0.4) - sk * A * 0.7 * t))
        out = transport(self.img, self.pts, pts, self.reach, self.size)
        self._cache.put(key, out)
        return out


# ====================================================================== 整张脸：构建 / 读写预计算
def _manifest_hash(folder) -> str:
    with open(os.path.join(folder, "manifest.json"), "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:16]


def _corr_upper(pts, sgn):
    c = (min if sgn > 0 else max)(range(len(pts)), key=lambda i: pts[i][0])
    arc = arc_samples(pts[:c + 1], K_ARC)
    tail = arc_samples(pts[c:], K_TAIL) if c < len(pts) - 1 else [pts[c]] * K_TAIL
    return arc + tail


def _union_box(m, side, pad=18):
    boxes = []
    for st in m["states"].values():
        for k in (f"eye_{side}", f"sclera_{side}"):
            e = st.get(k)
            if e:
                boxes.append((e["origin"][0], e["origin"][1], e["origin"][0] + e["size"][0], e["origin"][1] + e["size"][1]))
    return (min(b[0] for b in boxes) - pad, min(b[1] for b in boxes) - pad,
            max(b[2] for b in boxes) + pad, max(b[3] for b in boxes) + pad)


def _on_canvas(folder, ent, box):
    from PIL import Image

    im = Image.open(os.path.join(folder, ent["file"])).convert("RGBA")
    out = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    out.alpha_composite(im, (ent["origin"][0] - box[0], ent["origin"][1] - box[1]))
    return out


def build(folder: str) -> dict:
    """从 face_layers 的手绘 PNG 提取全部脊柱。返回可直接保存 / 使用的描述（图像是 PIL 对象）。"""
    from PIL import Image

    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    data = {"format": FORMAT, "manifest": _manifest_hash(folder), "eyes": {}, "brows": {}, "mouth": None}
    for side, sgn in (("l", 1), ("r", -1)):
        box = _union_box(m, side)
        start = "right" if sgn > 0 else "left"          # 从内眼角那一端开始
        keys = {}
        for st in STATES:
            ent = m["states"].get(st, {})
            if not ent.get(f"eye_{side}"):
                continue
            full = _on_canvas(folder, ent[f"eye_{side}"], box)
            sclera = _on_canvas(folder, ent[f"sclera_{side}"], box).getchannel("A") if ent.get(f"sclera_{side}") else None
            parts = split_strokes(full)
            if not parts:
                continue
            sb = sclera.getbbox() if sclera is not None else None
            up_img, ub, _ = parts[0]
            roles = {"upper": {"img": up_img}}
            bits = []
            for im, bb, n in parts[1:]:
                cy = (bb[1] + bb[3]) / 2
                if sb is not None and cy > (sb[1] + sb[3]) / 2 and n > 60 and "lower" not in roles:
                    roles["lower"] = {"img": im}
                elif cy < ub[1] + (ub[3] - ub[1]) * 0.35 and n > 40 and "crease" not in roles:
                    roles["crease"] = {"img": im}
                else:
                    bits.append(im)
            for b in bits:
                roles["upper"]["img"] = Image.alpha_composite(roles["upper"]["img"], b)
            roles["upper"]["pts"] = _corr_upper(spine_of(roles["upper"]["img"], start, lid=True), sgn)
            for r in ("lower", "crease"):
                if r in roles:
                    roles[r]["pts"] = arc_samples(spine_of(roles[r]["img"], start), K_LINE)
            adopt_leftovers(full, roles)
            if "lower" not in roles:                     # 虚拟下眼睑：闭合线内侧一段，没有像素
                arc = roles["upper"]["pts"][:K_ARC]
                nn = max(2, int(len(arc) * LOWER_SPAN))
                roles["lower"] = {"img": None, "pts": arc_samples(arc[:nn], K_LINE)}
            for r, v in roles.items():
                v["reach"] = reach_of(v["img"], v["pts"]) if v.get("img") is not None else 0.0
            roles["sclera"] = sclera
            keys[st] = roles
        iris = {}
        for name, sides in m.get("iris", {}).items():
            ent = sides.get(side)
            if ent:
                iris[name] = {"body": _on_canvas(folder, ent["body"], box), "hl": _on_canvas(folder, ent["highlight"], box)
                              if ent.get("highlight") else None, "center": [ent["center"][0] - box[0], ent["center"][1] - box[1]],
                              "radius": ent["radius"]}
        col = (246, 242, 242)
        sc_ent = m["states"].get("open", {}).get(f"sclera_{side}")
        if sc_ent:
            im = Image.open(os.path.join(folder, sc_ent["file"])).convert("RGBA")
            px = sorted((c[:3] for c in im.getdata() if c[3] > 200), key=sum)
            if px:
                col = px[len(px) // 2]
        data["eyes"][side] = {"box": list(box), "keys": keys, "iris": iris, "sclera_col": list(col)}
        # 眉毛：各状态那张的眉毛
        bboxes = []
        for st in STATES:
            e = m["states"].get(st, {}).get(f"brow_{side}")
            if e:
                bboxes.append((e["origin"][0], e["origin"][1], e["origin"][0] + e["size"][0], e["origin"][1] + e["size"][1]))
        if bboxes:
            bbox = (min(b[0] for b in bboxes) - 24, min(b[1] for b in bboxes) - 30, max(b[2] for b in bboxes) + 24,
                    max(b[3] for b in bboxes) + 30)
            bk = {}
            for st in STATES:
                e = m["states"].get(st, {}).get(f"brow_{side}")
                if not e:
                    continue
                img = _on_canvas(folder, e, bbox)
                pts = arc_samples(spine_of(img, start), K_BROW)
                if len(pts) >= 2:
                    bk[st] = {"img": img, "pts": pts, "reach": reach_of(img, pts)}
            if bk:
                data["brows"][side] = {"box": list(bbox), "keys": bk}
    # 嘴
    ent = m.get("mouths", {}).get("open")
    if ent:
        x0, y0 = ent["origin"]
        box = (x0 - 30, y0 - 22, x0 + ent["size"][0] + 30, y0 + ent["size"][1] + 40)
        data["mouth"] = _build_mouth(_on_canvas(folder, ent, box), box)
    ent = m.get("mouths", {}).get("small") or m.get("mouths", {}).get("dot")
    if ent:
        x0, y0 = ent["origin"]
        box = (x0 - 22, y0 - 16, x0 + ent["size"][0] + 22, y0 + ent["size"][1] + 16)
        img = _on_canvas(folder, ent, box)
        pts = arc_samples(spine_of(img, "left"), 16)
        if len(pts) >= 2:
            data["closed_mouth"] = {"box": list(box), "img": img, "pts": pts}
    return data


def _build_mouth(img, box):
    from PIL import Image

    w, h = img.size
    px = img.load()
    ink = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ip = ink.load()
    fills = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            if r + g + b < 360:
                ip[x, y] = (r, g, b, a)
            elif a > 200:
                fills.append((r, g, b))
    fills.sort(key=sum)
    fill = fills[len(fills) // 2] if fills else (230, 137, 123)
    a = ink.getchannel("A").tobytes()
    bb = ink.getchannel("A").point(lambda v: 255 if v > 120 else 0).getbbox()
    band = [(x, y) for y in range(bb[1], bb[1] + max(3, int((bb[3] - bb[1]) * 0.3))) for x in range(w) if a[y * w + x] > 120]
    left, right = min(band), max(band)
    ycut = max(left[1], right[1]) + 3
    corner_y = min(left[1], right[1])
    top0 = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bot0 = Image.new("RGBA", img.size, (0, 0, 0, 0))
    t0, b0 = top0.load(), bot0.load()
    for y in range(h):
        for x in range(w):
            c = ip[x, y]
            if c[3]:
                (t0 if (y <= ycut and left[0] + 2 <= x <= right[0] - 2) else b0)[x, y] = c
    tp, bp = spine_of(top0, "left"), spine_of(bot0, "right")
    # 按离哪条脊柱近重新分：嘴角往下的小段竖线和小钩归下轮廓（合嘴时跟着收起来）
    top = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bot = Image.new("RGBA", img.size, (0, 0, 0, 0))
    tt, bt = top.load(), bot.load()
    for y in range(h):
        for x in range(w):
            c = ip[x, y]
            if not c[3]:
                continue
            dt = min((p[0] - x) ** 2 + (p[1] - y) ** 2 for p in tp)
            db = min((p[0] - x) ** 2 + (p[1] - y) ** 2 for p in bp)
            below = y > y_at(tp, x) + 2.5 or ((x <= left[0] + 3 or x >= right[0] - 3) and y > corner_y + 1.5)
            (bt if (db < dt or below) else tt)[x, y] = c
    tpts = arc_samples(spine_of(top, "left"), K_MTOP)        # 上唇：左 → 右
    bpts = arc_samples(spine_of(bot, "right"), K_MBOT)       # 下轮廓：右 → 左（接成闭合环）
    return {"box": list(box), "top": {"img": top, "pts": tpts}, "bot": {"img": bot, "pts": bpts}, "fill": list(fill),
            "top_y": min(p[1] for p in tpts)}


def save(data: dict, folder: str) -> str:
    """把 build() 的结果写成 face_layers/strokes.json + strokes/*.png。"""
    sdir = os.path.join(folder, "strokes")
    os.makedirs(sdir, exist_ok=True)
    out = {"format": data["format"], "manifest": data["manifest"], "eyes": {}, "brows": {}, "mouth": None}

    def put(img, name, mode=None):
        if img is None:
            return None
        fn = name + ".png"
        (img if mode is None else img.convert(mode)).save(os.path.join(sdir, fn), optimize=True)
        return "strokes/" + fn

    for side, e in data["eyes"].items():
        keys = {}
        for st, roles in e["keys"].items():
            kd = {}
            for r, v in roles.items():
                if r == "sclera":
                    kd["sclera"] = put(v, f"eye_{side}_{st}_sclera")
                else:
                    kd[r] = {"img": put(v.get("img"), f"eye_{side}_{st}_{r}"), "pts": v["pts"], "reach": v["reach"]}
            keys[st] = kd
        iris = {name: {"body": put(v["body"], f"iris_{name}_{side}"), "hl": put(v["hl"], f"iris_{name}_{side}_hl"),
                       "center": v["center"], "radius": v["radius"]} for name, v in e["iris"].items()}
        out["eyes"][side] = {"box": e["box"], "keys": keys, "iris": iris, "sclera_col": e["sclera_col"]}
    for side, b in data["brows"].items():
        out["brows"][side] = {"box": b["box"], "keys": {st: {"img": put(v["img"], f"brow_{side}_{st}"), "pts": v["pts"],
                                                             "reach": v["reach"]} for st, v in b["keys"].items()}}
    if data["mouth"]:
        mo = data["mouth"]
        out["mouth"] = {"box": mo["box"], "fill": mo["fill"], "top_y": mo["top_y"],
                        "top": {"img": put(mo["top"]["img"], "mouth_top"), "pts": mo["top"]["pts"]},
                        "bot": {"img": put(mo["bot"]["img"], "mouth_bot"), "pts": mo["bot"]["pts"]}}
    if data.get("closed_mouth"):
        cm = data["closed_mouth"]
        out["closed_mouth"] = {"box": cm["box"], "img": put(cm["img"], "mouth_closed"), "pts": cm["pts"]}
    path = os.path.join(folder, "strokes.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    return path


def _load_saved(folder: str):
    from PIL import Image

    path = os.path.join(folder, "strokes.json")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("format") != FORMAT or d.get("manifest") != _manifest_hash(folder):
        return None

    def img(rel, mode="RGBA"):
        return Image.open(os.path.join(folder, rel)).convert(mode) if rel else None

    tup = lambda pts: [tuple(p) for p in pts]  # noqa: E731
    for side, e in d["eyes"].items():
        for st, roles in e["keys"].items():
            for r, v in list(roles.items()):
                if r == "sclera":
                    roles["sclera"] = img(v, "L")
                else:
                    v["img"] = img(v["img"])
                    v["pts"] = tup(v["pts"])
        for v in e["iris"].values():
            v["body"], v["hl"] = img(v["body"]), img(v["hl"])
    for b in d["brows"].values():
        for v in b["keys"].values():
            v["img"], v["pts"] = img(v["img"]), tup(v["pts"])
    if d.get("mouth"):
        for k in ("top", "bot"):
            d["mouth"][k]["img"] = img(d["mouth"][k]["img"])
            d["mouth"][k]["pts"] = tup(d["mouth"][k]["pts"])
    if d.get("closed_mouth"):
        d["closed_mouth"]["img"] = img(d["closed_mouth"]["img"])
        d["closed_mouth"]["pts"] = tup(d["closed_mouth"]["pts"])
    return d


_SHARED: dict = {}
_SHARED_LOCK = threading.Lock()


class MorphFace:
    """整张脸的连续变形模型：eyes[0/1]、brows[0/1]、mouth。坐标是标定图坐标（各部位画布的 box 左上角为原点）。"""

    @classmethod
    def shared(cls, folder: str, warm: bool = True):
        """同一套素材只建一份（半身、特写等各个视图共用，缓存也共用）；第一次建好后在后台预热常用状态。"""
        key = os.path.abspath(folder)
        with _SHARED_LOCK:
            if key in _SHARED:
                return _SHARED[key]
            face = cls.load(folder)
            _SHARED[key] = face
        if face is not None and warm:
            face.stop = threading.Event()
            threading.Thread(target=face.warmup, args=(face.stop,), name="morph-warmup", daemon=True).start()
        return face

    def __init__(self, data: dict):
        self.eyes = []
        for side in ("l", "r"):
            e = data["eyes"].get(side)
            self.eyes.append(EyeRig(side, e["box"], e["keys"], e["iris"], e["sclera_col"]) if e and e["keys"] else None)
        self.brows = []
        for i, side in enumerate(("l", "r")):
            b = data["brows"].get(side)
            self.brows.append(BrowRig(side, b["box"], b["keys"], 1 if i == 0 else -1) if b else None)
        mo = data.get("mouth")
        self.mouth = MouthRig(mo["box"], mo["top"], mo["bot"], mo["fill"], mo["top_y"]) if mo else None
        cm = data.get("closed_mouth")
        self.closed = ClosedMouthRig(cm["box"], cm) if cm else None
        self.lock = threading.RLock()
        self.stop = None

    @classmethod
    def load(cls, folder: str):
        """优先读预计算；没有（或和 manifest 对不上）就现算一份（约 1 秒）。失败返回 None。"""
        try:
            data = _load_saved(folder) if os.path.exists(os.path.join(folder, "strokes.json")) else None
        except Exception:
            log.exception("字符画：读取预计算的笔画脊柱失败，改为现算")
            data = None
        if data is None:
            try:
                import time

                t0 = time.perf_counter()
                data = build(folder)
                log.info("字符画：没有预计算的笔画脊柱（tools/build_face_strokes.py），现算用了 %.1f 秒",
                         time.perf_counter() - t0)
            except Exception:
                log.exception("字符画：提取笔画脊柱失败，退回分档的手绘差分")
                return None
        return cls(data)

    def warmup(self, stop=None) -> int:
        """后台预先算好常用的眼睛开合、笑眼、说话时的嘴（之后切表情、眨眼、说话都直接命中缓存）。"""
        n = 0
        plans = [(o / 20, 0.0) for o in range(0, 27)] + [(1.0, s / 10) for s in range(1, 11)] + \
            [(o / 10, 0.35) for o in range(4, 12)]
        for open_, smile in plans:
            for eye in self.eyes:
                if stop is not None and stop.is_set():
                    return n
                if eye is not None:
                    with self.lock:
                        eye.layers(open_, smile)
                    n += 1
        if self.mouth is not None:
            for mo in range(0, 21):
                if stop is not None and stop.is_set():
                    return n
                with self.lock:
                    self.mouth.layer(1.0, mo / 20, 0.2)
                n += 1
        return n
