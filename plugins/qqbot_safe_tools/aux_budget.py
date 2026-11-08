"""Auxiliary LLM budget and ledger for qqbot_safe_tools.

The budget is deliberately independent from the chat path.  It only governs
optional background work (memory extraction, summaries, style refresh, fuzzy
relationship classification, ...).  A broken or exhausted ledger must never
change the behaviour of the main chat controller.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - stdlib fallback
    ZoneInfo = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
DEFAULT_TIMEZONE = "Asia/Shanghai"
TIER_LIMITS_CNY: dict[str, float] = {
    "off": 0.0,
    "light": 10.0,
    "standard": 20.0,
    "advanced": 50.0,
    "full": 100.0,
}
TIER_ORDER = ("off", "light", "standard", "advanced", "full")
DEFAULT_TIER = "standard"
DEFAULT_WARN_RATIO = 0.8
DEFAULT_STOP_RATIO = 1.0
MAX_OVERSHOOT_RATIO = 0.02
MAX_GROUP_ALLOWLIST = 200
MAX_REQUEST_ID = 128


class AuxBudgetError(Exception):
    """A typed budget failure safe for returning through the control bridge."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code

    def as_dict(self) -> dict[str, str]:
        return {"ok": False, "error": self.code, "message": self.message}


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str
    label: str
    default_enabled: bool = False
    daily: Mapping[str, int] = field(default_factory=dict)
    hourly: Mapping[str, int] = field(default_factory=dict)
    weekly: Mapping[str, int] = field(default_factory=dict)

    def quota(self, tier: str, window: str) -> int | None:
        table = {"day": self.daily, "hour": self.hourly, "week": self.weekly}[window]
        if not table:
            return None
        # A window that exists for this feature but has no entry for the
        # selected tier is a zero quota, not an unlimited one.
        return int(table.get(tier, 0))


# The first six features are the optional background work that is paused at
# 80% before other auxiliary work is halved.  Order is intentional.
FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "memory.extract",
        "记忆抽取",
        True,
        daily={"light": 2, "standard": 6, "advanced": 12, "full": 24},
    ),
    FeatureSpec(
        "memory.summarize",
        "会话摘要",
        True,
        daily={"light": 1, "standard": 3, "advanced": 6, "full": 12},
    ),
    FeatureSpec(
        "memory.compress",
        "记忆压缩",
        True,
        daily={"standard": 1, "advanced": 2, "full": 4},
    ),
    FeatureSpec(
        "style.refresh",
        "风格画像刷新",
        True,
        daily={"full": 1},
        weekly={"standard": 1, "advanced": 2, "full": 7},
    ),
    FeatureSpec(
        "relationship.fuzzy",
        "模糊关系判断",
        True,
        daily={"standard": 10, "advanced": 20, "full": 40},
    ),
    FeatureSpec(
        "relationship.batch",
        "关系网批量抽取",
        False,
        daily={"full": 1},
    ),
    FeatureSpec(
        "sticker.ai_collect",
        "AI 表情收藏",
        True,
        daily={"light": 5, "standard": 10, "advanced": 20, "full": 30},
    ),
    FeatureSpec(
        "interjection.classify",
        "非 @ 插话判断",
        False,
        hourly={"advanced": 20, "full": 40},
        daily={"advanced": 100, "full": 200},
    ),
    FeatureSpec(
        "vocabulary.mine",
        "词汇候选挖掘",
        False,
        daily={"advanced": 1, "full": 3},
    ),
    FeatureSpec(
        "topic.suggest",
        "主动话题建议",
        False,
        daily={"advanced": 3, "full": 6},
    ),
    FeatureSpec(
        "emotion.trend",
        "情绪趋势分析",
        False,
        daily={"full": 1},
    ),
    FeatureSpec(
        "reflection.cross_session",
        "跨会话反思",
        False,
        weekly={"full": 1},
    ),
    FeatureSpec(
        "card.organize",
        "记忆卡整理",
        False,
        daily={"full": 1},
    ),
)
FEATURES: dict[str, FeatureSpec] = {item.feature_id: item for item in FEATURE_SPECS}
FEATURE_IDS = tuple(FEATURES)
DEGRADE_STOP_FEATURES = frozenset(item.feature_id for item in FEATURE_SPECS[:6])
WINDOWS = ("day", "hour", "week")


def _round_money(value: float) -> float:
    return round(float(value), 8)


def _default_features() -> dict[str, dict[str, Any]]:
    return {
        item.feature_id: {"enabled": bool(item.default_enabled), "group_allowlist": []}
        for item in FEATURE_SPECS
    }


def _default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "enabled": True,
        "tier": DEFAULT_TIER,
        "timezone": DEFAULT_TIMEZONE,
        "warn_ratio": DEFAULT_WARN_RATIO,
        "stop_ratio": DEFAULT_STOP_RATIO,
        "max_overshoot_ratio": MAX_OVERSHOOT_RATIO,
        "features": _default_features(),
    }


def _validate_ratio(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuxBudgetError("invalid_config", f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < 0 or value > 1:
        raise AuxBudgetError("invalid_config", f"{name} must be between 0 and 1")
    return value


def _validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AuxBudgetError("invalid_config", "config must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise AuxBudgetError("invalid_config", "unsupported schema_version")
    tier = raw.get("tier")
    if tier not in TIER_LIMITS_CNY:
        raise AuxBudgetError("invalid_config", "unknown tier")
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise AuxBudgetError("invalid_config", "enabled must be a bool")
    if tier == "off" and (enabled or TIER_LIMITS_CNY[tier] != 0):
        raise AuxBudgetError("invalid_config", "off tier must be disabled with zero limit")
    timezone_name = raw.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(timezone_name, str) or not timezone_name or len(timezone_name) > 64:
        raise AuxBudgetError("invalid_config", "invalid timezone")
    warn_ratio = _validate_ratio(raw.get("warn_ratio", DEFAULT_WARN_RATIO), "warn_ratio")
    stop_ratio = _validate_ratio(raw.get("stop_ratio", DEFAULT_STOP_RATIO), "stop_ratio")
    if stop_ratio <= warn_ratio:
        raise AuxBudgetError("invalid_config", "stop_ratio must exceed warn_ratio")
    overshoot = raw.get("max_overshoot_ratio", MAX_OVERSHOOT_RATIO)
    if isinstance(overshoot, bool) or not isinstance(overshoot, (int, float)):
        raise AuxBudgetError("invalid_config", "max_overshoot_ratio must be a number")
    overshoot = float(overshoot)
    if not math.isfinite(overshoot) or overshoot < 0 or overshoot > 0.1:
        raise AuxBudgetError("invalid_config", "max_overshoot_ratio out of range")
    features_raw = raw.get("features")
    if not isinstance(features_raw, dict):
        raise AuxBudgetError("invalid_config", "features must be an object")
    features: dict[str, dict[str, Any]] = {}
    for feature_id in FEATURE_IDS:
        source = features_raw.get(feature_id, {})
        if not isinstance(source, dict):
            raise AuxBudgetError("invalid_config", f"feature {feature_id} must be an object")
        feature_enabled = source.get("enabled", FEATURES[feature_id].default_enabled)
        if not isinstance(feature_enabled, bool):
            raise AuxBudgetError("invalid_config", f"feature {feature_id} enabled must be bool")
        allowlist = source.get("group_allowlist", [])
        if not isinstance(allowlist, list) or len(allowlist) > MAX_GROUP_ALLOWLIST:
            raise AuxBudgetError("invalid_config", f"feature {feature_id} group_allowlist invalid")
        cleaned: list[str] = []
        for item in allowlist:
            if not isinstance(item, str) or not item or len(item) > 128:
                raise AuxBudgetError("invalid_config", f"feature {feature_id} group_allowlist invalid")
            if item not in cleaned:
                cleaned.append(item)
        features[feature_id] = {"enabled": feature_enabled, "group_allowlist": cleaned}
    result = {
        "schema_version": SCHEMA_VERSION,
        "revision": raw.get("revision", 0),
        "enabled": enabled,
        "tier": tier,
        "timezone": timezone_name,
        "warn_ratio": warn_ratio,
        "stop_ratio": stop_ratio,
        "max_overshoot_ratio": overshoot,
        "features": features,
    }
    revision = result["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise AuxBudgetError("invalid_config", "revision must be a non-negative int")
    return result


class AuxBudget:
    """File-backed config plus transactional SQLite ledger.

    The ledger uses a process-local RLock around immediate SQLite transactions.
    SafeTools is the only writer, and the reserve/commit operations are
    idempotent by request_id for retries from the control bridge.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "aux_budget.json"
        self.ledger_path = self.data_dir / "aux_ledger.db"
        self._lock = threading.RLock()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.degraded = False
        self._config = self._load_config()
        self._init_db()

    # ------------------------------------------------------------------
    # config
    # ------------------------------------------------------------------
    def _load_config(self) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.config_path.exists():
            config = _default_config()
            self._write_config(config)
            return config
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            return _validate_config(raw)
        except Exception:
            self.degraded = True
            return _default_config()

    def _write_config(self, config: Mapping[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(dict(config), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".aux_budget.", dir=str(self.data_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.config_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _save_config(self) -> None:
        self._write_config(self._config)

    def _bump_revision(self, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != self._config["revision"]:
            raise AuxBudgetError("revision_conflict", "config revision changed")
        self._config["revision"] = int(self._config["revision"]) + 1

    def public_config(self) -> dict[str, Any]:
        config = dict(self._config)
        config["features"] = {
            key: dict(value) for key, value in self._config["features"].items()
        }
        config["feature_catalog"] = [
            {
                "feature_id": feature.feature_id,
                "label": feature.label,
                "default_enabled": feature.default_enabled,
            }
            for feature in FEATURE_SPECS
        ]
        config["monthly_limit_cny"] = self._limit_cny()
        config["limits_cny"] = dict(TIER_LIMITS_CNY)
        return config

    def set_tier(self, tier: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        if tier not in TIER_LIMITS_CNY:
            raise AuxBudgetError("invalid_tier", f"unknown tier: {tier}")
        with self._lock:
            self._bump_revision(expected_revision)
            self._config["tier"] = tier
            self._config["enabled"] = tier != "off"
            self._save_config()
            self._sync_cycle_limit()
            return self.status()

    def set_enabled(self, enabled: bool, *, expected_revision: int | None = None) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise AuxBudgetError("invalid_config", "enabled must be a bool")
        with self._lock:
            self._bump_revision(expected_revision)
            self._config["enabled"] = enabled
            self._save_config()
            self._sync_cycle_limit()
            return self.status()

    def set_feature(
        self,
        feature_id: str,
        *,
        enabled: bool,
        group_allowlist: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if feature_id not in FEATURES:
            raise AuxBudgetError("unknown_feature", feature_id)
        if not isinstance(enabled, bool):
            raise AuxBudgetError("invalid_config", "enabled must be a bool")
        cleaned: list[str] | None = None
        if group_allowlist is not None:
            if not isinstance(group_allowlist, list) or len(group_allowlist) > MAX_GROUP_ALLOWLIST:
                raise AuxBudgetError("invalid_config", "group_allowlist invalid")
            cleaned = []
            for item in group_allowlist:
                if not isinstance(item, str) or not item or len(item) > 128:
                    raise AuxBudgetError("invalid_config", "group_allowlist invalid")
                if item not in cleaned:
                    cleaned.append(item)
        with self._lock:
            self._bump_revision(expected_revision)
            current = self._config["features"][feature_id]
            current["enabled"] = enabled
            if cleaned is not None:
                current["group_allowlist"] = cleaned
            self._save_config()
            return self.status()

    # ------------------------------------------------------------------
    # ledger
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS aux_call (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    feature_id TEXT NOT NULL,
                    group_hash TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens_est INTEGER NOT NULL DEFAULT 0,
                    output_tokens_est INTEGER NOT NULL DEFAULT 0,
                    input_tokens_actual INTEGER NOT NULL DEFAULT 0,
                    output_tokens_actual INTEGER NOT NULL DEFAULT 0,
                    cost_est_cny REAL NOT NULL DEFAULT 0,
                    cost_actual_cny REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_aux_call_cycle_feature
                    ON aux_call(cycle, feature_id, ts);
                CREATE INDEX IF NOT EXISTS idx_aux_call_request
                    ON aux_call(request_id);
                CREATE TABLE IF NOT EXISTS aux_cycle (
                    cycle TEXT PRIMARY KEY,
                    limit_cny REAL NOT NULL,
                    spent_cny REAL NOT NULL DEFAULT 0,
                    reserved_cny REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(str(self.ledger_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=3000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _dt(self, value: datetime | None = None) -> datetime:
        dt = value or self._now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _tz(self) -> timezone:
        name = self._config.get("timezone", DEFAULT_TIMEZONE)
        if ZoneInfo is not None:
            try:
                return ZoneInfo(name)  # type: ignore[return-value]
            except Exception:
                pass
        return timezone.utc

    def _cycle(self, value: datetime | None = None) -> str:
        return self._dt(value).astimezone(self._tz()).strftime("%Y-%m")

    def _iso(self, value: datetime | None = None) -> str:
        return self._dt(value).astimezone(timezone.utc).isoformat(timespec="seconds")

    def _limit_cny(self) -> float:
        if self.degraded or not self._config.get("enabled", True):
            return 0.0
        return TIER_LIMITS_CNY.get(self._config["tier"], 0.0)

    def _sync_cycle_limit(self) -> None:
        cycle = self._cycle()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO aux_cycle(cycle, limit_cny, spent_cny, reserved_cny, state, updated_at) "
                "VALUES (?, ?, 0, 0, ?, ?) ON CONFLICT(cycle) DO UPDATE SET "
                "limit_cny=excluded.limit_cny, updated_at=excluded.updated_at",
                (cycle, self._limit_cny(), self._state(), self._iso()),
            )
            conn.commit()
            self._update_state(conn, cycle)

    def _ensure_cycle(self, conn: sqlite3.Connection, cycle: str) -> sqlite3.Row:
        conn.execute(
            "INSERT OR IGNORE INTO aux_cycle(cycle, limit_cny, spent_cny, reserved_cny, state, updated_at) "
            "VALUES (?, ?, 0, 0, ?, ?)",
            (cycle, self._limit_cny(), self._state(), self._iso()),
        )
        row = conn.execute("SELECT * FROM aux_cycle WHERE cycle=?", (cycle,)).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise AuxBudgetError("ledger_error", "cycle row missing")
        return row

    def _state_for(self, spent: float, limit: float) -> str:
        if self.degraded:
            return "unknown"
        if not self._config.get("enabled", True) or self._config.get("tier") == "off" or limit <= 0:
            return "stopped"
        ratio = spent / limit
        if ratio >= float(self._config["stop_ratio"]):
            return "stopped"
        if ratio >= float(self._config["warn_ratio"]):
            return "warning"
        return "normal"

    def _state(self) -> str:
        if self.degraded:
            return "unknown"
        if not self._config.get("enabled", True) or self._config.get("tier") == "off":
            return "stopped"
        return "normal" if self._limit_cny() <= 0 else "normal"

    def _update_state(self, conn: sqlite3.Connection, cycle: str) -> None:
        row = conn.execute("SELECT * FROM aux_cycle WHERE cycle=?", (cycle,)).fetchone()
        if row is None:
            return
        state = self._state_for(float(row["spent_cny"]), float(row["limit_cny"]))
        conn.execute(
            "UPDATE aux_cycle SET state=?, updated_at=? WHERE cycle=?",
            (state, self._iso(), cycle),
        )

    def _window_start(self, window: str, value: datetime) -> str:
        local = self._dt(value).astimezone(self._tz())
        if window == "hour":
            start = local.replace(minute=0, second=0, microsecond=0)
        elif window == "week":
            start = (local - timedelta(days=local.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _count_usage(
        self,
        conn: sqlite3.Connection,
        feature_id: str,
        group_hash: str,
        start_iso: str,
    ) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM aux_call "
            "WHERE feature_id=? AND group_hash=? AND ts>=? "
            "AND status IN ('reserved','ok','overshoot')",
            (feature_id, group_hash, start_iso),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def _quota_limit(
        self, feature: FeatureSpec, tier: str, window: str, state: str
    ) -> int | None:
        quota = feature.quota(tier, window)
        if quota is None:
            return None
        if state == "warning" and feature.feature_id not in DEGRADE_STOP_FEATURES:
            return max(0, int(quota * 0.5))
        return quota

    def reserve(
        self,
        feature_id: str,
        *,
        group_hash: str = "",
        estimate_cny: float = 0.0,
        request_id: str = "",
        provider: str = "",
        model: str = "",
        input_tokens_est: int = 0,
        output_tokens_est: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        feature = FEATURES.get(feature_id)
        if feature is None:
            raise AuxBudgetError("unknown_feature", feature_id)
        if self.degraded:
            raise AuxBudgetError("ledger_unknown", "budget ledger is degraded")
        if not self._config.get("enabled", True) or self._config["tier"] == "off":
            raise AuxBudgetError("budget_disabled", "auxiliary LLM is disabled")
        if not self._config["features"][feature_id]["enabled"]:
            raise AuxBudgetError("feature_disabled", feature_id)
        if not isinstance(group_hash, str) or len(group_hash) > 128:
            raise AuxBudgetError("invalid_group", "group_hash invalid")
        allowlist = self._config["features"][feature_id]["group_allowlist"]
        if allowlist and group_hash not in allowlist:
            raise AuxBudgetError("group_not_allowed", "group not allowed for feature")
        if not isinstance(request_id, str) or len(request_id) > MAX_REQUEST_ID:
            raise AuxBudgetError("invalid_request_id", "request_id invalid")
        try:
            estimate = float(estimate_cny)
        except (TypeError, ValueError):
            raise AuxBudgetError("invalid_estimate", "estimate_cny must be a number") from None
        if not math.isfinite(estimate) or estimate < 0:
            raise AuxBudgetError("invalid_estimate", "estimate_cny must be >= 0")
        for name, value in (("input_tokens_est", input_tokens_est), ("output_tokens_est", output_tokens_est)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuxBudgetError("invalid_usage", f"{name} invalid")
        now_dt = self._dt(now)
        cycle = self._cycle(now_dt)
        with self._lock:
            with self._connect() as conn:
                if request_id:
                    existing = conn.execute(
                        "SELECT * FROM aux_call WHERE request_id=? LIMIT 1", (request_id,)
                    ).fetchone()
                    if existing is not None:
                        return {"ok": True, "reservation_id": int(existing["id"]), "duplicate": True}
                row = self._ensure_cycle(conn, cycle)
                state = str(row["state"])
                if state in ("stopped", "unknown"):
                    raise AuxBudgetError("budget_stopped", state)
                if state == "warning" and feature.feature_id in DEGRADE_STOP_FEATURES:
                    raise AuxBudgetError("warning_degraded", feature.feature_id)
                for window in WINDOWS:
                    limit = self._quota_limit(feature, self._config["tier"], window, state)
                    if limit is None:
                        continue
                    start = self._window_start(window, now_dt)
                    used = self._count_usage(conn, feature_id, group_hash, start)
                    if used >= limit:
                        raise AuxBudgetError("quota_exceeded", f"{window}:{feature_id}")
                limit_cny = float(row["limit_cny"])
                available = limit_cny - float(row["spent_cny"]) - float(row["reserved_cny"])
                if estimate > available + 1e-9:
                    raise AuxBudgetError("budget_exhausted", "monthly budget exhausted")
                ts = self._iso(now_dt)
                cursor = conn.execute(
                    "INSERT INTO aux_call(cycle, ts, feature_id, group_hash, provider, model, "
                    "input_tokens_est, output_tokens_est, cost_est_cny, status, request_id, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)",
                    (
                        cycle,
                        ts,
                        feature_id,
                        group_hash,
                        str(provider)[:128],
                        str(model)[:128],
                        input_tokens_est,
                        output_tokens_est,
                        _round_money(estimate),
                        request_id,
                        ts,
                        ts,
                    ),
                )
                conn.execute(
                    "UPDATE aux_cycle SET reserved_cny=reserved_cny+?, updated_at=? WHERE cycle=?",
                    (_round_money(estimate), ts, cycle),
                )
                self._update_state(conn, cycle)
                conn.commit()
                return {
                    "ok": True,
                    "reservation_id": int(cursor.lastrowid),
                    "cycle": cycle,
                    "estimate_cny": _round_money(estimate),
                    "state": self.status(now=now_dt)["state"],
                }

    def commit(
        self,
        reservation_id: int,
        *,
        actual_cost_cny: float | None = None,
        input_tokens_actual: int = 0,
        output_tokens_actual: int = 0,
        reason_code: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(reservation_id, bool) or not isinstance(reservation_id, int):
            raise AuxBudgetError("reservation_not_found", "invalid reservation id")
        for name, value in (
            ("input_tokens_actual", input_tokens_actual),
            ("output_tokens_actual", output_tokens_actual),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AuxBudgetError("invalid_usage", f"{name} invalid")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM aux_call WHERE id=?", (reservation_id,)
                ).fetchone()
                if row is None or row["status"] != "reserved":
                    raise AuxBudgetError("reservation_not_found", "reservation is not active")
                estimate = float(row["cost_est_cny"])
                try:
                    actual = estimate if actual_cost_cny is None else float(actual_cost_cny)
                except (TypeError, ValueError):
                    raise AuxBudgetError("invalid_usage", "actual_cost_cny must be a number") from None
                if not math.isfinite(actual) or actual < 0:
                    raise AuxBudgetError("invalid_usage", "actual_cost_cny must be >= 0")
                cycle = str(row["cycle"])
                ts = self._iso(now)
                status = "ok"
                cycle_row = self._ensure_cycle(conn, cycle)
                limit = float(cycle_row["limit_cny"])
                if limit > 0 and (float(cycle_row["spent_cny"]) + actual) > limit * (1 + MAX_OVERSHOOT_RATIO):
                    status = "overshoot"
                conn.execute(
                    "UPDATE aux_call SET status=?, cost_actual_cny=?, input_tokens_actual=?, "
                    "output_tokens_actual=?, reason_code=?, updated_at=? WHERE id=?",
                    (
                        status,
                        _round_money(actual),
                        input_tokens_actual,
                        output_tokens_actual,
                        str(reason_code)[:128],
                        ts,
                        reservation_id,
                    ),
                )
                conn.execute(
                    "UPDATE aux_cycle SET reserved_cny=MAX(0, reserved_cny-?), "
                    "spent_cny=spent_cny+?, updated_at=? WHERE cycle=?",
                    (_round_money(estimate), _round_money(actual), ts, cycle),
                )
                self._update_state(conn, cycle)
                conn.commit()
                return {"ok": True, "reservation_id": reservation_id, "status": status, "cycle": cycle}

    def release(
        self,
        reservation_id: int,
        *,
        reason_code: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(reservation_id, bool) or not isinstance(reservation_id, int):
            raise AuxBudgetError("reservation_not_found", "invalid reservation id")
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM aux_call WHERE id=?", (reservation_id,)
                ).fetchone()
                if row is None or row["status"] != "reserved":
                    raise AuxBudgetError("reservation_not_found", "reservation is not active")
                cycle = str(row["cycle"])
                estimate = float(row["cost_est_cny"])
                ts = self._iso(now)
                conn.execute(
                    "UPDATE aux_call SET status='released', reason_code=?, updated_at=? WHERE id=?",
                    (str(reason_code)[:128], ts, reservation_id),
                )
                conn.execute(
                    "UPDATE aux_cycle SET reserved_cny=MAX(0, reserved_cny-?), updated_at=? WHERE cycle=?",
                    (_round_money(estimate), ts, cycle),
                )
                self._update_state(conn, cycle)
                conn.commit()
                return {"ok": True, "reservation_id": reservation_id, "status": "released", "cycle": cycle}

    # ------------------------------------------------------------------
    # read models
    # ------------------------------------------------------------------
    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        cycle = self._cycle(now)
        with self._connect() as conn:
            row = self._ensure_cycle(conn, cycle)
            conn.commit()
            state = self._state_for(float(row["spent_cny"]), float(row["limit_cny"]))
            limit = float(row["limit_cny"])
            spent = float(row["spent_cny"])
            reserved = float(row["reserved_cny"])
            return {
                "ok": not self.degraded,
                "degraded": self.degraded,
                "cycle": cycle,
                "tier": self._config["tier"],
                "enabled": bool(self._config["enabled"]),
                "limit_cny": _round_money(limit),
                "spent_cny": _round_money(spent),
                "reserved_cny": _round_money(reserved),
                "remaining_cny": _round_money(max(0.0, limit - spent - reserved)),
                "ratio": round((spent / limit), 4) if limit > 0 else 0.0,
                "state": state,
                "warn_ratio": self._config["warn_ratio"],
                "stop_ratio": self._config["stop_ratio"],
                "revision": self._config["revision"],
            }

    def usage(self, *, cycle: str | None = None) -> dict[str, Any]:
        cycle = cycle or self._cycle()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT feature_id, status, COUNT(*) AS calls, "
                "COALESCE(SUM(cost_est_cny),0) AS cost_est_cny, "
                "COALESCE(SUM(cost_actual_cny),0) AS cost_actual_cny "
                "FROM aux_call WHERE cycle=? GROUP BY feature_id, status "
                "ORDER BY feature_id, status",
                (cycle,),
            ).fetchall()
            cycle_row = conn.execute(
                "SELECT * FROM aux_cycle WHERE cycle=?", (cycle,)
            ).fetchone()
        features: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = features.setdefault(
                row["feature_id"],
                {"feature_id": row["feature_id"], "calls": 0, "cost_est_cny": 0.0, "cost_actual_cny": 0.0, "statuses": {}},
            )
            item["calls"] += int(row["calls"])
            item["cost_est_cny"] += float(row["cost_est_cny"])
            item["cost_actual_cny"] += float(row["cost_actual_cny"])
            item["statuses"][row["status"]] = int(row["calls"])
        return {
            "ok": not self.degraded,
            "cycle": cycle,
            "limit_cny": _round_money(float(cycle_row["limit_cny"])) if cycle_row else self._limit_cny(),
            "spent_cny": _round_money(float(cycle_row["spent_cny"])) if cycle_row else 0.0,
            "reserved_cny": _round_money(float(cycle_row["reserved_cny"])) if cycle_row else 0.0,
            "features": list(features.values()),
        }

    def reset_cycle(self, *, expected_revision: int | None = None) -> dict[str, Any]:
        """Reset the current cycle counters without deleting historical calls."""
        with self._lock:
            self._bump_revision(expected_revision)
            self._save_config()
            cycle = self._cycle()
            with self._connect() as conn:
                self._ensure_cycle(conn, cycle)
                conn.execute(
                    "UPDATE aux_cycle SET spent_cny=0, reserved_cny=0, limit_cny=?, state=?, updated_at=? WHERE cycle=?",
                    (self._limit_cny(), self._state(), self._iso(), cycle),
                )
                self._update_state(conn, cycle)
                conn.commit()
            return self.status()
