# qqbot_web_reader 2.0.0

独立的、仅使用 Python 标准库的公开网页读取插件（AstrBot >= 4.29）。
不导入旧 `qqbot_enhancer`，不启动浏览器、不执行 JavaScript、不带 Cookie/Authorization。

## 命令

```text
/网页 https://example.com
/读网页 https://example.com
```

只接受公开网页；默认仅 HTTPS（`allow_http` 默认关闭）。读取结果以纯文本返回标题和正文，
不调用聊天模型。

## Safe-tools API

```python
async def tool_fetch_webpage(self, event, *, url: str, max_chars: int = 0) -> result | None
```

- 返回 AstrBot result，或安全失败提示；
- `max_chars=0` 使用配置的 `max_chars`；
- 任何异常都会被捕获，不会向调用方抛出；
- 不返回 URL 细节/异常文本，失败原因映射为固定中文提示。

## 安全边界

- HTTPS-only 可通过配置显式放开 HTTP；
- 拒绝 URL 凭据、localhost、内网域名后缀、私有/回环/link-local/保留/非 global IP；
- 每一跳都重新做 DNS 解析与 URL 校验，并将 TCP 连接固定到已校验 IP，防止 DNS rebinding；
- 最多 `max_redirects` 次跳转（0-5）；
- 响应体 `max_bytes`、正文 `max_chars`、总超时 `timeout_seconds` 均硬限制；
- 只接受 HTML/XHTML/纯文本/XML/JSON；拒绝压缩编码与非 200；
- HTML 转可见纯文本，并对抓取内容中的 `[[tool:` 标记做中和处理。

## 配置

见 `_conf_schema.json`：`max_chars`、`max_bytes`、`timeout_seconds`、`max_redirects`、
`user_agent`、`allow_http`、`enabled` 以及各类安全失败提示。

## 测试

```bash
cd plugins/qqbot_web_reader
python3 -m unittest discover -s tests -q
```
