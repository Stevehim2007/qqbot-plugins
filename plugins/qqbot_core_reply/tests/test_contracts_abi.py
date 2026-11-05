from __future__ import annotations

import asyncio
import unittest

from support import (
    FakeContext,
    FakeEvent,
    collect_event,
    extras,
    make_plugin,
    make_request_context,
)
import contracts
import main as core_main


def run(coro):
    return asyncio.run(coro)


class RequestContextTests(unittest.TestCase):
    def test_dict_compatible_round_trip(self):
        context = make_request_context(turn_id="t-1")
        self.assertEqual(context["turn_id"], "t-1")
        self.assertEqual(context.get("capability"), "reply.turn")
        payload = context.to_dict()
        self.assertEqual(payload["protocol_version"], 1)
        loaded = contracts.load_request_context(payload)
        self.assertEqual(loaded.turn_id, "t-1")
        self.assertEqual(loaded.owner_epoch, 1)
        self.assertEqual(set(payload), set(context.keys()))

    def test_required_fields_fail_closed(self):
        for field in (
            "instance_id",
            "platform",
            "bot_id",
            "actor_id",
            "conversation_id",
            "source_message_id",
            "turn_id",
            "capability",
        ):
            payload = make_request_context().to_dict()
            payload[field] = ""
            with self.subTest(field=field):
                with self.assertRaises(contracts.ContractError) as caught:
                    contracts.load_request_context(payload)
                self.assertEqual(caught.exception.error_code, "invalid_args")

    def test_protocol_version_mismatch(self):
        payload = make_request_context(protocol_version=2).to_dict()
        with self.assertRaises(contracts.ContractError) as caught:
            contracts.load_request_context(payload)
        self.assertEqual(caught.exception.error_code, "abi_mismatch")

    def test_owner_epoch_type_validation(self):
        payload = make_request_context().to_dict()
        payload["owner_epoch"] = "newest"
        with self.assertRaises(contracts.ContractError):
            contracts.load_request_context(payload)

    def test_error_codes_required_set(self):
        for code in (
            "permission_denied",
            "invalid_args",
            "not_ready",
            "revision_conflict",
            "confirmation_expired",
            "outcome_unknown",
            "dependency_missing",
        ):
            self.assertIn(code, contracts.ERROR_CODES)
            self.assertTrue(contracts.is_valid_error_code(code))
        self.assertFalse(contracts.is_valid_error_code("made_up"))


class ToolResultTests(unittest.TestCase):
    def test_success_result(self):
        result = contracts.validate_tool_result(
            {
                "status": "ok",
                "call_id": "c1",
                "result_type": "data",
                "data": {"x": 1},
            }
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result["call_id"], "c1")

    def test_error_requires_known_error_code(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_tool_result(
                {"status": "error", "call_id": "c1", "error_code": "nonsense"}
            )
        result = contracts.validate_tool_result(
            {
                "status": "error",
                "call_id": "c1",
                "error_code": "outcome_unknown",
                "retryable": False,
            }
        )
        self.assertEqual(result.error_code, "outcome_unknown")

    def test_missing_call_id_rejected(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_tool_result({"status": "ok"})


class ReplyPlanTests(unittest.TestCase):
    def test_reply_plan_validation_and_serialization(self):
        context = make_request_context()
        plan = contracts.validate_reply_plan(
            {
                "request_context": context.to_dict(),
                "text_segments": ["hello"],
                "media_segments": [],
                "terminal": True,
                "delivery_key": "delivery-1",
            }
        )
        payload = plan.to_dict()
        self.assertEqual(payload["text_segments"], ["hello"])
        self.assertIsInstance(payload["request_context"], dict)
        self.assertEqual(payload["request_context"]["turn_id"], context.turn_id)

    def test_reply_plan_rejects_bad_segments(self):
        with self.assertRaises(contracts.ContractError):
            contracts.validate_reply_plan(
                {
                    "request_context": make_request_context().to_dict(),
                    "text_segments": [123],
                    "media_segments": [],
                    "terminal": True,
                    "delivery_key": "d",
                }
            )


class RegistryTests(unittest.TestCase):
    def test_duplicate_owner_rejected(self):
        registry = contracts.RuntimeRegistry()
        registry.register_provider("p", owner="owner-a", capability="reply")
        with self.assertRaises(contracts.ContractError) as caught:
            registry.register_provider("p", owner="owner-b", capability="reply")
        self.assertEqual(caught.exception.error_code, "duplicate_owner")

    def test_same_owner_idempotent(self):
        registry = contracts.RuntimeRegistry()
        first = registry.register_provider("p", owner="a", capability="reply")
        second = registry.register_provider("p", owner="a", capability="reply")
        self.assertIs(first, second)
        self.assertEqual(registry.get_provider("p").owner, "a")

    def test_abi_mismatch_rejected(self):
        registry = contracts.RuntimeRegistry()
        with self.assertRaises(contracts.ContractError) as caught:
            registry.register_provider("p", abi_version=2)
        self.assertEqual(caught.exception.error_code, "abi_mismatch")

    def test_ready_owner_and_epoch_checks(self):
        registry = contracts.RuntimeRegistry()
        registry.register_provider("p", owner="a", owner_epoch=3, ready=False)
        self.assertFalse(registry.is_ready("p"))
        registry.set_ready("p", True, owner="a", owner_epoch=3)
        self.assertTrue(registry.is_ready("p"))
        with self.assertRaises(contracts.ContractError) as caught:
            registry.set_ready("p", False, owner="b")
        self.assertEqual(caught.exception.error_code, "permission_denied")
        with self.assertRaises(contracts.ContractError) as caught:
            registry.set_ready("p", False, owner="a", owner_epoch=4)
        self.assertEqual(caught.exception.error_code, "owner_epoch_mismatch")

    def test_unregister_owner_check(self):
        registry = contracts.RuntimeRegistry()
        registry.register_provider("p", owner="a")
        with self.assertRaises(contracts.ContractError):
            registry.unregister("p", owner="b")
        self.assertTrue(registry.unregister("p", owner="a"))
        self.assertFalse(registry.unregister("p"))

    def test_context_registry_aliases_are_same_object(self):
        ctx = FakeContext()
        registry = contracts.ensure_registry(ctx)
        self.assertIs(ctx.qqbot_service_registry, registry)
        self.assertIs(ctx.qqbot_services, registry)
        self.assertIs(ctx.qqbot_contract_registry, registry)
        self.assertIs(contracts.ensure_registry(ctx), registry)


class PluginProviderTests(unittest.TestCase):
    def test_default_disabled_registers_unready_provider(self):
        plugin, ctx, _ = make_plugin(config=None)
        descriptor = ctx.qqbot_service_registry.get_provider("core_reply")
        self.assertIsNotNone(descriptor)
        self.assertFalse(descriptor.ready)
        self.assertEqual(
            set(descriptor.method_names()),
            set(core_main.CoreReplyService.METHOD_NAMES),
        )
        self.assertIs(descriptor.service, plugin.service)

    def test_enabled_registers_ready_provider_and_fixed_methods(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        descriptor = ctx.qqbot_service_registry.get_provider("core_reply")
        self.assertTrue(descriptor.ready)
        for name in (
            "reply_status",
            "reply_configure_plan",
            "reply_configure_commit",
            "reply_turn_run",
            "reply_cancel_turn",
        ):
            self.assertTrue(callable(getattr(plugin, name)))
            self.assertTrue(callable(getattr(descriptor.service, name)))

    def test_duplicate_plugin_owner_rejected(self):
        ctx = FakeContext()
        core_main.CoreReplyPlugin(ctx, config={"enabled": True})
        with self.assertRaises(contracts.ContractError) as caught:
            core_main.CoreReplyPlugin(ctx, config={"enabled": True})
        self.assertEqual(caught.exception.error_code, "duplicate_owner")

    def test_disabled_turn_run_fails_closed_without_model_call(self):
        plugin, ctx, _ = make_plugin(config=None)
        request = make_request_context(owner_epoch=plugin.state.owner_epoch)
        response = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(response.status, "error")
        self.assertEqual(response.error_code, "not_ready")
        self.assertEqual(ctx.llm_calls, [])

    def test_owner_epoch_mismatch_fails_closed(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        request = make_request_context(owner_epoch=plugin.state.owner_epoch + 1)
        response = run(plugin.reply_turn_run(request, {"message_text": "hello"}))
        self.assertEqual(response.status, "error")
        self.assertEqual(response.error_code, "owner_epoch_mismatch")
        self.assertEqual(ctx.llm_calls, [])

    def test_terminate_unregisters_and_cancels_readiness(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        self.assertTrue(ctx.qqbot_service_registry.is_ready("core_reply"))
        run(plugin.terminate())
        self.assertFalse(plugin.state.ready)
        self.assertTrue(plugin.state.terminated)
        self.assertIsNone(ctx.qqbot_service_registry.get_provider("core_reply"))
        self.assertFalse(getattr(ctx, "qqbot_core_reply_ready"))

    def test_disabled_plugin_blocks_native_default_llm(self):
        plugin, ctx, _ = make_plugin(config=None)
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])
        self.assertTrue(event.llm_blocked)


if __name__ == "__main__":
    unittest.main()
