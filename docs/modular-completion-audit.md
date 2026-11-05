# QQ Bot 插件最终模块化审计（2026-11-05）

## 审计范围

- 本地 staging：`.qqbot-plugins-staging`，分支基线 `4899457`。
- GitHub：`Stevehim2007/qqbot-plugins`，实际可见 release：
  - `v1.0.0`：`qqbot_web_reader-1.0.0.zip`、`qqbot_image_search-1.0.0.zip`
  - `v2.0.0`：`qqbot_food-2.0.0.zip`、`qqbot_stickers-2.0.0.zip`、`qqbot_safe_tools-2.0.0.zip`
  - `v2.0.1`：`qqbot_safe_tools-2.0.1.zip`
  - `v3.0.0`：`qqbot_memory_commands-3.0.0.zip`
  - `v3.1.0` / `v3.1.1`：`qqbot_core_reply-3.1.0/3.1.1.zip`
  - `v4.0.0`：`qqbot_core_full-4.0.0.zip`（过渡兼容包，最终必须删除）
- 旧功能基线：`plugins/qqbot_enhancer`（23 个文件、17,176 行、265 个配置字段）。
- 本地 `artifacts/*.zip` 的顶层目录和 SHA256 已与 catalog 交叉校验；旧版本 zip 保留作回滚，不参与最终 catalog。

## 结论

现有 v1/v2/v3 包解决了“能独立安装、能被 AstrBot 加载”的问题，但没有完成运行时能力迁移。除 `qqbot_memory_commands` 的纯逻辑模块外，其余包的主入口仍是单命令/只读演示实现。`qqbot_core_full` 只是旧增强的完整副本，不能作为最终架构。

最终运行时只能存在以下七个插件：

```text
qqbot_core_reply
qqbot_safe_tools
qqbot_web_reader
qqbot_image_search
qqbot_food
qqbot_stickers
qqbot_memory_commands
```

## 分包缺失项

### qqbot_web_reader 1.0.0

已有：`/网页`、基础 HTML 去标签、单次 DNS 私网检查、1 MiB/4,000 字上限。

缺失：

- 旧 `web_fetch.py` 的逐跳重定向校验、DNS 解析固定/重绑定防护、压缩/编码/Content-Type 处理、可见文本解析、细粒度超时/大小/错误原因。
- 给 `qqbot_safe_tools` 的 `fetch_webpage` 执行接口；当前 marker 只能由旧增强执行。
- 自然语言/自动网页上下文、冷却、配置化上限和可诊断错误。
- 足以证明 SSRF、超大响应、重定向攻击被拒绝的独立测试。

### qqbot_image_search 1.0.0

已有：`/图片搜索`、Bing `murl` 正则提取、最多构造 3 个远程图片组件。

缺失：

- 旧实现的百度 JSON 第二来源、候选解析/去重/相关性排序、结果数量配置。
- 下载上限、重定向/私网/Content-Type/图片 magic 校验、远程 URL 降级、发送前完整性验证。
- 自然语言找图、群/用户冷却和明确的失败回复。
- 给 `qqbot_safe_tools` 的 `search_images` 执行接口。
- 网络/解析/安全/数量边界的独立测试。

### qqbot_food 2.0.0

已有：`food.py` 几乎完整的纯工具库 + 只读 `/美食搜索`（最多 3 条）。

缺失：

- 旧 `/food`、`/吃什么`、`/饭店` 命令语义，名称/标签/位置/排序/limit 查询和歧义处理。
- 群位置上下文、位置路径解析/合并、标签搜索。
- 提交完整链路：固定 Food API、缺字段追问、当前日期、用户名/QQ 密码注入、位置搜索、POST `/api/upload`、错误码/中文错误映射。
- 确认/草稿/幂等/重复提交/无 sender 失败保护、提交冷却和草稿 TTL。
- 自然语言“明确添加”门禁和 Food 相关性门禁（不调用主模型）。
- 给 `qqbot_safe_tools` 的查询/提交执行接口。
- 旧 67 个 `food_*`/`food_submit_*`/`food_search_*` 配置字段的迁移映射与测试。

### qqbot_stickers 2.0.0

已有：完整 `sticker_store.py` 数据层 + 只读 `/表情库` 文件计数。

缺失：

- 收藏、分类、列表、发送、索引和状态命令；自然语言收藏/发送/重分类。
- 旧图片 magic/尺寸/大小校验、SHA 去重、多分类、语义标签/意义/语气、权限修复、路径逃逸防护的入口接线。
- 单写者/崩溃一致性保证（当前只有 store 原子写，无跨入口锁和 OS 锁）。
- 从旧 `plugin_data/qqbot_enhancer/collected_stickers` 的可重复、非破坏迁移。
- 主回复后的单张表情装饰钩子，以及对 safe_tools `send_sticker`/`collect_sticker`/`list_sticker_categories` 的稳定接口。
- 旧 36 个 `sticker_*`、30 个 `collected_*`、8 个 `auto_collect_*` 配置项映射。

### qqbot_safe_tools 2.0.1

已有：完整 marker 解析/校验库 + `/工具协议` 只读诊断。

缺失：

- marker 的实际唯一所有权：`on_decorating_result` 消费、一次性处理、剥离 marker、停止后续传播。
- 权限/白名单/每回复一个动作/各工具 allow 开关的执行期约束。
- `search_images`、`send_sticker`、`collect_sticker`、`list_sticker_categories`、`fetch_webpage`、`search_food_restaurants`、`submit_food_review` 的真实调度。
- 通过新插件稳定接口调度，而不是依赖 `qqbot_enhancer` 内部状态。
- 冷却、失败回退、禁止解析工具结果/网页文本中的二次 marker。
- 可测试的工具目录、缺失插件 fail-closed、异常不泄漏。

### qqbot_memory_commands 3.0.0

已有：完整 `memory_commands.py`（解析、授权、scope、删除/忘记、tidy 预览、脱敏）+ 只读 `/记忆管理` 接线。

缺失：

- 旧 `/记忆` 命令及完整动作接线；当前 `/记忆管理` 参数改写脆弱。
- legacy `memory_commands_*` 配置名兼容。
- 真实 AstrBot 记忆 service 的 mock 级 E2E、scope 隔离、destructive gate、失败不谎报。
- 命令注册/停止 LLM 的独立测试；不得启动聊天模型。
- 文档化记忆补丁依赖、备份/回滚和授权边界。

### qqbot_core_reply 3.1.1

已有：默认关闭的 @/私聊基础 provider 调用、slash 排除、简单超时/错误文本。

缺失：

- 群聊概率、群/用户冷却、最小/最大长度、纯图片/表情策略、ambient 群聊模式。
- 自定义/继承 wake prefix 的完整命令排除，保证命令不被核心吞掉或重复回复。
- direct 模式的会话历史、人格/profile、群人格、发送者身份、系统提示、图片/视觉/caption、provider 选择与回退。
- AstrBot memory 模式：只放行默认 Agent 一次，不直接调用模型。
- `/会话列表`、`/会话`、`/新会话`、`/删除会话`、`/option` 及旧本地 `/唱歌` 能力。
- 单一回复 owner 标记、与 safe_tools/stickers 的结果装饰握手，确保无双重回复。
- 旧 `reply_*`、`ambient_*`、`conversation_*`、`group_persona_*`、`group_identity_*`、`prompt_*`、`llm_*`、视觉/song/option 配置映射。

### qqbot_core_full 4.0.0 / qqbot_enhancer

二者均为过渡：`qqbot_core_full` 是旧增强完整副本，`qqbot_enhancer` 是生产历史插件。最终必须从插件目录、catalog、manifest、打包产物和运行时依赖中移除；只保留其配置/data 备份供迁移和回滚。

## 最终运行时所有权契约

```text
qqbot_core_reply       唯一主回复/LLM owner；命令排除；会话/人格；不放 marker 解析器
qqbot_safe_tools        唯一 marker 解析/校验/调度 owner；不调用 LLM
qqbot_web_reader        网页读取 + safe tool fetch 实现
qqbot_image_search      图片搜索 + safe tool search_images 实现
qqbot_food              Food 查询/草稿/确认/提交/幂等 + safe tool 实现
qqbot_stickers          收藏/索引/发送/装饰 + safe tool 实现 + 旧数据迁移
qqbot_memory_commands   /记忆 scope/授权；不调用 LLM
```

跨插件调用只允许通过 `context.get_registered_star(plugin_id).star_cls` 暴露的稳定方法；不允许 import 旧插件或读取其他插件源码目录。

## 阶段验收门槛

1. 每个插件目录内 `python3 -m unittest discover -s tests -q` 全绿。
2. staging 根目录全量测试全绿。
3. ZIP 顶层目录、版本、SHA256、metadata、schema 校验通过。
4. bot-4/qa-core 独立实例加载七个插件，无 `qqbot_enhancer`/`qqbot_core_full` 加载。
5. 在 qa-core 做命令、marker、记忆、主回复和单回复 owner 的 E2E。
6. 通过后才允许生产 bot-1 切换；切换前保留 compose/config/data/plugin_data 备份和回滚说明。
