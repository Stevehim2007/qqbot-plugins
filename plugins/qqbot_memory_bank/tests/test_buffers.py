from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import buffers


class BufferStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "buffers.json"

    def test_batch_counts_messages_from_different_senders(self):
        store = buffers.BufferStore(self.path)
        batches = []
        for idx in range(4):
            batches = store.add_turn(
                "group:g1",
                actor_hash=f"h{idx}",
                actor_label=f"user{idx}",
                text=f"msg{idx}",
                every=4,
            )
            if idx < 3:
                self.assertEqual(batches, [])
        self.assertEqual(len(batches), 4)
        self.assertEqual({label for _h, label, _t in batches}, {"user0", "user1", "user2", "user3"})
        self.assertEqual(store.turns["group:g1"], [])

    def test_same_sender_messages_stay_together(self):
        store = buffers.BufferStore(self.path)
        batches = []
        for idx in range(4):
            batches = store.add_turn(
                "group:g1", actor_hash="h1", actor_label="alice", text=f"m{idx}", every=4
            )
        self.assertEqual(len(batches), 1)
        actor_hash, label, texts = batches[0]
        self.assertEqual((actor_hash, label), ("h1", "alice"))
        self.assertEqual(texts, ["m0", "m1", "m2", "m3"])

    def test_buffers_survive_a_restart(self):
        store = buffers.BufferStore(self.path)
        store.add_turn("group:g1", actor_hash="h1", actor_label="a", text="一", every=4)
        store.add_turn("group:g1", actor_hash="h1", actor_label="a", text="二", every=4)
        store.add_digest_line("umo:1", "a：你好", every=20)

        fresh = buffers.BufferStore(self.path)
        batches = []
        for text in ("三", "四"):
            batches = fresh.add_turn(
                "group:g1", actor_hash="h1", actor_label="a", text=text, every=4
            )
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][2], ["一", "二", "三", "四"])
        self.assertEqual(fresh.digests["umo:1"], ["a：你好"])

    def test_digest_fires_at_threshold(self):
        store = buffers.BufferStore(self.path)
        out = []
        for idx in range(3):
            out = store.add_digest_line("umo:1", f"line{idx}", every=3)
        self.assertEqual(out, ["line0", "line1", "line2"])
        self.assertEqual(store.digests["umo:1"], [])


if __name__ == "__main__":
    unittest.main()
