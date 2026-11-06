"""Persistent SQLite call, confirmation and idempotency ledger.

The ledger is deliberately narrow: parameterized statements only, one small
fixed schema, ``BEGIN IMMEDIATE`` transactions, a busy timeout and an owner-only
file mode.  It never accepts SQL, filesystem paths or arbitrary table names from
a caller.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .contracts import (
    ContractError,
    RequestContext,
    ToolResult,
    canonical_json,
    new_call_id,
    operation_spec,
    payload_hash,
)

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")
_CONFIRM_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_SCOPE_RE = re.compile(r"^[a-z]{5,16}$")
_CALL_STATUSES = frozenset(
    {"pending", "success", "ok", "error", "denied", "not_ready", "outcome_unknown", "unknown"}
)


class LedgerError(ValueError):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400) -> None:
        self.code = str(code)
        self.message = str(message or code)
        self.http_status = int(http_status)
        super().__init__(self.message)


class LedgerConflict(LedgerError):
    def __init__(self, message: str = "idempotency payload conflict") -> None:
        super().__init__("idempotency_conflict", message, http_status=409)


class LedgerNotFound(LedgerError):
    def __init__(self, message: str = "call is unknown") -> None:
        super().__init__("not_found", message, http_status=404)


class LedgerStateError(LedgerError):
    def __init__(self, message: str = "ledger state conflict") -> None:
        super().__init__("revision_conflict", message, http_status=409)


class LedgerConfirmationError(LedgerError):
    pass


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    replayed: bool
    call_id: str
    status: str
    payload_hash: str
    result: dict[str, Any] | None = None
    error_code: str | None = None
    retryable: bool = False
    side_effect_state: str = "none"
    provider_operation_id: str = ""
    http_status: int = 200


def _safe_text(value: Any, name: str, *, max_chars: int = 256) -> str:
    if not isinstance(value, str):
        raise LedgerError("invalid_args", f"{name} must be a string")
    text = value.strip()
    if not text or len(text) > max_chars:
        raise LedgerError("invalid_args", f"{name} has invalid length")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise LedgerError("invalid_args", f"{name} has control characters")
    return text


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


class CallLedger:
    """Small SQLite ledger shared by safe_tools' call and confirmation paths."""

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_ms: int = 5_000) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise LedgerError("invalid_args", "busy_timeout_ms must be an integer")
        if not 100 <= busy_timeout_ms <= 120_000:
            raise LedgerError("invalid_args", "busy_timeout_ms is out of range")
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._memory = str(path) == ":memory:"
        self._closed = False
        if self._memory:
            self.path = ":memory:"
            self._memory_conn = sqlite3.connect(
                ":memory:", check_same_thread=False, timeout=busy_timeout_ms / 1000.0
            )
            self._configure_connection(self._memory_conn)
            self._create_schema(self._memory_conn)
        else:
            ledger_path = Path(path).expanduser()
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                str(ledger_path),
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            os.close(fd)
            try:
                os.chmod(ledger_path, 0o600)
            except OSError:
                pass
            self.path = str(ledger_path)
            with self._connection() as conn:
                self._create_schema(conn)
            self._secure_modes()

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise LedgerError("invalid_state", "ledger is closed")
        if self._memory:
            with self._lock:
                yield self._memory_conn
            return
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        try:
            self._configure_connection(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise LedgerError("invalid_state", "ledger is closed")
        if self._memory:
            with self._lock:
                conn = self._memory_conn
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        try:
            self._configure_connection(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except sqlite3.DatabaseError:
                pass
            raise
        finally:
            conn.close()
            self._secure_modes()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                owner_epoch INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error_code TEXT,
                retryable INTEGER NOT NULL DEFAULT 0,
                side_effect_state TEXT NOT NULL DEFAULT 'none',
                provider_operation_id TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(instance_id, bot_id, actor_id, scope, operation, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_calls_status
                ON calls(status, updated_at);
            CREATE TABLE IF NOT EXISTS confirmations (
                confirm_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                consumed_at REAL,
                provider_operation_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_confirmations_status
                ON confirmations(status, expires_at);
            """
        )
        conn.commit()

    def _secure_modes(self) -> None:
        if self._memory:
            return
        for suffix in ("", "-wal", "-shm"):
            target = self.path + suffix
            try:
                os.chmod(target, 0o600)
            except FileNotFoundError:
                continue
            except OSError:
                continue

    @staticmethod
    def _row_to_decision(row: sqlite3.Row, *, replayed: bool) -> IdempotencyDecision:
        result = _decode_json(row["result_json"])
        return IdempotencyDecision(
            replayed=replayed,
            call_id=str(row["call_id"]),
            status=str(row["status"]),
            payload_hash=str(row["payload_hash"]),
            result=result if isinstance(result, dict) else None,
            error_code=row["error_code"],
            retryable=bool(row["retryable"]),
            side_effect_state=str(row["side_effect_state"]),
            provider_operation_id=str(row["provider_operation_id"] or ""),
        )

    def begin_call(
        self,
        context: RequestContext,
        operation: str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None = None,
        *,
        call_id: str | None = None,
        now: float | None = None,
    ) -> IdempotencyDecision:
        """Reserve one idempotency key atomically.

        Same key + same canonical payload returns the original row; different
        payload raises :class:`LedgerConflict` with HTTP 409 semantics.
        """

        context.validate()
        spec = operation_spec(operation)
        key = _safe_text(idempotency_key, "idempotency_key", max_chars=256)
        if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
            raise LedgerError("invalid_args", "idempotency_key contains unsafe characters")
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise LedgerError("invalid_args", "payload must be an object")
        payload_dict = dict(payload)
        digest = payload_hash(payload_dict)
        call = call_id or new_call_id()
        if len(call) > 128 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", call):
            raise LedgerError("invalid_args", "call_id is invalid")
        now_value = float(now if now is not None else time.time())
        payload_text = canonical_json(payload_dict)
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM calls
                WHERE instance_id=? AND bot_id=? AND actor_id=? AND scope=?
                  AND operation=? AND idempotency_key=?
                """,
                (
                    context.instance_id,
                    context.bot_id,
                    context.actor_id,
                    context.requested_scope,
                    spec.operation,
                    key,
                ),
            ).fetchone()
            if row is not None:
                if str(row["payload_hash"]) != digest:
                    raise LedgerConflict()
                return self._row_to_decision(row, replayed=True)
            try:
                conn.execute(
                    """
                    INSERT INTO calls (
                        call_id, instance_id, bot_id, actor_id, scope, operation,
                        idempotency_key, payload_hash, payload_json, owner_epoch,
                        status, result_json, error_code, retryable,
                        side_effect_state, provider_operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 0,
                              'none', '', ?, ?)
                    """,
                    (
                        call,
                        context.instance_id,
                        context.bot_id,
                        context.actor_id,
                        context.requested_scope,
                        spec.operation,
                        key,
                        digest,
                        payload_text,
                        context.owner_epoch,
                        now_value,
                        now_value,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConflict("call_id or idempotency key already exists") from exc
            row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call,)).fetchone()
            if row is None:
                raise LedgerError("internal_error", "reserved call disappeared")
            return self._row_to_decision(row, replayed=False)

    reserve_call = begin_call

    def complete_call(
        self,
        call_id: str,
        result: ToolResult | Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        call = _safe_text(call_id, "call_id", max_chars=128)
        if isinstance(result, ToolResult):
            result.validate()
            result_dict = result.to_dict()
        elif isinstance(result, Mapping):
            result_dict = dict(result)
            canonical_json(result_dict)
        else:
            raise LedgerError("invalid_args", "result must be a ToolResult or object")
        status = str(result_dict.get("status", ""))
        if status not in _CALL_STATUSES or status == "pending":
            raise LedgerError("invalid_args", "terminal result status is invalid")
        if result_dict.get("call_id") not in (None, call):
            raise LedgerError("invalid_args", "result call_id does not match ledger call")
        now_value = float(now if now is not None else time.time())
        result_text = canonical_json(result_dict)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call,)).fetchone()
            if row is None:
                raise LedgerNotFound()
            if row["status"] != "pending":
                existing = _decode_json(row["result_json"])
                if existing == result_dict:
                    return self._public_call_row(row)
                raise LedgerStateError()
            conn.execute(
                """
                UPDATE calls SET status=?, result_json=?, error_code=?, retryable=?,
                    side_effect_state=?, provider_operation_id=?, updated_at=?
                WHERE call_id=? AND status='pending'
                """,
                (
                    status,
                    result_text,
                    result_dict.get("error_code"),
                    1 if result_dict.get("retryable") else 0,
                    str(result_dict.get("side_effect_state", "none")),
                    str(result_dict.get("provider_operation_id", "") or ""),
                    now_value,
                    call,
                ),
            )
            row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call,)).fetchone()
            if row is None:
                raise LedgerError("internal_error", "completed call disappeared")
            return self._public_call_row(row)

    def mark_unknown(
        self,
        call_id: str,
        *,
        provider_operation_id: str = "",
        error_code: str = "outcome_unknown",
        now: float | None = None,
    ) -> dict[str, Any]:
        result = ToolResult(
            status="outcome_unknown",
            call_id=call_id,
            result_type="error",
            data=None,
            error_code=error_code,
            retryable=False,
            side_effect_state="unknown",
            provider_operation_id=provider_operation_id,
        )
        return self.complete_call(call_id, result, now=now)

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()
            return self._full_call_row(row) if row is not None else None

    def get_by_idempotency(
        self,
        context: RequestContext,
        operation: str,
        idempotency_key: str,
    ) -> IdempotencyDecision | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM calls
                WHERE instance_id=? AND bot_id=? AND actor_id=? AND scope=?
                  AND operation=? AND idempotency_key=?
                """,
                (
                    context.instance_id,
                    context.bot_id,
                    context.actor_id,
                    context.requested_scope,
                    operation,
                    idempotency_key,
                ),
            ).fetchone()
            return self._row_to_decision(row, replayed=True) if row is not None else None

    @staticmethod
    def _public_call_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "call_id": str(row["call_id"]),
            "operation": str(row["operation"]),
            "status": str(row["status"]),
            "error_code": row["error_code"],
            "retryable": bool(row["retryable"]),
            "side_effect_state": str(row["side_effect_state"]),
            "provider_operation_id": str(row["provider_operation_id"] or ""),
            "owner_epoch": int(row["owner_epoch"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _full_call_row(row: sqlite3.Row) -> dict[str, Any]:
        public = CallLedger._public_call_row(row)
        public["instance_id"] = str(row["instance_id"])
        public["bot_id"] = str(row["bot_id"])
        public["actor_id"] = str(row["actor_id"])
        public["scope"] = str(row["scope"])
        public["result"] = _decode_json(row["result_json"])
        public["payload_hash"] = str(row["payload_hash"])
        return public

    def operation_status(self, call_id: str) -> dict[str, Any]:
        row = self.get_call(call_id)
        if row is None:
            return {
                "call_id": str(call_id),
                "status": "unknown",
                "error_code": "not_found",
                "retryable": False,
                "side_effect_state": "unknown",
            }
        return row

    # ------------------------------------------------------------------
    # Confirmation ledger
    # ------------------------------------------------------------------
    def create_confirmation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise LedgerError("invalid_args", "confirmation record must be an object")
        required = (
            "confirm_id",
            "token",
            "actor_id",
            "instance_id",
            "conversation_id",
            "persona_id",
            "scope",
            "action",
            "payload_hash",
            "payload_json",
            "revision",
            "status",
            "created_at",
            "expires_at",
        )
        if any(key not in record for key in required):
            raise LedgerError("invalid_args", "confirmation record is incomplete")
        values = dict(record)
        confirm_id = _safe_text(values["confirm_id"], "confirm_id", max_chars=128)
        if not _CONFIRM_ID_RE.fullmatch(confirm_id):
            raise LedgerError("invalid_args", "confirm_id is invalid")
        for name in (
            "token",
            "actor_id",
            "instance_id",
            "conversation_id",
            "persona_id",
            "scope",
            "action",
            "payload_hash",
            "payload_json",
            "status",
        ):
            _safe_text(values[name], name, max_chars=8192 if name in {"token", "payload_json"} else 256)
        try:
            revision = int(values["revision"])
            created_at = float(values["created_at"])
            expires_at = float(values["expires_at"])
        except (TypeError, ValueError) as exc:
            raise LedgerError("invalid_args", "confirmation numeric fields invalid") from exc
        if revision < 0 or expires_at <= created_at:
            raise LedgerError("invalid_args", "confirmation times/revision invalid")
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO confirmations (
                        confirm_id, token, actor_id, instance_id, conversation_id,
                        persona_id, scope, action, payload_hash, payload_json,
                        revision, status, created_at, expires_at, consumed_at,
                        provider_operation_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        confirm_id,
                        str(values["token"]),
                        str(values["actor_id"]),
                        str(values["instance_id"]),
                        str(values["conversation_id"]),
                        str(values["persona_id"]),
                        str(values["scope"]),
                        str(values["action"]),
                        str(values["payload_hash"]),
                        str(values["payload_json"]),
                        revision,
                        str(values["status"]),
                        created_at,
                        expires_at,
                        str(values.get("provider_operation_id", "") or ""),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerConfirmationError(
                    "confirmation_invalid", "confirmation already exists", http_status=409
                ) from exc
            row = conn.execute(
                "SELECT * FROM confirmations WHERE confirm_id=?", (confirm_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("internal_error", "confirmation disappeared")
            return self._confirmation_row(row)

    @staticmethod
    def _confirmation_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "confirm_id": str(row["confirm_id"]),
            "token": str(row["token"]),
            "actor_id": str(row["actor_id"]),
            "instance_id": str(row["instance_id"]),
            "conversation_id": str(row["conversation_id"]),
            "persona_id": str(row["persona_id"]),
            "scope": str(row["scope"]),
            "action": str(row["action"]),
            "payload_hash": str(row["payload_hash"]),
            "payload": _decode_json(row["payload_json"]),
            "payload_json": str(row["payload_json"]),
            "revision": int(row["revision"]),
            "status": str(row["status"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
            "consumed_at": row["consumed_at"],
            "provider_operation_id": str(row["provider_operation_id"] or ""),
        }

    def get_confirmation(self, confirm_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE confirm_id=?", (str(confirm_id),)
            ).fetchone()
            return self._confirmation_row(row) if row is not None else None

    def claim_confirmation(
        self,
        confirm_id: str,
        *,
        actor_id: str,
        instance_id: str,
        conversation_id: str,
        persona_id: str,
        scope: str,
        payload_hash: str,
        revision: int,
        now: float,
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE confirm_id=?", (str(confirm_id),)
            ).fetchone()
            if row is None:
                raise LedgerConfirmationError("confirmation_invalid", "not found", http_status=404)
            if any(
                str(row[field]) != str(value)
                for field, value in (
                    ("actor_id", actor_id),
                    ("instance_id", instance_id),
                    ("conversation_id", conversation_id),
                    ("persona_id", persona_id),
                    ("scope", scope),
                )
            ):
                raise LedgerConfirmationError("permission_denied", http_status=403)
            status = str(row["status"])
            if status in {"consumed", "cancelled"}:
                raise LedgerConfirmationError("confirmation_replayed", http_status=409)
            if status == "expired" or float(now) >= float(row["expires_at"]):
                conn.execute(
                    "UPDATE confirmations SET status='expired' WHERE confirm_id=?",
                    (str(confirm_id),),
                )
                raise LedgerConfirmationError("confirmation_expired", http_status=410)
            if status != "previewed":
                raise LedgerConfirmationError("confirmation_invalid", http_status=409)
            if str(row["payload_hash"]) != str(payload_hash) or int(row["revision"]) != int(revision):
                raise LedgerConfirmationError("revision_conflict", http_status=409)
            conn.execute(
                """
                UPDATE confirmations SET status='consumed', consumed_at=?
                WHERE confirm_id=? AND status='previewed'
                """,
                (float(now), str(confirm_id)),
            )
            updated = conn.execute(
                "SELECT * FROM confirmations WHERE confirm_id=?", (str(confirm_id),)
            ).fetchone()
            if updated is None:
                raise LedgerError("internal_error", "confirmation disappeared")
            return self._confirmation_row(updated)

    def cancel_confirmation(
        self,
        confirm_id: str,
        *,
        actor_id: str,
        instance_id: str,
        conversation_id: str,
        persona_id: str,
        scope: str,
        now: float,
    ) -> dict[str, Any]:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM confirmations WHERE confirm_id=?", (str(confirm_id),)
            ).fetchone()
            if row is None:
                raise LedgerConfirmationError("confirmation_invalid", "not found", http_status=404)
            if any(
                str(row[field]) != str(value)
                for field, value in (
                    ("actor_id", actor_id),
                    ("instance_id", instance_id),
                    ("conversation_id", conversation_id),
                    ("persona_id", persona_id),
                    ("scope", scope),
                )
            ):
                raise LedgerConfirmationError("permission_denied", http_status=403)
            status = str(row["status"])
            if status in {"consumed", "cancelled"}:
                raise LedgerConfirmationError("confirmation_replayed", http_status=409)
            if status == "expired" or float(now) >= float(row["expires_at"]):
                conn.execute(
                    "UPDATE confirmations SET status='expired' WHERE confirm_id=?",
                    (str(confirm_id),),
                )
                raise LedgerConfirmationError("confirmation_expired", http_status=410)
            if status != "previewed":
                raise LedgerConfirmationError("confirmation_invalid", http_status=409)
            conn.execute(
                "UPDATE confirmations SET status='cancelled', consumed_at=? WHERE confirm_id=?",
                (float(now), str(confirm_id)),
            )
            updated = conn.execute(
                "SELECT * FROM confirmations WHERE confirm_id=?", (str(confirm_id),)
            ).fetchone()
            if updated is None:
                raise LedgerError("internal_error", "confirmation disappeared")
            return self._confirmation_row(updated)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._memory:
                try:
                    self._memory_conn.close()
                except sqlite3.DatabaseError:
                    pass
