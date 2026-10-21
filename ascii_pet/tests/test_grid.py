from pet_ascii.grid import CLEAR, WIDE_CONT, Layer, compose, sanitize_char


def test_compose_order_transparent_and_clear():
    a = Layer("a", 4, 1, z=0)
    a.put_text(0, 0, "abcd", 0xFF111111)
    b = Layer("b", 4, 1, z=1)
    b.set(1, 0, "X", 0xFF222222)      # 覆盖
    b.set(2, 0, CLEAR)                # 主动清空
    chars, colors = compose([b, a], 4, 1)  # 顺序无关，按 z 叠加
    assert chars == ["a", "X", CLEAR, "d"]
    assert colors[1] == 0xFF222222 and colors[0] == 0xFF111111


def test_compose_clips_offsets_outside_canvas():
    a = Layer("a", 3, 3)
    a.fill("#", 0xFF000001)
    a.x, a.y = -1, 2
    chars, _ = compose([a], 4, 4)
    assert chars[2 * 4 + 0] == "#" and chars[2 * 4 + 1] == "#" and chars[2 * 4 + 2] == CLEAR
    assert chars[3 * 4 + 0] == "#"
    assert chars.count("#") == 4


def test_apply_patch_transparency_blank_and_mask():
    L = Layer("f", 5, 2)
    L.fill("o", 0xFF000000)
    n = L.apply_patch(0, 0, ["`A B", "zz"])            # ` 和空格都透明
    assert n == 4
    assert L.rows(transparent="?", clear="_") == ["oAoBo", "zzooo"]
    L.apply_patch(0, 0, ["  ", ""], blank="clear")       # 空格 = 清空
    assert L.get(0, 0)[0] == CLEAR and L.get(1, 0)[0] == CLEAR
    L.apply_patch(2, 1, ["xyz"], mask=["#.#"])           # 遮罩：. 保持不变
    assert L.rows(clear="_")[1] == "zzxoz"


def test_apply_patch_clips_without_touching_other_cells():
    L = Layer("f", 3, 3)
    L.apply_patch(2, 2, ["abc", "def"])
    assert L.get(2, 2)[0] == "a"
    assert L.occupied() == 1


def test_wide_chars_take_two_cells_and_are_rejected_in_patches():
    L = Layer("ui", 6, 1)
    end = L.put_text(0, 0, "你a", 0xFFFFFFFF)
    assert end == 3
    assert L.chars[:3] == ["你", WIDE_CONT, "a"]
    assert sanitize_char("你") == "?"
    assert sanitize_char("♥") == "♥"
    assert sanitize_char("\x07") == " "
