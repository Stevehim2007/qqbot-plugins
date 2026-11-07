"""Minimal AstrBot 4.29 stubs and fake registered plugin APIs for unit tests."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PARENT = PLUGIN_ROOT.parent
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))


class Plain:
    def __init__(self, text=""):
        self.text = str(text)

    def __repr__(self):
        return f"Plain({self.text!r})"


class Image:
    def __init__(self, url=""):
        self.url = str(url)


class Result:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


class Event:
    def __init__(self, *, text="", session="session-1", result=None, extras=None):
        self.message_str = text
        self.unified_msg_origin = session
        self._extras = dict(extras or {})
        self._result = result if result is not None else Result([Plain(text)])
        self.stopped = False
        self.sender_id = "10001"
        self.should_llm = None

    def get_result(self):
        return self._result

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return Result([Plain(text)])

    def chain_result(self, chain):
        return Result(chain)

    def should_call_llm(self, value):
        self.should_llm = value


class Star:
    def __init__(self, context):
        self.context = context


class Context:
    def __init__(self, metadata=None, global_config=None):
        self.metadata = dict(metadata or {})
        self.global_config = dict(global_config or {})
        self.global_config_reads = 0

    def get_registered_star(self, plugin_id):
        return self.metadata.get(plugin_id)

    def get_config(self):
        self.global_config_reads += 1
        return self.global_config


class _Filter:
    @staticmethod
    def command(*args, **kwargs):
        def decorate(function):
            function._qqbot_command = (args, kwargs)
            return function

        return decorate

    @staticmethod
    def llm_tool(*args, **kwargs):
        def decorate(function):
            function._qqbot_llm_tool = kwargs.get("name") or getattr(function, "__name__", "")
            return function

        return decorate

    @staticmethod
    def on_decorating_result(*args, **kwargs):
        def decorate(function):
            function._qqbot_decorating_priority = kwargs.get("priority")
            return function

        return decorate


filter = _Filter()


# Install a deterministic stub AstrBot before the package under test is imported.
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event_module = types.ModuleType("astrbot.api.event")
star_module = types.ModuleType("astrbot.api.star")
components_module = types.ModuleType("astrbot.api.message_components")
event_module.AstrMessageEvent = Event
event_module.filter = filter
star_module.Context = Context
star_module.Star = Star
components_module.Plain = Plain
components_module.Image = Image
api.event = event_module
api.star = star_module
api.message_components = components_module
astrbot.api = api
sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.star": star_module,
        "astrbot.api.message_components": components_module,
    }
)

from qqbot_safe_tools import main as main_module  # noqa: E402
from qqbot_safe_tools import safe_tools  # noqa: E402

SafeToolsPlugin = main_module.SafeToolsPlugin


def base_config(**overrides):
    config = {
        "enabled": True,
        "marker_enabled": True,
        "max_actions": 1,
        "allow_fetch_webpage": True,
        "allow_search_images": True,
        "allow_search_food_restaurants": True,
        "allow_submit_food_review": True,
        "allow_send_sticker": True,
        "allow_collect_sticker": True,
        "allow_list_categories": True,
        "web_fetch_cooldown_seconds": 0,
        "image_search_cooldown_seconds": 0,
        "food_search_cooldown_seconds": 0,
        "food_submit_cooldown_seconds": 0,
        "sticker_cooldown_seconds": 0,
        "collect_sticker_cooldown_seconds": 0,
        "list_categories_cooldown_seconds": 0,
        "max_keyword_chars": 100,
        "max_search_count": 1,
        "max_web_chars": 4000,
        "tool_timeout_seconds": 1,
        "max_fallback_chars": 500,
    }
    config.update(overrides)
    return config


def make_plugin(config=None, *, metadata=None, global_config=None):
    context = Context(metadata=metadata, global_config=global_config)
    plugin = SafeToolsPlugin(context=context, config=base_config() if config is None else config)
    return plugin


def register(plugin, plugin_id, **methods):
    api_obj = SimpleNamespace()
    for name, value in methods.items():
        if value is None:
            setattr(api_obj, name, None)
        elif callable(value):
            setattr(api_obj, name, value)
        else:
            setattr(api_obj, name, AsyncMock(return_value=value))
    plugin.context.metadata[plugin_id] = SimpleNamespace(
        star_cls=api_obj, activated=True
    )
    return api_obj


def register_meta(plugin, plugin_id, star_cls, *, activated=True):
    plugin.context.metadata[plugin_id] = SimpleNamespace(
        star_cls=star_cls, activated=activated
    )


def candidate_event(text, *, session="session-1", chain=None, extras=None):
    merged = {"qqbot_core_reply_candidate": True}
    merged.update(extras or {})
    result = Result(chain if chain is not None else [Plain(text)])
    return Event(text=text, session=session, result=result, extras=merged)


def plain_text(chain):
    return "".join(
        str(getattr(component, "text", "") or "")
        for component in chain
        if isinstance(component, Plain)
    )


async def decorate(plugin, event):
    await plugin.consume_safe_tools_marker(event)
    return event


def run_decorate(plugin, event):
    return asyncio.run(decorate(plugin, event))


async def collect_async_generator(agen):
    return [item async for item in agen]


def run_status(plugin, event):
    return asyncio.run(collect_async_generator(plugin.status(event)))


async def wait_forever(event, **kwargs):
    await asyncio.sleep(10)
    return event.plain_result("unreachable")
