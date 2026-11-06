"""Handler/config-injection tests for qqbot_memory_commands.

AstrBot is not installed in the offline test environment.  The small fake API
below mirrors only the public pieces this plugin uses: ``filter.command``,
``Star.__init__`` and the message event methods.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_DIR.parent
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))


class FakeCommandFilter:
    def __init__(self):
        self.registrations = []

    def command(self, name, alias=None, priority=0):
        def decorate(function):
            record = {
                "name": name,
                "aliases": set(alias or ()),
                "priority": priority,
                "function": function.__name__,
            }
            self.registrations.append(record)
            function.__command_registration__ = record
            return function

        return decorate


FILTER = FakeCommandFilter()


class FakeAstrMessageEvent:
    """Type marker used by the plugin import."""


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeContextMarker:
    """Type marker used by the plugin import."""


for module_name in (
    "astrbot",
    "astrbot.api",
    "astrbot.api.event",
    "astrbot.api.star",
):
    sys.modules.setdefault(module_name, types.ModuleType(module_name))
sys.modules["astrbot"].api = sys.modules["astrbot.api"]
sys.modules["astrbot"].__path__ = []
sys.modules["astrbot.api"].event = sys.modules["astrbot.api.event"]
sys.modules["astrbot.api"].star = sys.modules["astrbot.api.star"]
sys.modules["astrbot.api"].__path__ = []
sys.modules["astrbot.api.event"].AstrMessageEvent = FakeAstrMessageEvent
sys.modules["astrbot.api.event"].filter = FILTER
sys.modules["astrbot.api.star"].Context = FakeContextMarker
sys.modules["astrbot.api.star"].Star = FakeStar

MAIN = importlib.import_module("qqbot_memory_commands.main")
MC = MAIN.memory_commands

GROUP_SCOPE_ID = "aiocqhttp:group-1"
OTHER_GROUP_SCOPE_ID = "aiocqhttp:group-2"
USER_SCOPE_ID = "aiocqhttp:10001"


def record(memory_id, content="本群火锅", *, scope_type="group", scope_id=GROUP_SCOPE_ID):
    return {
        "id": memory_id,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "key": f"k{memory_id}",
        "content": content,
        "importance": 0.6,
        "confidence": 0.8,
        "hits": 0,
        "updated_at": 1000.0,
        "expires_at": None,
    }


class FakeMemoryContext:
    def __init__(self, *, private=False, group_id="group-1", user_id="10001"):
        self.platform = "aiocqhttp"
        self.user_id = user_id
        self.group_id = "" if private else group_id
        self.is_private = private

    @property
    def user_scope(self):
        return ("user", f"aiocqhttp:{self.user_id}")

    @property
    def group_scope(self):
        if self.is_private or not self.group_id:
            return None
        return ("group", f"aiocqhttp:{self.group_id}")


class FakeService:
    def __init__(self, records=None, *, enabled=True):
        self.records = [dict(item) for item in (records or [])]
        self.enabled = enabled
        self.fts_enabled = True
        self.export_calls = []
        self.deleted = []
        self.forgotten = []

    def export(self, scope_type, scope_id):
        self.export_calls.append((scope_type, scope_id))
        return [
            dict(item)
            for item in self.records
            if item.get("scope_type") == scope_type
            and item.get("scope_id") == scope_id
        ]

    def delete(self, memory_id):
        self.deleted.append(memory_id)
        self.records = [item for item in self.records if item.get("id") != memory_id]
        return True

    def forget(self, scope_type, scope_id):
        self.forgotten.append((scope_type, scope_id))
        matched = [
            item
            for item in self.records
            if item.get("scope_type") == scope_type
            and item.get("scope_id") == scope_id
        ]
        self.records = [
            item
            for item in self.records
            if not (
                item.get("scope_type") == scope_type
                and item.get("scope_id") == scope_id
            )
        ]
        return len(matched)

    def policy_for(self, memory_context):
        return SimpleNamespace(
            mode="long_term",
            scope="group",
            inject=True,
            extract=True,
        )


class FakeCore:
    def __init__(self, service=None, memory_context=None):
        self.service = service if service is not None else FakeService()
        self.memory_context = memory_context or FakeMemoryContext()

    def get_memory_service(self, config):
        return self.service

    def build_context(self, event):
        return self.memory_context


class FakeContext:
    """Context used for AstrBot global memory config, never plugin settings."""

    def __init__(self, global_config=None):
        self.global_config = (
            global_config
            if global_config is not None
            else {"memory": {"enable": True}, "enabled": True}
        )
        self.config_calls = []

    def get_config(self, umo=None):
        self.config_calls.append(umo)
        return self.global_config


class FakeEvent:
    def __init__(
        self,
        message_str,
        *,
        private=False,
        group_id="group-1",
        sender_id="10001",
        role="admin",
        fail_should_call=False,
        fail_stop=False,
    ):
        self.message_str = message_str
        self.private = private
        self.group_id = "" if private else group_id
        self.sender_id = sender_id
        self.role = role
        self.fail_should_call = fail_should_call
        self.fail_stop = fail_stop
        self.unified_msg_origin = (
            f"aiocqhttp:{'FriendMessage' if private else 'GroupMessage'}:session-1"
        )
        self.message_obj = SimpleNamespace(
            sender=SimpleNamespace(role=role),
        )
        self.call_llm_values = []
        self.stop_count = 0
        self.replies = []

    def get_sender_id(self):
        return self.sender_id

    def is_private_chat(self):
        return self.private

    def should_call_llm(self, value):
        self.call_llm_values.append(value)
        if self.fail_should_call:
            raise RuntimeError("should_call_llm exploded")

    def plain_result(self, text):
        self.replies.append(text)
        return SimpleNamespace(text=text)

    def stop_event(self):
        self.stop_count += 1
        if self.fail_stop:
            raise RuntimeError("stop_event exploded")


async def run_handler(plugin, event):
    results = []
    async for result in plugin.memory_command(event):
        results.append(result)
    return results


class HandlerTestCase(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, config):
        context = FakeContext()
        plugin = MAIN.MemoryCommandsPlugin(context=context, config=config)
        return plugin, context

    async def run_with_core(self, plugin, event, core):
        original = MC.load_core_memory
        MC.load_core_memory = lambda: core
        try:
            return await run_handler(plugin, event)
        finally:
            MC.load_core_memory = original


class TestRegistrationAndInjection(HandlerTestCase):
    def test_one_handler_registers_both_command_names(self):
        registrations = FILTER.registrations
        self.assertEqual(len(registrations), 1)
        registration = registrations[0]
        self.assertEqual(registration["function"], "memory_command")
        self.assertEqual(registration["name"], "记忆")
        self.assertEqual(
            registration["aliases"],
            {"记忆管理", "memory", "会话", "session", "人格", "persona"},
        )
        self.assertEqual(registration["priority"], 110)
        self.assertEqual(
            {registration["name"], *registration["aliases"]},
            {"记忆", "记忆管理", "memory", "会话", "session", "人格", "persona"},
        )

    def test_source_has_no_duplicate_memory_handlers(self):
        source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        decorated_handlers = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = decorator.func
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "command"
                ):
                    decorated_handlers.append(node.name)
        self.assertEqual(decorated_handlers, ["memory_command"])
        self.assertEqual(source.count("async def memory_command"), 1)
        self.assertEqual(source.count("@filter.command("), 1)

    def test_signature_accepts_injected_plugin_config(self):
        signature = inspect.signature(MAIN.MemoryCommandsPlugin.__init__)
        self.assertEqual(list(signature.parameters), ["self", "context", "config"])
        self.assertIsNone(signature.parameters["config"].default)

    def test_constructor_stores_injected_mapping_without_reading_global_config(self):
        content = {"enabled": True, "max_results": 3}
        plugin, context = self.make_plugin(content)
        self.assertIs(plugin.config, content)
        self.assertIs(plugin.plugin_config, plugin.config)
        self.assertEqual(context.config_calls, [])

    async def test_non_mapping_injected_config_is_safe_and_disabled(self):
        plugin = MAIN.MemoryCommandsPlugin(FakeContext(), object())
        self.assertEqual(dict(plugin.plugin_config), {})
        event = FakeEvent("/记忆 状态")
        replies = await run_handler(plugin, event)
        self.assertIn("未启用", replies[0].text)
        self.assertEqual(event.call_llm_values, [True])
        self.assertEqual(event.stop_count, 1)


class TestHandlerBehaviour(HandlerTestCase):
    async def test_disabled_reply_still_suppresses_default_llm_and_stops(self):
        plugin, _ = self.make_plugin({"enabled": False})
        event = FakeEvent("/记忆 状态")
        results = await run_handler(plugin, event)
        self.assertEqual(len(results), 1)
        self.assertIn("未启用", results[0].text)
        self.assertEqual(event.call_llm_values, [True])
        self.assertEqual(event.stop_count, 1)
        self.assertEqual(event.replies, [results[0].text])

    async def test_injected_config_wins_and_global_config_is_not_plugin_config(self):
        # Global fake config says enabled, but only injected plugin config is
        # authoritative for the plugin.
        plugin, context = self.make_plugin({"enabled": False})
        event = FakeEvent("/记忆 状态")
        results = await run_handler(plugin, event)
        self.assertIn("未启用", results[0].text)
        self.assertEqual(context.config_calls, [])  # no core was opened
        self.assertEqual(event.call_llm_values, [True])
        self.assertEqual(event.stop_count, 1)

    async def test_legacy_prefixed_injected_config_is_used_end_to_end(self):
        service = FakeService(
            [
                record(1, "本群火锅"),
                record(2, "别的群", scope_id=OTHER_GROUP_SCOPE_ID),
            ]
        )
        plugin, _ = self.make_plugin(
            {
                "memory_commands_enabled": True,
                "memory_commands_allow_user_ids": "10001",
                "memory_commands_max_results": 2,
            }
        )
        event = FakeEvent("/记忆 搜索 火锅")
        results = await self.run_with_core(
            plugin,
            event,
            FakeCore(service, FakeMemoryContext()),
        )
        self.assertIn("本群火锅", results[0].text)
        self.assertNotIn("别的群", results[0].text)
        self.assertEqual(service.export_calls, [("group", GROUP_SCOPE_ID)])
        self.assertEqual(event.call_llm_values, [True])
        self.assertEqual(event.stop_count, 1)

    async def test_alias_is_routed_to_full_status_handler(self):
        service = FakeService([record(1, "本群火锅")])
        plugin, _ = self.make_plugin({"enabled": True})
        event = FakeEvent("/记忆管理 状态")
        results = await self.run_with_core(
            plugin,
            event,
            FakeCore(service, FakeMemoryContext()),
        )
        self.assertIn("长期记忆管理", results[0].text)
        self.assertIn("本群 · 1 条记忆", results[0].text)
        self.assertEqual(service.export_calls, [("group", GROUP_SCOPE_ID)])
        self.assertEqual(event.stop_count, 1)

    async def test_private_allowlist_uses_only_sender_user_scope(self):
        service = FakeService(
            [
                record(
                    1,
                    "我的火锅",
                    scope_type="user",
                    scope_id=USER_SCOPE_ID,
                ),
                record(2, "本群火锅"),
            ]
        )
        plugin, _ = self.make_plugin(
            {"enabled": True, "allow_user_ids": "10001"}
        )
        event = FakeEvent("/记忆 搜索 火锅", private=True)
        results = await self.run_with_core(
            plugin,
            event,
            FakeCore(service, FakeMemoryContext(private=True)),
        )
        self.assertIn("我的火锅", results[0].text)
        self.assertNotIn("本群火锅", results[0].text)
        self.assertEqual(service.export_calls, [("user", USER_SCOPE_ID)])

    async def test_destructive_config_is_passed_to_engine(self):
        service = FakeService([record(7, "本群火锅")])
        plugin, _ = self.make_plugin(
            {
                "memory_commands_enabled": True,
                "memory_commands_destructive_enabled": True,
            }
        )
        event = FakeEvent("/记忆 删除 7")
        results = await self.run_with_core(
            plugin,
            event,
            FakeCore(service, FakeMemoryContext()),
        )
        self.assertIn("已删除记忆 id=7", results[0].text)
        self.assertEqual(service.deleted, [7])

    async def test_unpatched_core_reports_unpatched_and_stops(self):
        plugin, _ = self.make_plugin({"enabled": True})
        event = FakeEvent("/记忆 状态")
        results = await self.run_with_core(plugin, event, None)
        self.assertEqual(results[0].text, MC.UNPATCHED_REPLY)
        self.assertEqual(event.call_llm_values, [True])
        self.assertEqual(event.stop_count, 1)

    async def test_handler_catches_unexpected_engine_exception(self):
        plugin, _ = self.make_plugin({"enabled": True})
        event = FakeEvent("/记忆 状态")

        original = MC.handle_text
        MC.handle_text = lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("boom secret=traceback")
        )
        try:
            results = await self.run_with_core(
                plugin,
                event,
                FakeCore(FakeService([record(1)]), FakeMemoryContext()),
            )
        finally:
            MC.handle_text = original
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, MC.HANDLER_FAILED_REPLY)
        self.assertIn("失败", results[0].text)
        self.assertNotIn("Traceback", results[0].text)
        self.assertNotIn("boom", results[0].text)
        self.assertLessEqual(len(results[0].text), 200)
        self.assertEqual(event.call_llm_values, [True])
        self.assertEqual(event.stop_count, 1)

    async def test_should_call_llm_exception_is_contained_and_stopped(self):
        plugin, _ = self.make_plugin({"enabled": True})
        event = FakeEvent("/记忆 状态", fail_should_call=True)
        results = await run_handler(plugin, event)
        self.assertEqual(results[0].text, MC.HANDLER_FAILED_REPLY)
        self.assertIn("失败", results[0].text)
        self.assertEqual(event.stop_count, 1)

    async def test_stop_event_exception_does_not_drop_reply(self):
        plugin, _ = self.make_plugin({"enabled": False})
        event = FakeEvent("/记忆 状态", fail_stop=True)
        results = await run_handler(plugin, event)
        self.assertEqual(len(results), 1)
        self.assertIn("未启用", results[0].text)
        self.assertEqual(event.stop_count, 1)

    async def test_handler_never_calls_a_chat_model_surface(self):
        plugin, _ = self.make_plugin({"enabled": False})
        event = FakeEvent("/记忆 状态")

        async def forbidden(*args, **kwargs):
            raise AssertionError("chat model must not be called")

        for name in (
            "llm_generate",
            "request_llm",
            "call_llm",
            "chat",
            "generate",
        ):
            setattr(event, name, forbidden)
        results = await run_handler(plugin, event)
        self.assertEqual(len(results), 1)
        self.assertEqual(event.stop_count, 1)
        source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        for forbidden_name in ("llm_generate", "request_llm", "chat_model"):
            self.assertNotIn(forbidden_name, source)


if __name__ == "__main__":
    unittest.main()
