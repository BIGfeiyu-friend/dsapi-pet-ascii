"""接入冒烟测试（需要桌宠的 pet 模块可导入）：attach_to_pet=true 时，
创建 ChatService 后应自动打开字符画窗口；模拟一次对话回复，应触发一次表情生成（Mock）。"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
home = tempfile.mkdtemp(prefix="pet_ascii_integ_")
os.environ["PET_ASCII_HOME"] = home
with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
    json.dump({"attach_to_pet": True, "provider": "mock", "chat_link": "ai"}, f)

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from pet_ascii import integration  # noqa: E402

integration.install()
from pet.chat.service import ChatService  # noqa: E402

svc = ChatService()


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


pump(3.5)
st = integration._state
print("window opened:", st["win"] is not None, "| client:", st["rt"].director.client_desc if st["rt"] else None)
listeners = list(ChatService._global_finished_listeners)
print("global listeners registered:", len(listeners))
for cb in listeners:
    cb("（开心地转了个圈，尾巴甩来甩去）主人回来啦！今天想我了没有？")
pump(2.5)
rt = st["rt"]
last = rt.director.last
print("generated:", last.summary()["intent"] if last else None)
print("source:", last.request.source if last else None, "| mode:", last.anim.mode if last and last.anim else None)
print("engine expression:", rt.engine.expression, "| current:", rt.engine.current.summary() if rt.engine.current else None)
ok = st["win"] is not None and listeners and last is not None and last.request.source == "chat"
print("RESULT:", "PASS" if ok else "FAIL")
sys.stdout.flush()
os._exit(0 if ok else 1)
