"""Parser and validator tests: malformed syntax, whitelist and unsafe values."""

from __future__ import annotations

import random
import re
import unittest

from _support import safe_tools


class ParserBasicsTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self):
        parsed = safe_tools.parse_tool_markers("普通回复")
        self.assertFalse(parsed.had_marker)
        self.assertEqual(parsed.clean_text, "普通回复")
        self.assertEqual(parsed.intents, [])

    def test_double_quoted_argument(self):
        parsed = safe_tools.parse_tool_markers(
            '前文 [[tool:search_images keyword="白色 猫咪" count=1]]'
        )
        self.assertTrue(parsed.had_marker)
        self.assertEqual(parsed.clean_text, "前文")
        self.assertEqual(parsed.intents[0].args["keyword"], "白色 猫咪")
        self.assertTrue(parsed.intents[0].valid)

    def test_single_quoted_argument(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:send_sticker emotion='happy']]"
        )
        self.assertEqual(parsed.intents[0].args["emotion"], "happy")
        self.assertTrue(parsed.intents[0].valid)

    def test_bare_argument(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:search_images keyword=cat count=1]]"
        )
        self.assertEqual(parsed.intents[0].args["keyword"], "cat")
        self.assertTrue(parsed.intents[0].valid)

    def test_escaped_double_quote_is_decoded(self):
        parsed = safe_tools.parse_tool_markers(
            r'[[tool:search_images keyword="a\"b"]]'
        )
        self.assertEqual(parsed.intents[0].args["keyword"], 'a"b')
        self.assertTrue(parsed.intents[0].valid)

    def test_multiple_arguments_and_whitespace(self):
        parsed = safe_tools.parse_tool_markers(
            '[[tool:submit_food_review restaurant="店" taste="4.5" value="3.0"]]'
        )
        self.assertTrue(parsed.intents[0].valid)
        self.assertEqual(parsed.intents[0].args["restaurant"], "店")

    def test_clean_text_collapses_marker_gap(self):
        parsed = safe_tools.parse_tool_markers(
            "第一行\n[[tool:send_sticker emotion=happy]]  \n第二行"
        )
        self.assertEqual(parsed.clean_text, "第一行\n\n第二行")

    def test_multiple_markers_are_all_returned(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:send_sticker emotion=happy]]\n"
            "[[tool:list_sticker_categories()]]"
        )
        self.assertEqual(len(parsed.intents), 2)
        self.assertTrue(all(item.valid for item in parsed.intents))
        self.assertEqual(parsed.clean_text, "")

    def test_strict_regex_does_not_cross_newline(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:send_sticker emotion=happy\n]]"
        )
        self.assertTrue(parsed.had_marker)
        self.assertEqual(parsed.clean_text, "")
        self.assertFalse(any(item.valid for item in parsed.intents))

    def test_nested_brackets_fail_closed(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:send_sticker emotion=[x]]]"
        )
        self.assertTrue(parsed.had_marker)
        self.assertFalse(any(item.valid for item in parsed.intents))


class ParserMalformedTests(unittest.TestCase):
    def test_unknown_tool_rejected(self):
        parsed = safe_tools.parse_tool_markers("[[tool:rm_rf path=x]]")
        self.assertFalse(parsed.intents[0].valid)
        self.assertIn("白名单", parsed.intents[0].error)

    def test_unknown_argument_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:search_images keyword=cat extra=value]]"
        )
        self.assertFalse(parsed.intents[0].valid)
        self.assertIn("参数", parsed.intents[0].error)

    def test_duplicate_argument_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:search_images keyword=cat keyword=dog]]"
        )
        self.assertFalse(parsed.intents[0].valid)
        self.assertEqual(parsed.intents[0].error, "参数重复")

    def test_missing_closing_quote_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            '[[tool:search_images keyword="cat]]'
        )
        self.assertTrue(parsed.had_marker)
        self.assertEqual(parsed.clean_text, "")
        self.assertFalse(any(item.valid for item in parsed.intents))

    def test_trailing_junk_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:send_sticker emotion=happy ???]]"
        )
        self.assertFalse(parsed.intents[0].valid)
        self.assertEqual(parsed.intents[0].error, "参数格式无效")

    def test_adjacent_pairs_without_space_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            '[[tool:search_images keyword="cat"count=2]]'
        )
        self.assertFalse(parsed.intents[0].valid)

    def test_unbalanced_quote_inside_bare_value_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            '[[tool:search_images keyword=cat"dog]]'
        )
        self.assertFalse(parsed.intents[0].valid)

    def test_malformed_value_escape_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            r'[[tool:search_images keyword="bad\q"]]'
        )
        self.assertFalse(parsed.intents[0].valid)

    def test_dangling_marker_is_stripped_and_invalid(self):
        parsed = safe_tools.parse_tool_markers(
            'before [[tool:send_sticker emotion="happy" after'
        )
        self.assertTrue(parsed.had_marker)
        self.assertEqual(parsed.clean_text, "before")
        self.assertFalse(any(item.valid for item in parsed.intents))

    def test_valid_plus_dangling_marker_taints_reply(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:send_sticker emotion=happy]]\n"
            "[[tool:search_images keyword=cat"
        )
        self.assertTrue(parsed.had_marker)
        self.assertGreaterEqual(len(parsed.intents), 2)
        self.assertFalse(all(item.valid for item in parsed.intents))

    def test_marker_with_only_openers_is_stripped(self):
        parsed = safe_tools.parse_tool_markers("x [[tool: y")
        self.assertTrue(parsed.had_marker)
        self.assertEqual(parsed.clean_text, "x")
        self.assertFalse(any(item.valid for item in parsed.intents))

    def test_fuzzish_malformed_inputs_never_raise(self):
        rng = random.Random(20261021)
        alphabet = '[]tool:send_sticker= "\'\\/\n;|`$<>测试abc123'
        for _ in range(120):
            body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
            with self.subTest(body=body[:40]):
                parsed = safe_tools.parse_tool_markers(
                    "prefix " + body + " suffix"
                )
                self.assertIsInstance(parsed.clean_text, str)
                self.assertNotIn("[[tool:", parsed.clean_text.lower())


class SearchImageValidationTests(unittest.TestCase):
    def validate(self, body):
        return safe_tools.parse_tool_markers(f"[[tool:search_images {body}]]").intents[0]

    def test_missing_keyword_rejected(self):
        self.assertFalse(self.validate("count=1").valid)

    def test_empty_keyword_rejected(self):
        intent = self.validate('keyword="   "')
        self.assertFalse(intent.valid)
        self.assertIn("必填", intent.error)

    def test_keyword_length_limit(self):
        intent = self.validate('keyword="' + "a" * 101 + '"')
        self.assertFalse(intent.valid)
        self.assertIn("过长", intent.error)

    def test_keyword_slash_rejected(self):
        self.assertFalse(self.validate("keyword=a/b").valid)

    def test_keyword_url_rejected(self):
        self.assertFalse(self.validate("keyword=https://example.com").valid)

    def test_keyword_shell_metachar_rejected(self):
        self.assertFalse(self.validate("keyword=cat;rm").valid)

    def test_keyword_ampersand_command_rejected(self):
        self.assertFalse(self.validate("keyword=cat&whoami").valid)

    def test_keyword_pipe_command_rejected(self):
        self.assertFalse(self.validate("keyword=cat|id").valid)

    def test_keyword_command_word_rejected(self):
        self.assertFalse(self.validate("keyword=sudo cat").valid)

    def test_keyword_non_http_scheme_rejected(self):
        self.assertFalse(self.validate("keyword=data:text/html").valid)

    def test_keyword_windows_path_rejected(self):
        self.assertFalse(self.validate(r"keyword=C:\Windows\secret").valid)

    def test_count_zero_rejected(self):
        self.assertFalse(self.validate("keyword=cat count=0").valid)

    def test_count_over_limit_rejected(self):
        self.assertFalse(self.validate("keyword=cat count=9").valid)

    def test_count_not_integer_rejected(self):
        self.assertFalse(self.validate("keyword=cat count=many").valid)

    def test_count_valid(self):
        self.assertTrue(self.validate("keyword=cat count=5").valid)


class StickerValidationTests(unittest.TestCase):
    def validate(self, body):
        return safe_tools.parse_tool_markers(f"[[tool:send_sticker {body}]]").intents[0]

    def test_exactly_one_selector_valid(self):
        self.assertTrue(self.validate("category=开心").valid)

    def test_two_selectors_rejected(self):
        self.assertFalse(self.validate("category=开心 emotion=happy").valid)

    def test_zero_selectors_rejected(self):
        self.assertFalse(self.validate("").valid)

    def test_bad_category_rejected(self):
        self.assertFalse(self.validate("category=a/b").valid)

    def test_category_regex_length_boundary(self):
        self.assertTrue(self.validate("category=" + "a" * 32).valid)
        self.assertFalse(self.validate("category=" + "a" * 33).valid)

    def test_collect_empty_category_valid(self):
        parsed = safe_tools.parse_tool_markers("[[tool:collect_sticker()]]")
        self.assertTrue(parsed.intents[0].valid)

    def test_collect_bad_category_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:collect_sticker category=../secret]]"
        )
        self.assertFalse(parsed.intents[0].valid)

    def test_list_categories_valid(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:list_sticker_categories()]]"
        )
        self.assertTrue(parsed.intents[0].valid)

    def test_list_categories_extra_args_rejected(self):
        parsed = safe_tools.parse_tool_markers(
            "[[tool:list_sticker_categories category=x]]"
        )
        self.assertFalse(parsed.intents[0].valid)

    def test_safe_category_regex_public(self):
        self.assertTrue(safe_tools.SAFE_CATEGORY_RE.fullmatch("中文_标签-1"))
        self.assertFalse(safe_tools.SAFE_CATEGORY_RE.fullmatch("a/b"))
        self.assertFalse(safe_tools.SAFE_CATEGORY_RE.fullmatch(""))


class FetchValidationTests(unittest.TestCase):
    def validate(self, body):
        return safe_tools.parse_tool_markers(f"[[tool:fetch_webpage {body}]]").intents[0]

    def test_public_https_valid(self):
        self.assertTrue(self.validate('url="https://example.com/a"').valid)

    def test_http_is_allowed_by_public_validator(self):
        self.assertTrue(self.validate('url="http://example.com/a"').valid)

    def test_ftp_scheme_rejected(self):
        self.assertFalse(self.validate('url="ftp://example.com/a"').valid)

    def test_localhost_rejected(self):
        self.assertFalse(self.validate('url="http://127.0.0.1/a"').valid)

    def test_private_ip_rejected(self):
        self.assertFalse(self.validate('url="http://10.0.0.2/a"').valid)

    def test_credentials_rejected(self):
        self.assertFalse(
            self.validate('url="https://user:pass@example.com/a"').valid
        )

    def test_missing_url_rejected(self):
        self.assertFalse(self.validate("max_chars=10").valid)

    def test_max_chars_non_numeric_rejected(self):
        self.assertFalse(
            self.validate('url="https://example.com" max_chars=abc').valid
        )

    def test_max_chars_out_of_range_rejected(self):
        self.assertFalse(
            self.validate('url="https://example.com" max_chars=999999').valid
        )

    def test_max_chars_valid(self):
        self.assertTrue(
            self.validate('url="https://example.com" max_chars=100').valid
        )


class FoodValidationTests(unittest.TestCase):
    def parse(self, tool, body):
        return safe_tools.parse_tool_markers(f"[[tool:{tool} {body}]]").intents[0]

    def test_search_requires_selector(self):
        self.assertFalse(self.parse("search_food_restaurants", "limit=3").valid)

    def test_search_name_valid(self):
        self.assertTrue(
            self.parse("search_food_restaurants", "name=火锅店 limit=3").valid
        )

    def test_search_path_location_with_slashes_valid(self):
        self.assertTrue(
            self.parse(
                "search_food_restaurants",
                'location="中国/吉林省/长春市"',
            ).valid
        )

    def test_search_url_selector_rejected(self):
        self.assertFalse(
            self.parse(
                "search_food_restaurants",
                'location="https://evil.example"',
            ).valid
        )

    def test_search_traversal_location_rejected(self):
        self.assertFalse(
            self.parse(
                "search_food_restaurants",
                'location="../../etc/passwd"',
            ).valid
        )

    def test_search_absolute_path_rejected(self):
        self.assertFalse(
            self.parse(
                "search_food_restaurants",
                'location="/etc/passwd"',
            ).valid
        )

    def test_search_bad_sort_rejected(self):
        self.assertFalse(
            self.parse("search_food_restaurants", "tag=火锅 sort=hack").valid
        )

    def test_search_bad_order_rejected(self):
        self.assertFalse(
            self.parse("search_food_restaurants", "tag=火锅 order=sideways").valid
        )

    def test_search_bad_limit_rejected(self):
        self.assertFalse(
            self.parse("search_food_restaurants", "tag=火锅 limit=99").valid
        )

    def test_partial_submit_valid_for_followup(self):
        self.assertTrue(
            self.parse("submit_food_review", 'restaurant="店名"').valid
        )

    def test_empty_submit_rejected(self):
        self.assertFalse(self.parse("submit_food_review", "").valid)

    def test_submit_url_comment_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'comment="see https://evil.example"',
            ).valid
        )

    def test_submit_traversal_location_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'location="../../secret"',
            ).valid
        )

    def test_submit_command_comment_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'comment="好吃; rm -rf /"',
            ).valid
        )

    def test_submit_unexpected_field_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'restaurant="店" password="secret"',
            ).valid
        )

    def test_submit_bad_score_rejected(self):
        self.assertFalse(
            self.parse("submit_food_review", 'taste="99"').valid
        )

    def test_submit_control_character_rejected(self):
        self.assertFalse(
            self.parse("submit_food_review", 'comment="line\\nnext"').valid
        )

    def test_submit_long_comment_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'comment="' + "好" * 501 + '"',
            ).valid
        )

    def test_submit_too_many_tags_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'tags="a,b,c,d,e,f"',
            ).valid
        )

    def test_submit_overlong_tag_rejected(self):
        self.assertFalse(
            self.parse(
                "submit_food_review",
                'tags="' + "a" * 21 + '"',
            ).valid
        )

    def test_submit_tags_and_scores_valid(self):
        self.assertTrue(
            self.parse(
                "submit_food_review",
                'restaurant="店" taste="4.5" value="3.0" tags="火锅,夜宵"',
            ).valid
        )


class InstructionTests(unittest.TestCase):
    def test_no_enabled_tools_returns_empty(self):
        self.assertEqual(safe_tools.tool_instruction([]), "")

    def test_only_requested_catalog_is_returned(self):
        text = safe_tools.tool_instruction(["fetch_webpage"])
        self.assertIn("fetch_webpage", text)
        self.assertNotIn("search_images", text)
        self.assertNotIn("submit_food_review", text)
        self.assertIn("[[tool:", text)

    def test_all_catalog_contains_whitelist(self):
        text = safe_tools.tool_instruction(None)
        for name in safe_tools.ALLOWED_TOOLS:
            self.assertIn(name, text)

    def test_allowed_tools_are_fixed_whitelist(self):
        self.assertIsInstance(safe_tools.ALLOWED_TOOLS, tuple)
        self.assertNotIn("eval", safe_tools.ALLOWED_TOOLS)
        self.assertNotIn("shell", safe_tools.ALLOWED_TOOLS)
        self.assertEqual(len(set(safe_tools.ALLOWED_TOOLS)), 7)

    def test_marker_grammar_uses_private_double_brackets(self):
        text = safe_tools.tool_instruction(["send_sticker"])
        self.assertRegex(text, re.escape("[[tool:工具名"))


if __name__ == "__main__":
    unittest.main()
