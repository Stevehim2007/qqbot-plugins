import unittest

from sticker_intent import (
    detect_auto_collect_text,
    detect_collect_request,
    detect_sticker_request,
    resolve_sticker_target,
)
from sticker_reclassify import (
    build_local_semantics,
    classify_target_local,
    detect_reclassify_request,
    looks_like_incomplete_reclassify_request,
    parse_reclassify_judgement,
)


class StickerIntentTests(unittest.TestCase):
    def test_emotion_request_maps_chinese_keyword(self):
        result = detect_sticker_request(
            "发个开心的表情",
            available_emotions={"happy", "sad"},
            available_categories={"日常"},
        )
        self.assertEqual(result["kind"], "emotion")
        self.assertEqual(result["value"], "happy")

    def test_exact_category_takes_precedence(self):
        result = detect_sticker_request(
            "发个开心的表情",
            available_emotions={"happy"},
            available_categories={"开心"},
        )
        self.assertEqual(result["kind"], "category")
        self.assertEqual(result["value"], "开心")

    def test_generic_sticker_request(self):
        result = detect_sticker_request(
            "给我发个表情",
            available_emotions={"happy"},
            available_categories=set(),
        )
        self.assertEqual(result, {"kind": "generic", "target": ""})

    def test_unknown_target_is_not_found(self):
        result = detect_sticker_request(
            "发张好看的表情",
            available_emotions={"happy"},
            available_categories=set(),
        )
        self.assertEqual(result["kind"], "not_found")
        self.assertEqual(result["target"], "好看")

    def test_non_sticker_sentence_ignored(self):
        self.assertIsNone(
            detect_sticker_request(
                "我发了一个表情",
                available_emotions={"happy"},
                available_categories=set(),
            )
        )

    def test_snark_maps_to_available_emotion(self):
        result = resolve_sticker_target(
            "阴阳怪气",
            available_emotions={"angry", "cool"},
            available_categories=set(),
        )
        self.assertEqual(result["kind"], "emotion")
        self.assertEqual(result["value"], "angry")

    def test_collect_bare_request(self):
        self.assertEqual(
            detect_collect_request("收藏这个表情"),
            {"target": "", "category": ""},
        )

    def test_collect_with_category(self):
        self.assertEqual(
            detect_collect_request("把这个表情收藏到开心"),
            {"target": "开心", "category": "开心"},
        )

    def test_collect_polite_and_save_verb(self):
        self.assertEqual(
            detect_collect_request("帮我保存到日常"),
            {"target": "日常", "category": "日常"},
        )

    def test_collect_comma_separated_categories(self):
        self.assertEqual(
            detect_collect_request("收藏到开心,日常")["category"],
            "开心,日常",
        )

    def test_ordinary_chat_is_not_collect_intent(self):
        for text in ("我收藏了很多图片", "这只很可爱收藏一下", "收藏夹在哪里"):
            self.assertIsNone(detect_collect_request(text), text)

    def test_path_like_collect_target_rejected(self):
        self.assertIsNone(detect_collect_request("收藏到 ../etc"))

    def test_bare_auto_collect_text(self):
        self.assertTrue(detect_auto_collect_text("收藏这个表情"))
        self.assertTrue(detect_auto_collect_text("保存一下"))
        self.assertFalse(detect_auto_collect_text("把收藏的东西发出来"))

    def test_reclassify_set_forms(self):
        for text in (
            "把这个表情改成开心",
            "这个表情属于开心",
            "刚才那个表情放到开心",
        ):
            result = detect_reclassify_request(text)
            self.assertEqual(result["action"], "set", text)
            self.assertEqual(result["target"], "开心", text)

    def test_reclassify_add_form(self):
        result = detect_reclassify_request("把刚才那个表情加入分类 开心")
        self.assertEqual(result, {"target": "开心", "action": "add"})

    def test_reclassify_remove_form(self):
        result = detect_reclassify_request("把这个表情移出分类 开心")
        self.assertEqual(result, {"target": "开心", "action": "remove"})

    def test_incomplete_reclassify_detected(self):
        self.assertTrue(looks_like_incomplete_reclassify_request("把这个表情改成"))
        self.assertFalse(looks_like_incomplete_reclassify_request("把这个表情改成开心"))

    def test_local_semantics_emotion(self):
        result = build_local_semantics("开心")
        self.assertEqual(result["category"], "开心")
        self.assertIn("happy", result["tags"])
        self.assertEqual(result["method"], "local")

    def test_local_semantics_snark(self):
        result = build_local_semantics("嘲讽")
        self.assertEqual(result["tone"], "sarcastic")
        self.assertIn("angry", result["tags"])
        self.assertIn("讽刺", result["meaning"])

    def test_local_semantics_multi_category(self):
        result = build_local_semantics("开心/日常")
        self.assertEqual(result["categories"], ["开心", "日常"])
        self.assertIn("开心", result["tags"])
        self.assertIn("日常", result["tags"])

    def test_classify_target_local(self):
        self.assertEqual(classify_target_local("开心"), "happy")
        self.assertEqual(classify_target_local("自定义"), "自定义")
        self.assertIsNone(classify_target_local("bad/target"))

    def test_parse_judgement_fenced_json(self):
        result = parse_reclassify_judgement(
            '```json\n{"category":"开心","tags":["happy"],"meaning":"笑","tone":"expressive","confidence":1.5}\n```'
        )
        self.assertEqual(result["category"], "开心")
        self.assertEqual(result["confidence"], 1.0)

    def test_parse_judgement_invalid(self):
        self.assertIsNone(parse_reclassify_judgement("not json"))
        self.assertIsNone(parse_reclassify_judgement('{"tags":["x"]}'))


if __name__ == "__main__":
    unittest.main()
