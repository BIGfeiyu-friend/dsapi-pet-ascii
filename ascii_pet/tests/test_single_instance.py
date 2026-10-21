"""再次启动时打开已有的那只（single_instance）：双击第二次不多开，把正在运行的那只叫出来；生小肥鱼、带参数的启动照常。"""
import json
import sys
import threading
import time
import uuid

import pytest

from pet_ascii import single_instance as si


def env_with(tmp_path, **cfg):
    home = tmp_path / "ascii"
    home.mkdir(exist_ok=True)
    if cfg:
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return {"PET_ASCII_HOME": str(home), "USERNAME": "tester"}


def test_only_a_plain_relaunch_is_forwarded(tmp_path):
    env = env_with(tmp_path)
    assert si.should_forward(["pet.exe"], env)                                   # 默认开
    assert not si.should_forward(["pet.exe", "--slot", "1"], env)                # 带参数
    assert not si.should_forward(["pet.exe"], {**env, "DSH_PET_SPAWN_OFFSET_INDEX": "1"})   # 生小肥鱼
    assert not si.should_forward(["pet.exe"], {**env, "DSH_PET_INSTANCE": "slot-1"})
    assert not si.should_forward(["pet.exe"], env_with(tmp_path, single_instance=False))


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上转发")
def test_forward_writes_show_or_falls_back(tmp_path):
    env = env_with(tmp_path)
    sent = []

    class Pipe:
        def __init__(self, path, *a, **k):
            sent.append(path)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, b):
            sent.append(b)

    assert si.forward_to_running(["pet.exe"], env, opener=Pipe)
    assert sent[0].startswith("\\\\.\\pipe\\dsh-pet-ascii-main-") and sent[1] == b"show\n"

    def missing(*a, **k):
        raise FileNotFoundError

    assert not si.forward_to_running(["pet.exe"], env, opener=missing)          # 没有在运行的：照常启动
    tries = []

    def busy(*a, **k):
        tries.append(1)
        raise OSError("pipe busy")

    assert not si.forward_to_running(["pet.exe"], env, opener=busy, attempts=2) and len(tries) == 2
    assert not si.forward_to_running(["pet.exe", "--settings"], env, opener=Pipe)


def test_pipe_name_is_per_user(monkeypatch):
    monkeypatch.setenv("USERNAME", "张 三/x")
    assert si.pipe_name() == "dsh-pet-ascii-main-____x"                         # 张、空格、三、/ 都换成 _


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 命名管道")
def test_real_round_trip_brings_the_running_pet_forward(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    name = f"dsh-pet-ascii-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(si, "pipe_name", lambda: name)
    monkeypatch.delenv("DSH_PET_INSTANCE", raising=False)
    calls = []
    srv = si.start_server(lambda: calls.append(1))
    assert srv is not None
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", si.forward_to_running(["pet.exe"], env_with(tmp_path))))
    t.start()
    deadline = time.time() + 5
    while (not calls or t.is_alive()) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    t.join(1)
    srv.close()
    assert result.get("ok") is True and calls == [1]


def test_only_the_main_instance_listens(monkeypatch):
    monkeypatch.setenv("DSH_PET_INSTANCE", "slot-1")
    assert si.start_server(lambda: None) is None


def test_relaunch_opens_the_ascii_window(monkeypatch):
    from pet_ascii import integration

    opened = []
    monkeypatch.setattr(integration, "open_ascii_window", lambda: opened.append(1))
    integration._on_launched_again()
    assert opened == [1]
