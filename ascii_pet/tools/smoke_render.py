"""离屏冒烟测试：真实角色 + Mock 动画 → 引擎 → 渲染器，保存若干帧 PNG 并打印性能数据。

用法: python tools/smoke_render.py <输出目录>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pet_ascii.ai import mock  # noqa: E402
from pet_ascii.character import find_character  # noqa: E402
from pet_ascii.engine import Engine, EngineSettings  # noqa: E402
from pet_ascii.protocol import parse_animation  # noqa: E402
from pet_ascii.render import AsciiView  # noqa: E402


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    ch = find_character()
    eng = Engine(ch, EngineSettings(background="log"), seed=7)
    view = AsciiView(cell_h=int(os.environ.get("CELL_H", "10")), background=0xFF0B0E14)
    render_ms = []

    def step(seconds, dt=0.05):
        for _ in range(int(seconds / dt)):
            eng.tick(dt)
            c, k, w, h, off = eng.frame()
            t0 = time.perf_counter()
            view.set_frame(c, k, w, h, off)
            render_ms.append((time.perf_counter() - t0) * 1000)

    def shot(name):
        view.resize(view.sizeHint())
        p = os.path.join(out, name + ".png")
        view.snapshot().save(p)
        print("saved", p)

    def play(intent, seed):
        obj = mock.generate(intent, eng.visual_state(), seed=seed)
        anim = parse_animation(obj, eng.context())
        print(f"[{intent}] {anim.summary()} warnings={anim.warnings}")
        eng.apply(anim)

    print("character:", ch.display_name, "canvas", eng.W, "x", eng.H, "poses", list(ch.poses))
    step(0.3)
    shot("00_idle")
    play("高兴地打招呼", 1)
    step(0.65)
    shot("01_happy")
    step(2.0)
    shot("02_happy_hold")
    play("突然感到困惑", 3)
    step(0.9)
    shot("03_confused")
    play("变成一团乱码", 5)
    step(1.1)
    shot("04_dissolve")
    step(2.4)
    shot("05_restored")
    play("害羞地说谢谢主人", 9)
    eng.apply(parse_animation({"ops": [{"type": "effect", "effect": "bubble", "text": "谢谢主人~ 今天也要加油哦", "duration_ms": 3000}]}, eng.context()))
    step(1.0)
    shot("06_shy_bubble")
    print("expression:", eng.expression, "recent:", eng.recent)
    print("compose ms (last): %.2f" % eng.stats["compose_ms"])
    rm = sorted(render_ms)
    print("render ms p50=%.2f p95=%.2f max=%.2f frames=%d" % (rm[len(rm) // 2], rm[int(len(rm) * 0.95)], rm[-1], len(rm)))
    t0 = time.perf_counter()
    for _ in range(40):
        eng.tick(0.05)
        eng.frame()
    print("compose avg ms: %.2f" % ((time.perf_counter() - t0) * 1000 / 40))


if __name__ == "__main__":
    main()
