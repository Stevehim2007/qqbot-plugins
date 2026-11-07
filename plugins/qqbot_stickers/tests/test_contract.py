from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import unittest
from pathlib import Path

from tests import fakes

fakes.install()
import main

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_version_files_are_3_0_0(self):
        self.assertEqual((PLUGIN_DIR / "VERSION").read_text().strip(), "3.0.2")
        metadata_text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 3.0.2", metadata_text)
        self.assertIn('astrbot_version: ">=4.29,<5"', metadata_text)

    def test_config_schema_is_valid_and_complete(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIsInstance(schema, dict)
        self.assertGreaterEqual(len(schema), 60)
        self.assertEqual(set(schema), set(main.DEFAULTS))
        valid_types = {"bool", "int", "float", "string", "text", "list"}
        for key, item in schema.items():
            self.assertIn("description", item, key)
            self.assertIn(item["type"], valid_types, key)
            self.assertEqual(item["default"], main.DEFAULTS[key], key)

    def test_schema_covers_required_setting_groups(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        required_prefixes = (
            "sticker_",
            "collected_sticker_",
            "auto_collect_",
            "safe_tools_",
        )
        for prefix in required_prefixes:
            self.assertTrue(any(key.startswith(prefix) for key in schema), prefix)
        self.assertIn("sticker_natural_language_reclassify_enabled", schema)
        self.assertIn("sticker_semantic_tags_enabled", schema)
        self.assertIn("collected_sticker_saved_reply", schema)
        self.assertIn("sticker_natural_language_empty_reply", schema)

    def test_plugin_constructor_accepts_injected_config(self):
        signature = inspect.signature(main.StickerPlugin.__init__)
        self.assertEqual(list(signature.parameters), ["self", "context", "config"])
        self.assertIsNone(signature.parameters["config"].default)

    def test_required_commands_are_decorated(self):
        expected = {
            "collect_sticker_command": "收藏表情",
            "sticker_status_command": "表情状态",
            "sticker_categories_command": "表情分类",
            "sticker_list_command": "表情列表",
            "send_sticker_command": "发表情",
        }
        for method, command_name in expected.items():
            function = getattr(main.StickerPlugin, method)
            self.assertEqual(getattr(function, "_command"), command_name)

    def test_decoration_priority_is_100(self):
        function = main.StickerPlugin.decorate_core_result
        self.assertEqual(getattr(function, "_decorating_priority"), 100)

    def test_stable_tool_api_methods_exist_and_are_async(self):
        for name in (
            "tool_send_sticker",
            "tool_collect_sticker",
            "tool_list_categories",
        ):
            function = getattr(main.StickerPlugin, name)
            self.assertTrue(inspect.iscoroutinefunction(function), name)
        send_sig = inspect.signature(main.StickerPlugin.tool_send_sticker)
        self.assertEqual(list(send_sig.parameters), ["self", "event", "category", "emotion", "tag"])
        for param in ("category", "emotion", "tag"):
            self.assertIs(send_sig.parameters[param].kind, inspect.Parameter.KEYWORD_ONLY)
        collect_sig = inspect.signature(main.StickerPlugin.tool_collect_sticker)
        self.assertEqual(list(collect_sig.parameters), ["self", "event", "category"])
        self.assertIs(collect_sig.parameters["category"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_sources_do_not_import_other_plugin_modules(self):
        forbidden_roots = {"qqbot_enhancer", "qqbot_core_full", "qqbot_safe_tools"}
        for path in PLUGIN_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        self.assertNotIn(root, forbidden_roots, f"{path.name}: {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        root = node.module.split(".", 1)[0]
                        self.assertNotIn(root, forbidden_roots, f"{path.name}: {node.module}")

    def test_data_dir_uses_plugin_name_keyword(self):
        source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn('StarTools.get_data_dir(plugin_name=PLUGIN_NAME)', source)
        self.assertIn('StarTools.get_data_dir(plugin_name=LEGACY_PLUGIN_NAME)', source)

    def test_store_source_has_process_and_os_locks(self):
        source = (PLUGIN_DIR / "sticker_store.py").read_text(encoding="utf-8")
        self.assertIn("fcntl.flock", source)
        self.assertIn("LOCK_EX", source)
        self.assertIn("LOCK_SH", source)
        self.assertIn("os.replace", source)
        self.assertIn("os.fsync", source)
        self.assertIn("O_NOFOLLOW", source)

    def test_readme_documents_operations(self):
        readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "数据布局",
            "备份与恢复",
            "旧 qqbot_enhancer 迁移",
            "单写者",
            "权限",
            "tool_send_sticker",
            "priority=100",
        ):
            self.assertIn(phrase, readme)

    def test_no_third_party_runtime_imports(self):
        allowed_stdlib = {
            "__future__",
            "asyncio",
            "base64",
            "binascii",
            "contextlib",
            "copy",
            "dataclasses",
            "hashlib",
            "hmac",
            "secrets",
            "inspect",
            "json",
            "operator",
            "os",
            "random",
            "re",
            "stat",
            "tempfile",
            "threading",
            "time",
            "typing",
            "uuid",
            "pathlib",
            "fcntl",
            "sys",
        }
        allowed_external = {"astrbot"}
        local = {
            "contracts",
            "media",
            "migration",
            "sticker_intent",
            "sticker_reclassify",
            "sticker_service",
            "sticker_store",
        }
        for path in PLUGIN_DIR.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".", 1)[0]]
                for name in names:
                    self.assertTrue(
                        name in allowed_stdlib or name in allowed_external or name in local,
                        f"{path.name} imports {name}",
                    )

    def test_package_file_is_importable_marker(self):
        self.assertTrue((PLUGIN_DIR / "__init__.py").exists())
        spec = importlib.util.spec_from_file_location("qqbot_stickers_pkg", PLUGIN_DIR / "__init__.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIsNotNone(module)


if __name__ == "__main__":
    unittest.main()
