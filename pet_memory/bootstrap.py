"""把记忆与提速功能挂到桌宠的聊天管线上（猴子补丁，不改原有文件）。

挂载点：
  PromptBuilder.build_messages   → 构建带记忆的请求（所有聊天入口：经典/现代窗口、快捷气泡、文件解读）
  SessionStore.save / delete     → 消息入 L4 原始库、触发后台整理
  ChatService.send               → 图片消息补回记忆前缀
  ChatService.__init__           → 预热到模型服务的连接
  OpenAICompatibleProvider.stream→ 长连接 + 增量读取 + 关闭 DeepSeek 默认思考
任何一步出错都会回退到原始实现。
"""
from __future__ import annotations

import functools
import logging
import sys
import threading

log = logging.getLogger("pet.memory")

_installed = False
manager = None


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    argv = " ".join(sys.argv[1:])
    if "--ascii-lab" in sys.argv or "--ascii-pet" in sys.argv or "--ascii-room" in sys.argv:
        # 用桌宠自带的 Python/PySide6 直接运行字符画模块（开发测试台、独立窗口，或主人的“房间”窗口），不启动桌宠本体
        _ascii_path()
        from pet_ascii.integration import run_standalone

        sys.exit(run_standalone(sys.argv))
    if "--settings" in sys.argv:
        # 单独的设置窗口进程：只做一件事——隐藏已删去的“生小肥鱼”设置（字符画设置 remove_spawn_pet）
        try:
            if _ascii_path():
                from pet_ascii import no_spawn

                if no_spawn.enabled():
                    no_spawn.install_settings()
        except Exception:
            log.exception("设置窗口：隐藏“生小肥鱼”失败，照常打开")
        return
    if "--multiprocessing" in argv or "parent_pid=" in argv:
        return
    try:
        # 再次启动时打开已有的那只（字符画设置 single_instance）：已经有一只在运行，就叫它出来，自己不再多开
        if _ascii_path():
            from pet_ascii.single_instance import forward_to_running

            if forward_to_running():
                sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        log.exception("检查是否已有桌宠在运行时出错，照常启动")
    try:
        _install()
    except Exception:
        log.exception("pet_memory 安装失败，桌宠将按原样运行")
    try:
        if _ascii_path():
            from pet_ascii.integration import install as install_ascii

            install_ascii()
    except Exception:
        log.exception("pet_ascii 接入失败，桌宠将按原样运行")


def _ascii_path() -> bool:
    """字符画模块位于安装目录下的 ascii_pet 文件夹（与 exe 同级），把它加入导入路径。"""
    import os

    root = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ascii_pet")
    if os.path.isdir(os.path.join(root, "pet_ascii")):
        if root not in sys.path:
            sys.path.append(root)
        return True
    return False


def _install() -> None:
    global manager
    from .settings import load_settings, read_app_config
    from . import netfast

    s = load_settings()
    from pet.chat import prompt, providers, service, session_store  # type: ignore

    orig_stream = providers.OpenAICompatibleProvider.stream
    netfast.configure(s, providers, orig_stream)

    if s.fast_stream:
        providers.OpenAICompatibleProvider.stream = netfast.stream

        orig_ctx = providers._make_ssl_context
        ctx_cache: dict = {}

        @functools.wraps(orig_ctx)
        def _make_ssl_context(verify=True):
            key = bool(verify)
            if key not in ctx_cache:
                ctx_cache[key] = orig_ctx(verify)
            return ctx_cache[key]

        providers._make_ssl_context = _make_ssl_context

        def prewarm_active():
            try:
                chat = read_app_config().get("chat") or {}
                raw = (chat.get("providers") or {}).get(chat.get("active_provider") or "") or {}
                if raw.get("base_url"):
                    netfast.prewarm(raw["base_url"], raw.get("chat_path", ""), bool(raw.get("verify_ssl", True)))
            except Exception:
                pass

        orig_init = service.ChatService.__init__

        @functools.wraps(orig_init)
        def chat_service_init(self, *a, **k):
            orig_init(self, *a, **k)
            prewarm_active()
            if manager is not None:
                try:
                    manager.ensure_started()
                except Exception:
                    pass

        service.ChatService.__init__ = chat_service_init

    if not s.memory_enabled:
        log.info("pet_memory：提速补丁已启用，记忆功能已关闭")
        return

    from .core import MemoryManager

    manager = MemoryManager(s)
    mgr = manager

    orig_build = prompt.PromptBuilder.build_messages

    @functools.wraps(orig_build)
    def build_messages(self, settings, character_id, history, user_text, *args, **kwargs):
        role_prompt = args[0] if args else kwargs.get("role_prompt")
        try:
            return mgr.build(self, settings, character_id, history, user_text, role_prompt)
        except Exception:
            log.exception("记忆：构建请求失败，回退原始逻辑")
            return orig_build(self, settings, character_id, history, user_text, *args, **kwargs)

    prompt.PromptBuilder.build_messages = build_messages

    store_cls = session_store.SessionStore
    orig_save = store_cls.save

    @functools.wraps(orig_save)
    def save(self, session, *a, **k):
        result = orig_save(self, session, *a, **k)
        try:
            mgr.on_session_saved(session)
        except Exception:
            log.exception("记忆：记录会话失败")
        return result

    store_cls.save = save

    orig_delete = store_cls.delete

    @functools.wraps(orig_delete)
    def delete(self, session, *a, **k):
        result = orig_delete(self, session, *a, **k)
        try:
            mgr.on_session_deleted(session)
        except Exception:
            log.exception("记忆：记录会话删除失败")
        return result

    store_cls.delete = delete

    orig_send = service.ChatService.send

    @functools.wraps(orig_send)
    def send(self, messages, *a, **k):
        try:
            mgr.fix_multimodal(messages)
        except Exception:
            pass
        return orig_send(self, messages, *a, **k)

    service.ChatService.send = send

    # 启动 15 秒后（日志系统已就绪）开始回填旧会话并后台整理，不必等用户打开聊天窗口
    timer = threading.Timer(15.0, mgr.ensure_started)
    timer.daemon = True
    timer.start()
    log.info("pet_memory 已启用：记忆库 %s，思考模式=%s，长连接=%s", mgr.dir, s.deepseek_thinking, s.fast_stream)
