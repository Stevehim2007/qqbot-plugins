"""Minimal AstrBot API stubs used by the stdlib-only plugin tests.

AstrBot is not installed in the CI environment.  The stubs only model the
public surface this plugin uses, so tests can exercise command registration,
event results and the safe-tools API without importing the real framework.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

COMMAND_REGISTRY: list[dict[str, Any]] = []
EVENT_HANDLERS: list[dict[str, Any]] = []


def _decorator(registry, record):
    def deco(func):
        registry.append(record)
        func.__astrbot_registration__ = record
        return func

    return deco


class _CommandFilter:
    def command(self, name, alias=None, priority=0):
        aliases = list(alias or [])
        return _decorator(
            COMMAND_REGISTRY,
            {"name": name, "aliases": aliases, "priority": priority},
        )


class _EventFilter:
    class EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP_MESSAGE"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"

    class PlatformAdapterType:
        AIOCQHTTP = 1
        QQOFFICIAL = 2
        QQOFFICIAL_WEBHOOK = 4

    def event_message_type(self, event_type, priority=0):
        return _decorator(
            EVENT_HANDLERS,
            {"event_type": event_type, "priority": priority},
        )

    def platform_adapter_type(self, adapter_type, priority=0):
        return _decorator(
            EVENT_HANDLERS,
            {"adapter_type": adapter_type, "priority": priority},
        )


class AstrMessageEvent:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


class Context:
    def __init__(self, config: Any = None):
        self._config = config or {}

    def get_config(self, *args, **kwargs):
        return self._config


class StarTools:
    @staticmethod
    def get_data_dir(*args, **kwargs):
        return str(PLUGIN_ROOT / "data")


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


@dataclass
class FakeResult:
    kind: str
    text: str = ""
    components: list[Any] = field(default_factory=list)


@dataclass
class FakeComponent:
    kind: str
    value: str

    @classmethod
    def fromURL(cls, url: str):
        return cls("image_url", str(url))

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls("image_file", str(path))


class FakeImage:
    @classmethod
    def fromURL(cls, url: str):
        return FakeComponent("image_url", str(url))

    @classmethod
    def fromFileSystem(cls, path: str):
        return FakeComponent("image_file", str(path))


class FakePlain:
    def __init__(self, text: str):
        self.text = str(text)


class FakeEvent:
    def __init__(
        self,
        message: str = "",
        *,
        private: bool = False,
        at_bot: bool = False,
        group_id: str = "10001",
        sender_id: str = "42",
        self_id: str = "999",
    ):
        self.message_str = message
        self.unified_msg_origin = f"group:{group_id}" if not private else f"private:{sender_id}"
        self._private = private
        self._at_bot = at_bot
        self.is_at_or_wake_command = bool(at_bot)
        self.is_wake = bool(at_bot)
        self._group_id = "" if private else group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self.messages: list[Any] = []
        self.stopped = False
        self.llm_suppressed = False
        self.extras: dict[str, Any] = {}
        self.tracked_files: list[str] = []

    def plain_result(self, text: str):
        return FakeResult("plain", text=str(text))

    def chain_result(self, components):
        return FakeResult("chain", components=list(components))

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value: bool):
        self.llm_suppressed = bool(value)

    def is_private_chat(self):
        return self._private

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return list(self.messages)

    def track_temporary_local_file(self, path: str):
        self.tracked_files.append(str(path))

    def set_extra(self, key: str, value: Any):
        self.extras[key] = value

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)


def install_astrbot_stub() -> None:
    """Install idempotent stubs for the subset of ``astrbot`` used here."""

    if getattr(sys.modules.get("astrbot"), "__qqbot_test_stub__", False):
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__qqbot_test_stub__ = True
    api = types.ModuleType("astrbot.api")
    api.__path__ = []  # mark as package

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = AstrMessageEvent
    event_filter = _EventFilter()
    event_filter.command = _CommandFilter().command
    event_module.filter = event_filter

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = Context
    star_module.Star = Star
    star_module.StarTools = StarTools

    logger_module = types.ModuleType("astrbot.api.logger")
    logger_instance = _Logger()
    logger_module.logger = logger_instance
    for name in ("debug", "info", "warning", "error", "exception"):
        setattr(logger_module, name, getattr(logger_instance, name))

    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.Image = FakeImage
    components_module.Plain = FakePlain

    api.event = event_module
    api.star = star_module
    api.logger = logger_module
    api.message_components = components_module
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module
    sys.modules["astrbot.api.logger"] = logger_module
    sys.modules["astrbot.api.message_components"] = components_module


def import_main():
    install_astrbot_stub()
    import importlib

    if "main" in sys.modules:
        return importlib.reload(sys.modules["main"])
    return importlib.import_module("main")
