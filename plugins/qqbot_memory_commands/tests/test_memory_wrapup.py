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

    def test_preview_skips_casual_chat_and_keeps_open_topic(self):
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
        kinds = {item["kind"] for item in plan["cards"]}
        self.assertNotIn("episode", kinds)
        self.assertIn("open_topic", kinds)
        self.assertEqual(plan["phase_summary"], "")
        self.assertEqual(plan["open_topic"], "那家店怎么走？")

    def test_preview_prefers_ai_episode_hint(self):
        plan = wrapup.preview(
            conversation_id="c-ai",
            session_revision=1,
            scope="group:g-ai",
            messages=[{"role": "user", "content": "记住我喜欢吃火锅。"}],
            episode_hint="用户提到自己喜欢吃火锅，聊到下次一起去吃，尚未约定时间。",
            episode_enabled=True,
        )
        episodes = [item for item in plan["cards"] if item["kind"] == "episode"]
        self.assertEqual(len(episodes), 1)
        self.assertIn("喜欢吃火锅", episodes[0]["content"])
        self.assertNotEqual(episodes[0]["content"], "记住我喜欢吃火锅。")

    def test_preview_keeps_only_durable_user_facts(self):
        plan = wrapup.preview(
            conversation_id="qq_official:GroupMessage:g2",
            session_revision=1,
            scope="group:g2",
            messages=[
                {"role": "user", "content": "今天食堂的饭好难吃，随便说说。"},
                {"role": "assistant", "content": "哈哈，确实。"},
                {"role": "user", "content": "记住我喜欢吃火锅，不吃香菜。"},
            ],
        )
        episodes = [item for item in plan["cards"] if item["kind"] == "episode"]
        self.assertTrue(episodes)
        self.assertIn("火锅", episodes[0]["content"])
        self.assertNotIn("助手", episodes[0]["content"])
        self.assertNotIn("食堂", episodes[0]["content"])

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


    def test_transport_noise_is_not_memorized(self):
        summary = wrapup.summarize_messages([
            {"role": "user", "content": "[[NO_REPLY]]"},
            {"role": "assistant", "content": "![img](https://example.test/a.png)"},
            {"role": "user", "content": "记住我喜欢吃火锅"},
        ])
        self.assertNotIn("NO_REPLY", summary)
        self.assertNotIn("mqqapi", summary)
        self.assertNotIn("example.test", summary)
        self.assertIn("火锅", summary)

    def test_apply_skips_empty_cleaned_episode(self):
        plan = {
            "conversation_id": "c-noise",
            "session_revision": 1,
            "scope": "conversation:c-noise",
            "cards": [
                {"kind": "episode", "content": "[[NO_REPLY]]"},
                {"kind": "episode", "content": "![x](https://example.test/x.png)"},
            ],
        }
        result = wrapup.apply_plan(self.store, plan)
        self.assertTrue(result["applied"])
        self.assertEqual(result["wrapup"]["result"]["card_ids"], [])

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
