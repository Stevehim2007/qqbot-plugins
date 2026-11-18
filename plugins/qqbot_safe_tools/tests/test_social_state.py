from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import social_state


class SocialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 10_000.0
        self.store = social_state.SocialStore(
            Path(self.tmp.name) / "social.db", clock=lambda: self.now
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def adjust(self, delta, **kwargs):
        return self.store.adjust(
            "group:test", "actor1", delta=delta, confidence=0.9,
            reason=kwargs.pop("reason", "evidence"), enabled=True,
            cooldown_seconds=0, **kwargs,
        )

    def test_tier_boundaries(self):
        self.assertEqual(social_state.tier_for(0), "neutral")
        self.assertEqual(social_state.tier_for(10), "warm")
        self.assertEqual(social_state.tier_for(40), "close")
        self.assertEqual(social_state.tier_for(-10), "cold")
        self.assertEqual(social_state.tier_for(-40), "hostile")

    def test_model_delta_is_applied_and_clamped(self):
        result = self.adjust(2.5)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["applied"], 2.5)
        self.assertEqual(self.store.get("group:test", "actor1")["score"], 2.5)
        self.now += 301
        result = self.adjust(99)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["applied"], 5.0)
        self.assertEqual(self.store.get("group:test", "actor1")["score"], 7.5)

    def test_negative_delta_allowed(self):
        self.adjust(-0.5)
        self.now += 301
        result = self.adjust(-4)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["applied"], -4)
        self.assertEqual(self.store.get("group:test", "actor1")["score"], -4.5)

    def test_low_confidence_rejected(self):
        result = self.store.adjust(
            "group:test", "actor1", delta=1, confidence=0.2,
            reason="x", enabled=True, cooldown_seconds=0,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "low_confidence")

    def test_duplicate_and_cooldown(self):
        first = self.adjust(1)
        self.assertTrue(first["accepted"])
        duplicate = self.adjust(1)
        self.assertFalse(duplicate["accepted"])
        self.assertIn(duplicate["reason"], {"duplicate", "cooldown"})

    def test_disabled(self):
        result = self.store.adjust(
            "group:test", "actor1", delta=1, confidence=0.9, enabled=False
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "disabled")

    def test_context_does_not_expose_score(self):
        self.adjust(5, reason="a")
        self.now += 301
        self.adjust(5, reason="b")
        text = self.store.context("group:test", "actor1")
        self.assertIn("好感度", text)
        self.assertIn("warm", text)
        self.assertNotIn("12", text)
