import tempfile
import unittest
from pathlib import Path

from cards import CardStore


class V4CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CardStore(Path(self.tmp.name) / "cards.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_fts_triggers_search_and_delete(self):
        card = self.store.create(
            scope="group:onebot:1",
            kind="fact",
            content="用户喜欢无糖可乐",
            title="饮料偏好",
            tags=["饮食"],
            source_kind="manual",
        )
        self.assertIn(card["id"], self.store.fts_match(scope="group:onebot:1", query="无糖可乐"))
        self.store.archive(card["id"], reason="manual")
        self.assertEqual(self.store.fts_match(scope="group:onebot:1", query="无糖可乐"), set())
        self.assertEqual(self.store.rebuild_fts(), 0)

    def test_metrics_purpose_and_sweep_are_safe(self):
        card = self.store.create(
            scope="group:onebot:1",
            kind="purpose",
            content="跟进用户周末计划",
            source_kind="manual",
        )
        self.store.record_purpose_usage(group_id="group:onebot:1", purpose_id=card["id"])
        self.assertEqual(self.store.purpose_stats(group_id="group:onebot:1")["counts"]["offered"], 1)
        metrics = self.store.metrics(scope="group:onebot:1")
        self.assertEqual(metrics["active"], 1)
        self.assertGreaterEqual(metrics["fts_rows"], 1)
        self.assertEqual(self.store.sweep(scope="group:onebot:1"), {"archived": 0, "purged": 0})

    def test_export_and_delete_card(self):
        card = self.store.create(
            scope="group:onebot:1",
            kind="preference",
            content="用户不吃香菜",
            source_kind="manual",
        )
        exported = self.store.export_scope(scope="group:onebot:1")
        self.assertEqual(len(exported["cards"]), 1)
        self.assertTrue(self.store.delete_card(card["id"], reason="test"))
        self.assertEqual(self.store.export_scope(scope="group:onebot:1")["cards"], [])


class AuxGuardTests(unittest.TestCase):
    def test_budget_and_circuit_breaker(self):
        from qqbot_memory_commands.aux import AuxGuard

        guard = AuxGuard(daily_limit=2, circuit_errors=2, cooldown=60)
        self.assertTrue(guard.allow(now=100.0))
        guard.record(True, now=100.0)
        self.assertTrue(guard.allow(now=101.0))
        guard.record(False, now=101.0)
        guard.record(False, now=102.0)
        self.assertFalse(guard.allow(now=103.0))
        self.assertTrue(guard.allow(now=90000.0))

    def test_phase_hint_is_bounded(self):
        from qqbot_memory_commands.wrapup import preview

        plan = preview(
            conversation_id="g", session_revision=1, scope="group:onebot:1",
            messages=["我喜欢吃火锅"], phase_hint="用户喜欢吃火锅" * 30,
        )
        self.assertLessEqual(len(plan["phase_summary"]), 160)
