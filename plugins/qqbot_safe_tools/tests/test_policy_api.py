"""Plugin-level capability policy API tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _support import base_config, make_plugin


class PolicyApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = make_plugin(config=base_config())
        self.plugin._data_dir = lambda: Path(self.tmp.name)

    def test_set_effective_reset_and_snapshot(self):
        result = self.plugin.policy_set(
            plugin_id="qqbot_food",
            group_id="20002",
            enabled=False,
            params={"default_city": "长春"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], 1)
        effective = self.plugin.policy_effective("qqbot_food", group_id="20002")
        self.assertFalse(effective["enabled"])
        self.assertEqual(effective["params"]["default_city"], "长春")
        self.assertIn("group:20002", effective["sources"])

        snapshot = self.plugin.policy_snapshot()
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["groups"]["20002"]["plugins"]["qqbot_food"]["enabled"], False)

        reset = self.plugin.policy_reset(
            plugin_id="qqbot_food",
            group_id="20002",
            expected_revision=1,
        )
        self.assertTrue(reset["ok"])
        self.assertTrue(
            self.plugin.policy_effective("qqbot_food", group_id="20002")["enabled"]
        )
        self.assertEqual(reset["revision"], 2)

    def test_stale_revision_is_reported(self):
        first = self.plugin.policy_set(
            plugin_id="qqbot_food", group_id="20002", enabled=False
        )
        self.assertTrue(first["ok"])
        stale = self.plugin.policy_set(
            plugin_id="qqbot_food",
            group_id="20002",
            enabled=True,
            expected_revision=0,
        )
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"], "revision_conflict")


if __name__ == "__main__":
    unittest.main()
