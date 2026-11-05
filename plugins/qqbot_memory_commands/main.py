"""Standalone AstrBot plugin: deterministic ``/记忆`` management commands.

The handler never invokes a chat model.  It reads settings from the plugin
config injected by AstrBot (``star_cls(context=..., config=...)``), converts
them with the pure :func:`memory_commands.options_from_config` helper, then
delegates all memory work to the optional AstrBot native memory service.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import memory_commands


def _plugin_config(config: Any) -> Mapping[str, Any]:
    """Return the injected plugin config as a mapping (never global config)."""

    if isinstance(config, Mapping):
        return config
    try:
        converted = dict(config)
    except Exception:  # noqa: BLE001 - malformed injected config means disabled
        return {}
    return converted


def _event_text(event: AstrMessageEvent) -> str:
    return str(getattr(event, "message_str", "") or "")


def _stop_event(event: AstrMessageEvent) -> None:
    try:
        event.stop_event()
    except Exception:  # noqa: BLE001 - shutdown must not mask the reply path
        pass


class MemoryCommandsPlugin(Star):
    """One handler registered for ``/记忆`` and its ``/记忆管理`` alias."""

    def __init__(
        self,
        context: Context,
        config: Mapping[str, Any] | None = None,
    ):
        super().__init__(context)
        self.config = _plugin_config(config)
        # Alias kept for callers that used the first standalone package draft.
        self.plugin_config = self.config

    @filter.command("记忆", alias={"记忆管理"}, priority=110)
    async def memory_command(self, event: AstrMessageEvent):
        """Answer one memory management command without the main model."""

        reply = memory_commands.HANDLER_FAILED_REPLY
        try:
            # AstrBot's True flag means "handled; do not call the default LLM".
            event.should_call_llm(True)
            options = memory_commands.options_from_config(self.config)
            reply = memory_commands.handle_text(
                text=_event_text(event),
                prefixes=("/",),
                event=event,
                context=self.context,
                options=options,
                core=memory_commands.load_core_memory(),
            )
        except Exception:  # noqa: BLE001 - bounded Chinese failure, no traceback
            reply = memory_commands.HANDLER_FAILED_REPLY

        try:
            yield event.plain_result(reply)
        except Exception:  # noqa: BLE001 - transport failure cannot be re-sent
            pass
        finally:
            _stop_event(event)
