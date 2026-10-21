"""面部骨架（类似 Live2D 的参数化五官）。

原理：表情先在**像素级**画到“无五官”原图的脸上（粗黑上眼线、白色眼白、蓝色虹膜、星形高光、腮红、嘴巴……
都按原画风格绘制），再走与身体完全相同的字符化流程（同一裁剪网格、同一亮度梯度与配色），
所以五官的位置、比例、密度和颜色永远与原画一致，不会出现“瞳孔跑位”“嘴变成一条”。

默认参数时直接使用原画的五官像素；任何一项参数偏离默认值，该部位就改为程序绘制。

表情规格（spec，JSON 字典，所有字段可选）：
  preset      预设名（见 PRESETS），其余字段在预设基础上覆盖
  eyes        {"open":0~1.3, "smile":0~1, "tilt":-1~1, "iris":0.5~1.3, "pupil":"art|round|heart|star|spiral|dot|x",
               "tear":0~1, "highlight":0~1.5,
               "droop":-1~1（上眼睑绕内眼角转：正=外眼角下垂，负=上挑）, "lower":0~1（下眼睑往上抬：眯眼）}   两眼共用
  eye_l/eye_r 同上，单独覆盖某一只眼（眨单眼、不对称表情）
  gaze        [x, y] -1~1                                  视线
  brows       {"y":-1~1 (抬高为正), "angle":-1~1 (负=八字担心，正=生气), "show":true}
  mouth       {"shape":"art|normal|line|wavy|cat|pout|o|triangle|teeth|tongue", "open":0~1, "width":0.4~1.6, "curve":-1~1,
               "skew":-1~1（歪嘴：正=右边嘴角高）}
  blush 0~1   sweat 0~1   gloom 0~1（额头阴影线）
  parts / nodes / extra（v0.7，可选）：某个节点换成自创组件、移动 / 缩放 / 旋转节点、新增节点（见 parts.py）
  skeleton    已保存的骨架方案名（展开成上面三组字段）
"""
from __future__ import annotations

import json
import math
import os
import threading
from collections import OrderedDict

EYE_DEFAULT = {"open": 1.0, "smile": 0.0, "tilt": 0.0, "iris": 1.0, "pupil": "art", "tear": 0.0, "highlight": 1.0,
               "droop": 0.0, "lower": 0.0}
BROW_DEFAULT = {"y": 0.0, "angle": 0.0, "show": True}
MOUTH_DEFAULT = {"shape": "art", "open": 0.0, "width": 1.0, "curve": 0.0, "skew": 0.0}
PUPILS = ("art", "round", "heart", "star", "spiral", "dot", "x")
MOUTHS = ("art", "normal", "line", "wavy", "cat", "pout", "o", "triangle", "teeth", "tongue")

PRESETS = {
    "neutral": {},
    "calm": {"eyes": {"open": 0.85}, "mouth": {"shape": "normal", "curve": 0.25}, "brows": {"angle": -0.2}, "blush": 0.3},
    "smile": {"eyes": {"smile": 0.35}, "mouth": {"shape": "normal", "curve": 0.7}, "brows": {"angle": -0.2}, "blush": 0.45},
    "happy": {"eyes": {"smile": 1.0}, "mouth": {"shape": "normal", "open": 0.45, "curve": 0.8}, "brows": {"y": 0.3, "angle": -0.3}, "blush": 0.6},
    "laugh": {"eyes": {"smile": 1.0}, "mouth": {"shape": "normal", "open": 0.95, "curve": 0.9, "width": 1.3}, "brows": {"y": 0.4, "angle": -0.4}, "blush": 0.7},
    "grin": {"eyes": {"smile": 0.5, "lower": 0.3}, "mouth": {"shape": "teeth", "open": 0.55, "curve": 0.8, "width": 1.3}, "blush": 0.4},
    "shy": {"eyes": {"open": 0.75, "smile": 0.3, "lower": 0.3}, "gaze": [-0.6, 0.5], "mouth": {"shape": "wavy", "width": 0.7}, "brows": {"angle": -0.5}, "blush": 1.0},
    "embarrassed": {"eyes": {"open": 0.95, "tear": 0.25}, "mouth": {"shape": "wavy", "open": 0.2}, "brows": {"angle": -0.7}, "blush": 1.0, "sweat": 0.6},
    "sad": {"eyes": {"open": 0.8, "tilt": -0.7, "droop": 0.3}, "gaze": [0.0, 0.5], "mouth": {"shape": "normal", "curve": -0.6, "width": 0.8}, "brows": {"angle": -0.9, "y": 0.2}, "gloom": 0.2},
    "cry": {"eyes": {"smile": 0.6, "tilt": -0.6, "tear": 1.0}, "mouth": {"shape": "normal", "open": 0.5, "curve": -0.8}, "brows": {"angle": -1.0, "y": 0.3}, "blush": 0.5},
    "teary": {"eyes": {"open": 1.05, "tear": 0.7, "highlight": 1.4}, "mouth": {"shape": "wavy", "width": 0.7}, "brows": {"angle": -0.8}, "blush": 0.5},
    "angry": {"eyes": {"open": 0.85, "tilt": 0.9, "droop": -0.4}, "mouth": {"shape": "normal", "curve": -0.5, "open": 0.25}, "brows": {"angle": 1.0, "y": -0.3}, "blush": 0.2},
    "pout": {"eyes": {"open": 0.8, "tilt": 0.35}, "mouth": {"shape": "pout"}, "brows": {"angle": 0.5}, "blush": 0.7},
    "surprised": {"eyes": {"open": 1.25, "iris": 0.75}, "mouth": {"shape": "o", "open": 0.5}, "brows": {"y": 0.9, "angle": -0.2}},
    "shocked": {"eyes": {"open": 1.3, "iris": 0.5, "pupil": "dot"}, "mouth": {"shape": "triangle", "open": 0.8}, "brows": {"y": 1.0, "angle": -0.4}, "gloom": 0.6, "sweat": 0.7},
    "confused": {"eye_l": {"open": 0.7}, "eye_r": {"open": 1.1}, "gaze": [0.3, -0.4], "mouth": {"shape": "wavy", "width": 0.8}, "brows": {"angle": -0.3, "y": 0.3}, "sweat": 0.4},
    "sleepy": {"eyes": {"open": 0.32, "droop": 0.5}, "mouth": {"shape": "o", "open": 0.15}, "brows": {"y": -0.2, "angle": -0.2}},
    "bored": {"eyes": {"open": 0.55, "droop": 0.35, "lower": 0.2}, "gaze": [0.7, 0.0], "mouth": {"shape": "line", "width": 0.7}},
    "smug": {"eyes": {"open": 0.6, "smile": 0.4, "tilt": 0.3, "lower": 0.5}, "mouth": {"shape": "cat"}, "brows": {"angle": 0.3}, "blush": 0.3},
    "wink": {"eye_l": {"smile": 0.3}, "eye_r": {"smile": 1.0}, "mouth": {"shape": "normal", "curve": 0.8, "open": 0.3}, "blush": 0.5},
    "love": {"eyes": {"open": 1.05, "smile": 0.25, "pupil": "heart"}, "mouth": {"shape": "normal", "open": 0.35, "curve": 0.8}, "blush": 0.9},
    "starstruck": {"eyes": {"open": 1.15, "pupil": "star"}, "mouth": {"shape": "normal", "open": 0.6, "curve": 0.9}, "brows": {"y": 0.5}, "blush": 0.6},
    "dizzy": {"eyes": {"pupil": "spiral"}, "mouth": {"shape": "wavy", "open": 0.2}, "brows": {"angle": -0.4}, "sweat": 0.5},
    "determined": {"eyes": {"open": 0.95, "tilt": 0.5, "droop": -0.3}, "mouth": {"shape": "normal", "curve": 0.2, "width": 0.8}, "brows": {"angle": 0.6}},
    "thinking": {"eyes": {"open": 0.9}, "gaze": [0.6, -0.7], "mouth": {"shape": "line", "width": 0.6, "curve": -0.1}, "brows": {"angle": -0.2, "y": 0.2}},
    "worried": {"eyes": {"open": 1.0, "tilt": -0.3}, "mouth": {"shape": "wavy", "width": 0.8}, "brows": {"angle": -0.9, "y": 0.2}, "sweat": 0.3},
    "scared": {"eyes": {"open": 1.25, "iris": 0.6, "tilt": -0.4}, "mouth": {"shape": "wavy", "open": 0.3}, "brows": {"angle": -1.0, "y": 0.6}, "sweat": 0.8, "gloom": 0.4},
    "dead": {"eyes": {"pupil": "x"}, "mouth": {"shape": "wavy", "open": 0.2}, "gloom": 0.4},
    "closed": {"eyes": {"open": 0.0}, "mouth": {"shape": "normal", "curve": 0.3}, "blush": 0.3},
    "cat": {"eyes": {"smile": 0.8}, "mouth": {"shape": "cat"}, "blush": 0.5},
}


PRESET_CN = {
    "neutral": "平静", "calm": "安然", "smile": "微笑", "happy": "开心", "laugh": "大笑", "grin": "咧嘴笑", "shy": "害羞",
    "embarrassed": "窘迫", "sad": "难过", "cry": "哭泣", "teary": "泪汪汪", "angry": "生气", "pout": "鼓脸", "surprised": "惊讶",
    "shocked": "震惊", "confused": "困惑", "sleepy": "困倦", "bored": "无聊", "smug": "得意", "wink": "眨眼", "love": "心动",
    "starstruck": "星星眼", "dizzy": "晕乎乎", "determined": "认真", "thinking": "思考", "worried": "担心", "scared": "害怕",
    "dead": "灵魂出窍", "closed": "闭眼", "cat": "猫猫嘴",
}

# 自创表情注册表（名字 → 规整后的完整规格），由 custom_presets.CustomPresets 维护
CUSTOM: dict = {}
# 表情进化改良过的内置表情（内置名 → 完整规格）；删除即恢复原版
OVERRIDES: dict = {}
# 正式组件库（名字 → 组件规格）、它们的摘要（进渲染缓存键）、已保存的骨架方案；由 parts.PartsLibrary 维护
PARTS: dict = {}
PARTS_DIGEST: dict = {}
SKELETONS: dict = {}
# 五官画法：morph  = 画师手绘差分的连续变形（v0.8，morph_face.py；默认）：眼睛开合、笑眼、眼角、下眼睑、眉毛、嘴都连续可调；
#           layers = 手绘差分分档切换（v0.7.1，drawn_face.py）；drawn = 以前的程序绘制
EYE_STYLE = "morph"


def is_preset(name) -> bool:
    return isinstance(name, str) and (name in PRESETS or name in CUSTOM)


def display_name(name: str) -> str:
    return PRESET_CN.get(name, name)


def _clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return max(lo, min(hi, v))


def _eye(raw: dict | None, base: dict) -> dict:
    out = dict(base)
    if isinstance(raw, dict):
        for k, (lo, hi) in (("open", (0.0, 1.3)), ("smile", (0.0, 1.0)), ("tilt", (-1.0, 1.0)), ("iris", (0.5, 1.3)),
                            ("tear", (0.0, 1.0)), ("highlight", (0.0, 1.5)), ("droop", (-1.0, 1.0)), ("lower", (0.0, 1.0))):
            if k in raw:
                out[k] = _clamp(raw[k], lo, hi, out[k])
        if raw.get("pupil") in PUPILS:
            out["pupil"] = raw["pupil"]
    return out


def normalize(spec: dict | None, base: dict | None = None) -> dict:
    """把（可能来自模型的）表情规格规整成完整、取值合法的字典。base 为上一个状态（用于增量修改）。"""
    spec = dict(spec or {})
    preset = spec.get("preset")
    if isinstance(preset, str) and preset in OVERRIDES and preset in PRESETS:
        return normalize({k: v for k, v in spec.items() if k != "preset"}, OVERRIDES[preset])
    if isinstance(preset, str) and preset not in PRESETS and preset in CUSTOM:
        # 自创表情：以它保存的完整规格为起点，再叠加本次的覆盖字段（强度 level 照常生效）
        return normalize({k: v for k, v in spec.items() if k != "preset"}, CUSTOM[preset])
    cur = base or {}
    rigf = _rig_fields_of(spec, cur, preset)
    if isinstance(preset, str) and preset in PRESETS:
        layered = json.loads(json.dumps(PRESETS[preset]))
        for k, v in spec.items():
            if k == "preset":
                continue
            if isinstance(v, dict) and isinstance(layered.get(k), dict):
                layered[k] = {**layered[k], **v}
            else:
                layered[k] = v
        spec, cur = layered, {}
    eyes_base = _eye(spec.get("eyes"), cur.get("eye_l", EYE_DEFAULT) if not spec.get("eyes") and not preset else EYE_DEFAULT)
    if not spec.get("eyes") and not preset and cur:
        eye_l, eye_r = dict(cur["eye_l"]), dict(cur["eye_r"])
    else:
        eye_l, eye_r = dict(eyes_base), dict(eyes_base)
    eye_l = _eye(spec.get("eye_l"), eye_l)
    eye_r = _eye(spec.get("eye_r"), eye_r)
    g = spec.get("gaze", cur.get("gaze", [0.0, 0.0]) if not preset else [0.0, 0.0])
    if not isinstance(g, (list, tuple)) or len(g) < 2:
        g = [0.0, 0.0]
    gaze = [_clamp(g[0], -1, 1, 0.0), _clamp(g[1], -1, 1, 0.0)]
    braw = spec.get("brows") if isinstance(spec.get("brows"), dict) else {}
    bbase = cur.get("brows", BROW_DEFAULT) if not preset and "brows" not in spec else BROW_DEFAULT
    brows = {"y": _clamp(braw.get("y", bbase["y"]), -1, 1, 0.0),
             "angle": _clamp(braw.get("angle", bbase["angle"]), -1, 1, 0.0),
             "show": bool(braw.get("show", bbase.get("show", True)))}
    mraw = spec.get("mouth") if isinstance(spec.get("mouth"), dict) else {}
    mbase = cur.get("mouth", MOUTH_DEFAULT) if not preset and "mouth" not in spec else MOUTH_DEFAULT
    shape = mraw.get("shape", mbase["shape"])
    if shape not in MOUTHS:
        shape = "normal"
    mouth = {"shape": shape, "open": _clamp(mraw.get("open", mbase["open"]), 0, 1, 0.0),
             "width": _clamp(mraw.get("width", mbase["width"]), 0.4, 1.6, 1.0),
             "curve": _clamp(mraw.get("curve", mbase["curve"]), -1, 1, 0.0),
             "skew": _clamp(mraw.get("skew", mbase.get("skew", 0.0)), -1, 1, 0.0)}
    if shape == "art" and (mouth["open"] > 0.05 or abs(mouth["curve"]) > 0.05 or abs(mouth["width"] - 1) > 0.05
                           or abs(mouth["skew"]) > 0.05):
        mouth["shape"] = "normal"
    out = {"eye_l": eye_l, "eye_r": eye_r, "gaze": gaze, "brows": brows, "mouth": mouth}
    for k, d in (("blush", 0.35), ("sweat", 0.0), ("gloom", 0.0)):
        out[k] = _clamp(spec.get(k, cur.get(k, d) if not preset else d), 0, 1, d)
    out.update(rigf)
    level = _clamp(spec.get("level", 1.0), 0.0, 1.0, 1.0)
    if level < 0.999:
        # 表情强度：在中性脸与目标表情之间插值（离散的瞳孔/嘴型在 0.5 以上才切换）
        out = lerp_spec(normalize({}), out, level)
    return out


def neutral() -> dict:
    return normalize({})


def _rig_fields_of(spec: dict, cur: dict, preset) -> dict:
    """骨架字段（parts / nodes / extra）：本帧写了就用本帧的（叠在骨架方案上），没写就沿用上一帧（预设起点除外）。"""
    from . import parts as pt

    sk = spec.get("skeleton")
    base = dict(SKELETONS.get(sk) or {}) if isinstance(sk, str) else {}
    own = {k: spec[k] for k in pt.RIG_KEYS if k in spec}
    if own or base:
        merged = dict(base)
        for k, v in own.items():
            if k == "parts" and isinstance(v, dict) and isinstance(merged.get("parts"), dict):
                merged["parts"] = {**merged["parts"], **v}
            elif k == "nodes" and isinstance(v, dict) and isinstance(merged.get("nodes"), dict):
                merged["nodes"] = {**merged["nodes"], **v}
            else:
                merged[k] = v
        return pt.clean_rig_fields(merged)
    if preset or not cur:
        return {}
    return {k: cur[k] for k in pt.RIG_KEYS if cur.get(k)}


def normalize_full(spec: dict) -> dict:
    """重新校验一份已保存的完整规格（读取自创表情库时用）。"""
    return normalize({k: v for k, v in spec.items() if k != "preset"})


def lerp_spec(a: dict, b: dict, t: float) -> dict:
    """两个规格之间插值（连续参数线性插值，离散参数在中点切换）。"""
    def num(x, y):
        return x + (y - x) * t

    def eye(ea, eb):
        out = {k: num(ea.get(k, EYE_DEFAULT[k]), eb.get(k, EYE_DEFAULT[k]))
               for k in ("open", "smile", "tilt", "iris", "tear", "highlight", "droop", "lower")}
        out["pupil"] = ea["pupil"] if t < 0.5 else eb["pupil"]
        return out

    out = {
        "eye_l": eye(a["eye_l"], b["eye_l"]), "eye_r": eye(a["eye_r"], b["eye_r"]),
        "gaze": [num(a["gaze"][0], b["gaze"][0]), num(a["gaze"][1], b["gaze"][1])],
        "brows": {"y": num(a["brows"]["y"], b["brows"]["y"]), "angle": num(a["brows"]["angle"], b["brows"]["angle"]),
                  "show": a["brows"]["show"] if t < 0.5 else b["brows"]["show"]},
        "mouth": {"shape": a["mouth"]["shape"] if t < 0.5 else b["mouth"]["shape"],
                  "open": num(a["mouth"]["open"], b["mouth"]["open"]), "width": num(a["mouth"]["width"], b["mouth"]["width"]),
                  "curve": num(a["mouth"]["curve"], b["mouth"]["curve"]),
                  "skew": num(a["mouth"].get("skew", 0.0), b["mouth"].get("skew", 0.0))},
        "blush": num(a["blush"], b["blush"]), "sweat": num(a["sweat"], b["sweat"]), "gloom": num(a["gloom"], b["gloom"]),
    }
    if any(k in a or k in b for k in ("parts", "nodes", "extra")):
        from . import parts as pt

        out.update(pt.lerp_fields(a, b, t))
    return out


def spec_key(spec: dict, digests: dict | None = None) -> tuple:
    """量化后的缓存键（避免补间产生无穷多个渲染结果）。用到组件时，组件的内容摘要也进键（改了组件就重画）。"""
    q = lambda v, s=0.05: round(v / s) * s  # noqa: E731

    def eye(e):
        return (q(e["open"]), q(e["smile"]), q(e["tilt"]), q(e["iris"]), e["pupil"], q(e["tear"], 0.1), q(e["highlight"], 0.25),
                q(e.get("droop", 0.0), 0.1), q(e.get("lower", 0.0), 0.1))

    m = spec["mouth"]
    return (eye(spec["eye_l"]), eye(spec["eye_r"]), q(spec["gaze"][0], 0.1), q(spec["gaze"][1], 0.1),
            q(spec["brows"]["y"], 0.1), q(spec["brows"]["angle"], 0.1), spec["brows"]["show"],
            m["shape"], q(m["open"]), q(m["width"]), q(m["curve"], 0.1), q(m.get("skew", 0.0), 0.1),
            q(spec["blush"], 0.1), q(spec["sweat"], 0.1), q(spec["gloom"], 0.1)) + _fields_key(spec, digests)


def _fields_key(spec: dict, digests) -> tuple:
    if not ("parts" in spec or "nodes" in spec or "extra" in spec):
        return ()
    from . import parts as pt

    return pt.fields_key(spec, PARTS_DIGEST if digests is None else digests)


def describe(spec: dict) -> dict:
    """给模型看的紧凑描述（只列与默认不同的字段）。"""
    out = {}
    for side in ("eye_l", "eye_r"):
        d = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in spec[side].items() if v != EYE_DEFAULT.get(k)}
        if d:
            out[side] = d
    if spec["gaze"] != [0.0, 0.0]:
        out["gaze"] = [round(v, 2) for v in spec["gaze"]]
    b = {k: round(v, 2) for k, v in spec["brows"].items() if k != "show" and v != BROW_DEFAULT[k]}
    if b:
        out["brows"] = b
    m = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in spec["mouth"].items() if v != MOUTH_DEFAULT.get(k)}
    if m:
        out["mouth"] = m
    for k in ("blush", "sweat", "gloom"):
        if abs(spec[k] - (0.35 if k == "blush" else 0.0)) > 0.01:
            out[k] = round(spec[k], 2)
    for k in ("parts", "nodes", "extra"):
        if spec.get(k):
            out[k] = spec[k]
    return out


# ---------------------------------------------------------------------- 照镜子
PUPIL_CN = {"round": "圆圆的瞳孔", "heart": "心形瞳孔", "star": "星星瞳孔", "spiral": "漩涡瞳孔", "dot": "瞳孔缩成小点"}
MOUTH_CN = {"art": "原画的小嘴", "normal": "嘴", "line": "抿成一字的嘴", "wavy": "波浪形的嘴", "cat": "ω 形的猫猫嘴",
            "pout": "嘟起的嘴", "o": "O 形的嘴", "triangle": "三角形的嘴", "teeth": "露出牙齿的嘴", "tongue": "吐着舌头的嘴"}


def _eye_words(e: dict) -> str:
    w = []
    if e["pupil"] == "x":
        w.append("画成 × 形的叉叉眼")
    elif e["open"] < 0.1:
        w.append("闭着")
    elif e["smile"] >= 0.72:
        w.append("弯成 ^ 形的笑眼")
    else:
        o = e["open"]
        w.append("眯成一条缝" if o < 0.35 else "半睁着" if o < 0.7 else "微微眯着" if o < 0.9
                 else "正常睁着" if o <= 1.1 else "瞪得很大")
        if e["smile"] >= 0.45:
            w.append("笑得眯了起来")
        elif e["smile"] >= 0.2:
            w.append("带着笑意")
        if e.get("droop", 0) > 0.3:
            w.append("外眼角往下耷拉")
        elif e.get("droop", 0) < -0.3:
            w.append("外眼角往上挑")
        if e.get("lower", 0) > 0.35:
            w.append("下眼睑抬起、眯着眼")
        if e["tilt"] > 0.3:
            w.append("眼角上挑显得凶")
        elif e["tilt"] < -0.3:
            w.append("眼角下垂显得委屈")
        if e["pupil"] in PUPIL_CN:
            w.append(PUPIL_CN[e["pupil"]])
        if e["pupil"] != "dot":
            if e["iris"] < 0.8:
                w.append("瞳孔缩小")
            elif e["iris"] > 1.1:
                w.append("瞳孔放大")
        if e["highlight"] < 0.4:
            w.append("没有高光、显得空洞")
        elif e["highlight"] > 1.25:
            w.append("高光闪亮")
    if e["tear"] > 0.45:
        w.append("在流泪")
    elif e["tear"] > 0.15:
        w.append("泛着泪光")
    return "、".join(w)


def mirror_text(spec: dict) -> str:
    """照镜子：把（规整后的）表情规格翻译成“实际画出来是什么样子”的中文描述，给看不见图的模型用。"""
    parts = []
    swapped = {n: v for n, v in (spec.get("parts") or {}).items()}      # 换成组件 / 隐藏的部位：不再按内置参数描述
    el = "" if "eye_l" in swapped else _eye_words(spec["eye_l"])
    er = "" if "eye_r" in swapped else _eye_words(spec["eye_r"])
    if el and er:
        parts.append(f"双眼{el}" if el == er else f"左眼{el}，右眼{er}")
    elif el or er:
        parts.append(f"左眼{el}" if el else f"右眼{er}")
    gx, gy = spec["gaze"]
    if abs(gx) > 0.25 or abs(gy) > 0.25:
        h = "左" if gx < -0.25 else "右" if gx > 0.25 else ""
        v = "上" if gy < -0.25 else "下" if gy > 0.25 else ""
        parts.append("视线看向" + ((h + v) if h and v else ("左边" if h == "左" else "右边" if h == "右" else
                                                        "上方" if v == "上" else "下方")))
    m = spec["mouth"]
    if "mouth" in swapped:
        pass
    elif m["shape"] == "art":
        parts.append("原画的小嘴（平静）")
    else:
        o = m["open"]
        mw = ["闭着的" if o < 0.1 else "微张的" if o < 0.35 else "张开的" if o < 0.7 else "大张的"]
        if m["width"] < 0.75:
            mw.append("小小的")
        elif m["width"] > 1.25:
            mw.append("咧得很宽的")
        desc = "".join(mw) + MOUTH_CN[m["shape"]]
        if m["curve"] > 0.3:
            desc += "，嘴角上扬"
        elif m["curve"] < -0.3:
            desc += "，嘴角下撇"
        if abs(m.get("skew", 0)) > 0.3:
            desc += "，歪着（" + ("右" if m["skew"] > 0 else "左") + "边嘴角高）"
        parts.append(desc)
    b = spec["blush"]
    parts.append("满脸通红" if b > 0.75 else "脸颊泛红" if b > 0.45 else "淡淡的红晕" if b > 0.2 else "没有脸红")
    if spec["sweat"] > 0.3:
        parts.append("额头挂着汗滴")
    if spec["gloom"] > 0.3:
        parts.append("额头有阴沉的黑线")
    br = spec["brows"]
    if abs(br["angle"]) > 0.2 or abs(br["y"]) > 0.2:
        parts.append("（眉毛的变化几乎看不出来：它画在深色刘海上）")
    parts += _rig_words(spec)
    return "；".join(parts)


NODE_CN = {"eye_l": "左眼", "eye_r": "右眼", "brow_l": "左眉", "brow_r": "右眉", "mouth": "嘴", "cheek_l": "左脸颊",
           "cheek_r": "右脸颊", "forehead": "额头", "sweat": "汗滴位置"}


def _rig_words(spec: dict) -> list:
    """骨架字段的本地描述（按数据直接翻译，不是模型的想象）。"""
    out = []
    for node, v in (spec.get("parts") or {}).items():
        out.append(f"{NODE_CN.get(node, node)}" + ("被隐藏" if v == "none" else f"换成了组件「{v}」"))
    for node, t in (spec.get("nodes") or {}).items():
        w = []
        if abs(t.get("dx", 0)) > 0.01:
            w.append(("右" if t["dx"] > 0 else "左") + f"移 {abs(t['dx']):.0%}")
        if abs(t.get("dy", 0)) > 0.01:
            w.append(("下" if t["dy"] > 0 else "上") + f"移 {abs(t['dy']):.0%}")
        if abs(t.get("sx", 1) - 1) > 0.05 or abs(t.get("sy", 1) - 1) > 0.05:
            w.append(f"缩放 {t.get('sx', 1):.2g}×{t.get('sy', 1):.2g}")
        if abs(t.get("rot", 0)) > 2:
            w.append(f"旋转 {t['rot']:.0f}°")
        if w:
            out.append(f"{NODE_CN.get(node, node)}" + "、".join(w))
    for e in spec.get("extra") or []:
        out.append(f"新增节点 {e['id']}（脸框 {e['x']:.0%},{e['y']:.0%} 处）画着组件「{e['part']}」")
    return out


# ---------------------------------------------------------------------- 渲染
def _hex(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


class FaceRig:
    """加载某个角色视图的骨架；render(spec) → 面部区域的 (chars, colors)。"""

    def __init__(self, asset_dir: str, info: dict):
        from PIL import Image

        self.info = info
        self.fw, self.fh = info["cells"]
        self.px_box = info["px_box"]               # 相对于裁剪图左上角的浮点像素框（与字符格严格对齐）
        self.norm = tuple(info["norm"])
        with open(os.path.join(asset_dir, info["rig"]), encoding="utf-8") as f:
            self.cal = json.load(f)
        self.origin = info["crop_origin"]
        self.blank = Image.open(os.path.join(asset_dir, info["blank"])).convert("RGB")
        self.art = Image.open(os.path.join(asset_dir, info["neutral"])).convert("RGB")
        self.cw = (self.px_box[2] - self.px_box[0]) / self.fw
        self.ch = (self.px_box[3] - self.px_box[1]) / self.fh
        self.col = {k: _hex(v) for k, v in self.cal["colors"].items()}
        self._masks = self._part_masks()
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        self._nodes = None
        self.asset_dir = asset_dir
        self._drawn = None
        self._drawn_tried = False
        self.skin = self._skin_color()

    @property
    def drawn(self):
        """手绘差分（drawn_face.DrawnFace）；没有素材或设置成程序绘制时为 None。"""
        if EYE_STYLE not in ("layers", "morph"):
            return None
        if not self._drawn_tried:
            self._drawn_tried = True
            folder = os.path.join(self.asset_dir, "face_layers")
            if os.path.exists(os.path.join(folder, "manifest.json")):
                try:
                    from .drawn_face import DrawnFace

                    self._drawn = DrawnFace(self, folder)
                except Exception as exc:
                    import logging

                    logging.getLogger("pet.ascii").warning("字符画：手绘差分加载失败（%s），改用程序绘制", exc)
        return self._drawn

    @property
    def nodes(self) -> dict:
        """默认骨架节点（裁剪图像素坐标）。"""
        if self._nodes is None:
            from . import parts as pt

            self._nodes = pt.default_nodes(self)
        return self._nodes

    def _skin_color(self) -> tuple:
        try:
            x, y = self.p(*self.cal["cheeks"][0])
            px = [self.blank.getpixel((int(x) + dx, int(y) + dy)) for dx in (-6, 0, 6) for dy in (-4, 0, 4)]
            return tuple(int(sum(c[i] for c in px) / len(px)) for i in range(3))
        except Exception:
            return (240, 220, 210)

    def cell_of_px(self, px: float, py: float) -> tuple[float, float]:
        """裁剪图像素 → 面部区域字符格坐标。"""
        return (px - self.px_box[0]) / self.cw, (py - self.px_box[1]) / self.ch

    # 坐标：标定为整幅原图像素，转换为裁剪图坐标
    def p(self, x, y):
        return x - self.origin[0], y - self.origin[1]

    def cell(self, x, y) -> tuple[float, float]:
        """标定图上的像素坐标 → 面部区域里的字符格坐标（可以超出面部区域）。"""
        px, py = self.p(x, y)
        return (px - self.px_box[0]) / self.cw, (py - self.px_box[1]) / self.ch

    def _part_masks(self):
        from PIL import ImageChops, ImageDraw, ImageFilter
        from PIL import Image

        diff = ImageChops.difference(self.blank, self.art).convert("L").point(lambda v: 255 if v > 22 else 0)
        diff = diff.filter(ImageFilter.MaxFilter(5))
        masks = {}

        def region(name, box):
            m = Image.new("L", self.blank.size, 0)
            x0, y0 = self.p(box[0], box[1])
            x1, y1 = self.p(box[2], box[3])
            ImageDraw.Draw(m).rectangle([x0, y0, x1, y1], fill=255)
            masks[name] = ImageChops.multiply(m, diff).filter(ImageFilter.GaussianBlur(0.6))

        for i, e in enumerate(self.cal["eyes"]):
            region(f"eye{i}", e["region"])
        for i, b in enumerate(self.cal["brows"]):
            region(f"brow{i}", b["region"])
        region("mouth", self.cal["mouth"]["region"])
        return masks

    def render(self, spec: dict, parts: dict | None = None):
        """spec → 面部区域的 (chars, colors)。parts 给出时用这份组件表（实验中的候选组件），否则用正式组件库。"""
        if parts is None:
            key = spec_key(spec)
        else:
            from . import parts as pt

            key = spec_key(spec, {n: pt.digest(c) for n, c in parts.items()}) + ("cand",)
        with self._lock:          # 表情进化的“照镜子”、创作实验会在后台线程里渲染
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
            cells = self._render_uncached(spec, parts)
            self._cache[key] = cells
            if len(self._cache) > 400:
                self._cache.popitem(last=False)
            return cells

    def _render_uncached(self, spec: dict, parts: dict | None = None):
        if not ("parts" in spec or "nodes" in spec or "extra" in spec):
            return self._to_cells(self.draw(spec))
        img, layers, info = self.compose(spec, parts)
        chars, colors = self._to_cells(img)
        chars = [list(r) for r in chars]
        colors = [list(r) for r in colors]
        self._apply_glyphs(chars, colors, info)
        return chars, colors

    def _apply_glyphs(self, chars, colors, info) -> list:
        from . import parts as pt

        touched = []
        for node, comp, geom in info.get("glyphs", []):
            touched += pt.overlay_glyphs(chars, colors, comp, self.cell_of_px(geom["cx"], geom["cy"]), self.col, self.skin)
        return touched

    DARK = 70        # 两边都比这更暗的格子（深色刘海上的眉毛等）肉眼看不出变化，不计入

    def visible_diff(self, a: dict, b: dict, parts: dict | None = None) -> float:
        """两个表情渲染成字符后，面部区域里看得出不同的格子比例（0~1）：字符或颜色明显不同，且不是暗对暗。"""
        ca, ka = self.render(a, parts)
        cb, kb = self.render(b, parts)

        def lum(k):
            return 0.299 * k[0] + 0.587 * k[1] + 0.114 * k[2] if k else 0.0

        n = diff = 0
        for y in range(len(ca)):
            for x in range(len(ca[y])):
                n += 1
                k1, k2 = ka[y][x], kb[y][x]
                if max(lum(k1), lum(k2)) < self.DARK:
                    continue
                if ca[y][x] != cb[y][x]:
                    diff += 1
                elif k1 and k2 and sum(abs(k1[i] - k2[i]) for i in range(3)) > 60:
                    diff += 1
        return diff / max(1, n)

    def _to_cells(self, img):
        from PIL import Image

        from .convert import render_cells

        small = img.convert("RGBA").resize((self.fw, self.fh), Image.BOX, box=tuple(self.px_box))
        raw = [[small.getpixel((x, y)) for x in range(self.fw)] for y in range(self.fh)]
        return render_cells(raw, norm=self.norm)

    # ------------------------------------------------------------------ 分层绘制（带骨架字段的规格）
    def compose(self, spec: dict, parts: dict | None = None, want_layers: bool = False):
        """通用绘制：每个节点单独一层（内置五官 / 自创组件 / 隐藏），按节点变换合成。
        返回 (RGB 图, {节点: RGBA 图层}（want_layers 时）, info)。info 里有用到的组件、缺失的组件、字符纹理。"""
        from PIL import Image, ImageDraw, ImageFilter

        from . import parts as pt

        lib = PARTS if parts is None else parts
        fields = pt.rig_fields(spec)
        nodes = self.nodes
        size = self.blank.size
        pmap = fields.get("parts", {})
        tmap = fields.get("nodes", {})
        img = self.blank.copy().convert("RGBA")
        layers: dict = {}
        info = {"used": [], "missing": [], "glyphs": []}

        def part_of(node):
            v = pmap.get(node, "builtin")
            if v in ("builtin", "none"):
                return v
            if v not in lib:
                info["missing"].append(v)
                return "builtin"
            info["used"].append(v)
            return v

        def identity(node):
            t = tmap.get(node)
            return not t

        def blank_layer():
            return Image.new("RGBA", size, (0, 0, 0, 0))

        fw, fh = pt.face_size(nodes)

        def put(node, layer):
            nonlocal img
            if node in tmap and node in nodes and layer is not None and getattr(layer, "_builtin", False):
                t = dict(tmap[node], _dx_px=tmap[node].get("dx", 0.0) * fw, _dy_px=tmap[node].get("dy", 0.0) * fh)
                layer = self._xform(layer, nodes[node], t)
            if layer is None:
                return
            img = Image.alpha_composite(img, layer)
            if want_layers:
                layers[node] = layer

        def comp_layer(node, name):
            comp = lib[name]
            geom = pt.node_geom(nodes, fields, node)
            if comp.get("glyphs"):
                info["glyphs"].append((node, comp, geom))
            if not comp.get("draw"):
                return blank_layer()
            return pt.draw_component(size, comp, geom, self.col, self.ch, self.skin)

        def builtin(fn):
            layer = blank_layer()
            fn(layer, ImageDraw.Draw(layer))
            layer._builtin = True
            return layer

        # 腮红（最底层，柔化）
        blush_over = blank_layer()
        for i, side in enumerate(("l", "r")):
            node = f"cheek_{side}"
            mode = part_of(node)
            if mode == "builtin" and spec["blush"] > 0.02:
                g = pt.node_geom(nodes, fields, node)
                self._blush_one(blush_over, g["cx"], g["cy"], spec["blush"], g["a"] / 30.0, g["b"] / 15.0)
            elif mode not in ("builtin", "none"):
                put(node, comp_layer(node, mode))
        if spec["blush"] > 0.02:
            blurred = blush_over.filter(ImageFilter.GaussianBlur(max(2.0, self.cw * 0.9)))
            img = Image.alpha_composite(img, blurred)
            if want_layers:
                layers["blush"] = blurred
        # 额头阴影线
        mode = part_of("forehead")
        if mode == "builtin" and spec["gloom"] > 0.02:
            g = pt.node_geom(nodes, fields, "forehead")
            d0 = nodes["forehead"]
            layer = blank_layer()
            self._gloom(layer, spec["gloom"], g["cx"] - d0["cx"], g["cy"] - d0["cy"], g["a"] / d0["a"])
            put("forehead", layer)
        elif mode not in ("builtin", "none"):
            put("forehead", comp_layer("forehead", mode))
        # 眼睛
        for i, side in enumerate(("eye_l", "eye_r")):
            node = side
            mode = part_of(node)
            e = spec[side]
            if mode not in ("builtin", "none") and lib[mode].get("blink", "builtin") == "builtin" and e["open"] < 0.16 \
                    and e["smile"] < 0.72:
                mode = "builtin"                                  # 眨眼：组件眼睛眨眼时换成内置的闭眼弧线
            if mode == "none":
                continue
            if mode == "builtin":
                if identity(node) and self.drawn is None and self._eye_is_art(e, spec["gaze"]):
                    layer = blank_layer()
                    layer.paste(self.art.convert("RGBA"), (0, 0), self._masks[f"eye{i}"])
                    put(node, layer)
                else:
                    put(node, builtin(lambda L, d, i=i, e=e: self._draw_eye(L, d, i, e, spec["gaze"])))
            else:
                put(node, comp_layer(node, mode))
        # 眉毛
        b = spec["brows"]
        art_brow = self.drawn is None and b["show"] and abs(b["y"]) < 0.04 and abs(b["angle"]) < 0.04
        for i, side in enumerate(("l", "r")):
            node = f"brow_{side}"
            mode = part_of(node)
            if mode == "none" or (mode == "builtin" and not b["show"]):
                continue
            if mode == "builtin":
                if art_brow and identity(node):
                    layer = blank_layer()
                    layer.paste(self.art.convert("RGBA"), (0, 0), self._masks[f"brow{i}"])
                    put(node, layer)
                else:
                    put(node, builtin(lambda L, d, i=i: self._draw_brow(L, d, i, b, spec[("eye_l", "eye_r")[i]])))
            else:
                put(node, comp_layer(node, mode))
        # 嘴
        m = spec["mouth"]
        mode = part_of("mouth")
        if mode == "builtin":
            if m["shape"] == "art" and identity("mouth") and self.drawn is None:
                layer = blank_layer()
                layer.paste(self.art.convert("RGBA"), (0, 0), self._masks["mouth"])
                put("mouth", layer)
            else:
                put("mouth", builtin(lambda L, d: self._draw_mouth(L, d, m, (spec["eye_l"], spec["eye_r"]))))
        elif mode != "none":
            put("mouth", comp_layer("mouth", mode))
        # 汗滴
        mode = part_of("sweat")
        if mode == "builtin" and spec["sweat"] > 0.05:
            put("sweat", builtin(lambda L, d: self._sweat(d, spec["sweat"])))
        elif mode not in ("builtin", "none"):
            put("sweat", comp_layer("sweat", mode))
        # 新增节点（最上层）
        for e in fields.get("extra") or []:
            name = e["part"]
            if name not in lib:
                info["missing"].append(name)
                continue
            info["used"].append(name)
            put(e["id"], comp_layer(e["id"], name))
        return img.convert("RGB"), layers, info

    @staticmethod
    def _xform(layer, node: dict, t: dict):
        """内置五官图层按节点变换：绕节点中心缩放、旋转，再平移（dx/dy 是脸框比例，这里已按节点表换算）。"""
        from PIL import Image

        return layer.transform(layer.size, Image.AFFINE, _affine(node, t), resample=Image.BILINEAR)

    # ------------------------------------------------------------------ 绘制
    def draw(self, spec: dict):
        from PIL import Image, ImageDraw, ImageFilter

        img = self.blank.copy()
        hand = self.drawn is not None
        art_eye = [not hand and self._eye_is_art(spec[s], spec["gaze"]) for s in ("eye_l", "eye_r")]
        b = spec["brows"]
        art_brow = not hand and b["show"] and abs(b["y"]) < 0.04 and abs(b["angle"]) < 0.04
        m = spec["mouth"]
        art_mouth = not hand and m["shape"] == "art"
        for i, ok in enumerate(art_eye):
            if ok:
                img.paste(self.art, (0, 0), self._masks[f"eye{i}"])
        if art_brow:
            for i in range(2):
                img.paste(self.art, (0, 0), self._masks[f"brow{i}"])
        if art_mouth:
            img.paste(self.art, (0, 0), self._masks["mouth"])

        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        if spec["blush"] > 0.02:
            self._blush(over, spec["blush"])
            img = Image.alpha_composite(img.convert("RGBA"), over.filter(ImageFilter.GaussianBlur(max(2.0, self.cw * 0.9))))
            over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        img = img.convert("RGBA")
        d = ImageDraw.Draw(img)
        if spec["gloom"] > 0.02:
            self._gloom(img, spec["gloom"])
        for i, side in enumerate(("eye_l", "eye_r")):
            if not art_eye[i]:
                self._draw_eye(img, d, i, spec[side], spec["gaze"])
        if b["show"] and not art_brow:
            for i in range(2):
                self._draw_brow(img, d, i, b, spec[("eye_l", "eye_r")[i]])
        if not art_mouth:
            self._draw_mouth(img, d, m, (spec["eye_l"], spec["eye_r"]))
        if spec["sweat"] > 0.05:
            self._sweat(d, spec["sweat"])
        return img.convert("RGB")

    @staticmethod
    def _eye_is_art(e, gaze):
        return (e["pupil"] == "art" and abs(e["open"] - 1) < 0.04 and e["smile"] < 0.04 and abs(e["tilt"]) < 0.04
                and abs(e["iris"] - 1) < 0.04 and e["tear"] < 0.04 and abs(gaze[0]) < 0.08 and abs(gaze[1]) < 0.08
                and abs(e["highlight"] - 1) < 0.1)

    def _lw(self, k):
        return max(2, int(round(self.ch * k)))

    def _draw_eye(self, img, d, i, e, gaze):
        """先用手绘差分画；不支持的情况（× 叉叉眼、没有素材）用程序绘制。"""
        df = self.drawn
        if df is not None and df.draw_eye(img, i, e, gaze):
            cal = self.cal["eyes"][i]
            cx, cy = self.p(*cal["center"])
            self._tears(d, cx, cy, cal["a"], cal["b"], cal["inner"], e["tear"])     # 流下来的眼泪仍是程序画
            return
        self._eye(img, d, i, e, gaze)

    def _draw_brow(self, img, d, i, br, e):
        df = self.drawn
        if df is not None and df.draw_brow(img, i, br, e):
            return
        self._brow(d, i, br)

    def _draw_mouth(self, img, d, m, eyes=None):
        df = self.drawn
        if df is not None and df.draw_mouth(img, m, eyes):
            return
        self._mouth(img, d, m)

    def _eye(self, img, d, i, e, gaze):
        from PIL import Image, ImageDraw

        cal = self.cal["eyes"][i]
        cx, cy = self.p(*cal["center"])
        a, b = cal["a"], cal["b"]
        inner = cal["inner"]
        ink = self.col["ink"] + (255,)
        lid = self._lw(0.95)
        arcw = self._lw(1.3)        # 笑眼/闭眼的弧线要更粗，字符化后才清楚
        thin = self._lw(0.42)
        o, sm, tilt = e["open"], e["smile"], e["tilt"]

        if e["pupil"] == "x":
            # > < 形状
            s = -inner
            pts = [(cx - s * a * 0.6, cy - b * 0.55), (cx + s * a * 0.55, cy), (cx - s * a * 0.6, cy + b * 0.55)]
            d.line(pts, fill=ink, width=lid, joint="curve")
            return
        if sm >= 0.72:
            # 笑眼 ^：向上拱的粗弧
            h = b * (0.35 + 0.25 * sm)
            pts = [(cx + u * a * 0.95, cy + b * 0.2 - h * (1 - u * u) + tilt * b * 0.25 * u * inner)
                   for u in [k / 10 - 1 for k in range(21)]]
            d.line(pts, fill=ink, width=arcw, joint="curve")
            self._tears(d, cx, cy, a, b, inner, e["tear"])
            return
        if o < 0.16:
            # 闭眼：向下弯的粗弧 + 睫毛
            pts = [(cx + u * a * 0.95, cy + b * 0.05 + b * 0.22 * (1 - u * u) + tilt * b * 0.25 * u * inner)
                   for u in [k / 10 - 1 for k in range(21)]]
            d.line(pts, fill=ink, width=arcw, joint="curve")
            ox = cx - inner * a * 0.95
            d.line([(ox, cy + b * 0.05), (ox - inner * a * 0.18, cy - b * 0.12)], fill=ink, width=thin)
            self._tears(d, cx, cy, a, b, inner, e["tear"])
            return

        us = [k / 12 - 1 for k in range(25)]

        def top(u):
            arc = cy - b * o * math.sqrt(max(0.0, 1 - u * u)) ** 0.85 + (1 - min(o, 1.0)) * b * 0.25
            if abs(tilt) < 0.02:
                return arc
            # 生气：上眼睑是一条从外眼角向内眼角压低的斜线；难过：内眼角高、外眼角垂下
            s = (1 + (1 if tilt > 0 else -1) * u * inner) / 2
            return max(arc, cy - b * o + b * abs(tilt) * 1.05 * s)

        def bot(u):
            return cy + b * 0.95 * max(0.0, 1 - u * u) ** 0.55 * (1 - sm * 0.95) * min(1.12, max(0.8, o))

        top_pts = [(cx + u * a, top(u)) for u in us]
        bot_pts = [(cx + u * a, max(bot(u), top(u) + 1)) for u in reversed(us)]
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).polygon(top_pts + bot_pts, fill=255)
        layer = Image.new("RGBA", img.size, self.col["sclera"] + (255,))
        ld = ImageDraw.Draw(layer)
        if e["pupil"] != "dot":
            icx, icy, irx, iry = cal["iris"]
            icx, icy = self.p(icx, icy)
            icx += gaze[0] * a * 0.38
            icy += gaze[1] * b * 0.28
            irx, iry = irx * e["iris"] * 1.08, iry * e["iris"] * 1.08
            if e["pupil"] == "heart":
                self._heart(ld, icx, icy + iry * 0.05, irx * 1.05, self.col["heart"])
                ld.ellipse([icx - irx * 0.45, icy - iry * 0.5, icx - irx * 0.1, icy - iry * 0.18], fill=self.col["highlight"])
            else:
                ld.ellipse([icx - irx, icy - iry, icx + irx, icy + iry], fill=self.col["iris_dark"])
                ld.ellipse([icx - irx * 0.72, icy + iry * 0.05, icx + irx * 0.72, icy + iry * 0.9], fill=self.col["iris"])
                ld.ellipse([icx - irx * 0.45, icy + iry * 0.4, icx + irx * 0.45, icy + iry * 0.82], fill=self.col["iris_light"])
                if e["pupil"] == "spiral":
                    for k, r in enumerate((0.85, 0.62, 0.4, 0.2)):
                        ld.ellipse([icx - irx * r, icy - iry * r, icx + irx * r, icy + iry * r],
                                   outline=self.col["iris_dark"] if k % 2 == 0 else self.col["iris_light"], width=thin)
                elif e["pupil"] == "star":
                    self._star(ld, icx, icy, irx * 0.75, self.col["star"])
                    ld.ellipse([icx - irx * 0.15, icy - iry * 0.15, icx + irx * 0.15, icy + iry * 0.15], fill=self.col["highlight"])
                else:
                    ld.ellipse([icx - irx * 0.32, icy - iry * 0.35, icx + irx * 0.32, icy + iry * 0.3], fill=self.col["iris_dark"])
                    hl = e["highlight"]
                    if hl > 0.05:
                        self._star(ld, icx - irx * 0.12, icy - iry * 0.12, irx * 0.42 * min(1.4, hl), self.col["highlight"], 4)
                        if hl > 0.6:
                            r = irx * 0.13 * hl
                            hx, hy = icx + irx * 0.4, icy + iry * 0.42
                            ld.ellipse([hx - r, hy - r, hx + r, hy + r], fill=self.col["highlight"])
        else:
            dx, dy = self.p(*cal["iris"][:2])
            dx += gaze[0] * a * 0.38
            r = a * 0.12
            ld.ellipse([dx - r, dy - r, dx + r, dy + r], fill=self.col["ink"])
        if e["tear"] > 0.05:
            pool = Image.new("RGBA", img.size, (0, 0, 0, 0))
            pd = ImageDraw.Draw(pool)
            ty = cy + b * (0.95 - 0.55 * e["tear"]) * (1 - sm * 0.95)
            pd.rectangle([cx - a, ty, cx + a, cy + b * 1.2], fill=self.col["tear"] + (int(170 * e["tear"]),))
            layer = Image.alpha_composite(layer, pool)
        img.paste(layer, (0, 0), mask)
        d.line(top_pts, fill=ink, width=lid, joint="curve")
        wing = top_pts[0] if inner > 0 else top_pts[-1]
        d.line([wing, (wing[0] - inner * a * 0.16, wing[1] + b * 0.2)], fill=ink, width=thin)
        lo = [pt for pt in bot_pts if (pt[0] - cx) * inner > -a * 0.35]
        if len(lo) > 1:
            d.line(lo, fill=ink, width=thin, joint="curve")
        self._tears(d, cx, cy, a, b, inner, e["tear"])

    def _tears(self, d, cx, cy, a, b, inner, tear):
        if tear < 0.45:
            return
        tc = self.col["tear"] + (255,)
        x = cx - inner * a * 0.55
        y0 = cy + b * 0.85
        length = b * (0.4 + 1.1 * (tear - 0.45))
        w = self._lw(0.42)
        d.line([(x, y0), (x - inner * 2, y0 + length * 0.5), (x, y0 + length)], fill=tc, width=w, joint="curve")
        r = w * 0.9
        d.ellipse([x - r, y0 + length - r * 0.5, x + r, y0 + length + r * 1.4], fill=tc)

    def _brow(self, d, i, br):
        cal = self.cal["brows"][i]
        ix, iy = self.p(*cal["inner"])
        ox, oy = self.p(*cal["outer"])
        mx, my = (ix + ox) / 2, (iy + oy) / 2 - br["y"] * self.ch * 1.3
        length = math.hypot(ix - ox, iy - oy) * 0.95
        inner_dir = 1 if ix > ox else -1
        ang = math.radians(br["angle"] * 34)        # 正：内端向下（生气）
        hx, hy = math.cos(ang) * length / 2, math.sin(ang) * length / 2
        p_in = (mx + inner_dir * hx, my + hy)
        p_out = (mx - inner_dir * hx, my - hy * 0.4 + length * 0.08)
        mid = ((p_in[0] + p_out[0]) / 2, (p_in[1] + p_out[1]) / 2 - length * 0.08)
        d.line([p_out, mid, p_in], fill=self.col["ink"] + (255,), width=self._lw(0.7), joint="curve")

    def _mouth(self, img, d, m):
        cal = self.cal["mouth"]
        cx, cy = self.p(*cal["center"])
        w = max(30.0, self.cw * 6.5) * m["width"]
        H = max(22.0, self.ch * 2.3)
        ink = self.col["ink"] + (255,)
        lw = self._lw(0.72)
        shape, op, cv = m["shape"], m["open"], m["curve"]
        xs = [k / 12 - 1 for k in range(25)]
        if shape in ("normal", "teeth", "tongue") and op < 0.12 and shape == "normal" or shape == "line":
            pts = [(cx + u * w / 2, cy + cv * H * 0.32 * (1 - u * u)) for u in xs]
            d.line(pts, fill=ink, width=lw, joint="curve")
            return
        if shape == "wavy":
            pts = [(cx + u * w * 0.6, cy + math.sin((u + 1) * math.pi * 2) * H * 0.12 + op * H * 0.2) for u in xs]
            d.line(pts, fill=ink, width=lw, joint="curve")
            return
        if shape == "cat":
            for s in (-1, 1):
                pts = [(cx + s * w * 0.22 + u * w * 0.22, cy + H * 0.2 * (1 - u * u)) for u in xs]
                d.line(pts, fill=ink, width=lw, joint="curve")
            return
        if shape == "pout":
            r = w * 0.18
            d.ellipse([cx - r, cy - r * 1.1, cx + r, cy + r * 1.1], fill=self.col["mouth"], outline=ink, width=lw)
            return
        if shape == "o":
            rx = w * (0.14 + 0.16 * op)
            ry = rx * 1.25
            d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=self.col["mouth"], outline=ink, width=lw)
            return
        if shape == "triangle":
            hh = H * (0.5 + 0.6 * op)
            pts = [(cx - w * 0.32, cy + hh * 0.55), (cx + w * 0.32, cy + hh * 0.55), (cx, cy - hh * 0.45)]
            d.polygon(pts, fill=self.col["mouth"], outline=ink)
            d.line(pts + [pts[0]], fill=ink, width=lw, joint="curve")
            return
        # 张开的嘴（normal/teeth/tongue）：上缘随 curve 弯，下缘按张开程度
        depth = H * (0.35 + 1.0 * op)
        top = [(cx + u * w / 2, cy + cv * H * 0.18 * (1 - u * u) - (H * 0.12 if cv > 0 else 0)) for u in xs]
        if cv >= 0:
            bot = [(cx + u * w / 2, t[1] + depth * (1 - u * u) ** 0.7 * (0.6 + 0.4 * cv)) for u, t in zip(xs, top)]
        else:
            top = [(cx + u * w / 2, cy - depth * 0.5 * (1 - u * u) ** 0.8 * (0.5 - 0.5 * cv)) for u in xs]
            bot = [(cx + u * w / 2, cy + depth * 0.25 * (1 - u * u)) for u in xs]
        poly = top + list(reversed(bot))
        d.polygon(poly, fill=self.col["mouth"])
        if shape == "teeth":
            band = [(x, y + depth * 0.32) for x, y in top]
            d.polygon(top + list(reversed(band)), fill=self.col["teeth"])
        if shape == "tongue" or (op > 0.45 and shape == "normal"):
            tw = w * (0.32 if shape == "normal" else 0.4)
            ty = max(p[1] for p in bot)
            d.ellipse([cx - tw, ty - depth * 0.45, cx + tw, ty + (depth * 0.35 if shape == "tongue" else depth * 0.05)],
                      fill=self.col["tongue"])
            d.line(bot, fill=ink, width=lw, joint="curve")
        d.line(poly + [poly[0]], fill=ink, width=lw, joint="curve")

    def _blush(self, over, k):
        for c in self.cal["cheeks"]:
            x, y = self.p(*c)
            self._blush_one(over, x, y, k)

    def _blush_one(self, over, x, y, k, sx: float = 1.0, sy: float = 1.0):
        from PIL import ImageDraw

        d = ImageDraw.Draw(over)
        rx, ry = 30 * sx, 15 * sy
        d.ellipse([x - rx, y - ry, x + rx, y + ry], fill=self.col["blush"] + (int(205 * k),))
        if k > 0.6:
            lw = self._lw(0.22)
            for j in (-1, 0, 1):
                xx = x + j * rx * 0.45
                d.line([(xx + 5 * sx, y - ry * 0.6), (xx - 5 * sx, y + ry * 0.6)],
                       fill=self.col["blush_line"] + (int(255 * (k - 0.4)),), width=lw)

    def _sweat(self, d, k):
        x, y = self.p(*self.cal["sweat_anchor"])
        s = 10 + 10 * k
        pts = [(x, y - s * 1.2), (x + s * 0.7, y + s * 0.3), (x, y + s * 0.9), (x - s * 0.7, y + s * 0.3)]
        d.polygon(pts, fill=self.col["sweat"] + (255,))
        d.line(pts + [pts[0]], fill=self.col["ink"] + (255,), width=self._lw(0.25), joint="curve")

    def _gloom(self, img, k, dx: float = 0.0, dy: float = 0.0, s: float = 1.0):
        from PIL import Image, ImageDraw

        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(over)
        e0, e1 = self.cal["eyes"]
        x0, _ = self.p(e0["center"][0] - e0["a"], 0)
        x1, _ = self.p(e1["center"][0] + e1["a"], 0)
        _, y0 = self.p(0, min(e0["center"][1], e1["center"][1]) - 52)
        mid = (x0 + x1) / 2
        x0, x1 = mid + (x0 - mid) * s + dx, mid + (x1 - mid) * s + dx
        y0 += dy
        n = 7
        for j in range(n):
            x = x0 + (x1 - x0) * (j + 0.5) / n
            d.line([(x, y0), (x, y0 + (26 + 40 * k) * s)], fill=self.col["gloom"] + (int(200 * k),), width=self._lw(0.3))
        img.alpha_composite(over)

    @staticmethod
    def _star(d, x, y, r, color, points=5):
        pts = []
        for k in range(points * 2):
            ang = math.pi / points * k - math.pi / 2
            rr = r if k % 2 == 0 else r * (0.32 if points == 4 else 0.45)
            pts.append((x + math.cos(ang) * rr, y + math.sin(ang) * rr))
        d.polygon(pts, fill=color)

    @staticmethod
    def _heart(d, x, y, r, color):
        pts = []
        for k in range(48):
            t = 2 * math.pi * k / 48
            hx = 16 * math.sin(t) ** 3
            hy = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
            pts.append((x + hx * r / 17, y - hy * r / 17))
        d.polygon(pts, fill=color)


def _affine(node: dict, t: dict) -> tuple:
    """PIL AFFINE 参数（输出像素 → 输入像素）：p' = c + d + R·S·(p − c)。"""
    from . import parts as pt  # noqa: F401  （保持与 parts 的单位约定一致：dx/dy 已换算成像素）

    cx, cy = node["cx"], node["cy"]
    dx, dy = t.get("_dx_px", 0.0), t.get("_dy_px", 0.0)
    sx, sy = t.get("sx", 1.0), t.get("sy", 1.0)
    th = math.radians(t.get("rot", 0.0))
    c, s = math.cos(th), math.sin(th)
    m00, m01 = c / sx, s / sx
    m10, m11 = -s / sy, c / sy
    ox, oy = cx + dx, cy + dy
    return (m00, m01, cx - m00 * ox - m01 * oy, m10, m11, cy - m10 * ox - m11 * oy)
