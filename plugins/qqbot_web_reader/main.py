"""Public HTTPS reader plugin; stdlib only."""
from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.parse
import urllib.request

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

USER_AGENT = "QQBotStudio-WebReader/1.0"


def _public_https(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只支持公开 HTTPS URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("域名无法解析") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("禁止访问内网/回环地址")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def _html_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


class WebReaderPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("网页", alias={"读网页"}, priority=110)
    async def read_web(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = str(getattr(event, "message_str", "") or "")
        parts = text.removeprefix("/网页").removeprefix("/读网页").strip().split(maxsplit=1)
        if not parts:
            yield event.plain_result("用法：/网页 https://example.com")
            event.stop_event()
            return
        try:
            from urllib.parse import urlsplit
            url = _public_https(parts[0])
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=12) as response:
                data = response.read(1024 * 1024 + 1)
                final = response.geturl()
                body = _html_text(data.decode("utf-8", errors="replace"))
            if len(data) > 1024 * 1024:
                raise ValueError("网页超过大小限制")
            yield event.plain_result(f"网页：{final}\n\n{body[:4000]}")
        except Exception as exc:
            yield event.plain_result(f"网页读取失败：{type(exc).__name__}")
        event.stop_event()
