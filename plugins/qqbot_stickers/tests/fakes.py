"""Minimal fake AstrBot modules for unit tests.

They model only the public objects this plugin touches and deliberately keep
all event/result behavior inspectable without installing AstrBot.
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path
from typing import Any


class Plain:
    def __init__(self, text: str = ""):
        self.text = str(text)


class Face:
    def __init__(self, id: str = ""):
        self.id = id


class Image:
    def __init__(self, path: Any = None, data: bytes | None = None):
        self.path = Path(path) if path is not None else None
        self.data = data

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(path=path)

    async def convert_to_base64(self) -> str:
        data = self.data
        if data is None and self.path is not None:
            data = Path(self.path).read_bytes()
        if data is None:
            raise ValueError("no image data")
        return base64.b64encode(data).decode("ascii")


class Reply:
    def __init__(self, chain: list[Any] | None = None):
        self.chain = list(chain or [])


class Result:
    def __init__(self, chain: list[Any] | None = None):
        self.chain = list(chain or [])


class EventMessageType:
    ALL = "all"


class PlatformAdapterType:
    AIOCQHTTP = 1
    QQOFFICIAL = 2
    QQOFFICIAL_WEBHOOK = 4

    def __or__(self, other):
        return int(self) | int(other)


class Filter:
    EventMessageType = EventMessageType
    PlatformAdapterType = PlatformAdapterType

    @staticmethod
    def command(name: str, alias=None, priority=None):
        def decorate(func):
            func._command = name
            func._alias = alias
            func._priority = priority
            return func

        return decorate

    @staticmethod
    def event_message_type(kind, priority=None):
        def decorate(func):
            func._event_message_type = kind
            func._priority = priority
            return func

        return decorate

    @staticmethod
    def on_decorating_result(priority=None):
        def decorate(func):
            func._decorating_priority = priority
            return func

        return decorate

    @staticmethod
    def platform_adapter_type(kind, priority=None):
        def decorate(func):
            func._adapter_type = kind
            func._priority = priority
            return func

        return decorate


class AstrMessageEvent:
    pass


class Context:
    pass


FakeStar = None


class Star:
    def __init__(self, context):
        self.context = context
        self.config = None


class StarTools:
    data_dirs: dict[str, Path] = {}

    @classmethod
    def get_data_dir(cls, plugin_name: str | None = None):
        if plugin_name and plugin_name in cls.data_dirs:
            return cls.data_dirs[plugin_name]
        raise RuntimeError("fake data dir not configured")


class Logger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class FakeEvent:
    def __init__(
        self,
        message: str = "",
        components: list[Any] | None = None,
        *,
        unified_msg_origin: str = "fake:session",
        private: bool = False,
        at_wake: bool = False,
        result: Result | None = None,
    ):
        self.message_str = message
        self._components = list(components or [])
        self.unified_msg_origin = unified_msg_origin
        self._private = private
        self.is_at_or_wake_command = at_wake
        self.is_wake = at_wake
        self.extras: dict[str, Any] = {}
        self.result = result
        self.should_call_llm_arg = None
        self.stopped = False
        self.platform_name = "aiocqhttp"

    def get_messages(self):
        return list(self._components)

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return Result([Plain(text)])

    def chain_result(self, chain):
        return Result(chain)

    def should_call_llm(self, value):
        self.should_call_llm_arg = value

    def stop_event(self):
        self.stopped = True

    def get_platform_name(self):
        return self.platform_name

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result


class FakeContext:
    def __init__(self, global_config: dict[str, Any] | None = None):
        self.global_config = dict(global_config or {})
        self.llm_calls = 0

    def get_config(self):
        return dict(self.global_config)

    def llm_generate(self, **kwargs):
        self.llm_calls += 1
        raise AssertionError("LLM must not be called in deterministic tests")

    def get_using_provider(self, **kwargs):
        return None


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")
    for name, value in {
        "Plain": Plain,
        "Face": Face,
        "Image": Image,
        "Reply": Reply,
    }.items():
        setattr(components, name, value)
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = Filter
    star.Context = Context
    star.Star = Star
    star.StarTools = StarTools
    api.logger = Logger()
    api.event = event
    api.star = star
    api.message_components = components
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.message_components"] = components
    _installed = True
