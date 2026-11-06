from __future__ import annotations

import ast
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from support import DEFAULTS, FoodPlugin, PLUGIN_DIR, PluginConfig, make_plugin


class ImportContractTests(unittest.TestCase):
    def test_plugin_import_does_not_load_legacy_modules(self):
        forbidden = {
            name
            for name in sys.modules
            if name == "qqbot_enhancer"
            or name.startswith("qqbot_enhancer.")
            or name == "qqbot_core_full"
            or name.startswith("qqbot_core_full.")
        }
        self.assertEqual(forbidden, set())

    def test_source_files_do_not_import_legacy_or_call_llm(self):
        forbidden = ("qqbot_enhancer", "qqbot_core_full", "llm_generate", "get_using_provider")
        for path in PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{path.name} contains {token}")

    def test_production_imports_are_stdlib_or_astrbot_only(self):
        allowed_third_party = {"astrbot"}
        stdlib = set(getattr(sys, "stdlib_module_names", set()))
        for path in PLUGIN_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [(node.module or "").split(".")[0]]
                else:
                    continue
                for module in modules:
                    if not module:
                        continue
                    self.assertTrue(
                        module in stdlib or module in allowed_third_party,
                        f"{path.name} imports non-stdlib module {module}",
                    )

    def test_constructor_accepts_injected_config(self):
        signature = inspect.signature(FoodPlugin.__init__)
        self.assertIn("context", signature.parameters)
        self.assertIn("config", signature.parameters)
        self.assertIsNone(signature.parameters["config"].default)

    def test_stable_tool_methods_exist_with_expected_arguments(self):
        for name in ("tool_search_restaurants", "tool_submit_food_review"):
            method = getattr(FoodPlugin, name)
            signature = inspect.signature(method)
            params = list(signature.parameters)
            self.assertEqual(
                params[:4], ["self", "event", "args", "clean_text"], name
            )

    def test_command_registration_has_food_and_aliases(self):
        args, kwargs = FoodPlugin.food_command._qqbot_food_command
        self.assertEqual(args[0], "food")
        self.assertEqual(set(kwargs.get("alias", set())), {"美食", "吃什么", "饭店"})
        self.assertEqual(kwargs.get("priority"), 110)

    def test_confirmation_handler_registration(self):
        args, kwargs = FoodPlugin.on_food_message._qqbot_food_event
        self.assertEqual(kwargs.get("priority"), 120)


class MetadataAndSchemaTests(unittest.TestCase):
    def test_version_files_are_3_0_0(self):
        self.assertEqual((PLUGIN_DIR / "VERSION").read_text().strip(), "3.0.0")
        metadata = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 3.0.0", metadata)
        self.assertIn("astrbot_version:", metadata)
        self.assertIn(">=4.29", metadata)

    def test_config_schema_is_pure_json_and_matches_defaults(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIsInstance(schema, dict)
        self.assertEqual(set(schema), set(DEFAULTS))
        for key, default in DEFAULTS.items():
            item = schema[key]
            self.assertIn("description", item)
            self.assertIn("default", item)
            self.assertIs(type(item["default"]), type(default), key)
            expected_type = (
                "bool"
                if isinstance(default, bool)
                else "int"
                if isinstance(default, int)
                else "float"
                if isinstance(default, float)
                else "text"
            )
            self.assertEqual(item["type"], expected_type, key)

    def test_schema_includes_required_legacy_and_new_knobs(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        required = {
            "food_enabled",
            "food_timeout_seconds",
            "food_max_response_bytes",
            "food_max_results",
            "food_cooldown_seconds",
            "food_keyword_max_chars",
            "food_location_max_chars",
            "food_group_locations",
            "food_group_location_infer_from_name",
            "food_submit_enabled",
            "food_submit_timeout_seconds",
            "food_submit_max_response_bytes",
            "food_submit_cooldown_seconds",
            "food_submit_draft_ttl_seconds",
            "food_submit_name_max_chars",
            "food_submit_location_max_chars",
            "food_submit_comment_max_chars",
            "food_submit_tag_max_chars",
            "food_submit_tags_max_count",
            "food_submit_username_max_chars",
            "food_submit_username_fallback",
            "food_search_enabled",
            "food_search_max_tags",
            "food_search_default_limit",
            "food_search_name_max_chars",
            "food_search_tag_max_chars",
            "food_search_location_max_chars",
        }
        self.assertTrue(required.issubset(schema))

    def test_schema_does_not_contain_secret_fields(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in schema:
            lowered = key.casefold()
            self.assertNotIn("password", lowered)
            self.assertNotIn("api_key", lowered)
            self.assertNotIn("token", lowered)
            self.assertNotIn("secret", lowered)

    def test_readme_documents_commands_config_data_and_fixed_api(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        for token in (
            "/food",
            "/吃什么",
            "/饭店",
            "tool_search_restaurants",
            "tool_submit_food_review",
            "_conf_schema.json",
            "data/plugin_data/qqbot_food",
            "https://food.dlsz.top",
            "POST",
            "password",
        ):
            self.assertIn(token, readme)


class ConfigReaderTests(unittest.TestCase):
    def test_injected_config_overrides_defaults_and_coerces_types(self):
        config = PluginConfig(
            {
                "food_enabled": "false",
                "food_max_results": "3",
                "food_timeout_seconds": 7,
            }
        )
        self.assertFalse(config.bool("food_enabled"))
        self.assertEqual(config.int("food_max_results"), 3)
        self.assertEqual(config.int("food_timeout_seconds"), 7)
        self.assertEqual(config.string("food_usage_reply"), DEFAULTS["food_usage_reply"])

    def test_plugin_uses_injected_config_not_context_global_config(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plugin = make_plugin(
            Path(tmp.name),
            config={
                "food_max_results": 1,
                "food_disabled_reply": "injected",
            },
        )
        self.assertEqual(plugin._cfg_int("food_max_results"), 1)
        self.assertEqual(plugin._cfg_str("food_disabled_reply"), "injected")

    def test_data_dir_uses_plugin_specific_subdirectory_by_default(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = os.environ.get("QQBOT_FOOD_TEST_DATA_DIR")
        os.environ["QQBOT_FOOD_TEST_DATA_DIR"] = tmp.name
        try:
            plugin = FoodPlugin(None, config={})
            self.assertEqual(plugin._data_dir, Path(tmp.name) / "qqbot_food")
        finally:
            if old is None:
                os.environ.pop("QQBOT_FOOD_TEST_DATA_DIR", None)
            else:
                os.environ["QQBOT_FOOD_TEST_DATA_DIR"] = old


if __name__ == "__main__":
    unittest.main()
