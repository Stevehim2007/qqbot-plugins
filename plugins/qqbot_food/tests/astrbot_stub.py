"""Minimal AstrBot test double; no real AstrBot import is needed."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path


class FakeResult:
    def __init__(self, text: str):
        self.text = str(text)
        self.type = "plain"

    def __repr__(self) -> str:
        return f"FakeResult({self.text!r})"


class FakeLogger:
    def __init__(self):
        self.records: list[str] = []

    def _record(self, level: str, args):
        self.records.append(level + " " + " ".join(str(arg) for arg in args))

    def info(self, *args):
        self._record("INFO", args)

    def warning(self, *args):
        self._record("WARNING", args)

    def error(self, *args):
        self._record("ERROR", args)

    def debug(self, *args):
        self._record("DEBUG", args)


LOGGER = FakeLogger()


class _EventMessageType:
    ALL = "ALL"
    GROUP_MESSAGE = "GROUP_MESSAGE"
    PRIVATE_MESSAGE = "PRIVATE_MESSAGE"


class _PlatformAdapterType:
    AIOCQHTTP = "AIOCQHTTP"
    QQOFFICIAL = "QQOFFICIAL"
    QQOFFICIAL_WEBHOOK = "QQOFFICIAL_WEBHOOK"


class _Filter:
    EventMessageType = _EventMessageType
    PlatformAdapterType = _PlatformAdapterType

    @staticmethod
    def command(*args, **kwargs):
        def decorator(func):
            func._qqbot_food_command = (args, kwargs)
            return func

        return decorator

    @staticmethod
    def event_message_type(*args, **kwargs):
        def decorator(func):
            func._qqbot_food_event = (args, kwargs)
            return func

        return decorator

    @staticmethod
    def platform_adapter_type(*args, **kwargs):
        def decorator(func):
            func._qqbot_food_platform = (args, kwargs)
            return func

        return decorator


class AstrMessageEvent:  # pragma: no cover - shape only
    pass


class Context:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Star:
    def __init__(self, context=None):
        self.context = context
        self.init_extra = None


class StarTools:
    @staticmethod
    def get_data_dir(plugin_name=None):
        base = os.environ.get("QQBOT_FOOD_TEST_DATA_DIR")
        if base:
            root = Path(base)
        else:
            root = Path(os.environ.get("TMPDIR", "/tmp")) / "qqbot-food-astrbot-data"
        if plugin_name:
            return str(root / str(plugin_name))
        return str(root)


def install() -> None:
    """Install ``astrbot.*`` stubs into ``sys.modules`` exactly once."""

    if getattr(sys.modules.get("astrbot"), "_qqbot_food_stub", False):
        return
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot._qqbot_food_stub = True
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = LOGGER
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    astrbot.api = api
    api.event = event_mod
    api.star = star_mod
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


class FakeEvent:
    def __init__(
        self,
        message_str: str = "",
        *,
        sender_id: str = "123456789",
        sender_name: str = "小明",
        umo: str = "aiocqhttp:GroupMessage:10001",
        group_id: str = "10001",
        group_name: str = "长春市美食交流群",
        private: bool = False,
    ):
        self.message_str = message_str
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.unified_msg_origin = umo
        self.group_id = group_id
        self.group_name = group_name
        self.private = private
        self.message_obj = types.SimpleNamespace(
            group_name=group_name,
            group=types.SimpleNamespace(name=group_name),
        )
        self.should_call_llm_values: list[bool] = []
        self.stopped = False
        self.extras: dict[str, object] = {}
        self.results: list[FakeResult] = []

    def plain_result(self, text: str) -> FakeResult:
        result = FakeResult(text)
        self.results.append(result)
        return result

    def chain_result(self, chain):
        result = FakeResult("[chain]")
        result.chain = chain
        self.results.append(result)
        return result

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return self.sender_name

    def get_group_id(self):
        return self.group_id

    def is_private_chat(self):
        return self.private

    def should_call_llm(self, value: bool):
        self.should_call_llm_values.append(bool(value))

    def stop_event(self):
        self.stopped = True

    def set_extra(self, key, value):
        self.extras[key] = value


async def collect_async(agen):
    return [item async for item in agen]


def run(coro):
    import asyncio

    return asyncio.run(coro)
