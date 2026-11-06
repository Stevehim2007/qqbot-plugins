"""Crash-safe local ledgers for confirmations, idempotency and turn state.

Memory/session bodies stay in the authoritative backend.  This SQLite ledger
stores only operation identity, payload hashes, revisions, statuses and
secret-free audit summaries.  ``begin_*`` commits a pending intent before the
backend write, so a crash leaves a visible unknown/incomplete state instead of
allowing a blind duplicate submit.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from .contracts import (
    STATUS_CONFIRMATION_EXPIRED,
    STATUS_INVALID_ARGS,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PERMISSION_DENIED,
    STATUS_REVISION_CONFLICT,
    RequestContext,
    ScopeRef,
    canonical_json,
    error_result,
    make_result,
)

_TERMINAL_OK = "ok"
_PENDING = "pending"
_FAILED = "failed"
_UNKNOWN = "unknown"
_SUMMARY_KEYS = frozenset(
    {
        "id",
        "key",
        "action",
        "count",
        "preview_count",
        "revision",
        "conversation_id",
        "persona_id",
        "title",
        "status",
        "message_count",
        "role",
        "sequence",
        "turn_id",
        "scope_key",
        "target_id",
        "kind",
    }
)


def _scope_key(scope: ScopeRef | Mapping[str, Any] | str) -> str:
    if isinstance(scope, ScopeRef):
        return scope.canonical_id
    if isinstance(scope, Mapping):
        return str(scope.get("canonical_id") or "").strip()
    return str(scope or "").strip()


def _json(value: Any) -> str:
    return canonical_json(value)


def _from_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _identity(context: RequestContext) -> tuple[str, str, str, str]:
    return (
        str(context.instance_id),
        str(context.platform),
        str(context.bot_id),
        str(context.actor_id),
    )


def safe_audit_summary(value: Any) -> dict[str, Any]:
    """Whitelist non-body metadata so audit rows cannot retain memory text."""

    if not isinstance(value, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key, raw in value.items():
        name = str(key or "").strip().lower()
        if name not in _SUMMARY_KEYS:
            continue
        if isinstance(raw, (str, int, float, bool)) or raw is None:
            if isinstance(raw, str):
                summary[name] = raw[:200]
            else:
                summary[name] = raw
    return summary


class AsyncKeyedLocks:
    """Per-process serialization for one conversation/persona scope."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        name = str(key or "").strip() or "global"
        async with self._guard:
            lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            yield


class OperationStore:
    """SQLite ledger with a single guarded connection.

    The ledger is intentionally not the business database.  It serializes its
    own transactions and records only hashes/summaries; backend CAS remains the
    authority for the actual memory/session/persona revision.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str = ":memory:",
        *,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = str(path or ":memory:")
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(24))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            if self.path != ":memory:":
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    instance_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (
                        instance_id, platform, bot_id, actor_id, scope_key,
                        operation, business_key
                    )
                );
                CREATE TABLE IF NOT EXISTS confirmations (
                    token TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    revision_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    consumed_at REAL,
                    cancelled_at REAL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    instance_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (
                        instance_id, platform, bot_id, scope_key,
                        conversation_id, persona_id, turn_id, role, sequence
                    )
                );
                CREATE TABLE IF NOT EXISTS audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    payload_hash TEXT,
                    summary_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO ledger_meta(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------
    # generic idempotency
    # ------------------------------------------------------------------
    def begin_idempotent(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        payload_hash: str,
        scope_key: str = "",
    ) -> dict[str, Any]:
        operation = str(operation or "").strip()
        business_key = str(business_key or "").strip()
        payload_hash = str(payload_hash or "").strip()
        if not operation or not business_key or not payload_hash:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    """
                    SELECT payload_hash, status, result_json
                    FROM idempotency
                    WHERE instance_id=? AND platform=? AND bot_id=? AND actor_id=?
                      AND scope_key=? AND operation=? AND business_key=?
                    """,
                    (
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        str(scope_key or ""),
                        operation,
                        business_key,
                    ),
                ).fetchone()
                if row is not None:
                    if str(row["payload_hash"]) != payload_hash:
                        self._conn.execute("COMMIT")
                        return error_result(
                            STATUS_REVISION_CONFLICT,
                            "idempotency_payload_mismatch",
                        )
                    if row["status"] == _PENDING:
                        self._conn.execute("COMMIT")
                        return error_result(
                            STATUS_OUTCOME_UNKNOWN,
                            "idempotency_in_progress",
                            retryable=False,
                            side_effect_state="unknown",
                        )
                    self._conn.execute("COMMIT")
                    stored = _from_json(row["result_json"], {})
                    result = dict(stored) if isinstance(stored, dict) else {}
                    result.setdefault("status", row["status"])
                    result.setdefault("data", {})
                    result.setdefault("error_code", None)
                    result["idempotent_replay"] = True
                    return result
                self._conn.execute(
                    """
                    INSERT INTO idempotency(
                        instance_id, platform, bot_id, actor_id, scope_key,
                        operation, business_key, payload_hash, status,
                        result_json, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        str(scope_key or ""),
                        operation,
                        business_key,
                        payload_hash,
                        _PENDING,
                        None,
                        now,
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
        return {"status": "proceed", "data": {}, "error_code": None}

    def complete_idempotent(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        payload_hash: str,
        scope_key: str = "",
        result: Mapping[str, Any],
    ) -> None:
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        status = str(result.get("status") or _FAILED)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE idempotency
                    SET status=?, result_json=?, updated_at=?
                    WHERE instance_id=? AND platform=? AND bot_id=? AND actor_id=?
                      AND scope_key=? AND operation=? AND business_key=?
                      AND payload_hash=? AND status=?
                    """,
                    (
                        status,
                        _json(dict(result)),
                        now,
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        str(scope_key or ""),
                        str(operation),
                        str(business_key),
                        str(payload_hash),
                        _PENDING,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def lookup_idempotent(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        scope_key: str = "",
    ) -> dict[str, Any] | None:
        instance_id, platform, bot_id, actor_id = _identity(context)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT payload_hash, status, result_json
                FROM idempotency
                WHERE instance_id=? AND platform=? AND bot_id=? AND actor_id=?
                  AND scope_key=? AND operation=? AND business_key=?
                """,
                (
                    instance_id,
                    platform,
                    bot_id,
                    actor_id,
                    str(scope_key or ""),
                    str(operation),
                    str(business_key),
                ),
            ).fetchone()
        if row is None:
            return None
        return {
            "payload_hash": row["payload_hash"],
            "status": row["status"],
            "result": _from_json(row["result_json"], None),
        }

    # ------------------------------------------------------------------
    # append/turn ledger
    # ------------------------------------------------------------------
    def begin_append(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        payload_hash: str,
        scope_key: str,
        conversation_id: str,
        persona_id: str,
        turn_id: str,
        role: str,
        sequence: int,
    ) -> dict[str, Any]:
        operation = str(operation or "session_append").strip()
        business_key = str(business_key or "").strip()
        payload_hash = str(payload_hash or "").strip()
        if not business_key or not payload_hash:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT payload_hash, status, result_json
                    FROM idempotency
                    WHERE instance_id=? AND platform=? AND bot_id=? AND actor_id=?
                      AND scope_key=? AND operation=? AND business_key=?
                    """,
                    (
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        scope_key,
                        operation,
                        business_key,
                    ),
                ).fetchone()
                if row is not None:
                    if str(row["payload_hash"]) != payload_hash:
                        self._conn.execute("COMMIT")
                        return error_result(
                            STATUS_REVISION_CONFLICT,
                            "idempotency_payload_mismatch",
                        )
                    if row["status"] == _PENDING:
                        self._conn.execute("COMMIT")
                        return error_result(
                            STATUS_OUTCOME_UNKNOWN,
                            "idempotency_in_progress",
                            side_effect_state="unknown",
                        )
                    self._conn.execute("COMMIT")
                    stored = _from_json(row["result_json"], {})
                    result = dict(stored) if isinstance(stored, dict) else {}
                    result.setdefault("status", row["status"])
                    result.setdefault("data", {})
                    result.setdefault("error_code", None)
                    result["idempotent_replay"] = True
                    return result

                existing_turn = self._conn.execute(
                    """
                    SELECT status, result_json FROM turns
                    WHERE instance_id=? AND platform=? AND bot_id=? AND scope_key=?
                      AND conversation_id=? AND persona_id=? AND turn_id=?
                      AND role=? AND sequence=?
                    """,
                    (
                        instance_id,
                        platform,
                        bot_id,
                        scope_key,
                        str(conversation_id),
                        str(persona_id),
                        str(turn_id),
                        str(role),
                        int(sequence),
                    ),
                ).fetchone()
                if existing_turn is not None:
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_REVISION_CONFLICT,
                        "turn_key_conflict",
                        side_effect_state="unknown",
                    )

                self._conn.execute(
                    """
                    INSERT INTO idempotency(
                        instance_id, platform, bot_id, actor_id, scope_key,
                        operation, business_key, payload_hash, status,
                        result_json, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        scope_key,
                        operation,
                        business_key,
                        payload_hash,
                        _PENDING,
                        None,
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    """
                    INSERT INTO turns(
                        instance_id, platform, bot_id, actor_id, scope_key,
                        conversation_id, persona_id, turn_id, role, sequence,
                        status, payload_hash, result_json, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        scope_key,
                        str(conversation_id),
                        str(persona_id),
                        str(turn_id),
                        str(role),
                        int(sequence),
                        _PENDING,
                        payload_hash,
                        None,
                        now,
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
        return {"status": "proceed", "data": {}, "error_code": None}

    def complete_append(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        payload_hash: str,
        scope_key: str,
        conversation_id: str,
        persona_id: str,
        turn_id: str,
        role: str,
        sequence: int,
        result: Mapping[str, Any],
    ) -> None:
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        status = str(result.get("status") or _FAILED)
        turn_status = _TERMINAL_OK if status == STATUS_OK else (
            _UNKNOWN if status == STATUS_OUTCOME_UNKNOWN else _FAILED
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE idempotency
                    SET status=?, result_json=?, updated_at=?
                    WHERE instance_id=? AND platform=? AND bot_id=? AND actor_id=?
                      AND scope_key=? AND operation=? AND business_key=?
                      AND payload_hash=? AND status=?
                    """,
                    (
                        status,
                        _json(dict(result)),
                        now,
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        scope_key,
                        operation,
                        business_key,
                        payload_hash,
                        _PENDING,
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE turns
                    SET status=?, result_json=?, updated_at=?
                    WHERE instance_id=? AND platform=? AND bot_id=? AND scope_key=?
                      AND conversation_id=? AND persona_id=? AND turn_id=?
                      AND role=? AND sequence=?
                    """,
                    (
                        turn_status,
                        _json(dict(result)),
                        now,
                        instance_id,
                        platform,
                        bot_id,
                        scope_key,
                        str(conversation_id),
                        str(persona_id),
                        str(turn_id),
                        str(role),
                        int(sequence),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def incomplete_turns(
        self,
        context: RequestContext,
        *,
        scope_key: str,
        conversation_id: str,
        persona_id: str,
    ) -> list[dict[str, Any]]:
        instance_id, platform, bot_id, _ = _identity(context)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT turn_id, role, sequence, status, payload_hash, updated_at
                FROM turns
                WHERE instance_id=? AND platform=? AND bot_id=? AND scope_key=?
                  AND conversation_id=? AND persona_id=? AND status<>?
                ORDER BY updated_at ASC
                """,
                (
                    instance_id,
                    platform,
                    bot_id,
                    str(scope_key),
                    str(conversation_id),
                    str(persona_id),
                    _TERMINAL_OK,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # confirmation tokens
    # ------------------------------------------------------------------
    def create_confirmation(
        self,
        context: RequestContext,
        *,
        scope_key: str,
        scope_view: Mapping[str, Any] | None = None,
        persona_id: str,
        payload_hash: str,
        revision: Any,
        ttl_seconds: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ttl = max(1, min(600, int(ttl_seconds or 1)))
        token = str(self._token_factory() or "").strip()
        if not token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        expires_at = now + ttl
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO confirmations(
                        token, instance_id, platform, bot_id, actor_id,
                        scope_key, scope_json, persona_id, payload_hash,
                        revision_json, metadata_json, status, created_at,
                        expires_at, consumed_at, cancelled_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        token,
                        instance_id,
                        platform,
                        bot_id,
                        actor_id,
                        str(scope_key),
                        _json(dict(scope_view or {})),
                        str(persona_id),
                        str(payload_hash),
                        _json(revision),
                        _json(safe_audit_summary(metadata or {})),
                        "pending",
                        now,
                        expires_at,
                        None,
                        None,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return make_result(
            STATUS_OK,
            data={
                "confirmation_token": token,
                "expires_at": expires_at,
                "revision": revision,
                **safe_audit_summary(metadata or {}),
            },
        )

    def get_confirmation(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM confirmations WHERE token=?",
                (str(token or ""),),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["revision"] = _from_json(record.pop("revision_json", ""), None)
        record["scope"] = _from_json(record.pop("scope_json", ""), {})
        record["metadata"] = _from_json(record.pop("metadata_json", ""), {})
        return record

    def consume_confirmation(
        self,
        token: str,
        context: RequestContext,
        *,
        scope_key: str,
        persona_id: str,
        payload_hash: str,
        revision: Any,
    ) -> dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM confirmations WHERE token=?",
                    (token,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_CONFIRMATION_EXPIRED,
                        "confirmation_not_found",
                    )
                if (
                    str(row["instance_id"]) != instance_id
                    or str(row["platform"]) != platform
                    or str(row["bot_id"]) != bot_id
                    or str(row["actor_id"]) != actor_id
                ):
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_PERMISSION_DENIED,
                        "confirmation_actor_mismatch",
                    )
                if str(row["scope_key"]) != str(scope_key):
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_PERMISSION_DENIED,
                        "confirmation_scope_mismatch",
                    )
                if str(row["persona_id"]) != str(persona_id):
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_PERMISSION_DENIED,
                        "confirmation_scope_mismatch",
                    )
                if str(row["payload_hash"]) != str(payload_hash):
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_REVISION_CONFLICT,
                        "confirmation_payload_mismatch",
                    )
                expected_revision = _from_json(row["revision_json"], None)
                if _json(expected_revision) != _json(revision):
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_REVISION_CONFLICT,
                        "stale_revision",
                        data={
                            "expected_revision": expected_revision,
                            "current_revision": revision,
                        },
                    )
                if float(row["expires_at"]) <= now:
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_CONFIRMATION_EXPIRED,
                        "confirmation_expired",
                    )
                if str(row["status"]) != "pending":
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_CONFIRMATION_EXPIRED,
                        "confirmation_replayed",
                        side_effect_state="unknown",
                    )
                self._conn.execute(
                    "UPDATE confirmations SET status='consumed', consumed_at=? WHERE token=?",
                    (now, token),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
        return make_result(
            STATUS_OK,
            data={
                "confirmation_token": token,
                "scope_key": str(row["scope_key"]),
                "scope": _from_json(row["scope_json"], {}),
                "persona_id": str(row["persona_id"]),
                "payload_hash": str(row["payload_hash"]),
                "revision": expected_revision,
                **safe_audit_summary(
                    _from_json(row["metadata_json"], {}) or {}
                ),
            },
        )

    def cancel_confirmation(
        self,
        token: str,
        context: RequestContext,
    ) -> dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        instance_id, platform, bot_id, actor_id = _identity(context)
        now = float(self._clock())
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM confirmations WHERE token=?",
                    (token,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_NOT_FOUND,
                        "confirmation_not_found",
                    )
                if (
                    str(row["instance_id"]) != instance_id
                    or str(row["platform"]) != platform
                    or str(row["bot_id"]) != bot_id
                    or str(row["actor_id"]) != actor_id
                ):
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_PERMISSION_DENIED,
                        "confirmation_actor_mismatch",
                    )
                status = str(row["status"])
                if status == "cancelled":
                    self._conn.execute("COMMIT")
                    return make_result(
                        STATUS_OK,
                        data={"confirmation_token": token, "cancelled": True},
                    )
                if status != "pending":
                    self._conn.execute("COMMIT")
                    return error_result(
                        STATUS_CONFIRMATION_EXPIRED,
                        "confirmation_replayed",
                    )
                self._conn.execute(
                    "UPDATE confirmations SET status='cancelled', cancelled_at=? WHERE token=?",
                    (now, token),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise
        return make_result(
            STATUS_OK,
            data={"confirmation_token": token, "cancelled": True},
        )

    # ------------------------------------------------------------------
    # secret-free audit
    # ------------------------------------------------------------------
    def append_audit(
        self,
        context: RequestContext,
        *,
        operation: str,
        scope_key: str,
        status: str,
        error_code: str | None = None,
        payload_hash: str | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> None:
        instance_id, platform, bot_id, actor_id = _identity(context)
        sanitized = safe_audit_summary(summary or {})
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit(
                    instance_id, platform, bot_id, actor_id, scope_key,
                    operation, status, error_code, payload_hash,
                    summary_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    instance_id,
                    platform,
                    bot_id,
                    actor_id,
                    str(scope_key or ""),
                    str(operation or ""),
                    str(status or ""),
                    str(error_code)[:96] if error_code else None,
                    str(payload_hash)[:128] if payload_hash else None,
                    _json(sanitized),
                    float(self._clock()),
                ),
            )

    def audit_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit ORDER BY audit_id ASC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["summary"] = _from_json(item.pop("summary_json", ""), {})
            result.append(item)
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = {}
            for table in ("idempotency", "confirmations", "turns", "audit"):
                counts[table] = int(
                    self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
        return {
            "schema_version": self.SCHEMA_VERSION,
            "path_configured": self.path != ":memory:",
            "counts": counts,
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.DatabaseError:
                pass
