# 第三方声明与素材授权（THIRD_PARTY_NOTICES）

本仓库只包含扩展本身（`pet_memory/`、`ascii_pet/`、安装器及其源码）。上游桌宠的程序、代码和动画素材**不在本仓库里**，安装器只修改使用者自己电脑上已经安装好的那一份。

---

## 1. 上游项目：MerZlin/dsh-pet-indesktop（桌面大肥鱼）

- 仓库：<https://github.com/MerZlin/dsh-pet-indesktop>
- 许可证：MIT License，Copyright (c) 2026 Merzlin
- 本项目与上游的关系：
  - 两个扩展在运行时给上游桌宠打补丁（聊天管线、菜单、窗口等），使用上游的模块名和类名，但不包含上游代码。
  - `patch_src/pet_entry_patched.py` 由上游的打包入口 `packaging/pet_entry.py` 改写而来，以下几行逻辑来自上游：`--settings` 分支、`from pet.app import main`、`sys.exit(main())`。安装器内嵌的是它编译后的字节码。依照 MIT 许可，在此保留上游的版权声明，许可全文见上游仓库的 `LICENSE`。
  - 单元测试运行时需要上游的 `pet` 模块（需自行从已安装的桌宠中取得，本仓库不附带）。
- **原作者的同意**：2026-10-17，本项目作者通过哔哩哔哩私信，向上游作者（在 B 站发布桌面大肥鱼版本的 UP 主）说明了本项目：它基于上游的桌宠对话框架，增加了自动表情、人格迭代和永久记忆等功能。作者询问能否在 B 站分享进展、完成后上传到 GitHub，并承诺注明上游 GitHub 链接、不用于任何盈利目的。上游作者回复同意。本声明为转述，不是原话引用。这份同意针对上述非商业分享；第 2 节所列的角色素材另有权利人，其授权条件不因此改变。

## 2. 由上游角色「溟月」转换的素材（仅限个人非商业使用）

- 涉及的文件：
  - `ascii_pet/assets/characters/shenshen.json`
  - `ascii_pet/assets/characters/shenshen_bust.json`
  - `ascii_pet/assets/characters/shenshen_rig.json`
  - 画面里是这个角色的截图：`ascii_pet/docs/lab.png`、`ascii_pet/docs/api_happy_greeting.png`、`ascii_pet/docs/api_shy.png`、`ascii_pet/docs/api_mode3_glitch_dissolve.png`
- 来源：上游桌宠自带的角色动画素材（“深深 / 小鲸鱼”webm，上游 `assets/characters/`）。这些素材的做法和来源出自 [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)，由上游整理搬运。本项目把其中若干帧转换成了字符画数据。
- 原作：角色 OC「溟月」，**画师：上善无形**；素材由社区成员整理制作。
- 授权条件（与上游 `THIRD_PARTY_NOTICES.md` 第 2 节一致）：此类同人素材按 CC BY-NC-SA 类条款发布，**仅限个人非商业使用**；使用时必须**保留署名与来源**；**不得用于任何商业或盈利场景**。
- 本项目的代码许可证（MIT）和美术许可证（CC BY-NC 4.0）都**不适用于**上述文件。再分发或修改这些文件时，请保留本节的署名和非商业条款。

## 3. 本项目作者绘制的女仆插画与表情差分（CC BY-NC 4.0）

- 涉及的文件：
  - `ascii_pet/assets/source/`（`maid_face.jpg`、`maid_faceless.webp`、`maid_rig.json`、`face_layers_map.json`）
  - `ascii_pet/assets/characters/maid.json`、`maid_bust.json`、`maid_*.png`
  - `ascii_pet/assets/characters/face_layers/`（全部手绘五官图层、`strokes/`、`manifest.json`、`strokes.json`）
  - 画面里是这幅插画的截图：`ascii_pet/docs/maid_expressions_1.png`、`maid_expressions_2.png`、`real_pet_inline_smile.png`、`real_window_mock_happy.png`、`chat_line.png`（LINE 风格对话框里的头像）
- 作者：本项目作者 **BIGfeiyu-friend** 本人绘制（插画与表情差分，Procreate）。
- 授权：**知识共享 署名-非商业性使用 4.0 国际（CC BY-NC 4.0）**，© 2026 BIGfeiyu-friend。可以复制、分发、修改，但必须署名、注明许可并说明是否改动，不得用于商业目的。详见 [LICENSE-ART.md](LICENSE-ART.md)。这些素材不适用代码的 MIT 许可。

## 4. 运行时用到、但不在本仓库里的组件

扩展运行在上游桌宠自带的 Python 3.11 环境里，用到的第三方库都由上游打包和分发，本仓库不附带。它们是 PySide6 / Qt（LGPL）、Pillow、keyring、certifi，许可见上游的 `THIRD_PARTY_NOTICES.md` 和各项目自己的说明。

其它说明：

- 字体：字符画优先使用系统里已有的 `OCR A Extended`，没有就用 Consolas 等系统字体。本仓库不包含任何字体文件。
- `ascii_pet/room.ico` 由本项目代码生成（`pet_ascii/room_viewer.py` 的 `make_icon`）。
- 画面风格参考了 MV《world.search(you)》的字符画效果，没有使用其中的任何素材。
- 对话窗口的“LINE 风格”皮肤（`pet_ascii/chat_line.py`）只在配色和布局上模仿 LINE 手机版的聊天界面，全部由代码绘制，没有使用 LINE 的任何图标、图片、字体或其他素材。头像是本项目自带女仆插画的表情差分（见第 3 节）。

## 5. 商标

DeepSeek、Spotify、LINE、Windows 等名称是各自所有者的商标。本项目与它们没有任何关联，也没有得到它们的认可。
