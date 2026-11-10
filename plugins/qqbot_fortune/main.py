"""Deterministic Arknights-style daily fortune; no LLM calls."""
from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .fortune import generate_fortune
except ImportError:  # AstrBot loads plugin modules as top-level
    from fortune import generate_fortune

PLUGIN_NAME = "qqbot_fortune"


@register(PLUGIN_NAME, "QQ Bot Studio", "不调用大模型的每日运势", "1.0.2")
class FortunePlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config_obj = config if isinstance(config, dict) else {}
        self._last: dict[str, float] = {}

    def allow_unmentioned_command(self) -> bool:
        return self._bool("allow_unmentioned_command", False)

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config_obj.get(key, default)
        return bool(value) if isinstance(value, bool) else default

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config_obj.get(key, default))
        except (TypeError, ValueError):
            return default

    @filter.command("每日运势", alias={"运势", "方舟运势"}, priority=110)
    async def fortune_command(self, event: AstrMessageEvent):
        if not self._bool("enabled", True):
            event.stop_event()
            return
        user_id = str(event.get_sender_id() or "anonymous")
        cooldown = max(0, self._int("cooldown_seconds", 10))
        now = time.monotonic()
        last = self._last.get(user_id, 0.0)
        if cooldown and now - last < cooldown:
            remain = int(cooldown - (now - last)) + 1
            yield event.plain_result(f"运势查询冷却中，{remain} 秒后再来。")
            event.stop_event()
            return
        self._last[user_id] = now
        try:
            text = generate_fortune(
                user_id=user_id,
                display_name=str(event.get_sender_name() or ""),
                show_operator=self._bool("show_operator", True),
            )
        except Exception:
            logger.exception("qqbot_fortune generate failed")
            text = "今天的运势暂时算不出来，稍后再试。"
        yield event.plain_result(text)
        event.stop_event()
