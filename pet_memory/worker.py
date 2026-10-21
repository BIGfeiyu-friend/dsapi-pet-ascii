"""后台记忆整理（副 API）：未整理的对话块 → L2 情节摘要 + L3 事实增删改。

完全异步，不阻塞聊天。多进程时通过数据库租约保证同一时间只有一个整理者。
失败按指数退避；同一块连续失败 3 次则用本地兜底摘要跳过，避免卡死后续整理。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from . import netfast
from .settings import read_app_config

log = logging.getLogger("pet.memory")

SYSTEM_PROMPT = (
    "你是桌宠「{name}」的记忆整理模块。你会读到一段{name}与用户的对话记录，"
    "请把它整理成可长期保存的记忆。只输出一个 JSON 对象，不要输出任何其它文字。"
)

USER_PROMPT = """【已有的长期事实（key: 内容）】
{facts}

【需要整理的对话】（本地时间）
{transcript}

请输出 JSON，字段如下：
{{
  "title": "8~16 字的标题",
  "summary": "80~200 字的第三人称摘要：发生了什么、用户说了哪些重要的话、{name}如何回应",
  "events": ["关键事件，每条一句，没有则为空数组"],
  "relationship": "用户与{name}之间关系或情绪的变化，没有则为空字符串",
  "commitments": ["双方的约定或承诺（谁、做什么、何时）"],
  "open_items": ["尚未完成、之后需要跟进的事项"],
  "keywords": ["5~10 个检索关键词：人名、物品、地点、话题"],
  "importance": 0.3,
  "facts": [
    {{"op": "add", "key": "user.name", "category": "profile", "content": "用户名叫……", "importance": 0.9}}
  ]
}}
规则：
1. importance 取 0~1：日常闲聊约 0.2，涉及个人信息、重要经历、强烈情绪或约定 0.6 以上。
2. facts 只记录长期有用、可复用的信息：用户身份/称呼、偏好与雷区、习惯、重要经历、人际关系、双方约定、角色扮演中确立的设定。一次性闲聊不要记。
3. key 用稳定的英文点分小写形式（如 user.name、user.preference.food、user.job、relationship.nickname、promise.xxx、world.xxx）。
   已有事实发生变化时用 "op":"update" 并沿用原 key；被用户否认或已失效时用 "op":"delete"。没有需要变更的事实就输出空数组。
4. category 取 profile / preference / relationship / commitment / event / world / other 之一。
5. content 写成一句完整、独立可读的陈述，称用户为“用户”，称桌宠为“{name}”。
6. 不要编造对话中没有的信息；不确定的不要记录。"""


class ConsolidationWorker:
    def __init__(self, mgr):
        self.mgr = mgr
        self.s = mgr.s
        self.store = mgr.store
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._event = threading.Event()
        self._fails = 0
        self._chunk_fails: dict[str, int] = {}
        self._backoff_until = 0.0
        self._resolved_provider: dict | None = None
        self._resolved_at = 0.0

    def start(self) -> None:
        threading.Thread(target=self._loop, name="pet-memory-consolidate", daemon=True).start()

    def poke(self) -> None:
        self._event.set()

    def _loop(self) -> None:
        time.sleep(6)
        while True:
            again = False
            try:
                again = self.cycle()
            except Exception:
                log.exception("记忆整理出错")
            if again:
                time.sleep(1.5)
                continue
            self._event.wait(timeout=45)
            self._event.clear()

    # ------------------------------------------------------------------ 服务配置
    def provider(self) -> dict | None:
        cp = {k: v for k, v in dict(self.s.consolidation_provider or {}).items() if v not in (None, "")}
        main = netfast.last_provider() or self._from_app_config() or {}
        if not cp.get("model"):
            return main or None
        # 副 API 只填 model 即可：其余字段（地址、key、证书校验）沿用主 API；
        # 填了不同的 base_url 却没填 api_key 时同样沿用主 API 的 key
        merged = dict(main)
        merged.update(cp)
        merged.setdefault("chat_path", "/v1/chat/completions")
        merged.setdefault("verify_ssl", True)
        merged.setdefault("timeout", 60.0)
        return merged if merged.get("base_url") and merged.get("api_key") else None

    def _from_app_config(self) -> dict | None:
        if self._resolved_provider is not None and time.time() - self._resolved_at < 600:
            return self._resolved_provider or None
        result = {}
        try:
            chat = read_app_config().get("chat") or {}
            pid = chat.get("active_provider") or "openai-main"
            raw = (chat.get("providers") or {}).get(pid) or {}
            from pet.chat.models import ProviderConfig, SecretStore  # type: ignore

            cfg = ProviderConfig.from_dict(pid, raw)
            key = ""
            if cfg.api_key_ref:
                key = SecretStore().get(cfg.api_key_ref) or ""
            key = key or cfg.api_key or ""
            if key:
                result = dict(
                    base_url=cfg.base_url, chat_path=cfg.chat_path, model=cfg.model, api_key=key,
                    verify_ssl=bool(cfg.verify_ssl), timeout=float(cfg.timeout or 60),
                )
        except Exception as exc:
            log.debug("记忆：读取聊天服务配置失败: %s", exc)
        self._resolved_provider = result
        self._resolved_at = time.time()
        return result or None

    # ------------------------------------------------------------------ 主循环
    def cycle(self) -> bool:
        if time.time() < self._backoff_until:
            return False
        pending = self.store.pending_sessions()
        if not pending:
            return False
        provider = self.provider()
        if not provider:
            return False
        if not self.store.try_lease(self.owner):
            return False
        now = time.time()
        chunk = max(6, int(self.s.episode_chunk_messages))
        idle_s = max(60.0, float(self.s.idle_consolidate_minutes) * 60)
        did = 0
        try:
            for row in pending:
                n, last_ts = row["n"], row["last_ts"] or now
                idle = now - last_ts
                if n >= chunk:
                    take = chunk
                elif n >= 4 and idle >= idle_s:
                    take = n
                elif n >= 2 and idle >= 6 * 3600:
                    take = n
                else:
                    continue
                rows = self.store.pending_messages(row["session_id"], take + 1)
                if len(rows) > take and rows[take - 1]["role"] == "user" and rows[take]["role"] == "assistant":
                    take += 1  # 不把一问一答拆到两段里
                rows = rows[:take]
                if not rows:
                    continue
                ok = self._summarize(provider, row["character_id"], row["session_id"], rows)
                if not ok:
                    return False
                did += 1
                self.store.try_lease(self.owner)
                if did >= 3:
                    break
        finally:
            if did:
                self._export()
        return did > 0

    def _summarize(self, provider: dict, cid: str, sid: str, rows) -> bool:
        name = self.mgr._pet_name(cid)
        if name == "我":
            name = "桌宠"
        facts = self.store.active_facts(cid, 80)
        facts_text = "\n".join(f"- {f['fkey']}: {f['content']}" for f in facts) or "（暂无）"
        lines = []
        budget = 14000
        for r in rows:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M") if r["ts"] else "?"
            who = "用户" if r["role"] == "user" else name
            text = (r["content"] or "").strip()
            if len(text) > 1200:
                text = text[:1100] + "……（略）"
            line = f"[{ts}] {who}：{text}"
            budget -= len(line)
            if budget < 0:
                break
            lines.append(line)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(name=name)},
            {"role": "user", "content": USER_PROMPT.format(name=name, facts=facts_text, transcript="\n".join(lines))},
        ]
        chunk_key = rows[0]["message_id"]
        t0 = time.perf_counter()
        try:
            data, usage = netfast.complete_json(provider, messages, timeout=max(60.0, float(provider.get("timeout", 60)) * 2))
            if not isinstance(data, dict) or not str(data.get("summary") or "").strip():
                raise ValueError("模型未返回有效的 summary")
        except Exception as exc:
            self._fails += 1
            self._chunk_fails[chunk_key] = self._chunk_fails.get(chunk_key, 0) + 1
            if self._chunk_fails[chunk_key] >= 3 and isinstance(exc, (ValueError, json.JSONDecodeError, KeyError)):
                data, usage = self._fallback(rows, name), {}
                log.warning("记忆整理：同一段对话连续 3 次解析失败，已用本地摘要跳过（%s）", exc)
            else:
                delay = min(1800, 30 * (2 ** min(self._fails, 6)))
                self._backoff_until = time.time() + delay
                log.warning("记忆整理失败（%s），%d 秒后重试", exc, delay)
                return False
        self._fails = 0
        self._chunk_fails.pop(chunk_key, None)
        ops = data.get("facts") if isinstance(data.get("facts"), list) else []
        eid = self.store.save_episode(cid, sid, rows, data, ops, str(provider.get("model") or ""))
        log.info(
            "记忆整理：%d 条对话 → 情节#%d「%s」，事实变更 %d 条（%.1fs，输入 %s / 缓存命中 %s token）",
            len(rows), eid, data.get("title", ""), len(ops), time.perf_counter() - t0,
            usage.get("prompt_tokens", "-"), usage.get("prompt_cache_hit_tokens", "-"),
        )
        return True

    @staticmethod
    def _fallback(rows, name: str) -> dict:
        users = [(r["content"] or "").strip().replace("\n", " ") for r in rows if r["role"] == "user"]
        summary = "用户聊到：" + "；".join(u[:40] for u in users[:6])
        return {"title": "零散对话", "summary": summary[:300], "keywords": [], "importance": 0.2, "facts": []}

    # ------------------------------------------------------------------ 可读导出
    def _export(self) -> None:
        try:
            c = self.store.conn()
            out = ["# 桌宠记忆快照", "", f"> 自动生成于 {datetime.now():%Y-%m-%d %H:%M}，仅供查看；修改不会生效。"
                   "要手动补充记忆请编辑同目录的 notes.md。", ""]
            for cid, in c.execute("SELECT DISTINCT character_id FROM facts UNION SELECT DISTINCT character_id FROM episodes"):
                out += [f"## 角色：{cid}", "", "### 长期事实（L3）", ""]
                for f in c.execute(
                    "SELECT category, fkey, content, importance FROM facts WHERE character_id=? AND status='active'"
                    " ORDER BY category, importance DESC", (cid,)
                ):
                    out.append(f"- [{f['category']}] {f['content']}  `{f['fkey']}` ({f['importance']:.1f})")
                out += ["", "### 最近的情节记忆（L2）", ""]
                for e in c.execute(
                    "SELECT * FROM episodes WHERE character_id=? ORDER BY end_ts DESC LIMIT 50", (cid,)
                ):
                    day = datetime.fromtimestamp(e["end_ts"]).strftime("%Y-%m-%d %H:%M") if e["end_ts"] else "?"
                    out.append(f"- **{day}「{e['title']}」** {e['summary']}")
                    try:
                        d = json.loads(e["details"] or "{}")
                    except Exception:
                        d = {}
                    for label, k in (("约定", "commitments"), ("待办", "open_items")):
                        if d.get(k):
                            out.append(f"  - {label}：" + "；".join(map(str, d[k])))
                out.append("")
            path = self.mgr.dir / "记忆快照.md"
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(out), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.debug("记忆：导出快照失败", exc_info=True)
