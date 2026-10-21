# pet_memory：桌宠永久记忆 + 响应提速

## 它做了什么

**提速（每条消息都生效）**
- 关闭 DeepSeek 默认开启的“思考模式”（官方默认 effort=high，而本应用会把思维链丢掉——等于白等）
- 长连接复用：连续聊天不再每次重新 TCP + TLS 握手；启动聊天时预热连接
- 真正逐字流式：原实现要攒满 4KB（约 15~20 字）才显示第一批文字
- 请求结构为 DeepSeek 上下文缓存（前缀匹配）优化，命中部分更快也更便宜

**四层记忆**
| 层 | 内容 | 何时进入模型 |
|---|---|---|
| L1 工作记忆 | 本会话最近的原文（默认最多 40 条，满了一次性推进到 20 条） | 每次请求 |
| L2 情节记忆 | 每 20 条消息或话题空闲 15 分钟后，后台整理成摘要（事件/关系/约定/待办） | 最近 3 段作为“前情提要”；更早的按相关度检索 |
| L3 语义记忆 | 后台抽取的长期事实（称呼、偏好、经历、约定、设定），按 key 去重更新 | 重要的常驻系统提示；其余按相关度检索 |
| L4 原始历史 | 每条消息永久入库（含时间、消息 ID、来源会话），清空会话也不会丢 | 只在“还记得…/上次…”这类提问时检索原话 |

聊天时**不额外调用模型**，检索是本地 SQLite 全文索引（几毫秒）。整理在后台进行，不影响回复速度。
已有的旧会话会在首次启动时自动导入并整理。

## 文件位置（%APPDATA%\dsh-pet-standalone-webm-chat\memory\）
- `memory.db`：记忆库（SQLite）
- `记忆快照.md`：自动导出的可读版本，看看它记住了什么
- `notes.md`：**你手写的永久设定**（称呼、世界观、禁忌话题…），每次聊天都会带上
- `memory_config.json`：参数，改完重启桌宠生效。常用项：
  - `deepseek_thinking`："disabled"（默认，快）/ "enabled" / "default"
  - `episode_chunk_messages`、`idle_consolidate_minutes`：整理频率
  - `consolidation_provider`：副 API（后台整理用的模型）。例如只填 `{"model": "deepseek-flash"}`，地址和 key 沿用主 API；换服务商时再加 `base_url` / `api_key`
  - `forget_deleted_sessions`：删除会话时是否同时删除相关记忆（默认 false）
- 桌宠日志 `pet-*.log` 中搜索“聊天请求”可看到首字延迟、缓存命中 token；搜索“记忆”可看到整理过程

## 停用 / 还原
- 只停用：把本文件夹 `_internal\pet_memory` 改名或删除，桌宠即按原样运行
- 完全还原：用安装目录下的 `dsh-pet-standalone-webm-chat.exe.orig` 覆盖 `dsh-pet-standalone-webm-chat.exe`
- 注意：桌宠自动更新后 exe 会被替换，需要重新安装本补丁（记忆数据不受影响）
