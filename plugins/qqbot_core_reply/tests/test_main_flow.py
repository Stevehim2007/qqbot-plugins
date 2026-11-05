from __future__ import annotations

import asyncio
import json
import unittest

from support import (
    Clock,
    FakeContext,
    FakeEvent,
    FakeStarMeta,
    ScriptedRng,
    collect_event,
    extras,
    make_plugin,
)
from astrbot.api import message_components as Comp


def run(coro):
    return asyncio.run(coro)


GROUP = "123456789"


def group_event(text="hello", **kwargs):
    kwargs.setdefault("group_id", GROUP)
    return FakeEvent(text, **kwargs)


class MainFlowTests(unittest.TestCase):
    def test_disabled_plugin_short_circuits(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": False})
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])
        self.assertFalse(event.stopped)
        self.assertFalse(event.llm_blocked)

    def test_private_direct_success_single_reply(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["你好"]
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "plain")
        self.assertEqual(results[0].text, "你好")
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertTrue(event.stopped)
        self.assertTrue(event.llm_blocked)
        self.assertEqual(
            extras(event),
            {
                "qqbot_core_reply_candidate": True,
                "qqbot_core_reply_allow_sticker": True,
                "qqbot_core_reply_text": "hello",
            },
        )

    def test_at_group_direct_success(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["at reply"]
        event = group_event("hello", at_bot=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertTrue(extras(event)["qqbot_core_reply_candidate"])

    def test_group_non_at_probability_zero_no_call(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "reply_probability": 0}
        )
        event = group_event()
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])
        self.assertTrue(event.llm_blocked)
        self.assertFalse(event.stopped)

    def test_group_non_at_probability_one_replies(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            }
        )
        ctx.llm_responses = ["group reply"]
        event = group_event()
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, "group reply")
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_probability_boundary_equal_is_miss(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "reply_probability": 0.5},
            rng=ScriptedRng([0.5]),
        )
        results = run(collect_event(plugin, group_event()))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])

    def test_probability_boundary_below_hits(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 0.5,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            },
            rng=ScriptedRng([0.499]),
        )
        ctx.llm_responses = ["hit"]
        results = run(collect_event(plugin, group_event()))
        self.assertEqual(len(results), 1)

    def test_group_cooldown_blocks_second_message(self):
        clock = Clock()
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "group_cooldown_seconds": 60,
                "user_cooldown_seconds": 0,
            },
            clock=clock,
        )
        ctx.llm_responses = ["first", "second"]
        first = group_event("hello one")
        second = group_event("hello two")
        first_results = run(collect_event(plugin, first))
        second_results = run(collect_event(plugin, second))
        self.assertEqual(len(first_results), 1)
        self.assertEqual(second_results, [])
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_user_cooldown_blocks_same_user_but_not_other(self):
        clock = Clock()
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 60,
            },
            clock=clock,
        )
        ctx.llm_responses = ["a", "b", "c"]
        first = group_event("hello one", sender_id="1001")
        second = group_event("hello two", sender_id="1001")
        other = group_event("hello three", sender_id="1002")
        self.assertEqual(len(run(collect_event(plugin, first))), 1)
        self.assertEqual(run(collect_event(plugin, second)), [])
        self.assertEqual(len(run(collect_event(plugin, other))), 1)
        self.assertEqual(len(ctx.llm_calls), 2)

    def test_min_message_chars_boundary(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "min_message_chars": 5,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            }
        )
        ctx.llm_responses = ["ok"]
        short = run(collect_event(plugin, group_event("1234")))
        exact = run(collect_event(plugin, group_event("12345")))
        self.assertEqual(short, [])
        self.assertEqual(len(exact), 1)

    def test_max_input_chars_truncates_prompt(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "max_input_chars": 5,
                "max_message_chars": 5,
            }
        )
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("hello world", private=True)))
        prompt = ctx.llm_calls[0]["prompt"]
        self.assertIn("hello", prompt)
        self.assertNotIn("world", prompt)

    def test_max_reply_chars_truncates_with_ellipsis(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "max_reply_chars": 10}
        )
        ctx.llm_responses = ["x" * 30]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertTrue(results[0].text.endswith("…"))
        self.assertLessEqual(len(results[0].text), 11)

    def test_empty_llm_response_uses_fallback(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "empty_reply_fallback": "EMPTY-FALLBACK",
            }
        )
        ctx.llm_responses = ["   "]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(results[0].text, "EMPTY-FALLBACK")

    def test_llm_exception_uses_error_fallback(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "llm_error_reply": "ERROR-FALLBACK",
            }
        )
        ctx.llm_responses = [RuntimeError("boom")]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(results[0].text, "ERROR-FALLBACK")
        self.assertEqual(len(results), 1)

    def test_provider_missing_uses_error_without_llm_call(self):
        ctx = FakeContext(provider_id="", provider=None)
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "llm_error_reply": "NO-PROVIDER",
            },
        )
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results[0].text, "NO-PROVIDER")
        self.assertEqual(ctx.llm_calls, [])
        self.assertTrue(extras(event)["qqbot_core_reply_candidate"])

    def test_group_pure_image_ignored_by_default(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "reply_probability": 1}
        )
        event = group_event("", components=[Comp.Image(url="https://x/a.png")])
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])

    def test_group_pure_face_ignored_by_default(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "reply_probability": 1}
        )
        event = group_event("", components=[Comp.Face()])
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])

    def test_group_pure_image_eligible_when_ignore_disabled(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "ignore_pure_image": False,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            }
        )
        ctx.llm_responses = ["image seen"]
        event = group_event("", components=[Comp.Image(url="https://x/a.png")])
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertIn("image_urls", ctx.llm_calls[0])

    def test_ambient_off_uses_probability_path(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "ambient_group_chat_enabled": False,
                "reply_probability": 1,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            }
        )
        ctx.llm_responses = ["random"]
        results = run(collect_event(plugin, group_event()))
        self.assertEqual(len(results), 1)

    def test_ambient_on_miss_does_not_fall_back_to_probability(self):
        clock = Clock()
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "ambient_group_chat_enabled": True,
                "reply_probability": 1,
                "ambient_group_keywords": ["robot"],
                "ambient_group_keyword_probability": 1,
                "ambient_group_min_interval_seconds": 60,
            },
            clock=clock,
        )
        ctx.llm_responses = ["ambient first"]
        first = run(collect_event(plugin, group_event("robot here")))
        second = run(collect_event(plugin, group_event("plain text here")))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_ambient_keyword_hit_sets_ambient_extra(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "ambient_group_chat_enabled": True,
                "reply_probability": 0,
                "ambient_group_keywords": ["robot"],
                "ambient_group_keyword_probability": 1,
                "ambient_group_min_interval_seconds": 0,
            }
        )
        ctx.llm_responses = ["ambient reply"]
        event = group_event("hey robot")
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertTrue(extras(event).get("qqbot_core_reply_ambient"))

    def test_ambient_min_interval_blocks_second(self):
        clock = Clock()
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "ambient_group_chat_enabled": True,
                "reply_probability": 0,
                "ambient_group_keywords": ["robot"],
                "ambient_group_keyword_probability": 1,
                "ambient_group_min_interval_seconds": 60,
            },
            clock=clock,
        )
        ctx.llm_responses = ["first", "second"]
        self.assertEqual(len(run(collect_event(plugin, group_event("robot one")))), 1)
        self.assertEqual(run(collect_event(plugin, group_event("robot two"))), [])
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_ambient_round_interval_gate(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "ambient_group_chat_enabled": True,
                "reply_probability": 0,
                "ambient_group_keywords": ["robot"],
                "ambient_group_keyword_probability": 1,
                "ambient_group_min_interval_seconds": 0,
                "ambient_group_round_interval": 2,
            }
        )
        ctx.llm_responses = ["second"]
        first = run(collect_event(plugin, group_event("robot one")))
        second = run(collect_event(plugin, group_event("robot two")))
        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_command_prefix_excluded(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        event = FakeEvent("/help", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])
        self.assertFalse(event.stopped)

    def test_inherited_wake_prefix_excluded(self):
        ctx = FakeContext(global_config={"wake_prefix": ["!"]})
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={"enabled": True, "inherit_astrbot_wake_prefix": True},
        )
        event = FakeEvent("!ping", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])

    def test_own_command_name_excluded_without_prefix(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        event = group_event("会话列表")
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])

    def test_one_result_max(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["one"]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(len(results), 1)

    def test_identity_prompt_and_redaction(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["hi Alice 1001 123456789"]
        event = group_event("hello", at_bot=True)
        results = run(collect_event(plugin, event))
        prompt = ctx.llm_calls[0]["prompt"]
        self.assertIn("Alice", prompt)
        self.assertIn("1001", prompt)
        self.assertIn("123456789", prompt)
        self.assertNotIn("Alice", results[0].text)
        self.assertNotIn("1001", results[0].text)
        self.assertNotIn("123456789", results[0].text)

    def test_identity_context_can_be_disabled(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "group_identity_context_enabled": False,
            }
        )
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, group_event("hello", at_bot=True)))
        prompt = ctx.llm_calls[0]["prompt"]
        self.assertNotIn("发送者QQ", prompt)
        self.assertIn("用户昵称", prompt)

    def test_bounded_history_limit(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
                "group_identity_history_limit": 1,
            }
        )
        ctx.llm_responses = ["one", "two", "three"]
        run(collect_event(plugin, group_event("first message")))
        run(collect_event(plugin, group_event("second message")))
        run(collect_event(plugin, group_event("third message")))
        second_prompt = ctx.llm_calls[1]["prompt"]
        third_prompt = ctx.llm_calls[2]["prompt"]
        self.assertIn("first message", second_prompt)
        self.assertNotIn("first message", third_prompt)
        self.assertIn("second message", third_prompt)

    def test_group_persona_prompt_used(self):
        persona = json.dumps(
            {
                "groups": {
                    GROUP: {"name": "群人格", "prompt": "PERSONA-PROMPT"}
                }
            }
        )
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "group_personas": persona}
        )
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, group_event("hello", at_bot=True)))
        self.assertEqual(
            ctx.llm_calls[0]["system_prompt"],
            "PERSONA-PROMPT",
        )

    def test_prompt_profile_active_used(self):
        profiles = json.dumps(
            [{"id": "p1", "name": "Profile", "prompt": "PROFILE-PROMPT"}]
        )
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "prompt_profiles": profiles,
                "prompt_profile_active_id": "p1",
            }
        )
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertIn("PROFILE-PROMPT", ctx.llm_calls[0]["system_prompt"])

    def test_safe_tools_handshake_appended(self):
        class SafeStar:
            def __init__(self):
                self.calls = 0

            def tool_instruction(self):
                self.calls += 1
                return "SAFE-TOOLS-INSTRUCTION"

        star = SafeStar()
        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(star)}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(star.calls, 1)
        system_prompt = ctx.llm_calls[0]["system_prompt"]
        self.assertIn("SAFE-TOOLS-INSTRUCTION", system_prompt)
        self.assertIn("# Safe Tools Tool Catalog", system_prompt)

    def test_safe_tools_absent_means_no_marker_protocol(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertNotIn("# Safe Tools Tool Catalog", ctx.llm_calls[0]["system_prompt"])

    def test_safe_tools_inactive_not_used(self):
        class SafeStar:
            def tool_instruction(self):
                return "SHOULD-NOT-APPEAR"

        ctx = FakeContext(
            registered_stars={
                "qqbot_safe_tools": FakeStarMeta(SafeStar(), activated=False)
            }
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertNotIn("SHOULD-NOT-APPEAR", ctx.llm_calls[0]["system_prompt"])

    def test_safe_tools_error_degrades(self):
        class BrokenStar:
            def tool_instruction(self):
                raise RuntimeError("broken")

        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(BrokenStar())}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        ctx.llm_responses = ["still works"]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(results[0].text, "still works")
        self.assertNotIn("# Safe Tools Tool Catalog", ctx.llm_calls[0]["system_prompt"])

    def test_marker_text_is_not_parsed_stripped_or_executed(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["ok [[tool:send_sticker:cat]]"]
        event = FakeEvent("please [[tool:send_sticker:cat]]", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertIn(
            "[[tool:send_sticker:cat]]",
            ctx.llm_calls[0]["prompt"],
        )
        self.assertEqual(results[0].text, "ok [[tool:send_sticker:cat]]")

    def test_memory_mode_no_direct_llm_single_wake(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "memory_mode": "astrbot"}
        )
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])
        self.assertTrue(event.is_at_or_wake_command)
        self.assertFalse(event.stopped)
        self.assertTrue(extras(event)["qqbot_core_reply_candidate"])
        self.assertTrue(extras(event)["qqbot_core_reply_allow_sticker"])
        self.assertEqual(extras(event)["qqbot_core_reply_text"], "hello")

    def test_memory_mode_command_exclusion_first(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "memory_mode": "astrbot"}
        )
        event = FakeEvent("/会话列表", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])
        self.assertFalse(event.is_at_or_wake_command)
        self.assertNotIn("qqbot_core_reply_candidate", extras(event))

    def test_memory_mode_ambient_extra(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "memory_mode": "astrbot",
                "ambient_group_chat_enabled": True,
                "reply_probability": 0,
                "ambient_group_keywords": ["robot"],
                "ambient_group_keyword_probability": 1,
                "ambient_group_min_interval_seconds": 0,
            }
        )
        event = group_event("hey robot")
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertTrue(event.is_at_or_wake_command)
        self.assertTrue(extras(event)["qqbot_core_reply_ambient"])


if __name__ == "__main__":
    unittest.main()
