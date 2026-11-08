"""Per-group capability policy tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qqbot_safe_tools.capability_policy import (
    CapabilityPolicy,
    PolicyError,
    SCHEMA_VERSION,
)


class CapabilityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "capability_policy.json"
        self.policy = CapabilityPolicy(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_starts_with_safe_defaults(self):
        effective = self.policy.effective("qqbot_food", group_id="g1")
        self.assertTrue(effective.enabled)
        self.assertEqual(dict(effective.params), {})
        self.assertEqual(effective.revision, 0)

    def test_group_override_merges_over_global_and_template(self):
        self.policy.set_template_override(
            template_id="standard",
            plugin_id="qqbot_food",
            enabled=True,
            params={"default_city": "长春市", "max_results": 5},
        )
        self.policy.set_group_template("g1", "standard")
        self.policy.set_override(
            plugin_id="qqbot_food",
            group_id="g1",
            enabled=False,
            params={"max_results": 3},
        )
        effective = self.policy.effective("qqbot_food", group_id="g1")
        self.assertFalse(effective.enabled)
        self.assertEqual(effective.params["default_city"], "长春市")
        self.assertEqual(effective.params["max_results"], 3)
        self.assertIn("template:standard", effective.sources)
        self.assertIn("group:g1", effective.sources)

    def test_private_is_independent_of_group(self):
        self.policy.set_override(
            plugin_id="qqbot_food",
            group_id="g1",
            enabled=False,
            params={"max_results": 2},
        )
        private = self.policy.effective(
            "qqbot_food", user_id="u1", is_private=True
        )
        self.assertTrue(private.enabled)
        self.assertEqual(dict(private.params), {})

    def test_unknown_plugin_param_and_sensitive_param_are_rejected(self):
        with self.assertRaises(PolicyError) as ctx:
            self.policy.effective("qqbot_unknown", group_id="g1")
        self.assertEqual(ctx.exception.code, "unknown_plugin")
        with self.assertRaises(PolicyError) as ctx:
            self.policy.set_override(
                plugin_id="qqbot_food", group_id="g1", params={"nope": 1}
            )
        self.assertEqual(ctx.exception.code, "unknown_param")
        with self.assertRaises(PolicyError) as ctx:
            self.policy.set_override(
                plugin_id="qqbot_safe_tools",
                group_id="g1",
                params={"tool_allowlist": ["fetch_webpage"]},
            )
        self.assertEqual(ctx.exception.code, "sensitive_param")

    def test_type_and_boundaries_are_enforced(self):
        with self.assertRaises(PolicyError) as ctx:
            self.policy.set_override(
                plugin_id="qqbot_stickers",
                group_id="g1",
                params={"decoration_probability": 1.5},
            )
        self.assertEqual(ctx.exception.code, "invalid_param")
        with self.assertRaises(PolicyError) as ctx:
            self.policy.set_override(
                plugin_id="qqbot_core_reply",
                group_id="g1",
                params={"participation.reply_probability": "0.2"},
            )
        self.assertEqual(ctx.exception.code, "invalid_param")

    def test_revision_conflict_is_rejected(self):
        self.policy.set_override(
            plugin_id="qqbot_food", group_id="g1", params={"max_results": 2}
        )
        with self.assertRaises(PolicyError) as ctx:
            self.policy.set_override(
                plugin_id="qqbot_food",
                group_id="g1",
                params={"max_results": 4},
                expected_revision=0,
            )
        self.assertEqual(ctx.exception.code, "revision_conflict")

    def test_persist_and_reload(self):
        self.policy.set_override(
            plugin_id="qqbot_stickers",
            group_id="g1",
            params={"decoration_probability": 0.4},
        )
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], SCHEMA_VERSION)
        self.assertEqual(raw["revision"], 1)
        reloaded = CapabilityPolicy(self.path)
        effective = reloaded.effective("qqbot_stickers", group_id="g1")
        self.assertEqual(effective.params["decoration_probability"], 0.4)

    def test_reset_removes_group_override(self):
        self.policy.set_override(
            plugin_id="qqbot_food", group_id="g1", enabled=False
        )
        self.policy.reset_override(plugin_id="qqbot_food", group_id="g1")
        effective = self.policy.effective("qqbot_food", group_id="g1")
        self.assertTrue(effective.enabled)
        self.assertEqual(dict(effective.params), {})

    def test_scope_snapshot_and_template_crud(self):
        self.policy.upsert_template(
            "standard",
            plugins={
                "qqbot_food": {
                    "enabled": True,
                    "params": {"default_city": "长春市", "max_results": 5},
                }
            },
        )
        self.policy.set_group_template("g1", "standard", expected_revision=1)
        self.policy.set_override(
            plugin_id="qqbot_food",
            group_id="g1",
            enabled=False,
            params={"max_results": 3},
            expected_revision=2,
        )
        snapshot = self.policy.scope_snapshot("group", "g1")
        self.assertEqual(snapshot["scope_type"], "group")
        self.assertEqual(snapshot["scope_id"], "g1")
        self.assertEqual(snapshot["template"], "standard")
        self.assertFalse(snapshot["plugins"]["qqbot_food"]["enabled"])
        self.assertEqual(
            snapshot["effective"]["qqbot_food"]["params"]["default_city"], "长春市"
        )
        self.assertEqual(
            snapshot["effective"]["qqbot_food"]["params"]["max_results"], 3
        )
        self.assertIn("default_city", snapshot["schemas"]["qqbot_food"])
        template_data = self.policy.list_templates()
        self.assertEqual(template_data["templates"]["standard"]["used_by"], ["g1"])

    def test_scope_reset_and_audit_filter(self):
        self.policy.set_override(
            plugin_id="qqbot_food", group_id="g1", params={"max_results": 3}
        )
        self.policy.set_override(
            plugin_id="qqbot_stickers",
            group_id="g2",
            params={"decoration_probability": 0.2},
        )
        audit = self.policy.audit_entries(scope_type="group", scope_id="g1")
        self.assertGreaterEqual(audit["total"], 1)
        self.assertTrue(all(row["target"] == "group:g1" for row in audit["items"]))
        result = self.policy.scope_reset("group", "g1", expected_revision=self.policy.revision)
        self.assertTrue(result["ok"])
        snapshot = self.policy.scope_snapshot("group", "g1")
        self.assertFalse(snapshot["plugins"])

    def test_param_schema_hides_sensitive_group_writes(self):
        schema = self.policy.param_schema("qqbot_safe_tools")["fields"]
        self.assertFalse(schema["tool_allowlist"]["group_overridable"])
        self.assertTrue(schema["tool_allowlist"]["sensitive"])
        food = self.policy.param_schema("qqbot_food")["fields"]
        self.assertTrue(food["default_city"]["group_overridable"])

    def test_scope_snapshot_exposes_plugin_defaults(self):
        snapshot = self.policy.scope_snapshot("group", "g1")
        self.assertEqual(snapshot["defaults"]["qqbot_core_reply"]["help_command_enabled"], True)
        self.assertEqual(snapshot["defaults"]["qqbot_food"]["max_results"], 5)
        schema = self.policy.param_schema("qqbot_food")["fields"]
        self.assertEqual(schema["sort"]["default"], "total")

    def test_corrupt_store_is_rejected(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PolicyError) as ctx:
            CapabilityPolicy(self.path)
        self.assertEqual(ctx.exception.code, "invalid_store")


if __name__ == "__main__":
    unittest.main()
