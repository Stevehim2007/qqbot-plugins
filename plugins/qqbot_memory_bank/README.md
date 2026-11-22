# qqbot_memory_bank

四层记忆的存储与召回插件，与 `persona_corpus`（角色语料）、`memory_cards`（对话卡片）和 AstrBot 原生长期记忆分开。

## 四层

- **人格记忆**：`memory_layer(layer='persona')`，按 `persona_id` 常驻注入。
- **群记忆**：`memory_layer(layer='group')`，按 `group:<platform>:<id>` 常驻注入。
- **个人 AI 记忆**：`knowledge_entry`，按 `scope + persona_id + actor_hash` 隔离；模型用 `memory_add/search/forget`。
- **共享知识库**：`knowledge_doc`，只能由 Studio 导入；模型用 `knowledge_search` 只读，每轮自动 RAG top-k。

## 工具

- `knowledge_search`：只读检索用户导入资料。
- `memory_add`：模型确认一条长期事实后写入 AI 记忆。
- `memory_search`：按 query 检索当前用户的 AI 事实。
- `memory_forget`：删除错误/过期 AI 记忆，不能删除用户导入资料。

## 自动召回

`@filter.on_llm_request()` 在每次主模型请求前注入：

- `[人格记忆]` / `[群记忆]`（每层最多 6 条/600 字）
- `[共享知识库参考]`（top-k 3）
- `[个人记忆]`（当前 actor 最多 3 条）

总预算默认 1200 字；`auto_recall_enabled=false` 可关闭，退回纯工具检索。

## 数据

`plugin_data/qqbot_memory_bank/knowledge.db`

表：`memory_layer`、`knowledge_entry`、`knowledge_doc`。不含 QQ 密码；`actor_hash` 是 `sha256(scope + sender_id)` 前 16 位。

完整说明见主项目 `docs/memory-layers.md`。
