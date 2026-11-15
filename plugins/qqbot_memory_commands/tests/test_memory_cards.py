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

    def test_v4_source_key_is_idempotent(self) -> None:
        first = self.store.create(scope="group:g1", content="喜欢火锅", kind="preference")
        second = self.store.create(scope="group:g1", content="喜欢火锅", kind="preference")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.count_active("group:g1"), 1)

    def test_v4_tags_relationship_and_pinned(self) -> None:
        card = self.store.create(
            scope="group:g1",
            content="小明和小红是室友",
            kind="fact",
            tags=["室友", "关系", "室友"],
            relationship_target="u2",
            relationship_kind="roommate",
            pinned=True,
        )
        self.assertEqual(card["tags"], ["室友", "关系"])
        self.assertEqual(card["relationship_kind"], "roommate")
        self.assertEqual(card["pinned"], 1)

    def test_v4_session_state_and_audit(self) -> None:
        state = self.store.put_session_state(
            conversation_id="c1",
            session_revision=3,
            phase_summary="在聊长春火锅",
            open_topics=["周末去吃火锅", "第二件事"],
            purpose={"text": "找店", "confidence": 0.7},
            last_wrapup_revision=2,
        )
        self.assertEqual(state["phase_summary"], "在聊长春火锅")
        self.assertEqual(len(state["open_topics"]), 2)
        loaded = self.store.get_session_state("c1")
        self.assertEqual(loaded["session_revision"], 3)
        self.store.audit(action="update", target_type="card", target_id="x", reason="test")
        entries = self.store.audit_entries(limit=5)
        self.assertEqual(entries[0]["action"], "update")

    def test_v4_select_query_and_persona(self) -> None:
        self.store.create(scope="group:g1", content="用户喜欢火锅", kind="preference", persona_id="p1")
        self.store.create(scope="group:g1", content="今天下雨", kind="episode", persona_id="p2")
        selected = cards.select_for_prompt(self.store, scope="group:g1", query="火锅", persona_id="p1")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["content"], "用户喜欢火锅")

    def test_touch_updates_last_used(self) -> None:
        card = self.store.create(scope="group:g1", content="事实", now=0.0)
        touched = self.store.touch(card["id"], now=10.0)
        self.assertEqual(touched["last_used_at"], 10.0)
        self.assertEqual(touched["revision"], 0)
        self.assertEqual(touched["use_count"], 1)
        self.assertEqual([e["action"] for e in self.store.events(card["id"])], ["create"])


if __name__ == "__main__":
    unittest.main()
