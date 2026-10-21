"""Mock 生成器：不需要 API Key，按意图关键词用程序拼出符合协议的动画 JSON。

它的作用是离线测试渲染、合成、播放与调度链路——输出与真实模型同一格式，
五官由若干画法随机组合并带有随机扰动，每次结果都略有不同。
"""
from __future__ import annotations

import json
import random
import time

EMOTIONS = {
    "happy": ("高兴", "开心", "笑", "打招呼", "你好", "欢迎", "喜欢", "棒", "哈哈", "happy", "smile", "hello", "嘿"),
    "love": ("爱", "心动", "♥", "亲", "抱", "love"),
    "shy": ("害羞", "脸红", "不好意思", "shy"),
    "sad": ("难过", "伤心", "哭", "委屈", "sad", "失落"),
    "surprised": ("惊讶", "吓", "啊？", "震惊", "surprise", "哇"),
    "angry": ("生气", "愤怒", "哼", "气", "angry"),
    "confused": ("困惑", "疑惑", "不懂", "为什么", "？", "?", "confus"),
    "sleepy": ("困", "睡", "累", "哈欠", "sleep", "tired"),
    "think": ("想", "思考", "嗯…", "让我想", "think"),
}
MODE3_WORDS = ("乱码", "解体", "崩", "变成", "爆炸", "碎", "故障", "glitch", "全屏", "消失", "融化")
MODE1_WORDS = ("眨眼", "看左", "看右", "往左", "往右", "跳一下", "点头", "抬头", "低头", "晃", "动一动")

PALETTE = {"a": "#16205a", "b": "#3d7bff", "c": "#ffffff", "d": "#ff5d8f", "e": "#ff9fbf", "f": "#24306e",
           "g": "#ffd166", "h": "#7ee8ff"}


def classify(intent: str) -> str:
    low = intent.lower()
    best, score = "neutral", 0
    for emo, words in EMOTIONS.items():
        s = sum(1 for w in words if w in low)
        if s > score:
            best, score = emo, s
    return best


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.rows = [["`"] * w for _ in range(h)]
        self.cols = [["."] * w for _ in range(h)]

    def put(self, x, y, text, color):
        """color 可以是与 text 等长的颜色键串，也可以是单个颜色键。"""
        for i, ch in enumerate(text):
            xx = x + i
            if 0 <= xx < self.w and 0 <= y < self.h and ch != "`":
                self.rows[y][xx] = ch
                self.cols[y][xx] = color[i] if len(color) == len(text) else color[0]

    def frame(self, ms):
        return {"rows": ["".join(r) for r in self.rows], "colors": ["".join(c) for c in self.cols], "ms": ms}


def _eye(style: str, ew: int, eh: int, rng) -> list[tuple[str, str]]:
    """返回若干行 (字符, 颜色键串)。"""
    inner = max(1, ew - 2)
    if style == "open":
        top = "." + "#" * inner + "."
        mid = "#" + "@" * max(0, inner - 1) + "o" + "#"
        mid = mid[:ew]
        bot = "'" + "#" * inner + "'"
        rows = [(top, "a" * ew), (mid, "a" + "b" * (ew - 3) + "ca"), (bot, "a" * ew)]
        return rows[:max(2, eh)]
    if style == "happy":
        return [("/" + "^" * inner + "\\", "a" * ew)]
    if style == "wide":
        return [("." + "-" * inner + ".", "a" * ew), ("(" + "O".center(inner) + ")", "a" + "b" * inner + "a"),
                ("'" + "-" * inner + "'", "a" * ew)]
    if style == "sad":
        return [("." + "_" * inner + ".", "a" * ew), ("\\" + "o".center(inner, "#") + "/", "a" + "b" * inner + "a")]
    if style == "angry":
        return [("\\" + "=" * inner + "/" if rng.random() < 0.5 else "#" * ew, "a" * ew),
                ("#" + "@" * inner + "#", "a" + "b" * inner + "a")]
    if style == "closed":
        return [("-" * ew, "a" * ew)]
    if style == "heart":
        return [("♥".center(ew), "d" * ew)]
    if style == "star":
        return [("✦".center(ew), "g" * ew)]
    if style == "spiral":
        return [("@".center(ew, "~"), "b" * ew)]
    return [("#" * ew, "a" * ew), ("#" + "o".center(inner, "@") + "#", "a" + "b" * inner + "a")]


MOUTHS = {
    "smile": ("\\__/", "dddd"), "grin": ("\\___/", "ddddd"), "open": ("(O)", "ddd"), "small": ("o", "d"),
    "frown": ("/^^\\", "dddd"), "wavy": ("~~~", "ddd"), "cat": ("w", "d"), "flat": ("--", "dd"),
    "pout": ("3", "d"), "teeth": ("[==]", "dcdd"),
}

STYLE_BY_EMO = {
    "neutral": [("open", "small"), ("open", "flat")],
    "happy": [("happy", "grin"), ("open", "smile"), ("happy", "cat")],
    "love": [("heart", "smile"), ("happy", "cat")],
    "shy": [("happy", "wavy"), ("closed", "small")],
    "sad": [("sad", "frown"), ("sad", "wavy")],
    "surprised": [("wide", "open"), ("wide", "small")],
    "angry": [("angry", "teeth"), ("angry", "frown")],
    "confused": [("spiral", "wavy"), ("open", "wavy")],
    "sleepy": [("closed", "small"), ("closed", "flat")],
    "think": [("open", "flat"), ("star", "small")],
}


def compose_face(w: int, h: int, eye_style: str, mouth: str, rng, blush=False, brows=None, eye_shift=0) -> tuple[dict, list]:
    c = Canvas(w, h)
    ew = max(3, min(7, w // 4))
    eh = 3 if h >= 7 else 2
    ey = max(1, int(h * 0.3))
    lx = max(0, int(w * 0.27) - ew // 2 + eye_shift)
    rx = min(w - ew, int(w * 0.73) - ew // 2 + eye_shift)
    rows = _eye(eye_style, ew, eh, rng)
    for dy, (txt, col) in enumerate(rows):
        c.put(lx, ey + dy, txt, col[0] * len(txt) if len(col) != len(txt) else col)
        c.put(rx, ey + dy, txt, col[0] * len(txt) if len(col) != len(txt) else col)
    if brows and ey >= 1:
        left, right = brows
        c.put(lx + 1, ey - 1, left * max(1, ew - 2), "f" * (ew - 2))
        c.put(rx + 1, ey - 1, right * max(1, ew - 2), "f" * (ew - 2))
    mt, mc = MOUTHS[mouth]
    my = min(h - 1, ey + len(rows) + max(1, (h - ey - len(rows)) // 2))
    c.put(w // 2 - len(mt) // 2, my, mt, mc)
    if blush:
        c.put(max(0, lx - 1), min(h - 1, ey + len(rows)), "///", "eee")
        c.put(min(w - 3, rx + ew - 2), min(h - 1, ey + len(rows)), "///", "eee")
    eyes = [[lx, ey, ew, len(rows)], [rx, ey, ew, len(rows)]]
    return c, eyes


RIG_SEQUENCES = {
    "happy": [("surprised", 160), ("happy", 900), ("smile", 600)],
    "love": [("love", 1200), ("smile", 500)],
    "shy": [("embarrassed", 500), ("shy", 900)],
    "sad": [("sad", 900), ("teary", 700)],
    "surprised": [("surprised", 500), ("shocked", 400), ("surprised", 400)],
    "angry": [("angry", 700), ("pout", 800)],
    "confused": [("confused", 700), ("thinking", 600)],
    "sleepy": [("sleepy", 900), ("closed", 600), ("sleepy", 500)],
    "think": [("thinking", 900), ("calm", 400)],
    "neutral": [("smile", 600)],
}


def _rig_generate(intent: str, emo: str, mode: int, rng) -> dict:
    frames = [{"preset": p, "ms": ms} for p, ms in RIG_SEQUENCES.get(emo, RIG_SEQUENCES["neutral"])]
    frames[-1]["blush"] = round(rng.uniform(0.3, 0.8), 2)
    ops = [{"type": "face", "frames": frames, "blend_ms": 200, "hold": "last"}]
    params = {"happy": {"jump": 2}, "surprised": {"jump": 1}, "angry": {"shake": 1}}.get(emo)
    if params:
        ops.append({"type": "parameter_animation", "duration_ms": 600, "params": params})
    effect = {"happy": ("sparkle", "*+✦", "#ffe27a"), "love": ("particles", "♥♡", "#ff6f9f"),
              "surprised": ("symbols", "!", "#ffe066"), "confused": ("symbols", "?", "#9fe8ff"),
              "angry": ("symbols", "#", "#ff5a5a"), "sleepy": ("symbols", "z", "#c8d0ff")}.get(emo)
    if effect:
        ops.append({"type": "effect", "effect": effect[0], "chars": effect[1], "color": effect[2], "duration_ms": 1600})
    if mode == 3:
        ops = [{"type": "effect", "effect": "glitch", "duration_ms": 1400, "intensity": 0.8},
               {"type": "full_scene", "hide": ["body", "face"], "then": "restore", "duration_ms": 2400,
                "rules": [{"effect": "dissolve", "intensity": 0.8}]},
               {"type": "face", "delay_ms": 2400, "frames": [{"preset": "dizzy", "ms": 900}, {"preset": "smile", "ms": 500}]}]
    if "气泡" in intent or "说" in intent:
        ops.append({"type": "effect", "effect": "bubble", "text": intent[:20], "duration_ms": 2500})
    return {"mode": mode, "intent": intent, "expression": frames[-1]["preset"], "think": f"MOCK {emo.upper()}",
            "priority": 5, "ops": ops, "end_state": {"face": "hold"}}


def generate(intent: str, state: dict, allowed_modes=(1, 2, 3), seed=None) -> dict:
    rng = random.Random(seed if seed is not None else time.time_ns())
    emo = classify(intent)
    if ((state or {}).get("face_region") or {}).get("rig"):
        low = intent.lower()
        mode = 3 if 3 in allowed_modes and any(k in low for k in MODE3_WORDS) else 2
        return _rig_generate(intent, emo, mode, rng)
    face = (state or {}).get("face_region") or {"w": 20, "h": 8}
    w, h = face["w"], face["h"]
    low = intent.lower()
    mode = 2
    if 3 in allowed_modes and any(k in low for k in MODE3_WORDS):
        mode = 3
    elif 1 in allowed_modes and any(k in low for k in MODE1_WORDS) and emo == "neutral":
        mode = 1
    ops: list = []
    if mode == 1:
        params = {"blink": "twice" if "眨" in intent else "once"}
        if "左" in intent:
            params["gaze_x"] = -1
        if "右" in intent:
            params["gaze_x"] = 1
        if "跳" in intent:
            params["jump"] = 4
        if "点头" in intent or "低头" in intent:
            params["gaze_y"] = 1
        if "晃" in intent or "动" in intent:
            params["sway"] = 2
        ops.append({"type": "parameter_animation", "duration_ms": 900, "params": params})
        return {"mode": 1, "intent": intent, "expression": "glancing", "think": "LOOK AROUND", "priority": 3,
                "ops": ops, "end_state": {"face": "hold"}}

    eye_style, mouth = rng.choice(STYLE_BY_EMO.get(emo, STYLE_BY_EMO["neutral"]))
    brows = {"angry": ("\\", "/"), "sad": ("/", "\\"), "surprised": ("^", "^"), "confused": ("~", "^")}.get(emo)
    blush = emo in ("shy", "love") or (emo == "happy" and rng.random() < 0.4)
    frames = []
    a, eyes = compose_face(w, h, eye_style, mouth, rng, blush, brows)
    if emo in ("happy", "love"):
        b, _ = compose_face(w, h, "open", "smile", rng, blush, brows)
        frames = [b.frame(220), a.frame(420), b.frame(200), a.frame(600)]
    elif emo == "surprised":
        b, _ = compose_face(w, h, "closed", "small", rng)
        frames = [b.frame(90), a.frame(700)]
    elif emo == "confused":
        b, _ = compose_face(w, h, eye_style, mouth, rng, False, brows, eye_shift=-1)
        c, _ = compose_face(w, h, eye_style, mouth, rng, False, brows, eye_shift=1)
        frames = [b.frame(300), c.frame(300), a.frame(400)]
    elif emo == "sleepy":
        b, _ = compose_face(w, h, "open", "small", rng)
        frames = [b.frame(500), a.frame(900), b.frame(250), a.frame(1200)]
    else:
        frames = [a.frame(400)]
    ops.append({"type": "face_patch", "palette": PALETTE, "frames": frames, "loop": "once", "hold": "last",
                "eyes": eyes})
    if len(frames) > 1 and rng.random() < 0.5:
        # 演示差分帧：只改嘴巴
        mt, mc = MOUTHS["open"]
        ops.append({"type": "delta_update", "layer": "face", "delay_ms": sum(f["ms"] for f in frames) + 200,
                    "palette": PALETTE, "changes": [{"x": w // 2 - 1, "y": min(h - 1, h - 2), "text": mt, "colors": mc}]})
    params = {"happy": {"jump": 3, "bob": 2}, "love": {"bob": 2, "sway": 1}, "angry": {"shake": 2},
              "confused": {"head_tilt": 1, "gaze_x": 0.6}, "sad": {"body_dy": 1, "bob": 0.4},
              "surprised": {"jump": 2}, "sleepy": {"bob": 0.5, "speed": 0.6}, "shy": {"gaze_x": -1, "head_tilt": -1}
              }.get(emo)
    if params:
        ops.append({"type": "parameter_animation", "duration_ms": 900, "params": params})
    effect = {"happy": ("sparkle", "*+✦", "#ffe27a"), "love": ("particles", "♥♡", "#ff6f9f"),
              "surprised": ("symbols", "!", "#ffe066"), "confused": ("symbols", "?", "#9fe8ff"),
              "angry": ("symbols", "#", "#ff5a5a"), "sad": ("particles", "'.", "#8fc8ff"),
              "sleepy": ("symbols", "z", "#c8d0ff"), "think": ("symbols", ".o", "#c8f0ff")}.get(emo)
    if effect:
        name, chars, color = effect
        e = {"type": "effect", "effect": name, "chars": chars, "color": color, "duration_ms": 1800,
             "count": 18, "intensity": 0.7}
        if name == "particles":
            e["direction"] = "down" if emo == "sad" else "up"
        ops.append(e)
    if mode == 3:
        ops = [{"type": "effect", "effect": "glitch", "duration_ms": 1600, "intensity": 0.8},
               {"type": "full_scene", "hide": ["body", "face"], "then": "restore", "duration_ms": 2600,
                "rules": [{"effect": "dissolve", "intensity": 0.8}]},
               {"type": "effect", "effect": "flash", "delay_ms": 2600, "duration_ms": 500, "color": "#bff"}] + \
              [op for op in ops if op["type"] == "face_patch"]
    if "气泡" in intent or "说" in intent:
        ops.append({"type": "effect", "effect": "bubble", "text": intent[:20], "duration_ms": 2500})
    return {"mode": mode, "intent": intent, "expression": f"{emo}", "think": f"MOCK {emo.upper()} FACE",
            "priority": 5, "ops": ops, "end_state": {"face": "hold"}}


class MockClient:
    name = "mock"

    @staticmethod
    def _evolve(messages: list) -> dict:
        try:
            req = json.loads(messages[-1]["content"])
        except ValueError:
            req = {}
        if "designs" in req:          # 照镜子复查：全部通过
            obj = {"checks": [{"name": d.get("name", ""), "ok": True} for d in req["designs"]]}
        else:
            actions = [{"op": "create", "name": "憋笑", "emotion": "想笑又努力忍住", "from": "shy",
                        "spec": {"eyes": {"smile": 0.55, "open": 0.7}, "mouth": {"shape": "wavy", "curve": 0.4},
                                 "blush": 0.8, "gaze": [-0.5, 0.3]}}]
            # 被 👎 的自创表情：改得更夸张一点
            for c in req.get("custom_presets") or []:
                if c.get("dislikes", 0) > c.get("likes", 0):
                    actions.append({"op": "revise", "name": c["name"], "reason": "（Mock）用户不喜欢，改得更明显",
                                    "spec": {"blush": 1.0, "mouth": {"open": 0.4}}})
                    break
            obj = {"thoughts": "（Mock）她经常想笑又忍住，缺一个“憋笑”的表情。", "actions": actions}
        return {"text": json.dumps(obj, ensure_ascii=False), "usage": {"mock": True}, "elapsed": 0.0, "model": "mock"}

    def complete(self, messages: list, max_tokens: int = 1500, temperature: float = 0.8, timeout: float = 30,
                 extra: dict | None = None) -> dict:
        if messages and "表情设计师" in messages[0]["content"]:
            return self._evolve(messages)
        if messages and ("夜间回顾" in messages[0]["content"] or "独立的审核员" in messages[0]["content"]):
            if "夜间回顾" in messages[0]["content"]:      # 副人格：Mock 的回顾总是“没有值得记下的变化”
                obj = {"thoughts": "（Mock）今天没有值得记下的变化。", "proposals": []}
            elif "results" in messages[0]["content"] and "修改了桌宠的人设" in messages[0]["content"]:
                obj = {"results": []}
            else:
                obj = {"supported": False, "overreach": True, "canon_conflict": "可能", "injection": False,
                       "values": False, "event_or_name": False, "contradicts": [], "same_meaning": None,
                       "counter_found": False, "risk": "中", "confidence": 0.3, "note": "（Mock）不做真实审核"}
            return {"text": json.dumps(obj, ensure_ascii=False), "usage": {"mock": True}, "elapsed": 0.0, "model": "mock"}
        req = json.loads(messages[-1]["content"]) if messages and messages[-1]["content"].lstrip().startswith("{") else {}
        intent = req.get("intent") or (messages[-1]["content"] if messages else "")
        state = req.get("visual_state") or {}
        allowed = tuple(req.get("allowed_modes") or (1, 2, 3))
        t0 = time.perf_counter()
        time.sleep(0.25)  # 模拟网络延迟，便于观察“等待期间继续播放待机动画”
        obj = generate(intent, state, allowed)
        text = json.dumps(obj, ensure_ascii=False)
        return {"text": text, "usage": {"prompt_tokens": 0, "completion_tokens": len(text) // 3, "mock": True},
                "elapsed": time.perf_counter() - t0, "model": "mock"}
