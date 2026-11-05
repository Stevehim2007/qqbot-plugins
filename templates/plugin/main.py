"""Standard explicit-command plugin template; no model calls."""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class ExamplePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("示例", priority=110)
    async def example(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        try:
            yield event.plain_result("示例命令正常。")
        finally:
            event.stop_event()
