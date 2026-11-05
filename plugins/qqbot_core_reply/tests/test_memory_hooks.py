from __future__ import annotations

import asyncio
import types
import unittest

from support import FakeContext, FakeEvent, make_plugin


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

    def test_hook_never_calls_llm(self):
        plugin, ctx, _ = make_plugin(overrides={"memory_mode": "astrbot"})
        event = FakeEvent("hello", group_id=GROUP)
        plugin._mark_candidate(event, "hello", ambient=False)
        req = types.SimpleNamespace(system_prompt="BASE")
        run(plugin.inject_memory_mode_context(event, req))
        self.assertEqual(ctx.llm_calls, [])


if __name__ == "__main__":
    unittest.main()
