"""角色素材：多个基础姿势（身体字符画 + 面部可编辑区域 + 默认五官）。

素材文件由 pet_ascii.convert 生成（JSON，格式 pet_ascii.character/1）。
找不到素材时使用内置的小幽灵角色，保证 Mock 模式和测试随时可运行。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .grid import DEFAULT_COLOR, Layer, parse_color

ASSET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "characters")


@dataclass
class Pose:
    name: str
    w: int
    h: int
    body: Layer
    face: dict | None = None            # {"x","y","w","h"}：身体坐标系下的面部区域
    neutral: Layer | None = None        # 默认五官（面部区域尺寸），透明格为 None
    eyes: list = field(default_factory=list)  # 面部坐标系下的眼睛框 [x, y, w, h]
    skin_char: str = "@"
    skin_color: int = 0xFFFFE0D0
    feature_color: int = 0xFF1E3A8A
    rig_info: dict | None = None        # 面部骨架（format/2 场景角色）：五官由 FaceRig 实时绘制
    asset_dir: str = ""
    _rig: object = None

    @property
    def rig(self):
        if self.rig_info and self._rig is None:
            from .face_rig import FaceRig

            self._rig = FaceRig(self.asset_dir, self.rig_info)
        return self._rig


@dataclass
class Character:
    name: str
    display_name: str
    poses: dict
    default_pose: str
    source: str = "builtin"
    scene: bool = False                 # 整幅场景图（矩形画面），而不是透明背景上的角色

    def pose(self, name: str | None) -> Pose:
        return self.poses.get(name or "", self.poses[self.default_pose])

    @property
    def max_w(self) -> int:
        return max(p.w for p in self.poses.values())

    @property
    def max_h(self) -> int:
        return max(p.h for p in self.poses.values())


def _layer_from(rows, colors, palette, name, z=0, transparent=" ") -> Layer:
    h = len(rows)
    w = max((len(r) for r in rows), default=0)
    L = Layer(name, w, h, z)
    for y, row in enumerate(rows):
        crow = colors[y] if colors and y < len(colors) else None
        for x, ch in enumerate(row):
            if ch == transparent or ch == "`":
                continue
            ci = crow[x] if crow is not None and x < len(crow) else -1
            col = parse_color(palette[ci]) if isinstance(ci, int) and 0 <= ci < len(palette) else DEFAULT_COLOR
            L.set(x, y, ch, col)
    return L


def load_character(path: str) -> Character:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") not in ("pet_ascii.character/1", "pet_ascii.character/2"):
        raise ValueError(f"不支持的角色素材格式: {data.get('format')}")
    pal = data.get("palette") or []
    poses = {}
    for pname, p in data["poses"].items():
        body = _layer_from(p["rows"], p.get("colors"), pal, "body", transparent=" ")
        face = p.get("face")
        neutral = None
        eyes = []
        feature_color = 0xFF1E3A8A
        if face and p.get("neutral"):
            neutral = _layer_from(p["neutral"]["rows"], p["neutral"].get("colors"), pal, "face", transparent="`")
            eyes = [list(e) for e in p["neutral"].get("eyes", [])]
            cols = [c for c, ch in zip(neutral.colors, neutral.chars) if ch]
            if cols:
                feature_color = max(set(cols), key=cols.count)
        skin = p.get("skin") or {}
        poses[pname] = Pose(pname, p["w"], p["h"], body, face, neutral, eyes,
                            skin.get("skin_char", "@"), parse_color(skin.get("skin_color"), 0xFFFFE0D0), feature_color,
                            rig_info=p.get("face_rig"), asset_dir=os.path.dirname(os.path.abspath(path)))
    return Character(data.get("name", "character"), data.get("display_name") or data.get("name", ""), poses,
                     data.get("default_pose") or next(iter(poses)), path, scene=bool(data.get("scene")))


# ---------------------------------------------------------------------- 内置备用角色
_GHOST = [
    "        .-~~~~~~-.        ",
    "      .'          '.      ",
    "     /              \\     ",
    "    |                |    ",
    "    |                |    ",
    "    |                |    ",
    "    |                |    ",
    "    |                |    ",
    "    |                |    ",
    "   /                  \\   ",
    "  |                    |  ",
    "  |                    |  ",
    "  |                    |  ",
    "   \\                  /   ",
    "    |  /\\   /\\   /\\  |    ",
    "    \\_/  \\_/  \\_/  \\_/    ",
]


def builtin_character() -> Character:
    w, h = len(_GHOST[0]), len(_GHOST)
    body = Layer("body", w, h)
    outline, fillc = 0xFF9FD8FF, 0xFF3B6EA8
    for y, row in enumerate(_GHOST):
        inside = False
        for x, ch in enumerate(row):
            if ch != " ":
                body.set(x, y, ch, outline)
        xs = [x for x, ch in enumerate(row) if ch in "|/\\.'"]
        if len(xs) >= 2 and 1 <= y <= 13:
            for x in range(xs[0] + 1, xs[-1]):
                if row[x] == " ":
                    body.set(x, y, ":" if (x + y) % 2 else ".", fillc)
    face = {"x": 6, "y": 3, "w": 14, "h": 6}
    for y in range(face["h"]):
        for x in range(face["w"]):
            body.set(face["x"] + x, face["y"] + y, ".", 0xFF5A8CC8)
    neutral = Layer("face", face["w"], face["h"])
    eye_col, mouth_col = 0xFFFFFFFF, 0xFFFFB0C8
    for ex in (2, 9):
        neutral.put_text(ex, 1, "@@@", eye_col)
        neutral.put_text(ex, 2, "@@@", eye_col)
    neutral.put_text(6, 4, "\\_/", mouth_col)
    pose = Pose("idle", w, h, body, face, neutral, [[2, 1, 3, 2], [9, 1, 3, 2]], ".", 0xFF5A8CC8, eye_col)
    return Character("ghost", "小幽灵", {"idle": pose}, "idle")


def find_character(name: str | None = None) -> Character:
    candidates = []
    if name and os.path.isfile(name):
        candidates.append(name)
    if name:
        candidates.append(os.path.join(ASSET_DIR, f"{name}.json"))
    candidates.append(os.path.join(ASSET_DIR, "maid_bust.json"))
    candidates.append(os.path.join(ASSET_DIR, "shenshen_bust.json"))
    candidates.append(os.path.join(ASSET_DIR, "shenshen.json"))
    for c in candidates:
        if os.path.isfile(c):
            try:
                return load_character(c)
            except Exception:
                continue
    return builtin_character()
