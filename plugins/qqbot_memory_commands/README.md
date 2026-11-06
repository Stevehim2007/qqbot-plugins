# qqbot_memory_commands 4.0.1

面向 AstrBot `>=4.29,<5` 的独立长期记忆管理命令插件。命令完全确定性地读取、搜索和清理
AstrBot 原生记忆服务；**不调用聊天模型**，也不导入 `qqbot_enhancer`、`qqbot_core_full`
或其他插件源码。实现只依赖 Python 标准库和 AstrBot 公共 API。

## 命令

`/记忆` 与 `/记忆管理` 是同一个 handler 的两个命令名，不会重复注册或重复回复。

| 命令 | 作用 |
| --- | --- |
| `/记忆`、`/记忆 帮助` | 显示用法 |
| `/记忆 状态` | 当前作用域条数、策略、抽取/注入/FTS 状态 |
| `/记忆 搜索 <关键词>` | 当前作用域内按命中数、更新时间倒序显示 |
| `/记忆 删除 <id>` | 删除当前作用域中已确认存在的一条记忆 |
| `/记忆 忘记 本群` | 显式清空当前群 group 作用域（私聊拒绝） |
| `/记忆 忘记 我的` | 显式清空发送者自己的 user 作用域（任何会话都只指向发送者本人） |
| `/记忆 整理` | 只读 dry-run：重复、近重复、低价值、过期预览 |

删除/忘记不会猜 target：忘记必须写 `本群` 或 `我的`。服务未返回确认时不会显示“已删除/已忘记”。

## 作用域与隐私

- **群聊**：状态、搜索、整理和删除只读取/校验**当前群**的 `group` 作用域。
  `忘记 本群` 也只清理该 group 作用域；`忘记 我的` 是显式例外，只清发送者自己的 `user`
  作用域。看不到其他群，也看不到其他成员的私聊记忆。
- **私聊**：只使用发送者自己的 `user` 作用域；`忘记 本群` 会被拒绝。
- 每次读取都调用 `service.export(scope_type, scope_id)` 的显式两参数形式，**从不调用
  `export_all()`**，也不会把无 scope 的数据库内容拼进回复。
- 删除前会先导出当前作用域并校验 id 属于该作用域；不存在的 id 不调用删除。
- 回复中单条内容经过密钥清洗与截断，整条回复再受 `max_reply_chars` 限制。

## 权限

只有以下发送者可以使用：

1. AstrBot 管理员（`event.is_admin()`/管理员角色）；
2. QQ 群主或群管理员；
3. `allow_user_ids` 显式允许名单。

删除和忘记在以上权限之外还需要 `destructive_enabled=true`。命令关闭、权限不足、
记忆服务未打补丁、服务不可用、`memory.enable=false` 时都会给出明确的中文状态，不会伪造成功。

## 配置

配置由 AstrBot 以 `MemoryCommandsPlugin(context=..., config=plugin_config)` 注入；插件读取
注入的 **plugin config**，不把 `context.get_config()` 的全局配置当作插件设置。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关；默认关闭 |
| `destructive_enabled` | `false` | 删除/忘记第二道开关 |
| `allow_user_ids` | `""` | 逗号分隔或列表 |
| `max_results` | `5` | 限制到 1..10 |
| `content_chars` | `80` | 限制到 20..200 |
| `max_reply_chars` | `900` | 限制到 200..2000 |

纯函数 `memory_commands.options_from_config(mapping)` 负责类型容错和上下限裁剪；
不再读取 `memory_commands_*` 旧键。

## AstrBot 长期记忆补丁前提

本插件需要 AstrBot 安装并启用原生长期记忆补丁。补丁应提供：

- 可被可选导入的 `astrbot.core.memory`；
- `get_memory_service(config)` 与 `build_context(event)`；
- 绑定当前会话的 `group_scope`/`user_scope` 和 `is_private`；
- memory service 的 `enabled`、`export(scope_type, scope_id)`、`delete(id)`、
  `forget(scope_type, scope_id)`、`policy_for(memory_context)`。

导入失败、补丁 API 缺失、context 无法绑定作用域或服务不可用时，命令会明确回复不可用；
不会退回普通会话历史，也不会调用默认聊天模型。

## 无模型调用与回复边界

handler 在每次执行时先调用 `event.should_call_llm(True)`（AstrBot 中表示该消息已处理、
阻止默认 LLM），并在 `finally` 中调用 `event.stop_event()`。所有异常都被收敛为有界中文
失败提示，不输出 traceback、数据库路径或记忆正文。

## 升级、备份与回滚

**本插件不迁移、移动或删除记忆数据。** `astrbot_memory.db` 的 scope 格式必须保持不变；
升级只改插件配置键的读取兼容方式，因此不需要数据迁移工具。

升级前建议：

1. 停止 AstrBot；
2. 备份 `data/cmd_config.json`、插件配置以及实际安装路径下的 `astrbot_memory.db`
   （常见为 `data/astrbot_memory.db`，以部署为准）；
3. 记录当前 `enabled`、`destructive_enabled` 和允许名单，先以默认关闭状态安装 4.0.1；
4. 启动后确认 patch 可用，再按需开启总开关和破坏性开关。

回滚：

1. 停止 AstrBot；
2. 恢复旧版插件目录/配置和上述备份；
3. 不要为了回滚插件而删除记忆库；只有发生数据损坏的灾难恢复才恢复数据库备份。

## 测试

在插件目录运行：

```bash
python3 -m unittest discover -s tests -q
```

测试使用 fake AstrBot memory core/service，覆盖命令解析、群/私聊作用域隔离、管理员/群主/
允许名单、破坏性开关、状态与搜索排序/截断、删除和忘记无假成功、整理 dry-run、补丁缺失/
服务关闭、脱敏、旧配置回退、单 handler 双命令名、配置 schema 与 README 契约。
