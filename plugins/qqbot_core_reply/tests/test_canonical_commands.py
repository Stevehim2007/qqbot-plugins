from __future__ import annotations

import asyncio
import unittest

from support import (
    Clock,
    FakeContext,
    FakeEvent,
    ScriptedRng,
    collect_command,
    collect_event,
    extras,
    make_plugin,
)
from astrbot.api import message_components as Comp


def run(coro):
    return asyncio.run(coro)


class ReplyCommandTests(unittest.TestCase):
    def test_reply_status_alias_and_no_model_call(self):
        for command in ("/回复 状态", "/reply 状态"):
            with self.subTest(command=command):
                plugin, ctx, _ = make_plugin(overrides={"enabled": True})
                event = FakeEvent(command, private=True)
                results = run(collect_command(plugin, "reply_command", event))
                self.assertEqual(len(results), 1)
                self.assertIn("回复状态：启用", results[0].text)
                self.assertIn("owner_epoch=", results[0].text)
                self.assertEqual(ctx.llm_calls, [])
                self.assertTrue(event.llm_blocked)
                self.assertTrue(event.stopped)
                self.assertNotIn("qqbot_core_reply_candidate", extras(event))

    def test_reply_enable_disable_updates_readiness(self):
        plugin, ctx, _ = make_plugin(config=None)
        self.assertFalse(plugin.state.ready)
        enabled = run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/回复 启用", private=True),
            )
        )
        self.assertIn("已启用", enabled[0].text)
        self.assertTrue(plugin.config.bool("enabled", False))
        self.assertTrue(plugin.state.ready)
        self.assertTrue(ctx.qqbot_service_registry.is_ready("core_reply"))
        disabled = run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/reply 停用", private=True),
            )
        )
        self.assertIn("已停用", disabled[0].text)
        self.assertFalse(plugin.config.bool("enabled", False))
        self.assertFalse(plugin.state.ready)
        self.assertFalse(ctx.qqbot_service_registry.is_ready("core_reply"))

    def test_reply_set_probability_and_status_reflects_it(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        set_result = run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/回复 设置 概率 0.42", private=True),
            )
        )
        self.assertIn("已更新", set_result[0].text)
        self.assertAlmostEqual(
            plugin.config.effective_float(
                "participation.group_probability",
                "reply_probability",
            ),
            0.42,
        )
        self.assertEqual(plugin.state.config_revision, 2)
        status = run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/回复 状态", private=True),
            )
        )
        self.assertIn("42%", status[0].text)

    def test_reply_set_cooldown_and_mode(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/回复 设置 群冷却 12", private=True),
            )
        )
        self.assertEqual(plugin.config.float("cooldown.seconds", 0, 3600), 12)
        run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/回复 设置 模式 astrbot", private=True),
            )
        )
        self.assertEqual(plugin.persona.memory_mode(), "astrbot")

    def test_reply_unknown_subcommand_is_terminal_chinese_error(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        event = FakeEvent("/回复 不存在", private=True)
        results = run(collect_command(plugin, "reply_command", event))
        self.assertIn("未知的回复子命令", results[0].text)
        self.assertEqual(ctx.llm_calls, [])
        self.assertTrue(event.llm_blocked)

    def test_reply_unknown_setting_is_terminal_error(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        results = run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/reply 设置 不存在 1", private=True),
            )
        )
        self.assertIn("未知的设置项", results[0].text)
        self.assertEqual(ctx.llm_calls, [])

    def test_reply_invalid_probability_rejected(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        results = run(
            collect_command(
                plugin,
                "reply_command",
                FakeEvent("/回复 设置 概率 2", private=True),
            )
        )
        self.assertIn("0 到 1", results[0].text)
        self.assertEqual(ctx.llm_calls, [])


class CooldownCommandTests(unittest.TestCase):
    def test_cooldown_status_and_alias(self):
        for command in ("/冷却 状态", "/cooldown 状态"):
            with self.subTest(command=command):
                plugin, ctx, _ = make_plugin(overrides={"enabled": True})
                event = FakeEvent(command, private=True)
                results = run(collect_command(plugin, "cooldown_command", event))
                self.assertEqual(len(results), 1)
                self.assertIn("冷却状态", results[0].text)
                self.assertIn("key_mode=", results[0].text)
                self.assertEqual(ctx.llm_calls, [])
                self.assertTrue(event.llm_blocked)
                self.assertTrue(event.stopped)

    def test_cooldown_unknown_subcommand_terminal(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        results = run(
            collect_command(
                plugin,
                "cooldown_command",
                FakeEvent("/冷却 未知", private=True),
            )
        )
        self.assertIn("未知的冷却子命令", results[0].text)
        self.assertEqual(ctx.llm_calls, [])

    def test_cooldown_status_after_probability_reply_shows_remaining(self):
        clock = Clock()
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "group_cooldown_seconds": 60,
                "user_cooldown_seconds": 60,
            },
            clock=clock,
            rng=ScriptedRng([]),
        )
        ctx.llm_responses = ["first"]
        first = FakeEvent("hello", group_id="123456789")
        self.assertEqual(len(run(collect_event(plugin, first))), 1)
        event = FakeEvent("/冷却 状态", group_id="123456789")
        results = run(collect_command(plugin, "cooldown_command", event))
        self.assertIn("当前会话剩余 60s", results[0].text)


class DefaultLlmGateTests(unittest.TestCase):
    def test_canonical_commands_do_not_enter_chat_pipeline(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        for command in (
            "/回复 状态",
            "/reply 状态",
            "/冷却 状态",
            "/cooldown 状态",
            "/会话列表",
            "/option",
            "/唱歌",
        ):
            with self.subTest(command=command):
                event = FakeEvent(command, group_id="123456789")
                outcome = run(plugin.pipeline.handle_message(event))
                self.assertIsNone(outcome)
                self.assertTrue(event.llm_blocked)
                self.assertNotIn("qqbot_core_reply_candidate", extras(event))
        self.assertEqual(ctx.llm_calls, [])

    def test_unknown_slash_command_never_becomes_prompt_or_native_llm(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        event = FakeEvent("/definitely-unknown arg", group_id="123456789")
        outcome = run(plugin.pipeline.handle_message(event))
        self.assertIsNone(outcome)
        self.assertTrue(event.llm_blocked)
        self.assertEqual(ctx.llm_calls, [])

    def test_probability_zero_zero_model_calls_and_native_blocked(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "participation.group_probability": 0,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            }
        )
        event = FakeEvent("hello there", group_id="123456789")
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertTrue(event.llm_blocked)
        self.assertEqual(ctx.llm_calls, [])

    def test_cooldown_rejection_zero_model_calls(self):
        clock = Clock()
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "reply_probability": 1,
                "group_cooldown_seconds": 60,
                "user_cooldown_seconds": 60,
            },
            clock=clock,
            rng=ScriptedRng([]),
        )
        ctx.llm_responses = ["first"]
        first = FakeEvent("hello first", group_id="123456789")
        second = FakeEvent("hello second", group_id="123456789")
        self.assertEqual(len(run(collect_event(plugin, first))), 1)
        second_results = run(collect_event(plugin, second))
        self.assertEqual(second_results, [])
        self.assertTrue(second.llm_blocked)
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_disabled_zero_model_calls_native_blocked(self):
        plugin, ctx, _ = make_plugin(config=None)
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertTrue(event.llm_blocked)
        self.assertEqual(ctx.llm_calls, [])

    def test_pure_image_probability_zero_no_native_fallback(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "participation.group_probability": 0,
            }
        )
        event = FakeEvent(
            "",
            group_id="123456789",
            components=[Comp.Image(url="https://x/a.png")],
        )
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertTrue(event.llm_blocked)
        self.assertEqual(ctx.llm_calls, [])


if __name__ == "__main__":
    unittest.main()
