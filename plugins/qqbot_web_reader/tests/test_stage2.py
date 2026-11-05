from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import Context, FakeEvent, install_astrbot_stub, import_main

install_astrbot_stub()
main = import_main()
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


async def collect(generator):
    return [item async for item in generator]


def run(coro):
    return asyncio.run(coro)


def page(text="Body", title="Title", url="https://example.com/"):
    return main.web_fetch.PageResult(
        url,
        title,
        text,
        False,
        "text/html",
    )


def request_context(capability, *, actor="a1", scope=None, epoch=1):
    return main.contracts.make_request_context(
        capability=capability,
        actor_id=actor,
        requested_scope=scope or {"type": "private", "id": actor},
        owner_epoch=epoch,
    )


class CanonicalCommandTests(unittest.TestCase):
    def test_all_canonical_and_alias_spellings_work(self):
        plugin = main.WebReaderPlugin(context=None, config={})
        messages = (
            "/网页读取 https://example.com/",
            "/网页 https://example.com/",
            "/读网页 https://example.com/",
            "/web https://example.com/",
        )
        for message in messages:
            with self.subTest(message=message):
                event = FakeEvent(message, private=True)
                with patch("main.web_fetch.fetch_page_text", return_value=page("AliasBody")):
                    results = run(collect(plugin.read_web(event)))
                self.assertEqual(len(results), 1)
                self.assertIn("AliasBody", results[0].text)
                self.assertTrue(event.stopped)

    def test_parse_command_actions(self):
        self.assertEqual(main.parse_command("/网页读取 https://example.com/x"), ("read", "https://example.com/x"))
        self.assertEqual(main.parse_command("/网页读取 下一页"), ("next", ""))
        self.assertEqual(main.parse_command("/web 下一页"), ("next", ""))
        self.assertEqual(main.parse_command("/网页读取"), ("usage", ""))
        self.assertEqual(main.parse_command("/网页读取 下一页 extra"), ("invalid", ""))

    def test_next_page_command_uses_actor_scope_bound_cursor(self):
        plugin = main.WebReaderPlugin(
            context=None,
            config={"paging.page_chars": 5, "cache.ttl_seconds": 60, "cache.max_entries": 10},
        )
        text = "abcdefghijklmnopqrstuvwxyz"
        with patch("main.web_fetch.fetch_page_text", return_value=page(text)):
            first = run(collect(plugin.read_web(FakeEvent("/网页读取 https://example.com/", private=True))))
            second = run(collect(plugin.read_web(FakeEvent("/网页读取 下一页", private=True))))
            third = run(collect(plugin.read_web(FakeEvent("/web 下一页", private=True))))
        self.assertIn("abcde", first[0].text)
        self.assertIn("下一页", first[0].text)
        self.assertIn("fghij", second[0].text)
        self.assertIn("klmno", third[0].text)

    def test_next_page_without_previous_read_is_terminal(self):
        plugin = main.WebReaderPlugin(context=None, config={})
        event = FakeEvent("/网页读取 下一页", private=True)
        results = run(collect(plugin.read_web(event)))
        self.assertEqual(results[0].text, plugin._cfg_str("no_cursor_reply"))
        self.assertTrue(event.stopped)


class ProviderLifecycleTests(unittest.TestCase):
    def test_readiness_separates_registration_and_initialize(self):
        context = Context()
        plugin = main.WebReaderPlugin(context=context, config={})
        self.assertFalse(plugin._registry.get_provider("web.read").ready)
        rc = request_context("web.read")
        result = run(plugin.web_read(rc, {"url": "https://example.com/"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "not_ready")
        run(plugin.initialize())
        self.assertTrue(plugin._registry.get_provider("web.read").ready)

    def test_duplicate_provider_is_rejected(self):
        context = Context()
        main.WebReaderPlugin(context=context, config={})
        with self.assertRaises(main.contracts.ContractError) as ctx:
            main.WebReaderPlugin(context=context, config={})
        self.assertEqual(ctx.exception.code, "duplicate_provider")

    def test_terminate_removes_capabilities_and_caches(self):
        context = Context()
        plugin = main.WebReaderPlugin(
            context=context, config={"paging.page_chars": 10, "cache.max_entries": 10}
        )
        run(plugin.initialize())
        with patch("main.web_fetch.fetch_page_text", return_value=page("x" * 100)):
            run(plugin.web_read(request_context("web.read"), {"url": "https://example.com/"}))
        self.assertGreater(len(plugin._cache), 0)
        run(plugin.terminate())
        self.assertIsNone(plugin._registry.get_provider("web.read"))
        self.assertEqual(len(plugin._cache), 0)
        result = run(plugin.web_read(request_context("web.read"), {"url": "https://example.com/"}))
        self.assertEqual(result["status"], "error")
        self.assertIn(result["error_code"], {"not_ready", "dependency_missing"})


class ProviderReadTests(unittest.TestCase):
    def setUp(self):
        self.context = Context()
        self.plugin = main.WebReaderPlugin(
            context=self.context,
            config={
                "paging.page_chars": 5,
                "cache.ttl_seconds": 60,
                "cache.max_entries": 10,
                "fetch.timeout_seconds": 5,
            },
        )
        run(self.plugin.initialize())
        self.scope = {"type": "private", "id": "a1"}
        self.rc = request_context("web.read", actor="a1", scope=self.scope)
        self.next_rc = request_context("web.next_page", actor="a1", scope=self.scope)
        self.text = "abcdefghijklmnopqrstuvwxyz"

    def test_web_read_and_web_next_page(self):
        with patch("main.web_fetch.fetch_page_text", return_value=page(self.text)):
            first = run(self.plugin.web_read(self.rc, {"url": "https://example.com/"}))
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["result_type"], "web.page")
        self.assertEqual(first["data"]["text"], "abcde")
        self.assertTrue(first["data"]["has_next"])
        cursor = first["data"]["cursor"]
        with patch("main.web_fetch.fetch_page_text", return_value=page(self.text)):
            second = run(self.plugin.web_next_page(self.next_rc, {"cursor": cursor}))
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["data"]["text"], "fghij")
        self.assertEqual(second["data"]["page"], 2)
        self.assertFalse(second["data"]["stale"])

    def test_cursor_wrong_actor_and_scope_rejected(self):
        with patch("main.web_fetch.fetch_page_text", return_value=page(self.text)):
            first = run(self.plugin.web_read(self.rc, {"url": "https://example.com/"}))
        cursor = first["data"]["cursor"]
        wrong_actor = request_context(
            "web.next_page", actor="a2", scope={"type": "private", "id": "a2"}
        )
        result = run(self.plugin.web_next_page(wrong_actor, {"cursor": cursor}))
        self.assertEqual(result["error_code"], "cursor_actor_mismatch")
        wrong_scope = request_context(
            "web.next_page", actor="a1", scope={"type": "group", "id": "g1"}
        )
        result = run(self.plugin.web_next_page(wrong_scope, {"cursor": cursor}))
        self.assertEqual(result["error_code"], "cursor_scope_mismatch")

    def test_tampered_cursor_rejected(self):
        with patch("main.web_fetch.fetch_page_text", return_value=page(self.text)):
            first = run(self.plugin.web_read(self.rc, {"url": "https://example.com/"}))
        cursor = first["data"]["cursor"]
        result = run(self.plugin.web_next_page(self.next_rc, {"cursor": cursor + "x"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "invalid_cursor")

    def test_owner_epoch_mismatch_rejected(self):
        wrong_epoch = request_context("web.read", actor="a1", scope=self.scope, epoch=2)
        result = run(self.plugin.web_read(wrong_epoch, {"url": "https://example.com/"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "owner_epoch_mismatch")

    def test_arbitrary_host_override_argument_rejected(self):
        result = run(
            self.plugin.web_read(
                self.rc,
                {"url": "https://example.com/", "host": "127.0.0.1"},
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "invalid_args")

    def test_expired_cache_returns_stale_flag(self):
        plugin = main.WebReaderPlugin(
            context=None,
            config={
                "paging.page_chars": 5,
                "cache.ttl_seconds": 0,
                "cache.max_entries": 10,
            },
        )
        run(plugin.initialize())
        rc = request_context("web.read")
        next_rc = request_context("web.next_page")
        with patch("main.web_fetch.fetch_page_text", return_value=page(self.text)):
            first = run(plugin.web_read(rc, {"url": "https://example.com/"}))
            second = run(plugin.web_next_page(next_rc, {"cursor": first["data"]["cursor"]}))
        self.assertEqual(second["status"], "ok")
        self.assertTrue(second["data"]["stale"])

    def test_cache_max_entries_evicts_old_page(self):
        plugin = main.WebReaderPlugin(
            context=None,
            config={
                "paging.page_chars": 5,
                "cache.ttl_seconds": 60,
                "cache.max_entries": 1,
            },
        )
        run(plugin.initialize())
        rc = request_context("web.read")
        next_rc = request_context("web.next_page")
        with patch("main.web_fetch.fetch_page_text", return_value=page(self.text)):
            first = run(plugin.web_read(rc, {"url": "https://example.com/one"}))
            run(plugin.web_read(rc, {"url": "https://example.com/two"}))
            second = run(
                plugin.web_next_page(next_rc, {"cursor": first["data"]["cursor"]})
            )
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["error_code"], "cache_miss")
        self.assertTrue(second["data"]["stale"])

    def test_allowed_hosts_are_enforced_by_engine(self):
        with self.assertRaises(main.web_fetch.WebFetchError) as ctx:
            main.web_fetch.validate_fetch_url(
                "https://other.example/",
                allowed_hosts=["example.com"],
            )
        self.assertEqual(ctx.exception.reason, "blocked_host")
        self.assertEqual(
            main.web_fetch.validate_fetch_url(
                "https://sub.example.com/",
                allowed_hosts=["*.example.com"],
            ),
            "https://sub.example.com/",
        )



class _ForeignError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _ForeignRegistry:
    """Simulates the other plugin's contracts.py raising its own class."""

    abi_version = 1

    def __init__(self):
        self.registered = []

    def register_provider(self, owner, capability, methods, *, owner_epoch=1, abi_version=1):
        self.registered.append((owner, capability, owner_epoch))

    def has_provider(self, capability):
        return True

    def mark_ready(self, owner, capabilities=None):
        return None

    def resolve(self, capability, *, owner_epoch=None, require_ready=True):
        raise _ForeignError("owner_epoch_mismatch")

    def unregister_owner(self, owner):
        return 0

    def status(self):
        return {"abi_version": 1, "providers": {}, "owners": {}}


class CrossModuleContractTests(unittest.TestCase):
    def test_foreign_contract_error_is_mapped(self):
        context = Context()
        setattr(context, main.contracts.REGISTRY_ATTRIBUTE, _ForeignRegistry())
        plugin = main.WebReaderPlugin(context=context, config={})
        run(plugin.initialize())
        result = run(plugin.web_read(request_context("web.read"), {"url": "https://example.com/"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "owner_epoch_mismatch")


class CanonicalSchemaTests(unittest.TestCase):
    def test_canonical_groups_exist(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        required = {
            "commands.aliases",
            "fetch.timeout_seconds",
            "fetch.max_bytes",
            "fetch.max_redirects",
            "fetch.allowed_hosts",
            "extract.mode",
            "extract.max_chars",
            "paging.page_chars",
            "cache.ttl_seconds",
            "cache.max_entries",
            "policy.policy_id",
            "policy.owner_epoch",
        }
        self.assertTrue(required.issubset(schema))
        for key in required:
            self.assertIn("default", schema[key], key)

    def test_canonical_defaults_align(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        for key in (
            "commands.aliases",
            "fetch.timeout_seconds",
            "fetch.max_bytes",
            "fetch.max_redirects",
            "fetch.allowed_hosts",
            "extract.mode",
            "extract.max_chars",
            "paging.page_chars",
            "cache.ttl_seconds",
            "cache.max_entries",
            "policy.policy_id",
            "policy.owner_epoch",
        ):
            self.assertEqual(schema[key]["default"], main.DEFAULTS[key], key)


if __name__ == "__main__":
    unittest.main()
