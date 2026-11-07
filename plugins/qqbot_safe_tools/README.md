# qqbot_safe_tools 3.0.1

`qqbot_safe_tools` 是七插件运行时中**唯一的 `[[tool:...]]` marker owner**。
它不调用 LLM，也不解析用户消息或工具返回文本；它只检查由 `qqbot_core_reply`
标记为候选的直接回复链，完成解析、校验、权限检查、冷却与一次跨插件调度。

## 所有权边界

- `qqbot_core_reply` 生成候选回复时设置
  `event.set_extra("qqbot_core_reply_candidate", True)`，但绝不解析 marker。
- 本插件只注册一个消费者：
  `@filter.on_decorating_result(priority=200)`。
- 仅当候选标记为真、且 `qqbot_safe_tools_processed` 尚未设置时处理。
- 一旦检测到 marker，无论成功、无效、权限拒绝还是插件异常，都：
  - 移除 marker；
  - 设置 `qqbot_safe_tools_processed=True`；
  - 设置 `qqbot_core_reply_allow_sticker=False`；
  - 用工具结果或“清理后的文本 + 中文原因”替换整条 chain；
  - `stop_event()`，保证同一条回复不会被再次消费。
- 工具结果中的网页正文/列表文本属于不可信数据。本插件把它作为最终输出处理，
  **不会递归解析**其中的 `[[tool:...]]`；事件终止握手也会阻止 sticker 装饰器重读。

`qqbot_safe_tools` 只通过 `context.get_registered_star(...).star_cls` 获取其他
插件实例并调用下列声明方法，不 import 任何其他插件源码：

```python
# qqbot_web_reader
async def tool_fetch_webpage(self, event, *, url: str, max_chars: int = 0): ...

# qqbot_image_search
async def tool_search_images(self, event, *, keyword: str, count: int = 1): ...

# qqbot_food
async def tool_search_restaurants(self, event, args: dict[str, str], clean_text: str): ...
async def tool_submit_food_review(self, event, args: dict[str, str], clean_text: str): ...

# qqbot_stickers
async def tool_send_sticker(self, event, *, category="", emotion="", tag=""): ...
async def tool_collect_sticker(self, event, *, category=""): ...
async def tool_list_categories(self, event): ...
```

接口返回 AstrBot result 或 `None`。插件缺失、实例未激活、方法不存在、返回空结果、
超时或抛出异常时，本插件都 fail closed：不泄露异常原文，只输出有长度上限的中文原因。

## 安全模型

安全边界的核心是“单入口、固定白名单、纯文本解析、无动态执行、失败即关闭”：

1. 只有本插件的 decorating hook 消费 marker，而且只消费 core_reply 候选链，
   绝不扫描用户消息、网页正文、工具结果或命令输出。
2. marker 参数由确定性解析器处理，未知工具/参数、重复参数、残缺标记、多个动作
   全部拒绝；不会把参数变成文件路径、URL（fetch 例外且再校验）或命令。
3. 跨插件调用只允许上述固定 `tool_*` 方法，方法缺失或实例未激活即拒绝。
4. 单次调用有超时，同一会话串行执行，并有按工具冷却；异常文本不会进入 QQ 回复。
5. 禁止 `eval`、`exec`、`subprocess`、shell 和动态导入；本插件不调用任何 LLM。

## Marker 与校验

```text
[[tool:fetch_webpage url="https://example.com/article" max_chars=3000]]
[[tool:search_images keyword="猫咪" count=1]]
[[tool:send_sticker emotion="happy"]]
[[tool:collect_sticker category="收藏"]]
[[tool:list_sticker_categories()]]
[[tool:search_food_restaurants tag="火锅" location="南关区" limit=3]]
[[tool:submit_food_review restaurant="店名" location="位置" taste="4.5" value="3.5" comment="评价" tags="火锅,夜宵"]]
```

- marker 名必须在固定白名单内；未知工具直接拒绝。
- 参数解析器识别双引号、单引号和无空白裸值，拒绝重复参数、未知参数、
  未配对的引号、参数间粘连和超长参数体。
- 每条回复最多一个动作；出现多个 marker、混合残缺 marker 或无效参数时全部拒绝。
- 搜索结果、图片关键词、表情分类使用字段白名单、长度限制和危险引用检查。
  通用字段拒绝 URL、文件路径、反斜杠、控制字符和 shell 元字符。
- `fetch_webpage` 是唯一允许 URL 参数的工具，URL 仍须通过包内
  `web_fetch.validate_fetch_url()`：只允许公开 HTTP(S)、拒绝凭据/内网/回环目标。
- 食物自由文本使用包内校验器，拒绝 URL、路径、控制字符和命令语法；
  Food 地址分隔符 `/` 保留给地址层级，不参与本地文件访问。
- 图片数量、网页字符数、关键词长度、Food 结果数、冷却时间和超时均可配置。
- 发送/收藏/搜索/网页/Food/列表等工具分别按会话独立冷却。
- 同一会话的工具调用通过 `asyncio.Lock` 串行调度；单次调用有
  `tool_timeout_seconds` 超时保护。
- 无 `eval`、`exec`、`subprocess`、shell 或动态 import；没有 LLM 调用。

## 插件工具声明与通用调用

工具目录不再只能写死在 safe_tools。插件根目录可以放 `tools.json`：

```json
{
  "schema_version": 1,
  "plugin_id": "qqbot_example",
  "tools": [
    {
      "name": "example_lookup",
      "method": "tool_lookup",
      "description": "查询示例数据。",
      "parameters": {"query": {"type": "string", "required": true}},
      "allow_key": "allow_example_lookup",
      "cooldown_key": "example_lookup_cooldown_seconds",
      "cooldown_default": 5,
      "operation": "example.lookup",
      "call_style": "kwargs",
      "marker_example": "[[tool:example_lookup query=\"x\"]]"
    }
  ]
}
```

safe_tools 在启动时扫描已注册插件目录，合并 `tools.json` 与内置 7 个工具；无文件时回退内置表。新增插件不需要改 core_reply，也不需要把插件名写进 core_reply。`operation` 用于 ABI 模式；没有对应 operation 的实验工具可以走 marker/原生调用的直接路径。

safe_tools 同时注册一个 AstrBot 原生 function tool `qqbot_safe_tool(tool_name, args_json)`。模型在 function-calling 模式下可直接提交工具名和 JSON 参数，由同一套权限、冷却、参数校验与调度逻辑执行；`marker_enabled` 关闭时原生调用仍可用。

## 配置

配置由 AstrBot 以 `config` 注入到
`SafeToolsPlugin(context, config=None)`，不会误读 `context.get_config()` 的
全局配置。`_conf_schema.json` 包含：

- 总开关 `enabled`、`marker_enabled`、硬上限 `max_actions`；
- 七个 `allow_*` 工具开关；
- 各工具冷却、关键词/图片/网页/回退长度、调用超时；
- 每种拒绝/失败场景的中文回退原因。

`/工具协议` 是不调用 LLM 的诊断命令，显示开关、启用工具、允许开关和依赖插件
状态，并附带当前可下发的工具语法目录。

## 测试

```bash
cd plugins/qqbot_safe_tools
python3 -m unittest discover -s tests -q
```

测试使用 stub AstrBot 与 fake 注册插件 API，覆盖 malformed/fuzz marker、重复/未知
参数、单动作限制、权限、全部 dispatcher 的成功/`None`/异常、marker 清理、
candidate-only 装饰、工具结果不可重解析、冷却/串行调度、中文 fail-closed 回退、
schema 键完整性与禁止危险调用。
