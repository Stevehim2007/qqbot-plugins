from __future__ import annotations

import datetime
import unittest

from support import food_engine as f


class UrlAndParsingTests(unittest.TestCase):
    def test_build_url_fixed_host_and_encoded_query(self):
        url = f.build_food_url(f.FOOD_RESTAURANTS_PATH, {"q": "火锅&店", "limit": 3})
        self.assertTrue(url.startswith("https://food.dlsz.top/api/restaurants?"))
        self.assertIn("%E7%81%AB%E9%94%85%26%E5%BA%97", url)
        self.assertTrue(f.validate_food_final_url(url))

    def test_build_url_rejects_unknown_path(self):
        with self.assertRaises(f.FoodAPIError):
            f.build_food_url("https://evil.example/", {"q": "x"})

    def test_final_url_validation_rejects_http_userinfo_and_other_paths(self):
        self.assertFalse(f.validate_food_final_url("http://food.dlsz.top/api/restaurants"))
        self.assertFalse(
            f.validate_food_final_url("https://user:pw@food.dlsz.top/api/restaurants")
        )
        self.assertFalse(
            f.validate_food_final_url("https://food.dlsz.top/api/admin")
        )
        self.assertFalse(
            f.validate_food_final_url("https://food.dlsz.top:444/api/restaurants")
        )

    def test_sanitize_query_rejects_url_control_and_overlong(self):
        self.assertEqual(f.sanitize_food_query("  火锅  "), "火锅")
        self.assertIsNone(f.sanitize_food_query("https://evil.example"))
        self.assertIsNone(f.sanitize_food_query("a/b"))
        self.assertIsNone(f.sanitize_food_query("a\x00b"))
        self.assertIsNone(f.sanitize_food_query("x" * 51))
        self.assertEqual(f.sanitize_food_query("x" * 10, max_chars=20), "x" * 10)

    def test_extract_items_handles_wrapped_lists(self):
        self.assertEqual(f.extract_food_items([{"id": 1}, "x"]), [{"id": 1}])
        self.assertEqual(
            f.extract_food_items({"data": [{"id": 2}]}), [{"id": 2}]
        )
        self.assertEqual(f.extract_food_items({"nope": []}), [])

    def test_decode_payload_non_json_is_safe_error(self):
        with self.assertRaises(f.FoodAPIError):
            f.decode_food_payload(b"<html>")

    def test_select_location_exact_then_partial(self):
        items = [
            {"id": 1, "name": "长春市", "path": "中国/吉林省/长春市"},
            {"id": 2, "name": "朝阳区", "path": "中国/吉林省/长春市/朝阳区"},
        ]
        self.assertEqual(f.select_food_location(items, "朝阳区")["id"], 2)
        self.assertEqual(f.select_food_location(items, "长春")["id"], 1)
        self.assertIsNone(f.select_food_location(items, "不存在"))

    def test_likely_location_token(self):
        self.assertTrue(f.likely_location_token("南关区"))
        self.assertTrue(f.likely_location_token("长春市"))
        self.assertFalse(f.likely_location_token("火锅"))
        self.assertFalse(f.likely_location_token(""))

    def test_format_results_bounded_and_safe(self):
        text = f.format_food_results(
            [
                {
                    "name": "A" * 500,
                    "location_name": "B" * 300,
                    "tags": ["C" * 100, None, "火锅"],
                    "taste_avg": 4.5,
                    "value_avg": 3.5,
                    "total_avg": 8,
                    "review_count": 12,
                    "latest_review_date": "2026-01-02",
                }
            ],
            keyword="店",
            max_results=1,
            max_chars=260,
        )
        self.assertLessEqual(len(text), 260)
        self.assertIn("火锅", text)
        self.assertIn("均分 8", text)
        self.assertNotIn("\x00", text)


class ScoreAndSubmissionValidationTests(unittest.TestCase):
    def test_parse_score(self):
        self.assertEqual(f.parse_food_score("4.5"), 4.5)
        self.assertEqual(f.parse_food_score("４．５分"), 4.5)
        self.assertEqual(f.parse_food_score("0.1"), 0.1)
        self.assertEqual(f.parse_food_score("5"), 5.0)
        for bad in ("", "0", "5.1", "abc", "nan", "inf"):
            self.assertIsNone(f.parse_food_score(bad), bad)

    def test_split_tags_dedup_and_limit(self):
        self.assertEqual(f.split_food_tags("火锅,烤肉、火锅;夜宵", max_count=3), ["火锅", "烤肉", "夜宵"])
        self.assertEqual(f.split_food_tags(["a", "a", "b"], max_count=5), ["a", "b"])
        self.assertEqual(f.split_food_tags("https://evil,火锅"), [])

    def test_validate_submission_missing_fields_and_tags(self):
        result = f.validate_food_submission(
            {"restaurant": "店", "location": "长春市", "taste": "4", "value": "3"}
        )
        self.assertEqual(result["missing"], ["comment"])
        self.assertNotIn("comment", result["fields"])
        result = f.validate_food_submission(
            {
                "restaurant": "店",
                "location": "长春市",
                "taste": "4",
                "value": "3",
                "comment": "好吃",
                "tags": "火锅,夜宵",
            }
        )
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["fields"]["tags"], ["火锅", "夜宵"])

    def test_validate_submission_invalid_score(self):
        result = f.validate_food_submission(
            {
                "restaurant": "店",
                "location": "长春市",
                "taste": "9",
                "value": "0",
                "comment": "x",
            }
        )
        self.assertIn("taste", result["invalid"])
        self.assertIn("value", result["invalid"])

    def test_confirmation_regex_is_bare_only(self):
        for text in ("确认", "提交", "要", "添加", "好的", "可以", "嗯嗯", "确认！", "确认提交", "提交一下"):
            self.assertTrue(f.is_food_submit_confirmation(text), text)
        for text in ("确认一下这家店", "我不要", "普通聊天", "提交 https://x"):
            self.assertFalse(f.is_food_submit_confirmation(text), text)

    def test_add_intent_requires_verb_and_noun(self):
        self.assertTrue(f.detect_food_add_intent("帮我添加这个评价到 food"))
        self.assertTrue(f.detect_food_add_intent("记录一下这家餐厅"))
        self.assertFalse(f.detect_food_add_intent("这家火锅很好吃"))
        self.assertFalse(f.detect_food_add_intent("添加好友"))

    def test_date_and_payload_shape(self):
        payload = f.build_food_submit_payload(
            {
                "restaurant": "店",
                "location": "长春市",
                "taste": 4.5,
                "value": 3.5,
                "comment": "好吃",
                "tags": ["火锅", "夜宵"],
            },
            username="小明",
            password="123456",
            location_id=9,
        )
        self.assertEqual(payload["review_date"], datetime.date.today().isoformat())
        self.assertEqual(payload["tags"], ["火锅", "夜宵"])
        self.assertEqual(payload["password"], "123456")
        self.assertEqual(payload["username"], "小明")
        self.assertEqual(payload["location_id"], 9)
        self.assertNotIn("new_location_name", payload)

    def test_payload_requires_location(self):
        empty = f.build_food_submit_payload(
            {"restaurant": "店", "comment": "x", "taste": 4, "value": 3},
            username="u",
            password="p",
        )
        self.assertEqual(empty, {})
        bad_id = f.build_food_submit_payload(
            {"restaurant": "店", "comment": "x", "taste": 4, "value": 3},
            username="u",
            password="p",
            location_id="abc",
        )
        self.assertEqual(bad_id, {})

    def test_submit_url_validation(self):
        self.assertTrue(f.validate_food_submit_url(f.FOOD_SUBMIT_URL))
        self.assertFalse(f.validate_food_submit_url("http://food.dlsz.top/api/upload"))
        self.assertFalse(f.validate_food_submit_url("https://food.dlsz.top/api/upload?x=1"))
        self.assertFalse(f.validate_food_submit_url("https://evil.example/api/upload"))


class LocationTests(unittest.TestCase):
    def test_split_chinese_location_path(self):
        self.assertEqual(
            f.split_food_location_path("中国吉林省长春市南关区"),
            ["中国", "吉林省", "长春市", "南关区"],
        )
        self.assertEqual(
            f.split_food_location_path("中国 > 吉林省 → 长春市-南关区"),
            ["中国", "吉林省", "长春市", "南关区"],
        )
        self.assertEqual(f.split_food_location_path("https://evil"), [])
        self.assertEqual(f.split_food_location_path("a\x00b"), [])

    def test_match_food_location_full_prefix_ambiguous_none(self):
        items = [
            {"id": 1, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
            {"id": 2, "name": "朝阳区", "path": "中国/吉林省/长春市/朝阳区"},
            {"id": 4, "name": "长春市", "path": "中国/吉林省/长春市"},
        ]
        full = f.match_food_location(["中国", "吉林省", "长春市", "南关区"], items)
        self.assertEqual(full["kind"], "full")
        self.assertEqual(full["id"], 1)
        prefix = f.match_food_location(["中国", "吉林省", "长春市", "新区"], items)
        self.assertEqual(prefix["kind"], "prefix")
        self.assertEqual(prefix["tail"], ["新区"])
        ambiguous = f.match_food_location(["朝阳区"], [
            {"id": 2, "name": "朝阳区", "path": "中国/吉林省/长春市/朝阳区"},
            {"id": 3, "name": "朝阳区", "path": "中国/北京市/朝阳区"},
        ])
        self.assertEqual(ambiguous["kind"], "ambiguous")
        self.assertEqual(f.match_food_location(["无"], items)["kind"], "none")

    def test_group_locations_json_parsing(self):
        parsed = f.parse_food_group_locations('{"123":"吉林省/长春市","bad":"https://x"}')
        self.assertEqual(parsed, {"123": ["吉林省", "长春市"]})
        listed = f.parse_food_group_locations({"123": ["吉林省", "长春市"]})
        self.assertEqual(listed, {"123": ["吉林省", "长春市"]})
        self.assertEqual(f.parse_food_group_locations("not-json"), {})
        self.assertEqual(f.parse_food_group_locations(["bad"]), {})

    def test_infer_group_location_only_explicit_suffix(self):
        self.assertEqual(f.infer_food_group_location_parts("长春市火锅交流群"), ["长春市"])
        self.assertEqual(f.infer_food_group_location_parts("吉林省长春市美食群"), ["吉林省", "长春市"])
        self.assertEqual(f.infer_food_group_location_parts("长春火锅群"), [])

    def test_merge_group_context(self):
        self.assertEqual(
            f.merge_food_location_with_context(["南关区"], ["吉林省", "长春市"]),
            ["吉林省", "长春市", "南关区"],
        )
        self.assertEqual(
            f.merge_food_location_with_context(["长春市", "南关区"], ["吉林省", "长春市"]),
            ["吉林省", "长春市", "南关区"],
        )
        self.assertEqual(
            f.merge_food_location_with_context(["中国", "北京市"], ["吉林省", "长春市"]),
            ["中国", "北京市"],
        )
        self.assertEqual(f.merge_food_location_with_context([], ["长春市"]), ["长春市"])

    def test_strip_group_city_prefix(self):
        self.assertEqual(
            f.strip_food_group_city_prefix("长春火锅", ["吉林省", "长春市"]),
            ("火锅", True),
        )
        self.assertEqual(
            f.strip_food_group_city_prefix("火锅", ["吉林省", "长春市"]),
            ("火锅", False),
        )


class ResponseMappingTests(unittest.TestCase):
    def test_parse_success_requires_200_and_success_true(self):
        self.assertTrue(f.parse_food_submit_response(200, {"success": True})["ok"])
        self.assertFalse(f.parse_food_submit_response(200, {"success": "true"})["ok"])
        self.assertFalse(f.parse_food_submit_response(201, {"success": True})["ok"])

    def test_parse_duplicate_location_not_found_and_server(self):
        dup = f.parse_food_submit_response(
            400, {"success": False, "code": "DUPLICATE_USERNAME", "error": "用户名已存在"}
        )
        self.assertEqual(dup["kind"], "duplicate_username")
        loc = f.parse_food_submit_response(
            400, {"code": "LOCATION_EXISTS", "error": "位置「X」已存在"}
        )
        self.assertEqual(loc["kind"], "location_exists")
        missing = f.parse_food_submit_response(
            400, {"code": "MISSING_TASTE_SCORE", "error": "味道分不能为空"}
        )
        self.assertEqual(missing["kind"], "missing_field")
        invalid = f.parse_food_submit_response(
            400, {"code": "INVALID_VALUE_SCORE", "error": "性价比分格式不正确"}
        )
        self.assertEqual(invalid["kind"], "invalid_score")
        not_found = f.parse_food_submit_response(
            404, {"code": "RESTAURANT_NOT_FOUND", "error": "餐厅不存在"}
        )
        self.assertEqual(not_found["kind"], "restaurant_not_found")
        server = f.parse_food_submit_response(500, {})
        self.assertEqual(server["kind"], "server")
        redirect = f.parse_food_submit_response(302, {"success": True})
        self.assertEqual(redirect["kind"], "redirect")
        self.assertFalse(redirect["ok"])

    def test_safe_error_reason_removes_urls_paths_and_secret(self):
        reason = f.safe_food_error_reason(
            {"error": "bad https://evil.example/a/b 123456 /etc/passwd", "status": 400},
            forbidden="123456",
        )
        self.assertNotIn("https://", reason)
        self.assertNotIn("123456", reason)
        self.assertNotIn("/etc", reason)
        self.assertLessEqual(len(reason), 80)


class SearchHelperTests(unittest.TestCase):
    def test_sort_numeric_and_missing_last(self):
        items = [
            {"name": "A", "total_avg": 3, "taste_avg": 4, "value_avg": 2, "review_count": 1},
            {"name": "B", "total_avg": 8, "taste_avg": 2, "value_avg": 5, "review_count": 9},
            {"name": "C"},
        ]
        self.assertEqual([i["name"] for i in f.sort_food_restaurants(items, sort="total", order="desc")], ["B", "A", "C"])
        self.assertEqual([i["name"] for i in f.sort_food_restaurants(items, sort="taste", order="asc")], ["B", "A", "C"])
        self.assertEqual([i["name"] for i in f.sort_food_restaurants(items, sort="value", order="desc")], ["B", "A", "C"])
        self.assertEqual([i["name"] for i in f.sort_food_restaurants(items, sort="reviews", order="desc")], ["B", "A", "C"])
        self.assertEqual([i["name"] for i in f.sort_food_restaurants(items, sort="name", order="asc")], ["A", "B", "C"])

    def test_sort_latest(self):
        items = [
            {"name": "old", "latest_review_date": "2025-01-01"},
            {"name": "new", "latest_review_date": "2026-01-01"},
            {"name": "none"},
        ]
        self.assertEqual(
            [i["name"] for i in f.sort_food_restaurants(items, sort="latest", order="desc")],
            ["new", "old", "none"],
        )

    def test_normalize_sort_order_limit(self):
        self.assertEqual(f.normalize_food_search_sort("口味"), "taste")
        self.assertEqual(f.normalize_food_search_sort("评分"), "total")
        self.assertEqual(f.normalize_food_search_sort("bad"), "")
        self.assertEqual(f.normalize_food_search_order_value("升序"), "asc")
        self.assertEqual(f.normalize_food_search_order_value("bad"), "")
        self.assertEqual(f.normalize_food_search_order("", sort="name"), "asc")
        self.assertEqual(f.normalize_food_search_order("", sort="total"), "desc")
        self.assertEqual(f.normalize_food_search_limit("99", default=5, hard=5), 5)
        self.assertEqual(f.normalize_food_search_limit("0", default=3, hard=5), 3)
        self.assertEqual(f.normalize_food_search_limit("abc", default=3, hard=5), 3)

    def test_resolve_tag_exact_ambiguous_not_found(self):
        items = [
            {"id": 1, "name": "火锅"},
            {"id": 2, "name": "火锅自助"},
            {"id": 3, "name": "烤肉"},
        ]
        self.assertEqual(f.resolve_food_search_tag(items, "火锅")["id"], 1)
        ambiguous = f.resolve_food_search_tag(items, "自助")
        self.assertIn("ambiguous", ambiguous)
        duplicate_exact = f.resolve_food_search_tag(
            [{"id": 1, "name": "火锅"}, {"id": 2, "name": "火锅"}], "火锅"
        )
        self.assertIn("ambiguous", duplicate_exact)
        self.assertIn("not_found", f.resolve_food_search_tag(items, "甜品"))

    def test_resolve_location_exact_single_ambiguous(self):
        items = [
            {"id": 1, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
            {"id": 2, "name": "朝阳区", "path": "中国/吉林省/长春市/朝阳区"},
        ]
        self.assertEqual(f.resolve_food_search_location(items, "南关区")["id"], 1)
        ambiguous = f.resolve_food_search_location(items, "区")
        self.assertIn("ambiguous", ambiguous)
        duplicate_exact = f.resolve_food_search_location(
            [
                {"id": 1, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
                {"id": 2, "name": "南关区", "path": "中国/辽宁省/沈阳市/南关区"},
            ],
            "南关区",
        )
        self.assertIn("ambiguous", duplicate_exact)
        self.assertIn("not_found", f.resolve_food_search_location([], "x"))
        single = f.resolve_food_search_location([items[0]], "不匹配但唯一")
        self.assertEqual(single["id"], 1)

    def test_normalize_search_request(self):
        req = f.normalize_food_search_request(
            {"name": "火锅", "location": "南关区", "tag": "火锅,夜宵", "sort": "口味", "order": "升序", "limit": "3"},
            default_limit=5,
        )
        self.assertTrue(req["ok"])
        self.assertEqual(req["tags"], ["火锅", "夜宵"])
        self.assertEqual(req["sort"], "taste")
        self.assertEqual(req["order"], "asc")
        self.assertEqual(req["limit"], 3)
        self.assertEqual(f.normalize_food_search_request({})["error"], "empty")
        self.assertEqual(f.normalize_food_search_request({"sort": "bad", "name": "x"})["error"], "invalid_sort")
        self.assertEqual(f.normalize_food_search_request({"order": "bad", "name": "x"})["error"], "invalid_order")
        self.assertEqual(f.normalize_food_search_request({"limit": "abc", "name": "x"})["error"], "invalid_limit")
        self.assertEqual(f.normalize_food_search_request({"name": "https://x"})["error"], "invalid_name")

    def test_parse_command_forms_and_errors(self):
        self.assertEqual(f.parse_food_search_command("火锅")["name"], "火锅")
        location_only = f.parse_food_search_command("南关区")
        self.assertEqual(location_only["location"], "南关区")
        self.assertEqual(location_only["name"], "")
        self.assertEqual(f.parse_food_search_command("南关区 火锅")["name"], "火锅")
        self.assertEqual(f.parse_food_search_command("南关区 火锅")["location"], "南关区")
        self.assertEqual(f.parse_food_search_command("火锅 南关区")["location"], "南关区")
        flagged = f.parse_food_search_command(
            "--tag 火锅 --location 长春市 --sort taste --order asc --limit 2"
        )
        self.assertEqual(flagged["tags"], ["火锅"])
        self.assertEqual(flagged["location"], "长春市")
        self.assertEqual(flagged["limit"], 2)
        hashtag = f.parse_food_search_command("#火锅 海底捞")
        self.assertEqual(hashtag["tags"], ["火锅"])
        self.assertEqual(hashtag["name"], "海底捞")
        self.assertEqual(f.parse_food_search_command(""), {"ok": False, "error": "empty"})
        self.assertEqual(f.parse_food_search_command("--unknown x")["error"], "unknown_flag")
        self.assertEqual(f.parse_food_search_command("--limit")["error"], "missing_value")
        self.assertEqual(f.parse_food_search_command("--sort bad 火锅")["error"], "invalid_sort")

    def test_format_search_results_alias_bounded(self):
        text = f.format_food_search_results(
            [{"name": "店", "total_avg": 8, "review_count": 1}],
            label="火锅",
            location_name="长春市",
            max_results=1,
            max_chars=100,
        )
        self.assertIn("火锅", text)
        self.assertIn("长春市", text)
        self.assertLessEqual(len(text), 100)


if __name__ == "__main__":
    unittest.main()
