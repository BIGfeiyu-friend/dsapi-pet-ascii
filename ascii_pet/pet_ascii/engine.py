"""本地动画引擎：保持结构、连续性与流畅播放（AI 负责想象，程序负责执行）。

画布布局（单位：字符格）：
  画布 = 角色最大尺寸 + 四周留白（头顶留得多，给气泡和符号）
  身体原点 = (margin_x, margin_top) + 身体位移
  面部原点 = 身体原点 + pose.face(x, y) + 视线/歪头偏移

图层顺序（z 从低到高）：背景 → 身体（含脸底）→ 身体纹理（字符流）→ 身体补丁 → 面部 → 覆盖补丁 → 特效 → 全画布场景 → 界面（气泡）
上层的非透明格完全替换下层同一格。全画布场景 / 解体特效可以暂时隐藏身体与脸，结束后自动恢复。

本地负责：呼吸漂浮、摆动、抖动、跳跃弧线、位移补间、眨眼、东张西望、背景滚动、特效逐帧、关键帧计时。
AI 负责：五官与形态的字符设计（关键帧）、动作意图与参数。
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from . import effects as fx
from . import face_rig as fr
from .background import Background
from .character import Character, Pose
from .grid import CLEAR, DEFAULT_COLOR, WIDE_CONT, Layer, color_hex, mix, with_alpha
from .protocol import Animation, Context, Frame, Op  # noqa: F401

PARAM_DEFAULTS = {
    "gaze_x": 0.0, "gaze_y": 0.0, "head_tilt": 0.0, "body_dx": 0.0, "body_dy": 0.0,
    "bob": 1.0, "sway": 0.0, "shake": 0.0, "jump": 0.0, "speed": 1.0, "tint_amount": 0.0, "opacity": 1.0,
}


@dataclass
class EngineSettings:
    margin_x: int = 8
    margin_top: int = 9
    margin_bottom: int = 2
    background: str = "none"
    code_rows: bool = True          # MV 风格：身体每 4 行嵌入一行滚动字符流
    code_rows_every: int = 4
    idle_blink: bool = True
    idle_glance: bool = True
    bob_px: float = 1.6
    sway_px: float = 2.0
    loop_cap_s: float = 30.0         # 无限循环的面部动画最多播放多久


def _ease(name: str, k: float) -> float:
    k = max(0.0, min(1.0, k))
    if name == "linear":
        return k
    if name == "in":
        return k * k
    if name == "out":
        return 1 - (1 - k) ** 2
    if name == "bounce":
        n1, d1 = 7.5625, 2.75
        if k < 1 / d1:
            return n1 * k * k
        if k < 2 / d1:
            k -= 1.5 / d1
            return n1 * k * k + 0.75
        if k < 2.5 / d1:
            k -= 2.25 / d1
            return n1 * k * k + 0.9375
        k -= 2.625 / d1
        return n1 * k * k + 0.984375
    return 0.5 - 0.5 * math.cos(math.pi * k)


@dataclass
class Tween:
    start: float
    end: float
    t0: float
    dur: float
    ease: str = "inout"

    def value(self, t: float) -> float:
        return self.start + (self.end - self.start) * _ease(self.ease, (t - self.t0) / self.dur)

    def done(self, t: float) -> bool:
        return t >= self.t0 + self.dur


class PatchTrack:
    """一段关键帧序列（面部或任意区域）。帧在开始时一次性物化为 Layer，播放时只按时间取帧。"""

    def __init__(self, op: Op, w: int, h: int, base: Layer | None, default_color: int, t0: float, loop_cap: float,
                 offset: tuple = (0, 0)):
        self.op = op
        self.t0 = t0
        self.layers: list[Layer] = []
        self.ms: list[float] = []
        ox, oy = offset
        prev = base.copy() if base is not None else Layer("patch", w, h)
        for f in op.frames:
            if f.rows is not None:
                L = Layer("patch", w, h)
                L.apply_patch(ox, oy, f.rows, f.colors, f.blank, f.mask, default_color)
            else:
                L = prev.copy()
                for d in f.delta or []:
                    L.apply_patch(ox + d["x"], oy + d["y"], [d["text"]], [d["colors"]], blank="clear",
                                  default_color=default_color)
            self.layers.append(L)
            self.ms.append(f.ms / 1000.0)
            prev = L
        seq = list(range(len(self.layers)))
        if op.loop == "pingpong" and len(seq) > 2:
            seq = seq + seq[-2:0:-1]
        self.seq = seq
        self.cycle = sum(self.ms[i] for i in seq) or 0.1
        if op.loop == "loop" and op.repeat <= 1:
            reps = max(1, int(loop_cap / self.cycle))  # 无限循环：播放到上限或被新动画替换
        else:
            reps = op.repeat
        self.total = op.duration_ms / 1000.0 if op.duration_ms else self.cycle * reps

    def frame_at(self, t: float) -> Layer:
        el = max(0.0, t - self.t0)
        if el >= self.total:
            return self.layers[-1] if self.op.loop == "once" or self.op.hold == "last" else self.layers[self.seq[-1]]
        el %= self.cycle
        for i in self.seq:
            el -= self.ms[i]
            if el < 0:
                return self.layers[i]
        return self.layers[self.seq[-1]]

    def done(self, t: float) -> bool:
        return t - self.t0 >= self.total

    @property
    def last(self) -> Layer:
        return self.layers[-1]


class ExprTrack:
    """表情关键帧轨道：每帧是一份完整的表情规格，帧与帧之间按 blend 时长补间（参数插值，由骨架重新绘制）。"""

    def __init__(self, op: Op, specs: list, start_spec: dict, t0: float, loop_cap: float):
        self.op = op
        self.t0 = t0
        self.specs = specs
        self.ms = [max(0.04, f.ms / 1000.0) for f in op.frames]
        self.start = start_spec
        self.blend = max(0.0, op.params.get("blend_ms", 180) / 1000.0)
        seq = list(range(len(specs)))
        if op.loop == "pingpong" and len(seq) > 2:
            seq = seq + seq[-2:0:-1]
        self.seq = seq
        self.cycle = sum(self.ms[i] for i in seq) or 0.1
        reps = max(1, int(loop_cap / self.cycle)) if op.loop in ("loop", "pingpong") and op.repeat <= 1 else op.repeat
        self.total = self.cycle * reps

    def spec_at(self, t: float) -> dict:
        el = max(0.0, t - self.t0)
        if el >= self.total:
            return self.specs[self.seq[-1]]
        first_cycle = el < self.cycle
        el %= self.cycle
        prev = self.start if first_cycle else self.specs[self.seq[-1]]
        for i in self.seq:
            dur = self.ms[i]
            if el < dur:
                k = min(1.0, el / min(self.blend, dur)) if self.blend > 0 else 1.0
                k = 0.5 - 0.5 * math.cos(math.pi * k)
                return fr.lerp_spec(prev, self.specs[i], k) if k < 0.999 else self.specs[i]
            el -= dur
            prev = self.specs[i]
            first_cycle = False
        return self.specs[self.seq[-1]]

    def done(self, t: float) -> bool:
        return t - self.t0 >= self.total

    @property
    def last(self) -> dict:
        return self.specs[self.seq[-1]]


class SceneTrack:
    def __init__(self, op: Op, w: int, h: int, t0: float):
        self.op = op
        self.t0 = t0
        self.track = PatchTrack(op, w, h, None, DEFAULT_COLOR, t0, 20.0) if op.frames else None
        self.total = max(op.duration_ms / 1000.0, self.track.total if self.track else 0.0) or 3.0

    def done(self, t: float) -> bool:
        return self.op.hold == "restore" and t - self.t0 >= self.total


class Engine:
    def __init__(self, character: Character, settings: EngineSettings | None = None, seed: int | None = None):
        self.char = character
        self.s = settings or EngineSettings()
        if character.scene:
            # 整幅场景图：画面本身就是矩形，不需要留白；也不做整体漂浮（否则整个房间会上下晃）
            self.s.margin_x = self.s.margin_top = self.s.margin_bottom = 0
            self.s.code_rows_every = max(self.s.code_rows_every, 6)
        self.rng = random.Random(seed)
        self.t = 0.0
        self.W = character.max_w + 2 * self.s.margin_x
        self.H = character.max_h + self.s.margin_top + self.s.margin_bottom
        self.bg = Background(self.W, self.H, self.rng)
        self.params = dict(PARAM_DEFAULTS)
        if character.scene:
            self.params["bob"] = 0.0
        self.face_spec = fr.neutral()
        self.expr_track: ExprTrack | None = None
        self.talk_until = 0.0
        self._face_layers: dict = {}
        self.tint_color = 0xFFFF7EB6
        self.tweens: dict[str, Tween] = {}
        self.jump: Tween | None = None
        self.face_track: PatchTrack | None = None
        self.region_tracks: list[PatchTrack] = []
        self.persistent: dict[str, dict] = {"body": {}, "overlay": {}, "background": {}}
        self.effects: list[fx.Effect] = []
        self.scene: SceneTrack | None = None
        self.pending: list[tuple[float, Op, Animation]] = []
        self.current: Animation | None = None
        self.current_end = 0.0
        self.current_t0 = 0.0            # 当前动画开始的时间
        self.expression_t = -1.0         # 最近一次直接切换表情（set_expression）的时间
        self.expression = "neutral"
        self.recent: list[str] = []
        self.blink_until = 0.0
        self.blink_start = 0.0
        self.blink_queue: list[float] = []
        self.next_blink = 2.0
        self.blink_interval = 4.0
        self.blink_hold = False
        self.next_glance = 6.0
        self.glance_return = 0.0
        self.think_text = "<THINK> HELLO WORLD.SEARCH(YOU);"
        self._tex_t = 0.0
        self._body_cells: list = []
        self._body_dirty = True
        self.stats = {"frames": 0, "compose_ms": 0.0}
        self.on_event = None  # 可选回调：(事件名, 数据)
        self.set_pose(character.default_pose, keep_face=False)
        self.set_background(self.s.background)

    # ------------------------------------------------------------------ 几何
    def _jump_height(self) -> float:
        if not self.jump:
            return 0.0
        k = max(0.0, min(1.0, (self.t - self.jump.t0) / self.jump.dur))
        return self.jump.end * 4 * k * (1 - k)

    @property
    def body_origin(self) -> tuple[int, int]:
        jump = self._jump_height()
        bx = self.s.margin_x + (self.char.max_w - self.pose.w) // 2 + int(round(self.params["body_dx"]))
        by = self.s.margin_top + (self.char.max_h - self.pose.h) + int(round(self.params["body_dy"] - jump))
        return bx, by

    @property
    def rigged(self) -> bool:
        return self.pose.rig_info is not None

    def face_rect(self) -> tuple[int, int, int, int] | None:
        f = self.pose.face
        if not f:
            return None
        bx, by = self.body_origin
        if self.rigged:
            return bx + f["x"], by + f["y"], f["w"], f["h"]   # 骨架脸：视线由瞳孔表现，脸本身不移位
        dx = int(round(self.params["gaze_x"])) + int(round(self.params["head_tilt"]))
        dy = int(round(self.params["gaze_y"] * 0.6))
        return bx + f["x"] + dx, by + f["y"] + dy, f["w"], f["h"]

    def context(self) -> Context:
        f = self.pose.face or {"w": 20, "h": 8}
        return Context(canvas_w=self.W, canvas_h=self.H, face_w=f["w"], face_h=f["h"],
                       body_w=self.pose.w, body_h=self.pose.h, poses=tuple(self.char.poses))

    def _geo(self, op: Op) -> dict:
        bx, by = self.body_origin
        fr = self.face_rect() or (bx + self.pose.w // 2 - 4, by + 4, 8, 4)
        if op.anchor == "face":
            ox, oy = fr[0] + fr[2] // 2 + op.x, fr[1] + fr[3] // 2 + op.y
            rect = fr
        elif op.anchor == "body":
            ox, oy = bx + self.pose.w // 2 + op.x, by + self.pose.h // 2 + op.y
            rect = (bx, by, self.pose.w, self.pose.h)
        else:
            ox, oy = op.x, op.y
            rect = (0, 0, self.W, self.H)
        head = (fr[0] + fr[2] // 2, max(1, fr[1] - 8))
        return {"origin": (ox, oy), "rect": rect, "head": head, "W": self.W, "H": self.H,
                "center": (bx + self.pose.w // 2, by + self.pose.h // 2)}

    # ------------------------------------------------------------------ 状态切换
    def set_pose(self, name: str, keep_face: bool = True) -> None:
        old_face = getattr(self, "pose", None)
        pose = self.char.pose(name)
        self.pose = pose
        same_face = old_face is not None and old_face.face and pose.face and \
            (old_face.face["w"], old_face.face["h"]) == (pose.face["w"], pose.face["h"])
        if keep_face and old_face is not None and old_face.face and pose.face and not same_face:
            self._refit_face(pose.face["w"], pose.face["h"])
        elif not (keep_face and same_face):
            self.reset_face()
        self.bg.set_aura(self.s.margin_x + self.char.max_w / 2, self.s.margin_top + self.char.max_h * 0.5,
                         self.char.max_w * 0.62, self.char.max_h * 0.62)
        self._body_dirty = True

    def _refit_face(self, w: int, h: int) -> None:
        """换姿势时面部区域尺寸变了：把当前的脸居中搬到新区域，而不是丢掉 AI 画的表情。"""
        old = self.face
        dx, dy = (w - old.w) // 2, (h - old.h) // 2
        L = Layer("face", w, h)
        L.blit(old, dx, dy)
        self.face = L
        self.eyes = [[e[0] + dx, e[1] + dy, e[2], e[3]] for e in self.eyes]
        self.face_track = None

    def reset_face(self) -> None:
        p = self.pose
        self.expr_track = None
        if p.rig_info is not None:
            self.face = Layer("face", p.face["w"], p.face["h"])   # 透明：只承载旧式字符补丁，五官由骨架绘制
            self.eyes = []
            self.face_spec = fr.neutral()
            self.face_track = None
            return
        if p.face and p.neutral is not None:
            self.face = p.neutral.copy()
        elif p.face:
            self.face = Layer("face", p.face["w"], p.face["h"])
        else:
            self.face = Layer("face", 0, 0)
        self.eyes = [list(e) for e in p.eyes]
        self.face_track = None

    def set_background(self, mode: str) -> None:
        self.s.background = mode
        self.bg.set_mode(mode)

    def set_think(self, text: str) -> None:
        if text:
            self.think_text = text if text.startswith(("<THINK>", "<DREAM>", "<SLEEP>", "<ROOM>")) else "<THINK> " + text
            self.bg.add_think(text)
            self._body_dirty = True

    # ------------------------------------------------------------------ 执行动画
    def apply(self, anim: Animation) -> None:
        """开始播放一段（已校验的）动画。新动画会替换正在播放的面部/区域关键帧，特效继续。"""
        if self.current is not None and self.current is not anim:
            self._remember(self.current)  # 被打断的动画也计入表情记忆
        self.current = anim
        self.current_t0 = self.t
        self.pending = [p for p in self.pending if p[2].priority > anim.priority]
        end = self.t
        # 同一段动画里的换姿势参数先执行，保证随后的面部补丁画在新姿势的脸上
        ops = sorted(anim.ops, key=lambda o: 0 if (o.type == "parameter_animation" and "pose" in o.params
                                                   and not o.delay_ms) else 1)
        for op in ops:
            start = self.t + op.delay_ms / 1000.0
            if op.delay_ms:
                self.pending.append((start, op, anim))
            else:
                self._run_op(op, anim)
            end = max(end, start + self._op_length(op))
        self.current_end = end
        if anim.think:
            self.set_think(anim.think)
        self._emit("anim_start", anim)

    def _op_length(self, op: Op) -> float:
        if op.type in ("parameter_animation", "effect"):
            return op.duration_ms / 1000.0
        if op.type == "face":
            ms = sum(f.ms for f in op.frames) / 1000.0
            return min(self.s.loop_cap_s, ms * max(op.repeat, 3)) if op.loop != "once" else ms * op.repeat
        ms = sum(f.ms for f in op.frames) / 1000.0
        if op.loop != "once":
            return min(self.s.loop_cap_s, ms * max(op.repeat, 3))
        return ms * op.repeat if op.type != "full_scene" else max(ms * op.repeat, op.duration_ms / 1000.0)

    def _run_op(self, op: Op, anim: Animation) -> None:
        t = self.t
        if op.type == "parameter_animation":
            self._run_params(op)
        elif op.type == "face":
            if self.rigged:
                start = self.current_face_spec()
                specs, base = [], start
                for f in op.frames:
                    base = fr.normalize(f.spec, base)
                    specs.append(base)
                self.expr_track = ExprTrack(op, specs, start, t, self.s.loop_cap_s)
        elif op.type == "face_patch":
            if self.pose.face:
                off = self._face_offset(anim)
                self.face_track = PatchTrack(op, self.face.w, self.face.h, self.face, self.pose.feature_color, t,
                                             self.s.loop_cap_s, off)
                if op.eyes:
                    self._pending_eyes = [[e[0] + off[0], e[1] + off[1], e[2], e[3]] for e in op.eyes]
        elif op.type == "delta_update":
            if op.layer == "face":
                if self.pose.face:
                    self.face_track = PatchTrack(op, self.face.w, self.face.h, self.face, self.pose.feature_color, t,
                                                 self.s.loop_cap_s, self._face_offset(anim))
            else:
                self.region_tracks.append(self._region_track(op, t))
        elif op.type == "region_patch":
            self.region_tracks.append(self._region_track(op, t))
        elif op.type == "full_scene":
            self.scene = SceneTrack(op, self.W, self.H, t)
            for rule in op.effect.get("rules", []):
                self._start_effect(rule, op, max(1.0, self.scene.total))
        elif op.type == "effect":
            self._start_effect(op.effect, op, op.duration_ms / 1000.0)

    def _face_offset(self, anim: Animation) -> tuple:
        fw, fh = anim.face_dims
        if not fw or not fh or (fw, fh) == (self.face.w, self.face.h):
            return 0, 0
        return (self.face.w - fw) // 2, (self.face.h - fh) // 2

    def _region_track(self, op: Op, t: float) -> PatchTrack:
        w = max((len(r) for f in op.frames if f.rows for r in f.rows), default=0)
        h = max((len(f.rows) for f in op.frames if f.rows), default=0)
        for f in op.frames:
            for d in f.delta or []:
                w = max(w, d["x"] + len(d["text"]))
                h = max(h, d["y"] + 1)
        tr = PatchTrack(op, max(1, w), max(1, h), None, DEFAULT_COLOR, t, self.s.loop_cap_s)
        return tr

    def _run_params(self, op: Op) -> None:
        p = op.params
        dur = op.duration_ms / 1000.0
        ease = p.get("ease", "inout")
        for k in ("gaze_x", "gaze_y", "head_tilt", "body_dx", "body_dy", "bob", "sway", "shake", "speed",
                  "tint_amount", "opacity"):
            if k in p:
                target = p[k]
                if k == "body_dx":
                    target = max(-self.s.margin_x - self.pose.w * 0.3, min(self.s.margin_x + self.pose.w * 0.3, target))
                self.tweens[k] = Tween(self.params[k], target, self.t, max(0.08, dur * (0.4 if k.startswith("gaze") else 1.0)), ease)
        if "jump" in p and p["jump"] > 0:
            self.jump = Tween(0.0, p["jump"], self.t, max(0.3, dur), "linear")
        if "tint" in p:
            self.tint_color = p["tint"]
        if "blink_interval_ms" in p:
            self.blink_interval = p["blink_interval_ms"] / 1000.0
        if "blink" in p:
            self._blink(p["blink"])
        if "background" in p:
            self.set_background(p["background"])
        if "pose" in p:
            self.set_pose(p["pose"])
        if any(k in p for k in ("gaze_x", "gaze_y", "head_tilt")):
            self.glance_return = self.t + dur + 4.0

    def _blink(self, kind: str) -> None:
        t = self.t
        self.blink_hold = kind == "closed"
        if kind == "once":
            self.blink_queue = [t]
        elif kind == "twice":
            self.blink_queue = [t, t + 0.28]
        elif kind == "rapid":
            self.blink_queue = [t + i * 0.2 for i in range(4)]
        elif kind == "slow":
            self.blink_queue = [t]
            self.blink_start = t
            self.blink_until = t + 0.45
        elif kind == "none":
            self.next_blink = t + 60
        elif kind == "auto":
            self.blink_hold = False
            self.next_blink = t + 1.0

    def add_effect(self, effect) -> None:
        """直接加入一个本地特效对象（睡眠控制器用，不经过协议）。"""
        self.effects.append(effect)

    def face_point(self, src_x: float, src_y: float) -> tuple[int, int] | None:
        """骨架标定图上的一点 → 画布格坐标（给睡眠特效定位：嘴角、鼻尖、额头）。"""
        rig = self.pose.rig if self.rigged else None
        fr = self.face_rect()
        if rig is None or fr is None:
            return None
        cx, cy = rig.cell(src_x, src_y)
        return int(round(fr[0] + cx)), int(round(fr[1] + cy))

    def _start_effect(self, spec: dict, op: Op, duration: float) -> None:
        geo = self._geo(op)
        source = None
        if spec["name"] == "dissolve":
            source = self._character_cells()
        e = fx.make_effect(spec, duration, self.rng, geo, source)
        if e is not None:
            self.effects.append(e)

    # ------------------------------------------------------------------ 推进
    def tick(self, dt: float) -> None:
        dt = max(0.0, min(0.25, dt))
        self.t += dt
        t = self.t
        for k, tw in list(self.tweens.items()):
            self.params[k] = tw.value(t)
            if tw.done(t):
                self.params[k] = tw.end
                del self.tweens[k]
        if self.jump:
            k = (t - self.jump.t0) / self.jump.dur
            if k >= 1:
                self.jump = None
        for start, op, anim in [p for p in self.pending if p[0] <= t]:
            self.pending.remove((start, op, anim))
            self._run_op(op, anim)
        idle_dt = dt * self.params["speed"]
        for e in self.effects:
            e.update(dt)
        self.effects = [e for e in self.effects if not e.done]
        if self.face_track and self.face_track.done(t):
            self._finish_face_track()
        if self.expr_track and self.expr_track.done(t):
            tr = self.expr_track
            self.expr_track = None
            self.face_spec = tr.last if tr.op.hold == "last" else fr.neutral()
        for tr in list(self.region_tracks):
            if tr.done(t):
                self.region_tracks.remove(tr)
                if tr.op.hold == "last":
                    self._commit_region(tr)
        if self.scene and self.scene.done(t):
            self.scene = None
        if self.current and t >= self.current_end and not self.face_track and not self.region_tracks \
                and not self.expr_track:
            self._finish_anim()
        self._idle(idle_dt)
        if self.bg.update(dt):
            pass
        if self.s.code_rows:
            self._tex_t += dt
            if self._tex_t > 0.12:
                self._tex_t = 0.0
                self._body_dirty = True

    def _finish_face_track(self) -> None:
        tr = self.face_track
        self.face_track = None
        if tr.op.hold == "last":
            self.face = tr.last.copy("face")
            eyes = getattr(self, "_pending_eyes", None)
            if eyes:
                self.eyes = [list(e) for e in eyes]
                self._pending_eyes = None
        elif tr.op.hold in ("neutral", "restore"):
            self.reset_face()

    def _commit_region(self, tr: PatchTrack) -> None:
        store = self.persistent.setdefault(tr.op.layer if tr.op.layer in self.persistent else "overlay", {})
        L = tr.last
        ox, oy = tr.op.x, tr.op.y
        for y in range(L.h):
            for x in range(L.w):
                ch = L.chars[y * L.w + x]
                if ch is not None:
                    store[(tr.op.anchor, ox + x, oy + y)] = (ch, L.colors[y * L.w + x])

    def _finish_anim(self) -> None:
        anim = self.current
        self.current = None
        if anim.end_state.get("face") == "neutral":
            if self.rigged:
                self.face_spec = fr.neutral()
            else:
                self.reset_face()
        if anim.end_state.get("pose"):
            self.set_pose(anim.end_state["pose"])
        self._remember(anim)
        self._emit("anim_end", anim)

    def _remember(self, anim: Animation) -> None:
        if anim.expression and not getattr(anim, "_remembered", False):
            anim._remembered = True
            self.expression = anim.expression
            self.recent.append(anim.expression)
            self.recent = self.recent[-8:]

    def _idle(self, dt: float) -> None:
        t = self.t
        if self.s.idle_blink and not self.face_track and t >= self.next_blink and not self.blink_hold:
            # 骨架脸在表情切换过程中照常眨眼（闭眼帧由骨架绘制，位置永远正确）
            self.blink_queue.append(t)
            self.next_blink = t + self.blink_interval * self.rng.uniform(0.6, 1.4)
        if self.blink_queue and self.blink_queue[0] <= t:
            self.blink_queue.pop(0)
            if t >= self.blink_until:
                self.blink_start = t
            self.blink_until = max(self.blink_until, t + 0.13)
        if self.s.idle_glance and not self.tweens and t >= self.next_glance and not self.current:
            self.next_glance = t + self.rng.uniform(5, 12)
            gx = self.rng.choice((-1.0, 1.0, 0.0))
            self.tweens["gaze_x"] = Tween(self.params["gaze_x"], gx, t, 0.25, "out")
            self.glance_return = t + self.rng.uniform(1.2, 2.5)
        if self.glance_return and t >= self.glance_return and not self.current:
            self.glance_return = 0.0
            for k in ("gaze_x", "gaze_y", "head_tilt"):
                if abs(self.params[k]) > 0.01 and k not in self.tweens:
                    self.tweens[k] = Tween(self.params[k], 0.0, t, 0.35, "inout")

    def _emit(self, name: str, data) -> None:
        if self.on_event:
            try:
                self.on_event(name, data)
            except Exception:
                pass

    # ------------------------------------------------------------------ 合成
    def _body_cell_list(self) -> list:
        """身体（含脸底）+ 字符流纹理，按身体局部坐标列出所有非空格。"""
        if not self._body_dirty and self._body_cells:
            return self._body_cells
        self._body_dirty = False
        b = self.pose.body
        f = self.pose.face
        cells = []
        every = self.s.code_rows_every
        stream = self.think_text + "   " if self.s.code_rows else ""
        off = int(self.t * 11)
        n = len(stream) or 1
        # 字符流不经过头部（面部区域向外扩一圈，含刘海），避免干扰表情
        if f:
            mx, my = (8, 10) if self.char.scene else (0, 0)
            hx0, hy0, hx1, hy1 = f["x"] - mx, f["y"] - my, f["x"] + f["w"] + mx, f["y"] + f["h"] + 2
        else:
            hx0 = hy0 = hx1 = hy1 = -1
        whiten = 0.15 if self.char.scene else 0.25
        for y in range(b.h):
            tex_row = self.s.code_rows and y % every == every - 1
            for x in range(b.w):
                i = y * b.w + x
                ch = b.chars[i]
                if ch is None:
                    continue
                col = b.colors[i]
                if tex_row and not (hx0 <= x < hx1 and hy0 <= y < hy1):
                    sch = stream[(y * 7 + x + off) % n]
                    if sch != " ":
                        ch = sch
                        col = mix(col, 0xFFFFFFFF, whiten)
                cells.append((x, y, ch, col))
        self._body_cells = cells
        return cells

    # ------------------------------------------------------------------ 骨架脸
    def current_face_spec(self) -> dict:
        return self.expr_track.spec_at(self.t) if self.expr_track else self.face_spec

    def set_expression(self, spec: dict, blend_ms: int = 220, hold_ms: int = 0) -> None:
        """直接切换到某个表情（本地调用，例如对话里的表情标签）。"""
        if not self.rigged:
            return
        target = fr.normalize(spec, self.current_face_spec())
        if isinstance(spec.get("preset"), str):
            self.expression = spec["preset"]
            self.expression_t = self.t
        frame = Frame(spec=spec, ms=max(blend_ms, 60) + hold_ms)
        op = Op(type="face", frames=[frame], hold="last", params={"blend_ms": blend_ms})
        self.expr_track = ExprTrack(op, [target], self.current_face_spec(), self.t, self.s.loop_cap_s)

    def talk(self, seconds: float) -> None:
        self.talk_until = max(self.talk_until, self.t + seconds)

    def _displayed_spec(self) -> dict:
        spec = self.current_face_spec()
        gx, gy = self.params["gaze_x"], self.params["gaze_y"]
        blinking = self.blink_hold or self.t < self.blink_until
        talking = self.t < self.talk_until
        if not (gx or gy or blinking or talking):
            return spec
        spec = {**spec, "eye_l": dict(spec["eye_l"]), "eye_r": dict(spec["eye_r"]), "mouth": dict(spec["mouth"])}
        if gx or gy:
            spec["gaze"] = [max(-1.0, min(1.0, spec["gaze"][0] + gx)), max(-1.0, min(1.0, spec["gaze"][1] + gy * 0.8))]
        if blinking:
            # 眨眼有过程：快速合上 → 闭一下 → 睁开（连续变形的眼睛会经过半闭；分档的手绘 / 程序绘制照样能用）
            f = 0.0
            if not self.blink_hold:
                dur = max(0.06, self.blink_until - self.blink_start)
                el, rem = self.t - self.blink_start, self.blink_until - self.t
                close_t, open_t = min(0.05, dur * 0.35), min(0.07, dur * 0.45)
                if el < close_t:
                    f = 1.0 - el / close_t
                elif rem < open_t:
                    f = 1.0 - rem / open_t
            for side in ("eye_l", "eye_r"):
                e = spec[side]
                if e["open"] > 0.16 and e["smile"] < 0.72 and e["pupil"] != "x":
                    e["open"] = max(0.05, e["open"] * f)
        mouth_part = (spec.get("parts") or {}).get("mouth")
        if talking and mouth_part not in (None, "builtin", "none"):
            comp = fr.PARTS.get(mouth_part) or {}
            if comp.get("talk", "bob") == "bob":            # 自创的嘴：说话时整体上下张合（节点纵向缩放）
                nodes = {k: dict(v) for k, v in (spec.get("nodes") or {}).items()}
                t = nodes.setdefault("mouth", {})
                t["sy"] = t.get("sy", 1.0) * (1.0 + 0.45 * abs(math.sin(self.t * 12.5)))
                spec["nodes"] = nodes
        elif talking and spec["mouth"]["shape"] in ("art", "normal", "line", "teeth"):
            m = spec["mouth"]
            m["shape"] = "normal" if m["shape"] in ("art", "line") else m["shape"]
            m["open"] = max(m["open"], 0.15 + 0.5 * abs(math.sin(self.t * 12.5)))
            m["curve"] = max(m["curve"], 0.2)
        return spec

    def _rig_layer(self) -> Layer:
        spec = self._displayed_spec()
        key = fr.spec_key(spec)
        L = self._face_layers.get(key)
        if L is None:
            chars, colors = self.pose.rig.render(spec)
            f = self.pose.face
            L = Layer("face", f["w"], f["h"])
            for y in range(min(f["h"], len(chars))):
                for x in range(min(f["w"], len(chars[y]))):
                    c = colors[y][x]
                    if c is None:
                        continue
                    L.set(x, y, chars[y][x], 0xFF000000 | (c[0] << 16) | (c[1] << 8) | c[2])
            self._face_layers[key] = L
            if len(self._face_layers) > 300:
                self._face_layers.pop(next(iter(self._face_layers)))
        if self.face_track or self.face.occupied():
            over = self.face_track.frame_at(self.t) if self.face_track else self.face
            L = L.copy()
            L.blit(over, 0, 0)
        return L

    def _face_layer_now(self) -> Layer:
        if self.rigged:
            return self._rig_layer()
        L = self.face_track.frame_at(self.t) if self.face_track else self.face
        blinking = self.blink_hold or self.t < self.blink_until
        if not blinking or not self.eyes or self.face_track:
            return L
        L = L.copy()
        for ex, ey, ew, eh in self.eyes:
            mid = ey + eh // 2
            line_col = None
            for yy in range(ey, ey + eh):
                for xx in range(ex, ex + ew):
                    ch, col = L.get(xx, yy)
                    if ch:
                        line_col = line_col or col
                    L.set(xx, yy, None)
            for xx in range(ex, ex + ew):
                L.set(xx, mid, "-", line_col or self.pose.feature_color)
        return L

    def _character_cells(self) -> list:
        """当前身体+脸的画布坐标格（用于解体特效）。"""
        bx, by = self.body_origin
        out = {}
        for x, y, ch, col in self._body_cell_list():
            out[(bx + x, by + y)] = (ch, col)
        fr = self.face_rect()
        if fr:
            L = self._face_layer_now()
            for y in range(L.h):
                for x in range(L.w):
                    ch = L.chars[y * L.w + x]
                    if ch is not None:
                        out[(fr[0] + x, fr[1] + y)] = (ch, L.colors[y * L.w + x])
        return list(out.items())

    def frame(self):
        """合成当前帧。返回 (chars, colors, W, H, (px, py))。"""
        t0 = time.perf_counter()
        W, H = self.W, self.H
        hidden = set()
        if self.scene:
            hidden.update(self.scene.op.hide)
        for e in self.effects:
            hidden.update(e.hides)
        if "background" in hidden:
            chars, colors = [CLEAR] * (W * H), [0] * (W * H)
        else:
            chars, colors = list(self.bg.chars), list(self.bg.colors)

        def put(x, y, ch, col):
            if 0 <= x < W and 0 <= y < H:
                i = y * W + x
                chars[i] = ch
                colors[i] = col

        bx, by = self.body_origin
        if "body" not in hidden:
            for x, y, ch, col in self._body_cell_list():
                xx, yy = bx + x, by + y
                if 0 <= xx < W and 0 <= yy < H:
                    i = yy * W + xx
                    chars[i] = ch
                    colors[i] = col
            for (anchor, x, y), (ch, col) in self.persistent["body"].items():
                ax, ay = self._anchor_xy(anchor)
                put(ax + x, ay + y, ch, col)
        for tr in self.region_tracks:
            if tr.op.layer == "body" and "body" not in hidden:
                self._draw_layer(tr.frame_at(self.t), tr.op, put)
        fr = self.face_rect()
        if fr and "face" not in hidden:
            L = self._face_layer_now()
            for y in range(L.h):
                for x in range(L.w):
                    ch = L.chars[y * L.w + x]
                    if ch is not None:
                        put(fr[0] + x, fr[1] + y, ch, L.colors[y * L.w + x])
        for tr in self.region_tracks:
            if tr.op.layer in ("face", "overlay"):
                self._draw_layer(tr.frame_at(self.t), tr.op, put)
        for (anchor, x, y), (ch, col) in self.persistent["overlay"].items():
            ax, ay = self._anchor_xy(anchor)
            put(ax + x, ay + y, ch, col)
        if "effects" not in hidden:
            cells: dict = {}
            ui: dict = {}
            for e in self.effects:
                e.draw(ui if isinstance(e, fx.Bubble) else cells, {"W": W, "H": H})
            for (x, y), (ch, col) in cells.items():
                put(x, y, ch, col)
            self.fx_cells = cells.keys() | ui.keys()     # 本帧特效画到的格子（夜色等后处理可以跳过它们）
        else:
            ui = {}
            self.fx_cells = set()
        if self.scene and self.scene.track:
            self._draw_layer(self.scene.track.frame_at(self.t), self.scene.op, put)
        for (x, y), (ch, col) in ui.items():
            put(x, y, ch, col)
        # 色调 / 透明度
        ta = self.params["tint_amount"]
        op = self.params["opacity"]
        if ta > 0.01 or op < 0.99:
            tc = self.tint_color
            for i, ch in enumerate(chars):
                if ch != CLEAR:
                    c = colors[i]
                    if ta > 0.01:
                        c = mix(c, tc, ta) & 0x00FFFFFF | (c & 0xFF000000)
                    if op < 0.99:
                        c = with_alpha(c, op)
                    colors[i] = c
        for e in self.effects:
            e.post(chars, colors, W, H)
        # 像素级漂浮 / 摆动 / 抖动
        sp = self.params["speed"]
        px = math.sin(self.t * sp * 2 * math.pi / 5.3) * self.s.sway_px * self.params["sway"]
        py = math.sin(self.t * sp * 2 * math.pi / 3.4) * self.s.bob_px * self.params["bob"]
        sh = self.params["shake"]
        if sh > 0.01:
            px += self.rng.uniform(-sh, sh) * 1.5
            py += self.rng.uniform(-sh, sh) * 0.8
        for e in self.effects:
            dx, dy = e.px_offset()
            px += dx
            py += dy
        self.stats["frames"] += 1
        self.stats["compose_ms"] = (time.perf_counter() - t0) * 1000
        return chars, colors, W, H, (px, py)

    def _anchor_xy(self, anchor: str) -> tuple[int, int]:
        if anchor == "face":
            fr = self.face_rect()
            if fr:
                return fr[0], fr[1]
        if anchor in ("body", "face"):
            return self.body_origin
        return 0, 0

    def _draw_layer(self, L: Layer, op: Op, put) -> None:
        ax, ay = (0, 0) if op.type == "full_scene" else self._anchor_xy(op.anchor)
        ox, oy = ax + (0 if op.type == "full_scene" else op.x), ay + (0 if op.type == "full_scene" else op.y)
        for y in range(L.h):
            for x in range(L.w):
                ch = L.chars[y * L.w + x]
                if ch is not None:
                    put(ox + x, oy + y, ch, L.colors[y * L.w + x])

    # ------------------------------------------------------------------ 视觉状态
    def face_snapshot(self) -> dict:
        """当前面部的紧凑表示（行 + 调色板字母），作为下一次生成的起点。"""
        L = self.face
        palette: dict[int, str] = {}
        letters = "abcdefghijklmnopqrstuvwxyz"
        rows, crows = [], []
        for y in range(L.h):
            r, c = [], []
            for x in range(L.w):
                ch = L.chars[y * L.w + x]
                if ch is None:
                    r.append("`")
                    c.append(".")
                    continue
                r.append(" " if ch == CLEAR else ch)
                col = L.colors[y * L.w + x] & 0xF0F0F0 | 0xFF000000
                if col not in palette and len(palette) < len(letters):
                    palette[col] = letters[len(palette)]
                c.append(palette.get(col, "."))
            rows.append("".join(r))
            crows.append("".join(c))
        return {"rows": rows, "colors": crows, "palette": {v: color_hex(k) for k, v in palette.items()},
                "eyes": self.eyes}

    def visual_state(self) -> dict:
        f = self.pose.face
        active = []
        if self.expr_track:
            active.append("expression_anim")
        if self.face_track:
            active.append("face_anim")
        if self.region_tracks:
            active.append(f"region_anim x{len(self.region_tracks)}")
        if self.scene:
            active.append("full_scene")
        active += [type(e).__name__.lower() for e in self.effects]
        return {
            "character": self.char.display_name,
            "pose": self.pose.name,
            "poses": list(self.char.poses),
            "canvas": {"w": self.W, "h": self.H},
            "body": {"x": self.body_origin[0], "y": self.body_origin[1], "w": self.pose.w, "h": self.pose.h},
            "face_region": ({"w": f["w"], "h": f["h"], "rig": True} if self.rigged else
                            {"w": f["w"], "h": f["h"], "skin_char": self.pose.skin_char,
                             "skin_color": color_hex(self.pose.skin_color)}) if f else None,
            "face": (fr.describe(self.face_spec) if self.rigged else self.face_snapshot()) if f else None,
            "expression": self.expression,
            "recent_expressions": self.recent[-5:],
            "params": {k: round(v, 2) for k, v in self.params.items() if abs(v - PARAM_DEFAULTS[k]) > 0.01},
            "background": self.s.background,
            "active": active,
        }

    def export_state(self) -> dict:
        if self.rigged:
            return {"pose": self.pose.name, "face_spec": self.face_spec, "expression": self.expression,
                    "recent": self.recent, "background": self.s.background, "think": self.think_text}
        snap = self.face_snapshot() if self.pose.face else None
        return {"pose": self.pose.name, "face": snap, "expression": self.expression, "recent": self.recent,
                "background": self.s.background, "params": {k: self.params[k] for k in ("body_dx", "body_dy")},
                "think": self.think_text}

    def import_state(self, st: dict) -> None:
        try:
            if st.get("pose") in self.char.poses:
                self.set_pose(st["pose"], keep_face=False)
            snap = st.get("face")
            if self.rigged and isinstance(st.get("face_spec"), dict):
                self.face_spec = fr.normalize(st["face_spec"])
            elif snap and self.pose.face and not self.rigged and len(snap.get("rows", [])) == self.pose.face["h"]:
                pal = {k: int(v.lstrip("#"), 16) | 0xFF000000 for k, v in (snap.get("palette") or {}).items()}
                L = Layer("face", self.pose.face["w"], self.pose.face["h"])
                colors = [[pal.get(c) for c in row] for row in snap.get("colors", [])]
                L.apply_patch(0, 0, snap["rows"], colors, blank="clear", default_color=self.pose.feature_color)
                self.face = L
                self.eyes = [list(e) for e in snap.get("eyes") or self.pose.eyes]
            self.expression = st.get("expression") or self.expression
            self.recent = list(st.get("recent") or [])[-8:]
            if st.get("background"):
                self.set_background(st["background"])
            if st.get("think"):
                self.think_text = st["think"]
        except Exception:
            self.reset_face()
