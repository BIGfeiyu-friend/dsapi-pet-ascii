"""表情图鉴：用真实骨架与字符化流程渲染各预设表情，拼成一张图（检查五官效果）。

用法: python tools/face_gallery.py <角色名> <输出.png> [预设1 预设2 ...] [--pixels]
  --pixels  同时输出像素级绘制结果（字符化之前），方便调试骨架
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet_ascii import face_rig as fr  # noqa: E402
from pet_ascii.character import ASSET_DIR  # noqa: E402
from pet_ascii.render import AsciiView  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--custom" in sys.argv:          # --custom <自创表情库.json>：同时加载自创表情
        lib_path = sys.argv[sys.argv.index("--custom") + 1]
        args = [a for a in args if a != lib_path]
        from pet_ascii.custom_presets import CustomPresets

        CustomPresets(lib_path)
    name, out = args[0], args[1]
    presets = args[2:] or (list(fr.PRESETS) + list(fr.CUSTOM))
    app = QApplication.instance() or QApplication([])
    data = json.load(open(os.path.join(ASSET_DIR, f"{name}.json"), encoding="utf-8"))
    pose = data["poses"]["idle"]
    rig = fr.FaceRig(ASSET_DIR, pose["face_rig"])
    pal = data["palette"]
    f = pose["face"]
    pad_x, pad_y = 6, 3
    x0, y0 = max(0, f["x"] - pad_x), max(0, f["y"] - pad_y)
    w = min(pose["w"], f["x"] + f["w"] + pad_x) - x0
    h = min(pose["h"], f["y"] + f["h"] + pad_y) - y0
    cell = int(os.environ.get("CELL_H", "16"))
    tiles = []
    for pname in presets:
        spec = fr.normalize({"preset": pname})
        fchars, fcolors = rig.render(spec)
        chars, colors = [], []
        for y in range(y0, y0 + h):
            for x in range(x0, x0 + w):
                if f["x"] <= x < f["x"] + f["w"] and f["y"] <= y < f["y"] + f["h"]:
                    c = fchars[y - f["y"]][x - f["x"]]
                    k = fcolors[y - f["y"]][x - f["x"]]
                else:
                    c = pose["rows"][y][x]
                    ci = pose["colors"][y][x]
                    k = tuple(int(pal[ci][i:i + 2], 16) for i in (1, 3, 5)) if ci >= 0 else None
                chars.append(c if c != " " else "")
                colors.append((0xFF000000 | (k[0] << 16) | (k[1] << 8) | k[2]) if k else 0)
        v = AsciiView(cell_h=cell, background=0xFF0B0E14, glow=0.25)
        v.set_frame(chars, colors, w, h)
        v.resize(v.sizeHint())
        tiles.append((pname, v.snapshot()))
        if "--pixels" in sys.argv:
            rig.draw(spec).save(os.path.splitext(out)[0] + f"_{pname}_px.png")
    tw, th = tiles[0][1].width(), tiles[0][1].height()
    ncol = 4 if len(tiles) > 4 else len(tiles)
    nrow = (len(tiles) + ncol - 1) // ncol
    sheet = QImage(tw * ncol, (th + 22) * nrow, QImage.Format_RGB32)
    sheet.fill(QColor(8, 10, 14))
    p = QPainter(sheet)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(15)
    p.setFont(font)
    p.setPen(QColor(230, 230, 230))
    for i, (pname, img) in enumerate(tiles):
        cx, cy = (i % ncol) * tw, (i // ncol) * (th + 22)
        p.drawText(cx + 6, cy + 16, fr.display_name(pname))
        p.drawImage(QPoint(cx, cy + 22), img)
    p.end()
    sheet.save(out)
    print("saved", out, len(tiles), "tiles", tw, "x", th)


if __name__ == "__main__":
    main()
