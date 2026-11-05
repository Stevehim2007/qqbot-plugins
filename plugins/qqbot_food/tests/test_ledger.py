from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from support import food_engine
from qqbot_food import contracts
from qqbot_food.ledger import FoodLedger


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


PAYLOAD = {
    "restaurant": "A火锅",
    "location_id": 10,
    "location_text": "",
    "taste": 4.5,
    "value": 3.5,
    "comment": "好吃",
    "tags": ["火锅"],
    "tag_ids": [],
}


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.path = Path(self.tmp.name) / "food_ledger.sqlite3"
        self.ledger = FoodLedger(self.path, clock=self.clock)

    def create_draft(self, *, actor="111", scope="scope-1", persona="p1", payload=None, status="draft", missing=None):
        payload = dict(payload or PAYLOAD)
        result = self.ledger.create_draft(
            draft_id="d1",
            actor_id=actor,
            instance_id="i",
            platform="aiocqhttp",
            bot_id="b",
            scope_key=scope,
            persona_id=persona,
            payload=payload,
            payload_hash_value=contracts.payload_hash(payload),
            revision=1,
            status=status,
            missing_fields=list(missing or []),
            ttl_seconds=60,
            now=self.clock.value,
        )
        self.assertIn(result["kind"], {"created", "updated", "replay"})
        return result["draft"]

    def preview(self, draft, actor="111", scope="scope-1", persona="p1"):
        result = self.ledger.preview_draft(
            draft["draft_id"],
            actor_id=actor,
            scope_key=scope,
            persona_id=persona,
            confirmation_ttl_seconds=60,
            now=self.clock.value,
        )
        self.assertEqual(result["kind"], "ready")
        token, expires = "token-value", self.clock.value + 60
        self.assertTrue(
            self.ledger.store_confirmation_token(
                draft["draft_id"],
                actor_id=actor,
                scope_key=scope,
                persona_id=persona,
                token=token,
                expires_at=expires,
                now=self.clock.value,
            )
        )
        return result["draft"], token


class DraftLifecycleTests(LedgerTestBase):
    def test_create_get_and_payload_hash(self):
        created = self.create_draft()
        self.assertEqual(created["status"], "draft")
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["payload_hash"], contracts.payload_hash(PAYLOAD))
        loaded = self.ledger.get_draft("d1")
        self.assertEqual(loaded["payload"]["restaurant"], "A火锅")

    def test_ttl_expires_before_confirmation(self):
        self.create_draft()
        self.clock.value = 1000 + 61
        draft = self.ledger.get_draft("d1")
        self.assertEqual(draft["status"], "expired")
        preview = self.ledger.preview_draft(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            confirmation_ttl_seconds=60,
            now=self.clock.value,
        )
        self.assertEqual(preview["kind"], "expired")

    def test_preview_moves_draft_to_awaiting_confirmation(self):
        draft = self.create_draft()
        preview = self.ledger.preview_draft(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            confirmation_ttl_seconds=30,
            now=self.clock.value,
        )
        self.assertEqual(preview["kind"], "ready")
        self.assertEqual(preview["draft"]["status"], "awaiting_confirmation")
        self.assertEqual(preview["draft"]["confirmation_expires_at"], 1030)

    def test_update_draft_revision_and_payload(self):
        self.create_draft()
        payload = dict(PAYLOAD)
        payload["comment"] = "改过的评价"
        result = self.ledger.create_draft(
            draft_id="d1",
            actor_id="111",
            instance_id="i",
            platform="aiocqhttp",
            bot_id="b",
            scope_key="scope-1",
            persona_id="p1",
            payload=payload,
            payload_hash_value=contracts.payload_hash(payload),
            revision=1,
            status="draft",
            missing_fields=[],
            ttl_seconds=60,
            update_draft_id="d1",
            now=self.clock.value,
        )
        self.assertEqual(result["kind"], "updated")
        self.assertEqual(result["draft"]["revision"], 2)
        self.assertNotEqual(result["draft"]["payload_hash"], contracts.payload_hash(PAYLOAD))

    def test_cancel_draft_and_wrong_actor(self):
        self.create_draft()
        forbidden = self.ledger.cancel_draft(
            "d1",
            actor_id="222",
            scope_key="scope-1",
            persona_id="p1",
            now=self.clock.value,
        )
        self.assertEqual(forbidden["kind"], "forbidden")
        result = self.ledger.cancel_draft(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            now=self.clock.value,
        )
        self.assertEqual(result["kind"], "cancelled")
        self.assertEqual(self.ledger.get_draft("d1")["status"], "cancelled")

    def test_cancel_submitting_is_rejected(self):
        draft = self.create_draft()
        self.preview(draft)
        self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-1",
            now=self.clock.value,
        )
        result = self.ledger.cancel_draft(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            now=self.clock.value,
        )
        self.assertEqual(result["kind"], "invalid_state")


class BeginSubmitTests(LedgerTestBase):
    def test_cas_claim_and_finish_success(self):
        draft = self.create_draft()
        self.preview(draft)
        begin = self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-1",
            now=self.clock.value,
        )
        self.assertEqual(begin["kind"], "claimed")
        self.assertEqual(begin["operation"]["status"], "submitting")
        self.assertEqual(self.ledger.get_draft("d1")["status"], "submitting")
        finished = self.ledger.finish_operation(
            "op-1",
            status="succeeded",
            draft_status="succeeded",
            side_effect_state="committed",
            remote_code="SUCCESS",
            remote_data={"success": True},
            now=self.clock.value,
        )
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(self.ledger.get_draft("d1")["status"], "succeeded")

    def test_same_idempotency_key_same_payload_replay(self):
        draft = self.create_draft()
        self.preview(draft)
        first = self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-1",
            now=self.clock.value,
        )
        self.assertEqual(first["kind"], "claimed")
        second = self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-2",
            now=self.clock.value,
        )
        self.assertEqual(second["kind"], "replay")
        self.assertEqual(second["operation"]["op_id"], "op-1")

    def test_same_idempotency_key_different_payload_conflicts(self):
        draft = self.create_draft()
        self.preview(draft)
        first = self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-1",
            now=self.clock.value,
        )
        self.assertEqual(first["kind"], "claimed")
        second = self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value="different-hash",
            op_id="op-2",
            now=self.clock.value,
        )
        self.assertEqual(second["kind"], "conflict")

    def test_wrong_revision_and_wrong_actor_do_not_claim(self):
        draft = self.create_draft()
        self.preview(draft)
        wrong_rev = self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=9,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-1",
            now=self.clock.value,
        )
        self.assertEqual(wrong_rev["kind"], "revision_conflict")
        wrong_actor = self.ledger.begin_submit(
            "d1",
            actor_id="222",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-2",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-2",
            now=self.clock.value,
        )
        self.assertEqual(wrong_actor["kind"], "forbidden")

    def test_threaded_cas_has_one_claimed_winner(self):
        draft = self.create_draft()
        self.preview(draft)
        barrier = threading.Barrier(6)
        results = []
        lock = threading.Lock()

        def worker(index):
            barrier.wait()
            result = self.ledger.begin_submit(
                "d1",
                actor_id="111",
                scope_key="scope-1",
                persona_id="p1",
                expected_revision=1,
                expected_payload_hash=contracts.payload_hash(PAYLOAD),
                idempotency_key="idem-shared",
                payload_hash_value=contracts.payload_hash(PAYLOAD),
                op_id=f"op-{index}",
                now=1000.0 + index / 100.0,
            )
            with lock:
                results.append(result["kind"])

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("claimed"), 1)
        self.assertTrue(all(kind in {"claimed", "replay", "in_progress", "conflict"} for kind in results))


class RecoveryAndPreferencesTests(LedgerTestBase):
    def test_crash_during_submitting_recovers_as_outcome_unknown(self):
        draft = self.create_draft()
        self.preview(draft)
        self.ledger.begin_submit(
            "d1",
            actor_id="111",
            scope_key="scope-1",
            persona_id="p1",
            expected_revision=1,
            expected_payload_hash=contracts.payload_hash(PAYLOAD),
            idempotency_key="idem-1",
            payload_hash_value=contracts.payload_hash(PAYLOAD),
            op_id="op-1",
            now=self.clock.value,
        )
        self.ledger.close()
        recovered = FoodLedger(self.path, clock=self.clock)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.get_draft("d1")["status"], "outcome_unknown")
        operation = recovered.get_operation("op-1")
        self.assertEqual(operation["status"], "outcome_unknown")
        self.assertEqual(operation["side_effect_state"], "unknown")
        self.assertFalse(operation["retryable"])
        # A late success callback must not overwrite the unknown outcome.
        late = recovered.finish_operation(
            "op-1",
            status="succeeded",
            draft_status="succeeded",
            side_effect_state="committed",
            now=self.clock.value,
        )
        self.assertEqual(late["status"], "outcome_unknown")
        self.assertEqual(recovered.get_draft("d1")["status"], "outcome_unknown")

    def test_scope_preferences_are_isolated(self):
        self.ledger.set_scope_preference(
            instance_id="i",
            platform="aiocqhttp",
            bot_id="b",
            scope_key="scope-1",
            location={"location_id": 10, "name": "南关区"},
            tags=[1, 2],
        )
        first = self.ledger.get_scope_preference(
            instance_id="i", platform="aiocqhttp", bot_id="b", scope_key="scope-1"
        )
        second = self.ledger.get_scope_preference(
            instance_id="i", platform="aiocqhttp", bot_id="b", scope_key="scope-2"
        )
        self.assertEqual(first["location"]["location_id"], 10)
        self.assertEqual(first["tags"], [1, 2])
        self.assertEqual(second, {"location": {}, "tags": []})

    def test_ledger_file_is_private_and_persistent(self):
        self.create_draft()
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.ledger.close()
        reopened = FoodLedger(self.path, clock=self.clock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_draft("d1")["payload"]["restaurant"], "A火锅")


if __name__ == "__main__":
    unittest.main()
