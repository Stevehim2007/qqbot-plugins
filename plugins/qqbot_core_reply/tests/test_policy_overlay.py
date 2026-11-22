"""config.overlay.v1 wiring for qqbot_core_reply."""

from __future__ import annotations

import asyncio
import unittest

import policy_overlay
from config import ConfigReader, RuntimeConfig


def _overlay(**values):
    return policy_overlay.build(
        "qqbot_core_reply",
        {
            "params": {"participation.reply_probability": 0.85},
            "config_values": {"participation.group_probability": 0.85, **values},
            "sources": ["group"],
            "revision": 7,
        },
        scope="group",
        scope_id="123",
    )


class OverlayReadTest(unittest.TestCase):
    def test_config_reader_prefers_overlay(self) -> None:
        reader = ConfigReader({"participation": {"group_probability": 0.1}})
        with policy_overlay.policy_scope(_overlay()):
            self.assertAlmostEqual(reader.float("participation.group_probability", 0, 1), 0.85)
        self.assertAlmostEqual(reader.float("participation.group_probability", 0, 1), 0.1)

    def test_policy_name_fallback_also_matches(self) -> None:
        reader = ConfigReader({})
        with policy_overlay.policy_scope(_overlay()):
            self.assertAlmostEqual(reader.float("participation.reply_probability", 0, 1), 0.85)

    def test_falsy_values_are_real_overrides(self) -> None:
        reader = ConfigReader({"enabled": True, "participation.group_mention_only": True})
        with policy_overlay.policy_scope(_overlay(enabled=False)):
            self.assertFalse(reader.bool("enabled", True))
        self.assertTrue(reader.bool("enabled", True))

    def test_runtime_overrides_beat_overlay(self) -> None:
        runtime = RuntimeConfig({"participation": {"group_probability": 0.1}})
        with policy_overlay.policy_scope(_overlay()):
            self.assertAlmostEqual(runtime.float("participation.group_probability", 0, 1), 0.85)
            runtime.overrides["participation.group_probability"] = 0.2
            self.assertAlmostEqual(runtime.float("participation.group_probability", 0, 1), 0.2)

    def test_child_task_does_not_inherit(self) -> None:
        reader = ConfigReader({"enabled": True})

        async def main() -> tuple[bool, bool]:
            async def child() -> bool:
                return reader.bool("enabled", True)

            with policy_overlay.policy_scope(_overlay(enabled=False)):
                parent = reader.bool("enabled", True)
                child_value = await asyncio.create_task(child())
            return parent, child_value

        parent, child_value = asyncio.run(main())
        self.assertFalse(parent)
        self.assertTrue(child_value)

    def test_detached_scope_keeps_overlay(self) -> None:
        reader = ConfigReader({"enabled": True})

        async def main() -> bool:
            async def child() -> bool:
                with policy_overlay.detached():
                    return reader.bool("enabled", True)
                return False

            with policy_overlay.policy_scope(_overlay(enabled=False)):
                return await asyncio.create_task(child())

        self.assertFalse(asyncio.run(main()))


if __name__ == "__main__":
    unittest.main()
