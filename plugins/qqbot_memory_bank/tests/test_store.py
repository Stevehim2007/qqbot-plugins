from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = store.KnowledgeStore(Path(self.temp.name) / "knowledge.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_add_search_and_dedupe(self):
        first = self.store.add(scope="group:onebot:1", content="用户喜欢喝无糖可乐", tags="饮食")
        second = self.store.add(scope="group:onebot:1", content="用户喜欢喝无糖可乐")
        self.assertTrue(first["ok"])
        self.assertTrue(second["duplicate"])
        rows = self.store.search(scope="group:onebot:1", query="可乐")
        self.assertEqual(len(rows), 1)
        self.assertIn("可乐", rows[0]["content"])

    def test_noise_is_rejected(self):
        result = self.store.add(scope="group:onebot:1", content="[[NO_REPLY]] https://x.test/a.png")
        self.assertFalse(result["ok"])

    def test_scope_and_persona_isolation(self):
        self.store.add(scope="group:onebot:1", content="用户喜欢猫", persona_id="p1")
        self.assertEqual(self.store.search(scope="group:onebot:1", query="猫", persona_id="p2"), [])
        self.assertEqual(len(self.store.search(scope="group:onebot:1", query="猫", persona_id="p1")), 1)

    def test_forget_and_stats(self):
        self.store.add(scope="group:onebot:1", content="用户住在长春", actor_hash="a", entry_kind="ai")
        rows = self.store.search(scope="group:onebot:1", query="长春", actor_hash="a")
        self.assertTrue(self.store.forget(scope="group:onebot:1", entry_id=rows[0]["id"], actor_hash="a"))
        self.assertEqual(self.store.stats(scope="group:onebot:1", actor_hash="a")["total"], 0)

    def test_actor_isolation_and_shared_curated(self):
        self.store.add(scope="group:onebot:1", content="用户喜欢猫", actor_hash="a", entry_kind="ai")
        self.store.add(scope="group:onebot:1", content="用户喜欢猫", actor_hash="b", entry_kind="ai")
        self.store.add(scope="group:onebot:1", content="群规：禁止广告", entry_kind="curated")
        a_rows = self.store.search(scope="group:onebot:1", query="猫", actor_hash="a")
        b_rows = self.store.search(scope="group:onebot:1", query="猫", actor_hash="b")
        self.assertEqual(len(a_rows), 1)
        self.assertEqual(len(b_rows), 1)
        self.assertNotEqual(a_rows[0]["id"], b_rows[0]["id"])
        shared = self.store.search(scope="group:onebot:1", query="广告", actor_hash="a")
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["entry_kind"], "curated")
        self.assertFalse(self.store.forget(scope="group:onebot:1", entry_id=shared[0]["id"], actor_hash="a"))

    def test_subject_normalisation(self):
        self.assertEqual(store.normalise_fact_subject("讨厌下雨天"), "用户讨厌下雨天")
        self.assertEqual(store.normalise_fact_subject("用户喜欢猫"), "用户喜欢猫")

    def test_heuristic_fallback(self):
        facts = store.heuristic_facts("今天天气不错。记住我喜欢吃火锅，不吃香菜。")
        self.assertIn("用户喜欢吃火锅，不吃香菜", facts)
        self.assertNotIn("今天天气不错", facts)


if __name__ == "__main__":
    unittest.main()
