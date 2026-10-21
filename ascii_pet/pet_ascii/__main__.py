"""独立运行入口。

  python -m pet_ascii           透明桌宠窗口（右键菜单可打开开发测试台）
  python -m pet_ascii --lab     直接打开开发测试台
  python -m pet_ascii --mock    强制 Mock 模式（不调用 API）
"""
from __future__ import annotations

import argparse
import os
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pet_ascii")
    ap.add_argument("--lab", action="store_true", help="打开开发测试台")
    ap.add_argument("--mock", action="store_true", help="使用 Mock 生成器，不调用 API")
    args, _ = ap.parse_known_args(argv)

    from PySide6.QtWidgets import QApplication

    from .ai.mock import MockClient
    from .runtime import Runtime

    app = QApplication.instance() or QApplication(sys.argv)
    rt = Runtime(client=MockClient() if args.mock else None, client_desc="mock（--mock）" if args.mock else "")
    holder = {}

    def open_lab():
        from .lab import Lab

        if "lab" not in holder:
            holder["lab"] = Lab(rt)
        holder["lab"].show()
        holder["lab"].raise_()

    if args.lab:
        open_lab()
    else:
        from .window import AsciiPetWindow

        holder["win"] = AsciiPetWindow(rt, open_lab=open_lab, on_close=lambda: None)
        holder["win"].show()
    selftest = os.environ.get("PET_ASCII_SELFTEST")
    if selftest:  # 自检：触发一次表达，截取窗口画面后退出（用于验证打包环境下的真实渲染）
        from PySide6.QtCore import QTimer

        def snap():
            for name in ("win", "lab"):
                if name in holder:
                    holder[name].grab().save(os.path.join(selftest, f"selftest_{name}.png"))
            with open(os.path.join(selftest, "selftest.txt"), "w", encoding="utf-8") as f:
                lines = [f"client={rt.director.client_desc}", f"status={rt.director.status}", f"fps={rt.fps:.1f}",
                         f"expression={rt.engine.expression}", f"recent={rt.engine.recent}"]
                f.write("\n".join(lines) + "\n")
            app.quit()

        QTimer.singleShot(1200, lambda: rt.express("高兴地打招呼", source="lab", priority=9, force_new=True))
        QTimer.singleShot(int(os.environ.get("PET_ASCII_SELFTEST_MS", "4500")), snap)
    code = app.exec()
    rt.stop()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
