# macOS 版（实验版）

> **实验版，没有在 Mac 真机上运行过。** 作者没有 Mac。这一版做过的检查：
>
> - 安装器用上游 **v4.3.4 的 Mac 版程序**（Apple 芯片）试过：去掉旧签名、改写入口、把程序段和字符串表改对，全部自检通过；装上再卸掉，主程序和签名封条能逐字节还原。
> - 扩展的全部 360 项单元测试，用 v4.3.4 的程序模块跑过，全部通过。
> - 没测过的：真机上的重新签名（`codesign`）、第一次打开时的系统提示、菜单栏图标、窗口在 Mac 上的实际样子。
>
> 遇到问题时，运行一次卸载（见下文），就能完整还原成上游原版。也欢迎提 Issue 告诉作者哪里不对。

---

## 需要什么

| 项目 | 说明 |
|---|---|
| 电脑 | **Apple 芯片（M1 / M2 / M3 …）的 Mac**。上游只发布了 Apple 芯片版（`macos-arm64`） |
| 上游桌宠 | [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop/releases) 的 **`dsh-pet-standalone-webm-chat-macos-arm64.zip`**（带 AI 对话的 WebM Chat 版）。本扩展的 Mac 版按 **v4.3.4** 制作；不带对话的 `webm` 版不能用 |
| DeepSeek API Key | 在桌宠**自己的**设置里填好（扩展沿用它） |
| python3 | **只有安装器需要**（任意 3.8 以上）。在“终端”里输入 `python3 --version` 看看；没有的话系统会弹窗提示安装“命令行开发者工具”，点“安装”即可 |

---

## 安装

1. 下载上游的 `dsh-pet-standalone-webm-chat-macos-arm64.zip`，解压后把 **`dsh-pet-standalone-webm-chat.app`** 拖进“应用程序”文件夹。先打开一次，在它的设置里填好 DeepSeek API Key，确认能正常聊天。然后从菜单栏图标**退出**桌宠。
2. 下载本仓库（GitHub 页面上 Code → Download ZIP），解压。
3. 打开“终端”，进入解压出来的 `mac` 文件夹，运行安装器：

   ```bash
   cd ~/Downloads/dsapi-pet-ascii-main/mac
   python3 install_mac.py
   ```

   （路径按你实际解压的位置改。也可以双击 `install.command`；如果系统说“无法打开，因为它来自身份不明的开发者”，就在它上面**右键 → 打开**。）

   安装器会先列出它找到的桌宠和要做的事，输入 `y` 回车确认。它会：
   - 把 `.app` 里的主程序和签名封条备份到 `~/Library/Application Support/dsh-pet-ascii/backup/`；
   - 把主程序里的启动脚本换成“先加载扩展、再原样启动”的版本，然后用系统自带的 `codesign` 重新做一次本地签名（上游的 Mac 版本来就是这种本地签名，信任等级不变）；
   - 把扩展复制到 `~/Library/Application Support/dsh-pet-ascii/`（不放进 `.app` 里面）。

4. 像平时一样打开桌宠。这时永久记忆和回复提速已经生效。
5. 双击 `mac` 文件夹里的 **`open_lab.command`** 打开开发测试台，在「显示与设置」页勾选 **「随桌宠启动字符画窗口，并与对话联动」**，然后退出并重新打开桌宠。

之后字符画窗口会随桌宠出现。日常怎么用，见仓库根目录的 [使用说明.md](../使用说明.md)（Mac 上的几处不同见下文）。

### 如果安装时提示“没有权限修改”

macOS 13 起，“终端”要修改别的 App 需要你允许：**系统设置 → 隐私与安全性 → App 管理**，打开“终端”，然后重新运行安装器。

### 如果第一次打开桌宠时提示“无法验证开发者”

重新签名后，系统可能会像第一次下载时那样再提示一次。**右键点 `.app` → 打开**，或者在 **系统设置 → 隐私与安全性** 底部点“仍要打开”。也可以在安装时加 `--remove-quarantine`，让安装器去掉 `.app` 的下载隔离标记（效果相同）。

---

## 卸载

```bash
cd ~/Downloads/dsapi-pet-ascii-main/mac
python3 install_mac.py --uninstall
```

（或者双击 `uninstall.command`。）它会把备份的原版主程序和签名封条放回 `.app`，删除 `~/Library/Application Support/dsh-pet-ascii` 里的扩展。

你的数据不会被删除，不需要的话可以自己删：`~/Library/Application Support/dsh-pet-standalone-webm-chat/` 下的 `memory/`（永久记忆）和 `ascii_pet/`（字符画设置、表情库、房间）。注意同一个文件夹里也有桌宠本体的会话和设置，请只删这两个。

`python3 install_mac.py --status` 可以查看当前状态，`--dry-run` 只检查、不写入。

### 桌宠更新以后

上游更新（换了新的 `.app`）后，扩展就不会再加载，桌宠照常以原版运行。**更新后请重新运行一次安装器。** 你的记忆、设置和房间不受影响。

---

## Mac 上和 Windows 版不一样的地方

| 功能 | Mac 上 |
|---|---|
| 打开测试台 / 房间窗口 | 双击 `mac` 文件夹里的 `open_lab.command` / `open_room.command`（Windows 是 `.bat`） |
| 房间里图片的文字识别 | **暂不支持**（Windows 用的是系统自带的 OCR）。她仍然能看到图片的字符画、颜色，以及能看图的副模型的描述 |
| 防多开 | 不需要：macOS 本来就只开一只，再点图标会切到已经开着的那只 |
| 字体 | 没有 OCR A Extended、微软雅黑时，自动改用 Menlo、苹方，字符画的样子会略有不同 |
| 调字符大小 | Windows 是 Ctrl + 滚轮，Mac 上是 **⌘ + 滚轮** |
| 托盘 | 在屏幕顶部的菜单栏里 |
| Spotify | 电脑上的 Spotify 没开时，用系统的 `open` 打开它 |
| 数据位置 | `~/Library/Application Support/dsh-pet-standalone-webm-chat/`（Windows 是 `%APPDATA%`） |
