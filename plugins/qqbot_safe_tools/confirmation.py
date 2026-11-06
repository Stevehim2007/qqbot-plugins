"""HMAC-bound, short-lived confirmation previews for write operations.

A confirmation is bound to the server-derived RequestContext, action, canonical
payload hash and business revision.  The token is HMAC-SHA256 authenticated and
the manager consumes it through the persistent ledger when one is supplied.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import (
    ContractError,
    RequestContext,
    canonical_json,
    payload_hash,
)

_MAX_TTL_SECONDS = 600
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SECRET_KEYS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "private_key",
    }
)


class ConfirmationError(ValueError):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400) -> None:
        self.code = str(code)
        self.message = str(message or code)
        self.http_status = int(http_status)
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ConfirmationPreview:
    confirmation_id: str
    token: str
    action: str
    scope: str
    payload_hash: str
    revision: int
    expires_at: int
    preview_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "token": self.token,
            "action": self.action,
            "scope": self.scope,
            "payload_hash": self.payload_hash,
            "revision": self.revision,
            "expires_at": self.expires_at,
            "preview_text": self.preview_text,
        }


@dataclass(frozen=True, slots=True)
class ConfirmationDecision:
    confirmation_id: str
    status: str
    action: str
    scope: str
    payload: Any
    payload_hash: str
    revision: int
    canonical_args: dict[str, Any]
    confirmation_ref: str
    decided_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "status": self.status,
            "action": self.action,
            "scope": self.scope,
            "payload": _redact_value(self.payload),
            "payload_hash": self.payload_hash,
            "revision": self.revision,
            "canonical_args": _redact_value(self.canonical_args),
            "confirmation_ref": self.confirmation_ref,
            "decided_at": self.decided_at,
        }


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)) or not bytes(secret):
        raise ConfirmationError("invalid_args", "confirmation secret is required")
    return bytes(secret)


def _claims_bytes(claims: Mapping[str, Any]) -> bytes:
    return canonical_json(dict(claims)).encode("utf-8")


def sign_claims(secret: str | bytes, claims: Mapping[str, Any]) -> str:
    key = _secret_bytes(secret)
    body = _claims_bytes(claims)
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return _b64encode(body) + "." + _b64encode(signature)


def decode_claims(token: str, secret: str | bytes) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 16_384:
        raise ConfirmationError("confirmation_invalid", "token format is invalid")
    body_part, signature_part = token.split(".", 1)
    try:
        body = _b64decode(body_part)
        supplied_signature = _b64decode(signature_part)
    except Exception as exc:
        raise ConfirmationError("confirmation_invalid", "token encoding is invalid") from exc
    expected = hmac.new(_secret_bytes(secret), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied_signature):
        raise ConfirmationError("confirmation_invalid", "token signature is invalid")
    try:
        claims = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ConfirmationError("confirmation_invalid", "token payload is invalid") from exc
    if not isinstance(claims, dict) or claims.get("v") != 1:
        raise ConfirmationError("confirmation_invalid", "token version is invalid")
    return claims


def _context_matches(claims: Mapping[str, Any], context: RequestContext) -> bool:
    return all(
        str(claims.get(key, "")) == str(getattr(context, key))
        for key in (
            "instance_id",
            "actor_id",
            "bot_id",
            "conversation_id",
            "persona_id",
            "scope",
        )
    )


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 24:
                result["..."] = "[truncated]"
                break
            key_text = str(key)[:64]
            if key_text.casefold() in _SECRET_KEYS or any(
                marker in key_text.casefold()
                for marker in ("secret", "password", "token", "api_key", "authorization")
            ):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = _redact_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, depth=depth + 1) for item in list(value)[:24]]
    if isinstance(value, str):
        return value[:160]
    return value


def _preview_text(
    action: str, confirmation_id: str, digest: str, revision: int, payload: Any
) -> str:
    rendered = canonical_json(_redact_value(payload))
    if len(rendered) > 600:
        rendered = rendered[:600] + "…"
    return (
        f"待确认操作：{action}\n"
        f"确认ID：{confirmation_id}\n"
        f"revision：{int(revision)}；内容摘要：{digest[:16]}…\n"
        f"内容：{rendered}"
    )


class ConfirmationManager:
    """Issues and atomically consumes signed confirmation references."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        ttl_seconds: int = 120,
        ledger: Any = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.secret = _secret_bytes(secret)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ConfirmationError("invalid_args", "ttl_seconds must be an integer")
        if ttl_seconds < 1 or ttl_seconds > _MAX_TTL_SECONDS:
            raise ConfirmationError("invalid_args", "ttl_seconds is out of range")
        self.ttl_seconds = ttl_seconds
        self.ledger = ledger
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(18))
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def preview(
        self,
        context: RequestContext,
        action: str,
        payload: Mapping[str, Any] | None,
        revision: int,
        *,
        provider_operation_id: str = "",
    ) -> ConfirmationPreview:
        context.validate()
        action_text = str(action or "").strip()
        if not _ACTION_RE.fullmatch(action_text):
            raise ConfirmationError("invalid_args", "action is invalid")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ConfirmationError("invalid_args", "revision is invalid")
        if payload is not None and not isinstance(payload, Mapping):
            raise ConfirmationError("invalid_args", "payload must be an object")
        payload_obj = dict(payload or {})
        try:
            payload_text = canonical_json(payload_obj)
        except ContractError as exc:
            raise ConfirmationError("invalid_args", exc.message) from exc
        if len(payload_text.encode("utf-8")) > 16_384:
            raise ConfirmationError("invalid_args", "payload is too large")
        digest = payload_hash(payload_obj)
        now = int(self.clock())
        expires_at = now + self.ttl_seconds
        confirmation_id = "confirm_" + secrets.token_urlsafe(18)
        nonce = str(self.nonce_factory())[:128]
        claims = {
            "v": 1,
            "confirmation_id": confirmation_id,
            "instance_id": context.instance_id,
            "actor_id": context.actor_id,
            "bot_id": context.bot_id,
            "conversation_id": context.conversation_id,
            "persona_id": context.persona_id,
            "scope": context.requested_scope,
            "action": action_text,
            "payload_hash": digest,
            "revision": int(revision),
            "iat": now,
            "exp": expires_at,
            "nonce": nonce,
        }
        token = sign_claims(self.secret, claims)
        record = {
            "confirm_id": confirmation_id,
            "token": token,
            "actor_id": context.actor_id,
            "instance_id": context.instance_id,
            "conversation_id": context.conversation_id,
            "persona_id": context.persona_id,
            "scope": context.requested_scope,
            "action": action_text,
            "payload_hash": digest,
            "payload_json": payload_text,
            "payload": payload_obj,
            "revision": int(revision),
            "status": "previewed",
            "created_at": float(now),
            "expires_at": float(expires_at),
            "consumed_at": None,
            "provider_operation_id": str(provider_operation_id or "")[:256],
        }
        if self.ledger is not None:
            try:
                stored = self.ledger.create_confirmation(record)
                record["payload"] = stored.get("payload", payload_obj)
            except Exception as exc:
                code = str(getattr(exc, "code", "confirmation_invalid"))
                raise ConfirmationError(code) from exc
        else:
            with self._lock:
                if confirmation_id in self._records:
                    raise ConfirmationError("confirmation_invalid", "duplicate confirmation")
                self._records[confirmation_id] = record
        return ConfirmationPreview(
            confirmation_id=confirmation_id,
            token=token,
            action=action_text,
            scope=context.requested_scope,
            payload_hash=digest,
            revision=int(revision),
            expires_at=expires_at,
            preview_text=_preview_text(
                action_text, confirmation_id, digest, revision, payload_obj
            ),
        )

    # ------------------------------------------------------------------
    # Reference verification
    # ------------------------------------------------------------------
    def verify_reference(
        self,
        token: str,
        context: RequestContext,
        *,
        action: str | None = None,
        expected_payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        require_consumed: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        context.validate()
        claims = decode_claims(token, self.secret)
        if not _context_matches(claims, context):
            raise ConfirmationError("permission_denied", http_status=403)
        if action is not None and str(claims.get("action")) != str(action):
            raise ConfirmationError("confirmation_invalid")
        if expected_revision is not None and int(claims.get("revision", -1)) != int(
            expected_revision
        ):
            raise ConfirmationError("revision_conflict", http_status=409)
        if expected_payload is not None:
            if str(claims.get("payload_hash")) != payload_hash(dict(expected_payload)):
                raise ConfirmationError("revision_conflict", http_status=409)
        now_value = int(now if now is not None else self.clock())
        if now_value >= int(claims.get("exp", 0)):
            raise ConfirmationError("confirmation_expired", http_status=410)
        if require_consumed:
            record = self._load_record(str(claims.get("confirmation_id", "")))
            if record is None:
                raise ConfirmationError("confirmation_invalid")
            if record.get("status") != "consumed":
                raise ConfirmationError("confirmation_invalid")
        return claims

    def _load_record(self, confirmation_id: str) -> dict[str, Any] | None:
        if self.ledger is not None:
            try:
                return self.ledger.get_confirmation(confirmation_id)
            except Exception:
                return None
        with self._lock:
            return self._records.get(confirmation_id)

    def _claim_from_ledger(
        self, record: dict[str, Any], context: RequestContext, now: float
    ) -> dict[str, Any]:
        try:
            return self.ledger.claim_confirmation(
                str(record["confirm_id"]),
                actor_id=context.actor_id,
                instance_id=context.instance_id,
                conversation_id=context.conversation_id,
                persona_id=context.persona_id,
                scope=context.requested_scope,
                payload_hash=str(record["payload_hash"]),
                revision=int(record["revision"]),
                now=now,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "confirmation_invalid"))
            status = int(getattr(exc, "http_status", 400))
            raise ConfirmationError(code, http_status=status) from exc

    def _cancel_from_ledger(
        self, record: dict[str, Any], context: RequestContext, now: float
    ) -> dict[str, Any]:
        try:
            return self.ledger.cancel_confirmation(
                str(record["confirm_id"]),
                actor_id=context.actor_id,
                instance_id=context.instance_id,
                conversation_id=context.conversation_id,
                persona_id=context.persona_id,
                scope=context.requested_scope,
                now=now,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "confirmation_invalid"))
            status = int(getattr(exc, "http_status", 400))
            raise ConfirmationError(code, http_status=status) from exc

    # ------------------------------------------------------------------
    # Confirm / cancel
    # ------------------------------------------------------------------
    def confirm(
        self,
        confirmation_id: str,
        context: RequestContext,
        *,
        expected_payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
        now: float | None = None,
    ) -> ConfirmationDecision:
        context.validate()
        record = self._load_record(str(confirmation_id))
        if record is None:
            raise ConfirmationError("confirmation_invalid", http_status=404)
        claims = self.verify_reference(
            str(record["token"]),
            context,
            action=str(record["action"]),
            expected_payload=expected_payload,
            expected_revision=expected_revision,
            now=now,
        )
        if str(claims.get("confirmation_id")) != str(confirmation_id):
            raise ConfirmationError("confirmation_invalid")
        now_value = float(now if now is not None else self.clock())
        if self.ledger is not None:
            consumed = self._claim_from_ledger(record, context, now_value)
        else:
            with self._lock:
                current = self._records.get(str(confirmation_id))
                if current is None:
                    raise ConfirmationError("confirmation_invalid", http_status=404)
                if current.get("status") in {"consumed", "cancelled"}:
                    raise ConfirmationError("confirmation_replayed", http_status=409)
                if now_value >= float(current.get("expires_at", 0)):
                    current["status"] = "expired"
                    raise ConfirmationError("confirmation_expired", http_status=410)
                if current.get("status") != "previewed":
                    raise ConfirmationError("confirmation_invalid")
                if expected_payload is not None and current["payload_hash"] != payload_hash(
                    dict(expected_payload)
                ):
                    raise ConfirmationError("revision_conflict", http_status=409)
                if (
                    expected_revision is not None
                    and int(current["revision"]) != int(expected_revision)
                ):
                    raise ConfirmationError("revision_conflict", http_status=409)
                current["status"] = "consumed"
                current["consumed_at"] = now_value
                consumed = current
        payload = consumed.get("payload")
        if payload is None:
            payload = record.get("payload")
        return ConfirmationDecision(
            confirmation_id=str(confirmation_id),
            status="consumed",
            action=str(record["action"]),
            scope=context.requested_scope,
            payload=payload,
            payload_hash=str(record["payload_hash"]),
            revision=int(record["revision"]),
            canonical_args=dict(payload or {}),
            confirmation_ref=str(record["token"]),
            decided_at=int(now_value),
        )

    def cancel(
        self,
        confirmation_id: str,
        context: RequestContext,
        *,
        now: float | None = None,
    ) -> ConfirmationDecision:
        context.validate()
        record = self._load_record(str(confirmation_id))
        if record is None:
            raise ConfirmationError("confirmation_invalid", http_status=404)
        self.verify_reference(
            str(record["token"]),
            context,
            action=str(record["action"]),
            now=now,
        )
        now_value = float(now if now is not None else self.clock())
        if self.ledger is not None:
            cancelled = self._cancel_from_ledger(record, context, now_value)
        else:
            with self._lock:
                current = self._records.get(str(confirmation_id))
                if current is None:
                    raise ConfirmationError("confirmation_invalid", http_status=404)
                if current.get("status") in {"consumed", "cancelled"}:
                    raise ConfirmationError("confirmation_replayed", http_status=409)
                if now_value >= float(current.get("expires_at", 0)):
                    current["status"] = "expired"
                    raise ConfirmationError("confirmation_expired", http_status=410)
                if current.get("status") != "previewed":
                    raise ConfirmationError("confirmation_invalid")
                current["status"] = "cancelled"
                current["consumed_at"] = now_value
                cancelled = current
        payload = cancelled.get("payload")
        if payload is None:
            payload = record.get("payload")
        return ConfirmationDecision(
            confirmation_id=str(confirmation_id),
            status="cancelled",
            action=str(record["action"]),
            scope=context.requested_scope,
            payload=payload,
            payload_hash=str(record["payload_hash"]),
            revision=int(record["revision"]),
            canonical_args=dict(payload or {}),
            confirmation_ref="",
            decided_at=int(now_value),
        )
