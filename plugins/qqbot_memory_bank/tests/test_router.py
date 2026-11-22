import unittest

import memory_router as router


class RouterTests(unittest.TestCase):
    def test_parse_routes_keeps_allowed_routes_and_drops_ignore(self):
        raw = '''```json
        {"items": [
          {"route": "user_fact", "content": "用户喜欢无糖可乐", "confidence": 0.9},
          {"route": "group_memory", "content": "群里禁止广告", "confidence": 0.8},
          {"route": "ignore", "content": "你好", "confidence": 1.0},
          {"route": "not_a_route", "content": "坏数据", "confidence": 1.0}
        ]}
        ```'''
        items = router.parse_routes(raw)
        self.assertEqual([item["route"] for item in items], ["user_fact", "group_memory"])
        self.assertAlmostEqual(items[0]["confidence"], 0.9)

    def test_parse_routes_clamps_confidence_and_dedupes(self):
        raw = '{"items":[{"route":"user_fact","content":"用户喜欢猫","confidence":9},{"route":"user_fact","content":"用户喜欢猫","confidence":0.5}]}'
        items = router.parse_routes(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["confidence"], 1.0)

    def test_heuristic_fallback_is_marked_low_confidence(self):
        items = router.heuristic_routes(["记住我喜欢吃火锅，不吃香菜。"])
        self.assertEqual(items[0]["route"], "user_fact")
        self.assertLess(items[0]["confidence"], 0.8)

    def test_auto_write_requires_route_and_confidence(self):
        self.assertTrue(router.should_auto_write("user_fact", 0.7, min_confidence=0.6))
        self.assertFalse(router.should_auto_write("user_fact", 0.5, min_confidence=0.6))
        self.assertFalse(router.should_auto_write("episode", 0.9, min_confidence=0.6))


if __name__ == "__main__":
    unittest.main()
