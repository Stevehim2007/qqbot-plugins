from __future__ import annotations

import concurrent.futures
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aux_budget import (
    AuxBudget,
    AuxBudgetError,
    FEATURE_IDS,
    TIER_LIMITS_CNY,
)


class _Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class AuxBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = _Clock(datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
        self.budget = AuxBudget(self.tmp.name, now=self.clock)

    def test_default_config_and_feature_catalog(self) -> None:
        status = self.budget.status()
        self.assertEqual(status["tier"], "standard")
        self.assertEqual(status["limit_cny"], 20.0)
        self.assertEqual(status["state"], "normal")
        self.assertEqual(status["spent_cny"], 0.0)
        config = self.budget.public_config()
        self.assertEqual(config["monthly_limit_cny"], 20.0)
        self.assertEqual(config["limits_cny"], TIER_LIMITS_CNY)
        self.assertEqual(set(config["features"]), set(FEATURE_IDS))
        self.assertEqual(len(FEATURE_IDS), 13)

    def test_reserve_commit_and_release(self) -> None:
        result = self.budget.reserve(
            "memory.extract", group_hash="g1", estimate_cny=1.25, request_id="r1"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.budget.status()["reserved_cny"], 1.25)
        commit = self.budget.commit(result["reservation_id"], actual_cost_cny=0.5)
        self.assertEqual(commit["status"], "ok")
        status = self.budget.status()
        self.assertEqual(status["spent_cny"], 0.5)
        self.assertEqual(status["reserved_cny"], 0.0)

        second = self.budget.reserve("memory.summarize", estimate_cny=2.0)
        released = self.budget.release(second["reservation_id"], reason_code="cancelled")
        self.assertEqual(released["status"], "released")
        self.assertEqual(self.budget.status()["spent_cny"], 0.5)
        self.assertEqual(self.budget.status()["reserved_cny"], 0.0)

    def test_request_id_is_idempotent(self) -> None:
        first = self.budget.reserve("memory.extract", estimate_cny=0.1, request_id="same")
        second = self.budget.reserve("memory.extract", estimate_cny=0.1, request_id="same")
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(first["reservation_id"], second["reservation_id"])
        usage = self.budget.usage()
        self.assertEqual(sum(item["calls"] for item in usage["features"]), 1)

    def test_off_tier_blocks_all_new_calls(self) -> None:
        self.budget.set_tier("off")
        self.assertEqual(self.budget.status()["limit_cny"], 0.0)
        self.assertEqual(self.budget.status()["state"], "stopped")
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.reserve("memory.extract", estimate_cny=0.01)
        self.assertEqual(ctx.exception.code, "budget_disabled")

    def test_warning_and_stop_states(self) -> None:
        self.budget.set_tier("light")
        first = self.budget.reserve("sticker.ai_collect", estimate_cny=8.0)
        self.budget.commit(first["reservation_id"], actual_cost_cny=8.0)
        self.assertEqual(self.budget.status()["state"], "warning")
        second = self.budget.reserve("sticker.ai_collect", estimate_cny=2.0)
        self.budget.commit(second["reservation_id"], actual_cost_cny=2.0)
        self.assertEqual(self.budget.status()["state"], "stopped")
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.reserve("memory.extract", estimate_cny=0.01)
        self.assertEqual(ctx.exception.code, "budget_stopped")

    def test_budget_exhaustion_never_oversells(self) -> None:
        self.budget.set_tier("light")
        first = self.budget.reserve("memory.extract", estimate_cny=10.0)
        self.assertTrue(first["ok"])
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.reserve("memory.extract", estimate_cny=0.01)
        self.assertEqual(ctx.exception.code, "budget_exhausted")

    def test_daily_quota_and_release(self) -> None:
        reservations = [
            self.budget.reserve("memory.extract", group_hash="g1", estimate_cny=0.0)["reservation_id"]
            for _ in range(6)
        ]
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.reserve("memory.extract", group_hash="g1", estimate_cny=0.0)
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        self.budget.release(reservations[0])
        self.assertTrue(
            self.budget.reserve("memory.extract", group_hash="g1", estimate_cny=0.0)["ok"]
        )

    def test_absent_tier_quota_is_zero_not_unlimited(self) -> None:
        self.budget.set_tier("light")
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.reserve("memory.compress", estimate_cny=0.01)
        self.assertEqual(ctx.exception.code, "quota_exceeded")

    def test_group_allowlist(self) -> None:
        self.budget.set_feature(
            "sticker.ai_collect", enabled=True, group_allowlist=["g1"]
        )
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.reserve("sticker.ai_collect", group_hash="g2", estimate_cny=0.1)
        self.assertEqual(ctx.exception.code, "group_not_allowed")
        self.assertTrue(
            self.budget.reserve("sticker.ai_collect", group_hash="g1", estimate_cny=0.1)["ok"]
        )

    def test_concurrent_reservations_do_not_oversell(self) -> None:
        def reserve_one(_: int) -> str:
            try:
                self.budget.reserve("memory.extract", estimate_cny=4.0)
                return "ok"
            except AuxBudgetError as exc:
                return exc.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve_one, range(20)))
        self.assertEqual(results.count("ok"), 5)
        self.assertEqual(results.count("budget_exhausted"), 15)
        self.assertLessEqual(self.budget.status()["reserved_cny"], 20.0)

    def test_corrupt_config_is_degraded_and_never_spends(self) -> None:
        path = Path(self.tmp.name) / "aux_budget.json"
        path.write_text("{not-json", encoding="utf-8")
        budget = AuxBudget(self.tmp.name, now=self.clock)
        self.assertTrue(budget.degraded)
        self.assertEqual(budget.status()["state"], "unknown")
        with self.assertRaises(AuxBudgetError) as ctx:
            budget.reserve("memory.extract", estimate_cny=0.01)
        self.assertEqual(ctx.exception.code, "ledger_unknown")

    def test_cycle_rollover_starts_new_budget(self) -> None:
        first = self.budget.reserve("memory.extract", estimate_cny=1.0)
        self.budget.commit(first["reservation_id"], actual_cost_cny=1.0)
        self.assertEqual(self.budget.status()["spent_cny"], 1.0)
        self.clock.value = datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc)
        status = self.budget.status()
        self.assertEqual(status["cycle"], "2026-12")
        self.assertEqual(status["spent_cny"], 0.0)
        self.assertEqual(status["limit_cny"], 20.0)

    def test_revision_conflict(self) -> None:
        with self.assertRaises(AuxBudgetError) as ctx:
            self.budget.set_tier("light", expected_revision=99)
        self.assertEqual(ctx.exception.code, "revision_conflict")

    def test_usage_has_no_prompt_text_columns(self) -> None:
        result = self.budget.reserve("memory.extract", estimate_cny=0.1)
        self.budget.commit(result["reservation_id"], actual_cost_cny=0.1)
        usage = self.budget.usage()
        self.assertNotIn("prompt", json.dumps(usage, ensure_ascii=False).lower())
        self.assertNotIn("content", json.dumps(usage, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
