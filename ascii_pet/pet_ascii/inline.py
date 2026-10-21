"""对话内联表情：让桌宠的主模型在回复开头顺带输出一个表情标签，本地骨架直接演出（不再额外请求 API）。

  <face happy level=0.8 blush=0.6 look=left act=bounce fx=sparkle then=smile>
  <face new=憋笑 from=shy smile=0.6 open=0.7 mouth=cat curve=0.6 blush=0.9 look=left>   ← 当场自创新表情
  ……正文……<face idle=sleepy after=30 fx=zzz>   ← 收尾标签：说完 1 分钟后再过 30 秒，换成犯困的样子进入挂机

- 标签在流式输出中被 TagFilter 实时剥离：聊天窗口、气泡、语音、会话记录与记忆都看不到它
- 第一个裸词（或 preset=）是表情预设（内置或自创）；其余字段都可省略
- new=名字：用 from=最接近的预设 + 平铺参数 定义一个新表情，立即使用并存入自创表情库，以后直接用名字调用
- idle=表情 after=秒：模型决定说完之后是否、何时切换表情进入挂机；idle=keep 表示保持当前表情。
  说完后至少等 1 分钟（config.idle_min_delay_s）才可能挂机，after（0~240）是在那之后再等的秒数
"""
from __future__ import annotations

import re

from .face_rig import MOUTHS, PRESETS, PUPILS, is_preset

LOOKS = {"left": [-0.8, 0.0], "right": [0.8, 0.0], "up": [0.0, -0.8], "down": [0.0, 0.8], "away": [0.8, 0.4],
         "center": [0.0, 0.0], "upleft": [-0.7, -0.6], "upright": [0.7, -0.6], "downleft": [-0.7, 0.6],
         "downright": [0.7, 0.6]}
ACTS = ("nod", "shake", "jump", "bounce", "sway", "lean", "shiver", "none")
FX = {
    "hearts": {"effect": "particles", "chars": "♥♡", "color": "#ff6f9f", "direction": "up", "count": 16, "duration_ms": 1700},
    "sparkle": {"effect": "sparkle", "chars": "*+✦", "color": "#ffe27a", "duration_ms": 1600},
    "question": {"effect": "symbols", "chars": "?", "color": "#9fe8ff", "duration_ms": 1800},
    "exclaim": {"effect": "symbols", "chars": "!", "color": "#ffe066", "duration_ms": 1300},
    "anger": {"effect": "symbols", "chars": "#", "color": "#ff5a5a", "duration_ms": 1500},
    "tears": {"effect": "particles", "chars": "'.", "color": "#8fc8ff", "direction": "down", "count": 14, "duration_ms": 1600},
    "notes": {"effect": "symbols", "chars": "♪♫", "color": "#c8f0ff", "duration_ms": 2200},
    "zzz": {"effect": "symbols", "chars": "z", "color": "#c8d0ff", "duration_ms": 2600},
    "glitch": {"effect": "glitch", "intensity": 0.6, "duration_ms": 900},
    "flowers": {"effect": "particles", "chars": "*o", "color": "#ffb0d0", "direction": "up", "count": 14, "duration_ms": 1700},
    "flash": {"effect": "flash", "duration_ms": 500},
}
FACE_FX = {"sweat": {"sweat": 0.8}, "gloom": {"gloom": 0.7}}

# 自创表情时可用的平铺参数：键 → (所属部位, 字段, 下限, 上限)
EYE_KEYS = {"open": (0.0, 1.3), "smile": (0.0, 1.0), "tilt": (-1.0, 1.0), "iris": (0.5, 1.3), "tear": (0.0, 1.0),
            "highlight": (0.0, 1.5), "droop": (-1.0, 1.0), "lower": (0.0, 1.0)}
FLAT_KEYS = {"brow": ("brows", "angle", -1.0, 1.0), "browy": ("brows", "y", -1.0, 1.0),
             "mopen": ("mouth", "open", 0.0, 1.0), "mwidth": ("mouth", "width", 0.4, 1.6),
             "curve": ("mouth", "curve", -1.0, 1.0), "mskew": ("mouth", "skew", -1.0, 1.0),
             "sweat": (None, "sweat", 0.0, 1.0),
             "gloom": (None, "gloom", 0.0, 1.0)}

INSTRUCTION = (
    "【表情系统】你有一张会随对话实时变化的字符画脸。每次回复必须以一个表情标签开头（程序会自动移除，用户看不到），"
    "紧接着再写正文，标签之后不要提到它：\n"
    "<face 预设 level=强度0.3~1 blush=脸红0~1 look=视线 act=动作 fx=特效 then=随后的预设 idle=说完后的表情 after=秒>\n"
    "内置预设：" + " ".join(PRESETS) + "\n"
    "look：left right up down away；act：" + " ".join(a for a in ACTS if a != "none") + "；fx："
    + " ".join(list(FX) + list(FACE_FX)) + "\n"
    "预设、idle、after 必填，其余可省略；表情要贴合你这一句的情绪与神态。"
    "例：<face happy level=0.8 blush=0.6 act=bounce fx=sparkle idle=smile after=20>\n"
    "【自创表情】如果现有表情都不能准确表达你此刻的情绪，可以当场创造一个新表情并起个简短的中文名（2~6字），它会被永久记住：\n"
    "<face new=名字 from=最接近的预设 参数...>  参数：open睁眼0~1.3 smile笑眼0~1 tilt眼睑斜度-1~1(正=凶,负=委屈) "
    "droop外眼角-1~1(正=往下耷拉:困/委屈/无奈，负=往上挑:凶/傲) lower下眼睑上抬0~1(眯眼:坏笑/怀疑/憋笑) "
    "iris瞳孔0.5~1.3 pupil=" + "|".join(PUPILS[1:]) + " tear泪光0~1 brow眉角-1~1(负=担心,正=生气) browy眉高-1~1 "
    "mouth=" + "|".join(MOUTHS[1:]) + " mopen张嘴0~1 mwidth嘴宽0.4~1.6 curve嘴角-1~1 mskew歪嘴-1~1(正=右嘴角高) "
    "blush sweat gloom 0~1 look=视线；"
    "加 l. 或 r. 前缀只改一只眼（如 r.smile=1 眨右眼）。\n"
    "以后直接用名字调用自创表情（<face 名字 ...>）。只在确有新情绪时才创造，不要给已有的表情换个名字重复创造。"
    "新表情要和 from 的预设一眼就能区分。你的脸是画师手绘的、连续可调的：睁眼程度、笑眼（smile 0.4 左右是眯眼笑，≥0.72 是 ^）、"
    "外眼角 droop、下眼睑 lower、左右眼不一样都能细细地调；嘴的张开 / 嘴宽 / 嘴角 / 歪嘴也连续可调，张开的嘴很显眼。"
    "脸红、汗在字符画上几乎看不出来，只是点缀。\n"
    "【说完之后】你说完话后，至少会保持当前的表情 1 分钟（用户可能还在看、在打字回你）。"
    "idle 和 after 决定这 1 分钟过后，要不要换成别的表情进入挂机（安静待着等用户），以及在那之后再等几秒（after=0~240）；"
    "不想换就写 idle=keep。换不换、换成什么（内置或自创表情）、多久之后换，按你此刻的心情和这段对话的余韵自己决定，"
    "例如道别后 idle=sleepy after=30，被逗笑后 idle=smile after=0，聊到兴头上 idle=calm after=180，还在生气 idle=keep。"
    "正文写完后如果改了主意，可以在最末尾再加一个收尾标签覆盖：<face idle=表情 after=秒 look=视线 act=动作 fx=特效>\n"
    "【特别演出】你在创作实验里做过一些脱离角色骨架的全画布演出；只在真正特别的时刻，在标签里加 scene=演出名 播放（名字见备忘）。"
)

MUSIC_INSTRUCTION = (
    "【放歌】你可以用主人的 Spotify 放歌——不是必须的，只在你真的想放、或者主人请你放的时候。"
    "做法：在开头或末尾的表情标签里加 music=\"歌名 歌手\"，例如 <face happy idle=smile after=20 music=\"晴天 周杰伦\">；"
    "也可以先说话，想好了再在正文最后单独写一个 <face music=\"晴天 周杰伦\">。"
    "要写一首真实存在的、具体的歌（歌名 + 歌手）。只在嘴上说“放了”“这首怎么样”不会真的放出来，必须写进标签。"
    "主人正在听歌时会排进播放队列，不打断；放成了没有、放的是哪首，下一轮会告诉你。"
)

IDLE_KEEP = ("keep", "hold", "none", "same")
IDLE_MAX_S = 240.0       # after 的上限：1 分钟之后最多再等 4 分钟
IDLE_DEFAULT_S = 30.0    # 没写 after 时：1 分钟之后再等 30 秒

TAG_START = "<face"
_ATTR_RE = re.compile(r"([a-zA-Z_][a-zA-Z_.]*)\s*=\s*(\"[^\">]*\"|“[^”>]*”|'[^'>]*'|[^\s>]+)")   # 值可以带引号（music="歌名 歌手"）


class TagFilter:
    """流式剥离 <face ...> 标签。feed() 返回可以立即显示的文字；标签收集在 tags 里。"""

    MAX_TAG = 400

    def __init__(self):
        self.buf = ""
        self.in_tag = False
        self.tags: list[str] = []
        self._strip_ws = False
        self._ws = ""     # 暂扣的尾部空白：若后面紧跟标签就丢掉（收尾标签前不留空行）

    def _out(self, out: list, text: str) -> None:
        body = text.rstrip(" \t\r\n")
        if body:
            out.append(self._ws + body)
            self._ws = text[len(body):]
        else:
            self._ws += text

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            if self.in_tag:
                end = self.buf.find(">")
                if end < 0:
                    if len(self.buf) > self.MAX_TAG:   # 不像标签，原样放行
                        self._out(out, self.buf)
                        self.buf, self.in_tag = "", False
                    break
                self.tags.append(self.buf[:end + 1])
                self.buf = self.buf[end + 1:]
                self.in_tag = False
                self._strip_ws = True
                continue
            if self._strip_ws:
                stripped = self.buf.lstrip(" \t\r\n")
                if not stripped:
                    self.buf = ""
                    break
                self.buf = stripped
                self._strip_ws = False
            i = self.buf.find(TAG_START)
            if i >= 0:
                self._out(out, self.buf[:i])
                self._ws = ""
                self.buf = self.buf[i:]
                self.in_tag = True
                continue
            # 末尾可能是标签开头的一部分（如 "<fa"），先扣住
            j = self.buf.rfind("<")
            if j >= 0 and TAG_START.startswith(self.buf[j:]):
                self._out(out, self.buf[:j])
                self.buf = self.buf[j:]
            else:
                self._out(out, self.buf)
                self.buf = ""
            break
        return "".join(out)

    def flush(self) -> str:
        rest = self._ws + ("" if self.in_tag else self.buf)
        self.buf, self.in_tag, self._ws = "", False, ""
        return rest

    def pop_tags(self) -> list:
        tags, self.tags = self.tags, []
        return tags


def _num(v, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return None


def build_new_spec(attrs: dict) -> dict:
    """把自创表情的平铺参数整理成表情规格（from 预设为起点）。"""
    spec: dict = {}
    base = attrs.get("from", "")
    if is_preset(base):
        spec["preset"] = base
    eyes, eye_l, eye_r = {}, {}, {}
    for key, val in attrs.items():
        side = None
        k = key
        if key[:2] in ("l.", "r."):
            side, k = key[0], key[2:]
        if k in EYE_KEYS:
            v = _num(val, *EYE_KEYS[k])
            if v is not None:
                (eye_l if side == "l" else eye_r if side == "r" else eyes)[k] = v
        elif k == "pupil" and val in PUPILS:
            (eye_l if side == "l" else eye_r if side == "r" else eyes)["pupil"] = val
        elif side is None and k in FLAT_KEYS:
            part, field, lo, hi = FLAT_KEYS[k]
            v = _num(val, lo, hi)
            if v is None:
                continue
            if part:
                spec.setdefault(part, {})[field] = v
            else:
                spec[field] = v
        elif side is None and k == "mouth" and val in MOUTHS:
            spec.setdefault("mouth", {})["shape"] = val
        elif side is None and k == "blush":
            v = _num(val, 0.0, 1.0)
            if v is not None:
                spec["blush"] = v
        elif side is None and k == "look" and val in LOOKS:
            spec["gaze"] = LOOKS[val]
    if eyes:
        spec["eyes"] = eyes
    if eye_l:
        spec["eye_l"] = eye_l
    if eye_r:
        spec["eye_r"] = eye_r
    return spec


def parse_tag(tag: str) -> dict | None:
    body = tag.strip()
    if not body.startswith(TAG_START):
        return None
    body = body[len(TAG_START):].rstrip(">").strip()
    attrs = {k.lower(): v.strip("\"'“”，,") for k, v in _ATTR_RE.findall(body)}
    bare = [w for w in re.split(r"\s+", _ATTR_RE.sub(" ", body)) if w]
    out: dict = {}
    idle = attrs.get("idle", "")
    if idle:
        idle_key = idle.lower() if idle.lower() in PRESETS or idle.lower() in IDLE_KEEP else idle
        if idle_key in IDLE_KEEP:
            out["idle"] = "keep"
        elif is_preset(idle_key):
            out["idle"] = idle_key
        if "idle" in out:
            v = _num(attrs.get("after"), 0.0, IDLE_MAX_S)
            out["after"] = IDLE_DEFAULT_S if v is None else v
    if attrs.get("music") and "idle" not in out and not attrs.get("new") and not attrs.get("preset") and not bare:
        return {"music": attrs["music"].strip()[:80], "music_only": True}      # 只放歌的标签：不改变表情
    if "idle" in out and not attrs.get("new") and not attrs.get("preset") and not bare:
        out["idle_only"] = True                     # 收尾标签：只决定说完之后的表情，不改变当前表情
    elif attrs.get("new"):
        out["new"] = {"name": attrs["new"][:12], "spec": build_new_spec(attrs)}
        out["preset"] = attrs["new"][:12]          # 由运行时在入库后替换为实际名字
    else:
        preset = attrs.get("preset") or (bare[0] if bare else "")
        key = preset.lower() if preset.lower() in PRESETS else preset
        out["preset"] = key if is_preset(key) else "neutral"

    for k, lo, hi in (("level", 0.2, 1.0), ("blush", 0.0, 1.0)):
        v = _num(attrs.get(k), lo, hi)
        if v is not None:
            out[k] = v
    if out.get("idle_only"):
        out["preset"] = out["idle"]
    look = attrs.get("look", "").lower()
    if look in LOOKS and "new" not in out:
        out["look"] = look
    act = attrs.get("act", "").lower()
    if act in ACTS and act != "none":
        out["act"] = act
    fxs = [f for f in attrs.get("fx", "").lower().split("+") if f in FX or f in FACE_FX]
    if fxs:
        out["fx"] = fxs
    then = attrs.get("then", "")
    then = then.lower() if then.lower() in PRESETS else then
    if is_preset(then):
        out["then"] = then
    music = attrs.get("music", "").strip()
    if music:                                        # 想给主人放的歌（v1.3，Spotify）
        out["music"] = music[:80]
    scene = attrs.get("scene", "")
    if scene:
        from . import creative

        if scene in creative.SCENES:                 # 她创作过的自由演出（不存在的名字直接忽略）
            out["scene"] = scene
    return out


def _act_ops(act: str) -> list:
    if act == "nod":
        return [{"type": "parameter_animation", "duration_ms": 180, "params": {"body_dy": 1, "ease": "out"}},
                {"type": "parameter_animation", "delay_ms": 220, "duration_ms": 220, "params": {"body_dy": 0}}]
    if act == "shake":
        return [{"type": "effect", "effect": "shake", "intensity": 0.35, "duration_ms": 500}]
    if act == "jump":
        return [{"type": "parameter_animation", "duration_ms": 500, "params": {"jump": 2}}]
    if act == "bounce":
        return [{"type": "parameter_animation", "duration_ms": 420, "params": {"jump": 2}},
                {"type": "parameter_animation", "delay_ms": 460, "duration_ms": 420, "params": {"jump": 2}}]
    if act == "sway":
        return [{"type": "parameter_animation", "duration_ms": 400, "params": {"sway": 1.6}},
                {"type": "parameter_animation", "delay_ms": 2400, "duration_ms": 600, "params": {"sway": 0}}]
    if act == "lean":
        return [{"type": "parameter_animation", "duration_ms": 300, "params": {"body_dx": -2}},
                {"type": "parameter_animation", "delay_ms": 1500, "duration_ms": 400, "params": {"body_dx": 0}}]
    if act == "shiver":
        return [{"type": "effect", "effect": "shake", "intensity": 0.15, "duration_ms": 1400}]
    return []


def tag_to_animation(tag: dict, source_text: str = "") -> dict:
    """把解析后的标签变成协议 JSON（face 关键帧 + 动作 + 特效）。自创表情须先入库（preset 已是实际名字）。"""
    face = dict(tag["inline_spec"]) if tag.get("inline_spec") else {"preset": tag["preset"]}
    for k in ("level", "blush"):
        if k in tag:
            face[k] = tag[k]
    if "look" in tag:
        face["gaze"] = LOOKS[tag["look"]]
    for f in tag.get("fx", []):
        face.update(FACE_FX.get(f, {}))
    frames = [{**face, "ms": 1800 if "then" in tag else 600}]
    if "then" in tag:
        frames.append({"preset": tag["then"], **({"blush": tag["blush"]} if "blush" in tag else {}), "ms": 600})
    ops = [{"type": "face", "frames": frames, "hold": "last", "blend_ms": 200}]
    ops += _act_ops(tag.get("act", ""))
    for f in tag.get("fx", []):
        if f in FX:
            ops.append({"type": "effect", **FX[f]})
    return {"mode": 2, "intent": "对话表情", "expression": tag["preset"], "priority": 6, "ops": ops,
            "end_state": {"face": "hold"}}


def idle_animation(plan: dict) -> dict:
    """说完之后进入挂机：慢慢过渡到模型选定的表情（收尾标签里的 look/act/fx 在切换时一起演出）。"""
    face = {"preset": plan["idle"]}
    for k in ("level", "blush"):
        if k in plan:
            face[k] = plan[k]
    if "look" in plan:
        face["gaze"] = LOOKS[plan["look"]]
    for f in plan.get("fx", []):
        face.update(FACE_FX.get(f, {}))
    ops = [{"type": "face", "frames": [{**face, "ms": 900}], "hold": "last", "blend_ms": 700}]
    ops += _act_ops(plan.get("act", ""))
    for f in plan.get("fx", []):
        if f in FX:
            ops.append({"type": "effect", **FX[f]})
    return {"mode": 1, "intent": "说完之后进入挂机", "expression": plan["idle"], "priority": 4, "ops": ops,
            "end_state": {"face": "hold"}}


def custom_memo(names: list) -> str:
    """附在本轮用户消息末尾的自创表情备忘（不进系统提示，不影响前缀缓存）。"""
    if not names:
        return ""
    return "\n\n〔表情备忘：你自创过的表情有 " + "、".join(names) + "，可直接用 <face 名字>〕"


# 真实对话的历史里，她以前的回复都是剥掉标签后的文字，模型会照着历史的样子省略标签（实测 8 次只写了 1 次）。
# 在本轮用户消息末尾提醒一句就能恢复（实测 8/8）。动态内容放在最后，不影响前缀缓存。
# 注意：不要把猜出来的标签补进历史回复里——实测模型会照抄那种简化标签，表情变得单调。
TURN_REMINDER = "\n\n〔回复照常以表情标签开头：<face 表情 … idle=… after=…>〕"



# 回复里没有标签时的本地兜底：按关键词猜一个预设（不调用 API）
_KEYWORDS = [
    ("laugh", ("哈哈", "笑死", "噗", "233")), ("love", ("喜欢你", "爱你", "♥", "心动", "亲亲")),
    ("shy", ("害羞", "脸红", "不好意思", "羞")), ("cry", ("哭", "呜呜", "泪")), ("sad", ("难过", "伤心", "委屈", "失落", "抱歉")),
    ("angry", ("生气", "哼", "可恶", "气死")), ("surprised", ("诶？", "欸", "哇", "惊", "什么？", "真的吗")),
    ("confused", ("？？", "疑惑", "不懂", "奇怪")), ("sleepy", ("困", "哈欠", "睡")), ("thinking", ("想想", "嗯…", "让我想", "思考")),
    ("happy", ("开心", "太好了", "耶", "好耶", "高兴", "嘿嘿")), ("smile", ("～", "~", "呀", "啦", "呢")),
]


def guess_preset(text: str) -> str:
    for preset, words in _KEYWORDS:
        if any(w in text for w in words):
            return preset
    return "smile"
