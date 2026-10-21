#!/bin/bash
# 安装 / 更新 dsh-pet-ascii 扩展（macOS 实验版）。运行前请先从菜单栏退出桌宠。
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 python3：在终端里输入 python3，按系统提示安装“命令行开发者工具”后再运行。"
  read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi
python3 install_mac.py "$@"
read -n 1 -s -r -p "按任意键关闭…"
