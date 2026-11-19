import json
import tempfile
import unittest
from pathlib import Path

import expression_state as es


class ExpressionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_700_000_000.0
        self.store = es.ExpressionStore(Path(self.tmp.name) / "e.db", clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_single_event_is_capped(self):
        out = self.store.observe("group:onebot:1", "谢谢谢谢谢谢！你真好！")
        self.assertLessEqual(abs(out["valence"]), es.MAX_EVENT_DELTA)
        self.assertLessEqual(out["arousal"], es.MAX_EVENT_DELTA)

    def test_emotion_decays_half_life(self):
        self.store.observe("group:onebot:1", "太好了！")
        self.now += es.HALF_LIFE_SECONDS
        snap = self.store.snapshot("group:onebot:1")
        self.assertLess(abs(snap["valence"]), 0.15)

    def test_style_not_ready_before_threshold(self):
        for i in range(5):
            self.store.observe("group:onebot:1", f"你好呀 {i}")
        snap = self.store.snapshot("group:onebot:1")
        self.assertEqual(snap["style_samples"], 5)
        self.assertFalse(snap["style_ready"])
        self.assertTrue(self.store.context("group:onebot:1").startswith("表达状态（内部参考）：情绪=平稳/"))

    def test_style_ready_at_threshold_and_pin(self):
        for i in range(30):
            self.store.observe("group:onebot:1", "谢谢，你说得真棒！")
        snap = self.store.snapshot("group:onebot:1")
        self.assertTrue(snap["style_ready"])
        self.store.pin("group:onebot:1", "warmth", 0.9)
        self.assertGreater(self.store.snapshot("group:onebot:1")["style"]["warmth"], 0.8)

    def test_optout_suppresses_context(self):
        self.store.observe("group:onebot:1", "开心")
        self.store.set_optout("group:onebot:1", True)
        self.assertEqual(self.store.context("group:onebot:1"), "")
        self.assertTrue(self.store.snapshot("group:onebot:1")["optout"])

    def test_export_and_reset(self):
        self.store.observe("group:onebot:1", "谢谢")
        data = self.store.export()
        self.assertIn("group:onebot:1", data["scopes"])
        self.store.reset("group:onebot:1")
        self.assertFalse(self.store.snapshot("group:onebot:1")["style_samples"])

    def test_style_vector_bounds(self):
        vector = es.style_vector("谢谢您！！！真的非常非常感谢，您太好了 😊")
        self.assertTrue(all(0 <= value <= 1 for value in vector.values()))
        self.assertEqual(set(vector), set(es.DIMENSIONS))


if __name__ == "__main__":
    unittest.main()
