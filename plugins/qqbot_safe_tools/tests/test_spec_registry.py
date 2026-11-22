"""Schema-generated parameter specs (framework v1 §15)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import capability_policy as cp
import spec_registry as sr


def _write_plugin(root: Path, name: str, schema: dict, *, overlay: bool = False) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "_conf_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False), encoding="utf-8"
    )
    if overlay:
        (directory / "policy_overlay.py").write_text("VERSION = 1\n", encoding="utf-8")
    return directory


class RegistryBasicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_scalar_types_become_specs_and_complex_are_skipped(self) -> None:
        _write_plugin(
            self.root,
            "qqbot_demo",
            {
                "flag": {"type": "bool", "default": True},
                "count": {"type": "int", "default": 3, "slider": {"min": 1, "max": 9}},
                "ratio": {"type": "float", "default": 0.5, "slider": {"min": 0.0, "max": 1.0}},
                "label": {"type": "string", "default": "x"},
                "prompt": {"type": "text", "default": "hello"},
                "tags": {"type": "list", "default": []},
                "blob": {"type": "object", "default": {}},
            },
        )
        specs = sr.generate_specs("qqbot_demo", self.root)
        self.assertEqual(set(specs), {"flag", "count", "ratio", "label", "prompt"})
        self.assertEqual(specs["count"].kind, "int")
        self.assertEqual((specs["count"].minimum, specs["count"].maximum), (1, 9))
        self.assertEqual(specs["ratio"].kind, "float")
        self.assertEqual(specs["prompt"].kind, "text")
        self.assertEqual(sr.unsupported_keys("qqbot_demo", self.root), {"tags": "list", "blob": "object"})

    def test_missing_or_broken_schema_yields_nothing(self) -> None:
        (self.root / "qqbot_broken").mkdir()
        (self.root / "qqbot_broken" / "_conf_schema.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(sr.generate_specs("qqbot_broken", self.root), {})
        self.assertEqual(sr.generate_specs("qqbot_absent", self.root), {})
        self.assertEqual(sr.digest("qqbot_absent", self.root), "")
        self.assertFalse(sr.overlay_supported("qqbot_absent", self.root))

    def test_credential_keys_stay_sensitive(self) -> None:
        _write_plugin(
            self.root,
            "qqbot_demo",
            {
                "api_key": {"type": "string"},
                "client_secret": {"type": "string"},
                "openai_token": {"type": "string"},
                "display_name": {"type": "string"},
            },
        )
        specs = sr.generate_specs("qqbot_demo", self.root)
        self.assertTrue(specs["api_key"].sensitive)
        self.assertTrue(specs["client_secret"].sensitive)
        self.assertTrue(specs["openai_token"].sensitive)
        self.assertFalse(specs["display_name"].sensitive)

    def test_system_managed_anchors_are_not_group_overridable(self) -> None:
        _write_plugin(
            self.root,
            "qqbot_demo",
            {
                "owner_epoch": {"type": "int"},
                "policy.owner_epoch": {"type": "int"},
                "index_schema_version": {"type": "int"},
                "normal_toggle": {"type": "bool"},
            },
        )
        specs = sr.generate_specs("qqbot_demo", self.root)
        for key in ("owner_epoch", "policy.owner_epoch", "index_schema_version"):
            self.assertTrue(specs[key].system_managed, key)
        self.assertFalse(specs["normal_toggle"].system_managed)
        self.assertEqual(sr.digest("qqbot_demo", self.root), sr.digest("qqbot_demo", self.root))

    def test_cache_is_keyed_by_mtime(self) -> None:
        directory = _write_plugin(self.root, "qqbot_demo", {"a": {"type": "bool"}})
        self.assertEqual(set(sr.generate_specs("qqbot_demo", self.root)), {"a"})
        path = directory / "_conf_schema.json"
        path.write_text(json.dumps({"a": {"type": "bool"}, "b": {"type": "int"}}), encoding="utf-8")
        import os

        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 5))
        self.assertEqual(set(sr.generate_specs("qqbot_demo", self.root)), {"a", "b"})


class MergedSpecsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sr.clear_cache()

    def tearDown(self) -> None:
        sr.clear_cache()
        self.tmp.cleanup()

    def test_manual_specs_win_by_policy_name_and_config_key(self) -> None:
        manual = cp.PLUGIN_PARAM_SPECS["qqbot_core_reply"]
        specs = cp.plugin_specs("qqbot_core_reply")
        for name, spec in manual.items():
            self.assertEqual(specs[name].source, "manual", name)
            self.assertEqual(specs[name].config_key, spec.config_key)
        # participation.group_probability is the config key of a manual spec
        self.assertNotIn("participation.group_probability", specs)
        # the schema-only key is generated with itself as config key
        self.assertEqual(specs["context.max_chars"].source, "manual")
        generated = [name for name, spec in specs.items() if spec.source == "schema"]
        self.assertGreater(len(generated), 80)

    def test_schema_count_matches_declared_scalars(self) -> None:
        for plugin in ("qqbot_core_reply", "qqbot_food", "qqbot_stickers"):
            schema = json.loads(
                (sr.plugins_root() / plugin / "_conf_schema.json").read_text(encoding="utf-8")
            )
            scalar = {
                key
                for key, entry in schema.items()
                if isinstance(entry, dict)
                and str(entry.get("type") or "").lower() in sr.SCALAR_TYPES
            }
            generated = set(sr.generate_specs(plugin))
            self.assertEqual(generated, scalar, plugin)


class ParamSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = cp.CapabilityPolicy(Path(self.tmp.name) / "policy.json")
        self.policy.load()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_param_schema_reports_overlay_and_registry_metadata(self) -> None:
        payload = self.policy.param_schema("qqbot_core_reply")
        self.assertIn("overlay_supported", payload)
        self.assertIsInstance(payload["overlay_supported"], bool)
        self.assertTrue(payload["digest"])
        self.assertTrue(payload["registry_digest"])
        self.assertGreater(payload["scalar_count"], 80)
        field = payload["fields"]["participation.reply_probability"]
        self.assertEqual(field["source"], "manual")
        self.assertEqual(field["config_key"], "participation.group_probability")
        self.assertIn("system_managed", field)

    def test_schema_param_requires_overlay_support(self) -> None:
        specs = {
            name: spec
            for name, spec in cp.plugin_specs("qqbot_weather").items()
            if spec.source == "schema" and spec.kind in {"str", "text"}
        }
        self.assertTrue(specs, "qqbot_weather must expose at least one scalar string")
        key = sorted(specs)[0]
        self.assertFalse(cp.spec_registry.overlay_supported("qqbot_weather"))
        with self.assertRaises(cp.PolicyError) as ctx:
            self.policy.set_override(
                plugin_id="qqbot_weather",
                group_id="123456",
                params={key: "上海"},
            )
        self.assertEqual(ctx.exception.code, "overlay_unsupported")

    def test_text_kind_accepts_newlines(self) -> None:
        spec = cp.ParamSpec(kind="text", max_chars=20)
        self.assertEqual(cp._coerce_scalar(spec, "  a\nb  "), "a\nb")
        with self.assertRaises(cp.PolicyError):
            cp._coerce_scalar(spec, "a\x00b")
        with self.assertRaises(cp.PolicyError):
            cp._coerce_scalar(spec, "   ")


if __name__ == "__main__":
    unittest.main()
