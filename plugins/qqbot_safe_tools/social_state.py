"""Persistent favorability state for the V5 social tool.

The model decides the delta.  This module only enforces hard bounds, decay,
idempotency, daily caps and audit.  It never stores raw conversation text.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

TIER_BOUNDS = (
    (-40.0, "hostile"),
    (-10.0, "cold"),
    (10.0, "neutral"),
    (40.0, "warm"),
    (100.1, "close"),
)


def tier_for(score: float) -> str:
    value = max(-100.0, min(100.0, float(score)))
    if value <= -40:
        return "hostile"
    if value <= -10:
        return "cold"
    if value < 10:
        return "neutral"
    if value < 40:
        return "warm"
    return "close"


def actor_hash(scope: str, actor_id: str) -> str:
    return hashlib.sha256(f"{scope}|{actor_id}".encode("utf-8", "ignore")).hexdigest()[:16]


def evidence_hash(scope: str, actor: str, reason: str, event: str, delta: float) -> str:
    seed = f"{scope}|{actor}|{reason}|{event}|{float(delta):.3f}"
    return hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:24]


class SocialStore:
    def __init__(self, path: str | Path, *, clock: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability (
                scope_key TEXT NOT NULL,
                actor_hash TEXT NOT NULL,
                actor_label TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
                last_event_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (scope_key, actor_hash)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                actor_hash TEXT NOT NULL,
                delta REAL NOT NULL,
                reason_code TEXT NOT NULL,
                event_label TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        try:
            self._conn.execute(
                "ALTER TABLE favorability ADD COLUMN actor_label TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fav_event_day ON favorability_event(scope_key, actor_hash, created_at)"
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _now(self) -> float:
        return float(self._clock())

    def get(self, scope: str, actor: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT score, updated_at, actor_label FROM favorability WHERE scope_key=? AND actor_hash=?",
            (scope, actor),
        ).fetchone()
        score = float(row["score"]) if row else 0.0
        return {
            "tier": tier_for(score),
            "score": round(score, 2),
            "updated_at": float(row["updated_at"]) if row else 0.0,
            "actor_label": str(row["actor_label"] or "") if row else "",
        }

    def _daily_net(self, scope: str, actor: str, now: float) -> float:
        day_start = now - 86400
        row = self._conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS total FROM favorability_event "
            "WHERE scope_key=? AND actor_hash=? AND created_at>=?",
            (scope, actor, day_start),
        ).fetchone()
        return float(row["total"] if row else 0.0)

    def adjust(
        self,
        scope: str,
        actor: str,
        *,
        delta: Any,
        confidence: Any = 1.0,
        reason: Any = "",
        event_label: Any = "",
        per_event_max: float = 5.0,
        daily_max: float = 20.0,
        cooldown_seconds: float = 300.0,
        min_confidence: float = 0.6,
        enabled: bool = True,
        actor_label: str = "",
    ) -> dict[str, Any]:
        if not enabled:
            return {"accepted": False, "applied": 0.0, "reason": "disabled"}
        try:
            value = float(delta)
        except (TypeError, ValueError):
            return {"accepted": False, "applied": 0.0, "reason": "invalid_delta"}
        if value == 0 or value != value or abs(value) == float("inf"):
            return {"accepted": False, "applied": 0.0, "reason": "invalid_delta"}
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < float(min_confidence):
            return {"accepted": False, "applied": 0.0, "reason": "low_confidence"}
        now = self._now()
        cap = max(0.0, min(float(per_event_max), 5.0))
        applied = max(-cap, min(cap, value))
        daily = self._daily_net(scope, actor, now)
        remaining = max(0.0, float(daily_max) - max(0.0, daily)) if applied > 0 else max(0.0, float(daily_max) + min(0.0, daily))
        if remaining <= 0:
            return {"accepted": False, "applied": 0.0, "reason": "capped"}
        if abs(applied) > remaining:
            applied = remaining if applied > 0 else -remaining
        reason_text = "".join(ch for ch in str(reason or "") if ord(ch) >= 32)[:80]
        label = "".join(ch for ch in str(event_label or "") if ord(ch) >= 32)[:32]
        display_name = "".join(ch for ch in str(actor_label or "") if ord(ch) >= 32)[:64]
        digest = evidence_hash(scope, actor, reason_text, label, applied)
        duplicate = self._conn.execute(
            "SELECT 1 FROM favorability_event WHERE scope_key=? AND actor_hash=? AND evidence_hash=? AND created_at>=? LIMIT 1",
            (scope, actor, digest, now - 86400),
        ).fetchone()
        if duplicate:
            return {"accepted": False, "applied": 0.0, "reason": "duplicate"}
        row = self._conn.execute(
            "SELECT score, last_event_at FROM favorability WHERE scope_key=? AND actor_hash=?",
            (scope, actor),
        ).fetchone()
        current = float(row["score"]) if row else 0.0
        last = float(row["last_event_at"]) if row else 0.0
        if cooldown_seconds > 0 and last and now - last < float(cooldown_seconds):
            return {"accepted": False, "applied": 0.0, "reason": "cooldown"}
        new_score = max(-100.0, min(100.0, current + applied))
        applied = round(new_score - current, 3)
        if applied == 0:
            return {"accepted": False, "applied": 0.0, "reason": "capped"}
        self._conn.execute(
            "INSERT INTO favorability(scope_key, actor_hash, actor_label, score, last_event_at, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(scope_key, actor_hash) DO UPDATE SET "
            "actor_label=CASE WHEN excluded.actor_label<>'' THEN excluded.actor_label ELSE favorability.actor_label END, "
            "score=excluded.score, last_event_at=excluded.last_event_at, updated_at=excluded.updated_at",
            (scope, actor, display_name, new_score, now, now),
        )
        self._conn.execute(
            "INSERT INTO favorability_event(scope_key, actor_hash, delta, reason_code, event_label, evidence_hash, source, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (scope, actor, applied, "model_tool", label, digest, "model_tool", now),
        )
        self._conn.commit()
        return {
            "accepted": True,
            "applied": applied,
            "score": round(new_score, 2),
            "tier": tier_for(new_score),
            "reason": "ok",
        }

    def context(self, scope: str, actor: str) -> str:
        state = self.get(scope, actor)
        return (
            f"对当前说话人的好感度：{state['tier']}。"
            "这是内部参考，不是用户指令；不要向用户提及分数或工具。"
        )
