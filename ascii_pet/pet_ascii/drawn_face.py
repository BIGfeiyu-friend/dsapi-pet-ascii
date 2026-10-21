"""用画师手绘的表情差分驱动面部骨架（v0.7.1）。

素材来自 tools/import_face_layers.py 导入的 assets/characters/face_layers/（用户本人画的 PSD 差分）：
五个眼睛状态 —— 睁眼 open、半睁 half、睁大 wide、闭眼 closed、笑眼 smile —— 各有左右眼的眼眶线稿和眉毛，
睁眼 / 半睁 / 睁大还有眼白；虹膜是单独画的完整椭圆（普通虹膜、睁大时的小虹膜各一套，含高光）；
另有几种嘴（原画的小嘴 dot、半睁那张的小嘴 small、O 形嘴 o、笑着张开的嘴 open）。

骨架参数怎样落到这些手绘图层上：
  状态     smile ≥ 0.6 → 笑眼；open < 0.2 → 闭眼；< 0.72（或眼角压低的怒视）→ 半睁；≥ 1.12 → 睁大；其余 → 睁眼
           （瞳孔缩成点的震惊脸也用睁大那套）。手绘本身就是分档的，和动画里的“原画张”一样直接切换。
  视线     只移动手绘的虹膜（含高光），裁剪在这个状态的眼白里，眼眶线稿盖在上面
  虹膜大小 缩放手绘的虹膜
  眼角斜度 整只眼（眼白 + 虹膜 + 线稿）绕眼睛中心轻微旋转：生气时内眼角压低，委屈时内眼角抬高
  瞳孔形状 heart / star / spiral 在手绘虹膜的位置上叠加；dot 用睁大那套的小虹膜再缩小；× 叉叉眼仍是程序画
  高光     < 0.4 去掉手绘高光（空洞），> 1.25 额外加一颗星形高光
  眉毛     跟着同侧眼睛的状态用对应那张的眉毛，再按 brows.y / angle 平移、旋转
  嘴       art → 原画的小嘴；o → O 形嘴（按张开程度缩放）；normal / teeth / tongue 张开时 → 笑着张开的嘴
           （按张开程度纵向缩放，说话时一张一合；嘴角下撇时不用它）；其它嘴型还没有手绘，仍用程序画。
           这张嘴是和笑眼一起画的，眼睛不是笑眼 ^ 时（咧嘴笑、说话）往下挪一些，不贴着眼睛
"""
from __future__ import annotations

import json
import math
import os

STATES = ("open", "half", "wide", "closed", "smile")


def eye_state(e: dict) -> str:
    if e["smile"] >= 0.6:
        return "smile"
    if e["open"] < 0.2:
        return "closed"
    if e["pupil"] == "dot" and e["open"] >= 0.72:      # 瞳孔缩成点：睁着眼才用睁大那套；半闭着（困、发呆）还是半睁，虹膜缩小
        return "wide"
    if e["open"] < 0.72 or (e["tilt"] >= 0.5 and e["open"] < 1.0):     # 眼角明显压低的怒视：用半睁那张再旋转
        return "half"
    if e["open"] >= 1.12:
        return "wide"
    return "open"


class DrawnFace:
    def __init__(self, rig, folder: str):
        from PIL import Image

        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            self.m = json.load(f)
        self.rig = rig
        self.size = rig.blank.size
        ox, oy = rig.origin                               # 标定图坐标 → 本视图裁剪图坐标

        def load(ent):
            if not ent:
                return None
            im = Image.open(os.path.join(folder, ent["file"])).convert("RGBA")
            return {"img": im, "x": ent["origin"][0] - ox, "y": ent["origin"][1] - oy}

        self.parts = {}
        for state, st in self.m["states"].items():
            for k, ent in st.items():
                if isinstance(ent, dict):
                    self.parts[(state, k)] = load(ent)
        self.iris = {}
        for name, sides in self.m["iris"].items():
            for side, ent in sides.items():
                self.iris[(name, side)] = {"body": load(ent["body"]), "hl": load(ent["highlight"]),
                                           "cx": ent["center"][0] - ox, "cy": ent["center"][1] - oy,
                                           "rx": ent["radius"][0], "ry": ent["radius"][1]}
        self.mouths = {k: load(v) for k, v in self.m["mouths"].items() if v}
        self.states_iris = {s: st.get("iris") for s, st in self.m["states"].items()}
        self.open_drop = self._open_mouth_drop()
        self.folder = folder
        self._morph = None
        self._morph_tried = False

    @property
    def morph(self):
        """连续变形模型（morph_face.MorphFace，v0.8）；EYE_STYLE 不是 morph 或加载失败时为 None（退回分档的手绘差分）。"""
        from . import face_rig as fr

        if fr.EYE_STYLE != "morph":
            return None
        if not self._morph_tried:
            self._morph_tried = True
            from .morph_face import MorphFace

            self._morph = MorphFace.shared(self.folder)
            if self._morph is not None and self._morph.mouth is not None:
                mo = self._morph.mouth
                self._depth = max(p[1] for p in mo.bot["pts"]) - min(p[1] for p in mo.top["pts"])
        return self._morph

    MOUTH_ANCHOR = 0.4      # 嘴张开时：上唇线往上让 40%，下轮廓往下张 60%（锚点在上沿往下 40% 处，说话时嘴不会整体上下跳）
    CLOSED_UNTIL, OPEN_FROM = 0.1, 0.25                                   # 合着的小嘴 ↔ 张开的嘴：只在这一小段交叉淡化

    def _morph_mouth(self, img, m, eyes, mf) -> bool:
        from PIL import Image

        mo = mf.mouth
        k = 0.0 if m["shape"] == "line" else min(1.6, m["open"] / 0.55)   # 手绘那张对应张开程度 0.55
        # 不是笑眼 ^ 时整张嘴往下挪 open_drop（这张嘴是配笑眼画的）；笑眼程度是连续的，挪动量也连续
        s = 1.0 if not eyes else min(e["smile"] for e in eyes)
        drop = self.open_drop * (1.0 - min(1.0, max(0.0, (s - 0.3) / 0.4)))
        dy = drop + self.MOUTH_ANCHOR * self._depth * (1.0 - k)           # k=1（手绘那张）时正好在原画位置
        ox, oy = self.rig.origin
        t_open = 1.0 if mf.closed is None else min(1.0, max(0.0, (k - self.CLOSED_UNTIL) / (self.OPEN_FROM - self.CLOSED_UNTIL)))
        if t_open > 0.0:
            width = m["width"] * (0.55 + 0.45 * min(1.0, k / 0.9))       # 刚张开的嘴窄一些，张大后恢复原画宽度
            inner = m["shape"] if m["shape"] in ("teeth", "tongue") and k > 0.25 else ""
            with mf.lock:
                layer = mo.layer(width, k, m["curve"], m.get("skew", 0.0), inner)
            if t_open < 1.0:
                layer = layer.copy()
                layer.putalpha(layer.getchannel("A").point(lambda v: int(v * t_open)))
            img.alpha_composite(layer, (int(mo.box[0] - ox), int(round(mo.box[1] - oy + dy))))
        if t_open < 1.0:                                                  # 合着的嘴：画师画的小嘴拉直后按嘴角弯
            cm = mf.closed
            with mf.lock:
                layer = cm.layer(m["width"], m["curve"], m.get("skew", 0.0))
            if t_open > 0.0:
                layer = layer.copy()
                layer.putalpha(layer.getchannel("A").point(lambda v: int(v * (1 - t_open))))
            top = mo.top["pts"]                                           # 放在张开的嘴此刻上唇线的位置（交接处不错位）
            tx = mo.box[0] + sum(p[0] for p in top) / len(top)
            ty = mo.box[1] + sum(p[1] for p in top) / len(top) + dy
            img.alpha_composite(layer, (int(round(tx - cm.center[0] - ox)), int(round(ty - cm.center[1] - oy))))
        del Image
        return True

    OPEN_MOUTH_DROP = 2 / 3

    def _open_mouth_drop(self) -> float:
        """笑着张开的嘴是和笑眼 ^ 一起画的，位置偏高；配睁着的眼睛（咧嘴笑、说话）时贴得离眼睛太近。
        睁眼的下眼睑比笑眼低多少（这套差分是 24 像素），嘴就往下挪其中的 2/3（16 像素）：
        全挪的话这张大嘴会压到下巴（对比图看过 16 和 24）。"""
        if not self.m["mouths"].get("open"):
            return 0.0

        def bottom(state):
            st = self.m["states"].get(state) or {}
            ys = [e["origin"][1] + e["size"][1] for k, e in st.items() if k in ("eye_l", "eye_r") and isinstance(e, dict)]
            return max(ys) if ys else None

        smile, opened = bottom("smile"), bottom("open")
        if smile is None or opened is None:
            return 0.0
        return round(max(0.0, opened - smile) * self.OPEN_MOUTH_DROP)

    # ------------------------------------------------------------------ 眼睛
    def _eye_box(self, side: str, pad: int = 26):
        boxes = []
        for state in STATES:
            for k in (f"eye_{side}", f"sclera_{side}"):
                p = self.parts.get((state, k))
                if p:
                    boxes.append((p["x"], p["y"], p["x"] + p["img"].width, p["y"] + p["img"].height))
        x0, y0 = min(b[0] for b in boxes) - pad, min(b[1] for b in boxes) - pad
        x1, y1 = max(b[2] for b in boxes) + pad, max(b[3] for b in boxes) + pad
        return int(x0), int(y0), int(x1), int(y1)

    def draw_eye(self, img, i: int, e: dict, gaze) -> bool:
        """把一只眼画到 img（RGBA，裁剪图大小）上。× 叉叉眼等不支持的情况返回 False（交给程序绘制）。"""
        if e["pupil"] == "x":
            return False
        mf = self.morph
        if mf is not None and mf.eyes[i] is not None:
            return self._morph_eye(img, i, e, gaze, mf)
        from PIL import Image, ImageChops, ImageDraw

        side = "l" if i == 0 else "r"
        state = eye_state(e)
        lines = self.parts.get((state, f"eye_{side}"))
        if lines is None:
            return False
        bx0, by0, bx1, by1 = self._eye_box(side)
        W, H = bx1 - bx0, by1 - by0
        local = Image.new("RGBA", (W, H), (0, 0, 0, 0))

        def put(dst, p, dx=0.0, dy=0.0):
            dst.alpha_composite(p["img"], (int(round(p["x"] - bx0 + dx)), int(round(p["y"] - by0 + dy))))

        sclera = self.parts.get((state, f"sclera_{side}"))
        rig = self.rig
        cal = rig.cal["eyes"][i]
        a, b = float(cal["a"]), float(cal["b"])
        if sclera is not None:
            ball = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            put(ball, sclera)
            mask = ball.getchannel("A")
            iris_name = self.states_iris.get(state) or "normal"
            ir = self.iris.get((iris_name, side))
            if ir is not None:
                k = e["iris"] if iris_name == "normal" else max(0.6, min(1.3, e["iris"] / 0.8))
                if e["pupil"] == "dot":
                    k = 0.5
                dx = gaze[0] * a * 0.42
                dy = gaze[1] * b * 0.30
                cx, cy = ir["cx"] - bx0 + dx, ir["cy"] - by0 + dy
                layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                parts = [ir["body"]] + ([ir["hl"]] if e["highlight"] >= 0.4 and ir["hl"] else [])
                for p in parts:
                    if p is None:
                        continue
                    im = p["img"]
                    px, py = p["x"] - bx0, p["y"] - by0
                    if abs(k - 1) > 0.02:                       # 以虹膜中心为基准缩放
                        im = im.resize((max(1, int(round(im.width * k))), max(1, int(round(im.height * k)))), Image.LANCZOS)
                        px = (ir["cx"] - bx0) + (px - (ir["cx"] - bx0)) * k
                        py = (ir["cy"] - by0) + (py - (ir["cy"] - by0)) * k
                    layer.alpha_composite(im, (int(round(px + dx)), int(round(py + dy))))
                rx, ry = ir["rx"] * k, ir["ry"] * k
                d = ImageDraw.Draw(layer)
                if e["pupil"] == "heart":
                    rig._heart(d, cx, cy + ry * 0.1, rx * 0.95, rig.col["heart"])
                    d.ellipse([cx - rx * 0.4, cy - ry * 0.42, cx - rx * 0.12, cy - ry * 0.16], fill=rig.col["highlight"])
                elif e["pupil"] == "star":
                    rig._star(d, cx, cy, rx * 0.62, rig.col["star"])
                elif e["pupil"] == "spiral":
                    thin = max(1, int(round(rig.ch * 0.4)))
                    for j, rr in enumerate((0.85, 0.62, 0.4, 0.2)):
                        d.ellipse([cx - rx * rr, cy - ry * rr, cx + rx * rr, cy + ry * rr],
                                  outline=rig.col["iris_light"] if j % 2 else rig.col["ink"], width=thin)
                if e["highlight"] > 1.25 and e["pupil"] in ("art", "round"):
                    rig._star(d, cx - rx * 0.3, cy - ry * 0.3, rx * 0.4 * min(1.4, e["highlight"]), rig.col["highlight"], 4)
                layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))      # 虹膜只露在眼白里
                ball.alpha_composite(layer)
            if e["tear"] > 0.05:                                  # 泪光：眼白下部积起一层水
                pool = Image.new("RGBA", (W, H), tuple(rig.col["tear"]) + (int(150 * e["tear"]),))
                bb = mask.getbbox()
                if bb:
                    cut = Image.new("L", (W, H), 0)
                    top = bb[3] - (bb[3] - bb[1]) * 0.5 * e["tear"]
                    ImageDraw.Draw(cut).rectangle([0, top, W, H], fill=255)
                    pool.putalpha(ImageChops.multiply(ImageChops.multiply(pool.getchannel("A"), cut), mask))
                    ball.alpha_composite(pool)
            local.alpha_composite(ball)
        put(local, lines)
        if abs(e["tilt"]) > 0.03:                                 # 眼角斜度：整只眼轻微旋转
            ecx, ecy = rig.p(*cal["center"])
            ang = -e["tilt"] * 9.0 * cal["inner"]
            local = local.rotate(ang, resample=Image.BICUBIC, center=(ecx - bx0, ecy - by0))
        img.alpha_composite(local, (bx0, by0))
        return True

    def _morph_eye(self, img, i, e, gaze, mf) -> bool:
        """连续变形的眼睛：线稿和眼白来自 MorphFace（原像素沿脊柱搬运），虹膜 / 瞳孔 / 泪光 / 斜度照旧叠加。"""
        from PIL import Image, ImageChops, ImageDraw

        er = mf.eyes[i]
        with mf.lock:
            lines, mask, wide_w = er.layers(e["open"], e["smile"], e.get("droop", 0.0), e.get("lower", 0.0))
        rig = self.rig
        ox, oy = rig.origin
        W, H = er.size
        local = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        cal = rig.cal["eyes"][i]
        a, b = float(cal["a"]), float(cal["b"])
        if mask.getbbox() is not None:
            ball = Image.new("RGBA", (W, H), er.sclera_col + (255,))
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            dx, dy = gaze[0] * a * 0.42, gaze[1] * b * 0.30
            sets = [("normal", 1.0 - wide_w), ("small", wide_w)] if "small" in er.iris else [("normal", 1.0)]
            cx = cy = rx = ry = None
            for name, f in sets:
                ir = er.iris.get(name)
                if ir is None or f < 0.02:
                    continue
                k = e["iris"] if name == "normal" else max(0.6, min(1.3, e["iris"] / 0.8))
                if e["pupil"] == "dot":
                    k = 0.5
                icx, icy = ir["center"]
                part = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                for im in [ir["body"]] + ([ir["hl"]] if e["highlight"] >= 0.4 and ir["hl"] is not None else []):
                    bb = im.getbbox()
                    if bb is None:
                        continue
                    crop = im.crop(bb)
                    px, py = bb[0], bb[1]
                    if abs(k - 1) > 0.02:                     # 以虹膜中心为基准缩放
                        crop = crop.resize((max(1, int(round(crop.width * k))), max(1, int(round(crop.height * k)))),
                                           Image.LANCZOS)
                        px, py = icx + (px - icx) * k, icy + (py - icy) * k
                    part.alpha_composite(crop, (int(round(px + dx)), int(round(py + dy))))
                if f < 0.98:
                    part.putalpha(part.getchannel("A").point(lambda v, f=f: int(v * f)))
                layer.alpha_composite(part)
                if cx is None or f >= 0.5:
                    cx, cy, rx, ry = icx + dx, icy + dy, ir["radius"][0] * k, ir["radius"][1] * k
            d = ImageDraw.Draw(layer)
            if cx is not None:
                if e["pupil"] == "heart":
                    rig._heart(d, cx, cy + ry * 0.1, rx * 0.95, rig.col["heart"])
                    d.ellipse([cx - rx * 0.4, cy - ry * 0.42, cx - rx * 0.12, cy - ry * 0.16], fill=rig.col["highlight"])
                elif e["pupil"] == "star":
                    rig._star(d, cx, cy, rx * 0.62, rig.col["star"])
                elif e["pupil"] == "spiral":
                    thin = max(1, int(round(rig.ch * 0.4)))
                    for j, rr in enumerate((0.85, 0.62, 0.4, 0.2)):
                        d.ellipse([cx - rx * rr, cy - ry * rr, cx + rx * rr, cy + ry * rr],
                                  outline=rig.col["iris_light"] if j % 2 else rig.col["ink"], width=thin)
                if e["highlight"] > 1.25 and e["pupil"] in ("art", "round"):
                    rig._star(d, cx - rx * 0.3, cy - ry * 0.3, rx * 0.4 * min(1.4, e["highlight"]), rig.col["highlight"], 4)
            ball.alpha_composite(layer)
            if e["tear"] > 0.05:                                  # 泪光：眼白下部积起一层水
                bb = mask.getbbox()
                pool = Image.new("RGBA", (W, H), tuple(rig.col["tear"]) + (int(150 * e["tear"]),))
                cut = Image.new("L", (W, H), 0)
                ImageDraw.Draw(cut).rectangle([0, bb[3] - (bb[3] - bb[1]) * 0.5 * e["tear"], W, H], fill=255)
                pool.putalpha(ImageChops.multiply(pool.getchannel("A"), cut))
                ball.alpha_composite(pool)
            ball.putalpha(ImageChops.multiply(ball.getchannel("A"), mask))
            local.alpha_composite(ball)
        local.alpha_composite(lines)
        if abs(e["tilt"]) > 0.03:                                 # 眼角斜度：整只眼轻微旋转
            ecx, ecy = cal["center"][0] - er.box[0], cal["center"][1] - er.box[1]
            local = local.rotate(-e["tilt"] * 9.0 * cal["inner"], resample=Image.BICUBIC, center=(ecx, ecy))
        img.alpha_composite(local, (int(er.box[0] - ox), int(er.box[1] - oy)))
        return True

    # ------------------------------------------------------------------ 眉毛
    def draw_brow(self, img, i: int, br: dict, e: dict) -> bool:
        from PIL import Image

        mf = self.morph
        if mf is not None and mf.brows[i] is not None:          # 连续变形：跟着同侧眼睛的开合 / 笑眼一起变，再平移旋转
            bw = mf.brows[i]
            with mf.lock:
                layer = bw.layer(e["open"], e["smile"], -br["y"] * self.rig.ch * 1.3, br["angle"])
            ox, oy = self.rig.origin
            img.alpha_composite(layer, (int(bw.box[0] - ox), int(bw.box[1] - oy)))
            return True
        side = "l" if i == 0 else "r"
        p = self.parts.get((eye_state(e), f"brow_{side}")) or self.parts.get(("open", f"brow_{side}"))
        if p is None:
            return False
        im = p["img"]
        dy = -br["y"] * self.rig.ch * 1.3
        if abs(br["angle"]) > 0.03:
            pad = 8
            big = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
            big.alpha_composite(im, (pad, pad))
            ang = -br["angle"] * 14.0 * self.rig.cal["eyes"][i]["inner"]
            im = big.rotate(ang, resample=Image.BICUBIC)
            img.alpha_composite(im, (int(p["x"] - pad), int(round(p["y"] - pad + dy))))
        else:
            img.alpha_composite(im, (int(p["x"]), int(round(p["y"] + dy))))
        return True

    # ------------------------------------------------------------------ 嘴
    def draw_mouth(self, img, m: dict, eyes=None) -> bool:
        """eyes = (左眼, 右眼) 规格；给了且不是两只都笑眼 ^ 时，笑着张开的嘴往下挪 open_drop（眨眼不算，嘴不会跳）。"""
        from PIL import Image

        shape, op = m["shape"], m["open"]
        mf = self.morph
        if mf is not None and mf.mouth is not None and shape in ("normal", "teeth", "tongue", "line"):
            return self._morph_mouth(img, m, eyes, mf)
        if shape == "art":
            name, sx, sy = "dot", 1.0, 1.0
        elif shape == "o":
            name = "o"
            sx = sy = 0.8 + 1.4 * op
            sx *= m["width"]
        elif shape in ("normal", "teeth", "tongue") and op > 0.12 and m["curve"] >= -0.15:   # 手绘的是笑着张开的嘴
            name = "open"
            sy = max(0.35, min(1.35, op / 0.55))
            sx = max(0.7, min(1.25, 0.75 + 0.5 * op)) * m["width"]
        else:
            return False
        p = self.mouths.get(name)
        if p is None:
            return False
        im = p["img"]
        cx, cy = p["x"] + im.width / 2, p["y"] + im.height / 2
        if abs(sx - 1) > 0.02 or abs(sy - 1) > 0.02:
            im = im.resize((max(1, int(round(im.width * sx))), max(1, int(round(im.height * sy)))), Image.LANCZOS)
        if name == "open":                                       # 张嘴时上缘固定，往下张开
            top = p["y"]
            if eyes and not all(eye_state(e) == "smile" for e in eyes):
                top += self.open_drop
                top -= max(0, im.height - p["img"].height) * 0.5   # 张得比原画还大时一半往上长，不压到下巴
            img.alpha_composite(im, (int(round(cx - im.width / 2)), int(round(top))))
        else:
            img.alpha_composite(im, (int(round(cx - im.width / 2)), int(round(cy - im.height / 2))))
        return True
