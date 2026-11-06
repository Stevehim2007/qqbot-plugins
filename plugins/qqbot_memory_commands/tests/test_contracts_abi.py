from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_DIR.parent
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from qqbot_memory_commands import contracts  # noqa: E402
from _support import make_context  # noqa: E402


class FixedProvider:
    async def memory_read(self, context, args):
        return contracts.make_result("ok", data={"method": "memory_read"})

    async def memory_search(self, context, args):
        return contracts.make_result("ok", data={"method": "memory_search"})

    async def memory_add(self, context, args):
        return contracts.make_result("ok", data={"method": "memory_add"})

    async def memory_preview_clear(self, context, args):
        return contracts.make_result("ok", data={"method": "memory_preview_clear"})

    async def memory_confirm_clear(self, context, args):
        return contracts.make_result("ok", data={"method": "memory_confirm_clear"})

    async def session_open(self, context, args):
        return contracts.make_result("ok", data={"method": "session_open"})

    async def session_read(self, context, args):
        return contracts.make_result("ok", data={"method": "session_read"})

    async def session_append(self, context, args):
        return contracts.make_result("ok", data={"method": "session_append"})

    async def session_reset(self, context, args):
        return contracts.make_result("ok", data={"method": "session_reset"})

    async def session_switch(self, context, args):
        return contracts.make_result("ok", data={"method": "session_switch"})

    async def persona_list(self, context, args):
        return contracts.make_result("ok", data={"method": "persona_list"})

    async def persona_bind(self, context, args):
        return contracts.make_result("ok", data={"method": "persona_bind"})

    async def persona_switch(self, context, args):
        return contracts.make_result("ok", data={"method": "persona_switch"})


class TestABIConstants(unittest.TestCase):
    def test_abi_version_and_exact_fixed_methods(self):
        self.assertEqual(contracts.ABI_VERSION, 1)
        self.assertEqual(contracts.PROTOCOL_VERSION, 1)
        self.assertEqual(
            contracts.FIXED_METHODS,
            (
                "memory_read",
                "memory_search",
                "memory_add",
                "memory_preview_clear",
                "memory_confirm_clear",
                "session_open",
                "session_read",
                "session_append",
                "session_reset",
                "session_switch",
                "persona_list",
                "persona_bind",
                "persona_switch",
            ),
        )
        self.assertEqual(set(contracts.METHOD_CAPABILITIES), set(contracts.FIXED_METHODS))

    def test_canonical_scope_id_contains_namespace_and_target(self):
        scope = contracts.ScopeRef(
            kind="group",
            target_id="group-9",
            instance_id="instance-a",
            platform="aiocqhttp",
            bot_id="bot-7",
        )
        self.assertIsNone(scope.validate())
        self.assertEqual(scope.canonical_id, "instance-a:aiocqhttp:bot-7:group:group-9")
        self.assertIn("instance-a", scope.canonical_id)
        self.assertIn("aiocqhttp", scope.canonical_id)
        self.assertIn("bot-7", scope.canonical_id)
        self.assertIn("group-9", scope.canonical_id)
        self.assertEqual(contracts.ScopeRef.from_mapping(scope.to_dict()), scope)

    def test_scope_validation_rejects_unknown_kind_and_missing_namespace(self):
        bad_kind = contracts.ScopeRef("speaker", "x", "i", "p", "b")
        self.assertEqual(bad_kind.validate(), "invalid_scope_kind")
        no_bot = contracts.ScopeRef("user", "x", "i", "p", "")
        self.assertEqual(no_bot.validate(), "missing_bot_id")

    def test_result_shape_and_idempotent_replay_preserved(self):
        raw = contracts.make_result("ok", data={"id": 1})
        raw["idempotent_replay"] = True
        result = contracts.normalize_result(raw)
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(result["data"], {"id": 1})
        self.assertEqual(
            set(result),
            {
                "status",
                "data",
                "error_code",
                "retryable",
                "side_effect_state",
                "provider_operation_id",
                "idempotent_replay",
            },
        )

    def test_malformed_backend_result_is_unknown_failure(self):
        result = contracts.normalize_result({"status": "made-up", "data": {}})
        self.assertEqual(result["status"], "internal_error")
        self.assertEqual(result["error_code"], "invalid_backend_result")
        self.assertEqual(result["side_effect_state"], "unknown")


class TestRequestContextValidation(unittest.TestCase):
    def test_valid_context(self):
        context = make_context("memory_read")
        validated, error = contracts.validate_request_context(
            context,
            expected_method="memory_read",
        )
        self.assertIs(validated, context)
        self.assertIsNone(error)

    def test_missing_required_field_is_terminal(self):
        context = make_context("memory_read")
        broken = contracts.RequestContext(
            **{**context.to_dict(), "actor_id": ""}
        )
        validated, error = contracts.validate_request_context(broken)
        self.assertIsNone(validated)
        self.assertEqual(error["status"], "invalid_args")
        self.assertEqual(error["error_code"], "invalid_request_context")

    def test_wrong_capability_is_rejected(self):
        context = make_context("memory_add")
        _, error = contracts.validate_request_context(
            context,
            expected_method="memory_read",
        )
        self.assertEqual(error["status"], "invalid_args")
        self.assertEqual(error["error_code"], "abi_mismatch")

    def test_expired_deadline_is_rejected(self):
        context = make_context("memory_read", deadline_seconds=-1)
        _, error = contracts.validate_request_context(
            context,
            expected_method="memory_read",
        )
        self.assertEqual(error["error_code"], "deadline_exceeded")

    def test_bad_epoch_and_protocol_are_rejected(self):
        context = make_context("memory_read")
        bad_epoch = contracts.RequestContext(
            **{**context.to_dict(), "owner_epoch": -1}
        )
        _, error = contracts.validate_request_context(bad_epoch)
        self.assertEqual(error["error_code"], "owner_epoch_conflict")
        bad_protocol = contracts.RequestContext(
            **{**context.to_dict(), "protocol_version": 2}
        )
        _, error = contracts.validate_request_context(bad_protocol)
        self.assertEqual(error["error_code"], "invalid_protocol")

    def test_mapping_context_is_coerced(self):
        context = make_context("memory_read")
        validated, error = contracts.validate_request_context(context.to_dict())
        self.assertIsNone(error)
        self.assertEqual(validated.actor_id, "actor-1")


class TestProviderRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_registration_readiness_dispatch_and_terminate(self):
        registry = contracts.ProviderRegistry()
        provider = FixedProvider()
        registry.register(provider, provider_id="memory-owner")
        self.assertFalse(registry.is_ready("memory-owner"))
        context = make_context("memory_read")
        result = await registry.dispatch("memory_read", context, {})
        self.assertEqual(result["status"], "not_ready")
        registry.mark_ready("memory-owner")
        self.assertTrue(registry.is_ready("memory-owner"))
        result = await registry.dispatch("memory_read", context, {})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["method"], "memory_read")
        self.assertTrue(registry.terminate("memory-owner"))
        self.assertFalse(registry.is_ready("memory-owner"))
        result = await registry.dispatch("memory_read", context, {})
        self.assertEqual(result["status"], "not_ready")
        self.assertFalse(registry.terminate("memory-owner"))

    async def test_duplicate_provider_id_and_method_owner_are_rejected(self):
        registry = contracts.ProviderRegistry()
        registry.register(FixedProvider(), provider_id="owner-1")
        with self.assertRaises(contracts.DuplicateProviderError):
            registry.register(FixedProvider(), provider_id="owner-1")
        with self.assertRaises(contracts.DuplicateProviderError):
            registry.register(FixedProvider(), provider_id="owner-2")
        self.assertEqual(set(registry.snapshot()["owners"].values()), {"owner-1"})

    async def test_incomplete_or_extra_abi_is_rejected(self):
        class Missing(FixedProvider):
            memory_read = None

        class Extra(FixedProvider):
            async def arbitrary_execute(self, context, args):
                return contracts.make_result("ok")

        with self.assertRaises(contracts.ProviderABIError):
            contracts.validate_fixed_provider(Missing())
        with self.assertRaises(contracts.ProviderABIError):
            contracts.validate_fixed_provider(Extra())

    async def test_unknown_dispatch_method_is_invalid_args(self):
        registry = contracts.ProviderRegistry()
        registry.register(FixedProvider(), provider_id="owner")
        registry.mark_ready("owner")
        result = await registry.dispatch("arbitrary_execute", make_context("memory_read"), {})
        self.assertEqual(result["status"], "invalid_args")
        self.assertEqual(result["error_code"], "invalid_args")

    async def test_readiness_snapshot_is_secret_free(self):
        registry = contracts.ProviderRegistry()
        registry.register(FixedProvider(), provider_id="owner")
        snapshot = registry.snapshot()
        self.assertIn("abi_version", snapshot)
        self.assertNotIn("password", str(snapshot).lower())
        self.assertEqual(snapshot["providers"][0]["provider_id"], "owner")


if __name__ == "__main__":
    unittest.main()
