"""ASCII 动画协议：DeepSeek 输出 → 校验/裁剪/规整 → 引擎可直接执行的结构。

模型只能输出数据（字符矩阵、颜色、时长、参数、特效名），协议里没有任何可执行内容；
未知字段被忽略，越界数值被钳制，非法字符被替换，宽字符（中文等）在画面补丁中替换为 ?。

顶层格式：
{
  "mode": 1|2|3,                 # 1 参数 / 2 局部生成（默认）/ 3 全画布
  "intent": "...",               # 这次想表达什么（简短）
  "expression": "...",           # 结束后的表情描述，写入视觉状态记忆
  "think": "...",                # 可选，英文短句，会作为字符流显示在身体纹理里
  "priority": 0-9,
  "ops": [ 操作... ],
  "end_state": {"face": "hold"|"neutral", "pose": "姿势名"}
}

操作类型（type）：
  face                 表情关键帧（推荐）：每帧是一个表情规格（预设 + 连续参数），由面部骨架绘制，本地补间过渡
  parameter_animation  参数控制（视线、眨眼、身体位移、跳跃、摆动、抖动、色调、背景、姿势…）
  face_patch           面部区域关键帧（坐标原点 = 面部区域左上角）
  region_patch         任意区域关键帧（anchor: face|body|canvas，layer: body|face|overlay|background）
  frame_sequence       关键帧序列（target: face|region，规整为上面两种之一）
  delta_update         差分修改：只给出要改的坐标和字符
  full_scene           全画布关键帧或场景重构（可隐藏身体/脸，结束后恢复或保持）
  effect               本地特效（粒子、闪光、乱码、解体、气泡…），由本地引擎逐帧执行

帧（frame）：
  {"rows": [...], "colors": [...], "palette": {"a": "#ff8800"}, "color": "#hex", "mask": [...],
   "blank": "transparent"|"clear", "ms": 毫秒}
  或差分帧 {"delta": [{"x":0,"y":1,"text":"^ ^","colors":"aaa"}], "ms": 毫秒}
  rows 中 "`" 永远表示透明（保留下层）；空格默认也透明，blank="clear" 时表示清空；
  差分 text 中空格表示擦除、"`" 表示跳过。colors 每个字符是 palette 的键，"." 或空格表示用默认色。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field

from .grid import TRANSPARENT_MARK, parse_color, sanitize_char

OP_TYPES = ("face", "expression", "parameter_animation", "face_patch", "region_patch", "frame_sequence", "delta_update",
            "full_scene", "effect")
FACE_KEYS = ("preset", "level", "eyes", "eye_l", "eye_r", "gaze", "brows", "mouth", "blush", "sweat", "gloom",
             "parts", "nodes", "extra", "skeleton")
EFFECTS = ("particles", "sparkle", "symbols", "glitch", "dissolve", "bubble", "flash", "fade", "color_shift",
           "shake", "rain", "ripple", "text", "scanline")
LOOPS = ("once", "loop", "pingpong")
ANCHORS = ("face", "body", "canvas")
REGION_LAYERS = ("body", "face", "overlay", "background")
BACKGROUNDS = ("none", "code", "rain", "log", "noise")
BLINKS = ("none", "once", "twice", "slow", "rapid", "closed", "auto")
EASES = ("linear", "in", "out", "inout", "bounce")

LIMITS = {
    "ops": 10,
    "frames_per_op": 16,
    "frames_total": 32,
    "ms_min": 40,
    "ms_max": 6000,
    "duration_max": 20000,
    "region_w": 48,
    "region_h": 32,
    "text_len": 120,
}


class ProtocolError(ValueError):
    pass


@dataclass
class Frame:
    rows: list | None = None          # list[str]，已过滤字符
    colors: list | None = None        # list[list[int|None]]
    mask: list | None = None
    delta: list | None = None         # [{"x","y","text","colors":[int|None]}]
    ms: int = 160
    blank: str = "transparent"
    spec: dict | None = None          # face 操作：表情规格（原样保留，由引擎按骨架规整）


@dataclass
class Op:
    type: str
    layer: str = "face"
    anchor: str = "face"
    x: int = 0
    y: int = 0
    frames: list = field(default_factory=list)
    loop: str = "once"
    repeat: int = 1
    hold: str = "last"
    params: dict = field(default_factory=dict)
    effect: dict = field(default_factory=dict)
    duration_ms: int = 0
    delay_ms: int = 0
    eyes: list = field(default_factory=list)
    hide: list = field(default_factory=list)


@dataclass
class Animation:
    mode: int = 2
    intent: str = ""
    expression: str = ""
    think: str = ""
    priority: int = 5
    ops: list = field(default_factory=list)
    end_state: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    face_dims: tuple = (0, 0)      # 生成时面部区域尺寸（姿势切换后用于居中对齐）
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def summary(self) -> str:
        kinds = ",".join(op.type for op in self.ops)
        return f"mode{self.mode} [{kinds}] {self.intent[:40]}"


@dataclass
class Context:
    """校验时需要的画布信息（用于裁剪尺寸）。"""
    canvas_w: int = 120
    canvas_h: int = 70
    face_w: int = 20
    face_h: int = 8
    body_w: int = 92
    body_h: int = 56
    poses: tuple = ()


# ---------------------------------------------------------------------- JSON 提取
def extract_json(text: str) -> dict:
    """从模型输出中取出第一个完整 JSON 对象（容忍 ```json 包裹和前后多余文字）。"""
    if isinstance(text, dict):
        return text
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = s.find("{", start + 1)
    raise ProtocolError("输出中没有可解析的 JSON 对象")


# ---------------------------------------------------------------------- 小工具
def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _float(v, default: float, lo: float, hi: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _choice(v, options, default):
    v = str(v).strip().lower() if v is not None else ""
    return v if v in options else default


def _text(v, n: int = 80) -> str:
    return str(v or "").strip()[:n]


def _ascii_text(v, n: int = 80) -> str:
    return "".join(c if 0x20 <= ord(c) < 0x7F else " " for c in str(v or ""))[:n].strip()


def _clean_row(row, width: int) -> str:
    out = []
    for ch in str(row)[:width]:
        out.append(ch if ch in (TRANSPARENT_MARK, " ") else sanitize_char(ch))
    return "".join(out)


def _palette(raw, base: dict | None = None) -> dict:
    pal = dict(base or {})
    if isinstance(raw, dict):
        for k, v in list(raw.items())[:32]:
            c = parse_color(v)
            if c is not None and isinstance(k, str) and k:
                pal[k[0]] = c
    return pal


def _color_row(row, palette: dict, default, width: int) -> list:
    out = []
    for ch in str(row)[:width]:
        out.append(palette.get(ch, default) if ch not in (".", " ", TRANSPARENT_MARK) else default)
    return out


def _fix_palette_glyphs(rows: list, crows: list, pal: dict, warnings: list) -> list:
    """模型常把调色板字母当图案写进 rows（如 rows "bbb" + colors "bbb"）。
    字符与该格颜色键相同且是调色板键时，视为“用该颜色填实”，替换为实心字符 #。"""
    fixed, out = 0, []
    for r, c in zip(rows, crows + [""] * (len(rows) - len(crows))):
        chars = list(r)
        for i, ch in enumerate(chars):
            if ch.isalpha() and ch.islower() and ch in pal and i < len(c) and c[i] == ch:
                chars[i] = "#"
                fixed += 1
        out.append("".join(chars))
    if fixed:
        warnings.append(f"{fixed} 个调色板字母被当作图案写进了 rows，已按颜色填实")
    return out


# ---------------------------------------------------------------------- 帧
def parse_frame(raw, w: int, h: int, op_palette: dict, op_color, warnings: list, allow_delta: bool = True) -> Frame | None:
    if isinstance(raw, list):  # 允许直接给行列表
        raw = {"rows": raw}
    if not isinstance(raw, dict):
        warnings.append("帧不是对象，已忽略")
        return None
    pal = _palette(raw.get("palette"), op_palette)
    default = parse_color(raw.get("color"), op_color)
    ms = _int(raw.get("ms", raw.get("duration_ms", 160)), 160, LIMITS["ms_min"], LIMITS["ms_max"])
    blank = _choice(raw.get("blank"), ("transparent", "clear"), "transparent")
    if isinstance(raw.get("rows"), list) and raw["rows"]:
        rows_in = raw["rows"][:h]
        if len(raw["rows"]) > h:
            warnings.append(f"帧行数 {len(raw['rows'])} 超过区域高度 {h}，已裁剪")
        rows = [_clean_row(r, w) for r in rows_in]
        colors = None
        if isinstance(raw.get("colors"), list):
            crows = [str(r)[:w] for r in raw["colors"][:h]]
            rows = _fix_palette_glyphs(rows, crows, pal, warnings)
            colors = [_color_row(r, pal, default, w) for r in crows]
        elif default is not None:
            colors = [[default] * len(r) for r in rows]
        mask = None
        if isinstance(raw.get("mask"), list):
            mask = [str(m)[:w] for m in raw["mask"][:h]]
        return Frame(rows=rows, colors=colors, mask=mask, ms=ms, blank=blank)
    if allow_delta and isinstance(raw.get("delta"), list):
        changes = []
        for d in raw["delta"][:64]:
            if not isinstance(d, dict):
                continue
            x = _int(d.get("x"), 0, -w, w)
            y = _int(d.get("y"), 0, -h, h)
            text = _clean_row(d.get("text", ""), LIMITS["text_len"])
            if not text:
                continue
            dpal = _palette(d.get("palette"), pal)
            ddef = parse_color(d.get("color"), default)
            if isinstance(d.get("colors"), str):
                text = _fix_palette_glyphs([text], [d["colors"]], dpal, warnings)[0]
                cols = _color_row(d["colors"], dpal, ddef, len(text))
            else:
                cols = [ddef] * len(text)
            changes.append({"x": x, "y": y, "text": text, "colors": cols})
        if not changes:
            warnings.append("差分帧没有有效修改")
            return None
        return Frame(delta=changes, ms=ms)
    warnings.append("帧既没有 rows 也没有 delta，已忽略")
    return None


def _frames(raw_frames, w, h, pal, color, warnings) -> list:
    out = []
    if not isinstance(raw_frames, list):
        return out
    for rf in raw_frames[:LIMITS["frames_per_op"]]:
        f = parse_frame(rf, w, h, pal, color, warnings)
        if f is not None:
            out.append(f)
    if isinstance(raw_frames, list) and len(raw_frames) > LIMITS["frames_per_op"]:
        warnings.append(f"关键帧超过 {LIMITS['frames_per_op']} 张，已截断")
    return out


def _eyes(raw, w, h) -> list:
    out = []
    if isinstance(raw, list):
        for e in raw[:4]:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                ex = _int(e[0], 0, 0, w - 1)
                ey = _int(e[1], 0, 0, h - 1)
                ew = _int(e[2] if len(e) > 2 else 1, 1, 1, w)
                eh = _int(e[3] if len(e) > 3 else 1, 1, 1, h)
                out.append([ex, ey, ew, eh])
    return out


# ---------------------------------------------------------------------- 操作
PARAM_SPEC = {
    "gaze_x": (float, 0.0, -1.0, 1.0),
    "gaze_y": (float, 0.0, -1.0, 1.0),
    "head_tilt": (int, 0, -2, 2),
    "body_dx": (float, 0.0, -24.0, 24.0),
    "body_dy": (float, 0.0, -12.0, 12.0),
    "bob": (float, 1.0, 0.0, 4.0),
    "sway": (float, 0.0, 0.0, 4.0),
    "shake": (float, 0.0, 0.0, 4.0),
    "jump": (float, 0.0, 0.0, 8.0),
    "speed": (float, 1.0, 0.25, 4.0),
    "tint_amount": (float, 0.0, 0.0, 1.0),
    "opacity": (float, 1.0, 0.1, 1.0),
    "blink_interval_ms": (int, 4000, 800, 20000),
}


def parse_params(raw: dict, ctx: Context, warnings: list) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("gaze"), (list, tuple)) and len(raw["gaze"]) >= 2:
        raw = dict(raw, gaze_x=raw["gaze"][0], gaze_y=raw["gaze"][1])
    for k, (typ, default, lo, hi) in PARAM_SPEC.items():
        if k in raw:
            out[k] = _int(raw[k], default, lo, hi) if typ is int else _float(raw[k], default, lo, hi)
    if "blink" in raw:
        out["blink"] = _choice(raw["blink"], BLINKS, "once")
    if "tint" in raw:
        c = parse_color(raw["tint"])
        if c is not None:
            out["tint"] = c
            out.setdefault("tint_amount", 0.35)
    if "background" in raw:
        out["background"] = _choice(raw["background"], BACKGROUNDS, "none")
    if "pose" in raw:
        p = str(raw["pose"])
        if not ctx.poses or p in ctx.poses:
            out["pose"] = p
        else:
            warnings.append(f"未知姿势 {p}，已忽略")
    if "ease" in raw:
        out["ease"] = _choice(raw["ease"], EASES, "inout")
    unknown = set(raw) - set(PARAM_SPEC) - {"gaze", "blink", "tint", "background", "pose", "ease", "duration_ms"}
    if unknown:
        warnings.append("忽略未知参数: " + ",".join(sorted(unknown)[:6]))
    return out


def parse_effect(raw: dict, ctx: Context, warnings: list) -> dict | None:
    name = _choice(raw.get("effect") or raw.get("name"), EFFECTS, "")
    if not name:
        warnings.append(f"未知特效 {raw.get('effect')!r}，已忽略")
        return None
    chars = "".join(sanitize_char(c) for c in str(raw.get("chars") or "")[:24]).strip()
    return {
        "name": name,
        "chars": chars,
        "color": parse_color(raw.get("color")),
        "color2": parse_color(raw.get("color2")),
        "count": _int(raw.get("count"), 24, 1, 400),
        "intensity": _float(raw.get("intensity"), 0.6, 0.0, 1.0),
        "speed": _float(raw.get("speed"), 1.0, 0.1, 5.0),
        "text": _text(raw.get("text"), 60),
        "direction": _choice(raw.get("direction"), ("up", "down", "left", "right", "out", "in", "around"), "out"),
    }


def clean_face_spec(raw: dict, warnings: list) -> dict:
    """只保留表情规格里认识的字段（取值的钳制由 face_rig.normalize 负责）。"""
    from .face_rig import MOUTHS, PUPILS, is_preset

    out = {}
    for k in FACE_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if k == "preset":
            if is_preset(v):
                out[k] = v
            else:
                warnings.append(f"未知表情预设 {v!r}，已忽略")
        elif k in ("eyes", "eye_l", "eye_r", "brows", "mouth"):
            if isinstance(v, dict):
                d = {kk: vv for kk, vv in v.items() if isinstance(vv, (int, float, str, bool))}
                if k == "mouth" and "shape" in d and d["shape"] not in MOUTHS:
                    warnings.append(f"未知嘴型 {d['shape']!r}，改为 normal")
                    d["shape"] = "normal"
                if k != "mouth" and "pupil" in d and d["pupil"] not in PUPILS:
                    warnings.append(f"未知瞳孔 {d['pupil']!r}，已忽略")
                    d.pop("pupil")
                out[k] = d
        elif k == "gaze":
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                out[k] = [v[0], v[1]]
        elif k in ("parts", "nodes"):                # 骨架字段：由 face_rig.normalize → parts.clean_rig_fields 校验钳制
            if isinstance(v, dict):
                out[k] = v
        elif k == "extra":
            if isinstance(v, list):
                out[k] = v[:8]
        elif k == "skeleton":
            if isinstance(v, str):
                out[k] = v[:12]
        else:
            out[k] = v
    return out


def parse_op(raw, ctx: Context, warnings: list) -> Op | None:
    if not isinstance(raw, dict):
        warnings.append("操作不是对象，已忽略")
        return None
    t = str(raw.get("type") or "").strip().lower()
    if t not in OP_TYPES:
        warnings.append(f"未知操作类型 {t!r}，已忽略")
        return None
    pal = _palette(raw.get("palette"))
    color = parse_color(raw.get("color"))
    loop = _choice(raw.get("loop"), LOOPS, "once")
    repeat = _int(raw.get("repeat", 1), 1, 1, 20)
    delay = _int(raw.get("delay_ms", 0), 0, 0, LIMITS["duration_max"])
    duration = _int(raw.get("duration_ms", 0), 0, 0, LIMITS["duration_max"])

    if t in ("face", "expression"):
        raw_frames = raw.get("frames")
        if raw_frames is None:
            raw_frames = [raw]
        frames = []
        if isinstance(raw_frames, list):
            for rf in raw_frames[:LIMITS["frames_per_op"]]:
                if isinstance(rf, str):
                    rf = {"preset": rf}
                if not isinstance(rf, dict):
                    continue
                spec = clean_face_spec(rf, warnings)
                ms = _int(rf.get("ms", rf.get("duration_ms", 600)), 600, LIMITS["ms_min"], LIMITS["ms_max"])
                frames.append(Frame(spec=spec, ms=ms))
        if not frames:
            warnings.append("face 操作没有有效的表情帧")
            return None
        return Op(type="face", layer="face", anchor="face", frames=frames, loop=loop, repeat=repeat,
                  hold=_choice(raw.get("hold"), ("last", "neutral"), "last"), delay_ms=delay,
                  params={"blend_ms": _int(raw.get("blend_ms", 180), 180, 0, 2000)})

    if t == "frame_sequence":
        t = "face_patch" if _choice(raw.get("target"), ("face", "region"), "face") == "face" else "region_patch"

    if t == "parameter_animation":
        params = parse_params(raw.get("params") if isinstance(raw.get("params"), dict) else raw, ctx, warnings)
        if not params:
            warnings.append("参数动画没有有效参数")
            return None
        if not duration:
            duration = _int(raw.get("params", {}).get("duration_ms") if isinstance(raw.get("params"), dict) else 0, 800, 100, LIMITS["duration_max"])
        return Op(type=t, params=params, duration_ms=max(100, duration), delay_ms=delay, layer="", anchor="")

    if t == "effect":
        eff = parse_effect(raw, ctx, warnings)
        if eff is None:
            return None
        return Op(type=t, effect=eff, anchor=_choice(raw.get("anchor"), ANCHORS, "face"),
                  x=_int(raw.get("x", 0), 0, -200, 200), y=_int(raw.get("y", 0), 0, -200, 200),
                  duration_ms=duration or 1500, delay_ms=delay, layer="effect")

    if t == "face_patch":
        frames = _frames(raw.get("frames") if "frames" in raw else [raw], ctx.face_w, ctx.face_h, pal, color, warnings)
        if not frames:
            return None
        return Op(type=t, layer="face", anchor="face", frames=frames, loop=loop, repeat=repeat,
                  hold=_choice(raw.get("hold"), ("last", "neutral", "restore"), "last"),
                  delay_ms=delay, eyes=_eyes(raw.get("eyes"), ctx.face_w, ctx.face_h))

    if t == "region_patch":
        anchor = _choice(raw.get("anchor"), ANCHORS, "body")
        layer = _choice(raw.get("layer"), REGION_LAYERS, "overlay")
        w = min(LIMITS["region_w"], ctx.canvas_w)
        h = min(LIMITS["region_h"], ctx.canvas_h)
        frames = _frames(raw.get("frames") if "frames" in raw else [raw], w, h, pal, color, warnings)
        if not frames:
            return None
        return Op(type=t, layer=layer, anchor=anchor, x=_int(raw.get("x", 0), 0, -200, 200),
                  y=_int(raw.get("y", 0), 0, -200, 200), frames=frames, loop=loop, repeat=repeat,
                  hold=_choice(raw.get("hold"), ("last", "restore"), "restore"), delay_ms=delay)

    if t == "delta_update":
        target = _choice(raw.get("layer", raw.get("target")), ("face", "body", "overlay"), "face")
        w, h = (ctx.face_w, ctx.face_h) if target == "face" else (ctx.canvas_w, ctx.canvas_h)
        f = parse_frame({"delta": raw.get("changes", raw.get("delta")), "ms": raw.get("ms", 200),
                         "palette": raw.get("palette"), "color": raw.get("color")}, w, h, pal, color, warnings)
        if f is None:
            return None
        anchor = "face" if target == "face" else _choice(raw.get("anchor"), ANCHORS, "body")
        return Op(type=t, layer=target, anchor=anchor, frames=[f],
                  hold=_choice(raw.get("hold"), ("last", "restore", "neutral"), "last"), delay_ms=delay)

    if t == "full_scene":
        frames = _frames(raw.get("frames"), ctx.canvas_w, ctx.canvas_h, pal, color, warnings) if raw.get("frames") else []
        rules = []
        for r in (raw.get("rules") or [])[:6]:
            if isinstance(r, dict):
                e = parse_effect(r, ctx, warnings)
                if e:
                    rules.append(e)
        if not frames and not rules:
            warnings.append("full_scene 没有帧也没有规则")
            return None
        hide = [h for h in (raw.get("hide") or ["body", "face"]) if h in ("body", "face", "background", "effects")]
        return Op(type=t, layer="scene", anchor="canvas", frames=frames, loop=loop, repeat=repeat,
                  hold=_choice(raw.get("then", raw.get("hold")), ("restore", "hold", "last"), "restore"),
                  delay_ms=delay, hide=hide, effect={"rules": rules},
                  duration_ms=duration)
    return None


def parse_animation(data, ctx: Context | None = None, source: str = "model") -> Animation:
    """解析并校验一段模型输出（str 或 dict）。没有任何有效操作时抛出 ProtocolError。"""
    ctx = ctx or Context()
    obj = extract_json(data) if not isinstance(data, dict) else data
    warnings: list = []
    raw_ops = obj.get("ops")
    if raw_ops is None and obj.get("type"):
        raw_ops = [obj]
    if not isinstance(raw_ops, list):
        raise ProtocolError("缺少 ops 数组")
    if len(raw_ops) > LIMITS["ops"]:
        warnings.append(f"操作超过 {LIMITS['ops']} 个，已截断")
    ops = []
    frames_total = 0
    for r in raw_ops[:LIMITS["ops"]]:
        op = parse_op(r, ctx, warnings)
        if op is None:
            continue
        if frames_total + len(op.frames) > LIMITS["frames_total"]:
            warnings.append("总关键帧数超限，后续操作已丢弃")
            break
        frames_total += len(op.frames)
        ops.append(op)
    if not ops:
        raise ProtocolError("没有有效的动画操作: " + "; ".join(warnings[:4]))
    fills = [w for w in warnings if w.endswith("已按颜色填实")]
    if len(fills) > 1:
        n = sum(int(w.split(" ")[0]) for w in fills if w.split(" ")[0].isdigit())
        warnings = [w for w in warnings if not w.endswith("已按颜色填实")]
        warnings.append(f"{n} 个调色板字母被当作图案写进了 rows（{len(fills)} 处），已按颜色填实")
    mode = _int(obj.get("mode", 2), 2, 1, 3)
    end = obj.get("end_state") if isinstance(obj.get("end_state"), dict) else {}
    end_state = {"face": _choice(end.get("face"), ("hold", "neutral"), "hold")}
    if end.get("pose") and (not ctx.poses or end["pose"] in ctx.poses):
        end_state["pose"] = str(end["pose"])
    return Animation(
        mode=mode,
        intent=_text(obj.get("intent"), 80),
        expression=_text(obj.get("expression") or obj.get("intent"), 60),
        think=_ascii_text(obj.get("think"), 90),
        priority=_int(obj.get("priority", 5), 5, 0, 9),
        ops=ops,
        end_state=end_state,
        warnings=warnings,
        raw=obj,
        face_dims=(ctx.face_w, ctx.face_h),
    )
