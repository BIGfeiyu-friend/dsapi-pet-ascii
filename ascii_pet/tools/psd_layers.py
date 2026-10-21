"""最小的 PSD 图层读取器（纯 Python + PIL）。

PIL 自带的 PSD 读取对 Procreate 导出的文件会把所有图层读成同一张图，所以这里按 PSD 规范自己解析图层记录：
名字（优先 Unicode 名 luni）、位置、可见性、不透明度、混合模式、剪贴蒙版、图层组标记，以及每个通道的像素
（支持未压缩和 RLE/PackBits；Procreate、Photoshop 默认都是这两种）。只读，不修改原文件。

用法：python tools/psd_layers.py <文件.psd> [导出目录]   —— 列出图层；给了导出目录就把每层导出为 PNG（含 layers.json）
"""
from __future__ import annotations

import json
import os
import struct
import sys

BLEND_CN = {"norm": "正常", "mul ": "正片叠底", "scrn": "滤色", "over": "叠加", "pass": "穿透", "lddg": "线性减淡（添加）",
            "add ": "添加", "dark": "变暗", "lite": "变亮", "colr": "颜色", "soft": "柔光", "hard": "强光"}


class Layer:
    def __init__(self):
        self.name = ""
        self.top = self.left = self.bottom = self.right = 0
        self.channels: list = []          # [(id, length)]
        self.blend = "norm"
        self.opacity = 255
        self.clipping = 0
        self.flags = 0
        self.section = 0                  # 0 普通；1/2 图层组开始（展开/折叠）；3 图层组结束标记
        self.image = None                 # PIL RGBA（只含图层自身范围）

    @property
    def hidden(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def bbox(self) -> tuple:
        return (self.left, self.top, self.right, self.bottom)

    def info(self) -> dict:
        return {"name": self.name, "bbox": list(self.bbox), "hidden": self.hidden, "opacity": self.opacity,
                "blend": self.blend.strip(), "clipping": self.clipping, "section": self.section}


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0], o + 4


def read(path: str, load_pixels: bool = True):
    """返回 (宽, 高, [Layer]，自底向上的顺序)。"""
    from PIL import Image

    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"8BPS" or struct.unpack_from(">H", data, 4)[0] != 1:
        raise ValueError("不是 PSD（或是 PSB 大文件格式）")
    channels, h, w, depth, mode = struct.unpack_from(">HIIHH", data, 12)
    if depth != 8 or mode != 3:
        raise ValueError(f"只支持 8 位 RGB（这个文件是 {depth} 位、颜色模式 {mode}）")
    o = 26
    n, o = _u32(data, o)
    o += n                                              # 颜色模式数据
    n, o = _u32(data, o)
    o += n                                              # 图像资源
    lmi_len, o = _u32(data, o)
    lmi_end = o + lmi_len
    li_len, o = _u32(data, o)
    count = struct.unpack_from(">h", data, o)[0]
    o += 2
    count = abs(count)
    layers = []
    for _ in range(count):
        L = Layer()
        L.top, L.left, L.bottom, L.right = struct.unpack_from(">iiii", data, o)
        o += 16
        nch = struct.unpack_from(">H", data, o)[0]
        o += 2
        for _c in range(nch):
            cid, clen = struct.unpack_from(">hI", data, o)
            o += 6
            L.channels.append((cid, clen))
        o += 4                                          # '8BIM'
        L.blend = data[o:o + 4].decode("latin-1")
        o += 4
        L.opacity, L.clipping, L.flags = data[o], data[o + 1], data[o + 2]
        o += 4
        extra, o = _u32(data, o)
        end = o + extra
        n, o = _u32(data, o)
        o += n                                          # 图层蒙版
        n, o = _u32(data, o)
        o += n                                          # 混合范围
        nl = data[o]
        raw_name = data[o + 1:o + 1 + nl]
        o += (1 + nl + 3) // 4 * 4
        try:
            L.name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            L.name = raw_name.decode("gbk", "replace")
        while o + 12 <= end:                            # 附加图层信息
            sig, key = data[o:o + 4], data[o + 4:o + 8].decode("latin-1")
            if sig not in (b"8BIM", b"8B64"):
                break
            blen = struct.unpack_from(">I", data, o + 8)[0]
            body = data[o + 12:o + 12 + blen]
            if key == "luni" and len(body) >= 4:
                cnt = struct.unpack_from(">I", body, 0)[0]
                L.name = body[4:4 + cnt * 2].decode("utf-16-be", "replace").rstrip("\x00")
            elif key in ("lsct", "lsdk") and len(body) >= 4:
                L.section = struct.unpack_from(">I", body, 0)[0]
            o += 12 + (blen + 1) // 2 * 2
        o = end
        layers.append(L)
    # 各图层的通道像素
    for L in layers:
        w_, h_ = L.right - L.left, L.bottom - L.top
        planes = {}
        for cid, clen in L.channels:
            comp = struct.unpack_from(">H", data, o)[0]
            body = data[o + 2:o + clen]
            o += clen
            if not load_pixels or w_ <= 0 or h_ <= 0:
                continue
            if comp == 0:
                planes[cid] = Image.frombytes("L", (w_, h_), body[:w_ * h_])
            elif comp == 1:
                rows = h_ if cid != -2 else h_
                packed = body[rows * 2:]
                planes[cid] = Image.frombytes("L", (w_, h_), packed, "packbits", "L")
            else:
                raise ValueError(f"图层「{L.name}」用了 ZIP 压缩（{comp}），暂不支持")
        if load_pixels and w_ > 0 and h_ > 0 and all(c in planes for c in (0, 1, 2)):
            a = planes.get(-1) or Image.new("L", (w_, h_), 255)
            L.image = Image.merge("RGBA", (planes[0], planes[1], planes[2], a))
    return w, h, layers


def main():
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else ""
    w, h, layers = read(path, load_pixels=bool(out))
    print(f"画布 {w}x{h}，{len(layers)} 个图层（自底向上）")
    depth = 0
    rows = []
    for i, L in enumerate(layers):
        if L.section == 3:
            depth = max(0, depth - 1)
        tag = {1: "[组·展开]", 2: "[组·折叠]", 3: "[组结束]"}.get(L.section, "")
        print(f"{i:3d} {'  ' * depth}{L.name} {tag} bbox={L.bbox} {'隐藏 ' if L.hidden else ''}"
              f"不透明度={L.opacity} 混合={BLEND_CN.get(L.blend, L.blend)}{' 剪贴' if L.clipping else ''}")
        if L.section in (1, 2):
            depth += 1
        rows.append({"index": i, **L.info()})
        if out and L.image is not None:
            os.makedirs(out, exist_ok=True)
            L.image.save(os.path.join(out, f"{i:03d}.png"))
    if out:
        with open(os.path.join(out, "layers.json"), "w", encoding="utf-8") as f:
            json.dump({"size": [w, h], "layers": rows}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
