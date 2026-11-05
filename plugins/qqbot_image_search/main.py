"""Image search command plugin; no model calls."""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

SEARCH_URL = "https://www.bing.com/images/search?q={query}"
USER_AGENT = "Mozilla/5.0 QQBotStudio-ImageSearch/1.0"


def _extract_image_urls(page: str, limit: int = 5) -> list[str]:
    urls = []
    for match in re.finditer(r"murl(?:&quot;|\\\")\s*:\s*(?:&quot;|\\\")(https?://[^\"&<>]+)", page):
        raw = html.unescape(match.group(1)).replace("\\/", "/")
        if raw not in urls:
            urls.append(raw)
        if len(urls) >= limit:
            break
    return urls


class ImageSearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("图片搜索", alias={"搜图片"}, priority=110)
    async def search(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = str(getattr(event, "message_str", "") or "")
        parts = text.removeprefix("/图片搜索").removeprefix("/搜图片").strip().split(maxsplit=1)
        if not parts:
            yield event.plain_result("用法：/图片搜索 关键词")
            event.stop_event()
            return
        try:
            query = urllib.parse.quote(parts[0][:200])
            req = urllib.request.Request(SEARCH_URL.format(query=query), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as response:
                page = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
            urls = _extract_image_urls(page, 3)
            chain = []
            for url in urls:
                try:
                    chain.append(Comp.Image.fromURL(url))
                except Exception:
                    logger.warning("image search failed to build image component")
                if len(chain) >= 3:
                    break
            if chain:
                yield event.chain_result(chain)
            else:
                yield event.plain_result("没有找到可用图片。")
        except Exception as exc:
            yield event.plain_result(f"图片搜索失败：{type(exc).__name__}")
        event.stop_event()
