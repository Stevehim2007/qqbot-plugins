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


def candidate(index=0, url=None, title="猫"):
    return {
        "url": url or f"https://cdn.example.com/{index}.png",
        "title": title,
        "description": title,
        "source": "bing",
        "referer": main.image_search.BING_IMAGES_REFERER,
    }


def downloaded(url="https://cdn.example.com/0.png"):
    suffix = Path(url).suffix or ".png"
    return main.image_search.DownloadedImage(
        f"/tmp/qqbot-stage2-{abs(hash(url))}{suffix}",
        "image/png",
        suffix,
        url,
    )


def private_scope(actor="42"):
    return {
        "type": "private",
        "id": str(actor),
        "platform": "aiocqhttp",
        "bot_id": "999",
        "instance_id": "default",
    }


def request_context(capability, *, actor="42", scope=None, epoch=1):
    return main.contracts.make_request_context(
        capability=capability,
        actor_id=str(actor),
        requested_scope=scope or private_scope(actor),
        owner_epoch=epoch,
    )


class CanonicalCommandTests(unittest.TestCase):
    def test_all_canonical_and_alias_spellings_work(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"group_cooldown_seconds": 0, "user_cooldown_seconds": 0},
        )
        messages = (
            "/找图 猫",
            "/搜图 猫",
            "/搜图片 猫",
            "/image 猫",
            "/图片搜索 猫",
        )
        for message in messages:
            with self.subTest(message=message):
                event = FakeEvent(message, private=True)
                item = candidate()
                with patch(
                    "main.image_search.search_image_candidates",
                    return_value=[item],
                ), patch(
                    "main.image_search.download_images",
                    return_value=([], [item["url"]]),
                ):
                    results = run(collect(plugin.search_images(event)))
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].kind, "chain")
                self.assertTrue(event.stopped)

    def test_parse_subcommands(self):
        self.assertEqual(main.parse_image_command("/找图 猫"), ("search", "猫"))
        self.assertEqual(main.parse_image_command("/找图 下一页"), ("next", ""))
        self.assertEqual(
            main.parse_image_command("/找图 收藏 img_123"),
            ("collect", "img_123"),
        )
        self.assertEqual(
            main.parse_image_command("/找图 收藏"), ("collect_missing", "")
        )
        self.assertEqual(main.parse_image_command("/搜图"), ("usage", ""))
        self.assertEqual(main.parse_image_command("/找图 下一页 extra"), ("invalid", ""))
        self.assertEqual(main.parse_image_command("/找图 收藏夹"), ("search", "收藏夹"))

    def test_next_page_command_uses_saved_cursor(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={
                "max_results": 5,
                "paging.page_size": 2,
                "cache.ttl_seconds": 60,
                "cache.max_entries": 10,
                "group_cooldown_seconds": 0,
                "user_cooldown_seconds": 0,
            },
        )
        items = [candidate(index) for index in range(5)]
        with patch(
            "main.image_search.search_image_candidates", return_value=items
        ), patch(
            "main.image_search.download_images",
            side_effect=lambda candidates, **kwargs: ([], [item["url"] for item in candidates]),
        ):
            first = run(collect(plugin.search_images(FakeEvent("/找图 猫", private=True))))
            second = run(collect(plugin.search_images(FakeEvent("/找图 下一页", private=True))))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertIn("下一页", first[0].components[0].text)
        self.assertIn("下一页", second[0].components[0].text)

    def test_next_page_without_search_is_terminal(self):
        plugin = main.ImageSearchPlugin(context=None, config={})
        event = FakeEvent("/找图 下一页", private=True)
        results = run(collect(plugin.search_images(event)))
        self.assertEqual(results[0].text, plugin._cfg_str("no_cursor_reply"))
        self.assertTrue(event.stopped)


class ProviderLifecycleTests(unittest.TestCase):
    def test_readiness_and_owner_epoch(self):
        context = Context()
        plugin = main.ImageSearchPlugin(context=context, config={})
        rc = request_context("image.search")
        before = run(plugin.image_search(rc, {"query": "猫"}))
        self.assertEqual(before["error_code"], "not_ready")
        run(plugin.initialize())
        wrong_epoch = request_context("image.search", epoch=2)
        result = run(plugin.image_search(wrong_epoch, {"query": "猫"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "owner_epoch_mismatch")

    def test_duplicate_provider_is_rejected(self):
        context = Context()
        main.ImageSearchPlugin(context=context, config={})
        with self.assertRaises(main.contracts.ContractError) as ctx:
            main.ImageSearchPlugin(context=context, config={})
        self.assertEqual(ctx.exception.code, "duplicate_provider")

    def test_terminate_removes_provider_and_caches(self):
        context = Context()
        plugin = main.ImageSearchPlugin(
            context=context,
            config={"paging.page_size": 1, "cache.ttl_seconds": 60},
        )
        run(plugin.initialize())
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(0), candidate(1)],
        ):
            run(plugin.image_search(request_context("image.search"), {"query": "猫"}))
        self.assertGreater(len(plugin._search_cache), 0)
        run(plugin.terminate())
        self.assertIsNone(plugin._registry.get_provider("image.search"))
        self.assertEqual(len(plugin._search_cache), 0)
        result = run(plugin.image_search(request_context("image.search"), {"query": "猫"}))
        self.assertEqual(result["status"], "error")
        self.assertIn(result["error_code"], {"not_ready", "dependency_missing"})

    def test_no_arbitrary_http_or_host_override(self):
        context = Context()
        plugin = main.ImageSearchPlugin(context=context, config={})
        run(plugin.initialize())
        for args in (
            {"query": "猫", "url": "http://127.0.0.1/evil.jpg"},
            {"query": "猫", "host": "127.0.0.1"},
            {"query": "猫", "headers": {"Host": "evil"}},
        ):
            with self.subTest(args=args):
                result = run(plugin.image_search(request_context("image.search"), args))
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error_code"], "invalid_args")


class ProviderSearchTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.ImageSearchPlugin(
            context=Context(),
            config={
                "max_results": 5,
                "paging.page_size": 2,
                "cache.ttl_seconds": 60,
                "cache.max_entries": 10,
            },
        )
        run(self.plugin.initialize())
        self.items = [candidate(index) for index in range(5)]

    def test_image_search_returns_data_without_downloading(self):
        with patch(
            "main.image_search.search_image_candidates", return_value=self.items
        ), patch("main.image_search.download_images") as downloader:
            result = run(
                self.plugin.image_search(
                    request_context("image.search"), {"query": "猫", "count": 5}
                )
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result_type"], "image.search_results")
        self.assertEqual(len(result["data"]["items"]), 2)
        self.assertTrue(result["data"]["has_next"])
        self.assertEqual(result["data"]["stale"], False)
        downloader.assert_not_called()

    def test_next_page_and_cursor_binding(self):
        with patch(
            "main.image_search.search_image_candidates", return_value=self.items
        ):
            first = run(
                self.plugin.image_search(
                    request_context("image.search"), {"query": "猫", "count": 5}
                )
            )
        cursor = first["data"]["cursor"]
        next_rc = request_context("image.next_page")
        with patch(
            "main.image_search.search_image_candidates", return_value=self.items
        ):
            second = run(self.plugin.image_next_page(next_rc, {"cursor": cursor}))
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["data"]["page"], 2)
        self.assertEqual(len(second["data"]["items"]), 2)
        wrong_actor = request_context(
            "image.next_page", actor="99", scope=private_scope("99")
        )
        wrong = run(self.plugin.image_next_page(wrong_actor, {"cursor": cursor}))
        self.assertEqual(wrong["error_code"], "cursor_actor_mismatch")
        wrong_scope = request_context(
            "image.next_page", actor="42", scope={"type": "group", "id": "g1"}
        )
        wrong = run(self.plugin.image_next_page(wrong_scope, {"cursor": cursor}))
        self.assertEqual(wrong["error_code"], "cursor_scope_mismatch")
        tampered = run(
            self.plugin.image_next_page(next_rc, {"cursor": cursor + "x"})
        )
        self.assertEqual(tampered["error_code"], "invalid_cursor")

    def test_next_page_cache_stale_flag(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"paging.page_size": 1, "cache.ttl_seconds": 60},
        )
        run(plugin.initialize())
        now = [1000.0]
        plugin._search_cache = main.paging.TTLCache(
            max_entries=10,
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        plugin._cursor = main.paging.OpaqueCursor(
            ttl_seconds=600,
            clock=lambda: now[0],
        )
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(0), candidate(1)],
        ):
            first = run(
                plugin.image_search(
                    request_context("image.search"), {"query": "猫", "count": 2}
                )
            )
        cursor = first["data"]["cursor"]
        now[0] += 61.0
        second = run(
            plugin.image_next_page(
                request_context("image.next_page"), {"cursor": cursor}
            )
        )
        self.assertEqual(second["status"], "ok")
        self.assertTrue(second["data"]["stale"])

    def test_cache_max_entries_evicts_old_search(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={
                "paging.page_size": 1,
                "cache.ttl_seconds": 60,
                "cache.max_entries": 1,
            },
        )
        run(plugin.initialize())
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(0), candidate(1)],
        ):
            first = run(
                plugin.image_search(
                    request_context("image.search"), {"query": "猫", "count": 2}
                )
            )
            run(
                plugin.image_search(
                    request_context("image.search"), {"query": "狗", "count": 2}
                )
            )
        result = run(
            plugin.image_next_page(
                request_context("image.next_page"),
                {"cursor": first["data"]["cursor"]},
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "cache_miss")
        self.assertTrue(result["data"]["stale"])


class CollectionPlanTests(unittest.TestCase):
    def _plugin_with_result(self, context=None):
        plugin = main.ImageSearchPlugin(
            context=context or Context(),
            config={"max_results": 3, "paging.page_size": 2},
        )
        run(plugin.initialize())
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(0), candidate(1), candidate(2)],
        ):
            result = run(
                plugin.image_search(
                    request_context("image.search"), {"query": "猫", "count": 3}
                )
            )
        return plugin, result["data"]["items"][0]["result_id"]

    def test_missing_sticker_provider_fails_closed(self):
        plugin, result_id = self._plugin_with_result()
        with patch("main.image_search.download_images") as downloader:
            result = run(
                plugin.image_collect_plan(
                    request_context("image.collect_plan"),
                    {"result_id": result_id},
                )
            )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "dependency_missing")
        self.assertFalse(result["data"]["routed"])
        self.assertFalse(result["data"]["downloaded"])
        self.assertEqual(result["data"]["plan"]["result_id"], result_id)
        downloader.assert_not_called()

    def test_collect_plan_is_routed_to_sticker_provider(self):
        plugin, result_id = self._plugin_with_result()
        seen: list[dict] = []

        async def sticker_collect(ctx, args):
            seen.append(args)
            return main.contracts.tool_result_ok(
                "call_sticker", "sticker.collect_plan", {"accepted": True}
            )

        plugin._registry.register_provider(
            "qqbot_stickers",
            "sticker.collect_plan",
            {"sticker_collect_plan": sticker_collect},
            owner_epoch=2,
        )
        plugin._registry.mark_ready("qqbot_stickers", ["sticker.collect_plan"])
        with patch("main.image_search.download_images") as downloader:
            result = run(
                plugin.image_collect_plan(
                    request_context("image.collect_plan"),
                    {"result_id": result_id},
                )
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result_type"], "image.collect_plan")
        self.assertTrue(result["data"]["routed"])
        self.assertFalse(result["data"]["downloaded"])
        self.assertEqual(result["data"]["plan"]["result_id"], result_id)
        self.assertEqual(seen[0]["result_id"], result_id)
        downloader.assert_not_called()

    def test_collect_plan_wrong_actor_and_missing_result(self):
        plugin, result_id = self._plugin_with_result()
        wrong = request_context(
            "image.collect_plan", actor="99", scope=private_scope("99")
        )
        result = run(
            plugin.image_collect_plan(wrong, {"result_id": result_id})
        )
        self.assertEqual(result["error_code"], "permission_denied")
        missing = run(
            plugin.image_collect_plan(
                request_context("image.collect_plan"), {"result_id": "img_nope"}
            )
        )
        self.assertEqual(missing["error_code"], "result_not_found")

    def test_collect_command_routes_and_does_not_download(self):
        context = Context()
        plugin = main.ImageSearchPlugin(
            context=context,
            config={"max_results": 3, "paging.page_size": 2},
        )
        run(plugin.initialize())
        seen: list[dict] = []

        async def sticker_collect(ctx, args):
            seen.append(args)
            return main.contracts.tool_result_ok(
                "call_sticker", "sticker.collect_plan", {"accepted": True}
            )

        plugin._registry.register_provider(
            "qqbot_stickers",
            "sticker.collect_plan",
            {"sticker_collect_plan": sticker_collect},
            owner_epoch=2,
        )
        plugin._registry.mark_ready("qqbot_stickers", ["sticker.collect_plan"])
        search_event = FakeEvent("/找图 猫", private=True)
        search_context = plugin._request_context(
            search_event, "image.search", {"query": "猫"}
        )
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(0), candidate(1)],
        ):
            search_result = run(
                plugin.image_search(search_context, {"query": "猫", "count": 2})
            )
        result_id = search_result["data"]["items"][0]["result_id"]
        collect_event = FakeEvent(f"/找图 收藏 {result_id}", private=True)
        with patch("main.image_search.download_images") as downloader:
            results = run(collect(plugin.search_images(collect_event)))
        self.assertEqual(len(results), 1)
        self.assertIn("qqbot_stickers", results[0].text)
        self.assertTrue(collect_event.stopped)
        self.assertEqual(seen[0]["result_id"], result_id)
        downloader.assert_not_called()


class MediaPolicyTests(unittest.TestCase):
    def test_configured_allowed_types_reach_options_and_validation(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"media.allowed_types": ["image/png"]},
        )
        self.assertEqual(plugin._options().allowed_types, ("image/png",))
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        self.assertEqual(
            main.image_search.validate_image_payload(
                png,
                "image/png",
                max_bytes=1024,
                allowed_types=["image/png"],
            ),
            ("image/png", ".png"),
        )
        jpeg = b"\xff\xd8\xff\xe0" + b"x" * 32
        with self.assertRaises(main.image_search.ImageSearchError) as ctx:
            main.image_search.validate_image_payload(
                jpeg,
                "image/jpeg",
                max_bytes=1024,
                allowed_types=["image/png"],
            )
        self.assertEqual(ctx.exception.code, "bad_content_type") if hasattr(ctx.exception, "code") else self.assertEqual(ctx.exception.reason, "bad_content_type")


class CanonicalSchemaTests(unittest.TestCase):
    def test_canonical_groups_exist(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        required = {
            "commands.aliases",
            "search.source_id",
            "search.safe_mode",
            "search.result_limit",
            "search.timeout_seconds",
            "search.max_candidates",
            "media.max_bytes",
            "media.allowed_types",
            "media.download_timeout_seconds",
            "paging.page_size",
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
            "search.source_id",
            "search.safe_mode",
            "search.result_limit",
            "search.timeout_seconds",
            "search.max_candidates",
            "media.max_bytes",
            "media.allowed_types",
            "media.download_timeout_seconds",
            "paging.page_size",
            "cache.ttl_seconds",
            "cache.max_entries",
            "policy.policy_id",
            "policy.owner_epoch",
        ):
            self.assertEqual(schema[key]["default"], main.DEFAULTS[key], key)


if __name__ == "__main__":
    unittest.main()
