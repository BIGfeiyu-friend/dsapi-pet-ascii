"""再次启动时打开已有的那只（字符画设置 single_instance，默认开）。

桌宠已经在运行时，又双击了一次桌宠（桌面图标、开始菜单……）：新进程在启动最早的阶段（bootstrap）连上正在运行的
那只留下的本地管道，叫它把字符画窗口拿到最前面（关掉了就重新打开），然后自己直接退出，不再多开出第二只
（第二只有自己单独的会话列表，看起来就像聊天记录不见了）。

照常多开、不转发的情况：
  - 带参数启动（--slot、--settings、--ascii-room 等）
  - 桌宠自己“生小肥鱼”启动的新进程（环境变量 DSH_PET_SPAWN_OFFSET_INDEX）、指定了槽位的实例（DSH_PET_INSTANCE）
  - 设置里关掉了 single_instance，或者没有正在运行的那只（管道连不上）
只有主实例（槽位 0）监听管道；管道名按当前用户区分，只有本用户能连。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

PIPE_BASE = "dsh-pet-ascii-main"


def pipe_name() -> str:
    user = re.sub(r"[^0-9A-Za-z_.-]", "_", os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    return f"{PIPE_BASE}-{user}"


def _ascii_config(env) -> dict:
    home = env.get("PET_ASCII_HOME") or os.path.join(env.get("APPDATA") or str(Path.home()),
                                                     "dsh-pet-standalone-webm-chat", "ascii_pet")
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def should_forward(argv: list, env) -> bool:
    """这次启动是不是“又双击了一次桌宠”（应该交给已经在运行的那只）。"""
    if len(argv) > 1:                                         # 带任何参数：照常
        return False
    if env.get("DSH_PET_SPAWN_OFFSET_INDEX") or env.get("DSH_PET_INSTANCE"):
        return False                                          # 桌宠自己生出来的、指定了槽位的：照常多开
    return bool(_ascii_config(env).get("single_instance", True))


def forward_to_running(argv: list | None = None, env=None, opener=open, attempts: int = 3) -> bool:
    """连上正在运行的那只、叫它出来。成功返回 True（调用方随后直接退出），否则 False（照常启动）。"""
    argv = sys.argv if argv is None else argv
    env = os.environ if env is None else env
    if sys.platform != "win32" or not should_forward(argv, env):
        return False
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)      # 允许正在运行的那只把窗口放到最前面
    except Exception:
        pass
    path = "\\\\.\\pipe\\" + pipe_name()
    for i in range(max(1, attempts)):
        try:
            with opener(path, "wb", buffering=0) as f:
                f.write(b"show\n")
            return True
        except FileNotFoundError:                              # 没有正在运行的那只
            return False
        except OSError:                                        # 管道正忙：稍等再试
            if i + 1 < attempts:
                time.sleep(0.3)
    return False


def start_server(on_request, log=None):
    """主实例（槽位 0）监听管道；有人连上来就调用 on_request()。返回 QLocalServer（失败返回 None）。"""
    if os.environ.get("DSH_PET_INSTANCE"):                      # 不是主实例
        return None
    from PySide6.QtNetwork import QLocalServer

    srv = QLocalServer()
    srv.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not srv.listen(pipe_name()):
        if log is not None:
            log.info("字符画：没能监听单实例管道（%s），再次启动时会照常多开", srv.errorString())
        return None

    def on_conn():
        while srv.hasPendingConnections():
            c = srv.nextPendingConnection()
            if c is not None:
                c.disconnected.connect(c.deleteLater)
                c.readAll()
                c.disconnectFromServer()
        try:
            on_request()
        except Exception:
            if log is not None:
                log.exception("字符画：处理再次启动的请求失败")

    srv.newConnection.connect(on_conn)
    return srv
