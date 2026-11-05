from __future__ import annotations

import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path

from support import FakeFoodAPI, run, make_plugin, food_engine
from qqbot_food import contracts
from qqbot_food.backend import BackendOutcome


LOCATIONS = [
    {"id": 11, "name": "长春市", "path": "中国/吉林省/长春市"},
    {"id": 10, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
    {"id": 12, "name": "朝阳区", "path": "中国/吉林省/长春市/朝阳区"},
]
TAGS = [
    {"id": 1, "name": "火锅"},
    {"id": 2, "name": "烤肉"},
]
RESTAURANTS = [
    {"id": 101, "name": "A火锅", "location_id": 10, "location_name": "南关区", "tags": ["火锅"], "taste_avg": 4.8, "value_avg": 4.0, "total_avg": 8.8, "review_count": 20, "latest_review_date": "2026-01-03"},
    {"id": 102, "name": "B烤肉", "location_id": 12, "location_name": "朝阳区", "tags": ["烤肉"], "taste_avg": 3.0, "value_avg": 3.5, "total_avg": 6.5, "review_count": 2, "latest_review_date": "2025-01-01"},
    {"id": 103, "name": "C小店", "location_id": 10, "location_name": "南关区", "tags": ["小吃"], "taste_avg": 4.0, "value_avg": 4.1, "total_avg": 8.1, "review_count": 9, "latest_review_date": "2026-02-01"},
]


def ctx(actor="111", *, scope=None, persona="", name="小明"):
    return {
        "protocol_version": 1,
        "instance_id": "instance-a",
        "platform": "aiocqhttp",
        "bot_id": "bot-a",
        "actor_id": actor,
        "conversation_id": "conv-1",
        "persona_id": persona,
        "source_message_id": "msg-1",
        "turn_id": "turn-1",
        "capability": "food",
        "requested_scope": scope or {"kind": "user", "id": actor},
        "owner_epoch": 1,
        "deadline": 0,
        "actor_name": name,
    }


def payload(**overrides):
    value = {
        "restaurant": "A火锅",
        "location_id": 10,
        "taste": "4.5",
        "value": "3.5",
        "comment": "好吃，推荐",
        "tags": ["火锅"],
    }
    value.update(overrides)
    return value


class SlowAPI(FakeFoodAPI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.delay = 0.12

    def post_submit(self, body):
        self.post_calls += 1
        self.post_payloads.append(copy.deepcopy(body))
        time.sleep(self.delay)
        return (200, {"success": True, "restaurant_id": 1, "restaurant_name": str(body.get("name", ""))})


class ProviderTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = [1000.0]

        def now():
            return self.clock[0]

        self.api = FakeFoodAPI(
            locations=LOCATIONS,
            tags=TAGS,
            restaurants=RESTAURANTS,
        )
        self.plugin = make_plugin(
            Path(self.tmp.name),
            config={"drafts_confirmation_ttl_seconds": 60},
            api=self.api,
            clock=now,
        )
        self.plugin._confirmation_issuer.ttl_seconds = 60

    async def create_and_preview(self, *, args=None, actor="111", persona=""):
        created = await self.plugin.food_draft_create(
            ctx(actor, persona=persona), {"payload": args or payload()}
        )
        self.assertEqual(created["status"], "ok", created)
        draft_id = created["data"]["draft_id"]
        preview = await self.plugin.food_draft_preview(
            ctx(actor, persona=persona), {"draft_id": draft_id}
        )
        self.assertEqual(preview["status"], "ok", preview)
        return draft_id, preview["data"]["confirmation_ref"]


class QueryTests(ProviderTestBase):
    def test_query_restaurants_returns_bounded_items_and_stable_ids(self):
        result = run(
            self.plugin.food_query(
                ctx(), {"query_text": "火锅", "limit": 2}
            )
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["abi_version"], 1)
        self.assertLessEqual(len(result["data"]["items"]), 2)
        self.assertEqual(result["data"]["source"], "food.dlsz.top")
        self.assertTrue(all("id" in item for item in result["data"]["items"]))

    def test_query_cursor_pagination_and_tamper_rejection(self):
        first = run(self.plugin.food_query(ctx(), {"limit": 2}))
        self.assertEqual(first["status"], "ok")
        self.assertTrue(first["data"]["cursor"])
        second = run(
            self.plugin.food_query(ctx(), {"limit": 2, "cursor": first["data"]["cursor"]})
        )
        self.assertEqual(second["status"], "ok")
        self.assertNotEqual(
            [item["id"] for item in first["data"]["items"]],
            [item["id"] for item in second["data"]["items"]],
        )
        bad = run(self.plugin.food_query(ctx(), {"limit": 2, "cursor": "bad"}))
        self.assertEqual(bad["status"], "error")
        self.assertEqual(bad["error_code"], contracts.ERR_INVALID_ARGS)

    def test_query_empty_result_is_ok_not_network_error(self):
        self.api.restaurants = []
        result = run(self.plugin.food_query(ctx(), {"query_text": "不存在"}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["items"], [])

    def test_query_location_and_tags_by_name(self):
        result = run(
            self.plugin.food_query(
                ctx(), {"location": "南关区", "tags": ["火锅"], "limit": 5}
            )
        )
        self.assertEqual(result["status"], "ok", result)
        restaurant_calls = [
            params
            for path, params in self.api.get_calls
            if path == "api/restaurants" or path.endswith("/api/restaurants")
        ]
        self.assertTrue(restaurant_calls)
        self.assertEqual(restaurant_calls[-1].get("location_id"), "10")
        self.assertEqual(restaurant_calls[-1].get("tag_ids"), "1")

    def test_query_ambiguous_location_returns_candidates(self):
        self.api.locations = [
            {"id": 1, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
            {"id": 2, "name": "南关区", "path": "中国/辽宁省/沈阳市/南关区"},
        ]
        result = run(self.plugin.food_query(ctx(), {"location": "南关区"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], contracts.ERR_AMBIGUOUS_LOCATION)
        self.assertEqual(len(result["data"]["items"]), 2)

    def test_query_read_timeout_is_retryable_without_side_effect(self):
        from qqbot_food.api import FoodTimeoutError

        self.api.get_errors[food_engine.FOOD_RESTAURANTS_PATH] = [FoodTimeoutError("timeout")]
        result = run(self.plugin.food_query(ctx(), {"query_text": "火锅"}))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], contracts.ERR_BACKEND_TIMEOUT)
        self.assertTrue(result["retryable"])
        self.assertEqual(result["side_effect_state"], "none")


class LocationTagTests(ProviderTestBase):
    def test_locations_keep_ids_and_report_ambiguity(self):
        ok = run(self.plugin.food_locations(ctx(), {"query": "长春市"}))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["data"]["items"][0]["id"], 11)
        self.api.locations = [
            {"id": 1, "name": "南关区", "path": "中国/吉林省/长春市/南关区"},
            {"id": 2, "name": "南关区", "path": "中国/辽宁省/沈阳市/南关区"},
        ]
        ambiguous = run(
            self.plugin.food_locations(ctx(), {"query": "南关区", "require_unique": True})
        )
        self.assertEqual(ambiguous["status"], "error")
        self.assertEqual(ambiguous["error_code"], contracts.ERR_AMBIGUOUS_LOCATION)

    def test_tags_keep_ids_and_report_ambiguity(self):
        ok = run(self.plugin.food_tags(ctx(), {"query": "火锅"}))
        self.assertEqual(ok["status"], "ok")
        self.assertEqual(ok["data"]["items"][0]["id"], 1)
        self.api.tags = [
            {"id": 1, "name": "火锅"},
            {"id": 2, "name": "火锅"},
        ]
        ambiguous = run(
            self.plugin.food_tags(ctx(), {"query": "火锅", "require_unique": True})
        )
        self.assertEqual(ambiguous["status"], "error")
        self.assertEqual(ambiguous["error_code"], contracts.ERR_AMBIGUOUS_TAG)

    def test_scope_location_preference_is_used_and_isolated(self):
        set_result = run(
            self.plugin.food_set_location(ctx("111"), {"location_id": 11})
        )
        self.assertEqual(set_result["status"], "ok")
        query = run(self.plugin.food_query(ctx("111"), {}))
        self.assertEqual(query["status"], "ok")
        restaurant_calls = [
            params for path, params in self.api.get_calls if path.endswith("/api/restaurants")
        ]
        self.assertEqual(restaurant_calls[-1].get("location_id"), "11")
        other = run(self.plugin.food_query(ctx("222"), {}))
        self.assertEqual(other["status"], "ok")
        restaurant_calls = [
            params for path, params in self.api.get_calls if path.endswith("/api/restaurants")
        ]
        self.assertNotIn("location_id", restaurant_calls[-1])

    def test_scope_tag_preference_is_used_and_stable(self):
        set_result = run(
            self.plugin.food_change_tags(ctx("111"), {"tag_ids": [1, 2]})
        )
        self.assertEqual(set_result["status"], "ok")
        self.assertEqual(set_result["data"]["tag_ids"], [1, 2])
        query = run(self.plugin.food_query(ctx("111"), {}))
        self.assertEqual(query["status"], "ok")
        restaurant_calls = [
            params for path, params in self.api.get_calls if path.endswith("/api/restaurants")
        ]
        self.assertEqual(restaurant_calls[-1].get("tag_ids"), "1,2")

    def test_shared_location_write_requires_confirmation(self):
        denied = run(
            self.plugin.food_set_location(
                ctx("111"), {"location_id": 11, "visibility": "scope_shared"}
            )
        )
        self.assertEqual(denied["error_code"], contracts.ERR_CONFIRMATION_REQUIRED)
        preview = run(
            self.plugin.food_set_location(
                ctx("111"),
                {"location_id": 11, "visibility": "scope_shared", "preview": True},
            )
        )
        self.assertEqual(preview["status"], "ok")
        token = preview["data"]["confirmation_ref"]
        allowed = run(
            self.plugin.food_set_location(
                ctx("111"),
                {
                    "location_id": 11,
                    "visibility": "scope_shared",
                    "confirmation_ref": token,
                },
            )
        )
        self.assertEqual(allowed["status"], "ok")


class DraftTests(ProviderTestBase):
    def test_draft_create_collecting_then_complete(self):
        incomplete = run(
            self.plugin.food_draft_create(
                ctx(), {"payload": {"restaurant": "A火锅"}}
            )
        )
        self.assertEqual(incomplete["status"], "ok")
        self.assertEqual(incomplete["data"]["status"], "collecting")
        draft_id = incomplete["data"]["draft_id"]
        self.assertIn("location", incomplete["data"]["missing_fields"])
        preview = run(self.plugin.food_draft_preview(ctx(), {"draft_id": draft_id}))
        self.assertEqual(preview["error_code"], contracts.ERR_DRAFT_INCOMPLETE)
        completed = run(
            self.plugin.food_draft_create(
                ctx(), {"draft_id": draft_id, "payload": payload()}
            )
        )
        self.assertEqual(completed["status"], "ok")
        self.assertEqual(completed["data"]["status"], "draft")
        self.assertEqual(completed["data"]["revision"], 2)
        ready = run(self.plugin.food_draft_preview(ctx(), {"draft_id": draft_id}))
        self.assertEqual(ready["status"], "ok")
        self.assertEqual(ready["data"]["status"], "awaiting_confirmation")
        self.assertTrue(ready["data"]["confirmation_ref"])

    def test_cancel_draft(self):
        created = run(self.plugin.food_draft_create(ctx(), {"payload": payload()}))
        draft_id = created["data"]["draft_id"]
        cancelled = run(self.plugin.food_cancel(ctx(), {"draft_id": draft_id}))
        self.assertEqual(cancelled["status"], "ok")
        self.assertEqual(cancelled["data"]["status"], "cancelled")
        again = run(self.plugin.food_cancel(ctx(), {"draft_id": draft_id}))
        self.assertEqual(again["status"], "ok")
        self.assertEqual(again["data"]["status"], "cancelled")

    def test_wrong_actor_cannot_preview_or_confirm(self):
        created = run(self.plugin.food_draft_create(ctx("111"), {"payload": payload()}))
        draft_id = created["data"]["draft_id"]
        preview = run(self.plugin.food_draft_preview(ctx("222"), {"draft_id": draft_id}))
        self.assertEqual(preview["error_code"], contracts.ERR_PERMISSION_DENIED)
        confirm = run(self.plugin.food_confirm(ctx("222"), {"draft_id": draft_id}))
        self.assertEqual(confirm["error_code"], contracts.ERR_PERMISSION_DENIED)
        self.assertEqual(self.api.post_calls, 0)

    def test_changed_payload_invalidates_old_confirmation(self):
        draft_id, token = run(self.create_and_preview())
        changed = run(
            self.plugin.food_draft_create(
                ctx(),
                {
                    "draft_id": draft_id,
                    "payload": payload(comment="改过的评价"),
                },
            )
        )
        self.assertEqual(changed["data"]["revision"], 2)
        result = run(
            self.plugin.food_confirm(
                ctx(),
                {"draft_id": draft_id, "confirmation_ref": token},
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], contracts.ERR_REVISION_CONFLICT)
        self.assertEqual(self.api.post_calls, 0)

    def test_confirmation_expiry(self):
        draft_id, token = run(self.create_and_preview())
        self.clock[0] += 61
        result = run(
            self.plugin.food_confirm(
                ctx(),
                {"draft_id": draft_id, "confirmation_ref": token},
            )
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], contracts.ERR_CONFIRMATION_EXPIRED)
        self.assertEqual(self.api.post_calls, 0)


class ConfirmTests(ProviderTestBase):
    def test_confirm_success_posts_once_with_actor_password_date_and_tags(self):
        draft_id, token = run(self.create_and_preview())
        result = run(
            self.plugin.food_confirm(
                ctx(),
                {
                    "draft_id": draft_id,
                    "confirmation_ref": token,
                    "idempotency_key": "idem-success",
                },
            )
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["data"]["state"], "succeeded")
        self.assertEqual(self.api.post_calls, 1)
        sent = self.api.post_payloads[0]
        self.assertEqual(sent["password"], "111")
        self.assertEqual(sent["tags"], ["火锅"])
        self.assertIn("review_date", sent)
        self.assertNotIn("111", str(result))

    def test_repeated_confirmation_replays_without_second_post(self):
        draft_id, token = run(self.create_and_preview())
        args = {
            "draft_id": draft_id,
            "confirmation_ref": token,
            "idempotency_key": "idem-replay",
        }
        first = run(self.plugin.food_confirm(ctx(), args))
        second = run(self.plugin.food_confirm(ctx(), args))
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertTrue(second["data"]["replayed"])
        self.assertEqual(self.api.post_calls, 1)

    def test_same_idempotency_key_different_payload_is_409(self):
        first_id, first_token = run(self.create_and_preview())
        first = run(
            self.plugin.food_confirm(
                ctx(),
                {
                    "draft_id": first_id,
                    "confirmation_ref": first_token,
                    "idempotency_key": "shared-key",
                },
            )
        )
        self.assertEqual(first["status"], "ok")
        second = run(
            self.plugin.food_draft_create(
                ctx(), {"payload": payload(restaurant="B烤肉", comment="另一条")}
            )
        )
        second_id = second["data"]["draft_id"]
        second_preview = run(
            self.plugin.food_draft_preview(ctx(), {"draft_id": second_id})
        )
        second_token = second_preview["data"]["confirmation_ref"]
        conflict = run(
            self.plugin.food_confirm(
                ctx(),
                {
                    "draft_id": second_id,
                    "confirmation_ref": second_token,
                    "idempotency_key": "shared-key",
                },
            )
        )
        self.assertEqual(conflict["status"], "error")
        self.assertEqual(conflict["error_code"], contracts.ERR_IDEMPOTENCY_CONFLICT)
        self.assertEqual(conflict["data"]["http_status"], 409)
        self.assertEqual(self.api.post_calls, 1)

    def test_duplicate_username_is_already_submitted_only_with_evidence(self):
        self.api.post_results = [
            (
                400,
                {
                    "code": "DUPLICATE_USERNAME",
                    "error": "用户名「小明」在该店铺已存在",
                },
            )
        ]
        draft_id, token = run(self.create_and_preview())
        result = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["data"]["state"], "succeeded")
        self.assertTrue(result["data"]["remote_data"]["already_submitted"])
        self.assertEqual(result["data"]["remote_code"], "DUPLICATE_USERNAME")
        second = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(second["status"], "ok")
        self.assertEqual(self.api.post_calls, 1)

    def test_timeout_becomes_outcome_unknown_and_is_never_resent(self):
        from qqbot_food.api import FoodTimeoutError

        self.api.post_errors = [FoodTimeoutError("timeout")]
        draft_id, token = run(self.create_and_preview())
        first = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(first["status"], "error")
        self.assertEqual(first["error_code"], contracts.ERR_OUTCOME_UNKNOWN)
        self.assertFalse(first["retryable"])
        self.assertEqual(first["side_effect_state"], "unknown")
        second = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(second["status"], "error")
        self.assertEqual(self.api.post_calls, 1)
        status = run(
            self.plugin.food_operation_status(
                ctx(), {"op_id": first["provider_operation_id"]}
            )
        )
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["data"]["state"], "outcome_unknown")
        self.assertFalse(status["data"]["retryable"])

    def test_500_is_outcome_unknown(self):
        self.api.post_results = [(500, {"error": "server"})]
        draft_id, token = run(self.create_and_preview())
        result = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(result["error_code"], contracts.ERR_OUTCOME_UNKNOWN)
        self.assertEqual(result["side_effect_state"], "unknown")
        self.assertEqual(self.api.post_calls, 1)

    def test_400_rejection_is_retryable_after_edit_with_new_revision(self):
        self.api.post_results = [
            (400, {"code": "MISSING_COMMENT", "error": "评价不能为空"}),
            (200, {"success": True, "restaurant_id": 1}),
        ]
        draft_id, token = run(self.create_and_preview())
        rejected = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(rejected["status"], "error")
        self.assertEqual(rejected["error_code"], "site_missing_field")
        self.assertTrue(rejected["retryable"])
        self.assertEqual(rejected["side_effect_state"], "none")
        updated = run(
            self.plugin.food_draft_create(
                ctx(),
                {"draft_id": draft_id, "payload": payload(comment="补全后的评价")},
            )
        )
        self.assertEqual(updated["data"]["revision"], 2)
        preview = run(self.plugin.food_draft_preview(ctx(), {"draft_id": draft_id}))
        token2 = preview["data"]["confirmation_ref"]
        retried = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token2}
            )
        )
        self.assertEqual(retried["status"], "ok", retried)
        self.assertEqual(self.api.post_calls, 2)

    def test_concurrent_confirm_only_one_remote_post(self):
        self.api = SlowAPI(
            locations=LOCATIONS, tags=TAGS, restaurants=RESTAURANTS
        )
        plugin = make_plugin(
            Path(self.tmp.name) / "concurrent",
            config={"drafts_confirmation_ttl_seconds": 60},
            api=self.api,
            clock=lambda: self.clock[0],
        )
        plugin._confirmation_issuer.ttl_seconds = 60

        async def scenario():
            created = await plugin.food_draft_create(ctx(), {"payload": payload()})
            draft_id = created["data"]["draft_id"]
            preview = await plugin.food_draft_preview(ctx(), {"draft_id": draft_id})
            token = preview["data"]["confirmation_ref"]
            return await asyncio.gather(
                plugin.food_confirm(
                    ctx(), {"draft_id": draft_id, "confirmation_ref": token}
                ),
                plugin.food_confirm(
                    ctx(), {"draft_id": draft_id, "confirmation_ref": token}
                ),
            )

        results = run(scenario())
        successes = [item for item in results if item["status"] == "ok"]
        errors = [item for item in results if item["status"] == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.api.post_calls, 1)

    def test_crash_during_submitting_recovers_unknown_across_restart(self):
        created = run(self.plugin.food_draft_create(ctx(), {"payload": payload()}))
        draft_id = created["data"]["draft_id"]
        preview = run(self.plugin.food_draft_preview(ctx(), {"draft_id": draft_id}))
        token = preview["data"]["confirmation_ref"]
        # Simulate the crash after confirmed->submitting but before the
        # remote result was persisted.
        draft = self.plugin._ledger.get_draft(draft_id)
        self.plugin._ledger.begin_submit(
            draft_id,
            actor_id="111",
            scope_key=contracts.scope_key(contracts.coerce_request_context(ctx())),
            persona_id="",
            expected_revision=draft["revision"],
            expected_payload_hash=draft["payload_hash"],
            idempotency_key="crash-key",
            payload_hash_value=draft["payload_hash"],
            op_id="crash-op",
            now=self.clock[0],
        )
        self.plugin._ledger.close()
        restarted = make_plugin(
            Path(self.tmp.name),
            config={"drafts_confirmation_ttl_seconds": 60},
            api=self.api,
            clock=lambda: self.clock[0],
        )
        status = run(
            restarted.food_operation_status(ctx(), {"op_id": "crash-op"})
        )
        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["data"]["state"], "outcome_unknown")
        confirm = run(
            restarted.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(confirm["status"], "error")
        self.assertEqual(confirm["error_code"], contracts.ERR_OUTCOME_UNKNOWN)
        self.assertEqual(self.api.post_calls, 0)

    def test_operation_status_wrong_actor_denied(self):
        draft_id, token = run(self.create_and_preview())
        result = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        op_id = result["provider_operation_id"]
        denied = run(
            self.plugin.food_operation_status(ctx("222"), {"op_id": op_id})
        )
        self.assertEqual(denied["error_code"], contracts.ERR_PERMISSION_DENIED)

    def test_backend_evidence_mismatch_is_not_already_submitted(self):
        draft_id, token = run(self.create_and_preview())

        async def fake_submit(payload):
            return BackendOutcome(
                state="succeeded",
                remote_code="SUCCESS",
                remote_data={"success": True},
                message="ok",
                side_effect_state="committed",
                already_submitted=True,
                evidence={},
            )

        self.plugin._backend.submit = fake_submit
        result = run(
            self.plugin.food_confirm(
                ctx(), {"draft_id": draft_id, "confirmation_ref": token}
            )
        )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["remote_data"]["already_submitted"])


class ReadinessAndMalformedInputTests(ProviderTestBase):
    def test_malformed_context_and_args_never_raise(self):
        bad_contexts = [None, {}, {"actor_id": "", "instance_id": "i"}]
        for bad in bad_contexts:
            result = run(self.plugin.food_query(bad, {}))
            self.assertEqual(result["status"], "error")
        result = run(self.plugin.food_query(ctx(), "not-a-dict"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], contracts.ERR_INVALID_ARGS)
        result = run(self.plugin.food_draft_create(ctx(), {"payload": {"restaurant": "https://evil"}}))
        self.assertEqual(result["status"], "error")

    def test_provider_not_ready_after_terminate(self):
        async def scenario():
            await self.plugin.terminate()
            return await self.plugin.food_query(ctx(), {})

        result = run(scenario())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], contracts.ERR_NOT_READY)


if __name__ == "__main__":
    unittest.main()
