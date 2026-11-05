from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from support import Context, FakeFoodAPI, FoodPlugin, run
from qqbot_food import contracts


def ctx_dict(actor="111", scope=None, **extra):
    value = {
        "protocol_version": 1,
        "instance_id": "instance-a",
        "platform": "aiocqhttp",
        "bot_id": "bot-a",
        "actor_id": actor,
        "conversation_id": "conv-1",
        "persona_id": "persona-1",
        "source_message_id": "msg-1",
        "turn_id": "turn-1",
        "capability": "food",
        "requested_scope": scope or {"kind": "user", "id": actor},
        "owner_epoch": 1,
        "deadline": 0,
    }
    value.update(extra)
    return value


class RequestContextTests(unittest.TestCase):
    def test_coerce_defaults_and_normalizes_scope(self):
        ctx = contracts.coerce_request_context(ctx_dict(actor="123"))
        self.assertEqual(ctx.protocol_version, 1)
        self.assertEqual(ctx.actor_id, "123")
        self.assertEqual(ctx.requested_scope, {"kind": "user", "id": "123"})
        self.assertEqual(ctx.capability, "food")

    def test_missing_actor_is_permission_denied(self):
        with self.assertRaises(contracts.ContractError) as raised:
            contracts.coerce_request_context(ctx_dict(actor=""))
        self.assertEqual(raised.exception.code, contracts.ERR_PERMISSION_DENIED)

    def test_missing_instance_and_bad_protocol_are_invalid(self):
        with self.assertRaises(contracts.ContractError):
            contracts.coerce_request_context({**ctx_dict(), "instance_id": ""})
        with self.assertRaises(contracts.ContractError):
            contracts.coerce_request_context({**ctx_dict(), "protocol_version": 2})

    def test_scope_key_is_actor_scope_and_persona_in_token_tests(self):
        first = contracts.coerce_request_context(ctx_dict(actor="1"))
        second = contracts.coerce_request_context(
            ctx_dict(actor="1", scope={"kind": "group", "id": "g1"})
        )
        self.assertNotEqual(contracts.scope_key(first), contracts.scope_key(second))
        self.assertEqual(len(contracts.scope_key(first)), 64)


class ProviderResultTests(unittest.TestCase):
    def test_ok_and_error_shape(self):
        ok = contracts.ProviderResult.ok({"x": 1}, provider_operation_id="op").to_dict()
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["data"], {"x": 1})
        self.assertEqual(ok["provider_operation_id"], "op")
        self.assertEqual(ok["abi_version"], 1)
        err = contracts.ProviderResult.error(
            contracts.ERR_REVISION_CONFLICT, "old", retryable=False
        ).to_dict()
        self.assertEqual(err["status"], "error")
        self.assertEqual(err["error_code"], contracts.ERR_REVISION_CONFLICT)
        self.assertEqual(err["side_effect_state"], "none")

    def test_status_transitions(self):
        self.assertTrue(contracts.can_transition("collecting", "draft"))
        self.assertTrue(contracts.can_transition("draft", "awaiting_confirmation"))
        self.assertTrue(contracts.can_transition("awaiting_confirmation", "confirmed"))
        self.assertTrue(contracts.can_transition("confirmed", "submitting"))
        self.assertTrue(contracts.can_transition("submitting", "succeeded"))
        self.assertTrue(contracts.can_transition("submitting", "outcome_unknown"))
        self.assertFalse(contracts.can_transition("succeeded", "submitting"))


class RegistryTests(unittest.TestCase):
    def test_register_ready_unregister(self):
        registry = contracts.ProviderRegistry()
        provider = object()
        registry.register(owner="qqbot_food", capability="food", provider=provider)
        self.assertFalse(registry.is_ready("food"))
        self.assertTrue(registry.set_ready("qqbot_food", True))
        self.assertTrue(registry.is_ready("food"))
        record = registry.get("food")
        self.assertIs(record.provider, provider)
        self.assertEqual(registry.unregister("qqbot_food"), ["food"])
        self.assertIsNone(registry.get("food"))

    def test_duplicate_capability_and_owner_are_rejected(self):
        registry = contracts.ProviderRegistry()
        registry.register(owner="qqbot_food", capability="food", provider=object())
        with self.assertRaises(contracts.ProviderRegistrationError):
            registry.register(owner="other", capability="food", provider=object())
        registry2 = contracts.ProviderRegistry()
        registry2.register(owner="qqbot_food", capability="food", provider=object())
        with self.assertRaises(contracts.ProviderRegistrationError):
            registry2.register(owner="qqbot_food", capability="other", provider=object())

    def test_abi_mismatch_rejected(self):
        registry = contracts.ProviderRegistry()
        with self.assertRaises(contracts.ProviderRegistrationError):
            registry.register(
                owner="qqbot_food",
                capability="food",
                provider=object(),
                abi_version=2,
            )

    def test_context_attaches_shared_registry(self):
        context = Context()
        first = contracts.get_or_create_registry(context)
        second = contracts.get_or_create_registry(context)
        self.assertIs(first, second)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.ctx = contracts.coerce_request_context(ctx_dict(actor="111"))
        self.issuer = contracts.ConfirmationTokenIssuer("secret", ttl_seconds=10)

    def test_issue_and_verify(self):
        token, expires = self.issuer.issue(
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=100,
        )
        self.assertEqual(expires, 110)
        result = self.issuer.verify(
            token,
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=105,
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.payload["revision"], 3)

    def test_expiry_and_actor_binding(self):
        token, _ = self.issuer.issue(
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=100,
        )
        expired = self.issuer.verify(
            token,
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=111,
        )
        self.assertFalse(expired.valid)
        self.assertEqual(expired.code, contracts.ERR_CONFIRMATION_EXPIRED)
        other = contracts.coerce_request_context(ctx_dict(actor="222"))
        self.assertEqual(
            self.issuer.verify(
                token,
                other,
                kind="food_draft_confirm",
                draft_id="d1",
                revision=3,
                payload_hash_value="h1",
                now=105,
            ).code,
            contracts.ERR_PERMISSION_DENIED,
        )

    def test_revision_and_payload_binding(self):
        token, _ = self.issuer.issue(
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=100,
        )
        wrong_revision = self.issuer.verify(
            token,
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=4,
            payload_hash_value="h1",
            now=105,
        )
        self.assertEqual(wrong_revision.code, contracts.ERR_REVISION_CONFLICT)
        wrong_payload = self.issuer.verify(
            token,
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h2",
            now=105,
        )
        self.assertEqual(wrong_payload.code, contracts.ERR_REVISION_CONFLICT)

    def test_tampered_token_rejected(self):
        token, _ = self.issuer.issue(
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=100,
        )
        result = self.issuer.verify(
            token[:-2] + "xx",
            self.ctx,
            kind="food_draft_confirm",
            draft_id="d1",
            revision=3,
            payload_hash_value="h1",
            now=105,
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.code, contracts.ERR_CONFIRMATION_INVALID)


class PluginRegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_plugin_registers_ready_provider_and_terminate_cleans(self):
        context = Context()
        plugin = FoodPlugin(
            context,
            config={"provider_instance_id": "i", "provider_owner_epoch": 1},
            data_dir=Path(self.tmp.name) / "a",
            api_client=FakeFoodAPI(),
        )

        async def scenario():
            status = plugin.provider_status()
            self.assertTrue(status["ready"])
            record = contracts.get_or_create_registry(context).get("food")
            self.assertIsNotNone(record)
            self.assertIs(record.provider, plugin._contract_provider)
            await plugin.terminate()
            self.assertIsNone(contracts.get_or_create_registry(context).get("food"))
            self.assertFalse(plugin.provider_status()["ready"])
            result = await plugin.food_query({"actor_id": "1", "instance_id": "i"}, {})
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error_code"], contracts.ERR_NOT_READY)

        run(scenario())

    def test_duplicate_provider_owner_same_context_is_rejected(self):
        context = Context()
        FoodPlugin(
            context,
            config={},
            data_dir=Path(self.tmp.name) / "a",
            api_client=FakeFoodAPI(),
        )
        with self.assertRaises(contracts.ProviderRegistrationError):
            FoodPlugin(
                context,
                config={},
                data_dir=Path(self.tmp.name) / "b",
                api_client=FakeFoodAPI(),
            )


if __name__ == "__main__":
    unittest.main()
