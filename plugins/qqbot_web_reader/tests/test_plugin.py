from __future__ import annotations

import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import (
    COMMAND_REGISTRY,
    FakeEvent,
    install_astrbot_stub,
    import_main,
)

install_astrbot_stub()
main = import_main()
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REGISTRATIONS_AT_IMPORT = list(COMMAND_REGISTRY)


def run(coro):
    return asyncio.run(coro)


async def collect(async_generator):
    return [item async for item in async_generator]


def page_result(text="Body", title="Title", *, truncated=False, url="https://example.com/"):
    return main.web_fetch.PageResult(
        url,
        title,
        text,
        truncated,
        "text/html",
    )


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.WebReaderPlugin(context=None, config={})
        COMMAND_REGISTRY.clear()

    def test_command_registration_includes_canonical_name_and_aliases(self):
        registrations = [
            item for item in REGISTRATIONS_AT_IMPORT if item["name"] == "网页读取"
        ]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(
            set(registrations[0]["aliases"]),
            {"网页", "读网页", "web"},
        )
        for name in ("网页读取", "网页", "读网页", "web"):
            self.assertIn(name, main.COMMAND_NAMES)

    def test_extract_url_supports_both_command_names(self):
        self.assertEqual(
            main.extract_command_url("/网页 https://example.com/a。"),
            "https://example.com/a",
        )
        self.assertEqual(
            main.extract_command_url("/读网页 https://example.com/b"),
            "https://example.com/b",
        )
        self.assertEqual(
            main.extract_command_url("网页 https://example.com/c extra"),
            "https://example.com/c",
        )

    def test_empty_command_returns_usage(self):
        event = FakeEvent("/网页")
        results = run(collect(self.plugin.read_web(event)))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, self.plugin._cfg_str("usage_reply"))
        self.assertTrue(event.stopped)
        self.assertTrue(event.llm_suppressed)

    def test_success_returns_formatted_plain_result(self):
        event = FakeEvent("/网页 https://example.com/")
        with patch(
            "main.web_fetch.fetch_page_text",
            return_value=page_result("Hello body"),
        ):
            results = run(collect(self.plugin.read_web(event)))
        self.assertEqual(len(results), 1)
        self.assertIn("网页：Title", results[0].text)
        self.assertIn("Hello body", results[0].text)
        self.assertTrue(event.stopped)

    def test_disabled_plugin_returns_disabled_reply(self):
        plugin = main.WebReaderPlugin(context=None, config={"enabled": False})
        event = FakeEvent("/网页 https://example.com/")
        with patch("main.web_fetch.fetch_page_text") as fetcher:
            results = run(collect(plugin.read_web(event)))
        self.assertEqual(results[0].text, plugin._cfg_str("disabled_reply"))
        fetcher.assert_not_called()

    def test_fetch_failure_returns_safe_reply_without_exception_text(self):
        event = FakeEvent("/网页 https://example.com/secret")
        with patch(
            "main.web_fetch.fetch_page_text",
            side_effect=main.web_fetch.WebFetchError("blocked_ip"),
        ):
            results = run(collect(self.plugin.read_web(event)))
        self.assertEqual(results[0].text, self.plugin._cfg_str("blocked_reply"))
        self.assertNotIn("secret", results[0].text)

    def test_config_is_injected_into_engine_call(self):
        config = {
            "max_chars": 850,
            "max_bytes": 123456,
            "timeout_seconds": 7,
            "max_redirects": 1,
            "user_agent": "Injected-UA",
            "allow_http": True,
        }
        plugin = main.WebReaderPlugin(context=None, config=config)
        event = FakeEvent("/网页 http://example.com/")
        with patch(
            "main.web_fetch.fetch_page_text",
            return_value=page_result("ok"),
        ) as fetcher:
            run(collect(plugin.read_web(event)))
        kwargs = fetcher.call_args.kwargs
        self.assertEqual(kwargs["max_chars"], 850)
        self.assertEqual(kwargs["max_bytes"], 123456)
        self.assertEqual(kwargs["timeout"], 7.0)
        self.assertEqual(kwargs["max_redirects"], 1)
        self.assertEqual(kwargs["user_agent"], "Injected-UA")
        self.assertTrue(kwargs["allow_http"])

    def test_config_object_attribute_is_supported(self):
        class ConfigObject:
            def to_dict(self):
                return {"max_chars": 777, "enabled": True}

        plugin = main.WebReaderPlugin(context=None, config=ConfigObject())
        self.assertEqual(plugin._cfg_int("max_chars", minimum=1, maximum=20000), 777)


class ToolApiTests(unittest.TestCase):
    def setUp(self):
        self.plugin = main.WebReaderPlugin(context=None, config={})
        self.event = FakeEvent("tool")

    def test_tool_returns_result_on_success(self):
        with patch(
            "main.web_fetch.fetch_page_text",
            return_value=page_result("tool body"),
        ):
            result = run(
                self.plugin.tool_fetch_webpage(
                    self.event, url="https://example.com/"
                )
            )
        self.assertIsNotNone(result)
        self.assertIn("tool body", result.text)

    def test_tool_respects_max_chars_argument(self):
        with patch(
            "main.web_fetch.fetch_page_text",
            return_value=page_result("ok"),
        ) as fetcher:
            run(
                self.plugin.tool_fetch_webpage(
                    self.event,
                    url="https://example.com/",
                    max_chars=1234,
                )
            )
        self.assertEqual(fetcher.call_args.kwargs["max_chars"], 1234)

    def test_tool_clamps_max_chars_to_hard_limit(self):
        with patch(
            "main.web_fetch.fetch_page_text",
            return_value=page_result("ok"),
        ) as fetcher:
            run(
                self.plugin.tool_fetch_webpage(
                    self.event,
                    url="https://example.com/",
                    max_chars=999999,
                )
            )
        self.assertEqual(
            fetcher.call_args.kwargs["max_chars"],
            main.MAX_TOOL_CHARS_HARD_LIMIT,
        )

    def test_tool_zero_max_chars_uses_config(self):
        plugin = main.WebReaderPlugin(context=None, config={"max_chars": 900})
        with patch(
            "main.web_fetch.fetch_page_text",
            return_value=page_result("ok"),
        ) as fetcher:
            run(
                plugin.tool_fetch_webpage(
                    self.event, url="https://example.com/", max_chars=0
                )
            )
        self.assertEqual(fetcher.call_args.kwargs["max_chars"], 900)

    def test_tool_maps_safe_error(self):
        with patch(
            "main.web_fetch.fetch_page_text",
            side_effect=main.web_fetch.WebFetchError("too_large"),
        ):
            result = run(
                self.plugin.tool_fetch_webpage(
                    self.event, url="https://example.com/"
                )
            )
        self.assertEqual(result.text, self.plugin._cfg_str("too_large_reply"))

    def test_tool_never_raises_on_unexpected_error(self):
        with patch(
            "main.web_fetch.fetch_page_text",
            side_effect=RuntimeError("boom with secret"),
        ):
            result = run(
                self.plugin.tool_fetch_webpage(
                    self.event, url="https://example.com/"
                )
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.text, self.plugin._cfg_str("error_reply"))
        self.assertNotIn("secret", result.text)

    def test_tool_returns_none_if_event_cannot_build_result(self):
        class BadEvent:
            def plain_result(self, text):
                raise RuntimeError("no result support")

        with patch(
            "main.web_fetch.fetch_page_text",
            side_effect=main.web_fetch.WebFetchError("network"),
        ):
            result = run(
                self.plugin.tool_fetch_webpage(
                    BadEvent(), url="https://example.com/"
                )
            )
        self.assertIsNone(result)

    def test_tool_empty_url_returns_invalid_reply(self):
        with patch(
            "main.web_fetch.fetch_page_text",
            side_effect=main.web_fetch.WebFetchError("invalid_url"),
        ):
            result = run(
                self.plugin.tool_fetch_webpage(self.event, url="")
            )
        self.assertEqual(result.text, self.plugin._cfg_str("invalid_url_reply"))


class SchemaAndMetadataTests(unittest.TestCase):
    def test_schema_contains_required_settings(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        required = {
            "max_chars",
            "max_bytes",
            "timeout_seconds",
            "max_redirects",
            "user_agent",
            "allow_http",
            "usage_reply",
            "invalid_url_reply",
            "blocked_reply",
            "timeout_reply",
            "error_reply",
        }
        self.assertTrue(required.issubset(schema))
        for key in required:
            self.assertIn("default", schema[key], key)
            self.assertIn("type", schema[key], key)

    def test_schema_defaults_align_with_plugin_defaults(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        for key in (
            "max_chars",
            "max_bytes",
            "timeout_seconds",
            "max_redirects",
            "user_agent",
            "allow_http",
            "usage_reply",
        ):
            self.assertEqual(schema[key]["default"], main.DEFAULTS[key], key)

    def test_version_files_are_2_0_0(self):
        self.assertEqual((PLUGIN_ROOT / "VERSION").read_text("utf-8").strip(), "2.0.0")
        metadata = (PLUGIN_ROOT / "metadata.yaml").read_text("utf-8")
        self.assertRegex(metadata, r"(?m)^version:\s*2\.0\.0\s*$")
        self.assertIn("astrbot_version:", metadata)
        self.assertIn(">=4.29,<5", metadata)

    def test_readme_documents_command_and_tool_api(self):
        readme = (PLUGIN_ROOT / "README.md").read_text("utf-8")
        self.assertIn("/网页", readme)
        self.assertIn("/读网页", readme)
        self.assertIn("tool_fetch_webpage", readme)
        self.assertIn("2.0.0", readme)

    def test_package_has_no_legacy_plugin_imports(self):
        for path in PLUGIN_ROOT.glob("*.py"):
            text = path.read_text("utf-8")
            self.assertNotIn("qqbot_enhancer", text, path.name)
            self.assertNotIn("qqbot_core_full", text, path.name)


if __name__ == "__main__":
    unittest.main()
