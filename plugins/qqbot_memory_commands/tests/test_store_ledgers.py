from __future__ import annotations

import concurrent.futures
import sqlite3
import sys
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_DIR.parent
for path in (str(PLUGINS_ROOT), str(Path(__file__).resolve().parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

from qqbot_memory_commands import contracts  # noqa: E402
from qqbot_memory_commands.store import OperationStore  # noqa: E402
from _support import make_context  # noqa: E402


def scope_for(context):
    return contracts.ScopeRef(
        "user",
        context.actor_id,
        context.instance_id,
        context.platform,
        context.bot_id,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.tokens = iter(f"token-{index}" for index in range(1, 100))
        self.store = OperationStore(
            ":memory:",
            clock=lambda: self.now[0],
            token_factory=lambda: next(self.tokens),
        )
        self.context = make_context("memory_confirm_clear")
        self.scope = scope_for(self.context)

    def tearDown(self):
        self.store.close()


class TestIdempotencyLedger(StoreTestCase):
    def test_same_key_same_payload_replays_original_result(self):
        first = self.store.begin_idempotent(
            self.context,
            operation="memory_add",
            business_key="idem-1",
            payload_hash="payload-a",
            scope_key=self.scope.canonical_id,
        )
        self.assertEqual(first["status"], "proceed")
        result = contracts.make_result("ok", data={"id": 7, "key": "k"})
        self.store.complete_idempotent(
            self.context,
            operation="memory_add",
            business_key="idem-1",
            payload_hash="payload-a",
            scope_key=self.scope.canonical_id,
            result=result,
        )
        replay = self.store.begin_idempotent(
            self.context,
            operation="memory_add",
            business_key="idem-1",
            payload_hash="payload-a",
            scope_key=self.scope.canonical_id,
        )
        self.assertEqual(replay["status"], "ok")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["data"]["id"], 7)

    def test_same_key_different_payload_is_409(self):
        self.store.begin_idempotent(
            self.context,
            operation="memory_add",
            business_key="idem-1",
            payload_hash="payload-a",
            scope_key=self.scope.canonical_id,
        )
        result = self.store.begin_idempotent(
            self.context,
            operation="memory_add",
            business_key="idem-1",
            payload_hash="payload-b",
            scope_key=self.scope.canonical_id,
        )
        self.assertEqual(result["status"], "revision_conflict")
        self.assertEqual(result["error_code"], "idempotency_payload_mismatch")

    def test_pending_idempotency_is_outcome_unknown_not_retried(self):
        self.store.begin_idempotent(
            self.context,
            operation="session_reset",
            business_key="idem-2",
            payload_hash="payload",
            scope_key=self.scope.canonical_id,
        )
        result = self.store.begin_idempotent(
            self.context,
            operation="session_reset",
            business_key="idem-2",
            payload_hash="payload",
            scope_key=self.scope.canonical_id,
        )
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["error_code"], "idempotency_in_progress")

    def test_same_key_is_scoped_by_actor_scope_and_operation(self):
        self.store.begin_idempotent(
            self.context,
            operation="memory_add",
            business_key="shared-key",
            payload_hash="payload-a",
            scope_key=self.scope.canonical_id,
        )
        result = self.store.begin_idempotent(
            self.context,
            operation="memory_add",
            business_key="shared-key",
            payload_hash="payload-b",
            scope_key="different-scope",
        )
        self.assertEqual(result["status"], "proceed")


class TestConfirmationLedger(StoreTestCase):
    def _create(self, *, actor=None, ttl=60):
        context = actor or self.context
        return self.store.create_confirmation(
            context,
            scope_key=self.scope.canonical_id,
            scope_view=self.scope.to_dict(),
            persona_id=context.persona_id,
            payload_hash="payload-hash",
            revision=3,
            ttl_seconds=ttl,
            metadata={"count": 2, "content": "must-not-leak"},
        )

    def test_confirmation_is_bound_and_single_use(self):
        created = self._create()
        token = created["data"]["confirmation_token"]
        consumed = self.store.consume_confirmation(
            token,
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(consumed["status"], "ok")
        self.assertEqual(consumed["data"]["count"], 2)
        replay = self.store.consume_confirmation(
            token,
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(replay["status"], "confirmation_expired")
        self.assertEqual(replay["error_code"], "confirmation_replayed")

    def test_confirmation_rejects_other_actor_scope_payload_and_stale_revision(self):
        created = self._create()
        token = created["data"]["confirmation_token"]
        other_actor = make_context("memory_confirm_clear", actor="actor-2")
        actor_result = self.store.consume_confirmation(
            token,
            other_actor,
            scope_key=self.scope.canonical_id,
            persona_id=other_actor.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(actor_result["status"], "permission_denied")
        scope_result = self.store.consume_confirmation(
            token,
            self.context,
            scope_key="other-scope",
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(scope_result["error_code"], "confirmation_scope_mismatch")
        payload_result = self.store.consume_confirmation(
            token,
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="changed-payload",
            revision=3,
        )
        self.assertEqual(payload_result["status"], "revision_conflict")
        self.assertEqual(payload_result["error_code"], "confirmation_payload_mismatch")
        revision_result = self.store.consume_confirmation(
            token,
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=4,
        )
        self.assertEqual(revision_result["status"], "revision_conflict")
        self.assertEqual(revision_result["error_code"], "stale_revision")

    def test_confirmation_expires_and_cancellation_is_terminal(self):
        expiring = self._create(ttl=5)
        token = expiring["data"]["confirmation_token"]
        self.now[0] += 6
        expired = self.store.consume_confirmation(
            token,
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(expired["status"], "confirmation_expired")
        self.assertEqual(expired["error_code"], "confirmation_expired")

        created = self._create()
        token2 = created["data"]["confirmation_token"]
        cancelled = self.store.cancel_confirmation(token2, self.context)
        self.assertEqual(cancelled["status"], "ok")
        self.assertTrue(cancelled["data"]["cancelled"])
        replay = self.store.consume_confirmation(
            token2,
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(replay["status"], "confirmation_expired")

    def test_confirmation_unknown_token_is_explicit_expiry(self):
        result = self.store.consume_confirmation(
            "missing",
            self.context,
            scope_key=self.scope.canonical_id,
            persona_id=self.context.persona_id,
            payload_hash="payload-hash",
            revision=3,
        )
        self.assertEqual(result["status"], "confirmation_expired")
        self.assertEqual(result["error_code"], "confirmation_not_found")


class TestAppendLedger(StoreTestCase):
    def _begin(self, *, business_key="append:turn-1:user:0", payload_hash="p", content_hash="c"):
        return self.store.begin_append(
            self.context,
            operation="session_append",
            business_key=business_key,
            payload_hash=payload_hash,
            scope_key=self.scope.canonical_id,
            conversation_id=self.context.conversation_id,
            persona_id=self.context.persona_id,
            turn_id=self.context.turn_id,
            role="user",
            sequence=0,
        )

    def test_duplicate_concurrent_append_has_one_authoritative_intent(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: self._begin(), range(2)))
        statuses = sorted(result["status"] for result in results)
        self.assertEqual(statuses, ["outcome_unknown", "proceed"])
        self.store.complete_append(
            self.context,
            operation="session_append",
            business_key="append:turn-1:user:0",
            payload_hash="p",
            scope_key=self.scope.canonical_id,
            conversation_id=self.context.conversation_id,
            persona_id=self.context.persona_id,
            turn_id=self.context.turn_id,
            role="user",
            sequence=0,
            result=contracts.make_result("ok", data={"message_count": 1}),
        )
        replay = self._begin()
        self.assertEqual(replay["status"], "ok")
        self.assertTrue(replay["idempotent_replay"])

    def test_unique_turn_key_survives_a_failed_append_as_incomplete(self):
        self.assertEqual(self._begin(payload_hash="p1")["status"], "proceed")
        self.store.complete_append(
            self.context,
            operation="session_append",
            business_key="append:turn-1:user:0",
            payload_hash="p1",
            scope_key=self.scope.canonical_id,
            conversation_id=self.context.conversation_id,
            persona_id=self.context.persona_id,
            turn_id=self.context.turn_id,
            role="user",
            sequence=0,
            result=contracts.error_result(
                "outcome_unknown",
                "backend_error",
                side_effect_state="unknown",
            ),
        )
        incomplete = self.store.incomplete_turns(
            self.context,
            scope_key=self.scope.canonical_id,
            conversation_id=self.context.conversation_id,
            persona_id=self.context.persona_id,
        )
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]["status"], "unknown")
        # A different idempotency key cannot bypass the turn unique key.
        conflict = self.store.begin_append(
            self.context,
            operation="session_append",
            business_key="append:turn-1:user:0:other",
            payload_hash="p2",
            scope_key=self.scope.canonical_id,
            conversation_id=self.context.conversation_id,
            persona_id=self.context.persona_id,
            turn_id=self.context.turn_id,
            role="user",
            sequence=0,
        )
        self.assertEqual(conflict["status"], "revision_conflict")
        self.assertEqual(conflict["error_code"], "turn_key_conflict")

    def test_same_turn_key_different_content_is_conflict(self):
        self._begin(payload_hash="p1")
        conflict = self._begin(payload_hash="p2")
        self.assertEqual(conflict["status"], "revision_conflict")
        self.assertEqual(conflict["error_code"], "idempotency_payload_mismatch")


class TestAuditRedaction(StoreTestCase):
    def test_audit_drops_bodies_and_secrets(self):
        self.store.append_audit(
            self.context,
            operation="memory.add",
            scope_key=self.scope.canonical_id,
            status="ok",
            summary={
                "id": 9,
                "count": 1,
                "content": "secret memory body",
                "body": "secret",
                "text": "secret",
                "password": "hunter2",
                "token": "token-secret",
                "nested": {"content": "secret"},
            },
        )
        rows = self.store.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["summary"], {"id": 9, "count": 1})
        raw = self.store._conn.execute(
            "SELECT summary_json FROM audit"
        ).fetchone()[0]
        for secret in ("secret memory body", "hunter2", "token-secret", "nested"):
            self.assertNotIn(secret, raw)


if __name__ == "__main__":
    unittest.main()
