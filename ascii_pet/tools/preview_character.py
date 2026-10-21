"""把角色素材的每个姿势渲染成 PNG（离屏），用于检查转换效果和面部区域。

用法: python tools/preview_character.py <角色.json> <输出目录> [--box] [--neutral]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from pet_ascii.character import load_character  # noqa: E402
from pet_ascii.grid import Layer, compose  # noqa: E402
from pet_ascii.render import AsciiView  # noqa: E402


def main():
    path, out_dir = sys.argv[1], sys.argv[2]
    show_box = "--box" in sys.argv
    show_neutral = "--neutral" in sys.argv
    app = QApplication.instance() or QApplication([])
    ch = load_character(path)
    os.makedirs(out_dir, exist_ok=True)
    for name, pose in ch.poses.items():
        layers = [pose.body]
        if pose.face and pose.neutral and show_neutral:
            face = pose.neutral.copy()
            face.x, face.y, face.z = pose.face["x"], pose.face["y"], 2
            layers.append(face)
        if pose.face and show_box:
            box = Layer("box", pose.w, pose.h, 9)
            f = pose.face
            for x in range(f["x"] - 1, f["x"] + f["w"] + 1):
                box.set(x, f["y"] - 1, "-", 0xFFFFD000)
                box.set(x, f["y"] + f["h"], "-", 0xFFFFD000)
            layers.append(box)
        chars, colors = compose(layers, pose.w, pose.h)
        view = AsciiView(cell_h=int(os.environ.get("CELL_H", "12")), background=0xFF101418)
        view.set_frame(chars, colors, pose.w, pose.h)
        view.resize(view.sizeHint())
        out = os.path.join(out_dir, f"{ch.name}_{name}.png")
        view.snapshot().save(out)
        print(out, pose.w, pose.h, pose.face, pose.eyes)


if __name__ == "__main__":
    main()
