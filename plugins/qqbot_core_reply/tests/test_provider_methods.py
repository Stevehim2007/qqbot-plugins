from __future__ import annotations

import asyncio
import unittest

from support import (
    FakeContext,
    FakeStarMeta,
    make_plugin,
    make_request_context,
)
import contracts


def run(coro):
    return asyncio.run(coro)


class SafeStar:
    def __init__(self, prefix="safe"):
        self.prefix = prefix
        self.instruction_calls = 0
        self.process_calls: list[tuple[object, str]] = []

    def tool_instruction(self, enabled_tools=None):
        self.instruction_calls += 1
        return "SAFE-TOOL-INSTRUCTION"

    def process_model_output(self, request_context, full_model_output):
        self.process_calls.append((request_context, full_model_output))
        return contracts.ReplyPlan(
            request_context=request_context,
            text_segments=[f"{self.prefix}:{full_model_output}"],
            media_segments=[],
            terminal=True,
            delivery_key=getattr(request_context, "delivery_key", ""),
        )


class ProviderMethodTests(unittest.TestCase):
    def test_fixed_methods_return_contract_responses(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        for name in (
            "reply_status",
            "reply_configure_plan",
            "reply_configure_commit",
            "reply_turn_run",
            "reply_cancel_turn",
        ):
            method = getattr(plugin, name)
            self.assertTrue(callable(method))
            result = run(method(request, {}))
            if name in {"reply_status", "reply_configure_plan", "reply_configure_commit"}:
                # Empty changes are a deterministic invalid_args error, but it
                # still must be a contract response, never raw Chinese text.
                self.assertIsInstance(result, contracts.ContractResponse)
            else:
                self.assertIsInstance(result, contracts.ContractResponse)

    def test_reply_status_data(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        response = run(plugin.reply_status(request, {}))
        self.assertEqual(response.status, "ok")
        self.assertTrue(response.data["ready"])
        self.assertEqual(response.data["provider_id"], "core_reply")
        self.assertEqual(response.data["owner_epoch"], plugin.state.owner_epoch)
        self.assertEqual(response.data["concurrency_per_conversation"], 1)

    def test_configure_plan_and_commit_enables_provider(self):
        plugin, ctx, _ = make_plugin(config=None)
        self.assertFalse(plugin.state.ready)
        self.assertFalse(ctx.qqbot_service_registry.is_ready("core_reply"))
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        plan = run(
            plugin.reply_configure_plan(
                request,
                {"changes": {"enabled": True}},
            )
        )
        self.assertEqual(plan.status, "ok")
        self.assertEqual(plan.data["plan"]["config_revision"], 1)
        commit = run(
            plugin.reply_configure_commit(
                request,
                {
                    "plan_id": plan.data["plan_id"],
                    "expected_revision": 1,
                },
            )
        )
        self.assertEqual(commit.status, "ok")
        self.assertTrue(commit.data["ready"])
        self.assertTrue(plugin.state.ready)
        self.assertTrue(ctx.qqbot_service_registry.is_ready("core_reply"))
        status = run(plugin.reply_status(request, {}))
        self.assertTrue(status.data["enabled"])
        self.assertEqual(status.data["config_revision"], 2)

    def test_configure_plan_rejects_unknown_key(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        response = run(
            plugin.reply_configure_plan(
                request,
                {"changes": {"owner_epoch": 999}},
            )
        )
        self.assertEqual(response.status, "error")
        self.assertEqual(response.error_code, "invalid_args")

    def test_configure_commit_revision_conflict(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        plan = run(
            plugin.reply_configure_plan(
                request,
                {"changes": {"participation.group_probability": 0.5}},
            )
        )
        response = run(
            plugin.reply_configure_commit(
                request,
                {
                    "plan_id": plan.data["plan_id"],
                    "expected_revision": 99,
                },
            )
        )
        self.assertEqual(response.status, "error")
        self.assertEqual(response.error_code, "revision_conflict")

    def test_configure_commit_rejects_stale_owner_epoch(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch + 1)
        response = run(
            plugin.reply_configure_commit(
                request,
                {
                    "changes": {"enabled": True},
                    "expected_revision": plugin.state.config_revision,
                },
            )
        )
        self.assertEqual(response.error_code, "owner_epoch_mismatch")

    def test_turn_run_calls_safe_tools_once_and_returns_plan(self):
        star = SafeStar()
        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(star)}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        response = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(response.status, "ok")
        self.assertEqual(response.data["text"], "safe:默认回复")
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertEqual(len(star.process_calls), 1)
        self.assertIs(star.process_calls[0][0], request)
        self.assertEqual(star.process_calls[0][1], "默认回复")
        self.assertTrue(response.data["reply_plan"]["delivery_key"].startswith("reply:"))

    def test_turn_run_idempotent_does_not_call_model_again(self):
        star = SafeStar()
        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(star)}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        first = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        second = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(second.status, "ok")
        self.assertTrue(second.data["idempotent"])
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertEqual(len(star.process_calls), 1)

    def test_delivery_unknown_returns_contract_error_and_no_resend(self):
        star = SafeStar()
        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(star)}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        first = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(first.status, "ok")
        plugin.state.mark_delivery_unknown(request.turn_id)
        second = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(second.status, "outcome_unknown")
        self.assertEqual(second.error_code, "delivery_unknown")
        self.assertIn("do not resend", second.message)
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertEqual(len(star.process_calls), 1)

    def test_cancel_turn_does_not_resend(self):
        star = SafeStar()
        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(star)}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        cancelled = run(
            plugin.reply_cancel_turn(request, {"turn_id": request.turn_id})
        )
        self.assertEqual(cancelled.status, "ok")
        self.assertFalse(cancelled.data["resent"])
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_deadline_exceeded_fails_closed(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(
            owner_epoch=plugin.state.owner_epoch,
            deadline=plugin.state.clock() - 1,
        )
        response = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(response.status, "error")
        self.assertEqual(response.error_code, "deadline_exceeded")
        self.assertEqual(ctx.llm_calls, [])

    def test_concurrent_turns_same_conversation_reject_second(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_generate(_kwargs):
            started.set()
            await release.wait()
            return "slow reply"

        ctx = FakeContext(llm_handler=slow_generate)
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        first_request = make_request_context(
            turn_id="turn-a",
            conversation_id="conv-shared",
            owner_epoch=plugin.state.owner_epoch,
        )
        second_request = make_request_context(
            turn_id="turn-b",
            conversation_id="conv-shared",
            owner_epoch=plugin.state.owner_epoch,
        )

        async def scenario():
            first_task = asyncio.create_task(
                plugin.reply_turn_run(first_request, {"message_text": "one"})
            )
            await started.wait()
            second = await plugin.reply_turn_run(
                second_request, {"message_text": "two"}
            )
            release.set()
            first = await first_task
            return first, second

        first, second = run(scenario())
        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "error")
        self.assertEqual(second.error_code, "turn_conflict")
        self.assertEqual(len(ctx.llm_calls), 1)


    def test_duplicate_delivery_key_rejected_without_model_call(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        first = make_request_context(
            turn_id="turn-a",
            conversation_id="conv-delivery",
            owner_epoch=plugin.state.owner_epoch,
        )
        second = make_request_context(
            turn_id="turn-b",
            conversation_id="conv-delivery-2",
            owner_epoch=plugin.state.owner_epoch,
        )
        shared_delivery = "reply:shared-key"
        first_response = run(
            plugin.reply_turn_run(
                first,
                {"message_text": "one", "delivery_key": shared_delivery},
            )
        )
        self.assertEqual(first_response.status, "ok")
        second_response = run(
            plugin.reply_turn_run(
                second,
                {"message_text": "two", "delivery_key": shared_delivery},
            )
        )
        self.assertEqual(second_response.status, "error")
        self.assertEqual(second_response.error_code, "duplicate_call")
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_descriptor_service_matches_plugin_methods(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        descriptor = ctx.qqbot_service_registry.get_provider("core_reply")
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        direct = run(plugin.reply_status(request, {}))
        via_service = run(descriptor.service.reply_status(request, {}))
        self.assertEqual(direct.status, via_service.status)
        self.assertEqual(direct.data["provider_id"], via_service.data["provider_id"])


if __name__ == "__main__":
    unittest.main()
