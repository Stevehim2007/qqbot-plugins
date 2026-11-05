"""Opaque signed cursors and a small TTL cache (stdlib only).

Cursors are authenticated with HMAC-SHA256 and bind actor, scope and target
(the fetched URL or image query).  They are intentionally opaque to users:
base64url is only a transport encoding, not a security boundary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 8192


class CursorError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(str(code))
        self.code = str(code)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


class OpaqueCursor:
    """HMAC-signed, expiring, actor/scope/target-bound opaque cursor."""

    def __init__(
        self,
        secret: bytes | None = None,
        *,
        ttl_seconds: int = 600,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._ttl = max(1, int(ttl_seconds or 1))
        self._clock = clock or time.time

    def encode(
        self,
        payload: Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> str:
        body = dict(payload)
        body["v"] = CURSOR_VERSION
        body["exp"] = float(now if now is not None else self._clock()) + float(
            ttl_seconds if ttl_seconds is not None else self._ttl
        )
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        # Keep raw and signature in separate base64url components so an
        # arbitrary signature byte (including ``.``) cannot corrupt framing.
        return _b64encode(raw) + "." + _b64encode(signature)

    def decode(
        self,
        token: Any,
        *,
        actor_id: str | None = None,
        scope: Any = None,
        target_field: str | None = None,
        target_value: Any = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_CHARS:
            raise CursorError("invalid_cursor")
        try:
            raw_part, signature_part = token.split(".", 1)
            raw = _b64decode(raw_part)
            signature = _b64decode(signature_part)
        except Exception as exc:
            raise CursorError("invalid_cursor") from exc
        expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise CursorError("invalid_cursor")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CursorError("invalid_cursor") from exc
        if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
            raise CursorError("invalid_cursor")
        try:
            expires = float(payload.get("exp"))
        except (TypeError, ValueError) as exc:
            raise CursorError("invalid_cursor") from exc
        if expires <= float(now if now is not None else self._clock()):
            raise CursorError("cursor_expired")
        if actor_id is not None and str(payload.get("actor_id", "")) != str(actor_id):
            raise CursorError("cursor_actor_mismatch")
        if scope is not None:
            expected_scope = json.dumps(
                scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if str(payload.get("scope_key", "")) != expected_scope:
                raise CursorError("cursor_scope_mismatch")
        if target_field is not None:
            if str(payload.get(target_field, "")) != str(target_value or ""):
                raise CursorError("cursor_target_mismatch")
        return payload


@dataclass
class CacheRecord:
    value: Any
    created_at: float
    expires_at: float
    last_access: float


class TTLCache:
    """Bounded TTL cache that can report stale entries instead of hiding them."""

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_entries = max(0, int(max_entries or 0))
        self.ttl_seconds = max(0, int(ttl_seconds or 0))
        self._clock = clock or time.monotonic
        self._records: dict[str, CacheRecord] = {}
        self._lock = threading.RLock()

    def put(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> bool:
        if self.max_entries <= 0:
            return False
        now = float(self._clock())
        ttl = max(0, int(self.ttl_seconds if ttl_seconds is None else ttl_seconds))
        with self._lock:
            if key not in self._records:
                while len(self._records) >= self.max_entries:
                    oldest = min(
                        self._records,
                        key=lambda item: self._records[item].last_access,
                    )
                    self._records.pop(oldest, None)
            self._records[str(key)] = CacheRecord(
                value=value,
                created_at=now,
                expires_at=now + ttl,
                last_access=now,
            )
            return True

    def get(self, key: str) -> tuple[Any | None, bool]:
        """Return ``(value, stale)``; both are ``(None, True)`` on a miss."""

        now = float(self._clock())
        with self._lock:
            record = self._records.get(str(key))
            if record is None:
                return None, True
            record.last_access = now
            return record.value, now >= record.expires_at

    def peek_stale(self, key: str) -> bool | None:
        now = float(self._clock())
        with self._lock:
            record = self._records.get(str(key))
            if record is None:
                return None
            return now >= record.expires_at

    def pop(self, key: str) -> Any | None:
        with self._lock:
            record = self._records.pop(str(key), None)
            return None if record is None else record.value

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


def paginate_text(
    text: str,
    page_chars: int,
    *,
    offset: int = 0,
) -> tuple[str, int, bool]:
    page_chars = max(1, int(page_chars or 1))
    offset = max(0, int(offset or 0))
    value = str(text or "")
    page = value[offset : offset + page_chars]
    next_offset = offset + len(page)
    return page, next_offset, next_offset < len(value)
