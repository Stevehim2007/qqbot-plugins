# qqbot_image_search 2.0.0

独立、仅使用 Python 标准库的图片搜索插件（AstrBot >= 4.29）。
不导入旧 `qqbot_enhancer`，不调用任何聊天模型，不执行网页脚本。

## 命令

```text
/图片搜索 关键词      # 别名 /搜图片
/找图 关键词          # 别名 /搜图
```

`@机器人` 或私聊中也可以直接说「帮我找三张猫的图片」等明确请求；
自然语言识别完全由确定性正则/规则完成，不使用 LLM。

## Safe-tools API

```python
async def tool_search_images(self, event, *, keyword: str, count: int = 1) -> result | None
```

- `count` 会被限制在 1..`max_results`；
- 返回一个 AstrBot result，或安全失败提示；
- 任何异常均被捕获，不会向调用方抛出。

## 搜索与安全流程

1. 解析 Bing Images HTML `m="..."` JSON 图片元数据；
2. Bing 失败或相关候选不足时，回退百度图片 JSON（可配置关闭）；
3. 标题/描述相关性过滤 + URL 去重 + 候选数量上限；
4. 每个搜索页/图片请求都做公开 IP 校验与逐跳重定向校验，连接固定到已校验 DNS 地址；
5. 图片下载限制候选数、单张字节数和超时，并同时校验 `Content-Type`、文件魔数与大小；
6. 下载成功用本地临时文件发送；本地全部/部分失败时用 `Comp.Image.fromURL` 安全远程兜底；
7. 每次只产生一条回复，最多 `max_results` 张图片。

## 配置

见 `_conf_schema.json`：`max_results`、`max_keyword_chars`、`timeout_seconds`、
`max_response_bytes`、`max_candidates`、`max_redirects`、`download_*`、
`group_cooldown_seconds`、`user_cooldown_seconds`、`relevance_*`、
`baidu_fallback_enabled`、`safe_search`、`user_agent` 以及各类失败提示。

## 测试

```bash
cd plugins/qqbot_image_search
python3 -m unittest discover -s tests -q
```
