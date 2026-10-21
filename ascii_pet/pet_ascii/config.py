"""配置：<应用数据目录>/ascii_pet/config.json（首次运行自动生成）+ 环境变量覆盖。

API Key 查找顺序（不会写死在代码里）：
  1. config.json 的 api_key
  2. 环境变量 PET_ASCII_API_KEY / DEEPSEEK_API_KEY
  3. 桌宠本体已保存在系统凭据（钥匙串）里的 Key（在桌宠进程内，或安装了 keyring 模块时）
都没有时自动使用 Mock 模式。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_DIR_NAME = "dsh-pet-standalone-webm-chat"
KEYRING_SERVICE = "dsh-pet-standalone"


def app_base_dir() -> Path:
    """桌宠本体的数据目录。Windows：%APPDATA%\\<桌宠>（和以前完全一样）；其它系统跟着桌宠自己的算法走
    （macOS 是 ~/Library/Application Support/<桌宠>），拿不到时按系统推断。"""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home()) / APP_DIR_NAME
    try:
        from pet import config as pc  # type: ignore

        return Path(pc._default_base()) / (getattr(pc, "APP_DIR_NAME", None) or APP_DIR_NAME)
    except Exception:
        pass
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def data_dir() -> Path:
    base = os.environ.get("PET_ASCII_HOME")
    d = Path(base) if base else app_base_dir() / "ascii_pet"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class AsciiConfig:
    # —— API ——
    provider: str = "deepseek"            # deepseek | mock
    base_url: str = ""                     # 留空：沿用桌宠聊天设置，否则 https://api.deepseek.com
    chat_path: str = "/chat/completions"
    model: str = "deepseek-flash"          # DeepSeek 官方当前模型名（V4.1 Flash），快且便宜
    api_key: str = ""
    temperature: float = 0.9
    max_tokens: int = 2600
    timeout: float = 45.0
    json_mode: bool = True                 # DeepSeek 支持 response_format=json_object
    disable_thinking: bool = True          # 关闭思考模式（视觉生成不需要，且快很多）
    retries: int = 1                       # 输出不合规时带着错误信息重试的次数
    # —— 调度与预算 ——
    min_interval_s: float = 4.0            # 两次 API 请求的最小间隔
    dedupe_window_s: float = 20.0          # 同义请求去重窗口
    reuse_probability: float = 0.3         # 命中缓存时直接复用旧动画的概率（其余情况仍生成新的）
    daily_request_limit: int = 300
    daily_token_limit: int = 400000
    allow_mode3: bool = True
    # —— 自主表达（默认关闭）——
    autonomous: bool = False
    autonomous_min_s: float = 240.0
    autonomous_max_s: float = 720.0
    autonomous_daily_limit: int = 40
    # —— 自创表情 ——
    custom_presets: bool = True            # 允许模型在对话中用 new= 自创表情，并把自创表情备忘附在每轮消息里
    custom_presets_max: int = 60
    evolve: bool = True                    # 后台“表情进化”：攒够若干轮对话后，让副模型设计新表情
    evolve_min_replies: int = 6            # 至少多少轮新对话后才进化一次
    evolve_interval_min: float = 30.0      # 两次进化的最小间隔（分钟）
    evolve_daily_limit: int = 6
    evolve_thinking: bool = True           # 进化时开启思考模式（不占聊天时间，可以慢慢想）
    evolve_mirror_check: bool = True       # 设计完再“照镜子”复查一次（不开思考，便宜），不到位就再修一次
    evolve_min_change: float = 0.10        # 变化幅度目标：每次修改/新建在字符画上要改变多少比例的脸部格子（不同内置表情之间约 10~20%；以前的修改只有 5~8%）
    evolve_max_gain: float = 3.0           # 变化不够时最多自动放大几倍
    dialogue_keep: int = 16                # 存多少轮对话素材（字符画数据目录 dialogue.json，与桌宠记忆无关）
    # —— 创作实验（v0.7）：她自己发明部件、改造骨架、做自由演出；后台运行，只在没人理她 / 睡着时做 ——
    experiments: bool = True
    experiment_daily_limit: int = 4        # 每天最多几次实验
    experiment_interval_min: float = 45.0  # 两次实验的最小间隔（分钟）
    experiment_idle_min: float = 10.0      # 至少多少分钟没有互动才开始（睡着时也可以，相当于做梦）
    experiment_max_iters: int = 3          # 一次实验最多几轮“渲染 → 检查 → 修改”
    experiment_daily_tokens: int = 60000   # 实验每天最多花多少 token（另外还受全局的 daily_token_limit 约束）
    experiment_thinking: bool = True       # 设计时开思考（后台，不占聊天时间）
    experiment_vision_wait_s: float = 60.0  # 实验最多等视觉评价多久；没回来就先按本地检查继续，结果到达后写进记忆
    # —— 视觉评价（v0.7，可选，后台）：只评价全新部件 / 骨架改造 / 自由演出的截图，不参与平时的表情 ——
    vision_enabled: bool = True            # 有支持看图的模型才会真的调用；不支持时自动回退到只用本地检查
    vision_model: str = "deepseek-flash"   # 看图用的模型。DeepSeek 官方 API 上 deepseek-v4-pro 会把图片丢掉（实测），deepseek-flash 真的能看；
                                           # 留空：沿用桌宠设置里的视觉模型（设置为“与对话相同”时用对话模型）
    vision_base_url: str = ""
    vision_daily_requests: int = 12        # 每天最多几次视觉评价
    vision_daily_tokens: int = 40000
    vision_min_interval_s: float = 20.0    # 两次视觉评价的最小间隔（秒；实验每轮约 30~60 秒，太长会让后几轮等不到评价）
    vision_timeout_s: float = 45.0
    vision_max_pending: int = 2            # 最多排队几个（同一时间只跑 1 个）
    # —— 睡眠 ——
    sleep_enabled: bool = True             # 无互动一段时间后入睡（本地预设动画，不调用 API）；在对话里发消息会把她吵醒
    idle_min_delay_s: float = 60.0         # 回复说完后至少多少秒才会进入挂机；DS 在标签里决定的 after 秒在这之后再加
    sleep_after_min: float = 30.0          # 多少分钟无互动后入睡（对话、点按、切换表情、评价都算互动；拖动窗口、打开菜单只推迟入睡）
    sleep_tell_model: bool = True          # 被对话吵醒时，在那条消息末尾告诉模型“你刚才睡着了，被这条消息吵醒”（只在本轮附带，不写进记忆）
    # —— 副人格（v0.9，设计见 docs/persona_evolution_design.md）：她睡着时回顾最近的对话，提议慢慢形成的性格条目 ——
    persona_mode: str = "shadow"           # shadow 影子模式：完整跑管线但不进提示词（先看提议质量）/ trial 试运行 / live 正式 / off 关闭
    persona_reflect_min_hours: float = 20.0    # 两次夜间反思至少间隔几小时（一般一天最多一次）
    persona_after_sleep_min: float = 5.0   # 睡着多少分钟后开始反思
    persona_min_user_messages: int = 3     # 上次反思之后至少要有几条主人的新消息才反思（不够就等下次）
    persona_max_entries: int = 12          # 条目上限（满了以后新增必须先合并或退休）
    persona_max_chars: int = 420           # 副人格块的字数上限（约 250 token）
    persona_thinking: bool = True          # 反思时开思考（后台，不占聊天时间）
    persona_review_thinking: bool = True   # 审核时开思考
    persona_reasoning_effort: str = "low"
    persona_review_model: str = ""         # 审核用的模型；留空 = 与 model 相同（提示词独立，不带对话上下文）
    persona_daily_tokens: int = 60000      # 副人格每天最多花多少 token（另外还受全局的 daily_token_limit 约束）
    # —— 虚拟房间（v1.0）：属于她自己的文件夹，她在没有指令的时候自己决定进去做什么（桌宠运行时就会，与“房间”窗口开没开无关）——
    room_enabled: bool = True
    room_model: str = ""                   # 她在房间里思考用的模型；留空 = 桌宠聊天设置里的主模型（她平时说话用的那个）
    room_thinking: bool = True             # 开思考（思考量也是之后整理记忆时“这件事重不重要”的依据之一）
    room_reasoning_effort: str = "low"
    room_max_tokens: int = 6000            # 一步的上限（思考 + 输出）
    room_timeout_s: float = 120.0
    room_interval_min: float = 30.0        # 两次进房间的间隔（分钟）：她离开时自己决定过多久再来，限制在 min~max 之间；
    room_interval_max: float = 90.0        # 她没说（例如主人来了她就出来了）才按精力在这个范围里随便定
    room_night_start: int = 0              # 夜里（几点到几点）：间隔 × room_night_factor
    room_night_end: int = 8
    room_night_factor: float = 3.0         # 只在她没自己决定时用；夜里她也可以说“睡到天亮再来”
    room_after_chat_min: float = 3.0       # 主人最后一次互动后至少隔几分钟才进去；在房间里时主人发来消息，做完这一步就出来
    room_max_steps: int = 3                # 每次进去最多走几步（读文件后会接着决定下一步）
    room_daily_wakes: int = 16             # 每天最多进去几次
    room_daily_tokens: int = 150000        # 房间每天最多花多少 token（另外还受全局的 daily_token_limit 约束）
    room_wake_energy: float = 0.55         # 睡着时，精力至少这么多才会自己醒来进房间（出来后接着睡）
    room_asleep_interval_factor: float = 3.0  # 睡着时醒来进的房间（出来后接着睡）：她自己选“过多久再来”的范围
                                              # 最小、最大都 × 这个数（30~90 → 90~270 分钟，v1.3.5）；她选多少就睡多少；1 = 和醒着一样
    room_observer_known: bool = True       # 告诉她：主人知道这个房间，偶尔会透过“单向玻璃”看看
    room_memory_model: str = ""            # 睡着时整理房间记忆用的副模型；留空 = model
    room_short_hours: float = 20.0         # 短期记忆至少留多少小时才整理（整理在睡着时做，所以细节一般留一两天）
    room_gist_days: float = 7.0            # “印象”留几天
    room_long_max: int = 200               # 长期记忆上限（超过时最弱的淡出）
    room_snapshot_keep: int = 14           # 保留几天的房间快照
    # 看图片（v1.2）：DeepSeek 看不了图片，给她看图片转成的字符画 + 颜色概况 + 图里的文字（Windows 自带 OCR，离线）
    room_look: bool = True
    room_look_daily: int = 12              # 每天最多看几次图片
    room_look_cols: int = 64               # 字符画的宽度（列数；越宽越清楚，token 也越多）
    room_ocr: bool = True                  # 识别图里的文字
    room_look_vision: bool = True          # 也让能看图的副模型（vision_model，默认 deepseek-flash）真的看一眼原图，把描述一起给她
    # —— Spotify（v1.3）：给她用你的 Spotify 放歌的权限（用不用、什么时候、放哪首都由她决定）——
    spotify_enabled: bool = True           # 总开关（还要先在右键菜单「字符画 → Spotify → 连接」授权才真的能用；需要 Premium）
    spotify_client_id: str = ""            # 你在 developer.spotify.com 建的应用的 Client ID（不是密码）
    spotify_redirect_port: int = 8765      # 回调地址 http://127.0.0.1:8765/callback，要填进 Spotify 应用的 Redirect URIs
    spotify_interrupt: str = "queue_if_playing"   # 你在听歌时：queue_if_playing 只排队不打断（没在听时直接播放）/ queue_only 永远只排队 / play 直接切
    spotify_share_now_playing: bool = True # 让她知道你现在在听什么（聊天和房间里都能看到；每 30 秒看一次）
    spotify_max_per_hour: int = 3          # 每小时最多放几首（聊天和房间合计）
    spotify_max_per_day: int = 12
    # 双向打通（v1.1）：聊天和房间里的事都是她完整记忆的一部分（各自的存储不变：房间记忆不写进桌宠记忆库）
    room_bridge_to_chat: bool = True       # 聊天时她记得房间里的事（系统提示附房间的长期记忆 / 印象，本轮消息末尾附房间近况）
    room_bridge_from_chat: bool = True     # 房间里她记得和你聊过什么（只读桌宠记忆库：长期记忆、笔记、前情提要、最近的聊天）
    room_chat_recent: int = 12             # 房间里带最近几条聊天原文
    # —— 与桌宠对话联动 ——
    attach_to_pet: bool = False            # 随桌宠启动字符画窗口
    trim_unused_menu: bool = True          # 去掉字符画用不到的原桌宠菜单项（v1.3.6）：播放动画、切换角色、播放速率、音乐、桌宠控制、
                                           # 快捷启动、工具与帮助、Agent 联动、主动识屏、待办提醒、彩蛋「厉害了我的鲸」；改成 false 并重启即恢复
    remove_spawn_pet: bool = True          # 删去“生小肥鱼”：不再生成小肥鱼，右键菜单的「生小肥鱼」「退出子肥鱼」和设置里那一组都去掉（改成 false 并重启桌宠即恢复）
    single_instance: bool = True           # 桌宠已在运行时再次启动（双击图标等）：不多开第二只，把正在运行的这只叫出来；“生小肥鱼”和带 --slot 的启动不受影响
    close_tray_tip: bool = True            # 关掉字符画窗口时在托盘弹一条提示：桌宠还在后台、怎么再打开（右键托盘 →「显示 / 隐藏」）
    hide_original_pet: bool = True         # 原来那个会到处跑的桌宠常驻隐藏：关掉字符画窗口后也不出来，托盘/灵动岛的“显示桌宠”改为打开字符画窗口（字符画菜单里可以再显示，选择会记住）
    integrate_pet_menu: bool = True        # 在字符画窗口上右键：弹出原桌宠的完整菜单，字符画的选项插在最前面
    chat_theme: str = "ascii"              # “AI 对话”经典窗口的皮肤：line（LINE 风格，v1.4.0）/ ascii（字符画风格）/ original（桌宠原样）
    integrate_island: bool = True          # 把原来的灵动岛收进字符画窗口右下角的“◈ 灵动岛”按钮（点击弹出），原来浮在屏幕上的灵动岛隐藏
    island_balance_refresh_min: float = 10.0   # 灵动岛卡片里的余额在后台自动更新的间隔（分钟）；点开卡片、每次对话回复后也会查一次
    chat_title: str = "DeepSeek"           # LINE / ASCII 皮肤下对话框标题栏显示的文字（原来是“角色名 · AI 对话”）
    chat_link: str = "inline"              # inline：主模型回复开头顺带输出表情标签（推荐）；ai：另起一次表情生成；local：本地关键词；off
    inline_reminder: bool = True           # 在本轮用户消息末尾提醒一句“以表情标签开头”（真实对话历史很长时模型容易忘）
    # —— 显示 ——
    window_style: str = "framed"           # framed：不透明窗口（带标题栏）；floating：透明悬浮
    character: str = "maid_bust"            # maid_bust 女仆特写（脸大，表情最清楚）/ maid 半身带桌子 / shenshen_bust / shenshen
    cell_h: int = 8
    fps: int = 20
    background: str = "none"
    code_rows: bool = True
    glow: float = 0.35
    plate_mode: str = "tint"               # tint | solid | none
    scanlines: float = 0.0
    window_x: int = -1
    window_y: int = -1
    always_on_top: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return data_dir() / "config.json"

    def save(self) -> None:
        data = {"_说明": "修改后重启生效。api_key 留空则读取环境变量或桌宠已保存的 Key。"}
        data.update(asdict(self))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @classmethod
    def load(cls) -> "AsciiConfig":
        cfg = cls()
        p = cfg.path
        raw = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                raw = {}
        for k, v in raw.items():
            if k.startswith("_") or not hasattr(cfg, k):
                continue
            default = getattr(cfg, k)
            try:
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, float):
                    v = float(v)
                elif isinstance(default, str):
                    v = str(v)
                setattr(cfg, k, v)
            except (TypeError, ValueError):
                pass
        env = os.environ
        if env.get("PET_ASCII_MODEL"):
            cfg.model = env["PET_ASCII_MODEL"]
        if env.get("PET_ASCII_BASE_URL"):
            cfg.base_url = env["PET_ASCII_BASE_URL"]
        if env.get("PET_ASCII_PROVIDER"):
            cfg.provider = env["PET_ASCII_PROVIDER"]
        if not p.exists() or set(asdict(cfg)) - set(raw):
            try:
                cfg.save()
            except OSError:
                pass
        return cfg


def pet_app_config() -> dict:
    """读取桌宠本体的 config.json（只读），用于沿用它的 DeepSeek 地址与 Key 引用。"""
    p = app_base_dir() / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_api(cfg: AsciiConfig) -> dict:
    """得到实际使用的 base_url / api_key / 来源说明。"""
    chat = (pet_app_config().get("chat") or {})
    prov = (chat.get("providers") or {}).get(chat.get("active_provider") or "") or {}
    base = cfg.base_url or prov.get("base_url") or "https://api.deepseek.com"
    key, source = cfg.api_key, "config.json"
    if not key:
        for name in ("PET_ASCII_API_KEY", "DEEPSEEK_API_KEY"):
            if os.environ.get(name):
                key, source = os.environ[name], f"环境变量 {name}"
                break
    ref = prov.get("api_key_ref") or ""
    if not key and ref:
        try:
            from pet.chat.models import SecretStore  # type: ignore  # 在桌宠进程内可用

            key = SecretStore().get(ref) or ""
            source = "桌宠已保存的 Key"
        except Exception:
            key = ""
        if not key:
            try:
                import keyring  # type: ignore

                key = keyring.get_password(KEYRING_SERVICE, ref) or ""
                source = "系统凭据（keyring）"
            except Exception:
                key = ""
    if not key and prov.get("api_key"):
        key, source = prov["api_key"], "桌宠配置"
    return {"base_url": base, "api_key": key, "source": source if key else "无"}
