"""Bounded optional LLM assist for memory-card phase summaries."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any


class AuxGuard:
    """Small process-local budget/circuit breaker; never blocks chat on failure."""

    def __init__(self, *, daily_limit: int = 50, circuit_errors: int = 3, cooldown: float = 1800.0) -> None:
        self.daily_limit = max(0, int(daily_limit))
        self.circuit_errors = max(1, int(circuit_errors))
        self.cooldown = max(0.0, float(cooldown))
        self._day = ""
        self._calls = 0
        self._errors = 0
        self._open_until = 0.0

    def allow(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        day = time.strftime("%Y-%m-%d", time.localtime(current))
        if day != self._day:
            self._day, self._calls, self._errors, self._open_until = day, 0, 0, 0.0
        if self._open_until and current < self._open_until:
            return False
        if self._calls >= self.daily_limit:
            return False
        return True

    def record(self, ok: bool, now: float | None = None) -> None:
        current = time.time() if now is None else now
        self._calls += 1
        if ok:
            self._errors = 0
            return
        self._errors += 1
        if self._errors >= self.circuit_errors:
            self._open_until = current + self.cooldown


async def phase_summary(
    context: Any,
    messages: list[str],
    *,
    timeout: float = 8.0,
    max_chars: int = 160,
) -> str:
    """Return a short factual phase summary, or '' when unavailable/invalid."""

    try:
        getter = getattr(context, "get_using_provider", None)
        generator = getattr(context, "llm_generate", None)
        if not callable(getter) or not callable(generator):
            return ""
        provider = getter()
        if inspect.isawaitable(provider):
            provider = await provider
        provider_id = str(
            getattr(provider, "provider_id", "") or getattr(provider, "id", "") or ""
        ).strip()
        if not provider_id:
            return ""
        system = (
            "你是记忆收尾助手。只从用户消息中提取以后仍有用的事实、偏好、约定、计划；"
            "不要记录寒暄、问句、助手的话、猜测、图片或临时情绪。"
            "输出一句不超过 100 字的中文摘要，不要使用 markdown、引号或列表。"
            "没有可保留事实时只输出空字符串。"
        )
        pending = generator(
            chat_provider_id=provider_id,
            prompt="用户消息：\n" + "\n".join(f"- {m}" for m in messages[-12:]),
            system_prompt=system,
            max_tokens=160,
            temperature=0.1,
        )
        response = await asyncio.wait_for(pending, timeout=timeout) if inspect.isawaitable(pending) else pending
        raw = (
            getattr(response, "completion_text", None)
            or getattr(response, "text", None)
            or getattr(response, "_completion_text", None)
            or response
        )
        text = " ".join(str(raw or "").split()).strip()
        if not text or any(token in text for token in ("[[NO_REPLY]]", "mqqapi://", "```")):
            return ""
        return text[: max(1, int(max_chars))].rstrip()
    except Exception:  # noqa: BLE001 - optional assist must never break chat
        return ""
