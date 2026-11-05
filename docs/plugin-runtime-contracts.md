# 插件运行时契约（最终七插件）

所有插件只依赖 Python 标准库和 AstrBot >=4.29 公共 API。禁止导入 `qqbot_enhancer`、`qqbot_core_full` 或其他插件的源码模块。跨插件发现使用：

```python
meta = context.get_registered_star("qqbot_xxx")
star = meta.star_cls if meta and meta.activated else None
```

## qqbot_core_reply

唯一主回复 owner。生成回复结果前设置：

- `event.set_extra("qqbot_core_reply_candidate", True)`
- `event.set_extra("qqbot_core_reply_allow_sticker", True|False)`
- `event.set_extra("qqbot_core_reply_text", <原始消息>)`
- `event.set_extra("qqbot_core_reply_ambient", True)` 适用于群聊 ambient/food 参与

不解析 `[[tool:...]]`。通过 `qqbot_safe_tools.tool_instruction()` 获取工具目录。

## qqbot_safe_tools

唯一 marker owner，`on_decorating_result(priority=200)`。只在 `qqbot_core_reply_candidate=True` 时消费；处理一次后设置：

- `event.set_extra("qqbot_safe_tools_processed", True)`
- `event.set_extra("qqbot_core_reply_allow_sticker", False)`

稳定接口：

```python
def tool_instruction(self, enabled_tools: list[str] | None = None) -> str
```

调度接口（目标插件提供）：

```python
# web_reader
async def tool_fetch_webpage(self, event, *, url: str, max_chars: int = 0): ...
# image_search
async def tool_search_images(self, event, *, keyword: str, count: int = 1): ...
# food
async def tool_search_restaurants(self, event, args: dict[str, str], clean_text: str): ...
async def tool_submit_food_review(self, event, args: dict[str, str], clean_text: str): ...
# stickers
async def tool_send_sticker(self, event, *, category="", emotion="", tag=""): ...
async def tool_collect_sticker(self, event, *, category=""): ...
async def tool_list_categories(self, event): ...
```

任何接口返回 AstrBot result 或 `None`，不得抛出未处理异常。

## qqbot_stickers

`on_decorating_result(priority=100)` 只装饰 core_reply candidate，且需同时满足 `allow_sticker`、未 `safe_tools_processed`、结果中没有图片/工具 marker。不得解析或执行 marker。

## qqbot_food / qqbot_memory_commands

不调用 LLM；确定性命令/草稿/存储。Food 只访问固定 Food API。Memory 只访问 AstrBot 原生 memory service。

## 数据路径

- 每个插件使用 `StarTools.get_data_dir(plugin_name=<plugin_id>)`。
- 旧 `qqbot_enhancer` 数据只做一次性、幂等、非破坏导入；导入成功后仍保留原目录供回滚。
- 任何索引写入采用临时文件 + `fsync` + `os.replace`；多入口写操作使用进程内锁 + 文件锁。
