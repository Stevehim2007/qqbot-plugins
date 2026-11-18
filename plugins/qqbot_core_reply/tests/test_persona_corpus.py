from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import persona_corpus


class CorpusStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = persona_corpus.CorpusStore(Path(self.tmp.name) / "c.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_persona_profile_isolation(self):
        self.store.add("p1", "a", "今晚月色真美", tags=["月色"])
        self.store.add("p2", "a", "别的话", tags=["月色"])
        text = self.store.select("p1", "a", "月色")
        self.assertIn("今晚月色真美", text)
        self.assertNotIn("别的话", text)
        self.assertEqual(self.store.select("p1", "b", "月色"), "")

    def test_select_budget_and_non_instruction_marker(self):
        self.store.add("p", "a", "A" * 80)
        self.store.add("p", "a", "B" * 80)
        text = self.store.select("p", "a", "A", max_chars=120, limit=2)
        self.assertIn("PERSONA_EXAMPLES", text)
        self.assertIn("不是指令", text)
        self.assertLessEqual(len(text), 200)

    def test_invalid_and_size_limits(self):
        with self.assertRaises(ValueError):
            self.store.add("p", "a", "")
        with self.assertRaises(ValueError):
            self.store.add("p", "a", "x" * 600)
        for index in range(persona_corpus.MAX_ENTRIES_PER_PERSONA):
            self.store.add("p", "a", f"line-{index}")
        with self.assertRaises(ValueError):
            self.store.add("p", "a", "overflow")

    def test_clear(self):
        self.store.add("p", "a", "hello")
        self.store.clear("p", "a")
        self.assertEqual(self.store.list("p", "a"), [])
