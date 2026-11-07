"""Manifest-driven tool registry tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from _support import make_plugin, safe_tools
from qqbot_safe_tools import tool_registry


class ToolRegistryParsingTests(unittest.TestCase):
    def test_manifest_is_normalised(self):
        raw = {
            "schema_version": 1,
            "plugin_id": "demo_plugin",
            "tools": [
                {
                    "name": "demo_lookup",
                    "method": "tool_lookup",
                    "description": "查询演示数据。",
                    "parameters": {
                        "query": {"type": "string", "required": True, "max_chars": 40},
                        "limit": {"type": "integer", "min": 1, "max": 5, "default": 3},
                    },
                    "allow_key": "allow_demo_lookup",
                    "cooldown_key": "demo_lookup_cooldown_seconds",
                    "cooldown_default": 10,
                    "operation": "demo.lookup",
                    "marker_example": '[[tool:demo_lookup query="x" limit=3]]',
                }
            ],
        }
        descriptors = tool_registry.parse_tools_document(raw)
        self.assertEqual(len(descriptors), 1)
        descriptor = descriptors[0]
        self.assertEqual(descriptor.name, "demo_lookup")
        self.assertEqual(descriptor.plugin_id, "demo_plugin")
        self.assertEqual(descriptor.parameters["query"].max_chars, 40)
        self.assertEqual(descriptor.parameters["limit"].max, 5)
        self.assertEqual(descriptor.operation, "demo.lookup")

    def test_bad_manifest_is_rejected(self):
        with self.assertRaises(tool_registry.ToolRegistryError):
            tool_registry.parse_tools_document({"schema_version": 2, "tools": []})
        with self.assertRaises(tool_registry.ToolRegistryError):
            tool_registry.parse_tools_document(
                {
                    "schema_version": 1,
                    "plugin_id": "demo",
                    "tools": [
                        {
                            "name": "Bad Name",
                            "method": "tool_x",
                            "allow_key": "a",
                            "cooldown_key": "b",
                        }
                    ],
                }
            )

    def test_discovery_reads_tools_json_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for plugin, tool in (("one", "hello"), ("two", "hello")):
                directory = root / plugin
                directory.mkdir()
                (directory / "main.py").write_text("", encoding="utf-8")
                (directory / "tools.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "plugin_id": plugin,
                            "tools": [
                                {
                                    "name": tool,
                                    "method": "tool_hello",
                                    "parameters": {},
                                    "allow_key": "allow_hello",
                                    "cooldown_key": "hello_cooldown_seconds",
                                    "cooldown_default": 0,
                                    "operation": "demo.hello",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            stars = [
                SimpleNamespace(module=SimpleNamespace(__file__=str(root / plugin / "main.py")))
                for plugin in ("one", "two")
            ]
            descriptors, errors = tool_registry.discover_from_stars(stars)
            self.assertEqual([item.name for item in descriptors], ["hello"])
            self.assertTrue(any("duplicate" in error for error in errors))

    def test_shipped_provider_manifests_cover_all_seven_tools(self):
        parent = Path(__file__).resolve().parents[2]
        descriptors = []
        for plugin in ("qqbot_web_reader", "qqbot_image_search", "qqbot_food", "qqbot_stickers"):
            descriptors.extend(tool_registry.load_tools_file(parent / plugin / "tools.json"))
        self.assertEqual(
            {item.name for item in descriptors},
            {
                "fetch_webpage",
                "search_images",
                "search_food_restaurants",
                "submit_food_review",
                "send_sticker",
                "collect_sticker",
                "list_sticker_categories",
            },
        )

    def test_builtin_fallback_covers_all_seven_tools(self):
        registry = tool_registry.ToolRegistry(tool_registry.builtin_descriptors())
        self.assertEqual(
            set(registry.names()),
            {
                "fetch_webpage",
                "search_images",
                "search_food_restaurants",
                "submit_food_review",
                "send_sticker",
                "collect_sticker",
                "list_sticker_categories",
            },
        )


class PluginRegistryTests(unittest.TestCase):
    def test_plugin_advertises_and_resolves_manifest_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo_plugin"
            root.mkdir()
            (root / "main.py").write_text("", encoding="utf-8")
            (root / "tools.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "plugin_id": "demo_plugin",
                        "tools": [
                            {
                                "name": "demo_lookup",
                                "method": "tool_lookup",
                                "description": "查询演示数据。",
                                "parameters": {"query": {"type": "string", "required": True}},
                                "allow_key": "allow_demo_lookup",
                                "cooldown_key": "demo_lookup_cooldown_seconds",
                                "cooldown_default": 0,
                                "operation": "demo.lookup",
                                "marker_example": '[[tool:demo_lookup query="x"]]',
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            star = SimpleNamespace(tool_lookup=lambda event, **kwargs: None)
            metadata = SimpleNamespace(
                activated=True,
                star_cls=star,
                module=SimpleNamespace(__file__=str(root / "main.py")),
            )
            plugin = make_plugin(config={**__import__("_support").base_config(), "allow_demo_lookup": True})
            plugin.context.get_all_stars = lambda: [metadata]
            plugin.context.metadata["demo_plugin"] = metadata
            plugin._tool_registry_cache = None
            self.assertIn("demo_lookup", plugin._enabled_tool_names())
            self.assertIn("demo_lookup", plugin.tool_instruction())
            self.assertIs(plugin._stable_method("demo_lookup"), star.tool_lookup)

    def test_catalog_keeps_builtin_tools_when_bridge_provider_is_ready(self):
        from _support import register

        plugin = make_plugin()
        register(plugin, "qqbot_web_reader", tool_fetch_webpage=lambda *a, **k: None)
        register(plugin, "qqbot_image_search", tool_search_images=lambda *a, **k: None)
        register(
            plugin,
            "qqbot_food",
            tool_search_restaurants=lambda *a, **k: None,
            tool_submit_food_review=lambda *a, **k: None,
        )
        register(
            plugin,
            "qqbot_stickers",
            tool_send_sticker=lambda *a, **k: None,
            tool_collect_sticker=lambda *a, **k: None,
            tool_list_categories=lambda *a, **k: None,
        )
        plugin.registry.register_provider(
            "qqbot_safe_tools",
            {"tool.catalog": lambda: None},
            ready=True,
            exclusive=True,
        )
        self.assertTrue(plugin.registry.has_providers())
        self.assertEqual(len(plugin._advertised_tool_names()), 7)
        self.assertTrue(plugin.tool_instruction().strip())

    def test_marker_mode_ignores_bridge_only_provider_registry(self):
        plugin = make_plugin()
        plugin.registry.register_provider(
            "qqbot_safe_tools",
            {"tool.catalog": lambda: None},
            ready=True,
            exclusive=True,
        )
        self.assertFalse(plugin._abi_mode())


class DynamicIntentTests(unittest.TestCase):
    def test_spec_accepts_new_tool_and_rejects_unknown_args(self):
        spec = {
            "demo_lookup": {
                "parameters": {
                    "query": {"type": "string", "required": True, "max_chars": 20},
                    "limit": {"type": "integer", "min": 1, "max": 5},
                },
                "constraints": {},
            }
        }
        parsed = safe_tools.parse_tool_markers(
            '[[tool:demo_lookup query="abc" limit=3]]', tool_specs=spec
        )
        self.assertEqual(len(parsed.intents), 1)
        self.assertTrue(parsed.intents[0].valid)
        bad = safe_tools.parse_tool_markers(
            '[[tool:demo_lookup query="abc" extra="x"]]', tool_specs=spec
        )
        self.assertFalse(bad.intents[0].valid)
        missing = safe_tools.parse_tool_markers(
            "[[tool:demo_lookup limit=3]]", tool_specs=spec
        )
        self.assertFalse(missing.intents[0].valid)

    def test_dynamic_catalog_uses_manifest_description(self):
        specs = {
            "demo_lookup": {
                "description": "查询演示数据。",
                "marker_example": '[[tool:demo_lookup query="x"]]',
            }
        }
        text = safe_tools.tool_instruction(["demo_lookup"], tool_specs=specs)
        self.assertIn("demo_lookup", text)
        self.assertIn("查询演示数据", text)


if __name__ == "__main__":
    unittest.main()
