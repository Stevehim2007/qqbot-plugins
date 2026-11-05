"""Shared test helpers; imports the fake AstrBot before the plugin."""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from fake_astrbot import (  # type: ignore
        FakeContext,
        FakeConversation,
        FakeConversationManager,
        FakeEvent,
        FakePersonaManager,
        FakeProvider,
        FakeResult,
        FakeStarMeta,
        collect,
    )
except ImportError:  # pragma: no cover - package-style invocation
    from tests.fake_astrbot import (  # type: ignore
        FakeContext,
        FakeConversation,
        FakeConversationManager,
        FakeEvent,
        FakePersonaManager,
        FakeProvider,
        FakeStarMeta,
        collect,
    )

from fake_astrbot import install as install_fake_astrbot  # noqa: E402

install_fake_astrbot()

import main as core_main  # noqa: E402
from config import DEFAULTS  # noqa: E402


def make_config(**overrides):
    config = copy.deepcopy(DEFAULTS)
    config.update(overrides)
    return config


def make_plugin(
    *,
    config: dict | None = None,
    context=None,
    overrides: dict | None = None,
    clock=None,
    rng=None,
):
    merged = make_config(**(overrides or {}))
    if config is not None:
        merged.update(config)
    ctx = context if context is not None else FakeContext()
    plugin = core_main.CoreReplyPlugin(ctx, config=merged)
    if clock is not None:
        plugin._clock = clock
    if rng is not None:
        plugin._rng = rng
    return plugin, ctx, merged


async def collect_event(plugin, event):
    return await collect(plugin.on_message(event))


async def collect_command(plugin, method_name: str, event):
    method = getattr(plugin, method_name)
    return await collect(method(event))


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedRng:
    """Deterministic random source: random() pops, choice() cycles."""

    def __init__(self, values=None, choice_index: int = 0) -> None:
        self.values = list(values or [])
        self.choice_index = choice_index
        self.random_calls = 0

    def random(self) -> float:
        self.random_calls += 1
        if not self.values:
            return 0.5
        return float(self.values.pop(0))

    def choice(self, sequence):
        items = list(sequence)
        if not items:
            raise ValueError("empty sequence")
        value = items[self.choice_index % len(items)]
        self.choice_index += 1
        return value


def plain_text(result) -> str:
    return getattr(result, "text", "")


def extras(event) -> dict:
    return dict(getattr(event, "extras", {}) or {})


def make_request_context(**overrides):
    import contracts

    payload = {
        "protocol_version": contracts.PROTOCOL_VERSION,
        "instance_id": "inst-1",
        "platform": "aiocqhttp",
        "bot_id": "bot-1",
        "actor_id": "actor-1",
        "conversation_id": "conv-1",
        "persona_id": "persona-1",
        "source_message_id": "msg-1",
        "turn_id": "turn-1",
        "capability": "reply.turn",
        "requested_scope": {"kind": "group", "group_id": "123"},
        "owner_epoch": 1,
        "deadline": 0.0,
    }
    payload.update(overrides)
    return contracts.RequestContext(**payload)
