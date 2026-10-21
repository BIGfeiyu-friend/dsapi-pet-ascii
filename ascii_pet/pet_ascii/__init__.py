"""pet_ascii —— 由 DeepSeek 驱动、可自主创作字符画表情与动作的桌宠视觉模块。

设计原则：AI 负责想象和创造，程序负责保持结构、连续性与流畅播放。

模块：
  grid        字符网格、图层、补丁与合成（坐标/透明/清空规则）
  protocol    JSON 动画协议的解析与校验
  character   角色素材（姿势、面部区域、脸底、默认五官）
  convert     视频帧/图片 → 彩色字符画角色素材
  engine      本地动画引擎（关键帧、补间、眨眼、呼吸、特效、视觉状态）
  effects     本地特效（含睡眠用的 Z、鼻涕泡、夜色）；background 字符背景
  sleep       睡眠模式：无互动后入睡、即时编排的睡眠动画、吵醒反应
  parts       可创造的组件与可编辑骨架；visual_check 本地视觉检查；creative 创作记忆与探索
  ai/experiment 自主视觉实验；ai/vision 后台视觉评价；metrics 耗时记录
  render      Qt 字符渲染器；window 透明桌宠窗口；lab 开发测试台
  ai/         DeepSeek 客户端、提示词、调度器、Mock
  runtime     组装运行时；integration 接入现有桌宠
  persona     副人格（v0.9）：条目、候选池、规则审查、门槛、速度上限、分级生效、衰减、提示词呈现；
              persona_evidence 只读证据库（桌宠记忆库的原始对话）；ai/persona_ai 夜间回顾与独立审核
  room        虚拟房间（v1.0）：她自己的文件夹（沙盒、回收站、原始日志、礼物、快照）；room_life 生活循环
              （什么时候进去、一步一步做、无聊与联想）；room_memory 房间记忆（短期 → 印象 → 长期）；
              ai/room_ai 她的决定与记忆整理的提示词；room_viewer 主人的“房间”窗口（独立进程，单向玻璃）；
              chat_memory（v1.1）只读桌宠记忆库，让房间里的她也记得聊天
"""

__version__ = "1.4.2"
