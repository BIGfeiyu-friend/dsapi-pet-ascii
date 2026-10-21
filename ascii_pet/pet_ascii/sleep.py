"""睡眠模式：无互动超过一段时间（默认 30 分钟）后入睡；在对话里给她发消息会把她吵醒。

动画完全由本地预设生成，不调用 API。为了不像在“播片”，睡眠不是一段循环素材，而是一个即时编排的导演：
- 每一口呼吸的长短都不同（约 3.3~5.5 秒，越睡越慢），吸气时嘴微张、脸微红、画面微亮，呼气时冒出一串 Z；
- 梦境情绪会慢慢变换：安稳、轻鼾、梦里笑、皱眉、说梦话、流口水，每种持续几口呼吸，切换时缓慢过渡；
- 各种小事件按概率和冷却时间随机穿插：眼皮跳一下、深深叹一口气、梦话气泡、鼻涕泡鼓起又破掉、
  梦里的小星星、口水滴、身上字符流里的“梦话”文字；
- 入睡有 3 种过程，吵醒有 6 种反应，都带随机的节奏抖动，吵醒时不会连续两次一样。

状态：awake → falling（入睡过程）→ asleep（睡眠循环）→ waking（吵醒动画）→ awake
动画时间用引擎时间（engine.t），是否该入睡用真实时间（可注入时钟，便于测试）。
"""
from __future__ import annotations

import logging
import random
import time

from . import effects as fx
from .protocol import parse_animation

log = logging.getLogger("pet.ascii")

SLEEP_EXPR = "睡觉"
AWAKE_THINK = "<THINK> HELLO WORLD.SEARCH(YOU);"

SLEEP_TALK = ["唔……布丁……", "主人……再五分钟……", "嘿嘿……", "……不吃胡萝卜……", "数到第三十八朵云了……",
              "……别走嘛……", "唔嗯……好软……", "……今天也辛苦啦……", "星星……掉下来了……", "……鲸鱼……在天上游……",
              "……不是我吃的……", "唔……再一口……"]
DREAMS = ["<SLEEP> zzz... dream.search(pudding);", "<DREAM> counting_clouds(38)... 39...", "<SLEEP> whale.swim(sky=True);",
          "<DREAM> stars.collect(); stars.collect();", "<SLEEP> zzz... zzz... zzz...", "<DREAM> master.stay(forever);",
          "<SLEEP> recharging...... 87%", "<DREAM> pudding_mountain.climb();", "<SLEEP> do_not_disturb = True;"]

# 每种吵醒反应最后停在哪个表情（醒来后标题栏、👍/👎 评价都按这个算，而不是“被吵醒”）
WAKE_END = {"startle": "smile", "groggy": "sleepy", "grumpy": "pout", "drool": "shy", "glitch": "smile", "stretch": "happy"}

# 标定图（maid_rig.json）上的几个点：额头右上（Z 冒出来的地方）、鼻尖右侧（鼻涕泡）、右嘴角（口水）
FOREHEAD = (1084, 380)
NOSE = (974, 503)
MOUTH_CORNER = (979, 544)


def face(eyes=None, eye_l=None, eye_r=None, mouth=None, brows=None, blush=0.4, sweat=0.0, gloom=0.0, gaze=(0.0, 0.0)):
    """完整的表情规格（每一组都写全，关键帧之间不会继承到意外的值）。"""
    spec = {"eyes": {"open": 1.0, "smile": 0.0, "tilt": 0.0, "iris": 1.0, "pupil": "art", "tear": 0.0, "highlight": 1.0,
                     **(eyes or {})},
            "mouth": {"shape": "normal", "open": 0.0, "width": 1.0, "curve": 0.0, **(mouth or {})},
            "brows": {"y": 0.0, "angle": 0.0, **(brows or {})},
            "blush": blush, "sweat": sweat, "gloom": gloom, "gaze": [float(gaze[0]), float(gaze[1])]}
    if eye_l:
        spec["eye_l"] = dict(eye_l)
    if eye_r:
        spec["eye_r"] = dict(eye_r)
    return spec


CLOSED = {"open": 0.0, "smile": 0.0, "tilt": -0.12}

# 梦境情绪：呼气时的样子 + 吸气时的变化
MOODS = {
    "peaceful": {"base": dict(eyes=CLOSED, mouth={"shape": "normal", "curve": 0.15, "width": 0.75}, blush=0.42,
                              brows={"y": -0.2}),
                 "inhale": {"mouth": {"open": 0.08}, "blush": 0.47}},
    "snore": {"base": dict(eyes=CLOSED, mouth={"shape": "o", "open": 0.1, "width": 0.8}, blush=0.38, brows={"y": -0.25}),
              "inhale": {"mouth": {"open": 0.32}, "blush": 0.44}},
    "smile": {"base": dict(eyes={**CLOSED, "tilt": 0.0, "smile": 0.8}, mouth={"shape": "cat", "curve": 0.5, "width": 0.85},
                           blush=0.62),
              "inhale": {"mouth": {"open": 0.06}, "blush": 0.7}},
    "grumble": {"base": dict(eyes={**CLOSED, "tilt": -0.38}, mouth={"shape": "pout", "width": 0.8}, blush=0.34,
                             brows={"angle": -0.45}, gloom=0.38),
                "inhale": {"blush": 0.38, "gloom": 0.3}},
    "mumble": {"base": dict(eyes=CLOSED, mouth={"shape": "wavy", "open": 0.08, "width": 0.7}, blush=0.45),
               "inhale": {"mouth": {"shape": "o", "open": 0.2}}},
    "drool": {"base": dict(eyes=CLOSED, mouth={"shape": "o", "open": 0.22, "width": 0.9}, blush=0.46, brows={"y": -0.3}),
              "inhale": {"mouth": {"open": 0.34}}},
}
MOOD_WEIGHTS = {"peaceful": 34, "snore": 20, "smile": 16, "mumble": 12, "grumble": 8, "drool": 10}


def mood_face(mood: str, inhale: bool = False, rng: random.Random | None = None) -> dict:
    m = MOODS[mood]
    kw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in m["base"].items()}
    if inhale:
        for k, v in m["inhale"].items():
            kw[k] = {**kw.get(k, {}), **v} if isinstance(v, dict) else v
    if rng is not None:                                   # 每口呼吸都有一点点不同
        kw["blush"] = max(0.0, min(1.0, kw.get("blush", 0.4) + rng.uniform(-0.03, 0.03)))
        if "mouth" in kw and "open" in kw["mouth"]:
            kw["mouth"]["open"] = max(0.0, min(1.0, kw["mouth"]["open"] + rng.uniform(-0.03, 0.04)))
    return face(**kw)


class Seq:
    """把一段动画排成协议 JSON：若干段表情关键帧（各自的过渡速度）+ 特效 + 自定义回调，时间带随机抖动。"""

    def __init__(self, rng: random.Random, jitter: float = 0.12):
        self.rng, self.jitter = rng, jitter
        self.t = 0                      # 毫秒
        self.ops: list = []
        self.calls: list = []           # (毫秒, 回调)

    def _j(self, ms: float) -> int:
        return max(40, int(ms * self.rng.uniform(1 - self.jitter, 1 + self.jitter)))

    def face(self, frames: list, blend: int) -> "Seq":
        fr = []
        total = 0
        for spec, ms in frames:
            ms = self._j(ms)
            fr.append({**spec, "ms": ms})
            total += ms
        b = max(40, min(int(blend), min(f["ms"] for f in fr)))
        self.ops.append({"type": "face", "frames": fr, "blend_ms": b, "hold": "last", "delay_ms": self.t})
        self.last_start, self.last_ms = self.t, [f["ms"] for f in fr]
        self.t += total
        return self

    def at(self, ms: float, fn) -> "Seq":
        """在这段动画的第 ms 毫秒执行回调（绝对时间）。"""
        self.calls.append((max(0, int(ms)), fn))
        return self

    def wait(self, ms: float) -> "Seq":
        self.t += self._j(ms)
        return self

    def fx(self, effect: str, offset: int = 0, **params) -> "Seq":
        self.ops.append({"type": "effect", "effect": effect, "delay_ms": max(0, self.t + offset), **params})
        return self

    def call(self, fn, offset: int = 0) -> "Seq":
        self.calls.append((max(0, self.t + offset), fn))
        return self

    def build(self, expression: str, intent: str, priority: int = 6) -> dict:
        return {"mode": 2, "intent": intent, "expression": expression, "priority": priority, "ops": self.ops,
                "end_state": {"face": "hold"}}


class SleepController:
    def __init__(self, rt, clock=None, rng: random.Random | None = None):
        self.rt = rt
        self.clock = clock or time.monotonic
        self.rng = rng or random.Random()
        self.state = "awake"
        self.last_activity = self.clock()
        self.queue: list = []           # [(引擎时间, 回调)]
        self.next_beat_at = 0.0
        self.mood = "peaceful"
        self.mood_left = 0
        self.cool: dict = {}            # 事件 → 引擎时间（之前不再触发）
        self.zzz = None
        self.night = None
        self.bubble = None
        self.pending_tags: list = []
        self.after_calls: list = []
        self.woken_by_chat = False
        self.last_fall = self.last_wake = ""
        self.recent_talk: list = []
        self.talk_line, self.talk_until = "", 0.0       # 正在说的梦话（标题栏也显示一份，字符画里的中文太小）
        self.asleep_since = 0.0
        self.saved_think = AWAKE_THINK
        self.beats = 0
        self.history: list = []         # 最近几拍的摘要（测试与测试台用）

    # ------------------------------------------------------------------ 属性
    @property
    def engine(self):
        return self.rt.engine

    @property
    def sleeping(self) -> bool:
        return self.state in ("falling", "asleep")

    @property
    def busy(self) -> bool:
        return self.state != "awake"

    def idle_seconds(self) -> float:
        return max(0.0, self.clock() - self.last_activity)

    def status_text(self) -> str:
        text = {"falling": "犯困…", "asleep": "睡眠中 zZ", "waking": "被吵醒了"}.get(self.state, "")
        if text and self.state != "waking" and self.talk_line and self.engine.t < self.talk_until:
            text += f"  「{self.talk_line}」"
        return text

    def _say(self, text: str, seconds: float) -> None:
        self.talk_line, self.talk_until = text, self.engine.t + seconds

    # ------------------------------------------------------------------ 互动与计时
    def activity(self) -> None:
        self.last_activity = self.clock()

    def check(self) -> bool:
        """定时调用：无互动超过设定时间就入睡。返回是否开始入睡。"""
        cfg = self.rt.cfg
        if not getattr(cfg, "sleep_enabled", True) or self.state != "awake" or not self.engine.rigged:
            return False
        if self.idle_seconds() < max(1.0, float(getattr(cfg, "sleep_after_min", 30.0))) * 60:
            return False
        if self.rt.director.busy or getattr(self.rt, "_idle_pending", None) is not None:
            return False
        if getattr(getattr(self.rt, "room", None), "busy", False):     # 正在自己的房间里：出来再睡
            return False
        self.fall_asleep()
        return True

    def tick(self) -> None:
        """每帧调用：执行到点的回调；睡眠中按时排下一拍。"""
        t = self.engine.t
        if self.queue:
            due = [q for q in self.queue if q[0] <= t]
            if due:
                self.queue = [q for q in self.queue if q[0] > t]
                for _at, fn in sorted(due, key=lambda q: q[0]):
                    try:
                        fn()
                    except Exception:
                        log.exception("字符画：睡眠动画回调出错")
        if self.state == "asleep" and t >= self.next_beat_at:
            self._play_beat()

    def _schedule(self, seq: Seq, anim_dict: dict) -> float:
        """播放一段 Seq，回调按引擎时间排队。返回这段的结束时间。"""
        t0 = self.engine.t
        self.rt.apply_json(anim_dict)
        for ms, fn in seq.calls:
            self.queue.append((t0 + ms / 1000.0, fn))
        return t0 + seq.t / 1000.0

    def _point(self, src, dx=0, dy=0, fallback=(0.5, 0.5)):
        p = self.engine.face_point(*src)
        if p is None:
            fr = self.engine.face_rect() or (0, 0, self.engine.W, self.engine.H)
            p = (int(fr[0] + fr[2] * fallback[0]), int(fr[1] + fr[3] * fallback[1]))
        return p[0] + dx, p[1] + dy

    def _cooled(self, name: str) -> bool:
        return self.engine.t >= self.cool.get(name, 0.0)

    def _cooldown(self, name: str, lo: float, hi: float) -> None:
        self.cool[name] = self.engine.t + self.rng.uniform(lo, hi)

    # ------------------------------------------------------------------ 入睡
    def fall_asleep(self, variant: str | None = None) -> str:
        if self.state != "awake" or not self.engine.rigged:
            return ""
        options = [v for v in ("yawn", "nod", "goodnight") if v != self.last_fall] or ["yawn"]
        variant = variant or self.rng.choice(options)
        self.last_fall = variant
        self.state = "falling"
        self.queue.clear()
        self.saved_think = self.engine.think_text or AWAKE_THINK
        idle = getattr(self.rt, "idle_state", None)
        if idle is not None:
            self.rt.idle_state = None
        seq = getattr(self, f"_fall_{variant}")()
        seq.call(self._start_night, offset=-seq.t // 2)
        seq.call(self._asleep)
        self._schedule(seq, seq.build(SLEEP_EXPR, "入睡", priority=5))
        log.info("字符画：已经 %d 分钟没有互动，入睡（%s）", int(self.idle_seconds() // 60), variant)
        self.rt._notify()
        return variant

    def _fall_yawn(self) -> Seq:
        s = Seq(self.rng)
        s.face([(face(eyes={"open": 0.45}, mouth={"open": 0.08}, blush=0.42), 900)], 600)
        s.face([(face(eyes={"smile": 0.85, "tear": 0.35}, mouth={"shape": "o", "open": 0.95, "width": 1.15},
                      blush=0.5), 1300)], 500)
        s.fx("bubble", offset=-1200, text="哈啊——", duration_ms=1500)
        s.call(lambda: self._say("哈啊——", 1.5), offset=-1200)
        s.face([(face(eyes={"open": 0.3, "tear": 0.25}, mouth={"shape": "o", "open": 0.15}, blush=0.45), 1100)], 700)
        s.face([(face(eyes={"open": 0.0}, blush=0.45), 520), (face(eyes={"open": 0.28}, blush=0.45), 700),
                (face(eyes={"open": 0.0}, blush=0.45), 700), (face(eyes={"open": 0.18}, blush=0.45), 650)], 420)
        s.face([(mood_face("peaceful"), 1700)], 1300)
        return s

    def _fall_nod(self) -> Seq:
        s = Seq(self.rng)
        s.face([(face(eyes={"open": 0.5}, gaze=(0, 0.3), blush=0.4), 900)], 600)
        s.face([(face(eyes={"open": 0.25}, gaze=(0, 0.6), blush=0.42), 1400),
                (face(eyes={"open": 0.08}, gaze=(0, 0.7), blush=0.42), 1200)], 900)
        s.face([(face(eyes={"open": 1.05, "iris": 0.85}, mouth={"shape": "o", "open": 0.2}, sweat=0.35), 750)], 110)
        s.fx("symbols", offset=-700, chars="!", color="#ffe066", duration_ms=900)
        s.fx("shake", offset=-740, intensity=0.25, duration_ms=300)
        s.face([(face(eyes={"open": 0.45}, sweat=0.15, blush=0.42), 900)], 600)
        s.face([(face(eyes={"open": 0.2}, gaze=(0, 0.4), blush=0.44), 1200),
                (face(eyes={"open": 0.0}, gaze=(0, 0.3), blush=0.44), 1300)], 1000)
        s.face([(mood_face("peaceful"), 1500)], 1100)
        return s

    def _fall_goodnight(self) -> Seq:
        s = Seq(self.rng)
        s.face([(face(eyes={"open": 0.6, "smile": 0.4}, mouth={"curve": 0.4}, blush=0.5), 900)], 600)
        s.fx("bubble", offset=-850, text="晚安……", duration_ms=2600)
        s.call(lambda: self._say("晚安……", 2.6), offset=-850)
        s.face([(face(eye_l={"open": 0.0}, eye_r={"open": 0.5}, eyes={"open": 0.5}, mouth={"curve": 0.3}, blush=0.5), 600),
                (face(eye_l={"open": 0.5}, eye_r={"open": 0.0}, eyes={"open": 0.5}, mouth={"curve": 0.3}, blush=0.5), 600)], 300)
        s.face([(face(eyes={"smile": 0.8, "tear": 0.2}, mouth={"shape": "o", "open": 0.6}, blush=0.55), 1000)], 450)
        s.face([(face(eyes={**CLOSED, "tilt": 0.0, "smile": 0.8}, mouth={"shape": "normal", "curve": 0.4, "width": 0.8},
                      blush=0.58), 1800)], 1200)
        self.mood = "smile"
        return s

    def _start_night(self) -> None:
        eng = self.engine
        if self.night is None:
            self.night = fx.NightMood(self.rng)
            self.night.glow_fn = lambda: getattr(eng, "fx_cells", ())
            eng.add_effect(self.night)
        if self.zzz is None:
            self.zzz = fx.SleepZ(self._point(FOREHEAD, 3, -1, fallback=(0.9, 0.1)), self.rng)
            eng.add_effect(self.zzz)
            self.night.glow_sources = [self.zzz]

    def _asleep(self) -> None:
        if self.state != "falling":
            return
        self.state = "asleep"
        self.asleep_since = self.engine.t
        if self.mood not in MOODS:
            self.mood = "peaceful"
        self.mood_left = self.rng.randint(2, 4)
        self.engine.set_think(self.rng.choice(DREAMS))
        self._cooldown("dream_text", 60, 150)
        self._cooldown("talk", 40, 120)
        self._cooldown("bubble", 50, 110)
        self._cooldown("twitch", 40, 90)
        self._cooldown("sigh", 25, 60)
        if self.zzz is not None:
            self.zzz.emit(2)
        self.next_beat_at = self.engine.t
        self.rt._notify()

    # ------------------------------------------------------------------ 睡眠循环（即时编排）
    def _next_mood(self) -> str:
        names = [m for m in MOOD_WEIGHTS if m != self.mood]
        return self.rng.choices(names, weights=[MOOD_WEIGHTS[m] for m in names])[0]

    def _play_beat(self) -> None:
        rng = self.rng
        s = Seq(rng, jitter=0.05)
        changed = False
        self.mood_left -= 1
        if self.mood_left <= 0:
            self.mood = self._next_mood()
            self.mood_left = rng.randint(2, 6)
            changed = True
        mood = self.mood
        deep = min(1.0, (self.engine.t - self.asleep_since) / 300.0)      # 睡得越久，呼吸越慢
        events = []
        if changed:                                                         # 换梦：先慢慢过渡过去
            s.face([(mood_face(mood, False, rng), rng.uniform(1600, 2400))], 1500)
        breaths = rng.choice((1, 2, 2, 3))
        sigh_at = rng.randrange(breaths) if (self._cooled("sigh") and rng.random() < 0.22) else -1
        for i in range(breaths):
            if s.t > 12000:                                                 # 一拍不超过十几秒（协议的延迟上限是 20 秒）
                break
            period = rng.uniform(3.3, 5.2) * (1 + 0.12 * deep)
            inh = period * rng.uniform(0.38, 0.48)
            # 一口呼吸 = 一个两帧的表情操作：吸气（慢慢过渡过去）→ 呼气（过渡完稍停一下，像真的呼吸那样）
            if i == sigh_at:                                                # 深深叹一口气
                period, inh = period * 1.5, period * 0.75
                up = face(**{**{k: v for k, v in MOODS[mood]["base"].items()}, "mouth": {"shape": "o", "open": 0.42},
                             "blush": MOODS[mood]["base"].get("blush", 0.4) + 0.08})
                out = lambda: (self._breath(False), self.zzz and self.zzz.emit(3, big=True, gap=0.55))
                self._cooldown("sigh", 45, 100)
                events.append("sigh")
            else:
                up = mood_face(mood, True, rng)
                n = rng.choice((0, 1, 2, 2, 3)) if rng.random() < 0.8 else 0
                out = lambda n=n: (self._breath(False), self.zzz and n and self.zzz.emit(n, big=rng.random() < 0.12))
            s.face([(up, inh * 1000), (mood_face(mood, False, rng), (period - inh) * 1000)], int(inh * 880))
            s.at(s.last_start, lambda: self._breath(True))
            s.at(s.last_start + s.last_ms[0], out)
            if i == 0 and self._cooled("twitch") and rng.random() < 0.14:  # 眼皮跳一下
                lid = mood_face(mood, False, rng)
                lid["eyes"] = {**lid["eyes"], "open": rng.uniform(0.17, 0.24)}
                s.face([(lid, 130), (mood_face(mood, False, rng), 260)], 70)
                self._cooldown("twitch", 70, 150)
                events.append("twitch")
        # 穿插的小事件
        if mood in ("peaceful", "smile", "mumble") and self._cooled("talk") and rng.random() < 0.3:
            text = rng.choice([t for t in SLEEP_TALK if t not in self.recent_talk] or SLEEP_TALK)
            self.recent_talk = (self.recent_talk + [text])[-4:]
            off, dur = -int(s.t * rng.uniform(0.5, 0.9)), int(rng.uniform(2600, 3600))
            s.fx("bubble", offset=off, text=text, duration_ms=dur)
            s.call(lambda: self._say(text, dur / 1000), offset=off)
            self._cooldown("talk", 140, 300)
            events.append("talk")
        if mood in ("snore", "peaceful", "drool") and self.bubble is None and self._cooled("bubble") and rng.random() < 0.25:
            s.call(self._start_bubble, offset=-s.t)
            self._cooldown("bubble", 110, 240)
            events.append("bubble")
        if mood == "smile" and self._cooled("sparkle") and rng.random() < 0.4:
            s.fx("sparkle", offset=-int(s.t * 0.6), chars="·✦*", color="#ffc8e8", intensity=0.25, anchor="face",
                 duration_ms=int(rng.uniform(1600, 2600)))
            self._cooldown("sparkle", 25, 55)
            events.append("sparkle")
        if mood == "drool" and self._cooled("drool") and rng.random() < 0.55:
            x, y = self._point(MOUTH_CORNER, 0, 1, fallback=(0.6, 0.85))
            s.fx("particles", offset=-int(s.t * 0.5), chars="'.", color="#9fd8ff", count=2, direction="down", speed=0.35,
                 anchor="canvas", x=x, y=y, duration_ms=1500)
            self._cooldown("drool", 14, 30)
            events.append("drool")
        if self._cooled("dream_text") and rng.random() < 0.5:
            s.call(lambda: self.engine.set_think(rng.choice(DREAMS)), offset=-s.t // 2)
            self._cooldown("dream_text", 120, 300)
        self.next_beat_at = self._schedule(s, s.build(SLEEP_EXPR, "睡眠", priority=4)) - 0.02
        self.beats += 1
        self.history = (self.history + [{"mood": mood, "breaths": breaths, "events": events,
                                         "ms": s.t}])[-12:]

    def _breath(self, inhale: bool) -> None:
        if self.night is not None:
            self.night.set_breath(inhale)
        if self.bubble is not None:
            self.bubble.breathe(inhale)
            if self.bubble.done or self.bubble.popped_at >= 0 and self.bubble.t - self.bubble.popped_at > 1.0:
                self.bubble = None

    def _start_bubble(self) -> None:
        if self.bubble is None and self.state == "asleep":
            self.bubble = fx.NoseBubble(self._point(NOSE, 1, 0, fallback=(0.55, 0.65)), self.rng,
                                        max_size=self.rng.choice((4, 5, 5)))
            self.engine.add_effect(self.bubble)
            if self.night is not None:
                self.night.glow_sources = [s for s in (self.zzz, self.bubble) if s is not None]

    # ------------------------------------------------------------------ 吵醒
    def wake(self, reason: str = "chat", variant: str | None = None) -> str:
        """吵醒：随机播放一个被吵醒的反应（不会连续两次一样）。不在睡觉时返回空字符串。"""
        if not self.sleeping:
            return ""
        pool = ("startle", "groggy", "grumpy", "drool", "glitch", "stretch") if reason != "poke" else \
            ("startle", "groggy", "grumpy", "glitch")
        options = [v for v in pool if v != self.last_wake] or list(pool)
        variant = variant or self.rng.choice(options)
        self.last_wake = variant
        self.state = "waking"
        self.queue.clear()
        seq = getattr(self, f"_wake_{variant}")()
        seq.call(self._awake)
        self._schedule(seq, seq.build("被吵醒", "被吵醒", priority=8))
        log.info("字符画：被吵醒（%s，%s）", reason, variant)
        self.rt._notify()
        return variant

    def end_sleep(self) -> None:
        """不播动画直接醒（手动切换表情、退出时）。"""
        if self.state == "awake":
            return
        self.queue.clear()
        self._clear_night(fast=True)
        self.state = "awake"
        self.engine.set_think(self.saved_think)
        self.activity()
        self.pending_tags.clear()
        calls, self.after_calls = self.after_calls, []
        for fn in calls:
            try:
                fn()
            except Exception:
                log.exception("字符画：醒来后处理回复出错")

    def _clear_night(self, fast: bool = False) -> None:
        if self.zzz is not None:
            self.zzz.stop(0.6 if fast else 2.0)
            self.zzz = None
        if self.bubble is not None:
            if fast:
                self.bubble.stop()
            else:
                self.bubble.pop()
            self.bubble = None
        if self.night is not None:
            self.night.stop(0.3 if fast else self.rng.uniform(0.6, 1.1))
            self.night = None

    def _awake(self) -> None:
        self.state = "awake"
        self.engine.set_think(self.saved_think)
        end = WAKE_END.get(self.last_wake, "neutral")
        cur = self.engine.current
        if cur is not None and cur.expression == "被吵醒":   # 尾巴上的特效还在播：结束时按最后的表情记
            cur.expression = end
        self.engine.expression = end
        self.engine.expression_t = self.engine.t
        self.activity()
        tags, self.pending_tags = self.pending_tags, []
        for tag in tags:                                 # 吵醒期间到达的回复表情：现在再演
            self.rt.apply_inline_tag(tag)
        calls, self.after_calls = self.after_calls, []
        for fn in calls:                                 # 吵醒期间结束的回复：现在再处理（挂机决定等）
            try:
                fn()
            except Exception:
                log.exception("字符画：吵醒后处理回复出错")
        self.rt._notify()

    def after_wake(self, fn) -> None:
        if self.state == "awake":
            fn()
        else:
            self.after_calls.append(fn)

    def intercept_tag(self, tag_text: str) -> bool:
        """回复表情标签到达时：睡着就先吵醒，吵醒动画播完再演这个表情。返回是否已接管。"""
        if self.state == "awake":
            return False
        if self.sleeping:
            self.wake("reply")
        self.pending_tags.append(tag_text)
        return True

    def consume_woken_by_chat(self) -> bool:
        was, self.woken_by_chat = self.woken_by_chat, False
        return was

    def _wake_start(self, s: Seq, fast: bool) -> None:
        s.call(lambda: self._clear_night(fast=fast))

    def _wake_startle(self) -> Seq:
        s = Seq(self.rng)
        self._wake_start(s, fast=True)
        s.face([(face(eyes={"open": 1.3, "iris": 0.5, "pupil": "dot"}, mouth={"shape": "triangle", "open": 0.6},
                      brows={"y": 1.0}, sweat=0.6, gloom=0.3, blush=0.3), 760)], 90)
        s.fx("symbols", offset=-760, chars="!", color="#ffe066", duration_ms=1100)
        s.fx("flash", offset=-760, intensity=0.35, duration_ms=380)
        s.fx("shake", offset=-760, intensity=0.5, duration_ms=520)
        s.face([(face(eyes={"open": 1.0, "tear": 0.2}, mouth={"shape": "wavy", "open": 0.2}, blush=1.0, sweat=0.7), 1100)], 300)
        s.face([(dict(preset="smile", blush=0.7), 900)], 500)
        return s

    def _wake_groggy(self) -> Seq:
        s = Seq(self.rng)
        self._wake_start(s, fast=False)
        s.face([(face(eyes={"open": 0.22}, mouth={"shape": "o", "open": 0.1}, blush=0.45), 1000)], 600)
        s.face([(face(eyes={"open": 0.0}, blush=0.45), 300), (face(eyes={"open": 0.35}, blush=0.45), 520)], 200)
        s.face([(face(eyes={"smile": 0.85, "tear": 0.4}, mouth={"shape": "o", "open": 0.9, "width": 1.1}, blush=0.5), 1200)],
               450)
        s.fx("symbols", chars="?", color="#9fe8ff", duration_ms=1300)
        s.face([(dict(preset="sleepy", blush=0.5), 1000)], 600)
        return s

    def _wake_grumpy(self) -> Seq:
        s = Seq(self.rng)
        self._wake_start(s, fast=False)
        s.face([(face(eyes={"open": 0.4, "tilt": 0.5}, eye_l={"open": 0.0}, mouth={"shape": "pout"}, blush=0.3, gloom=0.45),
                 1200)], 400)
        s.fx("symbols", offset=-900, chars="#", color="#ff6b6b", duration_ms=1000)
        s.face([(face(eyes={"open": 0.6, "tilt": 0.4}, mouth={"shape": "pout"}, gloom=0.2, blush=0.35), 900)], 500)
        s.face([(face(eyes={"open": 0.55, "tilt": 0.15}, mouth={"shape": "o", "open": 0.3}, blush=0.4), 650)], 300)
        s.face([(dict(preset="pout", level=0.6), 800)], 600)
        return s

    def _wake_drool(self) -> Seq:
        s = Seq(self.rng)
        x, y = self._point(MOUTH_CORNER, 0, 1, fallback=(0.6, 0.85))
        s.face([(mood_face("drool"), 300)], 200)
        s.fx("particles", offset=-300, chars="'.", color="#9fd8ff", count=3, direction="down", speed=0.4,
             anchor="canvas", x=x, y=y, duration_ms=1300)
        self._wake_start(s, fast=True)
        s.face([(face(eyes={"open": 1.15, "iris": 0.8}, mouth={"shape": "o", "open": 0.35}, blush=0.5), 700)], 120)
        s.fx("symbols", offset=-700, chars="!!", color="#ffe066", duration_ms=1000)
        s.face([(face(eyes={"open": 1.0}, mouth={"shape": "wavy", "open": 0.15, "width": 0.7}, blush=1.0, sweat=0.8,
                      gaze=(-0.6, 0.4)), 1200)], 250)
        s.face([(dict(preset="shy"), 900)], 600)
        return s

    def _wake_glitch(self) -> Seq:
        s = Seq(self.rng)
        self._wake_start(s, fast=True)
        s.fx("glitch", intensity=0.7, duration_ms=900)
        s.fx("shake", intensity=0.45, duration_ms=600)
        s.face([(face(eyes={"open": 1.0, "pupil": "spiral"}, mouth={"shape": "wavy", "open": 0.2}, sweat=0.5, blush=0.4),
                 1100)], 120)
        s.face([(dict(preset="confused"), 900)], 400)
        s.fx("symbols", offset=-800, chars="?", color="#9fe8ff", duration_ms=1100)
        s.face([(dict(preset="smile", level=0.8), 800)], 600)
        return s

    def _wake_stretch(self) -> Seq:
        s = Seq(self.rng)
        self._wake_start(s, fast=False)
        s.face([(face(eyes={"smile": 0.9}, mouth={"shape": "o", "open": 0.95, "width": 1.2}, blush=0.55), 1400)], 700)
        s.fx("sparkle", offset=-900, chars="*+✦", color="#ffe27a", intensity=0.4, duration_ms=1500)
        s.face([(face(eyes={"smile": 0.9}, mouth={"shape": "cat", "curve": 0.5}, blush=0.6), 900)], 500)
        s.face([(dict(preset="happy", level=0.8), 900)], 500)
        return s


def parse(anim_dict: dict, engine):
    """测试用：把 Seq 生成的 JSON 过一遍协议校验。"""
    return parse_animation(anim_dict, engine.context())
