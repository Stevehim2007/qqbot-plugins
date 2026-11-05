# qqbot_core_reply

`qqbot_core_reply` 是最终七插件架构里**唯一的主回复 / LLM owner**。它自包含、
只依赖 Python 标准库和 AstrBot >= 4.29 公共插件 API；不导入 `qqbot_enhancer`、
`qqbot_core_full` 或任何其他新插件源码。

## 架构与所有权

- **主回复**：`on_message` 只在本插件决定回复时产生一条结果；显式 @ / 私聊必回，
  非 @ 群消息按 `reply_probability`、群/用户冷却、最短长度与纯图片/表情策略决定。
  ambient 开启时只走 ambient 决策，不再回落到普通概率。
- **direct 模式**：本插件选择当前会话 provider，调用 `Context.llm_generate`，
  注入 system prompt / 群人格 / 提示词 profile / 当前发送者身份 / 有界短期历史；
  图片优先原生视觉，失败后走 caption provider，再以 caption 作为文本上下文生成。
  超时、空结果、无 provider、图片不可读均有单条安全兜底。
- **astrbot memory 模式**：本插件**不直接调用 LLM**。命令排除与概率决策完成后，
  只设置 wake flag、候选 extras 并交给 AstrBot 默认 Agent 一次。
- **候选 extras**：所有生成/回复候选都设置
  `qqbot_core_reply_candidate=True`、`qqbot_core_reply_allow_sticker=True`、
  `qqbot_core_reply_text=<原始消息>`；ambient 额外设置
  `qqbot_core_reply_ambient=True`。显式命令结果绝不标记候选。
- **工具 handshake**：仅通过
  `context.get_registered_star("qqbot_safe_tools")` 找到激活 star，并调用它的
  `tool_instruction()`；成功才把说明追加到 direct prompt。插件不解析、不校验、
  不剥离、不执行任何 `[[tool:...]]` marker；safe_tools 不存在时不提供协议。
- **表情装饰**：本插件不自行追加表情；只通过候选 extras 交给 `qqbot_stickers`
  的结果钩子决定。
- **命令**：`/会话列表`、`/会话`（别名 `/切换会话`）、`/新会话`、
  `/删除会话`（别名 `/会话删除`）、`/option`（别名 `/插件用法`、`/用法`）
  以及本地 `/唱歌`（别名 `/点歌`）。命令一律先 `should_call_llm(True)`、
  不发候选，最后 `stop_event()`；会话后端不支持时安全降级。
- **人格桥**：仅在 AstrBot PersonaManager / ConversationManager 公共 API 可用时
  创建/绑定专用 persona；不可用时保留 AstrBot 全局人格或明确报告不支持。

## 配置

插件配置由 AstrBot 在 `Star.__init__(context, config)` 注入；本插件不会把
`Context.get_config()`（全局配置）误当成插件配置。`_conf_schema.json` 映射了旧
`qqbot_enhancer` 的核心回复键：`memory_mode`、`reply_probability`、
`group_cooldown_seconds`、`user_cooldown_seconds`、`min_message_chars`、
`max_input_chars` / `max_message_chars`、`max_reply_chars`、指令前缀继承/排除、
ambient 组、`system_prompt`、`llm_timeout_seconds`、视觉/caption、群人格与提示词
profile、群聊身份/历史、会话命令、`/option`、本地点歌。

## 回滚

1. 在 WebUI 停用 `qqbot_core_reply`。
2. 重新启用旧 `qqbot_enhancer`（及其原有配置）。
3. 本插件不写旧数据目录、不改全局 provider/model/key、不推送 GitHub；停用即可
   回到旧主回复 owner。旧插件的 `songs/` 与数据目录保留供回滚。
4. 如需数据回滚，本插件只维护进程内冷却/短期历史；无持久化状态需要清理。

## 测试

```bash
cd plugins/qqbot_core_reply
python3 -m unittest discover -s tests -q
```

测试使用 fake AstrBot 模块/event/provider/context，覆盖概率边界、冷却、命令排除、
direct/memory 单次调用、候选 extras、safe_tools handshake、人格/身份、
会话命令、图片 fallback、无 marker 解析、schema 与无旧插件 import。
