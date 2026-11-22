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

    def test_digest_keeps_only_the_newest_entries(self):
        for index in range(4):
            result = self.store.add_digest(
                scope="group:onebot:1",
                persona_id="p1",
                content=f"第{index}次群聊摘要",
                keep=2,
            )
            self.assertTrue(result["ok"], result)
        rows = self.store.latest_digests(scope="group:onebot:1", persona_id="p1", limit=5)
        self.assertEqual(len(rows), 2)
        self.assertIn("第3次", rows[0]["content"])

    def test_digest_is_scoped_per_group_and_persona(self):
        self.store.add_digest(scope="group:onebot:1", persona_id="p1", content="一群在聊比赛")
        self.assertEqual(
            self.store.latest_digests(scope="group:onebot:2", persona_id="p1"), []
        )
        self.assertEqual(
            self.store.latest_digests(scope="group:onebot:1", persona_id="p2"), []
        )
        self.assertEqual(len(self.store.latest_digests(scope="group:onebot:1", persona_id="p1")), 1)

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

    def test_imported_docs_are_separate_from_ai_memory(self):
        self.store.add(scope="group:onebot:1", content="用户喜欢猫", actor_hash="a", entry_kind="ai")
        doc = self.store.add_doc(scope="group:onebot:1", title="宿舍", content="宿舍门禁 23:00", tags="规则")
        self.assertTrue(doc["ok"])
        self.assertEqual(self.store.search(scope="group:onebot:1", query="猫", actor_hash="a")[0]["content"], "用户喜欢猫")
        self.assertEqual(self.store.search(scope="group:onebot:1", query="门禁", actor_hash="a"), [])
        docs = self.store.search_docs(scope="group:onebot:1", query="门禁")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["title"], "宿舍")

    def test_doc_dedupe_list_and_delete(self):
        first = self.store.add_doc(scope="global", content="群规：禁止广告")
        second = self.store.add_doc(scope="global", content="群规：禁止广告")
        self.assertTrue(first["ok"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.store.list_docs(scope="global")), 1)
        self.assertTrue(self.store.delete_doc(entry_id=first["id"]))
        self.assertEqual(self.store.list_docs(scope="global"), [])

    def test_subject_normalisation(self):
        self.assertEqual(store.normalise_fact_subject("讨厌下雨天"), "用户讨厌下雨天")
        self.assertEqual(store.normalise_fact_subject("用户喜欢猫"), "用户喜欢猫")

    def test_memory_layer_crud_and_isolation(self):
        first = self.store.add_layer(
            layer="persona", owner_id="p1", title="身份", content="衣衣是电子羊的群人格", tags="设定"
        )
        duplicate = self.store.add_layer(
            layer="persona", owner_id="p1", content="衣衣是电子羊的群人格", priority=5
        )
        self.assertTrue(first["ok"])
        self.assertTrue(duplicate["duplicate"])
        rows = self.store.list_layers(layer="persona", owner_id="p1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["priority"], 5)
        self.assertEqual(self.store.list_layers(layer="persona", owner_id="p2"), [])
        self.assertTrue(
            self.store.add_layer(
                layer="group", owner_id="group:onebot:1", content="群规：禁止广告"
            )["ok"]
        )
        self.assertEqual(len(self.store.list_layers(layer="group", owner_id="group:onebot:1")), 1)
        self.assertTrue(
            self.store.delete_layer(layer="persona", owner_id="p1", layer_id=first["id"])
        )
        self.assertEqual(self.store.list_layers(layer="persona", owner_id="p1"), [])

    def test_memory_layers_for_prompt_order(self):
        self.store.add_layer(layer="persona", owner_id="p1", content="人格记忆", priority=0)
        self.store.add_layer(layer="group", owner_id="group:onebot:1", content="群记忆", priority=9)
        rows = self.store.layers_for_prompt(persona_id="p1", scope="group:onebot:1")
        self.assertEqual([r["layer"] for r in rows], ["persona", "group"])
        self.assertEqual(rows[1]["content"], "群记忆")

    def test_heuristic_fallback(self):
        facts = store.heuristic_facts("今天天气不错。记住我喜欢吃火锅，不吃香菜。")
        self.assertIn("用户喜欢吃火锅，不吃香菜", facts)
        self.assertNotIn("今天天气不错", facts)


    def test_routing_inbox_is_isolated_and_deduped(self):
        store = self.store
        first = store.add_inbox(
            scope="group:onebot:1", persona_id="p1", actor_hash="a1",
            actor_label="Alice", route="group_memory", content="群里禁止广告",
            confidence=0.8, reason="test",
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first["duplicate"])
        again = store.add_inbox(
            scope="group:onebot:1", persona_id="p1", actor_hash="a1",
            actor_label="Alice", route="group_memory", content="群里禁止广告",
            confidence=0.9, reason="test",
        )
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(store.list_inbox(scope="group:onebot:1")), 1)
        self.assertEqual(store.list_inbox(scope="group:onebot:2"), [])
        self.assertTrue(store.resolve_inbox(item_id=first["id"], status="dismissed"))
        self.assertEqual(store.list_inbox(scope="group:onebot:1"), [])


if __name__ == "__main__":
    unittest.main()
