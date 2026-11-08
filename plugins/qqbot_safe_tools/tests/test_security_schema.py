"""Static security, packaging metadata and configuration-schema sanity tests."""

from __future__ import annotations

import ast
import inspect
import json
import sys
import unittest

from _support import PLUGIN_ROOT, main_module, safe_tools

PRODUCTION_FILES = (
    "main.py",
    "safe_tools.py",
    "food.py",
    "web_fetch.py",
    "contracts.py",
    "registry.py",
    "auth.py",
    "confirmation.py",
    "call_ledger.py",
    "dispatcher.py",
    "output_gate.py",
    "control_server.py",
)
ALLOW_KEYS = {
    "allow_fetch_webpage",
    "allow_search_images",
    "allow_search_food_restaurants",
    "allow_submit_food_review",
    "allow_send_sticker",
    "allow_collect_sticker",
    "allow_list_categories",
}
COOLDOWN_KEYS = {
    "web_fetch_cooldown_seconds",
    "image_search_cooldown_seconds",
    "food_search_cooldown_seconds",
    "food_submit_cooldown_seconds",
    "sticker_cooldown_seconds",
    "collect_sticker_cooldown_seconds",
    "list_categories_cooldown_seconds",
}
LIMIT_KEYS = {
    "max_keyword_chars",
    "max_search_count",
    "max_web_chars",
    "tool_timeout_seconds",
    "max_fallback_chars",
}
FALLBACK_KEYS = set(main_module._FALLBACK_CONFIG_KEYS.values())
EXPECTED_METHODS = {
    "fetch_webpage": ("qqbot_web_reader", "tool_fetch_webpage"),
    "search_images": ("qqbot_image_search", "tool_search_images"),
    "search_food_restaurants": ("qqbot_food", "tool_search_restaurants"),
    "submit_food_review": ("qqbot_food", "tool_submit_food_review"),
    "send_sticker": ("qqbot_stickers", "tool_send_sticker"),
    "collect_sticker": ("qqbot_stickers", "tool_collect_sticker"),
    "list_sticker_categories": ("qqbot_stickers", "tool_list_categories"),
}


class ConfigSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(
            (PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
        )

    def test_core_keys_present(self):
        for key in ("enabled", "marker_enabled", "max_actions"):
            self.assertIn(key, self.schema)

    def test_all_allow_toggles_present(self):
        self.assertTrue(ALLOW_KEYS.issubset(self.schema))
        self.assertEqual(
            {key for key in self.schema if key.startswith("allow_")},
            ALLOW_KEYS,
        )

    def test_all_cooldown_keys_present(self):
        self.assertTrue(COOLDOWN_KEYS.issubset(self.schema))

    def test_all_limit_keys_present(self):
        self.assertTrue(LIMIT_KEYS.issubset(self.schema))

    def test_all_fallback_reply_keys_present(self):
        self.assertTrue(FALLBACK_KEYS.issubset(self.schema))
        self.assertIn("fallback_generic", self.schema)

    def test_every_entry_has_type_and_default(self):
        for key, value in self.schema.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, dict)
                self.assertIn(value.get("type"), {"bool", "int", "string"})
                self.assertIn("default", value)
                self.assertIsInstance(value.get("description"), str)
                self.assertTrue(value["description"])

    def test_boolean_defaults_are_boolean(self):
        for key, value in self.schema.items():
            if value["type"] == "bool":
                with self.subTest(key=key):
                    self.assertIsInstance(value["default"], bool)

    def test_integer_defaults_and_sliders_are_in_range(self):
        for key, value in self.schema.items():
            if value["type"] != "int":
                continue
            with self.subTest(key=key):
                self.assertIsInstance(value["default"], int)
                slider = value.get("slider")
                self.assertIsInstance(slider, dict)
                self.assertLessEqual(slider["min"], value["default"])
                self.assertGreaterEqual(slider["max"], value["default"])
                self.assertGreaterEqual(slider["step"], 1)

    def test_max_actions_hard_limit_is_one(self):
        self.assertEqual(self.schema["max_actions"]["default"], 1)
        self.assertEqual(self.schema["max_actions"]["slider"]["max"], 1)

    def test_string_defaults_are_bounded_nonempty(self):
        for key, value in self.schema.items():
            if value["type"] != "string":
                continue
            with self.subTest(key=key):
                self.assertIsInstance(value["default"], str)
                self.assertTrue(value["default"])
                self.assertLessEqual(len(value["default"]), 500)

    def test_fallback_defaults_are_chinese(self):
        for key in FALLBACK_KEYS:
            with self.subTest(key=key):
                self.assertRegex(
                    self.schema[key]["default"], r"[\u4e00-\u9fff]"
                )


class PackagingTests(unittest.TestCase):
    def test_version_file_is_3_1_0(self):
        version = (PLUGIN_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "3.1.1")

    def test_metadata_version_matches(self):
        metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("name: qqbot_safe_tools", metadata)
        self.assertIn("version: 3.1.1", metadata)
        self.assertIn('astrbot_version: ">=4.29,<5"', metadata)
        self.assertIn("aiocqhttp", metadata)

    def test_readme_documents_sole_owner_and_contracts(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("唯一", readme)
        self.assertIn("qqbot_core_reply_candidate", readme)
        self.assertIn("tool_fetch_webpage", readme)
        self.assertIn("tool_search_images", readme)
        self.assertIn("tool_search_restaurants", readme)
        self.assertIn("tool_send_sticker", readme)
        self.assertIn("安全", readme)
        self.assertIn("unittest", readme)

    def test_readme_mentions_no_llm_and_hard_cap(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("不调用 LLM", readme)
        self.assertIn("最多一个", readme)

    def test_constructor_accepts_injected_config(self):
        signature = inspect.signature(main_module.SafeToolsPlugin.__init__)
        self.assertEqual(
            list(signature.parameters),
            ["self", "context", "config"],
        )
        self.assertIsNone(signature.parameters["config"].default)

    def test_dependency_method_mapping_is_exact(self):
        self.assertEqual(main_module._PLUGIN_METHODS, EXPECTED_METHODS)

    def test_safe_tools_whitelist_excludes_execution_names(self):
        lowered = {name.casefold() for name in safe_tools.ALLOWED_TOOLS}
        self.assertFalse(lowered & {"eval", "exec", "shell", "subprocess"})

    def test_marker_handler_priority_and_command_priority(self):
        self.assertEqual(
            main_module.SafeToolsPlugin.consume_safe_tools_marker._qqbot_decorating_priority,
            200,
        )
        args, kwargs = main_module.SafeToolsPlugin.status._qqbot_command
        self.assertEqual(args, ("工具协议",))
        self.assertEqual(kwargs.get("priority"), 110)


class StaticSecurityTests(unittest.TestCase):
    def _trees(self):
        for name in PRODUCTION_FILES:
            path = PLUGIN_ROOT / name
            with self.subTest(file=name):
                yield name, ast.parse(path.read_text(encoding="utf-8"))

    def test_no_dynamic_code_execution_calls(self):
        forbidden_builtins = {"eval", "exec", "compile", "__import__"}
        forbidden_methods = {"system", "popen"}
        for name, tree in self._trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if isinstance(function, ast.Name):
                    with self.subTest(file=name, call=function.id):
                        self.assertNotIn(function.id, forbidden_builtins)
                elif isinstance(function, ast.Attribute):
                    with self.subTest(file=name, call=function.attr):
                        self.assertNotIn(function.attr, forbidden_methods)

    def test_no_shell_true_keyword(self):
        for name, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        with self.subTest(file=name):
                            self.assertFalse(
                                keyword.arg == "shell"
                                and isinstance(keyword.value, ast.Constant)
                                and keyword.value.value is True
                            )

    def test_no_subprocess_or_pty_imports(self):
        forbidden = {"subprocess", "pty", "multiprocessing"}
        for name, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        with self.subTest(file=name, module=alias.name):
                            self.assertNotIn(alias.name.split(".")[0], forbidden)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    with self.subTest(file=name, module=node.module):
                        self.assertNotIn(
                            str(node.module or "").split(".")[0], forbidden
                        )

    def test_imports_are_stdlib_astrbot_or_local(self):
        allowed = set(sys.stdlib_module_names) | {"astrbot", "__future__"}
        allowed_local = {
            "safe_tools",
            "food",
            "web_fetch",
            "auth",
            "aux_budget",
            "call_ledger",
            "capability_policy",
            "confirmation",
            "contracts",
            "control_server",
            "dispatcher",
            "output_gate",
            "registry",
            "tool_registry",
        }
        for name, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        with self.subTest(file=name, module=alias.name):
                            self.assertTrue(
                                root in allowed or root in allowed_local
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    root = str(node.module or "").split(".")[0]
                    with self.subTest(file=name, module=node.module):
                        self.assertTrue(root in allowed or root in allowed_local)

    def test_no_other_plugin_source_imports(self):
        forbidden_prefixes = (
            "qqbot_enhancer",
            "qqbot_core_full",
            "qqbot_web_reader",
            "qqbot_image_search",
            "qqbot_food",
            "qqbot_stickers",
            "qqbot_memory_commands",
            "plugins.",
        )
        for name, tree in self._trees():
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.Import):
                    module = ",".join(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = str(node.module or "")
                if module:
                    for prefix in forbidden_prefixes:
                        with self.subTest(file=name, module=module):
                            self.assertFalse(module.startswith(prefix))

    def test_no_llm_invocation_in_main(self):
        tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
        forbidden_attrs = {
            "llm_generate",
            "get_using_provider",
            "get_provider",
            "chat",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                with self.subTest(attr=node.attr):
                    self.assertNotIn(node.attr, forbidden_attrs)

    def test_no_global_get_config_read(self):
        tree = ast.parse((PLUGIN_ROOT / "main.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                with self.subTest(attr=node.attr):
                    self.assertNotEqual(node.attr, "get_config")

    def test_no_requests_or_third_party_http_client(self):
        for name, tree in self._trees():
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.Import):
                    module = ",".join(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = str(node.module or "")
                if module:
                    with self.subTest(file=name, module=module):
                        self.assertNotIn(module.split(".")[0], {"requests", "httpx", "aiohttp"})

    def test_marker_parser_has_strict_regex_and_safe_category_regex(self):
        self.assertIsInstance(safe_tools.TOOL_MARKER_RE.pattern, str)
        self.assertIn("tool", safe_tools.TOOL_MARKER_RE.pattern)
        self.assertTrue(safe_tools.SAFE_CATEGORY_RE.fullmatch("安全_标签-1"))
        self.assertFalse(safe_tools.SAFE_CATEGORY_RE.fullmatch("../x"))

    def test_all_production_files_compile(self):
        for name in PRODUCTION_FILES:
            with self.subTest(file=name):
                source = (PLUGIN_ROOT / name).read_text(encoding="utf-8")
                compile(source, name, "exec")


if __name__ == "__main__":
    unittest.main()
