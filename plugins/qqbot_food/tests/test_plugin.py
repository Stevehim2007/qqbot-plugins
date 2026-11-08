from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from support import (
    LOGGER,
    FakeEvent,
    FakeFoodAPI,
    collect_async,
    food_api,
    food_engine,
    make_plugin,
    run,
)

LOCATIONS = [
    {"id": 11, "name": "长春市", "path": "中国/吉林省/长春市"},
    {"id": 10, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
    {"id": 12, "name": "朝阳区", "path": "中国/吉林省/长春市/朝阳区"},
]
TAGS = [
    {"id": 1, "name": "火锅"},
    {"id": 2, "name": "火锅自助"},
    {"id": 3, "name": "烤肉"},
]
RESTAURANTS = [
    {
        "id": 101,
        "name": "A火锅",
        "location_name": "南关区",
        "tags": ["火锅"],
        "taste_avg": 4.8,
        "value_avg": 4.0,
        "total_avg": 8.8,
        "review_count": 20,
        "latest_review_date": "2026-01-03",
    },
    {
        "id": 102,
        "name": "B烤肉",
        "location_name": "朝阳区",
        "tags": ["烤肉"],
        "taste_avg": 3.0,
        "value_avg": 3.5,
        "total_avg": 6.5,
        "review_count": 2,
        "latest_review_date": "2025-01-01",
    },
    {
        "id": 103,
        "name": "C小店",
        "location_name": "南关区",
        "tags": ["小吃"],
        "taste_avg": 4.0,
        "value_avg": 4.1,
        "total_avg": 8.1,
        "review_count": 9,
        "latest_review_date": "2026-02-01",
    },
]


def call_command(plugin, event):
    return run(collect_async(plugin.food_command(event)))


def call_confirm(plugin, event):
    return run(collect_async(plugin.on_food_message(event)))


def valid_submit_args(**overrides):
    args = {
        "restaurant": "A火锅",
        "location": "吉林省/长春市/南关区",
        "taste": "4.5",
        "value": "3.5",
        "comment": "好吃，推荐",
        "tags": "火锅,夜宵",
    }
    args.update(overrides)
    return args


class PluginTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api = FakeFoodAPI(
            locations=LOCATIONS,
            tags=TAGS,
            restaurants=RESTAURANTS,
        )

    def plugin(self, **config):
        return make_plugin(
            Path(self.tmp.name), config=config, api=self.api
        )

    def event(self, text="", **kwargs):
        kwargs.setdefault("private", True)
        return FakeEvent(text, **kwargs)


class CommandSearchTests(PluginTestBase):
    def test_group_policy_disables_food_command(self):
        plugin = self.plugin()
        calls = []

        class SafeStar:
            def policy_effective(self, plugin_id, *, group_id=None, user_id=None, is_private=False):
                calls.append((plugin_id, group_id, user_id, is_private))
                return {"enabled": False, "params": {}}

        plugin.context.get_registered_star = lambda name: (
            type("Meta", (), {"star_cls": SafeStar()})()
            if name == "qqbot_safe_tools"
            else None
        )
        event = self.event("/food 火锅", private=False, group_id="20002")
        results = call_command(plugin, event)
        self.assertEqual(results, [])
        self.assertEqual(self.api.get_calls, [])
        self.assertTrue(event.stopped)
        self.assertEqual(calls[0][0], "qqbot_food")
        self.assertEqual(calls[0][1], "20002")
        self.assertFalse(calls[0][3])

    def test_group_policy_default_city(self):
        plugin = self.plugin()

        class SafeStar:
            def policy_effective(self, plugin_id, *, group_id=None, user_id=None, is_private=False):
                return {
                    "enabled": True,
                    "params": {"default_city": "长春"},
                }

        plugin.context.get_registered_star = lambda name: (
            type("Meta", (), {"star_cls": SafeStar()})()
            if name == "qqbot_safe_tools"
            else None
        )
        event = self.event("/food 火锅", private=False, group_id="20002")
        results = call_command(plugin, event)
        self.assertTrue(results)
        restaurant_calls = [
            params
            for path, params in self.api.get_calls
            if path == food_engine.FOOD_RESTAURANTS_PATH
        ]
        self.assertTrue(restaurant_calls)
        self.assertIn("location_id", restaurant_calls[0])

    def test_command_basic_search(self):
        plugin = self.plugin()
        event = self.event("/food 火锅")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("A火锅", results[0].text)
        self.assertTrue(event.stopped)
        self.assertEqual(event.should_call_llm_values[-1], True)
        self.assertIn(
            food_engine.FOOD_RESTAURANTS_PATH,
            [path for path, _params in self.api.get_calls],
        )

    def test_command_aliases_are_registered_and_work(self):
        plugin = self.plugin()
        args, kwargs = plugin.food_command._qqbot_food_command
        self.assertEqual(args[0], "food")
        self.assertEqual(set(kwargs["alias"]), {"美食", "吃什么", "饭店"})
        for command in ("/吃什么 火锅", "/饭店 火锅"):
            event = self.event(command)
            results = call_command(plugin, event)
            self.assertTrue(results)
            self.assertIn("A火锅", results[0].text)

    def test_command_location_before_and_after_name(self):
        plugin = self.plugin()
        for text in ("/food 南关区 火锅", "/food 火锅 南关区"):
            event = self.event(text)
            results = call_command(plugin, event)
            self.assertTrue(results)
        restaurant_calls = [
            params for path, params in self.api.get_calls
            if path == food_engine.FOOD_RESTAURANTS_PATH
        ]
        self.assertEqual(restaurant_calls[-1].get("location_id"), "10")
        self.assertEqual(restaurant_calls[-2].get("location_id"), "10")

    def test_command_tag_resolution_and_ambiguous_tag(self):
        plugin = self.plugin()
        event = self.event("/food --tag 火锅")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("A火锅", results[0].text)
        event = self.event("/food --tag 自助")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("匹配到多个", results[0].text)

    def test_command_unknown_tag(self):
        plugin = self.plugin()
        event = self.event("/food --tag 甜品")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("没有找到标签", results[0].text)

    def test_command_sort_order_limit(self):
        plugin = self.plugin(config={"food_max_results": 2})
        event = self.event("/food 火锅 --sort total --order desc --limit 2")
        results = call_command(plugin, event)
        self.assertTrue(results)
        text = results[0].text
        self.assertIn("A火锅", text)
        self.assertIn("C小店", text)
        self.assertNotIn("B烤肉", text)

    def test_command_invalid_query_and_empty_usage(self):
        plugin = self.plugin()
        event = self.event("/food https://evil.example")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("不合法", results[0].text)
        event = self.event("/food")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("用法", results[0].text)

    def test_command_disabled_custom_config_reply_is_honored(self):
        plugin = self.plugin(
            food_enabled=False,
            food_disabled_reply="自定义关闭文案",
        )
        event = self.event("/food 火锅")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertEqual(results[0].text, "自定义关闭文案")

    def test_command_empty_reply_uses_food_empty_config(self):
        self.api.restaurants = []
        plugin = self.plugin(food_empty_reply="自定义无结果")
        event = self.event("/food 火锅")
        results = call_command(plugin, event)
        self.assertEqual(results[0].text, "自定义无结果")

    def test_command_location_not_found_uses_command_config(self):
        self.api.locations = []
        plugin = self.plugin(food_location_not_found_reply="没有这个地点:{location}")
        event = self.event("/food 不存在区 火锅")
        results = call_command(plugin, event)
        self.assertEqual(results[0].text, "没有这个地点:不存在区")

    def test_command_timeout_maps_to_chinese(self):
        self.api.get_errors[food_engine.FOOD_RESTAURANTS_PATH] = [
            food_api.FoodTimeoutError("timeout")
        ]
        plugin = self.plugin()
        event = self.event("/food 火锅")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("超时", results[0].text)
        self.assertNotIn("FoodTimeoutError", results[0].text)

    def test_group_location_mapping_filters_search(self):
        plugin = self.plugin(
            food_group_locations=json.dumps({"10001": "吉林省/长春市"})
        )
        event = self.event("/food 火锅", private=False)
        results = call_command(plugin, event)
        self.assertTrue(results)
        restaurant_calls = [
            params for path, params in self.api.get_calls
            if path == food_engine.FOOD_RESTAURANTS_PATH
        ]
        self.assertEqual(restaurant_calls[-1].get("location_id"), "11")

    def test_group_location_inferred_from_group_name(self):
        plugin = self.plugin()
        event = self.event("/food 火锅", private=False)
        results = call_command(plugin, event)
        self.assertTrue(results)
        restaurant_calls = [
            params for path, params in self.api.get_calls
            if path == food_engine.FOOD_RESTAURANTS_PATH
        ]
        self.assertEqual(restaurant_calls[-1].get("location_id"), "11")

    def test_group_city_prefix_is_stripped_from_keyword(self):
        plugin = self.plugin()
        event = self.event("/food 长春火锅", private=False)
        results = call_command(plugin, event)
        self.assertTrue(results)
        restaurant_calls = [
            params for path, params in self.api.get_calls
            if path == food_engine.FOOD_RESTAURANTS_PATH
        ]
        self.assertEqual(restaurant_calls[-1].get("q"), "火锅")
        self.assertEqual(restaurant_calls[-1].get("location_id"), "11")

    def test_location_only_command(self):
        plugin = self.plugin()
        event = self.event("/food 南关区")
        results = call_command(plugin, event)
        self.assertTrue(results)
        restaurant_calls = [
            params for path, params in self.api.get_calls
            if path == food_engine.FOOD_RESTAURANTS_PATH
        ]
        self.assertEqual(restaurant_calls[-1].get("location_id"), "10")
        self.assertNotIn("q", restaurant_calls[-1])

    def test_location_api_error_maps_to_search_error_not_not_found(self):
        self.api.get_errors[food_engine.FOOD_LOCATIONS_PATH] = [
            food_api.FoodTransportError("network")
        ]
        plugin = self.plugin()
        event = self.event("/food 南关区 火锅")
        results = call_command(plugin, event)
        self.assertTrue(results)
        self.assertIn("暂时失败", results[0].text)
        self.assertNotIn("没有找到地点", results[0].text)


class SearchToolTests(PluginTestBase):
    def test_search_tool_direct_args(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_search_restaurants(
                event, {"name": "火锅", "limit": "2"}, "找火锅"
            )
        )
        self.assertIsNotNone(result)
        self.assertIn("A火锅", result.text)

    def test_search_tool_tag_and_location(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_search_restaurants(
                event,
                {"tag": "火锅", "location": "南关区", "sort": "taste"},
                "找南关区火锅",
            )
        )
        self.assertIsNotNone(result)
        self.assertIn("A火锅", result.text)

    def test_search_tool_empty_and_invalid(self):
        plugin = self.plugin()
        event = self.event()
        result = run(plugin.tool_search_restaurants(event, {}, ""))
        self.assertIn("想找哪家店", result.text)
        result = run(
            plugin.tool_search_restaurants(event, {"sort": "bad", "name": "x"}, "")
        )
        self.assertIn("不合法", result.text)

    def test_search_tool_never_raises_on_api_error(self):
        self.api.get_errors[food_engine.FOOD_RESTAURANTS_PATH] = [
            RuntimeError("boom /secret/path")
        ]
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_search_restaurants(event, {"name": "火锅"}, "")
        )
        self.assertIsNotNone(result)
        self.assertIn("暂时失败", result.text)
        self.assertNotIn("secret/path", result.text)
        self.assertNotIn("RuntimeError", result.text)

    def test_search_tool_disabled_returns_fixed_reply(self):
        plugin = self.plugin(food_search_enabled=False)
        event = self.event()
        result = run(plugin.tool_search_restaurants(event, {"name": "x"}, ""))
        self.assertIn("未启用", result.text)


class SubmitDraftTests(PluginTestBase):
    def test_submit_tool_drafts_and_asks_confirmation_without_post(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_submit_food_review(
                event, valid_submit_args(), "帮我添加这个评价到 food"
            )
        )
        self.assertIsNotNone(result)
        self.assertIn("请确认", result.text)
        self.assertEqual(self.api.post_calls, 0)
        key, _password = plugin._submit_key(event)
        draft = plugin._state.get_draft(key)
        self.assertEqual(draft["status"], "pending_confirmation")
        self.assertEqual(draft["fields"]["restaurant"], "A火锅")

    def test_submit_tool_structured_marker_with_empty_text_asks_confirmation(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_submit_food_review(event, valid_submit_args(), "")
        )
        self.assertIn("请确认", result.text)
        self.assertEqual(self.api.post_calls, 0)

    def test_submit_tool_missing_fields(self):
        plugin = self.plugin()
        event = self.event()
        args = valid_submit_args()
        args.pop("comment")
        result = run(
            plugin.tool_submit_food_review(event, args, "帮我添加评价")
        )
        self.assertIn("评价内容", result.text)
        self.assertEqual(self.api.post_calls, 0)

    def test_submit_tool_invalid_score(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_submit_food_review(
                event, valid_submit_args(taste="9"), "帮我添加评价"
            )
        )
        self.assertIn("0.1-5", result.text)
        self.assertEqual(self.api.post_calls, 0)

    def test_submit_tool_rejects_unknown_fields_including_url_password(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_submit_food_review(
                event,
                {**valid_submit_args(), "url": "https://evil", "password": "x"},
                "帮我添加评价",
            )
        )
        self.assertIn("没有生效", result.text)
        self.assertEqual(self.api.post_calls, 0)
        key, _ = plugin._submit_key(event)
        self.assertIsNone(plugin._state.get_draft(key))

    def test_submit_tool_requires_explicit_add_intent(self):
        plugin = self.plugin()
        event = self.event()
        result = run(
            plugin.tool_submit_food_review(
                event, valid_submit_args(), "这家火锅很好吃"
            )
        )
        self.assertIn("要把这条评价添加", result.text)
        self.assertEqual(self.api.post_calls, 0)
        confirm = self.event("添加")
        results = call_confirm(plugin, confirm)
        self.assertTrue(results)
        self.assertEqual(self.api.post_calls, 1)

    def test_submit_tool_blank_sender_fails_safe(self):
        plugin = self.plugin()
        event = self.event(sender_id="")
        result = run(
            plugin.tool_submit_food_review(
                event, valid_submit_args(), "帮我添加评价"
            )
        )
        self.assertIn("无法确认你的身份", result.text)
        self.assertEqual(self.api.post_calls, 0)
        snapshot = plugin._state.snapshot()
        self.assertEqual(snapshot.get("drafts"), {})
        confirm = self.event("确认", sender_id="")
        results = call_confirm(plugin, confirm)
        self.assertEqual(results, [])
        self.assertFalse(confirm.stopped)

    def test_submit_tool_partial_draft_completion(self):
        plugin = self.plugin()
        event = self.event()
        first = valid_submit_args()
        first.pop("comment")
        run(plugin.tool_submit_food_review(event, first, "帮我添加评价"))
        second = {"comment": "补全的评价"}
        result = run(
            plugin.tool_submit_food_review(event, second, "帮我添加评价")
        )
        self.assertIn("请确认", result.text)
        key, _ = plugin._submit_key(event)
        draft = plugin._state.get_draft(key)
        self.assertEqual(draft["fields"]["restaurant"], "A火锅")
        self.assertEqual(draft["fields"]["comment"], "补全的评价")


class ConfirmAndSubmitTests(PluginTestBase):
    def _draft(self, plugin, *, text="帮我添加这个评价到 food", args=None):
        event = self.event()
        result = run(
            plugin.tool_submit_food_review(
                event, args or valid_submit_args(), text
            )
        )
        self.assertIn("请确认", result.text)
        return event

    def test_confirm_posts_once_and_marks_submitted(self):
        plugin = self.plugin()
        draft_event = self._draft(plugin)
        confirm = self.event("确认")
        results = call_confirm(plugin, confirm)
        self.assertTrue(results)
        self.assertIn("已提交", results[0].text)
        self.assertEqual(self.api.post_calls, 1)
        self.assertTrue(confirm.stopped)
        self.assertEqual(confirm.should_call_llm_values, [True])
        key, _ = plugin._submit_key(draft_event)
        self.assertEqual(plugin._state.get_draft(key)["status"], "submitted")

    def test_repeated_confirmation_never_posts_twice(self):
        plugin = self.plugin()
        self._draft(plugin)
        first = call_confirm(plugin, self.event("确认"))
        second = call_confirm(plugin, self.event("提交"))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(self.api.post_calls, 1)
        self.assertIn("已经提交过", second[0].text)

    def test_duplicate_username_is_treated_as_already_submitted(self):
        self.api.post_results = [
            (
                400,
                {
                    "success": False,
                    "code": "DUPLICATE_USERNAME",
                    "error": "用户名「小明」在该店铺已存在",
                },
            )
        ]
        plugin = self.plugin()
        self._draft(plugin)
        first = call_confirm(plugin, self.event("确认"))
        self.assertIn("已有同名评价", first[0].text)
        self.assertEqual(self.api.post_calls, 1)
        second = call_confirm(plugin, self.event("确认"))
        self.assertEqual(self.api.post_calls, 1)
        self.assertIn("已经提交过", second[0].text)

    def test_400_missing_field_maps_to_actionable_chinese(self):
        self.api.post_results = [
            (400, {"success": False, "code": "MISSING_COMMENT", "error": "评价不能为空"})
        ]
        plugin = self.plugin()
        self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertIn("站点没有接受", results[0].text)
        self.assertIn("评价不能为空", results[0].text)
        self.assertNotIn("Traceback", results[0].text)

    def test_404_maps_to_restaurant_not_found(self):
        self.api.post_results = [
            (404, {"success": False, "code": "RESTAURANT_NOT_FOUND", "error": "餐厅不存在"})
        ]
        plugin = self.plugin()
        self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertIn("没有找到这家店", results[0].text)

    def test_500_maps_to_generic_error_and_keeps_draft_pending(self):
        self.api.post_results = [(500, {"error": "<html>boom</html>"})]
        plugin = self.plugin()
        draft_event = self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertIn("失败", results[0].text)
        key, _ = plugin._submit_key(draft_event)
        self.assertEqual(plugin._state.get_draft(key)["status"], "pending_confirmation")

    def test_post_timeout_maps_to_chinese_and_does_not_claim_success(self):
        self.api.post_errors = [food_api.FoodTimeoutError("timeout")]
        plugin = self.plugin()
        self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertIn("超时", results[0].text)
        self.assertNotIn("已提交", results[0].text)

    def test_post_oversize_maps_to_generic_error(self):
        self.api.post_results = [food_api.FoodResponseTooLarge("too large")]
        plugin = self.plugin()
        self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertIn("失败", results[0].text)
        self.assertNotIn("too large", results[0].text)

    def test_success_requires_success_true(self):
        self.api.post_results = [(200, {"success": False, "error": "nope"})]
        plugin = self.plugin()
        self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertNotIn("已提交到", results[0].text)

    def test_one_sender_isolation(self):
        self.api.post_results = [
            (200, {"success": True, "restaurant_id": 1}),
            (200, {"success": True, "restaurant_id": 1}),
        ]
        plugin = self.plugin()
        alice = self.event(sender_id="111", sender_name="Alice")
        bob = self.event(sender_id="222", sender_name="Bob")
        run(
            plugin.tool_submit_food_review(
                alice, valid_submit_args(restaurant="A火锅"), "帮我添加评价"
            )
        )
        run(
            plugin.tool_submit_food_review(
                bob, valid_submit_args(restaurant="B烤肉"), "帮我添加评价"
            )
        )
        call_confirm(plugin, self.event("确认", sender_id="111", sender_name="Alice"))
        self.assertEqual(self.api.post_calls, 1)
        self.assertEqual(self.api.post_payloads[0]["name"], "A火锅")
        call_confirm(plugin, self.event("确认", sender_id="222", sender_name="Bob"))
        self.assertEqual(self.api.post_calls, 2)
        self.assertEqual(self.api.post_payloads[1]["name"], "B烤肉")

    def test_draft_ttl_expires_confirmation(self):
        now = [1000.0]

        def clock():
            return now[0]

        plugin = make_plugin(
            Path(self.tmp.name),
            config={
                "food_submit_draft_ttl_seconds": 60,
                "food_submit_cooldown_seconds": 0,
            },
            api=self.api,
            clock=clock,
        )
        self._draft(plugin)
        now[0] = 1100.0
        confirm = self.event("确认")
        results = call_confirm(plugin, confirm)
        self.assertEqual(results, [])
        self.assertFalse(confirm.stopped)
        self.assertEqual(self.api.post_calls, 0)

    def test_submit_uses_today_tags_and_no_password_leak(self):
        plugin = self.plugin()
        sender = "123456789"
        draft_event = self.event(sender_id=sender, sender_name="小明")
        run(
            plugin.tool_submit_food_review(
                draft_event, valid_submit_args(), "帮我添加评价"
            )
        )
        # Confirmation reply must not contain the sender id used as password.
        confirm = self.event("确认", sender_id=sender, sender_name="小明")
        results = call_confirm(plugin, confirm)
        self.assertTrue(results)
        self.assertEqual(self.api.post_calls, 1)
        payload = self.api.post_payloads[0]
        self.assertEqual(payload["password"], sender)
        self.assertEqual(payload["username"], "小明")
        self.assertEqual(payload["tags"], ["火锅", "夜宵"])
        self.assertEqual(
            payload["review_date"], datetime.date.today().isoformat()
        )
        visible = results[0].text
        self.assertNotIn(sender, visible)
        state_text = (Path(self.tmp.name) / "food_state.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(sender, state_text)
        self.assertNotIn(sender, "\n".join(LOGGER.records))

    def test_username_fallback_is_used(self):
        plugin = self.plugin(food_submit_username_fallback="匿名群友")
        event = self.event(sender_name="")
        run(
            plugin.tool_submit_food_review(
                event, valid_submit_args(), "帮我添加评价"
            )
        )
        call_confirm(plugin, self.event("确认", sender_name=""))
        self.assertEqual(self.api.post_payloads[0]["username"], "匿名群友")

    def test_group_location_is_merged_into_submit_payload(self):
        plugin = self.plugin(
            food_group_locations=json.dumps({"10001": "吉林省/长春市"})
        )
        event = self.event(private=False)
        run(
            plugin.tool_submit_food_review(
                event,
                valid_submit_args(location="南关区"),
                "帮我添加评价",
            )
        )
        confirm = self.event("确认", private=False)
        results = call_confirm(plugin, confirm)
        self.assertTrue(results)
        self.assertEqual(self.api.post_payloads[0]["location_id"], 10)
        location_queries = [
            params.get("q")
            for path, params in self.api.get_calls
            if path == food_engine.FOOD_LOCATIONS_PATH
        ]
        self.assertIn("吉林省/长春市/南关区", location_queries)

    def test_custom_submit_success_reply_config_is_honored(self):
        plugin = self.plugin(
            food_submit_success_reply="OK:{name}/{taste}/{value}"
        )
        self._draft(plugin)
        results = call_confirm(plugin, self.event("确认"))
        self.assertEqual(results[0].text, "OK:A火锅/4.5/3.5")

    def test_confirm_without_draft_does_not_hijack_message(self):
        plugin = self.plugin()
        event = self.event("确认")
        results = call_confirm(plugin, event)
        self.assertEqual(results, [])
        self.assertFalse(event.stopped)
        self.assertEqual(event.should_call_llm_values, [])

    def test_slash_confirmation_is_not_consumed(self):
        plugin = self.plugin()
        self._draft(plugin)
        event = self.event("/确认")
        results = call_confirm(plugin, event)
        self.assertEqual(results, [])
        self.assertEqual(self.api.post_calls, 0)


if __name__ == "__main__":
    unittest.main()
