#!/bin/bash
# 打开字符画开发测试台（需要先用 install.command 装好扩展）
APP_FILE="$HOME/Library/Application Support/dsh-pet-ascii/app_path"
if [ ! -f "$APP_FILE" ]; then
  echo "还没有安装扩展：请先运行 install.command"
  read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi
open -n "$(cat "$APP_FILE")" --args --ascii-lab
