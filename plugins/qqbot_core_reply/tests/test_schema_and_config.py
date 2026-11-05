from __future__ import annotations

import ast
import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

from support import FakeContext, FakeEvent, collect_event, extras, make_plugin
import config as config_module
import main as core_main

ROOT = Path(__file__).resolve().parent.parent


def run(coro):
    return asyncio.run(coro)


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_json(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIsInstance(schema, dict)
        self.assertGreaterEqual(len(schema), 60)

    def test_schema_covers_every_default_key(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema), set(config_module.DEFAULTS))

    def test_required_legacy_keys_mapped(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in (
            "memory_mode",
            "reply_probability",
            "group_cooldown_seconds",
            "user_cooldown_seconds",
            "min_message_chars",
            "max_input_chars",
            "max_message_chars",
            "max_reply_chars",
            "ignore_command_prefixes",
            "inherit_astrbot_wake_prefix",
            "ambient_group_chat_enabled",
            "system_prompt",
            "llm_timeout_seconds",
            "vision_provider_id",
            "image_caption_prompt",
            "group_personas",
            "group_identity_context_enabled",
            "conversation_switch_enabled",
            "option_max_chars",
            "song_enabled",
        ):
            self.assertIn(key, schema)

    def test_schema_probability_bounds(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["reply_probability"]["slider"]["min"], 0)
        self.assertEqual(schema["reply_probability"]["slider"]["max"], 1)

    def test_version_and_metadata_bumped(self):
        self.assertEqual((ROOT / "VERSION").read_text().strip(), "4.0.0")
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 4.0.0", metadata)
        self.assertIn("name: qqbot_core_reply", metadata)
        self.assertIn("astrbot_version: \">=4.29,<5\"", metadata)

    def test_readme_documents_architecture_ownership_rollback(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("唯一的主回复 / LLM owner", readme)
        self.assertIn("qqbot_safe_tools", readme)
        self.assertIn("qqbot_stickers", readme)
        self.assertIn("回滚", readme)
        self.assertIn("python3 -m unittest discover -s tests -q", readme)


class ImportIsolationTests(unittest.TestCase):
    def test_no_legacy_modules_imported(self):
        self.assertNotIn("qqbot_enhancer", sys.modules)
        self.assertNotIn("qqbot_core_full", sys.modules)

    def test_source_has_no_legacy_plugin_imports(self):
        for name in (
            "main.py",
            "config.py",
            "conversations.py",
            "personas.py",
            "persona_bridge.py",
            "media.py",
            "songs.py",
        ):
            source = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("qqbot_enhancer", source)
            self.assertNotIn("qqbot_core_full", source)

    def test_main_imports_only_stdlib_astrbot_and_local_helpers(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        allowed_local = {
            "config",
            "conversations",
            "media",
            "persona_bridge",
            "personas",
            "songs",
        }
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        external = imported - allowed_local - {"astrbot"}
        stdlib = {
            "__future__",
            "asyncio",
            "base64",
            "inspect",
            "json",
            "math",
            "random",
            "re",
            "time",
            "pathlib",
            "typing",
            "sys",
        }
        self.assertEqual(external - stdlib, set())

    def test_no_marker_parser_or_executor_in_core(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("TOOL_MARKER_RE", source)
        self.assertNotIn("re.compile(r\"\\[\\[tool", source)
        self.assertNotIn("extract_tool", source)
        self.assertNotIn("execute_tool", source)


class ConfigInjectionTests(unittest.TestCase):
    def test_injected_config_controls_enabled_not_global_config(self):
        ctx = FakeContext(global_config={"enabled": False})
        plugin, _, _ = make_plugin(
            context=ctx,
            config={"enabled": True},
        )
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(len(results), 1)

    def test_global_config_cannot_enable_plugin(self):
        ctx = FakeContext(global_config={"enabled": True})
        plugin, _, _ = make_plugin(
            context=ctx,
            config={"enabled": False},
        )
        event = FakeEvent("hello", private=True)
        results = run(collect_event(plugin, event))
        self.assertEqual(results, [])
        self.assertEqual(ctx.llm_calls, [])

    def test_attribute_style_plugin_config_object(self):
        ctx = FakeContext()
        config = types.SimpleNamespace(
            enabled=True,
            reply_probability=1,
            system_prompt="ATTR-SYSTEM",
        )
        plugin = core_main.CoreReplyPlugin(ctx, config=config)
        ctx.llm_responses = ["ok"]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(len(results), 1)
        self.assertEqual(ctx.llm_calls[0]["system_prompt"], "ATTR-SYSTEM")

    def test_missing_config_defaults_enabled(self):
        ctx = FakeContext()
        plugin = core_main.CoreReplyPlugin(ctx, config=None)
        ctx.llm_responses = ["ok"]
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(len(results), 1)

    def test_plugin_constructor_signature_accepts_context_and_config(self):
        import inspect

        parameters = list(
            inspect.signature(core_main.CoreReplyPlugin.__init__).parameters
        )
        self.assertEqual(parameters[:3], ["self", "context", "config"])

    def test_max_message_alias_uses_tighter_limit(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "max_input_chars": 10,
                "max_message_chars": 4,
            }
        )
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("abcdefghij", private=True)))
        self.assertIn("abcd", ctx.llm_calls[0]["prompt"])
        self.assertNotIn("abcde", ctx.llm_calls[0]["prompt"])

    def test_candidate_extras_original_text(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        event = FakeEvent("original text", private=True)
        run(collect_event(plugin, event))
        self.assertEqual(extras(event)["qqbot_core_reply_text"], "original text")
        self.assertTrue(extras(event)["qqbot_core_reply_candidate"])
        self.assertTrue(extras(event)["qqbot_core_reply_allow_sticker"])


if __name__ == "__main__":
    unittest.main()
