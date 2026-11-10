from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qqbot_memory_commands import cards, wrapup


class MemoryWrapupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = cards.CardStore(Path(self.temp.name) / "cards.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_preview_builds_summary_phase_and_open_topic(self):
        plan = wrapup.preview(
            conversation_id="qq_official:GroupMessage:g1",
            session_revision=7,
            scope="group:g1",
            messages=[
                {"role": "user", "content": "明天去长春吃火锅吗？"},
                {"role": "assistant", "content": "可以，先看看天气。"},
                {"role": "user", "content": "那家店怎么走？"},
            ],
        )
        self.assertTrue(plan["cards"])
        kinds = {item["kind"] for item in plan["cards"]}
        self.assertIn("episode", kinds)
        self.assertIn("open_topic", kinds)
        self.assertTrue(plan["phase_summary"])
        self.assertEqual(plan["open_topic"], "那家店怎么走？")

    def test_apply_is_idempotent_and_archives_nothing(self):
        plan = wrapup.preview(
            conversation_id="c1",
            session_revision=3,
            scope="conversation:c1",
            messages=[{"role": "user", "content": "记住我喜欢吃火锅"}],
        )
        first = wrapup.apply_plan(self.store, plan)
        second = wrapup.apply_plan(self.store, plan)
        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertEqual(second["reason"], "already_applied")
        self.assertEqual(self.store.stats(scope="conversation:c1").get("active"), len(first["cards"]))

    def test_purpose_is_opt_in(self):
        plan = wrapup.preview(
            conversation_id="c2",
            session_revision=1,
            scope="user:u1",
            messages=[{"role": "user", "content": "怎么申请退款？"}],
            purpose_enabled=True,
        )
        self.assertTrue(any(item["kind"] == "purpose" for item in plan["cards"]))
        plan_off = wrapup.preview(
            conversation_id="c3",
            session_revision=1,
            scope="user:u1",
            messages=[{"role": "user", "content": "怎么申请退款？"}],
        )
        self.assertFalse(any(item["kind"] == "purpose" for item in plan_off["cards"]))

    def test_empty_messages_are_safe(self):
        plan = wrapup.preview(
            conversation_id="c4",
            session_revision=1,
            scope="group:g1",
            messages=[],
        )
        self.assertEqual(plan["cards"], [])
        result = wrapup.apply_plan(self.store, plan)
        self.assertTrue(result["applied"])
        self.assertEqual(result["cards"], [])


if __name__ == "__main__":
    unittest.main()
