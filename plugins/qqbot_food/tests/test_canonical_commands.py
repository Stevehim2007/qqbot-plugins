from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from support import FakeEvent, FakeFoodAPI, collect_async, make_plugin, run

LOCATIONS = [
    {"id": 11, "name": "长春市", "path": "中国/吉林省/长春市"},
    {"id": 10, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
]
TAGS = [{"id": 1, "name": "火锅"}, {"id": 2, "name": "烤肉"}]
RESTAURANTS = [
    {"id": 101, "name": "A火锅", "location_id": 10, "location_name": "南关区", "tags": ["火锅"], "total_avg": 8.8, "review_count": 3},
    {"id": 102, "name": "B烤肉", "location_id": 10, "location_name": "南关区", "tags": ["烤肉"], "total_avg": 7.0, "review_count": 1},
]
DRAFT_RE = re.compile(r"草稿已就绪：([0-9a-f]{32})")
OP_RE = re.compile(r"操作ID：([0-9a-f]{32})")


def call_command(plugin, event):
    return run(collect_async(plugin.food_command(event)))


class CanonicalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cloud = [1000.0]
        self.api = FakeFoodAPI(
            locations=LOCATIONS, tags=TAGS, restaurants=RESTAURANTS
        )
        self.plugin = make_plugin(
            Path(self.tmp.name),
            config={"drafts_confirmation_ttl_seconds": 60},
            api=self.api,
            clock=lambda: self.cloud[0],
        )
        self.plugin._confirmation_issuer.ttl_seconds = 60

    def event(self, text, *, sender_id="111", private=True):
        return FakeEvent(
            text, sender_id=sender_id, sender_name="小明", private=private
        )

    def submit_draft(self):
        event = self.event(
            "/food 提交 店名=A火锅 位置ID=10 味道=4.5 性价比=3.5 评价=好吃 标签=火锅"
        )
        results = call_command(self.plugin, event)
        self.assertTrue(results)
        match = DRAFT_RE.search(results[0].text)
        self.assertIsNotNone(match, results[0].text)
        return match.group(1), results[0].text

    def test_registration_has_m美食_alias(self):
        _args, kwargs = self.plugin.food_command._qqbot_food_command
        self.assertEqual(set(kwargs["alias"]), {"美食", "吃什么", "饭店"})

    def test_query_subcommand_and_meishi_alias(self):
        for text in ("/food 查询 火锅", "/美食 查询 火锅"):
            event = self.event(text)
            results = call_command(self.plugin, event)
            self.assertTrue(results, text)
            self.assertIn("A火锅", results[0].text)
            self.assertTrue(event.stopped)
            self.assertEqual(event.should_call_llm_values[-1], True)

    def test_legacy_search_and_old_aliases_still_work(self):
        for text in ("/food 火锅", "/吃什么 火锅", "/饭店 火锅"):
            event = self.event(text)
            results = call_command(self.plugin, event)
            self.assertTrue(results, text)
            self.assertIn("A火锅", results[0].text)

    def test_locations_subcommand_shows_stable_id(self):
        event = self.event("/美食 位置 南关区")
        results = call_command(self.plugin, event)
        self.assertTrue(results)
        self.assertIn("南关区", results[0].text)
        self.assertIn("ID:10", results[0].text)

    def test_tags_subcommand_shows_stable_id(self):
        event = self.event("/food 标签 火锅")
        results = call_command(self.plugin, event)
        self.assertTrue(results)
        self.assertIn("火锅", results[0].text)
        self.assertIn("ID:1", results[0].text)

    def test_submit_preview_confirm_and_status(self):
        draft_id, preview_text = self.submit_draft()
        self.assertIn("请先预览" if False else "草稿已就绪", preview_text)
        self.assertIn("确认", preview_text)
        self.assertEqual(self.api.post_calls, 0)

        confirm_event = self.event(f"/food 确认 {draft_id}")
        confirm_results = call_command(self.plugin, confirm_event)
        self.assertTrue(confirm_results)
        self.assertIn("已提交", confirm_results[0].text)
        self.assertEqual(self.api.post_calls, 1)
        self.assertEqual(self.api.post_payloads[0]["password"], "111")
        op_match = OP_RE.search(confirm_results[0].text)
        self.assertIsNotNone(op_match, confirm_results[0].text)
        op_id = op_match.group(1)

        status_event = self.event(f"/美食 状态 {op_id}")
        status_results = call_command(self.plugin, status_event)
        self.assertTrue(status_results)
        self.assertIn("Food 操作状态", status_results[0].text)
        self.assertIn("succeeded", status_results[0].text)

    def test_confirm_replay_command_does_not_post_twice(self):
        draft_id, _ = self.submit_draft()
        first = call_command(self.plugin, self.event(f"/food 确认 {draft_id}"))
        second = call_command(self.plugin, self.event(f"/food 确认 {draft_id}"))
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertIn("已提交", second[0].text)
        self.assertEqual(self.api.post_calls, 1)

    def test_cancel_subcommand(self):
        draft_id, _ = self.submit_draft()
        results = call_command(self.plugin, self.event(f"/food 取消 {draft_id}"))
        self.assertTrue(results)
        self.assertIn("草稿已取消", results[0].text)
        self.assertEqual(self.api.post_calls, 0)

    def test_wrong_actor_cannot_confirm_canonical_draft(self):
        draft_id, _ = self.submit_draft()
        results = call_command(
            self.plugin, self.event(f"/food 确认 {draft_id}", sender_id="222")
        )
        self.assertTrue(results)
        self.assertIn("无权", results[0].text)
        self.assertEqual(self.api.post_calls, 0)

    def test_missing_arguments_have_terminal_usage(self):
        cases = [
            "/food 位置",
            "/food 标签",
            "/food 提交",
            "/food 确认",
            "/food 取消",
            "/food 状态",
        ]
        for text in cases:
            results = call_command(self.plugin, self.event(text))
            self.assertTrue(results, text)
            self.assertIn("用法", results[0].text)

    def test_unknown_subcommand_falls_back_to_legacy_search(self):
        event = self.event("/food 查询不存在的子命令")
        results = call_command(self.plugin, event)
        self.assertTrue(results)
        # It is treated as a legacy query, not a raw Python/HTTP dispatch.
        self.assertNotIn("Traceback", results[0].text)

    def test_provider_readiness_is_reported(self):
        status = self.plugin.provider_status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["abi_version"], 1)
        self.assertEqual(status["capability"], "food")

    def test_disabled_provider_command_still_terminal(self):
        self.plugin._contract_ready = False
        results = call_command(self.plugin, self.event("/food 查询 火锅"))
        self.assertTrue(results)
        self.assertIn("尚未就绪", results[0].text)


if __name__ == "__main__":
    unittest.main()
