# qqbot_core_reply 4.0.1

`qqbot_core_reply` 是最终七插件架构里**唯一的主回复 / LLM owner**。它自包含、
只依赖 Python 标准库和 AstrBot >= 4.29 公共插件 API；不导入 `qqbot_enhancer`、
`qqbot_core_full` 或任何其他新插件源码。新安装默认停用（`enabled=false`），
必须在显式交接配置后启用。

## 架构与所有权

`main.py` 只做 AstrBot 注册/适配；业务逻辑拆分到：

- `participation.py`：私聊/@/概率/群允许拒绝/ambient 参与策略
- `command_exclusion.py`：全部命令/别名确定性解析与聊天排除
- `cooldown.py`：群/用户冷却原子槽位
- `conversation.py`：AstrBot ConversationManager 会话命令与人格绑定
- `persona.py`：人格/profile/身份上下文/有界历史 prompt
- `model_gateway.py`：唯一 provider/LLM 网关、视觉/caption、complete-output 回调
- `reply_pipeline.py`：单 turn 控制器、候选 extras、Direct/memory 编排与 ABI 服务
- `state.py`：turn claim、reply lease/delivery key、cooldown 与短期状态
- `contracts.py`：ABI v1 DTO/错误码/共享 Context registry
- `commands.py`：`/回复`、`/冷却`、`/option`、本地点歌的确定性命令服务

### 唯一模型控制器

- direct 模式只通过 `model_gateway.py` 调用 `Context.llm_generate`。
- 每个 turn 有唯一 `turn_id`、`delivery_key` 与 reply lease；同
  `conversation+persona` 并发由 `concurrency.per_conversation=1` 拒绝第二个
  turn（`turn_conflict`），不会产生第二次模型调用。
- `delivery_unknown`/`outcome_unknown` 后同一 turn 只返回契约错误，不盲目重发。
- memory 模式**不直接调用 LLM**：设置 wake flag、候选 extras 和 turn 上下文后，
  只交给 AstrBot 默认 Agent 一次。默认 Agent 的完整输出由一个 decorating adapter
  按 `turn_id + model_round` fingerprint 调用 safe_tools 一次，绝不二次解析。

### 完整输出与 safe_tools

- direct 模式等模型完整输出后调用
  `qqbot_safe_tools.process_model_output(request_context, full_model_output)` 一次；
  渲染返回的 `ReplyPlan`，或使用安全 fallback。
- 本插件不解析、不校验、不剥离、不执行任何 `[[tool:...]]` marker。
- `tool_instruction()` 仅通过 `context.get_registered_star("qqbot_safe_tools")`
  动态获取；safe_tools 不存在时不提供 marker 协议。
- 校验 `owner_epoch`、`turn_id`、`call_id` 与 callback；不合法结果不会变成群消息。

### 候选 extras

所有生成/回复候选设置：

- `qqbot_core_reply_candidate=True`
- `qqbot_core_reply_allow_sticker=True`
- `qqbot_core_reply_text=<原始消息>`
- ambient 时额外 `qqbot_core_reply_ambient=True`

显式命令结果绝不标记候选。表情装饰由 `qqbot_stickers` 的结果钩子决定，本插件
不自行附加图片/表情。

## ABI v1 provider

共享 Context registry（`context.qqbot_service_registry`，同名别名
`qqbot_services`/`qqbot_contract_registry`）登记 `core_reply` provider。固定方法：

```text
reply_status
reply_configure_plan
reply_configure_commit
reply_turn_run
reply_cancel_turn
```

每个方法接收 `RequestContext`（dict-compatible）+ `args`，返回
`ContractResponse(status, data, error_code)`，不返回裸文本。重复 owner 报
`duplicate_owner`，ABI 不符报 `abi_mismatch`；未启用的 provider 对 turn/写操作
fail-closed 为 `not_ready`。插件 `terminate()` 注销 provider 并取消自有任务。

## 命令

- `/回复 状态|设置|启用|停用`，别名 `/reply`
- `/冷却 状态`，别名 `/cooldown`
- `/会话列表`
- `/会话 <名称>`，别名 `/切换会话`
- `/新会话 <名称> [提示词标题]`
- `/删除会话 <名称|短ID>`，别名 `/会话删除`
- `/option`，别名 `/插件用法`、`/用法`
- `/唱歌 [关键词]`，别名 `/点歌`

未知子命令返回终结中文错误；所有命令先 `should_call_llm(True)`、不发候选、
最后 `stop_event()`。会话后端不支持时安全降级。

## 配置

插件配置由 AstrBot 在 `Star.__init__(context, config)` 注入；本插件不会把
`Context.get_config()`（全局配置）误当成插件配置。`_conf_schema.json` 同时保留
旧 `qqbot_enhancer` 核心键并加入 canonical 等价组：

- `participation.*`、`cooldown.*`、`concurrency.per_conversation`
- `commands.exclude_aliases`、`commands.unknown_command_mode`
- `persona.default_id`、`persona.binding_mode`
- `context.max_messages`、`context.max_chars`
- `llm.provider_id`、`llm.timeout_seconds`、`llm.tool_round_limit`
- `reply.max_segments`、`reply.streaming_mode`、`reply.error_mode`
- `session.key_mode`

旧键 `reply_probability`、`group_cooldown_seconds`、`user_cooldown_seconds`、
`min_message_chars`、`max_input_chars`、`max_message_chars`、`max_reply_chars`、
`system_prompt`、`llm_timeout_seconds` 等继续映射。显式迁移旧键不会被 canonical
schema 默认值覆盖。

## 回滚

1. 在 WebUI 停用 `qqbot_core_reply`。
2. 重新启用旧 `qqbot_enhancer`（及其原有配置）。
3. 本插件不写旧数据目录、不改全局 provider/model/key、不推送 GitHub；停用即可
   回到旧主回复 owner。旧插件的 `songs/` 与数据目录保留供回滚。
4. 本插件只维护进程内 turn/冷却/短期历史；无持久化状态需要清理。`terminate()`
   会注销 shared Context provider 并取消待处理任务，旧 owner 不会被自动 fallback。

## 测试

```bash
cd plugins/qqbot_core_reply
python3 -m unittest discover -s tests -q
```

测试使用 fake AstrBot 模块/event/provider/context，覆盖概率边界、冷却、命令排除、
direct/memory 单次调用、候选 extras、safe_tools complete-output 回调、
`owner_epoch`/readiness fail-closed、ABI 重复 owner、delivery_unknown 不重发、
人格/身份、会话命令、图片 fallback、无 marker 解析、schema 与无旧插件 import。
