from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qqbot_memory_commands import cards


class MemoryCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = cards.CardStore(Path(self.temp.name) / "memory_cards.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_create_and_events(self) -> None:
        card = self.store.create(
            scope="group:g1",
            content="用户喜欢长春的火锅。",
            kind="preference",
            confidence=0.8,
            importance=0.7,
            source_record_ids=["mem-1", "mem-1", "mem-2"],
        )
        self.assertEqual(card["status"], "active")
        self.assertEqual(card["source_record_ids"], ["mem-1", "mem-2"])
        self.assertAlmostEqual(card["clarity"], 0.8)
        actions = [event["action"] for event in self.store.events(card["id"])]
        self.assertEqual(actions, ["create"])

    def test_clarity_decays_by_half_life(self) -> None:
        card = self.store.create(
            scope="group:g1",
            content="事实",
            kind="fact",
            confidence=1.0,
            now=0.0,
        )
        one_half_life = 120 * 86400
        self.assertAlmostEqual(cards.effective_clarity(card, now=one_half_life), 0.5, places=6)
        self.assertAlmostEqual(cards.effective_clarity(card, now=2 * one_half_life), 0.25, places=6)

    def test_archive_decayed_and_stats(self) -> None:
        old = self.store.create(
            scope="group:g1",
            content="旧事实",
            kind="open_topic",
            confidence=0.06,
            now=0.0,
        )
        fresh = self.store.create(
            scope="group:g1",
            content="新事实",
            kind="fact",
            confidence=0.9,
            now=0.0,
        )
        archived = self.store.archive_decayed(scope="group:g1", now=60 * 86400)
        self.assertEqual([card["id"] for card in archived], [old["id"]])
        self.assertEqual(self.store.get(old["id"])["status"], "archived")
        self.assertEqual(self.store.get(fresh["id"])["status"], "active")
        self.assertEqual(self.store.stats(scope="group:g1"), {"active": 1, "archived": 1})
        actions = [event["action"] for event in self.store.events(old["id"])]
        self.assertEqual(actions, ["create", "archive"])

    def test_invalid_card_rejected(self) -> None:
        with self.assertRaises(cards.CardError):
            self.store.create(scope="group:g1", content="x", kind="unknown")
        with self.assertRaises(cards.CardError):
            self.store.create(scope="group:g1", content="x", kind="fact", subtype="session_summary")

    def test_touch_updates_last_used(self) -> None:
        card = self.store.create(scope="group:g1", content="事实", now=0.0)
        touched = self.store.touch(card["id"], now=10.0)
        self.assertEqual(touched["last_used_at"], 10.0)
        self.assertEqual(touched["revision"], 1)
        self.assertEqual([e["action"] for e in self.store.events(card["id"])], ["create", "touch"])


if __name__ == "__main__":
    unittest.main()
