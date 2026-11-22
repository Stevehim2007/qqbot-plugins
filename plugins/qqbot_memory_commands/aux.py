"""Bounded optional LLM assist for memory-card phase summaries."""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any

try:
    from . import aux_ai
except ImportError:  # AstrBot loads plugin modules as top-level
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import aux_ai  # type: ignore[no-redef]


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
    plugin_data_dir: Path | None = None,
) -> str:
    """Return a short factual phase summary, or '' when unavailable/invalid."""

    try:
        system = (
            "你是记忆收尾助手。只从用户消息中提取以后仍有用的事实、偏好、约定、计划；"
            "不要记录寒暄、问句、助手的话、猜测、图片或临时情绪。"
            "输出一句不超过 100 字的中文摘要，不要使用 markdown、引号或列表。"
            "没有可保留事实时只输出空字符串。"
        )
        if plugin_data_dir is not None:
            dedicated = await aux_ai.generate(
                plugin_data_dir=plugin_data_dir,
                system_prompt=system,
                prompt="用户消息：\n" + "\n".join(f"- {m}" for m in messages[-12:]),
                max_tokens=160,
                temperature=0.1,
                timeout=timeout,
            )
            if dedicated:
                text = " ".join(dedicated.split()).strip()
                if text and not any(token in text for token in ("[[NO_REPLY]]", "mqqapi://", "```")):
                    return text[: max(1, int(max_chars))].rstrip()
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


async def episode_summary(
    context: Any,
    messages: list[str],
    *,
    timeout: float = 10.0,
    max_chars: int = 220,
    plugin_data_dir: Path | None = None,
) -> str:
    """Return a short narrative episode, or '' when unavailable/invalid."""

    try:
        system = (
            "你是对话记忆助手，只写一段 120–200 字的中文叙事摘要。"
            "写清楚发生了什么、用户表达的事实/偏好/情绪、以及结果或未完成事项；"
            "不要逐句复述聊天记录，不要写助手内心戏，不要编造，不要 markdown、引号或列表。"
            "只有确实发生了值得以后回忆的事件、决定、结果或未完成事项时才写；"
            "寒暄、一次性闲聊、普通问答、没有后续价值的内容只输出 [[NONE]]。"
        )
        if plugin_data_dir is not None:
            dedicated = await aux_ai.generate(
                plugin_data_dir=plugin_data_dir,
                system_prompt=system,
                prompt="最近对话：\n" + "\n".join(f"- {m}" for m in messages[-12:]),
                max_tokens=320,
                temperature=0.3,
                timeout=timeout,
            )
            if dedicated:
                text = " ".join(dedicated.split()).strip()
                if len(text) >= 20 and not any(
                    token in text for token in ("[[NONE]]", "[[NO_REPLY]]", "mqqapi://", "ComponentType.")
                ):
                    return text[: max(1, int(max_chars))].rstrip()
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
            "你是对话记忆助手，只写一段 120–200 字的中文叙事摘要。"
            "写清楚发生了什么、用户表达的事实/偏好/情绪、以及结果或未完成事项；"
            "不要逐句复述聊天记录，不要写助手内心戏，不要编造，不要 markdown、引号或列表。"
            "只有确实发生了值得以后回忆的事件、决定、结果或未完成事项时才写；"
            "寒暄、一次性闲聊、普通问答、没有后续价值的内容只输出 [[NONE]]。"
        )
        pending = generator(
            chat_provider_id=provider_id,
            prompt="最近对话：\n" + "\n".join(f"- {m}" for m in messages[-12:]),
            system_prompt=system,
            max_tokens=320,
            temperature=0.3,
        )
        response = await asyncio.wait_for(pending, timeout=timeout) if inspect.isawaitable(pending) else pending
        raw = (
            getattr(response, "completion_text", None)
            or getattr(response, "text", None)
            or getattr(response, "_completion_text", None)
            or response
        )
        text = " ".join(str(raw or "").replace("```", " ").split()).strip().strip('"“”')
        if len(text) < 20 or any(
            token in text for token in ("[[NONE]]", "[[NO_REPLY]]", "mqqapi://", "ComponentType.")
        ):
            return ""
        return text[: max(1, int(max_chars))].rstrip()
    except Exception:  # noqa: BLE001 - optional assist must never break chat
        return ""
