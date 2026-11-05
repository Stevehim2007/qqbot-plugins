from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from _support import (
    COMMAND_REGISTRY,
    FakeComponent,
    FakeEvent,
    FakePlain,
    import_main,
    install_astrbot_stub,
)

install_astrbot_stub()
main = import_main()
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REGISTRATIONS_AT_IMPORT = list(COMMAND_REGISTRY)


def run(coro):
    return asyncio.run(coro)


async def collect(async_generator):
    return [item async for item in async_generator]


def candidate(url="https://cdn.example.com/a.png", title="猫"):
    return {
        "url": url,
        "title": title,
        "description": title,
        "referer": main.image_search.BING_IMAGES_REFERER,
        "source": "bing",
        "relevance_score": 1.0,
    }


def downloaded(url="https://cdn.example.com/a.png"):
    suffix = Path(url).suffix or ".png"
    return main.image_search.DownloadedImage(
        f"/tmp/qqbot-image-{abs(hash(url))}{suffix}",
        "image/png",
        suffix,
        url,
    )


class CommandRegistrationTests(unittest.TestCase):
    def test_single_registration_covers_all_four_spellings(self):
        registrations = [
            item for item in REGISTRATIONS_AT_IMPORT if item["name"] == "图片搜索"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(
            set(registrations[0]["aliases"]),
            {"搜图片", "找图", "搜图"},
        )
        self.assertEqual(registrations[0]["priority"], 110)

    def test_extract_keyword_for_every_spelling(self):
        self.assertEqual(main.extract_command_keyword("/图片搜索 猫"), "猫")
        self.assertEqual(main.extract_command_keyword("/搜图片 猫"), "猫")
        self.assertEqual(main.extract_command_keyword("/找图 猫"), "猫")
        self.assertEqual(main.extract_command_keyword("/搜图 猫"), "猫")
        self.assertEqual(main.extract_command_keyword("图片搜索 猫"), "猫")

    def test_extract_empty_for_bare_commands(self):
        for message in ("/图片搜索", "/搜图片", "/找图", "/搜图", "图片搜索"):
            with self.subTest(message=message):
                self.assertEqual(main.extract_command_keyword(message), "")

    def test_extract_falls_back_to_consumed_command_argument(self):
        self.assertEqual(main.extract_command_keyword("猫 壁纸"), "猫 壁纸")

    def test_command_text_detection(self):
        self.assertTrue(main._looks_like_command("/找图 猫"))
        self.assertTrue(main._looks_like_command("图片搜索 猫"))
        self.assertFalse(main._looks_like_command("帮我找几张猫图"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.ImageSearchPlugin(context=None, config={})
        self.event = FakeEvent("/图片搜索 猫")

    def test_usage_when_keyword_missing(self):
        event = FakeEvent("/图片搜索")
        results = run(collect(self.plugin.search_images(event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("usage_reply"))
        self.assertTrue(event.stopped)
        self.assertTrue(event.llm_suppressed)

    def test_invalid_keyword_reply(self):
        event = FakeEvent("/找图 ！！！")
        with patch("main.image_search.search_image_candidates") as search:
            results = run(collect(self.plugin.search_images(event)))
        self.assertEqual(
            results[0].text, self.plugin._cfg_str("invalid_keyword_reply")
        )
        search.assert_not_called()

    def test_disabled_command_does_not_search(self):
        plugin = main.ImageSearchPlugin(context=None, config={"enabled": False})
        event = FakeEvent("/图片搜索 猫")
        with patch("main.image_search.search_image_candidates") as search:
            results = run(collect(plugin.search_images(event)))
        self.assertEqual(results[0].text, plugin._cfg_str("disabled_reply"))
        search.assert_not_called()
        self.assertTrue(event.stopped)

    def test_successful_command_returns_one_chain_reply(self):
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate()],
        ), patch(
            "main.image_search.download_images",
            return_value=([downloaded()], ["https://cdn.example.com/a.png"]),
        ):
            results = run(collect(self.plugin.search_images(self.event)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "chain")
        kinds = [getattr(component, "kind", "plain") for component in results[0].components]
        self.assertEqual(kinds, ["plain", "image_file"])
        self.assertTrue(self.event.stopped)
        self.assertTrue(self.event.llm_suppressed)
        self.assertTrue(self.event.tracked_files)

    def test_timeout_maps_to_timeout_reply(self):
        with patch(
            "main.image_search.search_image_candidates",
            side_effect=main.image_search.ImageSearchError("timeout"),
        ):
            results = run(collect(self.plugin.search_images(self.event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("timeout_reply"))

    def test_network_error_maps_to_error_reply(self):
        with patch(
            "main.image_search.search_image_candidates",
            side_effect=main.image_search.ImageSearchError("network"),
        ):
            results = run(collect(self.plugin.search_images(self.event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("error_reply"))

    def test_empty_candidates_returns_empty_reply(self):
        with patch("main.image_search.search_image_candidates", return_value=[]):
            results = run(collect(self.plugin.search_images(self.event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("empty_reply"))

    def test_command_cooldown_blocks_second_request(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"group_cooldown_seconds": 60, "user_cooldown_seconds": 60},
        )
        first = FakeEvent("/图片搜索 猫")
        second = FakeEvent("/图片搜索 猫")
        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ) as search:
            first_results = run(collect(plugin.search_images(first)))
            second_results = run(collect(plugin.search_images(second)))
        self.assertEqual(first_results[0].text, plugin._cfg_str("empty_reply"))
        self.assertIn("60", second_results[0].text)
        self.assertEqual(search.call_count, 1)

    def test_zero_cooldown_allows_repeat(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"group_cooldown_seconds": 0, "user_cooldown_seconds": 0},
        )
        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ) as search:
            run(collect(plugin.search_images(FakeEvent("/图片搜索 猫"))))
            run(collect(plugin.search_images(FakeEvent("/图片搜索 猫"))))
        self.assertEqual(search.call_count, 2)

    def test_config_is_injected_into_search_options(self):
        config = {
            "max_results": 2,
            "timeout_seconds": 7,
            "max_response_bytes": 123456,
            "max_candidates": 6,
            "max_redirects": 2,
            "relevance_enabled": False,
            "min_relevance_score": 0.3,
            "baidu_fallback_enabled": False,
            "safe_search": "moderate",
            "user_agent": "Injected-UA",
            "download_timeout_seconds": 9,
            "download_max_bytes": 123456,
            "download_attempts": 4,
            "group_cooldown_seconds": 0,
            "user_cooldown_seconds": 0,
        }
        plugin = main.ImageSearchPlugin(context=None, config=config)
        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ) as search:
            run(collect(plugin.search_images(FakeEvent("/图片搜索 猫"))))
        args = search.call_args.args
        self.assertEqual(args[0], "猫")
        options = args[1]
        self.assertEqual(options.result_count, 2)
        self.assertEqual(options.timeout, 7)
        self.assertEqual(options.max_response_bytes, 123456)
        self.assertEqual(options.max_candidates, 6)
        self.assertEqual(options.max_redirects, 2)
        self.assertFalse(options.relevance_enabled)
        self.assertEqual(options.min_relevance_score, 0.3)
        self.assertFalse(options.baidu_fallback_enabled)
        self.assertEqual(options.safe_search, "moderate")
        self.assertEqual(options.user_agent, "Injected-UA")
        self.assertEqual(options.download_timeout, 9)
        self.assertEqual(options.download_max_bytes, 123456)
        self.assertEqual(options.download_attempts, 4)

    def test_config_object_to_dict_is_supported(self):
        class ConfigObject:
            def to_dict(self):
                return {"max_results": 1, "max_keyword_chars": 20}

        plugin = main.ImageSearchPlugin(context=None, config=ConfigObject())
        self.assertEqual(plugin._result_limit(), 1)
        self.assertEqual(plugin._cfg_int("max_keyword_chars", minimum=1, maximum=300), 20)

    def test_keyword_is_length_limited(self):
        plugin = main.ImageSearchPlugin(
            context=None, config={"max_keyword_chars": 5}
        )
        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ) as search:
            run(collect(plugin.search_images(FakeEvent("/图片搜索 abcdefghij"))))
        self.assertEqual(search.call_args.args[0], "abcde")

    def test_empty_download_result_uses_remote_fallback(self):
        remote_url = "https://cdn.example.com/remote.png"
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(remote_url)],
        ), patch(
            "main.image_search.download_images",
            return_value=([], [remote_url]),
        ):
            results = run(collect(self.plugin.search_images(self.event)))
        kinds = [getattr(component, "kind", "plain") for component in results[0].components]
        self.assertEqual(kinds, ["plain", "image_url"])
        self.assertEqual(results[0].components[1].value, remote_url)


class ToolApiTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.ImageSearchPlugin(
            context=None,
            config={"max_results": 2, "group_cooldown_seconds": 0, "user_cooldown_seconds": 0},
        )
        self.event = FakeEvent("tool")

    def test_tool_success_returns_chain(self):
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate()],
        ), patch(
            "main.image_search.download_images",
            return_value=([downloaded()], ["https://cdn.example.com/a.png"]),
        ):
            result = run(
                self.plugin.tool_search_images(self.event, keyword="猫", count=1)
            )
        self.assertEqual(result.kind, "chain")
        self.assertLessEqual(len(result.components) - 1, 2)

    def test_tool_count_is_capped_by_max_results(self):
        items = [
            downloaded(f"https://cdn.example.com/{index}.png")
            for index in range(5)
        ]
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(f"https://cdn.example.com/{index}.png") for index in range(5)],
        ), patch(
            "main.image_search.download_images",
            return_value=(items, [item.source_url for item in items]),
        ) as downloader:
            result = run(
                self.plugin.tool_search_images(self.event, keyword="猫", count=99)
            )
        self.assertEqual(downloader.call_args.kwargs["result_limit"], 2)
        images = [
            component
            for component in result.components
            if getattr(component, "kind", "") in {"image_file", "image_url"}
        ]
        self.assertEqual(len(images), 2)

    def test_tool_count_zero_is_clamped_to_one(self):
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate()],
        ), patch(
            "main.image_search.download_images",
            return_value=([downloaded()], []),
        ) as downloader:
            run(self.plugin.tool_search_images(self.event, keyword="猫", count=0))
        self.assertEqual(downloader.call_args.kwargs["result_limit"], 1)

    def test_tool_invalid_keyword(self):
        with patch("main.image_search.search_image_candidates") as search:
            result = run(
                self.plugin.tool_search_images(self.event, keyword="！！！")
            )
        self.assertEqual(result.text, self.plugin._cfg_str("invalid_keyword_reply"))
        search.assert_not_called()

    def test_tool_timeout_and_unexpected_error_never_raise(self):
        with patch(
            "main.image_search.search_image_candidates",
            side_effect=main.image_search.ImageSearchError("timeout"),
        ):
            timeout_result = run(
                self.plugin.tool_search_images(self.event, keyword="猫")
            )
        self.assertEqual(timeout_result.text, self.plugin._cfg_str("timeout_reply"))
        with patch(
            "main.image_search.search_image_candidates",
            side_effect=RuntimeError("secret boom"),
        ):
            error_result = run(
                self.plugin.tool_search_images(self.event, keyword="猫")
            )
        self.assertEqual(error_result.text, self.plugin._cfg_str("error_reply"))
        self.assertNotIn("secret", error_result.text)

    def test_tool_disabled(self):
        plugin = main.ImageSearchPlugin(context=None, config={"enabled": False})
        with patch("main.image_search.search_image_candidates") as search:
            result = run(plugin.tool_search_images(self.event, keyword="猫"))
        self.assertEqual(result.text, plugin._cfg_str("disabled_reply"))
        search.assert_not_called()

    def test_tool_does_not_consume_command_cooldown(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"group_cooldown_seconds": 600, "user_cooldown_seconds": 600},
        )
        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ) as search:
            run(plugin.tool_search_images(self.event, keyword="猫"))
            run(plugin.tool_search_images(self.event, keyword="猫"))
        self.assertEqual(search.call_count, 2)

    def test_tool_returns_none_when_event_cannot_make_results(self):
        class BadEvent:
            def plain_result(self, text):
                raise RuntimeError("no result support")

        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ):
            result = run(
                self.plugin.tool_search_images(BadEvent(), keyword="猫")
            )
        self.assertIsNone(result)

    def test_remote_url_fallback_is_bounded(self):
        urls = [f"https://cdn.example.com/{index}.png" for index in range(5)]
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(url) for url in urls],
        ), patch(
            "main.image_search.download_images",
            return_value=([], urls),
        ):
            result = run(
                self.plugin.tool_search_images(self.event, keyword="猫", count=99)
            )
        images = [
            component
            for component in result.components
            if getattr(component, "kind", "") == "image_url"
        ]
        self.assertEqual(len(images), 2)

    def test_local_success_does_not_duplicate_remote_url(self):
        item = downloaded()
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate(item.source_url)],
        ), patch(
            "main.image_search.download_images",
            return_value=([item], [item.source_url]),
        ):
            result = run(
                self.plugin.tool_search_images(self.event, keyword="猫", count=2)
            )
        images = [
            component
            for component in result.components
            if getattr(component, "kind", "") in {"image_file", "image_url"}
        ]
        self.assertEqual(len(images), 1)


class NaturalLanguageTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.ImageSearchPlugin(context=None, config={})
        self.private_event = FakeEvent("帮我找两张猫的图片", private=True)
        self.group_event = FakeEvent("帮我找两张猫的图片", private=False, at_bot=False)
        self.at_event = FakeEvent("帮我找两张猫的图片", private=False, at_bot=True)

    def _handled(self, event):
        with patch(
            "main.image_search.search_image_candidates",
            return_value=[candidate()],
        ) as search, patch(
            "main.image_search.download_images",
            return_value=([downloaded()], ["https://cdn.example.com/a.png"]),
        ):
            results = run(collect(self.plugin.on_natural_language(event)))
        return results, search

    def test_private_natural_language_triggers_search(self):
        results, search = self._handled(self.private_event)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "chain")
        self.assertTrue(self.private_event.stopped)
        self.assertTrue(self.private_event.llm_suppressed)
        self.assertEqual(search.call_args.args[0], "猫")

    def test_group_non_at_natural_language_is_ignored(self):
        results, search = self._handled(self.group_event)
        self.assertEqual(results, [])
        search.assert_not_called()
        self.assertFalse(self.group_event.stopped)

    def test_group_at_bot_natural_language_triggers_search(self):
        results, search = self._handled(self.at_event)
        self.assertEqual(len(results), 1)
        self.assertTrue(self.at_event.stopped)
        search.assert_called_once()

    def test_at_component_with_bot_self_id_triggers(self):
        class At:
            def __init__(self, qq):
                self.qq = qq

        event = FakeEvent("帮我找一张猫图", private=False, at_bot=False)
        event.messages = [At("999")]
        results, search = self._handled(event)
        self.assertEqual(len(results), 1)
        search.assert_called_once()

    def test_at_component_with_other_id_does_not_trigger(self):
        class At:
            def __init__(self, qq):
                self.qq = qq

        event = FakeEvent("帮我找一张猫图", private=False, at_bot=False)
        event.messages = [At("123")]
        results, search = self._handled(event)
        self.assertEqual(results, [])
        search.assert_not_called()

    def test_command_text_is_not_handled_by_natural_language(self):
        event = FakeEvent("/图片搜索 猫", private=True)
        results, search = self._handled(event)
        self.assertEqual(results, [])
        search.assert_not_called()
        self.assertFalse(event.stopped)

    def test_plain_chat_is_not_handled(self):
        event = FakeEvent("今天天气不错", private=True)
        results, search = self._handled(event)
        self.assertEqual(results, [])
        search.assert_not_called()
        self.assertFalse(event.stopped)

    def test_natural_language_disabled(self):
        plugin = main.ImageSearchPlugin(
            context=None, config={"natural_language_enabled": False}
        )
        with patch("main.image_search.search_image_candidates") as search:
            results = run(collect(plugin.on_natural_language(self.private_event)))
        self.assertEqual(results, [])
        search.assert_not_called()

    def test_natural_language_cooldown(self):
        plugin = main.ImageSearchPlugin(
            context=None,
            config={"group_cooldown_seconds": 60, "user_cooldown_seconds": 60},
        )
        with patch(
            "main.image_search.search_image_candidates", return_value=[]
        ) as search:
            first = run(collect(plugin.on_natural_language(FakeEvent("帮我找猫图", private=True))))
            second = run(collect(plugin.on_natural_language(FakeEvent("帮我找猫图", private=True))))
        self.assertEqual(first[0].text, plugin._cfg_str("empty_reply"))
        self.assertIn("60", second[0].text)
        self.assertEqual(search.call_count, 1)

    def test_natural_language_timeout_reply(self):
        with patch(
            "main.image_search.search_image_candidates",
            side_effect=main.image_search.ImageSearchError("timeout"),
        ):
            results = run(collect(self.plugin.on_natural_language(self.private_event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("timeout_reply"))
        self.assertTrue(self.private_event.stopped)

    def test_natural_language_search_error_reply(self):
        with patch(
            "main.image_search.search_image_candidates",
            side_effect=RuntimeError("boom"),
        ):
            results = run(collect(self.plugin.on_natural_language(self.private_event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("error_reply"))
        self.assertTrue(self.private_event.stopped)


class SchemaAndMetadataTests(unittest.TestCase):
    def test_schema_required_keys(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        required = {
            "max_results",
            "max_keyword_chars",
            "timeout_seconds",
            "max_response_bytes",
            "max_candidates",
            "max_redirects",
            "download_timeout_seconds",
            "download_max_bytes",
            "download_attempts",
            "group_cooldown_seconds",
            "user_cooldown_seconds",
            "relevance_enabled",
            "min_relevance_score",
            "baidu_fallback_enabled",
            "safe_search",
            "user_agent",
            "usage_reply",
            "empty_reply",
            "error_reply",
            "timeout_reply",
            "cooldown_reply",
        }
        self.assertTrue(required.issubset(schema))
        for key in required:
            self.assertIn("default", schema[key], key)
            self.assertIn("type", schema[key], key)

    def test_schema_defaults_align_with_plugin_defaults(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        for key in (
            "max_results",
            "max_keyword_chars",
            "timeout_seconds",
            "max_response_bytes",
            "max_candidates",
            "max_redirects",
            "download_timeout_seconds",
            "download_max_bytes",
            "download_attempts",
            "group_cooldown_seconds",
            "user_cooldown_seconds",
            "relevance_enabled",
            "min_relevance_score",
            "baidu_fallback_enabled",
            "safe_search",
            "user_agent",
            "usage_reply",
            "empty_reply",
            "error_reply",
            "timeout_reply",
            "cooldown_reply",
        ):
            self.assertEqual(schema[key]["default"], main.DEFAULTS[key], key)

    def test_max_results_slider_is_bounded(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        slider = schema["max_results"]["slider"]
        self.assertEqual(slider["min"], 1)
        self.assertEqual(slider["max"], 5)

    def test_version_files_are_2_0_0(self):
        self.assertEqual((PLUGIN_ROOT / "VERSION").read_text("utf-8").strip(), "2.0.0")
        metadata = (PLUGIN_ROOT / "metadata.yaml").read_text("utf-8")
        self.assertRegex(metadata, r"(?m)^version:\s*2\.0\.0\s*$")
        self.assertIn(">=4.29,<5", metadata)

    def test_readme_documents_commands_and_api(self):
        readme = (PLUGIN_ROOT / "README.md").read_text("utf-8")
        for text in (
            "/图片搜索",
            "/搜图片",
            "/找图",
            "/搜图",
            "tool_search_images",
            "2.0.0",
        ):
            self.assertIn(text, readme)

    def test_package_has_no_legacy_plugin_imports(self):
        for path in PLUGIN_ROOT.glob("*.py"):
            text = path.read_text("utf-8")
            self.assertNotIn("qqbot_enhancer", text, path.name)
            self.assertNotIn("qqbot_core_full", text, path.name)


if __name__ == "__main__":
    unittest.main()
