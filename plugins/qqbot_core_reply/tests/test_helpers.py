from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from support import (  # noqa: F401 - installs fake AstrBot
    core_main,
)
import config as config_module
import conversations as conversation_engine
import media as media_engine
import persona_bridge
import personas as persona_engine
import songs as song_engine


class ConfigReaderTests(unittest.TestCase):
    def test_dict_value_and_default(self):
        reader = config_module.ConfigReader({"reply_probability": 0.3})
        self.assertAlmostEqual(reader.float("reply_probability"), 0.3)
        self.assertEqual(
            reader.string("system_prompt"),
            config_module.DEFAULTS["system_prompt"],
        )

    def test_attribute_object(self):
        obj = types.SimpleNamespace(enabled=False, min_message_chars=7)
        reader = config_module.ConfigReader(obj)
        self.assertFalse(reader.bool("enabled"))
        self.assertEqual(reader.int("min_message_chars"), 7)

    def test_nested_config_wrapper(self):
        reader = config_module.ConfigReader({"config": {"reply_probability": 0.75}})
        self.assertAlmostEqual(reader.float("reply_probability"), 0.75)

    def test_bool_strings(self):
        reader = config_module.ConfigReader(
            {
                "a": "true",
                "b": "关闭",
                "c": 1,
                "d": "no",
                "e": "unknown",
            }
        )
        self.assertTrue(reader.bool("a"))
        self.assertFalse(reader.bool("b"))
        self.assertTrue(reader.bool("c"))
        self.assertFalse(reader.bool("d"))
        self.assertTrue(reader.bool("e", default=True))

    def test_int_clamped(self):
        reader = config_module.ConfigReader({"x": "999", "y": "-3"})
        self.assertEqual(reader.int("x", 0, 50), 50)
        self.assertEqual(reader.int("y", 0), 0)

    def test_float_clamped(self):
        reader = config_module.ConfigReader({"p": "1.7", "q": -0.2})
        self.assertEqual(reader.float("p"), 1.0)
        self.assertEqual(reader.float("q"), 0.0)

    def test_string_list_variants(self):
        reader = config_module.ConfigReader(
            {
                "a": ["/", "!"],
                "b": '["/", "?"]',
                "c": "a,b，c",
                "d": "single",
            }
        )
        self.assertEqual(reader.string_list("a"), ["/", "!"])
        self.assertEqual(reader.string_list("b"), ["/", "?"])
        self.assertEqual(reader.string_list("c"), ["a", "b", "c"])
        self.assertEqual(reader.string_list("d"), ["single"])

    def test_default_list_is_copied(self):
        reader = config_module.ConfigReader({})
        first = reader.string_list("ignore_command_prefixes")
        first.append("mutated")
        second = reader.string_list("ignore_command_prefixes")
        self.assertNotIn("mutated", second)


class PersonaTests(unittest.TestCase):
    def test_parse_default_and_group(self):
        parsed = persona_engine.parse_group_personas(
            json.dumps(
                {
                    "default": {"name": "D", "prompt": "default prompt"},
                    "groups": {"123": {"name": "G", "prompt": "group prompt"}},
                }
            )
        )
        self.assertEqual(parsed.default.name, "D")
        self.assertEqual(parsed.groups["123"].prompt, "group prompt")
        self.assertEqual(parsed.errors, 0)

    def test_parse_invalid_json(self):
        parsed = persona_engine.parse_group_personas("{not json")
        self.assertIsNone(parsed.default)
        self.assertEqual(parsed.groups, {})
        self.assertEqual(parsed.errors, 1)

    def test_invalid_group_ids_rejected(self):
        parsed = persona_engine.parse_group_personas(
            json.dumps({"groups": {"abc": "x", "123": "ok"}})
        )
        self.assertNotIn("abc", parsed.groups)
        self.assertIn("123", parsed.groups)
        self.assertGreaterEqual(parsed.errors, 1)

    def test_resolve_group_exact(self):
        parsed = persona_engine.parse_group_personas(
            json.dumps({"groups": {"123": {"name": "G", "prompt": "GP"}}})
        )
        prompt, name, source = persona_engine.resolve_persona(
            parsed, group_id="123", legacy_prompt="legacy"
        )
        self.assertEqual((prompt, name, source), ("GP", "G", "group"))

    def test_resolve_private_default(self):
        parsed = persona_engine.parse_group_personas(
            json.dumps({"default": {"name": "D", "prompt": "DP"}})
        )
        prompt, name, source = persona_engine.resolve_persona(
            parsed, is_private=True, legacy_prompt="legacy"
        )
        self.assertEqual((prompt, name, source), ("DP", "D", "default"))

    def test_resolve_legacy_fallback(self):
        parsed = persona_engine.parse_group_personas("{}")
        prompt, name, source = persona_engine.resolve_persona(
            parsed, group_id="999", legacy_prompt="legacy"
        )
        self.assertEqual((prompt, name, source), ("legacy", "", "legacy"))

    def test_prompt_profiles_active_and_find(self):
        config = persona_engine.parse_prompt_profiles(
            json.dumps(
                [
                    {"id": "a", "name": "Alpha", "prompt": "PA", "pinned": False},
                    {"id": "b", "name": "Beta", "prompt": "PB", "pinned": True},
                ]
            ),
            "a",
        )
        self.assertEqual(config.active.id, "a")
        self.assertEqual(config.default.id, "b")
        self.assertEqual(persona_engine.find_prompt_profile(config, "beta").id, "b")
        self.assertEqual(persona_engine.find_prompt_profile(config, "B").id, "b")

    def test_persona_bridge_profile_id_safe(self):
        self.assertEqual(
            persona_bridge.persona_id_for_profile("abc-1"),
            "qqbot_core_reply_prompt_abc-1",
        )
        self.assertEqual(persona_bridge.persona_id_for_profile("bad id"), "")
        self.assertEqual(
            persona_bridge.persona_id_for("123", "group"),
            "qqbot_core_reply_group_123",
        )


class ConversationHelperTests(unittest.TestCase):
    def test_normalize_title(self):
        self.assertEqual(conversation_engine.normalize_title("  a   b "), "a b")
        self.assertIsNone(conversation_engine.normalize_title(""))
        self.assertIsNone(conversation_engine.normalize_title("x" * 40, max_chars=32))
        self.assertIsNone(conversation_engine.normalize_title("a/b"))

    def test_find_all_by_title_casefold(self):
        items = [
            {"cid": "1", "title": "Alpha"},
            {"cid": "2", "title": "alpha"},
            {"cid": "3", "title": "Beta"},
        ]
        matches = conversation_engine.find_all_by_title(items, "ALPHA")
        self.assertEqual([item["cid"] for item in matches], ["1", "2"])

    def test_conversation_id_and_title_from_dict_and_attr(self):
        self.assertEqual(
            conversation_engine.conversation_id({"conversation_id": "abc"}),
            "abc",
        )
        obj = types.SimpleNamespace(cid="cid", title="T", persona_id="p")
        self.assertEqual(conversation_engine.conversation_id(obj), "cid")
        self.assertEqual(conversation_engine.conversation_title(obj), "T")
        self.assertEqual(conversation_engine.conversation_persona_id(obj), "p")

    def test_short_id_sanitizes(self):
        self.assertEqual(conversation_engine.short_id("abc/../123456789"), "123456789"[-8:])
        self.assertEqual(conversation_engine.short_id(""), "?")

    def test_created_at_unknown_sorts_last(self):
        self.assertEqual(conversation_engine.conversation_created_at({"created_at": "2"}), 2.0)
        self.assertEqual(
            conversation_engine.conversation_created_at({}),
            float("inf"),
        )


class SongHelperTests(unittest.TestCase):
    def test_resolve_song_file_and_reject_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good = root / "song.mp3"
            good.write_bytes(b"ID3")
            bad = root / "note.txt"
            bad.write_text("x")
            self.assertEqual(song_engine.resolve_song_file(root, "song.mp3"), good)
            self.assertIsNone(song_engine.resolve_song_file(root, "../song.mp3"))
            self.assertIsNone(song_engine.resolve_song_file(root, "note.txt"))

    def test_list_song_files_marks_oversized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.mp3").write_bytes(b"a" * 5)
            (root / "b.wav").write_bytes(b"b" * 50)
            files, oversized = song_engine.list_song_files(root, max_bytes=10)
            self.assertEqual([item.name for item in files], ["a.mp3"])
            self.assertEqual([item.name for item in oversized], ["b.wav"])


class MediaHelperTests(unittest.TestCase):
    def test_media_refs(self):
        self.assertTrue(media_engine.is_media_ref("https://example.com/a.png"))
        self.assertTrue(media_engine.is_media_ref("base64://abc"))
        self.assertTrue(media_engine.is_media_ref("data:image/png;base64,abc"))
        self.assertFalse(media_engine.is_media_ref(""))

    def test_local_image_ref_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "x.png"
            file_path.write_bytes(b"x")
            self.assertEqual(
                media_engine.local_image_ref_path(str(file_path)),
                file_path,
            )
            self.assertIsNone(media_engine.local_image_ref_path("https://x/a.png"))


if __name__ == "__main__":
    unittest.main()
