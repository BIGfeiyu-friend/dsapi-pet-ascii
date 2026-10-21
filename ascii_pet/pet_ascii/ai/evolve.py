"""表情进化（后台，副 API）：回看最近的对话、表情的使用次数和用户的 👍/👎，让模型迭代表情库。

一次进化 = 设计（开思考，认真推敲）→ 照镜子复查（不开思考，便宜）：
1. 设计：模型输出 0~4 个动作 —— create 新建 / revise 修改 / merge 合并 / retire 淘汰
2. 复查：把新建、修改后的表情实际画出来的样子（mirror）交还给模型，不到位就再修一次

不占聊天时间。结果经同样的钳制、去重后写入自创表情库；每次修改都保留版本，可在测试台回滚。
对话素材来自字符画数据目录里的 dialogue.json，与桌宠的记忆模块无关。

设计输出解析失败（正文为空、被截断、不是 JSON）时：日志记下 finish_reason、token 用量和原文片段，
再不开思考补问一次“只输出一个 JSON 对象”；仍失败就返回带原因和 token 用量的错误报告。
"""
from __future__ import annotations

import json
import logging
import time

from .. import face_rig as fr
from ..protocol import ProtocolError, extract_json

log = logging.getLogger("pet.ascii")

SPEC_DOC = """表情规格（字段都可选）：
{{"eyes":{{"open":0~1.3, "smile":0~1(0.3~0.6 眯眼笑, ≥0.72 成^弧), "tilt":-1~1(整只眼的斜度:正=凶,负=委屈),
          "droop":-1~1(外眼角:正=往下耷拉,负=往上挑), "lower":0~1(下眼睑上抬=眯眼), "iris":0.5~1.3, "pupil":"{pupils}", "tear":0~1, "highlight":0~1.5}},
  "eye_l":{{...}}, "eye_r":{{...}}(单独改一只眼), "gaze":[x,y](-1~1),
  "brows":{{"y":-1~1, "angle":-1~1}}, "mouth":{{"shape":"{mouths}", "open":0~1, "width":0.4~1.6, "curve":-1~1, "skew":-1~1(歪嘴)}},
  "blush":0~1, "sweat":0~1, "gloom":0~1}}"""

SYSTEM = """你是桌宠「{name}」的表情设计师，负责让她的表情库随着相处不断迭代。你会看到：
- recent_dialogue：最近的对话（用户说的话、她的回复、当时用的表情标签）
- builtin_presets：内置表情（规格；用过几次 uses；用户的 likes 👍 / dislikes 👎；被嫌弃的还有 mirror）
- custom_presets：她自创的表情（适用情绪、来源、用过几次、多久没用、👍/👎、版本数、规格，
  以及 mirror 照镜子：looks 实际画出来的样子、closest 最像的已有表情、visible_diff 在字符画上和它有多少格不同）
- feedback_log：用户最近对表情的评价和当时的语境

请认真思考，然后输出 0~4 个动作（宁缺毋滥，表情库已经很好就返回空数组）：
- create：现有表情都表达不准的情绪 → {{"op":"create","name":"2~6个汉字","emotion":"适用情绪（一句话）","from":"最接近的内置预设","spec":{{表情规格}}}}
- revise：改进已有表情 → {{"op":"revise","name":"已有名字","reason":"为什么改","spec":{{只写要改的字段}}}}
- merge：两个自创表情太像（visible_diff 很小或情绪重叠）→ 把 A 并入 B：{{"op":"merge","name":"A","into":"B","reason":"…"}}
- retire：长期不用、被 👎、或照镜子发现根本看不出区别的自创表情 → {{"op":"retire","name":"…","reason":"…"}}

规则：
- 【变化要大胆】用户觉得以前的修改太保守。每个 create / revise 都要让字符画一眼看出不同：和原来（新建则是和最像的已有表情）相比，
  visible_diff 至少 {min_pct}%（参考：不同内置表情之间通常差 10~20%，你以前的修改只有 5~8%，太保守了）。不够的会被程序沿着你改的方向自动放大，放大后仍不够就不采用。
- 她的五官是画师手绘的差分连续变形出来的（v0.8）：睁闭程度、笑眼程度（不只是有 / 没有 ^）、外眼角 droop、下眼睑 lower、
  眼角斜度 tilt、左右眼不对称都能细调，眉毛也跟着眼睛变；嘴的张开、嘴宽、嘴角 curve、歪嘴 skew 同样连续，而且用的是画师的笔触。
  最显眼的仍是眼睛和视线 gaze，张开的嘴也很显眼；泪、汗、脸红、黑线只改变 1% 左右的格子，只能当点缀。
- 用户 👎 的表情优先改进（revise）；用户 👍 的不要淘汰、不要并掉，也尽量别改。
- 内置表情只能 revise，而且只能改 dislikes 多于 likes 的；其它内置表情不要动。
- visible_diff 低于 5% 说明在字符画上几乎分不出来，这样的表情要么改得更有特点，要么并掉。
- 眉毛画在深色刘海上几乎看不见，不要靠眉毛区分表情；眼型、瞳孔、嘴型、泪/汗/脸红/黑线才看得清。
- 动作里的 name / into 用表情的键名：内置表情用英文键名（如 sleepy），自创表情用它的中文名。
- 新名字不能与已有表情重复。

""" + SPEC_DOC + """

只输出一个 JSON 对象：{{"thoughts":"一两句话：你观察到了什么", "actions":[...]}}"""

CHECK_SYSTEM = """你是桌宠「{name}」的表情设计师。你刚刚设计或修改了下面这些表情，现在照镜子看它们实际画出来的样子：
mirror.looks 是画出来的样子，closest 是最像的已有表情，visible_diff 是在字符画上和它有多少格不同（低于 5% 几乎分不出来）。
对照每个表情想表达的情绪（intent），判断是否到位：到位写 ok；不到位给出修正（只写要改的字段）。
change 是这次修改在字符画上改变的格子比例，低于 {min_pct}% 也算不到位。带 problem 的条目是因为变化太小没被采用：
必须给出更大胆的修正（ok=false 并给 spec），优先改眼睛（睁闭、笑眼、droop、lower、tilt、瞳孔）和视线，其次是嘴的张开；泪、汗、脸红在字符画上几乎看不出来。

""" + SPEC_DOC + """

只输出一个 JSON 对象：{{"checks":[{{"name":"…","ok":true}}, {{"name":"…","ok":false,"why":"…","spec":{{…}}}}]}}"""

MAX_ACTIONS = 4
# 开思考时，思考的 token 也算在 max_tokens 里。以前是 4000，思考稍长就把正文挤没了（finish_reason=length）。
# 这只是上限，按实际用量计费；成功的一次进化总共约 5000~7700 token（含输入）。
DESIGN_MAX_TOKENS = 8000

REPAIR = ("上一次的回答无法使用（{why}）。请不要再写分析过程，直接只输出一个 JSON 对象："
          "{{\"thoughts\":\"一两句话\",\"actions\":[...]}}，不需要改动就让 actions 为空数组。")


def _fmt(tpl: str, name: str, min_change: float = 0.10) -> str:
    return tpl.format(name=name, pupils="|".join(fr.PUPILS[1:]), mouths="|".join(fr.MOUTHS[1:]),
                      min_pct=round((min_change or 0.10) * 100))


def _pct(change: dict) -> str:
    d = change.get("diff")
    if d is None:
        return ""
    return f"{d:.0%}" + (f"（自动放大×{change['gain']:g}）" if change.get("gain", 1) > 1 else "")


def build_messages(name: str, dialogue: list, context: dict, min_change: float = 0.10) -> list:
    req = {"recent_dialogue": dialogue[-12:], **context, "time": time.strftime("%Y-%m-%d %H:%M")}
    return [
        {"role": "system", "content": _fmt(SYSTEM, name, min_change)},
        {"role": "user", "content": json.dumps(req, ensure_ascii=False, separators=(",", ":"))},
    ]


def parse_result(text: str) -> tuple[str, list]:
    """解析设计结果 → (thoughts, actions)。兼容旧格式 {"presets":[...]}（全部当作 create）。"""
    obj = extract_json(text)
    if not any(k in obj for k in ("actions", "presets", "thoughts")):
        # 输出被截断时 extract_json 可能只捞到里面某个完整的动作或 spec，不能当成“没有改动”
        raise ProtocolError("输出不完整：没有 actions")
    raw = obj.get("actions")
    if not isinstance(raw, list):
        raw = [{**p, "op": "create"} for p in (obj.get("presets") or []) if isinstance(p, dict)]
    out = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        op = str(a.get("op") or "").lower()
        name = str(a.get("name") or "")[:12]
        reason = str(a.get("reason") or a.get("emotion") or "")[:60]
        if op in ("create", "revise"):
            if not isinstance(a.get("spec"), dict):
                continue
            spec = dict(a["spec"])
            if fr.is_preset(a.get("from")):
                spec["from" if op == "revise" else "preset"] = a["from"]
            out.append({"op": op, "name": name, "reason": reason, "spec": spec})
        elif op == "merge" and a.get("into"):
            out.append({"op": op, "name": name, "into": str(a["into"])[:12], "reason": reason})
        elif op == "retire":
            out.append({"op": op, "name": name, "reason": reason})
        if len(out) >= MAX_ACTIONS:
            break
    return str(obj.get("thoughts") or "")[:200], out


def apply_actions(store, actions: list, by: str = "evolve") -> list:
    results = []
    creates = 0
    for a in actions:
        op, name = a["op"], a["name"]
        if op == "create":
            if creates >= 3:
                continue
            creates += 1
            final, status = store.add(name, a["spec"], origin=by, note=a["reason"])
            detail = a["reason"]
        elif op == "revise":
            final = store.resolve(name)
            status, detail = store.revise(name, a["spec"], by=by, reason=a["reason"])
        elif op == "merge":
            ok, detail = store.merge(name, a["into"], reason=a["reason"], by=by)
            final, status = (store.resolve(a["into"]), "merged") if ok else (name, "refused")
        else:
            ok, detail = store.retire(name, reason=a["reason"], by=by)
            final, status = name, ("retired" if ok else "refused")
        entry = {"op": op, "name": name, "final": final, "status": status, "detail": detail,
                 "emotion": a.get("reason", "")}
        if status in ("new", "updated", "revised"):
            entry["change"] = _pct(store.change_of(final))
        results.append(entry)
    return results


def _current_spec(store, name: str):
    if name in store.items:
        return store.items[name]["spec"]
    if name in fr.PRESETS:
        return fr.normalize({"preset": name})
    return None


def parse_checks(text: str) -> list:
    obj = extract_json(text)
    if "checks" not in obj:
        raise ProtocolError("输出不完整：没有 checks")
    out = []
    for c in (obj.get("checks") or [])[:MAX_ACTIONS]:
        if isinstance(c, dict) and c.get("name"):
            out.append({"name": str(c["name"])[:12], "ok": bool(c.get("ok", True)),
                        "why": str(c.get("why") or "")[:60], "spec": c.get("spec") if isinstance(c.get("spec"), dict) else None})
    return out


def _call(client, messages, max_tokens, timeout, extra):
    try:
        return client.complete(messages, max_tokens=max_tokens, temperature=0.9, timeout=timeout, extra=extra)
    except TypeError:          # Mock / 旧客户端不支持 extra
        return client.complete(messages, max_tokens=max_tokens, temperature=0.9, timeout=timeout)


def _add_usage(total: dict, u: dict) -> None:
    for k, v in (u or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


def _why(out: dict, exc) -> str:
    """解析失败的原因（日志、补问、报错共用）。"""
    empty = not str(out.get("text") or "").strip()
    if out.get("finish_reason") == "length":
        return "思考用完了长度上限，没有输出正文" if empty else "输出超过长度上限被截断"
    return "输出为空" if empty else str(exc)


def _diag(out: dict) -> str:
    """排查用：finish_reason、token 用量、原文片段（≤200 字，取头尾：截断时尾巴最能说明问题）。"""
    u = out.get("usage") or {}
    think = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    raw = str(out.get("text") or "")
    snip = " ".join(raw.split())
    if len(snip) > 200:
        snip = snip[:120] + "…" + snip[-79:]
    return (f"finish_reason={out.get('finish_reason') or '?'} token=输入{u.get('prompt_tokens', '?')}"
            f"/输出{u.get('completion_tokens', '?')}" + (f"（思考{think}）" if think is not None else "")
            + f" 原文{len(raw)}字「{snip}」")


def repair_messages(messages: list, bad_output: str, why: str) -> list:
    """补问：只要一个 JSON 对象。原输出为空时不附空的 assistant 消息，把要求接在原来的用户消息后面。"""
    note = REPAIR.format(why=why)
    if str(bad_output or "").strip():
        return messages + [{"role": "assistant", "content": bad_output[:3000]}, {"role": "user", "content": note}]
    return messages[:-1] + [{"role": "user", "content": messages[-1]["content"] + "\n\n" + note}]


def _design(client, messages, max_tokens, timeout, thinking, usage) -> tuple:
    """设计请求 → (thoughts, actions, model, 补问原因)。解析失败时记日志、不开思考补问一次；仍失败抛 ProtocolError。"""
    extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"} if thinking else None
    out = _call(client, messages, max_tokens, timeout, extra)
    _add_usage(usage, out.get("usage"))
    try:
        return (*parse_result(out["text"]), out.get("model", ""), "")
    except ProtocolError as exc:
        why = _why(out, exc)
        log.warning("字符画：表情进化输出无法解析（%s），不开思考补问一次；%s", why, _diag(out))
    out = _call(client, repair_messages(messages, out["text"], why), max_tokens, timeout,
                {"thinking": {"type": "disabled"}} if thinking else None)
    _add_usage(usage, out.get("usage"))
    try:
        return (*parse_result(out["text"]), out.get("model", ""), why)
    except ProtocolError as exc:
        why2 = _why(out, exc)
        log.warning("字符画：表情进化补问后仍无法解析（%s）；%s", why2, _diag(out))
        raise ProtocolError(f"{why}；补问后仍失败：{why2}") from None


def run(client, name: str, dialogue: list, store, max_tokens: int = DESIGN_MAX_TOKENS, timeout: float = 120.0,
        thinking: bool = True, rig=None, check: bool = False) -> dict:
    """同步执行一次进化（在后台线程里调用）。返回报告字典；设计失败时是 {"error", "usage", "elapsed"}。"""
    min_change = getattr(store, "min_change", 0.0)
    messages = build_messages(name, dialogue, store.evolve_context(rig=rig), min_change)
    t0 = time.perf_counter()
    usage: dict = {}
    try:
        thoughts, actions, model, repaired = _design(client, messages, max_tokens, timeout, thinking, usage)
    except Exception as exc:          # 失败也带上已花掉的 token，照样计入预算
        return {"error": str(exc)[:200], "usage": usage, "elapsed": round(time.perf_counter() - t0, 2)}
    results = apply_actions(store, actions)
    report = {"thoughts": thoughts, "results": results, "checks": [], "usage": usage, "model": model}
    if repaired:
        report["repaired"] = repaired

    # 照镜子复查：新建/修改成功的表情看看实际画出来的样子是否到位；变化太小没被采用的，请她改得更大胆
    changed = [r for r in results if r["status"] in ("new", "updated", "revised", "too_small")]
    if check and changed:
        items = []
        for r in changed:
            spec = _current_spec(store, r["final"])
            if spec is None:
                continue
            it = {"name": r["final"], "intent": r.get("emotion", ""), "spec": fr.describe(spec),
                  "mirror": store.mirror(spec, exclude=r["final"], rig=rig)}
            if r["status"] == "too_small":
                it["problem"] = r["detail"]
            elif r.get("change"):
                it["change"] = r["change"]
            items.append(it)
        if items:
            msgs = [{"role": "system", "content": _fmt(CHECK_SYSTEM, name, min_change)},
                    {"role": "user", "content": json.dumps({"designs": items}, ensure_ascii=False, separators=(",", ":"))}]
            try:
                out2 = _call(client, msgs, 1200, timeout, {"thinking": {"type": "disabled"}} if thinking else None)
                _add_usage(usage, out2.get("usage"))
                names = {it["name"] for it in items}
                for c in parse_checks(out2["text"]):
                    if c["name"] not in names:
                        continue
                    entry = {"name": c["name"], "ok": c["ok"], "why": c["why"]}
                    if not c["ok"] and c["spec"]:
                        status, _ = store.revise(c["name"], c["spec"], by="mirror", reason="照镜子后修改：" + c["why"])
                        entry["status"] = status
                        if status == "revised":
                            entry["change"] = _pct(store.change_of(store.resolve(c["name"])))
                    report["checks"].append(entry)
            except Exception as exc:          # 复查失败不影响已经完成的设计
                report["check_error"] = str(exc)[:120]
    report["elapsed"] = round(time.perf_counter() - t0, 2)
    return report
