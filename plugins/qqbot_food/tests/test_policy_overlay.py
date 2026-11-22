"""config.overlay.v1 wiring for qqbot_food."""

from __future__ import annotations

import unittest

import policy_overlay
from config import PluginConfig


def _overlay(**values):
    return policy_overlay.build(
        "qqbot_food",
        {
            "params": {"max_results": 9},
            "config_values": {"food_max_results": 9, **values},
            "revision": 3,
        },
        scope="group",
        scope_id="456",
    )


class FoodOverlayTest(unittest.TestCase):
    def test_config_keys_are_overridden_for_the_event(self) -> None:
        config = PluginConfig({"food_max_results": 2, "food_default_city": "北京"})
        self.assertEqual(config.int("food_max_results", default=5), 2)
        with policy_overlay.policy_scope(_overlay()):
            self.assertEqual(config.int("food_max_results", default=5), 9)
            self.assertEqual(config.raw("food_default_city"), "北京")
        self.assertEqual(config.int("food_max_results", default=5), 2)

    def test_policy_names_work_as_fallback(self) -> None:
        config = PluginConfig({})
        with policy_overlay.policy_scope(_overlay()):
            self.assertEqual(config.raw("max_results"), 9)

    def test_falsy_override_wins(self) -> None:
        config = PluginConfig({"food_submit_enabled": True})
        with policy_overlay.policy_scope(_overlay(food_submit_enabled=False)):
            self.assertFalse(config.bool("food_submit_enabled"))
        self.assertTrue(config.bool("food_submit_enabled"))


if __name__ == "__main__":
    unittest.main()
