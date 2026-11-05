"""Minimal standalone core reply prototype.

Default disabled. This package is intentionally not a drop-in replacement for
the legacy enhancer until the old command/owner gate is installed.
"""
from __future__ import annotations

import asyncio

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


def _cfg(context: Context) -> dict:
    try:
        raw = context.get_config() if hasattr(context, "get_config") else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


class CoreReplyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=100)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        cfg = _cfg(self.context)
        if not bool(cfg.get("enabled", False)):
            return
        try:
            # Never turn an explicit slash command into a model reply; the
            # command-owning plugin must stay the single handler.
            if str(getattr(event, "message_str", "") or "").lstrip().startswith("/"):
                return
            if not (event.is_private_chat() or event.is_at_or_wake_command):
                return
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            if provider is None:
                yield event.plain_result("核心回复：没有可用模型 Provider。")
                event.stop_event()
                return
            provider_id = str(provider.meta().id)
            timeout = max(10, min(int(cfg.get("llm_timeout_seconds", 60) or 60), 180))
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=str(getattr(event, "message_str", "") or ""),
                    system_prompt=str(cfg.get("system_prompt", "") or ""),
                ),
                timeout=timeout,
            )
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text:
                yield event.plain_result(text)
            event.stop_event()
        except Exception:
            yield event.plain_result("核心回复失败。")
            event.stop_event()
