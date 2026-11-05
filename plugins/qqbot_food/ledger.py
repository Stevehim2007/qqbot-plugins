"""SQLite ledger for Food drafts and provider operations.

The ledger is the canonical persistent state for the ABI v1 provider draft
state machine.  It is deliberately separate from qqbot_safe_tools' confirmation
ledger: safe_tools records dispatch/confirmation policy, while this ledger owns
the Food business payload, revision, idempotency key and remote outcome.

All SQL is fixed and parameterized.  No user-supplied SQL, path, URL or method
name is ever interpolated into a statement.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import contracts

LEDGER_VERSION = 1
ACTIVE_DRAFT_STATUSES = contracts.PRE_SUBMIT_STATUSES
INFLIGHT_STATUSES = {"confirmed", "submitting"}


class LedgerError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _loads(value: Any, default: Any) -> Any:
    try:
        loaded = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default
    return loaded


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for name in ("payload_json", "missing_json", "remote_data_json"):
        if name in result:
            result[name] = _loads(result[name], {} if name != "missing_json" else [])
    if "payload_json" in result:
        result["payload"] = result["payload_json"]
    if "missing_json" in result:
        result["missing_fields"] = result["missing_json"]
    if "remote_data_json" in result:
        result["remote_data"] = result["remote_data_json"]
    if "retryable" in result:
        result["retryable"] = bool(result["retryable"])
    return result


class FoodLedger:
    """Small WAL SQLite ledger with BEGIN IMMEDIATE CAS transactions."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Any = None,
        timeout: float = 8.0,
    ):
        self.path = Path(path)
        self._clock = clock or time.time
        self._timeout = max(0.5, float(timeout))
        self._mutex = threading.RLock()
        self._closed = False
        self.initialize()

    def _now(self) -> float:
        try:
            value = self._clock() if callable(self._clock) else self._clock
            return float(value)
        except Exception:
            return time.time()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path), timeout=self._timeout, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 8000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return conn

    @contextmanager
    def _tx(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise LedgerError("ledger closed")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def initialize(self) -> None:
        with self._mutex:
            with self._tx() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS drafts (
                        draft_id TEXT PRIMARY KEY,
                        actor_id TEXT NOT NULL,
                        instance_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        bot_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        persona_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        missing_json TEXT NOT NULL DEFAULT '[]',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        confirmation_expires_at REAL NOT NULL DEFAULT 0,
                        confirmation_token TEXT NOT NULL DEFAULT '',
                        last_operation_id TEXT NOT NULL DEFAULT '',
                        create_key TEXT UNIQUE
                    );
                    CREATE INDEX IF NOT EXISTS idx_drafts_actor
                        ON drafts(instance_id, bot_id, actor_id, scope_key);
                    CREATE TABLE IF NOT EXISTS operations (
                        op_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        draft_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        instance_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        bot_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        persona_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        side_effect_state TEXT NOT NULL DEFAULT 'none',
                        retryable INTEGER NOT NULL DEFAULT 0,
                        remote_code TEXT NOT NULL DEFAULT '',
                        remote_data_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT NOT NULL DEFAULT '',
                        message TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_operations_draft
                        ON operations(draft_id);
                    CREATE INDEX IF NOT EXISTS idx_operations_actor
                        ON operations(instance_id, bot_id, actor_id, scope_key);
                    CREATE TABLE IF NOT EXISTS scope_preferences (
                        instance_id TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        bot_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        location_json TEXT NOT NULL DEFAULT '{}',
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (instance_id, platform, bot_id, scope_key)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        draft_id TEXT NOT NULL DEFAULT '',
                        op_id TEXT NOT NULL DEFAULT '',
                        event TEXT NOT NULL,
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(LEDGER_VERSION),),
                )
                # A fresh per-ledger HMAC secret persists confirmation-token
                # verification across restarts; it is never written to logs.
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('token_secret', ?)",
                    (secrets.token_hex(32),),
                )
            self._recover_inflight()

    def _recover_inflight(self) -> None:
        now = self._now()
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT op_id, draft_id FROM operations "
                "WHERE status IN ('submitting', 'confirmed')"
            ).fetchall()
            for row in rows:
                op_id = str(row["op_id"])
                draft_id = str(row["draft_id"])
                conn.execute(
                    "UPDATE operations SET status='outcome_unknown', "
                    "side_effect_state='unknown', retryable=0, "
                    "error_code=?, message=?, updated_at=? WHERE op_id=?",
                    (
                        contracts.ERR_OUTCOME_UNKNOWN,
                        "进程重启时提交仍在途，结果未知，禁止自动重发",
                        now,
                        op_id,
                    ),
                )
                conn.execute(
                    "UPDATE drafts SET status='outcome_unknown', updated_at=? "
                    "WHERE draft_id=? AND status IN ('confirmed', 'submitting')",
                    (now, draft_id),
                )
                conn.execute(
                    "INSERT INTO events(draft_id, op_id, event, detail_json, created_at) "
                    "VALUES(?, ?, 'outcome_unknown', ?, ?)",
                    (
                        draft_id,
                        op_id,
                        _json({"reason": "inflight_at_startup"}),
                        now,
                    ),
                )
            # A draft stuck in confirmed/submitting without an operation row is
            # also fail-closed: never turn it back into awaiting_confirmation.
            conn.execute(
                "UPDATE drafts SET status='outcome_unknown', updated_at=? "
                "WHERE status IN ('confirmed', 'submitting')",
                (now,),
            )

    # ---- metadata / helpers -----------------------------------------
    def token_secret(self) -> str:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='token_secret'"
            ).fetchone()
        if row is None:
            raise LedgerError("token secret missing")
        return str(row["value"])

    @staticmethod
    def _auth_matches(
        row: dict[str, Any],
        *,
        actor_id: str,
        scope_key: str,
        persona_id: str,
    ) -> bool:
        return bool(
            str(row.get("actor_id", "")) == str(actor_id or "")
            and str(row.get("scope_key", "")) == str(scope_key or "")
            and str(row.get("persona_id", "")) == str(persona_id or "")
        )

    def _get_draft_row(
        self, conn: sqlite3.Connection, draft_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM drafts WHERE draft_id=?", (str(draft_id or ""),)
        ).fetchone()
        return _row_dict(row)

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        draft_id: str = "",
        op_id: str = "",
        event: str,
        detail: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO events(draft_id, op_id, event, detail_json, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                str(draft_id or ""),
                str(op_id or ""),
                str(event or ""),
                _json(detail or {}),
                float(now if now is not None else self._now()),
            ),
        )

    def _expire_if_needed(
        self, conn: sqlite3.Connection, draft: dict[str, Any], now: float
    ) -> dict[str, Any]:
        status = str(draft.get("status", ""))
        if status in contracts.PRE_SUBMIT_STATUSES:
            try:
                expires = float(draft.get("expires_at", 0) or 0)
            except (TypeError, ValueError):
                expires = 0
            if expires and now > expires:
                conn.execute(
                    "UPDATE drafts SET status='expired', updated_at=?, "
                    "confirmation_token='' WHERE draft_id=?",
                    (now, draft["draft_id"]),
                )
                self._event(
                    conn,
                    draft_id=draft["draft_id"],
                    event="expired",
                    detail={"from": status},
                    now=now,
                )
                updated = self._get_draft_row(conn, draft["draft_id"])
                return updated or draft
        return draft

    # ---- drafts -----------------------------------------------------
    def create_draft(
        self,
        *,
        draft_id: str,
        actor_id: str,
        instance_id: str,
        platform: str,
        bot_id: str,
        scope_key: str,
        persona_id: str,
        payload: dict[str, Any],
        payload_hash_value: str,
        revision: int,
        status: str,
        missing_fields: list[str],
        ttl_seconds: int,
        create_key: str = "",
        now: float | None = None,
        update_draft_id: str = "",
    ) -> dict[str, Any]:
        current = float(now if now is not None else self._now())
        expires = current + max(1, int(ttl_seconds))
        create_key_value = str(create_key or "") or None
        with self._tx() as conn:
            if create_key_value:
                existing = conn.execute(
                    "SELECT * FROM drafts WHERE create_key=?",
                    (create_key_value,),
                ).fetchone()
                if existing is not None:
                    row = _row_dict(existing) or {}
                    if str(row.get("payload_hash", "")) == str(payload_hash_value):
                        return {"kind": "replay", "draft": row}
                    return {"kind": "conflict", "draft": row}
            if update_draft_id:
                prior = self._get_draft_row(conn, update_draft_id)
                if prior is None:
                    return {"kind": "missing"}
                if not self._auth_matches(
                    prior,
                    actor_id=actor_id,
                    scope_key=scope_key,
                    persona_id=persona_id,
                ):
                    return {"kind": "forbidden"}
                previous_status = str(prior.get("status", ""))
                if previous_status not in (
                    "collecting",
                    "draft",
                    "awaiting_confirmation",
                    "rejected",
                ):
                    return {"kind": "invalid_state", "draft": prior}
                new_revision = int(prior.get("revision", 0) or 0) + 1
                if previous_status == "rejected":
                    new_status = status
                elif status == "collecting":
                    new_status = "collecting"
                elif str(prior.get("payload_hash", "")) == str(payload_hash_value):
                    new_status = previous_status
                else:
                    new_status = status
                conn.execute(
                    "UPDATE drafts SET payload_json=?, payload_hash=?, "
                    "revision=?, status=?, missing_json=?, updated_at=?, "
                    "expires_at=?, confirmation_expires_at=0, "
                    "confirmation_token='' WHERE draft_id=?",
                    (
                        _json(payload),
                        str(payload_hash_value),
                        new_revision,
                        new_status,
                        _json(list(missing_fields)),
                        current,
                        expires,
                        update_draft_id,
                    ),
                )
                self._event(
                    conn,
                    draft_id=update_draft_id,
                    event="draft_updated",
                    detail={"revision": new_revision, "status": new_status},
                    now=current,
                )
                return {
                    "kind": "updated",
                    "draft": self._get_draft_row(conn, update_draft_id),
                }
            conn.execute(
                "INSERT INTO drafts("
                "draft_id, actor_id, instance_id, platform, bot_id, scope_key, "
                "persona_id, payload_json, payload_hash, revision, status, "
                "missing_json, created_at, updated_at, expires_at, create_key"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(draft_id),
                    str(actor_id),
                    str(instance_id),
                    str(platform),
                    str(bot_id),
                    str(scope_key),
                    str(persona_id),
                    _json(payload),
                    str(payload_hash_value),
                    int(revision),
                    str(status),
                    _json(list(missing_fields)),
                    current,
                    current,
                    expires,
                    create_key_value,
                ),
            )
            self._event(
                conn,
                draft_id=str(draft_id),
                event="draft_created",
                detail={"status": str(status), "revision": int(revision)},
                now=current,
            )
            return {"kind": "created", "draft": self._get_draft_row(conn, draft_id)}

    def get_draft(self, draft_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            draft = self._get_draft_row(conn, draft_id)
            if draft is None:
                return None
            return self._expire_if_needed(conn, draft, current)

    def preview_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        scope_key: str,
        persona_id: str,
        confirmation_ttl_seconds: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            draft = self._get_draft_row(conn, draft_id)
            if draft is None:
                return {"kind": "missing"}
            draft = self._expire_if_needed(conn, draft, current)
            if not self._auth_matches(
                draft,
                actor_id=actor_id,
                scope_key=scope_key,
                persona_id=persona_id,
            ):
                return {"kind": "forbidden"}
            status = str(draft.get("status", ""))
            if status == "expired":
                return {"kind": "expired", "draft": draft}
            if status == "collecting":
                return {"kind": "incomplete", "draft": draft}
            if status == "draft":
                confirmation_expires = current + max(
                    1, int(confirmation_ttl_seconds)
                )
                conn.execute(
                    "UPDATE drafts SET status='awaiting_confirmation', "
                    "updated_at=?, confirmation_expires_at=? "
                    "WHERE draft_id=? AND status='draft'",
                    (current, confirmation_expires, str(draft_id)),
                )
                self._event(
                    conn,
                    draft_id=str(draft_id),
                    event="awaiting_confirmation",
                    detail={"revision": draft.get("revision")},
                    now=current,
                )
                return {
                    "kind": "ready",
                    "draft": self._get_draft_row(conn, draft_id),
                }
            if status == "awaiting_confirmation":
                return {"kind": "ready", "draft": draft}
            return {"kind": "invalid_state", "draft": draft}

    def store_confirmation_token(
        self,
        draft_id: str,
        *,
        actor_id: str,
        scope_key: str,
        persona_id: str,
        token: str,
        expires_at: float,
        now: float | None = None,
    ) -> bool:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            draft = self._get_draft_row(conn, draft_id)
            if draft is None or not self._auth_matches(
                draft,
                actor_id=actor_id,
                scope_key=scope_key,
                persona_id=persona_id,
            ):
                return False
            if str(draft.get("status", "")) != "awaiting_confirmation":
                return False
            conn.execute(
                "UPDATE drafts SET confirmation_token=?, "
                "confirmation_expires_at=?, updated_at=? WHERE draft_id=?",
                (str(token), float(expires_at), current, str(draft_id)),
            )
            return True

    def begin_submit(
        self,
        draft_id: str,
        *,
        actor_id: str,
        scope_key: str,
        persona_id: str,
        expected_revision: int,
        expected_payload_hash: str,
        idempotency_key: str,
        payload_hash_value: str,
        op_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM operations WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
            if existing is not None:
                op = _row_dict(existing) or {}
                if str(op.get("payload_hash", "")) == str(payload_hash_value):
                    draft = self._get_draft_row(conn, draft_id)
                    return {"kind": "replay", "operation": op, "draft": draft}
                return {"kind": "conflict", "operation": op}
            draft = self._get_draft_row(conn, draft_id)
            if draft is None:
                return {"kind": "missing"}
            draft = self._expire_if_needed(conn, draft, current)
            if not self._auth_matches(
                draft,
                actor_id=actor_id,
                scope_key=scope_key,
                persona_id=persona_id,
            ):
                return {"kind": "forbidden"}
            status = str(draft.get("status", ""))
            if status == "expired":
                return {"kind": "expired", "draft": draft}
            if status in INFLIGHT_STATUSES:
                return {"kind": "in_progress", "draft": draft}
            if status in contracts.TERMINAL_DRAFT_STATUSES:
                return {"kind": "terminal", "draft": draft}
            if status != "awaiting_confirmation":
                return {"kind": "invalid_state", "draft": draft}
            try:
                confirmation_expires = float(
                    draft.get("confirmation_expires_at", 0) or 0
                )
            except (TypeError, ValueError):
                confirmation_expires = 0
            if confirmation_expires and current > confirmation_expires:
                conn.execute(
                    "UPDATE drafts SET status='draft', confirmation_token='', "
                    "confirmation_expires_at=0, updated_at=? WHERE draft_id=?",
                    (current, str(draft_id)),
                )
                self._event(
                    conn,
                    draft_id=str(draft_id),
                    event="confirmation_expired",
                    now=current,
                )
                return {
                    "kind": "confirmation_expired",
                    "draft": self._get_draft_row(conn, draft_id),
                }
            if (
                int(draft.get("revision", 0) or 0) != int(expected_revision)
                or str(draft.get("payload_hash", "")) != str(expected_payload_hash)
            ):
                return {"kind": "revision_conflict", "draft": draft}
            cursor = conn.execute(
                "UPDATE drafts SET status='confirmed', updated_at=? "
                "WHERE draft_id=? AND status='awaiting_confirmation' "
                "AND revision=? AND payload_hash=? AND actor_id=? "
                "AND scope_key=? AND persona_id=?",
                (
                    current,
                    str(draft_id),
                    int(expected_revision),
                    str(expected_payload_hash),
                    str(actor_id),
                    str(scope_key),
                    str(persona_id),
                ),
            )
            if cursor.rowcount != 1:
                current_draft = self._get_draft_row(conn, draft_id)
                if current_draft and str(current_draft.get("status", "")) in INFLIGHT_STATUSES:
                    return {"kind": "in_progress", "draft": current_draft}
                return {"kind": "revision_conflict", "draft": current_draft}
            self._event(
                conn,
                draft_id=str(draft_id),
                event="confirmed",
                detail={"revision": int(expected_revision)},
                now=current,
            )
            cursor = conn.execute(
                "UPDATE drafts SET status='submitting', last_operation_id=?, "
                "updated_at=? WHERE draft_id=? AND status='confirmed'",
                (str(op_id), current, str(draft_id)),
            )
            if cursor.rowcount != 1:
                raise LedgerError("confirmed->submitting CAS failed")
            self._event(
                conn,
                draft_id=str(draft_id),
                op_id=str(op_id),
                event="submitting",
                detail={"revision": int(expected_revision)},
                now=current,
            )
            try:
                conn.execute(
                    "INSERT INTO operations("
                    "op_id, idempotency_key, draft_id, actor_id, instance_id, "
                    "platform, bot_id, scope_key, persona_id, payload_hash, "
                    "status, side_effect_state, retryable, created_at, updated_at"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'submitting', "
                    "'in_flight', 0, ?, ?)",
                    (
                        str(op_id),
                        str(idempotency_key),
                        str(draft_id),
                        str(actor_id),
                        str(draft.get("instance_id", "")),
                        str(draft.get("platform", "")),
                        str(draft.get("bot_id", "")),
                        str(scope_key),
                        str(persona_id),
                        str(payload_hash_value),
                        current,
                        current,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM operations WHERE idempotency_key=?",
                    (str(idempotency_key),),
                ).fetchone()
                op = _row_dict(existing) or {}
                if str(op.get("payload_hash", "")) == str(payload_hash_value):
                    return {"kind": "replay", "operation": op}
                return {"kind": "conflict", "operation": op}
            conn.execute(
                "INSERT INTO events(draft_id, op_id, event, detail_json, created_at) "
                "VALUES(?, ?, 'operation_created', ?, ?)",
                (
                    str(draft_id),
                    str(op_id),
                    _json({"idempotency_key": str(idempotency_key)}),
                    current,
                ),
            )
            op = conn.execute(
                "SELECT * FROM operations WHERE op_id=?", (str(op_id),)
            ).fetchone()
            return {
                "kind": "claimed",
                "operation": _row_dict(op),
                "draft": self._get_draft_row(conn, draft_id),
            }

    def finish_operation(
        self,
        op_id: str,
        *,
        status: str,
        draft_status: str,
        side_effect_state: str,
        remote_code: str = "",
        remote_data: dict[str, Any] | None = None,
        error_code: str = "",
        message: str = "",
        retryable: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE op_id=?", (str(op_id),)
            ).fetchone()
            if row is None:
                return None
            operation = _row_dict(row) or {}
            existing_status = str(operation.get("status", ""))
            if existing_status not in ("submitting", "confirmed"):
                # Terminal operations are immutable; a delayed duplicate result
                # must not overwrite a recorded outcome.
                return operation
            conn.execute(
                "UPDATE operations SET status=?, side_effect_state=?, "
                "retryable=?, remote_code=?, remote_data_json=?, error_code=?, "
                "message=?, updated_at=? WHERE op_id=?",
                (
                    str(status),
                    str(side_effect_state or "none"),
                    1 if retryable else 0,
                    str(remote_code or "")[:100],
                    _json(dict(remote_data or {})),
                    str(error_code or "")[:100],
                    str(message or "")[:300],
                    current,
                    str(op_id),
                ),
            )
            draft_id = str(operation.get("draft_id", ""))
            draft = self._get_draft_row(conn, draft_id)
            if draft is not None and str(draft.get("status", "")) in (
                "submitting",
                "confirmed",
            ):
                conn.execute(
                    "UPDATE drafts SET status=?, updated_at=? WHERE draft_id=?",
                    (str(draft_status), current, draft_id),
                )
            self._event(
                conn,
                draft_id=draft_id,
                op_id=str(op_id),
                event="operation_finished",
                detail={
                    "status": str(status),
                    "draft_status": str(draft_status),
                    "side_effect_state": str(side_effect_state),
                },
                now=current,
            )
            op = conn.execute(
                "SELECT * FROM operations WHERE op_id=?", (str(op_id),)
            ).fetchone()
            return _row_dict(op)

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE op_id=?", (str(op_id),)
            ).fetchone()
            return _row_dict(row)

    def cancel_draft(
        self,
        draft_id: str,
        *,
        actor_id: str,
        scope_key: str,
        persona_id: str,
        reason: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            draft = self._get_draft_row(conn, draft_id)
            if draft is None:
                return {"kind": "missing"}
            draft = self._expire_if_needed(conn, draft, current)
            if not self._auth_matches(
                draft,
                actor_id=actor_id,
                scope_key=scope_key,
                persona_id=persona_id,
            ):
                return {"kind": "forbidden"}
            status = str(draft.get("status", ""))
            if status in ("cancelled",):
                return {"kind": "cancelled", "draft": draft}
            if status in ("expired",):
                return {"kind": "expired", "draft": draft}
            if status in ("succeeded", "outcome_unknown", "submitting", "confirmed"):
                return {"kind": "invalid_state", "draft": draft}
            cursor = conn.execute(
                "UPDATE drafts SET status='cancelled', updated_at=?, "
                "confirmation_token='', confirmation_expires_at=0 "
                "WHERE draft_id=? AND status=?",
                (current, str(draft_id), status),
            )
            if cursor.rowcount != 1:
                return {"kind": "in_progress", "draft": self._get_draft_row(conn, draft_id)}
            self._event(
                conn,
                draft_id=str(draft_id),
                event="cancelled",
                detail={"reason": str(reason or "")[:100], "from": status},
                now=current,
            )
            return {"kind": "cancelled", "draft": self._get_draft_row(conn, draft_id)}

    # ---- scope preferences -----------------------------------------
    def set_scope_preference(
        self,
        *,
        instance_id: str,
        platform: str,
        bot_id: str,
        scope_key: str,
        location: dict[str, Any] | None = None,
        tags: list[Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = float(now if now is not None else self._now())
        with self._tx() as conn:
            prior = conn.execute(
                "SELECT * FROM scope_preferences WHERE instance_id=? "
                "AND platform=? AND bot_id=? AND scope_key=?",
                (str(instance_id), str(platform), str(bot_id), str(scope_key)),
            ).fetchone()
            if prior is None:
                location_json = _json(location or {})
                tags_json = _json(list(tags or []))
            else:
                prior_row = dict(prior)
                location_json = (
                    _json(location)
                    if location is not None
                    else str(prior_row.get("location_json", "{}"))
                )
                tags_json = (
                    _json(list(tags))
                    if tags is not None
                    else str(prior_row.get("tags_json", "[]"))
                )
            conn.execute(
                "INSERT INTO scope_preferences("
                "instance_id, platform, bot_id, scope_key, location_json, "
                "tags_json, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, platform, bot_id, scope_key) DO UPDATE SET "
                "location_json=excluded.location_json, "
                "tags_json=excluded.tags_json, updated_at=excluded.updated_at",
                (
                    str(instance_id),
                    str(platform),
                    str(bot_id),
                    str(scope_key),
                    location_json,
                    tags_json,
                    current,
                ),
            )
            return {
                "location": _loads(location_json, {}),
                "tags": _loads(tags_json, []),
                "updated_at": current,
            }

    def get_scope_preference(
        self,
        *,
        instance_id: str,
        platform: str,
        bot_id: str,
        scope_key: str,
    ) -> dict[str, Any]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM scope_preferences WHERE instance_id=? "
                "AND platform=? AND bot_id=? AND scope_key=?",
                (str(instance_id), str(platform), str(bot_id), str(scope_key)),
            ).fetchone()
        if row is None:
            return {"location": {}, "tags": []}
        raw = dict(row)
        return {
            "location": _loads(raw.get("location_json"), {}),
            "tags": _loads(raw.get("tags_json"), []),
            "updated_at": float(raw.get("updated_at", 0) or 0),
        }

    def close(self) -> None:
        self._closed = True
