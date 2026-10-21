"""预先提取手绘差分的笔画脊柱（连续变形用，见 pet_ascii/morph_face.py）。

输入：assets/characters/face_layers/（tools/import_face_layers.py 的输出）
输出：face_layers/strokes.json + face_layers/strokes/*.png
      每只眼每张差分拆成上眼睑 / 下眼睑 / 眼褶（淡淡的排线归给最近的一笔），各自的脊柱和覆盖范围；眉毛、嘴同理。
不跑这一步也能用（运行时会现算，约 1 秒），但启动会慢一点。重新导入 PSD 后会自动重跑。

用法：python tools/build_face_strokes.py [face_layers 目录]
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def run(folder: str) -> str:
    from pet_ascii import morph_face as mf

    t0 = time.perf_counter()
    data = mf.build(folder)
    path = mf.save(data, folder)
    eyes = {s: sorted(e["keys"]) for s, e in data["eyes"].items()}
    print(f"笔画脊柱：{time.perf_counter() - t0:.1f} 秒 → {path}")
    print(f"  眼睛 {eyes}；眉毛 {sorted(data['brows'])}；嘴 {'有' if data['mouth'] else '无'}")
    return path


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "assets", "characters", "face_layers"))
