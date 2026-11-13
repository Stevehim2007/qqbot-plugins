# qqbot-plugins

独立于 QQ Bot Studio 主仓库的插件清单与发布包。插件默认只使用 Python 标准库，不接受 pip 安装和 post-install 脚本。

当前正式发布：**v4.3.1**。

当前测试发布：**v4.4.0-beta.1**（V1：per-group capability policy + auxiliary LLM budget ledger）。

## 插件

- `qqbot_core_reply`：唯一主回复/LLM owner，负责 direct/astrbot memory、人格、会话、工具目录 handshake。
- `qqbot_safe_tools`：marker 与 native 工具的唯一安全入口，负责授权、确认、幂等、冷却和审计。
- `qqbot_web_reader`：`/网页 <url>` 安全读取公开 HTTPS 网页。
- `qqbot_image_search`：`/图片搜索 <关键词>` 搜索并返回图片。
- `qqbot_food`：Food 查询、地点/标签解析、草稿确认、评价提交，并暴露 `tool_search_restaurants`。
- `qqbot_stickers`：表情收藏、分类、索引、发送计划与 AI 装饰/收藏。
- `qqbot_memory_commands`：`/记忆` 与记忆 scope/会话/人格管理。

## 通用工具

每个 provider 插件可在目录内提供 `tools.json`，声明模型可调用的工具名、方法、参数、operation、权限和冷却。`qqbot_safe_tools` 启动时聚合这些声明，同时提供 marker 和 `qqbot_safe_tool` native bridge。

## 发布

每个插件单独打 ZIP，ZIP 唯一顶层目录必须等于插件 ID；清单记录 SHA256、大小和 GitHub Release URL。

## 平台 ID 语义

- `qq_official`：群/用户 ID 是 32 位 openid（例如 `6D71B9EAD547FAF9F8970A1C31C22043`）。
- `aiocqhttp` / OneBot：群/用户 ID 是数字 QQ 群号/QQ 号，由 NapCat 等第三方网关负责登录。

两类 ID 不互相转换，也不能混填。群策略、群白名单、记忆 scope、人格分组、Food 群位置均按当前实例平台填写。
