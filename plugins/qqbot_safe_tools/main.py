"""Standalone safe-tools protocol diagnostic command."""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import safe_tools


class SafeToolsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("工具协议", priority=110)
    async def status(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        yield event.plain_result(
            "Safe Tools 协议：独立解析器已加载；旧 qqbot_enhancer 仍是 marker 执行 owner。\n"
            + safe_tools.tool_instruction(["send_sticker", "fetch_webpage"])
        )
        event.stop_event()
