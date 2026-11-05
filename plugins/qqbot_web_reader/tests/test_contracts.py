from __future__ import annotations

import asyncio
import time
import unittest

from _support import install_astrbot_stub

install_astrbot_stub()

import contracts
import paging


class _Context:
    pass


def run(coro):
    return asyncio.run(coro)


class RequestContextTests(unittest.TestCase):
    def test_make_request_context_is_complete(self):
        context = contracts.make_request_context(capability="web.read")
        for field in contracts.REQUIRED_REQUEST_CONTEXT_FIELDS:
            self.assertIn(field, context)
        self.assertIsNone(
            contracts.validate_request_context(
                context, expected_capability="web.read"
            )
        )

    def test_missing_field_invalid(self):
        context = contracts.make_request_context(capability="web.read")
        context.pop("actor_id")
        self.assertEqual(
            contracts.validate_request_context(context), "invalid_args"
        )

    def test_wrong_protocol_and_capability(self):
        context = contracts.make_request_context(
            capability="web.read", protocol_version=2
        )
        self.assertEqual(
            contracts.validate_request_context(context), "abi_mismatch"
        )
        context = contracts.make_request_context(capability="web.read")
        self.assertEqual(
            contracts.validate_request_context(
                context, expected_capability="web.next_page"
            ),
            "invalid_args",
        )

    def test_expired_deadline(self):
        context = contracts.make_request_context(
            capability="web.read", deadline=time.time() - 1
        )
        self.assertEqual(
            contracts.validate_request_context(context), "deadline_expired"
        )

    def test_call_id_and_args_hash_are_deterministic(self):
        context = contracts.make_request_context(capability="web.read")
        first = contracts.make_call_id(context, "web.read", {"url": "https://x"})
        second = contracts.make_call_id(context, "web.read", {"url": "https://x"})
        self.assertEqual(first, second)
        self.assertEqual(
            contracts.args_hash({"a": 1, "b": 2}),
            contracts.args_hash({"b": 2, "a": 1}),
        )

    def test_tool_result_has_required_fields(self):
        ok = contracts.tool_result_ok("call_1", "web.page", {"text": "x"})
        error = contracts.tool_result_error("call_2", "not_ready")
        for result in (ok, error):
            for field in contracts.REQUIRED_TOOL_RESULT_FIELDS:
                self.assertIn(field, result)
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(error["status"], "error")


class RegistryTests(unittest.TestCase):
    def test_shared_registry_is_attached_to_context(self):
        context = _Context()
        first = contracts.get_shared_registry(context)
        second = contracts.get_shared_registry(context)
        self.assertIs(first, second)
        self.assertEqual(first.abi_version, contracts.ABI_VERSION)

    def test_registration_is_not_ready_until_marked(self):
        registry = contracts.SharedRegistry()
        registry.register_provider(
            "owner-a", "web.read", {"web_read": lambda ctx, args: {}}
        )
        with self.assertRaises(contracts.ContractError) as ctx:
            registry.resolve("web.read", require_ready=True)
        self.assertEqual(ctx.exception.code, "not_ready")
        registry.mark_ready("owner-a", ["web.read"])
        self.assertTrue(registry.resolve("web.read").ready)

    def test_duplicate_provider_from_other_owner_rejected(self):
        registry = contracts.SharedRegistry()
        registry.register_provider(
            "owner-a", "web.read", {"web_read": lambda ctx, args: {}}
        )
        with self.assertRaises(contracts.ContractError) as ctx:
            registry.register_provider(
                "owner-b", "web.read", {"web_read": lambda ctx, args: {}}
            )
        self.assertEqual(ctx.exception.code, "duplicate_provider")

    def test_same_owner_duplicate_registration_is_rejected(self):
        registry = contracts.SharedRegistry()
        registry.register_provider(
            "owner-a", "web.read", {"web_read": lambda ctx, args: 1}
        )
        with self.assertRaises(contracts.ContractError) as ctx:
            registry.register_provider(
                "owner-a", "web.read", {"web_read": lambda ctx, args: 2}
            )
        self.assertEqual(ctx.exception.code, "duplicate_provider")

    def test_owner_epoch_mismatch_is_rejected(self):
        registry = contracts.SharedRegistry()
        registry.register_provider(
            "owner-a",
            "web.read",
            {"web_read": lambda ctx, args: {}},
            owner_epoch=3,
        )
        registry.mark_ready("owner-a", ["web.read"])
        with self.assertRaises(contracts.ContractError) as ctx:
            registry.resolve("web.read", owner_epoch=2, require_ready=True)
        self.assertEqual(ctx.exception.code, "owner_epoch_mismatch")
        self.assertTrue(
            registry.resolve("web.read", owner_epoch=3, require_ready=True).ready
        )

    def test_unregister_owner_removes_all_capabilities(self):
        registry = contracts.SharedRegistry()
        registry.register_provider(
            "owner-a", "web.read", {"web_read": lambda ctx, args: {}}
        )
        registry.register_provider(
            "owner-a", "web.next_page", {"web_next_page": lambda ctx, args: {}}
        )
        self.assertEqual(
            registry.unregister_owner("owner-a"),
            2,
        )
        self.assertIsNone(registry.get_provider("web.read"))
        with self.assertRaises(contracts.ContractError) as ctx:
            registry.resolve("web.read")
        self.assertEqual(ctx.exception.code, "dependency_missing")

    def test_abi_mismatch_on_foreign_context_attribute(self):
        context = _Context()
        setattr(context, contracts.REGISTRY_ATTRIBUTE, object())
        with self.assertRaises(contracts.ContractError) as ctx:
            contracts.get_shared_registry(context)
        self.assertEqual(ctx.exception.code, "abi_mismatch")


class ProviderCallTests(unittest.TestCase):
    def test_call_provider_async_routes_arguments(self):
        registry = contracts.SharedRegistry()
        seen = {}

        async def sticker(ctx, args):
            seen["ctx"] = ctx
            seen["args"] = args
            return contracts.tool_result_ok("call_s", "sticker.collect_plan", {"ok": True})

        registry.register_provider(
            "qqbot_stickers",
            "sticker.collect_plan",
            {"sticker_collect_plan": sticker},
            owner_epoch=2,
        )
        registry.mark_ready("qqbot_stickers", ["sticker.collect_plan"])
        request_context = contracts.make_request_context(
            capability="image.collect_plan"
        )
        result = run(
            contracts.call_provider_async(
                registry,
                "sticker.collect_plan",
                request_context,
                {"result_id": "r1"},
            )
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen["args"], {"result_id": "r1"})

    def test_call_provider_missing_is_dependency_missing(self):
        registry = contracts.SharedRegistry()
        request_context = contracts.make_request_context(
            capability="image.collect_plan"
        )
        result = run(
            contracts.call_provider_async(
                registry,
                "sticker.collect_plan",
                request_context,
                {"result_id": "r1"},
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "dependency_missing")


class PagingTests(unittest.TestCase):
    def test_cursor_is_opaque_signed_and_target_bound(self):
        now = [1000.0]
        signer = paging.OpaqueCursor(
            secret=b"x" * 32,
            ttl_seconds=30,
            clock=lambda: now[0],
        )
        token = signer.encode(
            {"actor_id": "a1", "scope_key": contracts.scope_key(
                contracts.make_request_context(actor_id="a1")
            ), "url": "https://example.com/"},
            now=now[0],
        )
        self.assertNotIn("example.com", token)
        payload = signer.decode(
            token,
            actor_id="a1",
            target_field="url",
            target_value="https://example.com/",
        )
        self.assertEqual(payload["url"], "https://example.com/")
        with self.assertRaises(paging.CursorError) as ctx:
            signer.decode(token, target_field="url", target_value="https://other/")
        self.assertEqual(ctx.exception.code, "cursor_target_mismatch")
        with self.assertRaises(paging.CursorError) as ctx:
            signer.decode(token + "x")
        self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_cursor_expiry(self):
        now = [1000.0]
        signer = paging.OpaqueCursor(
            secret=b"y" * 32,
            ttl_seconds=5,
            clock=lambda: now[0],
        )
        token = signer.encode({"actor_id": "a1"}, now=now[0])
        now[0] += 6
        with self.assertRaises(paging.CursorError) as ctx:
            signer.decode(token)
        self.assertEqual(ctx.exception.code, "cursor_expired")

    def test_ttl_cache_stale_flags_and_max_entries(self):
        now = [1000.0]
        cache = paging.TTLCache(
            max_entries=1,
            ttl_seconds=5,
            clock=lambda: now[0],
        )
        self.assertTrue(cache.put("one", "value"))
        self.assertEqual(cache.get("one"), ("value", False))
        now[0] += 6
        self.assertEqual(cache.get("one"), ("value", True))
        self.assertTrue(cache.put("two", "value2"))
        self.assertEqual(cache.get("one"), (None, True))
        self.assertEqual(cache.get("two"), ("value2", False))


if __name__ == "__main__":
    unittest.main()
