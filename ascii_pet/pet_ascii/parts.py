"""可创造的表情组件与可编辑骨架（v0.7）。

组件（component）是 DeepSeek 用“绘图指令”设计的新五官或装饰。它是一段**数据**（不是代码），
经过本模块校验、钳制后由本地按原画的笔触和配色绘制，再走与身体相同的字符化流程：
  {"slot":"eye", "style":"quirky", "mirror":true, "blink":"builtin",
   "draw":[{"t":"ellipse","c":[0,0],"r":[0.9,0.7],"fill":"sclera","stroke":"ink","w":0.9}, ...],
   "glyphs":{"rows":["> <"],"color":"#ff8cc0"}}
- 坐标在节点自己的框里：x、y 都是 -1~1（超出一点可以到 ±2.5）；y 向下为正。
  左右成对的部位（眼、眉、腮）+x 指向脸的中线，右边那只自动镜像，所以只设计一只。
- 图元：ellipse 椭圆 / line 折线 / poly 多边形 / arc 弧线 / rect 矩形 / star 星形 / heart 心形 / dots 圆点。
- 颜色：原画调色板里的角色名（ink 墨线、sclera 眼白、iris…、mouth、blush、tear、star、heart、skin 肤色…）或 #rrggbb。
- glyphs：直接叠在字符画上的字符纹理（ASCII），用来做 > <、TT、@@ 这类字符化的五官。

骨架：原来固定的五官位置升级为节点（eye_l、eye_r、brow_l、brow_r、mouth、cheek_l、cheek_r、forehead、
sweat，以及只作锚点的 nose、chin、face）。表情规格里可以带三组可选字段：
  "parts": {"eye_l":"星屑眼", "mouth":"none"}           某个节点改用组件 / 隐藏 / 恢复内置（"builtin"）
  "nodes": {"mouth":{"dx":0,"dy":0.05,"sx":1.3,"sy":1,"rot":-10}}   移动（脸宽/脸高的比例）、缩放、旋转
  "extra": [{"id":"third_eye","x":0.5,"y":0.12,"w":0.07,"h":0.06,"part":"竖瞳","rot":0}]  新增节点（脸框内归一化坐标）
骨架操作（实验里由模型提出）：move / scale / rotate / replace / add / remove / group / reset，结果仍落到上面三组字段，
全部经过钳制；默认骨架始终可用（没有这些字段的规格和以前完全一样）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

SLOTS = ("eye", "mouth", "brow", "cheek", "forehead", "nose", "accessory", "free")
STYLES = ("natural", "exaggerated", "abstract", "quirky", "cute", "minimal")
STYLE_CN = {"natural": "自然", "exaggerated": "夸张", "abstract": "抽象", "quirky": "古怪", "cute": "可爱", "minimal": "极简"}
PRIMS = ("ellipse", "line", "poly", "arc", "rect", "star", "heart", "dots")
NAME_RE = re.compile(r"^[\w㐀-鿿぀-ヿ·\-]{1,12}$")
EXTRA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")

CORE_NODES = ("eye_l", "eye_r", "brow_l", "brow_r", "mouth", "cheek_l", "cheek_r", "forehead", "sweat")
ANCHOR_NODES = ("nose", "chin", "face")
NODE_SLOT = {"eye_l": "eye", "eye_r": "eye", "brow_l": "brow", "brow_r": "brow", "mouth": "mouth", "cheek_l": "cheek",
             "cheek_r": "cheek", "forehead": "forehead", "sweat": "accessory"}

LIMITS = {"prims": 32, "points": 24, "coord": 2.5, "glyph_rows": 6, "glyph_cols": 16, "extra": 8,
          "move": 0.5, "scale": (0.3, 3.0), "rot": 90.0, "extra_size": (0.02, 0.6)}


# ---------------------------------------------------------------------- 校验
def _num(v, lo, hi, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                                   # NaN
        return default
    return max(lo, min(hi, f))


def _pt(v, lim: float = LIMITS["coord"]):
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        x, y = _num(v[0], -lim, lim), _num(v[1], -lim, lim)
        if x is not None and y is not None:
            return [round(x, 3), round(y, 3)]
    return None


def color_ok(v, roles) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    if v in roles or v == "skin":
        return v
    if re.fullmatch(r"#[0-9a-fA-F]{6}", v):
        return v.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        return "#" + "".join(c * 2 for c in v[1:]).lower()
    return None


def _clean_prim(raw: dict, roles, warnings: list) -> dict | None:
    t = str(raw.get("t") or raw.get("type") or "").lower()
    if t not in PRIMS:
        warnings.append(f"未知图元 {t!r}，已忽略")
        return None
    p = {"t": t}
    for k in ("fill", "stroke"):
        if k in raw:
            c = color_ok(raw.get(k), roles)
            if c:
                p[k] = c
            elif raw.get(k) not in (None, "", "none"):
                warnings.append(f"{t}: 不认识的颜色 {raw.get(k)!r}，已忽略")
    p["w"] = round(_num(raw.get("w"), 0.15, 2.5, 0.7), 3)
    p["a"] = round(_num(raw.get("a"), 0.05, 1.0, 1.0), 3)
    if t in ("ellipse", "arc"):
        c = _pt(raw.get("c")) or [0.0, 0.0]
        r = raw.get("r")
        if isinstance(r, (int, float)):
            r = [r, r]
        r = _pt(r) if isinstance(r, (list, tuple)) else None
        if not r:
            warnings.append(f"{t}: 缺少半径 r")
            return None
        p["c"], p["r"] = c, [max(0.02, abs(r[0])), max(0.02, abs(r[1]))]
        if t == "arc":
            p["a0"] = round(_num(raw.get("a0"), -360, 720, 180.0), 1)
            p["a1"] = round(_num(raw.get("a1"), -360, 720, 360.0), 1)
    elif t in ("line", "poly", "dots"):
        pts = [q for q in (_pt(v) for v in (raw.get("pts") or [])[:LIMITS["points"]]) if q]
        need = {"line": 2, "poly": 3, "dots": 1}[t]
        if len(pts) < need:
            warnings.append(f"{t}: 点数不够（至少 {need} 个）")
            return None
        p["pts"] = pts
        if t == "dots":
            p["r"] = round(_num(raw.get("r"), 0.01, 0.6, 0.08), 3)
        if t == "line" and raw.get("closed"):
            p["closed"] = True
    elif t == "rect":
        b = raw.get("box")
        if not (isinstance(b, (list, tuple)) and len(b) >= 4):
            warnings.append("rect: 缺少 box")
            return None
        q0, q1 = _pt(b[:2]), _pt(b[2:4])
        if not q0 or not q1:
            return None
        p["box"] = [min(q0[0], q1[0]), min(q0[1], q1[1]), max(q0[0], q1[0]), max(q0[1], q1[1])]
    elif t in ("star", "heart"):
        p["c"] = _pt(raw.get("c")) or [0.0, 0.0]
        p["r"] = round(_num(raw.get("r"), 0.05, 2.5, 0.5), 3)
        if t == "star":
            p["n"] = int(_num(raw.get("n"), 3, 8, 5))
    if t in ("line", "arc") and "stroke" not in p:
        p["stroke"] = "ink"
    if t not in ("line", "arc") and "fill" not in p and "stroke" not in p:
        p["fill"] = {"star": "star", "heart": "heart"}.get(t, "ink")
    return p


def clean_component(raw, roles, warnings: list | None = None) -> dict | None:
    """校验并钳制一个组件规格；无效时返回 None（原因写进 warnings）。"""
    warnings = warnings if warnings is not None else []
    if not isinstance(raw, dict):
        warnings.append("组件不是对象")
        return None
    slot = str(raw.get("slot") or "free").lower()
    if slot not in SLOTS:
        warnings.append(f"未知部位 {slot!r}，按 free 处理")
        slot = "free"
    style = str(raw.get("style") or "natural").lower()
    if style not in STYLES:
        style = "natural"
    comp = {"slot": slot, "style": style,
            "mirror": bool(raw.get("mirror", slot in ("eye", "brow", "cheek"))),
            "blink": "keep" if str(raw.get("blink", "builtin")).lower() == "keep" else "builtin",
            "talk": "keep" if str(raw.get("talk", "bob")).lower() == "keep" else "bob"}
    draw = []
    for r in (raw.get("draw") or [])[:LIMITS["prims"]]:
        if isinstance(r, dict):
            p = _clean_prim(r, roles, warnings)
            if p:
                draw.append(p)
    if len(raw.get("draw") or []) > LIMITS["prims"]:
        warnings.append(f"图元超过 {LIMITS['prims']} 个，已截断")
    comp["draw"] = draw
    g = raw.get("glyphs")
    if isinstance(g, dict) and isinstance(g.get("rows"), list):
        from .grid import sanitize_char

        rows = []
        for row in g["rows"][:LIMITS["glyph_rows"]]:
            s = "".join(sanitize_char(ch) for ch in str(row)[:LIMITS["glyph_cols"]])
            rows.append(s)
        while rows and not rows[-1].strip():
            rows.pop()
        if any(r.strip() for r in rows):
            comp["glyphs"] = {"rows": rows, "color": color_ok(g.get("color"), roles) or "ink",
                              "dx": int(_num(g.get("dx"), -6, 6, 0)), "dy": int(_num(g.get("dy"), -4, 4, 0))}
    if not draw and "glyphs" not in comp:
        warnings.append("组件没有任何有效的图元或字符")
        return None
    return comp


def digest(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]


def valid_name(name) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name)) and name not in ("builtin", "none")


# ---------------------------------------------------------------------- 表情规格里的骨架字段
RIG_KEYS = ("parts", "nodes", "extra")


def clean_rig_fields(raw: dict) -> dict:
    """校验表情规格里的 parts / nodes / extra，返回只含非空字段的字典（全部钳制在骨架约束内）。"""
    out = {}
    extra = []
    ids = set()
    for e in (raw.get("extra") or [])[:LIMITS["extra"]] if isinstance(raw.get("extra"), list) else []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "").lower()
        part = e.get("part")
        if not EXTRA_ID_RE.match(eid) or eid in CORE_NODES or eid in ANCHOR_NODES or eid in ids or not valid_name(part):
            continue
        lo, hi = LIMITS["extra_size"]
        item = {"id": eid, "x": round(_num(e.get("x"), -0.2, 1.2, 0.5), 3), "y": round(_num(e.get("y"), -0.2, 1.2, 0.5), 3),
                "w": round(_num(e.get("w"), lo, hi, 0.08), 3), "h": round(_num(e.get("h"), lo, hi, 0.08), 3), "part": part}
        rot = _num(e.get("rot"), -LIMITS["rot"], LIMITS["rot"], 0.0)
        if abs(rot) > 0.5:
            item["rot"] = round(rot, 1)
        if e.get("flip"):
            item["flip"] = True
        ids.add(eid)
        extra.append(item)
    if extra:
        out["extra"] = extra
    known = set(CORE_NODES) | ids
    parts = {}
    if isinstance(raw.get("parts"), dict):
        for node, v in raw["parts"].items():
            if node in CORE_NODES and (v in ("builtin", "none") or valid_name(v)):
                if v != "builtin":
                    parts[node] = v
    if parts:
        out["parts"] = parts
    nodes = {}
    if isinstance(raw.get("nodes"), dict):
        lo, hi = LIMITS["scale"]
        for node, t in raw["nodes"].items():
            if node not in known or not isinstance(t, dict):
                continue
            n = {}
            for k, (a, b, d) in (("dx", (-LIMITS["move"], LIMITS["move"], 0.0)), ("dy", (-LIMITS["move"], LIMITS["move"], 0.0)),
                                 ("sx", (lo, hi, 1.0)), ("sy", (lo, hi, 1.0)), ("rot", (-LIMITS["rot"], LIMITS["rot"], 0.0))):
                v = _num(t.get(k), a, b, d)
                if abs(v - d) > (0.5 if k == "rot" else 0.004):
                    n[k] = round(v, 3)
            if n:
                nodes[node] = n
    if nodes:
        out["nodes"] = nodes
    return out


def rig_fields(spec: dict) -> dict:
    return {k: spec[k] for k in RIG_KEYS if spec.get(k)}


def used_parts(spec: dict) -> list:
    names = [v for v in (spec.get("parts") or {}).values() if v not in ("none", "builtin")]
    names += [e["part"] for e in (spec.get("extra") or [])]
    return sorted(set(names))


def lerp_fields(a: dict, b: dict, t: float) -> dict:
    """两组骨架字段之间插值：变换参数连续插值，部件替换在中点切换；同 id 的新增节点位置也插值。"""
    out = {}
    na, nb = a.get("nodes") or {}, b.get("nodes") or {}
    nodes = {}
    for node in set(na) | set(nb):
        ta, tb = na.get(node, {}), nb.get(node, {})
        n = {}
        for k, d in (("dx", 0.0), ("dy", 0.0), ("sx", 1.0), ("sy", 1.0), ("rot", 0.0)):
            v = ta.get(k, d) + (tb.get(k, d) - ta.get(k, d)) * t
            if abs(v - d) > 1e-4:
                n[k] = v
        if n:
            nodes[node] = n
    if nodes:
        out["nodes"] = nodes
    src = a if t < 0.5 else b
    if src.get("parts"):
        out["parts"] = dict(src["parts"])
    ea = {e["id"]: e for e in a.get("extra") or []}
    eb = {e["id"]: e for e in b.get("extra") or []}
    extra = []
    for e in (src.get("extra") or []):
        other = (eb if src is a else ea).get(e["id"])
        if other is not None and other.get("part") == e.get("part"):
            x, y = (ea[e["id"]], eb[e["id"]])
            e = {**e, **{k: x.get(k, 0.0) + (y.get(k, 0.0) - x.get(k, 0.0)) * t for k in ("x", "y", "w", "h", "rot")}}
        extra.append(e)
    if extra:
        out["extra"] = extra
    return out


def fields_key(spec: dict, digests: dict) -> tuple:
    f = rig_fields(spec)
    if not f:
        return ()
    q = json.dumps(f, sort_keys=True, ensure_ascii=False, default=lambda v: round(v, 2))
    return (q, tuple((n, digests.get(n, "")) for n in used_parts(f)))


# ---------------------------------------------------------------------- 骨架节点
def default_nodes(rig) -> dict:
    """默认骨架：节点名 → {"cx","cy","a","b","inner"}（裁剪图像素坐标）。由原画标定推出，和内置五官的位置一致。"""
    cal = rig.cal
    nodes = {}
    for i, side in enumerate(("l", "r")):
        e = cal["eyes"][i]
        cx, cy = rig.p(*e["center"])
        nodes[f"eye_{side}"] = {"cx": cx, "cy": cy, "a": float(e["a"]), "b": float(e["b"]), "inner": e["inner"]}
        br = cal["brows"][i]
        ix, iy = rig.p(*br["inner"])
        ox, oy = rig.p(*br["outer"])
        nodes[f"brow_{side}"] = {"cx": (ix + ox) / 2, "cy": (iy + oy) / 2, "a": math.hypot(ix - ox, iy - oy) / 2,
                                 "b": max(6.0, rig.ch * 0.8), "inner": e["inner"]}
        chx, chy = rig.p(*cal["cheeks"][i])
        nodes[f"cheek_{side}"] = {"cx": chx, "cy": chy, "a": 30.0, "b": 15.0, "inner": e["inner"]}
    mx, my = rig.p(*cal["mouth"]["center"])
    nodes["mouth"] = {"cx": mx, "cy": my, "a": max(30.0, rig.cw * 6.5) / 2, "b": max(22.0, rig.ch * 2.3) / 2, "inner": 1}
    e0, e1 = cal["eyes"]
    fx0, _ = rig.p(e0["center"][0] - e0["a"], 0)
    fx1, _ = rig.p(e1["center"][0] + e1["a"], 0)
    _, fy = rig.p(0, min(e0["center"][1], e1["center"][1]) - 52)
    nodes["forehead"] = {"cx": (fx0 + fx1) / 2, "cy": fy + 30, "a": (fx1 - fx0) / 2, "b": 30.0, "inner": 1}
    sx, sy = rig.p(*cal["sweat_anchor"])
    nodes["sweat"] = {"cx": sx, "cy": sy, "a": 14.0, "b": 18.0, "inner": 1}
    ecx = (nodes["eye_l"]["cx"] + nodes["eye_r"]["cx"]) / 2
    ecy = (nodes["eye_l"]["cy"] + nodes["eye_r"]["cy"]) / 2
    nodes["nose"] = {"cx": (ecx + mx) / 2, "cy": ecy + (my - ecy) * 0.6, "a": 10.0, "b": 10.0, "inner": 1}
    nodes["chin"] = {"cx": mx, "cy": my + 34, "a": 30.0, "b": 12.0, "inner": 1}
    fb = cal.get("face_box") or [e0["center"][0] - 60, e0["center"][1] - 100, e1["center"][0] + 60, cal["mouth"]["center"][1] + 60]
    x0, y0 = rig.p(fb[0], fb[1])
    x1, y1 = rig.p(fb[2], fb[3])
    nodes["face"] = {"cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2, "a": (x1 - x0) / 2, "b": (y1 - y0) / 2, "inner": 1}
    return nodes


def face_size(nodes: dict) -> tuple[float, float]:
    f = nodes["face"]
    return f["a"] * 2, f["b"] * 2


def node_geom(nodes: dict, fields: dict, name: str) -> dict | None:
    """某个节点在当前骨架字段下的实际几何：{"cx","cy","a","b","rot","inner","flip"}。"""
    fw, fh = face_size(nodes)
    f = nodes["face"]
    if name in nodes:
        n = dict(nodes[name])
    else:
        e = next((x for x in fields.get("extra") or [] if x["id"] == name), None)
        if e is None:
            return None
        n = {"cx": f["cx"] - f["a"] + e["x"] * fw, "cy": f["cy"] - f["b"] + e["y"] * fh, "a": e["w"] * fw, "b": e["h"] * fh,
             "inner": -1 if e.get("flip") else 1, "rot": e.get("rot", 0.0)}
    t = (fields.get("nodes") or {}).get(name, {})
    n["cx"] += t.get("dx", 0.0) * fw
    n["cy"] += t.get("dy", 0.0) * fh
    n["a"] *= t.get("sx", 1.0)
    n["b"] *= t.get("sy", 1.0)
    n["rot"] = n.get("rot", 0.0) + t.get("rot", 0.0)
    return n


def node_table(rig, nodes: dict | None = None) -> list:
    """给模型看的骨架表：每个节点在脸框里的归一化位置与大小（0~1）。"""
    nodes = nodes or default_nodes(rig)
    f = nodes["face"]
    fw, fh = face_size(nodes)
    rows = []
    for name in CORE_NODES + ANCHOR_NODES:
        n = nodes[name]
        rows.append({"node": name, "x": round((n["cx"] - (f["cx"] - f["a"])) / fw, 2),
                     "y": round((n["cy"] - (f["cy"] - f["b"])) / fh, 2), "w": round(n["a"] / fw, 3),
                     "h": round(n["b"] / fh, 3), "draws": "内置" if name in CORE_NODES else "锚点"})
    return rows


SKELETON_OPS = ("move", "scale", "rotate", "replace", "add", "remove", "group", "reset")


def apply_ops(fields: dict, ops: list, nodes: dict, warnings: list | None = None) -> dict:
    """在骨架字段上执行一串骨架操作（模型在实验里提出），返回校验后的新字段。越界的值被钳制，非法操作被忽略。"""
    warnings = warnings if warnings is not None else []
    out = json.loads(json.dumps(fields or {}))
    out.setdefault("nodes", {})
    out.setdefault("parts", {})
    out.setdefault("extra", [])
    fw, fh = face_size(nodes)

    def known(n):
        return n in CORE_NODES or any(e["id"] == n for e in out["extra"])

    def tf(n):
        return out["nodes"].setdefault(n, {})

    for op in (ops or [])[:24]:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").lower()
        node = str(op.get("node") or op.get("id") or "")
        if kind not in SKELETON_OPS:
            warnings.append(f"未知骨架操作 {kind!r}")
            continue
        if kind == "add":
            out["extra"].append({k: op.get(k) for k in ("x", "y", "w", "h", "part", "rot", "flip") if k in op} | {"id": node})
            continue
        if kind == "group":
            members = [m for m in (op.get("nodes") or []) if known(m)]
            if not members:
                warnings.append("group：没有有效的节点")
                continue
            geoms = [node_geom(nodes, out, m) for m in members]
            gx = sum(g["cx"] for g in geoms) / len(geoms)
            gy = sum(g["cy"] for g in geoms) / len(geoms)
            s = _num(op.get("s"), *LIMITS["scale"], 1.0)
            rot = _num(op.get("rot"), -LIMITS["rot"], LIMITS["rot"], 0.0)
            dx, dy = _num(op.get("dx"), -0.5, 0.5, 0.0), _num(op.get("dy"), -0.5, 0.5, 0.0)
            c, si = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            for m, g in zip(members, geoms):
                ox, oy = (g["cx"] - gx) * s, (g["cy"] - gy) * s
                nx, ny = gx + ox * c - oy * si, gy + ox * si + oy * c
                t = tf(m)
                t["dx"] = t.get("dx", 0.0) + (nx - g["cx"]) / fw + dx
                t["dy"] = t.get("dy", 0.0) + (ny - g["cy"]) / fh + dy
                t["sx"] = t.get("sx", 1.0) * s
                t["sy"] = t.get("sy", 1.0) * s
                t["rot"] = t.get("rot", 0.0) + rot
            continue
        if not known(node):
            warnings.append(f"{kind}：没有节点 {node!r}")
            continue
        if kind == "move":
            t = tf(node)
            t["dx"] = t.get("dx", 0.0) + (_num(op.get("dx"), -0.5, 0.5, 0.0) or 0.0)
            t["dy"] = t.get("dy", 0.0) + (_num(op.get("dy"), -0.5, 0.5, 0.0) or 0.0)
        elif kind == "scale":
            t = tf(node)
            s = _num(op.get("s"), *LIMITS["scale"], None)
            t["sx"] = t.get("sx", 1.0) * (_num(op.get("sx"), *LIMITS["scale"], s or 1.0))
            t["sy"] = t.get("sy", 1.0) * (_num(op.get("sy"), *LIMITS["scale"], s or 1.0))
        elif kind == "rotate":
            t = tf(node)
            t["rot"] = t.get("rot", 0.0) + (_num(op.get("deg", op.get("rot")), -LIMITS["rot"], LIMITS["rot"], 0.0) or 0.0)
        elif kind == "replace":
            part = op.get("part")
            if node in CORE_NODES and (part in ("builtin", "none") or valid_name(part)):
                out["parts"][node] = part
            elif node not in CORE_NODES and valid_name(part):
                for e in out["extra"]:
                    if e["id"] == node:
                        e["part"] = part
            else:
                warnings.append(f"replace：部件名 {part!r} 不合法")
        elif kind == "remove":
            if node in CORE_NODES:
                out["parts"][node] = "none"
            else:
                out["extra"] = [e for e in out["extra"] if e["id"] != node]
            out["nodes"].pop(node, None)
        elif kind == "reset":
            out["nodes"].pop(node, None)
            out["parts"].pop(node, None)
    return clean_rig_fields(out)


# ---------------------------------------------------------------------- 绘制
def _rgb(c: str, colors: dict, skin: tuple) -> tuple:
    if c == "skin":
        return skin
    if c in colors:
        return colors[c]
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _ellipse_pts(cx, cy, rx, ry, a0=0.0, a1=360.0, n=36):
    steps = max(4, int(n * abs(a1 - a0) / 360) + 1)
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * k / (steps - 1))),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * k / (steps - 1)))) for k in range(steps)]


def _star_pts(cx, cy, r, n):
    pts = []
    for k in range(n * 2):
        ang = math.pi / n * k - math.pi / 2
        rr = r if k % 2 == 0 else r * (0.32 if n == 4 else 0.45)
        pts.append((cx + math.cos(ang) * rr, cy + math.sin(ang) * rr))
    return pts


def _heart_pts(cx, cy, r):
    pts = []
    for k in range(40):
        t = 2 * math.pi * k / 40
        hx = 16 * math.sin(t) ** 3
        hy = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + hx * r / 17, cy - hy * r / 17))
    return pts


def draw_component(size: tuple, comp: dict, geom: dict, colors: dict, ch: float, skin: tuple = (240, 220, 210)):
    """把组件画到一张透明的 RGBA 图层上（与裁剪图同尺寸）并返回该图层。geom 来自 node_geom。"""
    from PIL import Image, ImageDraw

    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, a, b = geom["cx"], geom["cy"], max(2.0, geom["a"]), max(2.0, geom["b"])
    fx = 1 if (not comp.get("mirror") or geom.get("inner", 1) > 0) else -1
    rot = math.radians(geom.get("rot", 0.0))
    cr, sr = math.cos(rot), math.sin(rot)
    uni = (a + b) / 2

    def P(x, y, uniform=False):
        lx, ly = (x * uni * fx, y * uni) if uniform else (x * a * fx, y * b)
        return (cx + lx * cr - ly * sr, cy + lx * sr + ly * cr)

    def col(name, alpha):
        return _rgb(name, colors, skin) + (int(255 * alpha),)

    for p in comp.get("draw", []):
        t, alpha = p["t"], p.get("a", 1.0)
        w = max(1, int(round(p.get("w", 0.7) * ch)))
        fill = col(p["fill"], alpha) if p.get("fill") else None
        stroke = col(p["stroke"], alpha) if p.get("stroke") else None
        if t in ("ellipse", "arc"):
            (ex, ey), (rx, ry) = p["c"], p["r"]
            a0, a1 = (p.get("a0", 0.0), p.get("a1", 360.0)) if t == "arc" else (0.0, 360.0)
            pts = [P(x, y) for x, y in _ellipse_pts(ex, ey, rx, ry, a0, a1)]
            if t == "ellipse":
                if fill:
                    d.polygon(pts, fill=fill)
                if stroke:
                    d.line(pts + [pts[0]], fill=stroke, width=w, joint="curve")
            else:
                d.line(pts, fill=stroke or col("ink", alpha), width=w, joint="curve")
        elif t in ("line", "poly"):
            pts = [P(x, y) for x, y in p["pts"]]
            if t == "poly":
                if fill:
                    d.polygon(pts, fill=fill)
                if stroke:
                    d.line(pts + [pts[0]], fill=stroke, width=w, joint="curve")
            else:
                if p.get("closed"):
                    pts = pts + [pts[0]]
                d.line(pts, fill=stroke or col("ink", alpha), width=w, joint="curve")
        elif t == "rect":
            x0, y0, x1, y1 = p["box"]
            pts = [P(x0, y0), P(x1, y0), P(x1, y1), P(x0, y1)]
            if fill:
                d.polygon(pts, fill=fill)
            if stroke:
                d.line(pts + [pts[0]], fill=stroke, width=w, joint="curve")
        elif t == "dots":
            r = p["r"] * uni
            for x, y in p["pts"]:
                qx, qy = P(x, y)
                d.ellipse([qx - r, qy - r, qx + r, qy + r], fill=fill or stroke or col("ink", alpha))
        elif t in ("star", "heart"):
            ex, ey = p["c"]
            raw = _star_pts(ex, ey, p["r"], p.get("n", 5)) if t == "star" else _heart_pts(ex, ey, p["r"])
            pts = [P(x, y, uniform=True) for x, y in raw]
            if fill:
                d.polygon(pts, fill=fill)
            if stroke:
                d.line(pts + [pts[0]], fill=stroke, width=w, joint="curve")
    return layer


def overlay_glyphs(chars: list, colors_grid: list, comp: dict, cell_xy: tuple, colors: dict, skin=(240, 220, 210)) -> list:
    """把组件的字符纹理叠到面部字符网格上（原地修改），返回改动过的格子坐标。"""
    g = comp.get("glyphs")
    if not g:
        return []
    rows = g["rows"]
    h = len(rows)
    w = max(len(r) for r in rows)
    x0 = int(round(cell_xy[0] - w / 2)) + g.get("dx", 0)
    y0 = int(round(cell_xy[1] - h / 2)) + g.get("dy", 0)
    rgb = _rgb(g.get("color", "ink"), colors, skin)
    touched = []
    for j, row in enumerate(rows):
        y = y0 + j
        if not 0 <= y < len(chars):
            continue
        for i, ch in enumerate(row):
            x = x0 + i
            if ch == " " or not 0 <= x < len(chars[y]):
                continue
            chars[y][x] = ch
            colors_grid[y][x] = rgb
            touched.append((x, y))
    return touched


def primitives_doc() -> str:
    return ("组件 = {\"slot\":\"" + "|".join(SLOTS) + "\", \"style\":\"" + "|".join(STYLES) + "\", \"mirror\":true/false,"
            " \"blink\":\"builtin|keep\"(眼睛组件眨眼时是否换成内置闭眼), \"talk\":\"bob|keep\"(嘴巴组件说话时是否上下张合),"
            " \"draw\":[图元...], \"glyphs\":{\"rows\":[\"字符行\"...], \"color\":颜色, \"dx\":格, \"dy\":格}}\n"
            "坐标：节点框内 x,y∈[-1,1]（最多 ±2.5），y 向下为正；成对部位 +x 指向脸中线，另一侧自动镜像。\n"
            "图元：{\"t\":\"ellipse\",\"c\":[x,y],\"r\":[rx,ry]} {\"t\":\"arc\",\"c\",\"r\",\"a0\":度,\"a1\":度} "
            "{\"t\":\"line\",\"pts\":[[x,y],...],\"closed\":false} {\"t\":\"poly\",\"pts\":[...]} {\"t\":\"rect\",\"box\":[x0,y0,x1,y1]} "
            "{\"t\":\"star\",\"c\",\"r\",\"n\":3~8} {\"t\":\"heart\",\"c\",\"r\"} {\"t\":\"dots\",\"pts\":[...],\"r\"}；"
            "每个图元可选 \"fill\"、\"stroke\"、\"w\"(线宽，按字符高的倍数 0.15~2.5，默认 0.7)、\"a\"(不透明度)。"
            f"最多 {LIMITS['prims']} 个图元、每个 {LIMITS['points']} 个点。\n"
            "颜色：ink sclera iris_dark iris iris_light highlight blush blush_line mouth tongue teeth tear sweat gloom heart star skin，或 #rrggbb。\n"
            "字符画一格约 5×8 像素，节点框通常只有 6~16 格宽：线宽 ≥0.6、图形占满节点框，才能在字符画上看清；细节太碎会被字符化抹掉。")


# ---------------------------------------------------------------------- 组件库
class PartsLibrary:
    """正式组件库与骨架方案（<数据目录>/components.json）。只有 active 的组件会被渲染器使用；
    实验中的候选设计不在这里（见 creative.CreativeMemory），通过验证并由模型决定采用后才登记进来。"""

    MAX_HISTORY = 6

    def __init__(self, path: Path, roles=(), max_items: int = 80):
        self.path = Path(path)
        self.roles = tuple(roles)
        self.max_items = max_items
        self._lock = threading.RLock()
        self.items: dict = {}
        self.skeletons: dict = {}
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = obj.get("components") or {}
            self.skeletons = obj.get("skeletons") or {}
        except Exception:
            pass
        self._publish()

    def _publish(self) -> None:
        from . import face_rig as fr

        with self._lock:
            active = {n: it["spec"] for n, it in self.items.items() if it.get("status") == "active"}
            fr.PARTS = active
            fr.PARTS_DIGEST = {n: digest(s) for n, s in active.items()}
            fr.SKELETONS = {n: it["fields"] for n, it in self.skeletons.items() if it.get("status") == "active"}

    def save(self) -> None:
        with self._lock:
            data = json.dumps({"components": self.items, "skeletons": self.skeletons}, ensure_ascii=False,
                              separators=(",", ":"))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self.path)

    def active(self) -> dict:
        return {n: it["spec"] for n, it in self.items.items() if it.get("status") == "active"}

    def adopt(self, name: str, spec: dict, intent: str = "", origin: str = "experiment", report: dict | None = None,
              by: str = "experiment") -> tuple[str, str]:
        """登记（或更新）一个已通过验证的组件。返回 (名字, 状态 new|revised|same|invalid)。"""
        if not valid_name(name):
            return name, "invalid"
        with self._lock:
            now = round(time.time(), 1)
            it = self.items.get(name)
            if it is not None and digest(it["spec"]) == digest(spec) and it.get("status") == "active":
                return name, "same"
            if it is not None:
                hist = it.setdefault("history", [])
                hist.append({"spec": it["spec"], "time": it.get("updated", now), "by": it.get("by", "")})
                del hist[:-self.MAX_HISTORY]
                it.update(spec=spec, updated=now, status="active", by=by, intent=intent or it.get("intent", ""))
                if report:
                    it["check"] = report
                status = "revised"
            else:
                self.items[name] = {"spec": spec, "slot": spec["slot"], "style": spec["style"], "intent": intent[:80],
                                    "origin": origin, "by": by, "status": "active", "created": now, "updated": now,
                                    "uses": 0, "likes": 0, "dislikes": 0, "history": [], "check": report or {},
                                    "vision": []}
                status = "new"
                self._evict()
            self._publish()
            self.save()
            return name, status

    def _evict(self) -> None:
        live = [n for n, it in self.items.items() if it.get("status") == "active"]
        if len(live) <= self.max_items:
            return
        live.sort(key=lambda n: (self.items[n].get("likes", 0) - self.items[n].get("dislikes", 0),
                                 self.items[n].get("uses", 0), self.items[n].get("updated", 0)))
        for n in live[:len(live) - self.max_items]:
            self.items[n]["status"] = "retired"
            self.items[n]["retired_reason"] = "超过上限，淘汰最少用的"

    def retire(self, name: str, reason: str = "") -> bool:
        with self._lock:
            it = self.items.get(name)
            if not it or it.get("status") != "active":
                return False
            it.update(status="retired", retired_reason=reason[:60], updated=round(time.time(), 1))
            self._publish()
            self.save()
            return True

    def restore(self, name: str) -> bool:
        with self._lock:
            it = self.items.get(name)
            if not it or it.get("status") == "active":
                return False
            it["status"] = "active"
            self._publish()
            self.save()
            return True

    def rollback(self, name: str) -> bool:
        with self._lock:
            it = self.items.get(name)
            if not it or not it.get("history"):
                return False
            prev = it["history"].pop()
            it.update(spec=prev["spec"], updated=round(time.time(), 1))
            self._publish()
            self.save()
            return True

    def add_feedback(self, name: str, entry: dict) -> None:
        with self._lock:
            it = self.items.get(name)
            if it is not None:
                it.setdefault("vision", []).append(entry)
                del it["vision"][:-5]
                self.save()

    def touch(self, names) -> None:
        with self._lock:
            for n in names:
                if n in self.items:
                    self.items[n]["uses"] = self.items[n].get("uses", 0) + 1

    def rate(self, names, vote: int) -> None:
        with self._lock:
            for n in names:
                it = self.items.get(n)
                if it is not None:
                    it["likes" if vote > 0 else "dislikes"] = it.get("likes" if vote > 0 else "dislikes", 0) + 1
            self.save()

    def save_skeleton(self, name: str, fields: dict, intent: str = "") -> tuple[str, str]:
        if not valid_name(name):
            return name, "invalid"
        with self._lock:
            old = self.skeletons.get(name)
            status = "revised" if old else "new"
            self.skeletons[name] = {"fields": fields, "intent": intent[:80], "status": "active",
                                    "updated": round(time.time(), 1),
                                    "history": ((old or {}).get("history", []) + ([old["fields"]] if old else []))[-4:]}
            self._publish()
            self.save()
            return name, status

    def summary(self, limit: int = 24, include_retired: bool = False) -> list:
        with self._lock:
            rows = []
            for n, it in sorted(self.items.items(), key=lambda kv: -kv[1].get("updated", 0)):
                if it.get("status") != "active" and not include_retired:
                    continue
                v = (it.get("vision") or [{}])[-1]
                rows.append({"name": n, "slot": it.get("slot"), "style": it.get("style"), "intent": it.get("intent", ""),
                             "uses": it.get("uses", 0), "likes": it.get("likes", 0), "dislikes": it.get("dislikes", 0),
                             "status": it.get("status"), "versions": len(it.get("history", [])) + 1,
                             **({"vision": v.get("summary", "")} if v else {})})
                if len(rows) >= limit:
                    break
            return rows
