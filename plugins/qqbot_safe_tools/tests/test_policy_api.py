"""Plugin-level capability policy API tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_server import CONTROL_METHODS
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

    def test_aux_budget_get_set_usage_and_feature(self):
        info = self.plugin.aux_budget_get()
        self.assertTrue(info["ok"])
        self.assertEqual(info["status"]["tier"], "standard")
        self.assertEqual(info["status"]["limit_cny"], 20.0)
        self.assertEqual(len(info["config"]["features"]), 13)

        updated = self.plugin.aux_budget_set(tier="light")
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["status"]["limit_cny"], 10.0)
        feature = self.plugin.aux_budget_feature_set(
            feature_id="memory.extract", enabled=False
        )
        self.assertTrue(feature["ok"])
        self.assertFalse(feature["status"]["ok"] is False)  # status is a read model

        reservation = self.plugin._aux_budget().reserve(
            "sticker.ai_collect", estimate_cny=0.2, request_id="api-1"
        )
        self.plugin._aux_budget().commit(reservation["reservation_id"], actual_cost_cny=0.1)
        usage = self.plugin.aux_budget_usage()
        self.assertEqual(usage["spent_cny"], 0.1)
        self.assertTrue(any(item["feature_id"] == "sticker.ai_collect" for item in usage["features"]))
        self.plugin.aux_budget_cycle_reset()
        self.assertEqual(self.plugin.aux_budget_usage()["spent_cny"], 0.0)

    def test_aux_budget_control_methods_are_allowlisted(self):
        for method in (
            "aux_budget.get",
            "aux_budget.set",
            "aux_budget.usage",
            "aux_budget.feature_set",
            "aux_budget.cycle_reset",
        ):
            self.assertIn(method, CONTROL_METHODS)

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
