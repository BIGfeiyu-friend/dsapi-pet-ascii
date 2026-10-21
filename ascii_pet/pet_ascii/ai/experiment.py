"""自主视觉实验（v0.7，后台）：提出设计意图 → 创建候选形态 → 本地真实渲染 → 获取实际反馈 → 自己决定修改 / 采用 / 放弃。

一次实验最多 max_iters 轮（默认 3），受每日次数和 token 预算约束，可随时取消；全部在后台线程里运行。
- 候选设计（新组件、骨架操作、表情规格、自由演出）先全部经过数据校验和钳制，绝不执行模型生成的代码
- 本地检查（visual_check / 演出检查）给出确定性的错误和指标；有确定性错误时不调用视觉模型，先让她自己修
- 全新组件、骨架改造、自由演出这类高风险变化，才在后台请视觉模型看截图（可选，有预算，结果只作参考）
- 她看到实际检查结果（和视觉评价）后自己决定；有确定性错误时程序不允许采用
- 采用：组件进正式组件库、表情进表情库（带风格标签）、演出进演出库；放弃：失败原因写进创作记忆
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time

from .. import face_rig as fr
from .. import parts as pt
from .. import visual_check as vc
from ..creative import KIND_CN
from ..parts import STYLE_CN
from ..protocol import ProtocolError, extract_json, parse_animation

log = logging.getLogger("pet.ascii")

KIND_HINT = {
    "invent": "发明 1~2 个全新的部件（眼睛、嘴、眉、腮、额头装饰或完全非传统的五官都可以），再用它们组成一个新表情。",
    "skeleton": "改造脸的结构：移动 / 缩放 / 旋转 / 隐藏 / 新增节点（可以配合新部件），做出默认骨架做不到的表情。",
    "remix": "挑一个你以前的部件或表情（见 existing_components / my_expressions），在它的基础上做出明显不同的新版本。",
    "scene": "暂时脱离角色骨架，用整个画布做一段自由的字符画演出（字符形态、粒子、抽象动画），作为特别时刻的表演。",
}

SPEC_DOC = ("{\"preset\":\"起点预设(可选)\", \"eyes\":{\"open\":0~1.3,\"smile\":0~1,\"tilt\":-1~1,\"droop\":-1~1(外眼角下垂/上挑),"
            "\"lower\":0~1(下眼睑上抬),\"iris\":0.5~1.3,"
            "\"pupil\":\"round|heart|star|spiral|dot|x\",\"tear\":0~1,\"highlight\":0~1.5}, \"eye_l\"/\"eye_r\":{…},"
            " \"gaze\":[x,y], \"brows\":{\"y\":-1~1,\"angle\":-1~1}, \"mouth\":{\"shape\":\"normal|line|wavy|cat|pout|o|triangle|teeth|tongue\","
            "\"open\":0~1,\"width\":0.4~1.6,\"curve\":-1~1,\"skew\":-1~1(歪嘴)}, \"blush\":0~1, \"sweat\":0~1, \"gloom\":0~1, \"parts\", \"nodes\", \"extra\"}"
            "  内置的眼睛、眉毛、嘴是画师手绘差分的连续变形：上面这些参数都是连续的，而且保持画师的笔触")

DESIGN_SYSTEM = """你是桌宠「{name}」自己——一个会画画的 AI。现在是你的“自主视觉实验”时间：你可以发明新的五官部件、改造脸的结构，
或者暂时脱离角色骨架做一段自由的字符画演出。目标不是调参数，而是**发明**属于你自己的视觉表达方式。

这次的方向：风格「{style_cn}」（{style}），类型「{kind_cn}」。{kind_hint}

## 你的脸（骨架节点；x,y 是在脸框里 0~1 的位置，w/h 是半宽/半高占脸框的比例）
{nodes}
脸部字符区域只有约 {fw}×{fh} 格（每格约 5×8 像素）：部件要够大、线要够粗，太碎的细节会被字符化抹掉。
深色刘海盖住了脸框上部（大约 y<0.3），画在那里会和头发混在一起；皮肤主要在 y=0.3~0.95。

## 发明部件
{prims}

## 改造骨架
表情规格里可选："parts":{{"eye_l":"部件名"|"none"|"builtin"}}（节点：{core}），
"nodes":{{"mouth":{{"dx":-0.5~0.5,"dy":-0.5~0.5,"sx":0.3~3,"sy":0.3~3,"rot":-90~90}}}}（dx/dy 按脸框宽高的比例），
"extra":[{{"id":"英文小写","x":0~1,"y":0~1,"w":0.02~0.6,"h":0.02~0.6,"part":"部件名","rot":度,"flip":false}}]（最多 8 个新节点）。
也可以写骨架操作（按顺序执行，越界会被钳制）："skeleton_ops":[{{"op":"move","node":"eye_l","dx":0.03,"dy":0}},
{{"op":"scale","node":"mouth","s":1.4}}, {{"op":"rotate","node":"eye_r","deg":15}}, {{"op":"replace","node":"mouth","part":"部件名"}},
{{"op":"add","id":"third_eye","x":0.5,"y":0.35,"w":0.05,"h":0.06,"part":"部件名"}}, {{"op":"remove","node":"brow_l"}},
{{"op":"group","nodes":["eye_l","eye_r"],"dy":0.02,"s":1.2,"rot":0}}, {{"op":"reset","node":"mouth"}}]

## 表情规格（原来的参数照样可用，和部件一起组合）
{spec_doc}

## 自由演出（只在类型是“自由演出”时写 scene）
协议 JSON，例如 {{"mode":3,"intent":"…","ops":[{{"type":"full_scene","frames":[{{"rows":["每行≤{cw}个字符",…],"color":"#hex","ms":300}},…],
"rules":[{{"effect":"particles","chars":"*+","color":"#hex","count":40}}],"hide":["body","face"],"then":"restore"}},
{{"type":"effect","effect":"sparkle|rain|ripple|glitch|dissolve|text|symbols|particles","duration_ms":1500,"chars":"…","color":"#hex"}}]}}
画布 {cw}×{ch} 格，整段 1~10 秒，结束后恢复原样；rows 只用 ASCII 字符。

## 参考资料（在用户消息里）
- existing_components：已有的正式部件（可直接在 parts / extra 里用名字）；my_expressions：已有的自创表情
- creative_memory：你最近的实验（结果、失败原因、视觉评价）和你写给自己的创作心得；user_taste：用户按风格给的 👍/👎
- 不要重复已有的设计；失败过的方向换个思路，不要原样再来

只输出一个 JSON 对象：
{{"intent":"想表达的情绪或感觉（一句话）","name":"作品名（2~6个汉字）","emotion":"适合在什么情绪下用",
 "components":[{{"name":"部件名(2~6字)", …部件字段}}], "skeleton_ops":[…], "spec":{{表情规格}}, "scene":{{只在自由演出时}},
 "notes":"设计思路（一两句）"}}"""

REVIEW_SYSTEM = """你是桌宠「{name}」。你刚才的视觉实验作品已经在本地**真实渲染**出来了，下面是对实际渲染结果的检查：
- local_check：本地程序的确定性检查。errors 必须解决才能采用；warnings 自己判断；parts.visible_cells 是该部件在字符画上实际改变的格子数
- looks：按渲染数据直接翻译的样子（不是想象）
- vision：另一个看图模型对截图的评价（如果有）。它只是参考，可能看错，听不听由你决定
- validation：你的设计里被程序忽略或钳制的部分
- round：第几轮 / 最多几轮；final=true 表示这是最后一轮：只能 adopt 或 abandon（这时写 revise 会被当作放弃），
  没有确定性错误、整体已经表达出意图的作品就采用，细节以后还可以在“重做旧作”里继续改
决定：
- adopt：满意 → 作品进入正式库（errors 不为空时不能采用）
- revise：给出修改（只写要改的：components 按名字整体替换；skeleton_ops 给完整的新列表；spec 只写要改的字段；scene 整体替换）
- abandon：这个方向不值得继续 → 写清楚原因（会记进创作记忆，以后避开）

""" + "部件格式提醒：{prims_short}\n" + """
只输出一个 JSON 对象：{{"decision":"adopt|revise|abandon","reason":"…","changes":{{…}},"self_note":"可选：写给以后的自己的一句创作心得"}}"""

DESIGN_MAX_TOKENS = 9000
REVIEW_MAX_TOKENS = 3000


# ---------------------------------------------------------------------- 候选设计
class Candidate:
    """一次实验里正在打磨的候选设计（原始数据，构建时才校验）。"""

    def __init__(self, design: dict):
        self.intent = str(design.get("intent") or "")[:100]
        self.name = str(design.get("name") or "")[:12]
        self.emotion = str(design.get("emotion") or self.intent)[:60]
        self.notes = str(design.get("notes") or "")[:120]
        self.components = {}
        for c in design.get("components") or []:
            if isinstance(c, dict) and c.get("name"):
                self.components[str(c["name"])[:12]] = c
        self.ops = [o for o in (design.get("skeleton_ops") or []) if isinstance(o, dict)][:24]
        self.spec = dict(design.get("spec") or {}) if isinstance(design.get("spec"), dict) else {}
        self.scene = design.get("scene") if isinstance(design.get("scene"), dict) else None

    def apply(self, changes: dict) -> None:
        if not isinstance(changes, dict):
            return
        for c in changes.get("components") or []:
            if isinstance(c, dict) and c.get("name"):
                self.components[str(c["name"])[:12]] = c
        if isinstance(changes.get("skeleton_ops"), list):
            self.ops = [o for o in changes["skeleton_ops"] if isinstance(o, dict)][:24]
        if isinstance(changes.get("spec"), dict):
            for k, v in changes["spec"].items():
                if k in ("parts", "nodes") and isinstance(v, dict) and isinstance(self.spec.get(k), dict):
                    self.spec[k] = {**self.spec[k], **v}
                else:
                    self.spec[k] = v
        if isinstance(changes.get("scene"), dict):
            self.scene = changes["scene"]
        for k in ("name", "emotion", "intent"):
            if changes.get(k):
                setattr(self, k, str(changes[k])[:100 if k == "intent" else 12 if k == "name" else 60])

    def summary(self) -> dict:
        out = {"intent": self.intent, "name": self.name}
        if self.components:
            out["components"] = list(self.components.values())
        if self.ops:
            out["skeleton_ops"] = self.ops
        if self.spec:
            out["spec"] = self.spec
        if self.scene:
            out["scene"] = self.scene
        return out


def build(cand: Candidate, rig, roles, engine_ctx=None) -> dict:
    """校验候选设计：组件 → 候选组件表；骨架操作 → 字段；表情规格 → 规整；演出 → 协议校验。"""
    warnings: list = []
    lib = {}
    for name, raw in cand.components.items():
        if not pt.valid_name(name):
            warnings.append(f"部件名「{name}」不合法，已忽略")
            continue
        w: list = []
        comp = pt.clean_component(raw, roles, w)
        warnings += [f"{name}：{x}" for x in w]
        if comp is not None:
            lib[name] = comp
    out = {"lib": lib, "warnings": warnings, "spec": None, "scene": None, "fields": {}}
    if cand.scene is not None:
        try:
            anim = parse_animation(cand.scene, engine_ctx)
            out["scene"] = cand.scene
            out["warnings"] += anim.warnings[:6]
        except ProtocolError as exc:
            out["warnings"].append(f"演出无效：{exc}")
    if cand.spec or cand.ops or lib:
        raw = dict(cand.spec)
        if cand.ops:
            base = pt.clean_rig_fields(raw)
            w2: list = []
            fields = pt.apply_ops(base, cand.ops, rig.nodes, w2)
            out["warnings"] += w2
            for k in pt.RIG_KEYS:
                raw.pop(k, None)
            raw.update(fields)
        out["spec"] = fr.normalize(raw)
        out["fields"] = pt.rig_fields(out["spec"])
    return out


# ---------------------------------------------------------------------- 自由演出的本地检查
def scene_check(engine_factory, scene: dict, snapshots: bool = True) -> dict:
    """在一个离线引擎里把演出完整跑一遍：时长、画面覆盖率、是否恢复原样，并截几帧图。"""
    errors, warnings = [], []
    eng = engine_factory()
    eng.tick(0.0)
    base_chars, _, W, H, _ = eng.frame()
    anim = parse_animation(scene, eng.context())
    eng.apply(anim)
    t0 = eng.t
    total = max(0.1, eng.current_end - t0)
    if total > 12:
        errors.append(f"演出太长（{total:.1f} 秒，上限 10 秒）")
    elif total < 0.8:
        warnings.append(f"演出太短（{total:.1f} 秒）")
    samples, coverage, changed = [], [], []
    steps = int(min(total, 12) / 0.1) + 1
    picks = {int(steps * 0.3), int(steps * 0.65)}
    for k in range(steps):
        eng.tick(0.1)
        chars, colors, w, h, _ = eng.frame()
        vis = sum(1 for c in chars if c not in (" ", "", None) and c is not None)
        coverage.append(vis / max(1, w * h))
        changed.append(sum(1 for a, b in zip(chars, base_chars) if a != b) / max(1, w * h))
        if k in picks and snapshots:
            samples.append(_grid(chars, colors, w, h))
    for _ in range(10):
        eng.tick(0.1)
    end_chars = eng.frame()[0]
    residue = sum(1 for a, b in zip(end_chars, base_chars) if a != b) / max(1, W * H)
    peak_change = max(changed) if changed else 0.0
    if peak_change < 0.05:
        errors.append(f"演出几乎没有改变画面（最多只改变了 {peak_change:.0%} 的格子）")
    if residue > 0.15:
        warnings.append(f"演出结束后画面没有完全恢复（{residue:.0%} 的格子还不一样）")
    out = {"ok": not errors, "errors": errors, "warnings": warnings + anim.warnings[:4],
           "metrics": {"duration_s": round(total, 2), "peak_change": round(peak_change, 3),
                       "avg_coverage": round(sum(coverage) / max(1, len(coverage)), 3), "residue": round(residue, 3)},
           "looks": f"全画布演出 {total:.1f} 秒，最多改变 {peak_change:.0%} 的画面"}
    if snapshots and samples:
        out["images"] = {"ascii_focus": vc.ascii_png(*samples[0], None, cell=(5, 9)),
                         "ascii_face": vc.ascii_png(*samples[-1], None, cell=(4, 7))}
    return out


def _grid(chars, colors, w, h):
    cg = [[chars[y * w + x] for x in range(w)] for y in range(h)]
    kg = []
    for y in range(h):
        row = []
        for x in range(w):
            c = colors[y * w + x]
            row.append(((c >> 16) & 255, (c >> 8) & 255, c & 255) if c and chars[y * w + x] not in (" ", "", None) else None)
        kg.append(row)
    return cg, kg


# ---------------------------------------------------------------------- 模型调用
def _call(client, messages, max_tokens, timeout, extra):
    try:
        return client.complete(messages, max_tokens=max_tokens, temperature=1.0, timeout=timeout, extra=extra)
    except TypeError:
        return client.complete(messages, max_tokens=max_tokens, temperature=1.0, timeout=timeout)


def _add_usage(total: dict, u: dict) -> None:
    for k, v in (u or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


def _ask_json(client, messages, max_tokens, timeout, thinking, usage, need: str) -> dict:
    extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"} if thinking else {"thinking": {"type": "disabled"}}
    out = _call(client, messages, max_tokens, timeout, extra)
    _add_usage(usage, out.get("usage"))
    try:
        obj = extract_json(out["text"])
        if need in obj:
            return obj
        raise ProtocolError(f"输出里没有 {need}")
    except ProtocolError as exc:
        why = "输出为空" if not str(out.get("text") or "").strip() else str(exc)
        log.warning("字符画·创作实验：输出无法解析（%s），不开思考补问一次", why)
    msgs = messages + ([{"role": "assistant", "content": out["text"][:3000]}] if str(out.get("text") or "").strip() else []) + \
        [{"role": "user", "content": f"上一次的回答无法使用（{why}）。不要写分析过程，直接只输出要求的那一个 JSON 对象。"}]
    out = _call(client, msgs, max_tokens, timeout, {"thinking": {"type": "disabled"}})
    _add_usage(usage, out.get("usage"))
    obj = extract_json(out["text"])
    if need not in obj:
        raise ProtocolError(f"补问后仍没有 {need}")
    return obj


def design_messages(ctx, target: dict) -> list:
    rig = ctx.rig
    sys_text = DESIGN_SYSTEM.format(
        name=ctx.name, style=target["style"], style_cn=STYLE_CN.get(target["style"], target["style"]),
        kind_cn=KIND_CN.get(target["kind"], target["kind"]), kind_hint=KIND_HINT.get(target["kind"], ""),
        nodes=json.dumps(pt.node_table(rig), ensure_ascii=False, separators=(",", ":")), fw=rig.fw, fh=rig.fh,
        prims=pt.primitives_doc(), core="|".join(pt.CORE_NODES), spec_doc=SPEC_DOC, cw=ctx.canvas[0], ch=ctx.canvas[1])
    store = ctx.store
    mine = []
    for n, it in list(getattr(store, "items", {}).items())[-16:]:
        mine.append({"name": n, "emotion": it.get("note", ""), **({"style": it["style"]} if it.get("style") else {}),
                     "uses": it.get("uses", 0), "likes": it.get("likes", 0), "dislikes": it.get("dislikes", 0)})
    user = {"target": target, "existing_components": ctx.parts_lib.summary(16), "my_expressions": mine,
            "creative_memory": ctx.memory.context(), "user_taste": ctx.memory.user_style_votes(store, ctx.parts_lib),
            "time": time.strftime("%Y-%m-%d %H:%M")}
    if target["kind"] == "remix":                 # 可以重做的旧作：最近的部件（active() 已经是 名字 → 规格）和自创表情
        pool = [{"name": n, "kind": "component", "spec": spec} for n, spec in list(ctx.parts_lib.active().items())[-6:]]
        pool += [{"name": n, "kind": "expression", "spec": fr.describe(it["spec"])}
                 for n, it in list(getattr(store, "items", {}).items())[-4:] if it.get("spec")]
        user["remix_pool"] = pool
    return [{"role": "system", "content": sys_text},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, separators=(",", ":"))}]


def review_messages(ctx, cand: Candidate, local: dict, vision: dict | None, validation: list, rnd: int, max_iters: int) -> list:
    sys_text = REVIEW_SYSTEM.format(name=ctx.name, prims_short="图元 ellipse/line/poly/arc/rect/star/heart/dots，坐标 -1~1，"
                                                              "线宽 w≥0.6 才看得清；节点 " + "|".join(pt.CORE_NODES))
    payload = {"design": cand.summary(), "local_check": vc.summary(local) if "parts" in local else
               {k: local.get(k) for k in ("ok", "errors", "warnings", "metrics")},
               "looks": local.get("mirror") or local.get("looks", ""), "validation": validation[:8],
               "round": f"{rnd}/{max_iters}", "final": rnd >= max_iters}
    if vision:
        payload["vision"] = {k: vision.get(k) for k in ("reads_as", "intent_match", "clarity", "appeal", "problems", "suggestions")}
    return [{"role": "system", "content": sys_text},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]


# ---------------------------------------------------------------------- 主流程
class ExperimentContext:
    def __init__(self, name, rig, store, parts_lib, memory, vision=None, engine_factory=None, canvas=(110, 60),
                 metrics=None, vision_wait_s: float = 75.0):
        self.name, self.rig, self.store, self.parts_lib, self.memory = name, rig, store, parts_lib, memory
        self.vision, self.engine_factory, self.canvas, self.metrics = vision, engine_factory, canvas, metrics
        self.vision_wait_s = vision_wait_s


def _quality(local: dict, vision: dict | None) -> float:
    q = 1.0 - 0.12 * len(local.get("warnings", []))
    if local.get("errors"):
        q -= 0.5
    q = max(0.0, min(1.0, q))
    if vision and vision.get("score") is not None:
        q = 0.5 * q + 0.5 * vision["score"]
    return round(q, 3)


def run(client, ctx: ExperimentContext, target: dict, max_iters: int = 3, thinking: bool = True, timeout: float = 150.0,
        cancel: threading.Event | None = None) -> dict:
    """同步执行一次创作实验（后台线程里调用）。返回报告字典。"""
    cancel = cancel or threading.Event()
    t0 = time.perf_counter()
    usage: dict = {}
    mem = ctx.memory
    exp = mem.start(target)
    roles = tuple(ctx.rig.col)
    report = {"id": exp["id"], "target": target, "rounds": [], "outcome": "", "name": "", "usage": usage}

    def finish(outcome, failure="", name="", quality=None, adopted=None, intent=""):
        elapsed = time.perf_counter() - t0
        mem.finish(exp, outcome, name=name, failure=failure, intent=intent, usage=usage, elapsed=elapsed,
                   quality=quality, adopted=adopted)
        mem.spend(int(usage.get("total_tokens") or 0))
        report.update(outcome=outcome, failure=failure, name=name, elapsed=round(elapsed, 1), quality=quality,
                      adopted=adopted or {})
        if ctx.metrics is not None:
            ctx.metrics.record("创作实验（后台）", elapsed * 1000, f"{outcome} {name}".strip())
        return report

    try:
        design = _ask_json(client, design_messages(ctx, target), DESIGN_MAX_TOKENS, timeout, thinking, usage, "intent")
    except Exception as exc:
        return finish("error", f"设计请求失败：{str(exc)[:100]}")
    cand = Candidate(design)
    engine_ctx = None
    if ctx.engine_factory is not None and (target["kind"] == "scene" or cand.scene):
        try:
            engine_ctx = ctx.engine_factory().context()
        except Exception:
            engine_ctx = None
    for rnd in range(1, max_iters + 1):
        if cancel.is_set():
            return finish("cancelled", "被取消", intent=cand.intent)
        built = build(cand, ctx.rig, roles, engine_ctx)
        is_scene = built["scene"] is not None and (target["kind"] == "scene" or built["spec"] is None)
        if is_scene:
            try:
                local = scene_check(ctx.engine_factory, built["scene"], snapshots=True)
            except Exception as exc:
                local = {"ok": False, "errors": [f"演出无法运行：{str(exc)[:80]}"], "warnings": [], "metrics": {}}
        elif built["spec"] is not None:
            focus = None
            local = vc.analyze(ctx.rig, built["spec"], parts=built["lib"], focus=focus, snapshots=True,
                               intended_asym="不对称" in cand.intent)
        else:
            local = {"ok": False, "errors": ["设计里既没有可用的表情规格，也没有可用的演出"], "warnings": [], "metrics": {}}
        high_risk = bool(built["lib"] or built["fields"].get("nodes") or built["fields"].get("extra") or is_scene)
        vision_res = None
        if high_risk and local.get("ok") and ctx.vision is not None and local.get("images") and not cancel.is_set():
            idx = len(exp["iterations"])
            task = {"images": local["images"], "intent": cand.intent, "style": target["style"],
                    "kind": "scene" if is_scene else "face",
                    "changes": cand.notes or json.dumps({k: list(v) if isinstance(v, dict) else v
                                                         for k, v in built["fields"].items()}, ensure_ascii=False)[:300],
                    "local": {k: local.get(k) for k in ("errors", "warnings", "metrics")}}

            def on_done(job, exp_id=exp["id"], i=idx):
                if job.result:
                    mem.attach_vision(exp_id, i, job.result)
                    mem.add_vision({"exp": exp_id, "round": i + 1, **{k: job.result.get(k) for k in
                                                                       ("summary", "score", "elapsed", "tokens", "time")}})
                    for name in built["lib"]:
                        ctx.parts_lib.add_feedback(name, {"summary": job.result.get("summary", ""),
                                                          "score": job.result.get("score"), "time": job.result.get("time")})

            job = ctx.vision.submit(task, on_done=on_done)
            if job is not None:
                waited = 0.0
                while waited < ctx.vision_wait_s and not job.event.is_set() and not cancel.is_set():
                    job.event.wait(0.5)
                    waited += 0.5
                vision_res = job.result
                if vision_res is None and not job.event.is_set():
                    log.info("字符画·创作实验：视觉评价 %.0f 秒内没回来，先按本地检查继续（结果到达后会写进记忆）", waited)
        entry = {"round": rnd, "design": cand.summary(), "validation": built["warnings"][:8],
                 "local_errors": local.get("errors", []), "local_warnings": local.get("warnings", [])[:8],
                 "metrics": local.get("metrics", {}), "looks": local.get("mirror") or local.get("looks", "")}
        if vision_res:
            entry["vision"] = {k: vision_res.get(k) for k in ("summary", "score", "reads_as", "problems", "suggestions")}
        mem.add_iteration(exp, entry)
        round_rep = {"round": rnd, "errors": entry["local_errors"], "warnings": entry["local_warnings"],
                     "vision": (vision_res or {}).get("summary", "")}
        report["rounds"].append(round_rep)
        if cancel.is_set():
            return finish("cancelled", "被取消", intent=cand.intent)
        try:
            rev = _ask_json(client, review_messages(ctx, cand, local, vision_res, built["warnings"], rnd, max_iters),
                            REVIEW_MAX_TOKENS, timeout, False, usage, "decision")
        except Exception as exc:
            return finish("error", f"复查请求失败：{str(exc)[:100]}", intent=cand.intent)
        decision = str(rev.get("decision") or "").lower()
        reason = str(rev.get("reason") or "")[:120]
        round_rep.update(decision=decision, reason=reason)
        entry.update(decision=decision, reason=reason)
        mem.save()
        if rev.get("self_note"):
            mem.add_note(str(rev["self_note"]))
        if decision == "adopt" and local.get("errors"):
            if rnd < max_iters:
                decision = "revise"
                round_rep["overridden"] = "还有确定性错误，不能采用，继续修改"
            else:
                return finish("abandoned", "到最后一轮仍有确定性错误：" + "；".join(local["errors"][:2]), intent=cand.intent)
        if decision == "adopt":
            adopted = _adopt(ctx, cand, built, target, local, vision_res)
            q = _quality(local, vision_res)
            return finish("adopted", name=adopted.get("expression") or adopted.get("scene") or cand.name, quality=q,
                          adopted=adopted, intent=cand.intent)
        if decision == "abandon":
            return finish("abandoned", reason or "她决定放弃这个方向", intent=cand.intent)
        if rnd >= max_iters:
            return finish("abandoned", f"{max_iters} 轮之后仍不满意：{reason}", intent=cand.intent)
        cand.apply(rev.get("changes") or {})
    return finish("abandoned", "达到最大轮数", intent=cand.intent)


def _adopt(ctx, cand: Candidate, built: dict, target: dict, local: dict, vision: dict | None) -> dict:
    """采用：组件 → 正式组件库；表情 → 表情库（带风格）；骨架改造 → 骨架方案；演出 → 演出库。"""
    out = {}
    check = {k: local.get(k) for k in ("warnings", "metrics")}
    used = set(pt.used_parts(built["spec"])) if built["spec"] else set(built["lib"])
    comps = []
    for name, comp in built["lib"].items():
        if name in used or not built["spec"]:
            final, status = ctx.parts_lib.adopt(name, comp, intent=cand.intent, report=check)
            comps.append({"name": final, "status": status})
            if vision and status in ("new", "revised"):            # 采用前到达的视觉评价，登记时一起记到组件上
                ctx.parts_lib.add_feedback(final, {"summary": vision.get("summary", ""), "score": vision.get("score"),
                                                   "time": vision.get("time")})
    if comps:
        out["components"] = comps
    if built["scene"] is not None and (target["kind"] == "scene" or built["spec"] is None):
        name, status = ctx.memory.add_scene(cand.name or f"演出{random.randint(10, 99)}", built["scene"], target["style"],
                                            cand.intent)
        out["scene"] = name
        out["scene_status"] = status
        return out
    if built["spec"] is not None:
        spec = dict(built["spec"])
        if target["kind"] == "skeleton" and built["fields"]:
            sk, st = ctx.parts_lib.save_skeleton(cand.name or "新骨架", built["fields"], intent=cand.intent)
            out["skeleton"] = sk
        final, status = ctx.store.add(cand.name, spec, origin="experiment", note=cand.emotion, style=target["style"])
        out["expression"] = final
        out["expression_status"] = status
        it = getattr(ctx.store, "items", {}).get(final)
        if it is not None:
            it["quality"] = _quality(local, vision)
    return out
