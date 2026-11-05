"""Standalone memory management command; old monolith remains the reply owner."""
from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import memory_commands


def _cfg(context: Context) -> dict:
    try:
        raw = context.get_config() if hasattr(context, "get_config") else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _ids(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(raw or "").split(",") if item.strip())


class MemoryCommandsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("记忆管理", priority=110)
    async def memory(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        cfg = _cfg(self.context)
        text = str(getattr(event, "message_str", "") or "")
        text = "/记忆" + text[len("/记忆管理"):] if text.startswith("/记忆管理") else text
        options = memory_commands.Options(
            enabled=bool(cfg.get("enabled", True)),
            destructive_enabled=bool(cfg.get("destructive_enabled", False)),
            allow_user_ids=_ids(cfg.get("allow_user_ids", "")),
            max_results=max(1, min(int(cfg.get("max_results", 5) or 5), 10)),
        )
        try:
            reply = memory_commands.handle_text(
                text=text,
                prefixes=("/",),
                event=event,
                context=self.context,
                options=options,
                core=memory_commands.load_core_memory(),
            )
            yield event.plain_result(reply)
        except Exception:
            yield event.plain_result("记忆管理命令执行失败。")
        event.stop_event()
