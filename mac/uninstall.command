#!/bin/bash
# 卸载 dsh-pet-ascii 扩展：还原原版程序。你的记忆和设置不会被删除。
cd "$(dirname "$0")" || exit 1
python3 install_mac.py --uninstall "$@"
read -n 1 -s -r -p "按任意键关闭…"
