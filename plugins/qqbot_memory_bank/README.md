# qqbot_memory_bank

可检索的知识记忆库，与 `persona_corpus`（角色语料）和 `memory_cards`（事件/偏好卡片）分离。

- `memory_add`：模型确认一条长期事实后写入。
- `memory_search`：需要回忆时按 query 检索，不默认注入 system prompt。
- `memory_forget`：删除错误/过期条目。
- 可选自动提取：每 N 条用户消息调用一次辅助模型，严格 JSON；失败时用确定性规则兜底并校验。
- 数据文件：`plugin_data/qqbot_memory_bank/knowledge.db`。
