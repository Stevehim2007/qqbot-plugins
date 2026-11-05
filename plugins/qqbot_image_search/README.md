# qqbot_image_search 2.0.0

独立、仅使用 Python 标准库的图片搜索插件（AstrBot >= 4.29）。
不导入旧 `qqbot_enhancer`，不调用任何聊天模型，不执行网页脚本。

## 命令

```text
/找图 关键词              # 规范搜索命令
/找图 下一页               # 读取同一 actor/scope/query 的下一页
/找图 收藏 RESULT_ID       # 生成收藏计划并交给 qqbot_stickers
/搜图 关键词               # 别名
/搜图片 关键词             # 别名
/image 关键词              # 别名
/图片搜索 关键词           # 兼容旧命令
```

`@机器人` 或私聊中也可以直接说「帮我找三张猫的图片」等明确请求；
自然语言识别完全由确定性正则/规则完成，不使用 LLM。

## ABI v1 provider

实现固定方法：

```python
async def image_search(request_context, args) -> dict
async def image_next_page(request_context, args) -> dict
async def image_collect_plan(request_context, args) -> dict
```

- `image_search` 只允许 `{"query": str, "count": int}`；不接受任意 URL/host/header；
- `image_next_page` 只允许 `{"cursor": str}`；
- `image_collect_plan` 只允许 `{"result_id": str}`；
- 均返回 ToolResult 契约字典，且 provider 路径只返回数据/计划，不发送消息、不下载图片。

插件通过共享 AstrBot `Context` 上的 `_qqbot_service_registry_v1` 注册
`image.search` / `image.next_page` / `image.collect_plan`。注册与 ready 分离，
`initialize()` 后 ready；`terminate()` 注销 capability 并清空搜索/结果缓存。
重复 owner/provider、ABI 不符和 owner_epoch 不符都会拒绝。

## Safe-tools API（向后兼容）

```python
async def tool_search_images(self, event, *, keyword: str, count: int = 1) -> result | None
```

`count` 会被限制在 1..`search.result_limit`；返回 AstrBot result 或安全失败提示；
任何异常均被捕获，不会向调用方抛出。工具路径同样不调用模型。

## 分页与游标

搜索结果按 `paging.page_size` 分页，`cache.ttl_seconds` / `cache.max_entries`
控制缓存。游标为 HMAC 签名的 opaque cursor，绑定 actor、scope、query、offset、
页大小与过期时间；篡改、换 actor、换 scope、换 query 均拒绝。缓存过期时
`stale=true`，缓存被淘汰时返回稳定 `cache_miss`。

## 收藏计划（不写表情 DB）

`image_collect_plan` 只做三件事：

1. 校验 `RESULT_ID` 属于当前 actor/scope 且未过期；
2. 构造不可变 plan（asset URL、来源查询、标题、idempotency key、scope、policy）；
3. 通过共享 Context 查找 `sticker.collect_plan` provider 并把 plan 路由给
   `qqbot_stickers`；本身绝不写表情资产/索引 DB。

`qqbot_stickers` 未注册或未 ready 时 fail-closed 返回 `dependency_missing`，
`data.routed=false`、`data.downloaded=false`，不产生任何副作用。

## 安全流程

1. Bing Images HTML 主源解析，失败或不足时回退百度图片 JSON（可配置关闭）；
2. 标题/描述相关性过滤 + URL 去重 + 候选数量上限；
3. 搜索页/图片请求逐跳校验公开 IP 与重定向，连接 DNS 固定；
4. 下载限制候选数、超时、单张字节，并校验 `Content-Type`、文件魔数与大小；
5. 命令路径下载成功用本地临时文件，否则 `Comp.Image.fromURL` 安全兜底；
6. 每次命令只产生一条回复，最多 `search.result_limit` 张图片。

## 配置

旧扁平键全部保留；规范点号组同时存在：

- `commands.aliases`
- `search.source_id` / `search.safe_mode` / `search.result_limit` /
  `search.timeout_seconds` / `search.max_candidates` / `search.max_keyword_chars`
- `media.max_bytes` / `media.allowed_types` /
  `media.download_timeout_seconds` / `media.download_attempts`
- `paging.page_size`
- `cache.ttl_seconds` / `cache.max_entries`
- `policy.policy_id` / `policy.owner_epoch`

旧键 `max_results`、`max_keyword_chars`、`timeout_seconds`、`max_response_bytes`、
`safe_search`、`download_*`、冷却与各类失败提示继续兼容。

## 测试

```bash
cd plugins/qqbot_image_search
python3 -m unittest discover -s tests -q
```
