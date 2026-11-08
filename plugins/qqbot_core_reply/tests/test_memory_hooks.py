from __future__ import annotations

import asyncio
import types
import unittest

from support import FakeContext, FakeEvent, make_plugin
from fake_astrbot import FakeResult, FakeStarMeta
from reply_pipeline import CANDIDATE_EXTRA, MODEL_ROUND_EXTRA, TURN_EXTRA


def run(coro):
    return asyncio.run(coro)


GROUP = "123456789"


class MemoryHookTests(unittest.TestCase):
    def test_memory_mode_aliases(self):
        for value in ("astrbot", "memory", "conversation", "long-term", "long_term"):
            plugin, _, _ = make_plugin(overrides={"memory_mode": value})
            self.assertEqual(plugin._memory_mode(), "astrbot", value)

    def test_direct_mode_hook_is_noop(self):
        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "direct"})
        event = FakeEvent("hello", private=True)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertEqual(req.system_prompt, "BASE")

    def test_candidate_identity_injected(self):
        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", group_id=GROUP, sender_name="Alice", sender_id="1001")
        plugin._mark_candidate(event, "hello", ambient=False)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertIn("Current Sender Identity", req.system_prompt)
        self.assertIn("Alice", req.system_prompt)
        self.assertIn("1001", req.system_prompt)
        self.assertTrue(event.get_extra("qqbot_core_reply_identity_injected"))

    def test_safe_tools_catalog_injected(self):
        class SafeStar:
            def tool_instruction(self):
                return "SAFE TOOLS CATALOG\n[[tool:search_food_restaurants name=\"火锅\"]]"

        ctx = FakeContext(
            registered_stars={
                "qqbot_safe_tools": FakeStarMeta(SafeStar())
            }
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", group_id=GROUP, sender_name="Alice", sender_id="1001")
        plugin._mark_candidate(event, "hello", ambient=False)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertIn("SAFE TOOLS CATALOG", req.system_prompt)
        self.assertIn("search_food_restaurants", req.system_prompt)
        self.assertTrue(event.get_extra("qqbot_core_reply_safe_tools_injected"))

    def test_english_help_phrase_is_not_a_command(self):
        plugin, _, _ = make_plugin(overrides={"enabled": True})
        event = FakeEvent(
            "help me find some good hot pot restaurants in Vancouver",
            at_bot=True,
            wake=True,
        )
        run(plugin.guard_english_help_phrase(event))
        self.assertFalse(event.is_at_or_wake_command)

    def test_slash_help_stays_a_command(self):
        plugin, _, _ = make_plugin(overrides={"enabled": True})
        event = FakeEvent("/help", wake=True)
        run(plugin.guard_english_help_phrase(event))
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.stopped)

    def test_help_command_can_be_disabled(self):
        plugin, _, _ = make_plugin(
            overrides={"enabled": True, "help_command_enabled": False}
        )
        event = FakeEvent("/help", wake=True)
        run(plugin.guard_english_help_phrase(event))
        self.assertTrue(event.stopped)

    def test_group_admin_global_admin_fallback(self):
        class SafeStar:
            def group_admin_override_enabled(self):
                return True

            def group_admin_allow_bot_admin(self):
                return True

        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(SafeStar())},
            global_config={"admins_id": ["2933886037"]},
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        event = FakeEvent("hello", group_id=GROUP, sender_id="2933886037")
        event.message_obj = types.SimpleNamespace(
            sender=types.SimpleNamespace(role="member")
        )
        event.is_admin = lambda: False
        self.assertTrue(plugin._group_admin_allowed(event))

    def test_group_admin_switch_allows_bot_admin(self):
        class SafeStar:
            def group_admin_override_enabled(self):
                return True

            def group_admin_allow_bot_admin(self):
                return True

        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(SafeStar())}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        event = FakeEvent("hello", group_id=GROUP, sender_id="2933886037")
        event.message_obj = types.SimpleNamespace(
            sender=types.SimpleNamespace(role="member")
        )
        event.is_admin = lambda: True
        self.assertTrue(plugin._group_admin_allowed(event))

    def test_non_candidate_has_no_injection(self):
        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", group_id=GROUP)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertEqual(req.system_prompt, "BASE")

    def test_private_candidate_identity_injected(self):
        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", private=True, sender_name="Bob", sender_id="2002")
        plugin._mark_candidate(event, "hello", ambient=False)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertIn("Bob", req.system_prompt)
        self.assertIn("2002", req.system_prompt)

    def test_ambient_history_injected(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "memory_mode": "astrbot",
                "ambient_group_chat_enabled": True,
                "ambient_group_context_max_chars": 1000,
                "ambient_group_cache_limit": 5,
            }
        )
        first = FakeEvent("first group line", group_id=GROUP, sender_name="Alice")
        plugin._remember_history(first, "first group line", has_image=False, has_face=False)
        event = FakeEvent("robot hello", group_id=GROUP, sender_name="Bob")
        plugin._mark_candidate(event, "robot hello", ambient=True)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertIn("Ambient Group Context", req.system_prompt)
        self.assertIn("first group line", req.system_prompt)
        self.assertTrue(event.get_extra("qqbot_core_reply_ambient_context_injected"))

    def test_hook_exception_degrades(self):
        class BrokenReq:
            @property
            def system_prompt(self):
                raise RuntimeError("broken")

            @system_prompt.setter
            def system_prompt(self, value):
                raise RuntimeError("broken")

        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", group_id=GROUP)
        plugin._mark_candidate(event, "hello", ambient=False)
        run(plugin.inject_memory_mode_context(event, BrokenReq()))
        self.assertFalse(
            event.get_extra("qqbot_core_reply_ambient_context_injected")
        )

    def _claim_memory_turn(self, plugin, event):
        context = plugin.pipeline.request_context(event)
        record, _ = plugin.pipeline.state.claim_turn(
            context, delivery_key=f"reply:{context.turn_id}"
        )
        event.set_extra(CANDIDATE_EXTRA, True)
        event.set_extra(TURN_EXTRA, context.turn_id)
        event.set_extra(MODEL_ROUND_EXTRA, 0)
        return context, record

    def test_empty_memory_output_releases_turn(self):
        plugin, _, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("https://example.com", private=True)
        context, record = self._claim_memory_turn(plugin, event)
        event.set_result(FakeResult("chain", chain=[]))
        run(plugin.pipeline.handle_decorating_result(event))
        self.assertEqual(record.status, "delivered")
        self.assertNotIn(context.turn_id, plugin.pipeline.state.active_turns.values())

    def test_missing_processor_releases_turn(self):
        from astrbot.api import message_components as Comp

        plugin, _, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", private=True)
        context, record = self._claim_memory_turn(plugin, event)
        event.set_result(FakeResult("chain", chain=[Comp.Plain("hello")]))
        run(plugin.pipeline.handle_decorating_result(event))
        self.assertEqual(record.status, "delivered")
        self.assertNotIn(context.turn_id, plugin.pipeline.state.active_turns.values())

    def test_memory_turn_watchdog_releases_stuck_turn(self):
        plugin, _, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", private=True)
        context, record = self._claim_memory_turn(plugin, event)
        run(plugin.pipeline._expire_memory_turn(context.turn_id, 0))
        self.assertEqual(record.status, "outcome_unknown")
        self.assertNotIn(context.turn_id, plugin.pipeline.state.active_turns.values())

    def test_after_message_sent_releases_turn(self):
        from reply_pipeline import MEMORY_MODE_EXTRA

        plugin, _, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", private=True)
        context, record = self._claim_memory_turn(plugin, event)
        event.set_extra(MEMORY_MODE_EXTRA, True)
        plugin.pipeline.release_memory_turn_after_send(event)
        self.assertEqual(record.status, "delivered")
        self.assertNotIn(context.turn_id, plugin.pipeline.state.active_turns.values())

    def test_hook_never_calls_llm(self):
        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", group_id=GROUP)
        plugin._mark_candidate(event, "hello", ambient=False)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertEqual(ctx.llm_calls, [])


if __name__ == "__main__":
    unittest.main()
