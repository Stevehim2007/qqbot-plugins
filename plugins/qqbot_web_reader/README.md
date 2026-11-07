# qqbot_web_reader 2.0.1

独立的、仅使用 Python 标准库的公开网页读取插件（AstrBot >= 4.29）。
不导入旧 `qqbot_enhancer`，不启动浏览器、不执行 JavaScript、不带 Cookie/Authorization。

## 命令

```text
/网页读取 https://example.com      # 规范命令
/网页读取 下一页                   # 读取同一 actor/scope/URL 的下一页
/网页 HTTPS://example.com           # 别名
/读网页 https://example.com         # 别名
/web https://example.com            # 别名
```

只接受公开网页；默认仅 HTTPS（`fetch.allow_http` / 旧 `allow_http` 默认关闭）。
结果以纯文本返回；分页游标为 HMAC 签名的 opaque cursor，绑定 actor、scope、
URL、offset 与过期时间，不能跨用户/会话重放。`paging.page_chars` 控制每页长度，
`cache.ttl_seconds` / `cache.max_entries` 控制分页缓存；缓存过期时返回
`stale=true`，缓存被淘汰时返回稳定 `cache_miss`。

## ABI v1 provider

实现固定方法：

```python
async def web_read(request_context, args) -> dict
async def web_next_page(request_context, args) -> dict
```

- `web_read` 的参数只允许 `{"url": str, "max_chars": int}`；
- `web_next_page` 的参数只允许 `{"cursor": str}`；
- 返回 ToolResult 契约字典：`status`、`call_id`、`result_type`、`data`、
  `error_code`、`retryable`、`side_effect_state`、`provider_operation_id`；
- 不接受任意 host/header/proxy 覆盖；URL 始终走安全引擎校验。

插件通过共享 AstrBot `Context` 上的 `_qqbot_service_registry_v1` 注册
`web.read` / `web.next_page`。注册与 ready 分离，`initialize()` 后 ready；
`terminate()` 注销 capability 并清空 cache/cursor。重复 owner/provider、
ABI 不符和 owner_epoch 不符都会拒绝。

## Safe-tools API（向后兼容）

```python
async def tool_fetch_webpage(self, event, *, url: str, max_chars: int = 0) -> result | None
```

返回 AstrBot result 或安全失败提示；任何异常均被捕获，不会向调用方抛出。

## 安全边界

- HTTPS-only 可通过配置显式放开 HTTP；
- 拒绝 URL 凭据、localhost、内网域名后缀、私有/回环/link-local/保留/非 global IP；
- 每一跳都重新做 DNS 解析与 URL 校验，并将 TCP 连接固定到已校验 IP，防止 DNS rebinding；
- 可选 `fetch.allowed_hosts` 主机白名单（精确或 `*.suffix`）；
- 最多 `fetch.max_redirects` 次跳转（0-5）；
- `fetch.max_bytes`、`extract.max_chars`、`fetch.timeout_seconds` 均硬限制；
- 只接受 HTML/XHTML/纯文本/XML/JSON；拒绝压缩编码与非 200；
- HTML 转可见纯文本，并对抓取内容中的 `[[tool:` 标记做中和处理。

## 配置

旧扁平键仍保留；规范点号组同时存在（AstrBot 扁平 schema 可读）：

- `commands.aliases`
- `fetch.timeout_seconds` / `fetch.max_bytes` / `fetch.max_redirects` /
  `fetch.allowed_hosts` / `fetch.allow_http` / `fetch.user_agent`
- `extract.mode` (`text` / `html_text`) / `extract.max_chars`
- `paging.page_chars`
- `cache.ttl_seconds` / `cache.max_entries`
- `policy.policy_id` / `policy.owner_epoch`

旧键 `max_chars`、`max_bytes`、`timeout_seconds`、`max_redirects`、
`allow_http`、`user_agent` 与各类失败提示继续兼容。

## 测试

```bash
cd plugins/qqbot_web_reader
python3 -m unittest discover -s tests -q
```
