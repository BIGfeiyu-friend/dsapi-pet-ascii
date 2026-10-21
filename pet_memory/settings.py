"""记忆模块配置：<应用数据目录>/memory/memory_config.json（首次运行自动生成）。"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("pet_memory")

APP_DIR_FALLBACK = "dsh-pet-standalone-webm-chat"


@dataclass
class MemorySettings:
    # 总开关
    memory_enabled: bool = True
    fast_stream: bool = True
    # DeepSeek 思考模式："disabled"（推荐，桌宠闲聊更快）/ "enabled" / "default"（不传参数，交给服务端默认）
    deepseek_thinking: str = "disabled"
    # 合并到每个聊天请求里的额外字段（高级用法，例如 {"top_p": 0.9}）
    extra_payload: dict = field(default_factory=dict)

    # L1：0 表示沿用聊天设置里的“历史消息条数”
    l1_max_messages: int = 0
    # 窗口满后一次性推进到只保留这么多条（0 = l1_max 的一半）
    l1_keep_messages: int = 0

    # L2：未整理消息达到该条数就压缩成一段情节记忆
    episode_chunk_messages: int = 20
    # 会话空闲超过该分钟数、且有 >= 4 条未整理消息时，也整理（视为一个话题结束）
    idle_consolidate_minutes: float = 15.0
    # 系统提示里附带最近几段“前情提要”
    recap_episodes: int = 3

    # 字数预算
    core_fact_chars: int = 1200
    recap_chars: int = 900
    recall_chars: int = 1500
    # 动态检索里最多带几条原始消息（L4），仅在“还记得/上次…”这类提问或强匹配时
    raw_recall_max: int = 3

    include_time: bool = True
    # 系统提示中的记忆快照多久强制刷新一次（小时）；其余时间仅在窗口推进时刷新，以保持缓存命中
    snapshot_refresh_hours: float = 6.0
    # 删除会话时是否连同记忆一起删除（默认保留：记忆是“永久”的）
    forget_deleted_sessions: bool = False

    # 副 API（后台整理记忆用）。留空 = 与主 API 相同；只填 {"model": "deepseek-flash"} 即可换模型，
    # 地址和 key 沿用主 API；换服务商时再填 base_url / api_key / chat_path
    consolidation_provider: dict = field(default_factory=dict)

    # 网络：空闲多久的长连接不再复用（秒）
    keepalive_idle_seconds: float = 50.0
    log_timing: bool = True


def app_data_dir() -> Path:
    try:
        from pet import config as pc  # type: ignore

        base = pc._default_base()
        name = getattr(pc, "APP_DIR_NAME", None) or APP_DIR_FALLBACK
        return Path(base) / name
    except Exception:
        if sys.platform == "win32":
            return Path(os.environ.get("APPDATA") or Path.home()) / APP_DIR_FALLBACK
        return Path.home() / ".config" / APP_DIR_FALLBACK


def memory_dir() -> Path:
    d = app_data_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_settings() -> MemorySettings:
    path = memory_dir() / "memory_config.json"
    s = MemorySettings()
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            log.warning("memory_config.json 解析失败，使用默认值: %s", exc)
            raw = {}
    for k, v in raw.items():
        if k.startswith("_") or not hasattr(s, k):
            continue
        default = getattr(s, k)
        try:
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = int(v)
            elif isinstance(default, float):
                v = float(v)
            elif isinstance(default, dict):
                v = dict(v or {})
            elif isinstance(default, str):
                v = str(v)
            setattr(s, k, v)
        except Exception:
            pass
    # 写回：补全新增字段，方便用户查看/修改
    missing = [k for k in asdict(s) if k not in raw]
    if missing or not path.exists():
        try:
            data = {"_说明": "修改后重启桌宠生效。各字段含义见 pet_memory/settings.py。"}
            data.update(asdict(s))
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass
    return s


def read_app_config() -> dict:
    try:
        return json.loads((app_data_dir() / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
