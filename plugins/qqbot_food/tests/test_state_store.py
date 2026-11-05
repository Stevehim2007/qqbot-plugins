from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from support import food_state


class Clock:
    def __init__(self, value=1_000.0):
        self.value = value

    def __call__(self):
        return self.value


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.path = Path(self.tmp.name) / "food_state.json"
        self.store = food_state.FoodStateStore(
            self.path, draft_ttl=60, clock=self.clock, use_file_lock=False
        )

    def test_state_key_is_deterministic_and_hides_sender(self):
        first = food_state.state_key("umo:group:1", "123456789")
        second = food_state.state_key("umo:group:1", "123456789")
        other = food_state.state_key("umo:group:1", "987654321")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotIn("123456789", first)
        self.assertEqual(len(first), 64)

    def test_save_get_and_delete_draft(self):
        saved = self.store.save_draft("k1", {"restaurant": "店", "comment": "好吃"})
        self.assertEqual(saved["status"], "pending_confirmation")
        loaded = self.store.get_draft("k1")
        self.assertEqual(loaded["fields"]["restaurant"], "店")
        self.store.delete_draft("k1")
        self.assertIsNone(self.store.get_draft("k1"))

    def test_extra_secret_field_is_never_persisted(self):
        self.store.save_draft(
            "k1",
            {
                "restaurant": "店",
                "comment": "x",
                "password": "123456789",
                "sender_id": "123456789",
            },
        )
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("123456789", raw)
        self.assertIn("店", raw)

    def test_ttl_prunes_draft(self):
        self.clock.value = 1000
        self.store.save_draft("k1", {"restaurant": "店"})
        self.clock.value = 1030
        self.assertIsNotNone(self.store.get_draft("k1"))
        self.clock.value = 1070
        self.assertIsNone(self.store.get_draft("k1"))

    def test_submitted_draft_retained_longer_than_ttl(self):
        self.clock.value = 1000
        self.store.save_draft("k1", {"restaurant": "店"})
        self.store.mark_submitted("k1")
        self.clock.value = 1400
        self.assertIsNotNone(self.store.get_draft("k1"))
        self.clock.value = 5000
        self.assertIsNone(self.store.get_draft("k1"))

    def test_merge_draft_resets_on_different_restaurant(self):
        self.store.merge_draft("k1", {"restaurant": "A", "location": "长春市"})
        merged = self.store.merge_draft("k1", {"restaurant": "B", "taste": 4})
        self.assertEqual(merged["fields"]["restaurant"], "B")
        self.assertNotIn("location", merged["fields"])
        self.assertEqual(merged["fields"]["taste"], 4)

    def test_merge_draft_keeps_missing_field_completion(self):
        self.store.merge_draft("k1", {"restaurant": "A", "location": "长春市"})
        merged = self.store.merge_draft("k1", {"taste": 4.5, "value": 3})
        self.assertEqual(merged["fields"]["restaurant"], "A")
        self.assertEqual(merged["fields"]["location"], "长春市")
        self.assertEqual(merged["fields"]["taste"], 4.5)

    def test_claim_prevents_double_submit(self):
        self.store.save_draft("k1", {"restaurant": "店"})
        self.assertEqual(self.store.claim_submit("k1"), "claimed")
        self.assertEqual(self.store.claim_submit("k1"), "submitting")
        self.store.mark_submitted("k1")
        self.assertEqual(self.store.claim_submit("k1"), "submitted")
        self.assertEqual(self.store.claim_submit("missing"), "missing")

    def test_restore_pending_allows_retry_after_definite_failure(self):
        self.store.save_draft("k1", {"restaurant": "店"})
        self.assertEqual(self.store.claim_submit("k1"), "claimed")
        self.store.restore_pending("k1")
        self.assertEqual(self.store.get_draft("k1")["status"], "pending_confirmation")
        self.assertEqual(self.store.claim_submit("k1"), "claimed")

    def test_threaded_claim_has_exactly_one_winner(self):
        self.store.save_draft("k1", {"restaurant": "店"})
        results: list[str] = []
        lock = threading.Lock()

        def worker():
            value = self.store.claim_submit("k1")
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("submitting"), 11)

    def test_cooldown(self):
        self.store.record_submit_attempt("k1")
        self.assertEqual(self.store.cooldown_remaining("k1", 60), 60)
        self.clock.value += 30
        self.assertEqual(self.store.cooldown_remaining("k1", 60), 30)
        self.clock.value += 31
        self.assertEqual(self.store.cooldown_remaining("k1", 60), 0)
        self.assertEqual(self.store.cooldown_remaining("k1", 0), 0)

    def test_atomic_file_is_valid_json_and_private(self):
        self.store.save_draft("k1", {"restaurant": "店"})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertIn("k1", data["drafts"])
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_corrupt_state_file_fails_safe(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.get_draft("k1"))
        self.store.save_draft("k1", {"restaurant": "店"})
        self.assertIsNotNone(self.store.get_draft("k1"))

    def test_two_sender_keys_are_isolated(self):
        a = food_state.state_key("umo", "111")
        b = food_state.state_key("umo", "222")
        self.store.save_draft(a, {"restaurant": "A"})
        self.store.save_draft(b, {"restaurant": "B"})
        self.assertEqual(self.store.get_draft(a)["fields"]["restaurant"], "A")
        self.assertEqual(self.store.get_draft(b)["fields"]["restaurant"], "B")


if __name__ == "__main__":
    unittest.main()
