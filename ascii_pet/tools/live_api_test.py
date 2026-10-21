"""真实 API 测试：用当前配置的模型生成几段动画，打印耗时 / token / 校验警告，并渲染截图。

用法: python tools/live_api_test.py <输出目录> [意图1 意图2 ...]
（会真实调用 API、产生少量费用；Key 按 config.resolve_api 的顺序查找）
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PET_ASCII_HOME", tempfile.mkdtemp(prefix="pet_ascii_live_"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from pet_ascii.ai.client import make_client  # noqa: E402
from pet_ascii.ai.director import Director  # noqa: E402
from pet_ascii.cache import AnimCache  # noqa: E402
from pet_ascii.character import find_character  # noqa: E402
from pet_ascii.config import AsciiConfig, data_dir  # noqa: E402
from pet_ascii.engine import Engine, EngineSettings  # noqa: E402
from pet_ascii.persist import StateStore  # noqa: E402
from pet_ascii.render import AsciiView  # noqa: E402

INTENTS = ["高兴地打招呼", "突然感到困惑", "害羞地低下头，脸红了", "变成一团乱码"]


def main():
    out = sys.argv[1]
    intents = sys.argv[2:] or INTENTS
    os.makedirs(out, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    cfg = AsciiConfig.load()
    cfg.min_interval_s = 0
    cfg.reuse_probability = 0
    client, desc = make_client(cfg)
    print("client:", desc)
    eng = Engine(find_character(cfg.character), EngineSettings(background="none"), seed=11)
    store = StateStore(data_dir() / "state.json")
    got = []
    ev = threading.Event()
    d = Director(cfg, store, AnimCache(data_dir() / "anim_cache"), lambda: (eng.visual_state(), eng.context()),
                 lambda r: (got.append(r), ev.set()), client, desc)
    view = AsciiView(cell_h=11, background=0xFF0B0E14)
    report = []
    for i, intent in enumerate(intents):
        ev.clear()
        n0 = len(got)
        d.request(intent, source="lab", priority=8, force_new=True)
        t0 = time.time()
        while len(got) == n0 and time.time() - t0 < 120:
            eng.tick(0.05)
            time.sleep(0.05)
        if len(got) == n0:
            print(f"[{intent}] 超时")
            continue
        res = got[-1]
        s = res.summary()
        print(f"\n[{intent}] mode={s['mode']} ops={s['ops']} {s['elapsed']}s tokens={s['prompt_tokens']}/"
              f"{s['completion_tokens']} hit={s['cache_hit_tokens']} attempts={s['attempts']} "
              f"fallback={s['fallback']} err={s['error']!r}")
        if s["warnings"]:
            print("  warnings:", s["warnings"])
        if res.anim:
            eng.apply(res.anim)
            shots = []
            for k in range(60):
                eng.tick(0.05)
                c, kk, w, h, off = eng.frame()
                view.set_frame(c, kk, w, h, off)
                if k in (6, 20, 45):
                    view.resize(view.sizeHint())
                    p = os.path.join(out, f"{i:02d}_{k:02d}.png")
                    view.snapshot().save(p)
                    shots.append(p)
            print("  shots:", [os.path.basename(p) for p in shots])
            face = eng.face_snapshot()
            print("  face after:")
            for r in face["rows"]:
                print("   |" + r + "|")
        report.append({"intent": intent, **s, "raw": res.raw_text})
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\nbudget:", store.budget.to_dict())


if __name__ == "__main__":
    main()
