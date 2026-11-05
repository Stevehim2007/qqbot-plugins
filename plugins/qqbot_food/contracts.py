"""QQ Bot provider ABI v1 for qqbot_food.

Pure standard-library DTOs and a small in-process provider registry.  This
module never performs HTTP, SQL, model calls, shell operations or dynamic
function dispatch.  Business methods receive a RequestContext plus a plain
arguments mapping and return a ProviderResult-shaped dict.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

ABI_VERSION = 1
PROTOCOL_VERSION = 1
PROVIDER_CAPABILITY = "food"
PROVIDER_OWNER = "qqbot_food"
REGISTRY_ATTRS = (
    "_qqbot_provider_registry_v1",
    "qqbot_provider_registry",
)

# Canonical error codes used by provider results.
ERR_INVALID_ARGS = "invalid_args"
ERR_PERMISSION_DENIED = "permission_denied"
ERR_NOT_READY = "not_ready"
ERR_DEPENDENCY_MISSING = "dependency_missing"
ERR_REVISION_CONFLICT = "revision_conflict"
ERR_CONFIRMATION_REQUIRED = "confirmation_required"
ERR_CONFIRMATION_EXPIRED = "confirmation_expired"
ERR_CONFIRMATION_INVALID = "confirmation_invalid"
ERR_DRAFT_NOT_FOUND = "draft_not_found"
ERR_DRAFT_EXPIRED = "draft_expired"
ERR_DRAFT_INCOMPLETE = "draft_incomplete"
ERR_INVALID_STATE = "invalid_state"
ERR_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
ERR_OUTCOME_UNKNOWN = "outcome_unknown"
ERR_RETRYABLE = "retryable_error"
ERR_BACKEND_TIMEOUT = "backend_timeout"
ERR_BACKEND_ERROR = "backend_error"
ERR_AMBIGUOUS_LOCATION = "ambiguous_location"
ERR_AMBIGUOUS_TAG = "ambiguous_tag"
ERR_NOT_FOUND = "not_found"
ERR_INTERNAL = "internal_error"

DRAFT_STATUSES = (
    "collecting",
    "draft",
    "awaiting_confirmation",
    "confirmed",
    "submitting",
    "succeeded",
    "rejected",
    "outcome_unknown",
    "cancelled",
    "expired",
)
PRE_SUBMIT_STATUSES = {
    "collecting",
    "draft",
    "awaiting_confirmation",
    "confirmed",
}
TERMINAL_DRAFT_STATUSES = {
    "succeeded",
    "rejected",
    "outcome_unknown",
    "cancelled",
    "expired",
}
ALLOWED_DRAFT_TRANSITIONS = {
    "collecting": {"draft", "cancelled", "expired"},
    "draft": {
        "awaiting_confirmation",
        "cancelled",
        "expired",
        "collecting",
    },
    "awaiting_confirmation": {
        "confirmed",
        "draft",
        "cancelled",
        "expired",
    },
    "confirmed": {"submitting", "cancelled", "expired"},
    "submitting": {"succeeded", "rejected", "outcome_unknown"},
    "succeeded": set(),
    "rejected": {"draft", "cancelled"},
    "outcome_unknown": set(),
    "cancelled": set(),
    "expired": set(),
}


def can_transition(old: str, new: str) -> bool:
    return new in ALLOWED_DRAFT_TRANSITIONS.get(str(old or ""), set())


def _text(value: Any, limit: int = 300) -> str:
    try:
        value = str(value or "")
    except Exception:
        return ""
    value = "".join(
        char for char in value if ord(char) >= 32 and ord(char) != 127
    ).strip()
    return value[: max(1, int(limit))]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def normalize_scope(raw: Any, actor_id: str = "") -> dict[str, str]:
    """Normalize a requested scope without trusting untrusted marker fields."""

    if isinstance(raw, Mapping):
        kind = _text(raw.get("kind") or raw.get("type"), 32).casefold()
        ident = _text(
            raw.get("id")
            or raw.get("scope_id")
            or raw.get("target_id"),
            200,
        )
        if not kind:
            kind = "group" if ident else "user"
        return {"kind": kind or "user", "id": ident or _text(actor_id, 200)}
    if isinstance(raw, str) and raw.strip():
        return {"kind": "user", "id": _text(raw, 200)}
    return {"kind": "user", "id": _text(actor_id, 200)}


@dataclass(frozen=True)
class RequestContext:
    protocol_version: int = PROTOCOL_VERSION
    instance_id: str = ""
    platform: str = ""
    bot_id: str = ""
    actor_id: str = ""
    conversation_id: str = ""
    persona_id: str = ""
    source_message_id: str = ""
    turn_id: str = ""
    capability: str = PROVIDER_CAPABILITY
    requested_scope: dict[str, str] = field(default_factory=dict)
    owner_epoch: int = 0
    deadline: float = 0.0
    group_id: str = ""
    group_name: str = ""
    actor_name: str = ""

    def __post_init__(self):
        object.__setattr__(self, "protocol_version", int(self.protocol_version or 0))
        for name in (
            "instance_id",
            "platform",
            "bot_id",
            "actor_id",
            "conversation_id",
            "persona_id",
            "source_message_id",
            "turn_id",
            "capability",
            "group_id",
            "group_name",
            "actor_name",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), 300))
        object.__setattr__(
            self,
            "requested_scope",
            normalize_scope(self.requested_scope, self.actor_id),
        )
        try:
            object.__setattr__(self, "owner_epoch", int(self.owner_epoch or 0))
        except (TypeError, ValueError):
            object.__setattr__(self, "owner_epoch", 0)
        try:
            object.__setattr__(self, "deadline", float(self.deadline or 0.0))
        except (TypeError, ValueError):
            object.__setattr__(self, "deadline", 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "instance_id": self.instance_id,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "actor_id": self.actor_id,
            "conversation_id": self.conversation_id,
            "persona_id": self.persona_id,
            "source_message_id": self.source_message_id,
            "turn_id": self.turn_id,
            "capability": self.capability,
            "requested_scope": dict(self.requested_scope),
            "owner_epoch": self.owner_epoch,
            "deadline": self.deadline,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "actor_name": self.actor_name,
        }


def coerce_request_context(value: Any) -> RequestContext:
    if isinstance(value, RequestContext):
        return value
    if not isinstance(value, Mapping):
        raise ContractError(ERR_INVALID_ARGS, "request_context 必须为对象")
    protocol = value.get("protocol_version", PROTOCOL_VERSION)
    try:
        protocol_version = int(protocol)
    except (TypeError, ValueError):
        raise ContractError(ERR_INVALID_ARGS, "protocol_version 非法")
    if protocol_version != PROTOCOL_VERSION:
        raise ContractError(ERR_INVALID_ARGS, "不支持的协议版本")
    actor = _text(value.get("actor_id"), 200)
    instance = _text(value.get("instance_id"), 200)
    if not actor:
        raise ContractError(ERR_PERMISSION_DENIED, "缺少已认证的发送者身份")
    if not instance:
        raise ContractError(ERR_INVALID_ARGS, "缺少实例ID")
    scope = normalize_scope(value.get("requested_scope"), actor)
    return RequestContext(
        protocol_version=protocol_version,
        instance_id=instance,
        platform=_text(value.get("platform"), 100),
        bot_id=_text(value.get("bot_id"), 200),
        actor_id=actor,
        conversation_id=_text(value.get("conversation_id"), 300),
        persona_id=_text(value.get("persona_id"), 200),
        source_message_id=_text(value.get("source_message_id"), 200),
        turn_id=_text(value.get("turn_id"), 200),
        capability=_text(value.get("capability") or PROVIDER_CAPABILITY, 64),
        requested_scope=scope,
        owner_epoch=value.get("owner_epoch", 0),
        deadline=value.get("deadline", 0),
        group_id=_text(value.get("group_id"), 64),
        group_name=_text(value.get("group_name"), 100),
        actor_name=_text(value.get("actor_name"), 100),
    )


def scope_key(ctx: RequestContext) -> str:
    raw = {
        "instance_id": ctx.instance_id,
        "platform": ctx.platform,
        "bot_id": ctx.bot_id,
        "requested_scope": ctx.requested_scope,
    }
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


def actor_scope_key(ctx: RequestContext) -> str:
    raw = {
        "instance_id": ctx.instance_id,
        "platform": ctx.platform,
        "bot_id": ctx.bot_id,
        "actor_id": ctx.actor_id,
        "requested_scope": ctx.requested_scope,
    }
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


class ContractError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        side_effect_state: str = "none",
        data: dict[str, Any] | None = None,
    ):
        super().__init__(message or code)
        self.code = str(code or ERR_INTERNAL)
        self.message = str(message or code)
        self.retryable = bool(retryable)
        self.side_effect_state = str(side_effect_state or "none")
        self.data = dict(data or {})


@dataclass(frozen=True)
class ProviderResult:
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""
    retryable: bool = False
    side_effect_state: str = "none"
    provider_operation_id: str = ""
    abi_version: int = ABI_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": dict(self.data),
            "error_code": self.error_code,
            "message": self.message,
            "retryable": bool(self.retryable),
            "side_effect_state": self.side_effect_state,
            "provider_operation_id": self.provider_operation_id,
            "abi_version": self.abi_version,
        }

    @classmethod
    def ok(
        cls,
        data: dict[str, Any] | None = None,
        *,
        provider_operation_id: str = "",
        side_effect_state: str = "none",
        retryable: bool = False,
    ) -> "ProviderResult":
        return cls(
            status="ok",
            data=dict(data or {}),
            retryable=bool(retryable),
            side_effect_state=str(side_effect_state or "none"),
            provider_operation_id=str(provider_operation_id or ""),
        )

    @classmethod
    def error(
        cls,
        error_code: str,
        message: str = "",
        *,
        retryable: bool = False,
        side_effect_state: str = "none",
        data: dict[str, Any] | None = None,
        provider_operation_id: str = "",
    ) -> "ProviderResult":
        return cls(
            status="error",
            data=dict(data or {}),
            error_code=str(error_code or ERR_INTERNAL),
            message=str(message or error_code or ERR_INTERNAL)[:300],
            retryable=bool(retryable),
            side_effect_state=str(side_effect_state or "none"),
            provider_operation_id=str(provider_operation_id or ""),
        )

    @classmethod
    def not_ready(cls, message: str = "Food provider 尚未就绪") -> "ProviderResult":
        return cls.error(ERR_NOT_READY, message)


@dataclass(frozen=True)
class ProviderRecord:
    owner: str
    capability: str
    provider: Any
    abi_version: int = ABI_VERSION
    ready: bool = False
    registered_at: float = 0.0


class ProviderRegistrationError(RuntimeError):
    pass


class ProviderRegistry:
    """In-process ABI v1 capability registry with duplicate-owner refusal."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, ProviderRecord] = {}

    def register(
        self,
        *,
        owner: str,
        capability: str,
        provider: Any,
        abi_version: int = ABI_VERSION,
    ) -> ProviderRecord:
        owner = _text(owner, 100)
        capability = _text(capability, 100)
        try:
            abi = int(abi_version)
        except (TypeError, ValueError):
            raise ProviderRegistrationError("ABI 版本非法")
        if abi != ABI_VERSION:
            raise ProviderRegistrationError(
                f"ABI 版本不兼容：{abi} != {ABI_VERSION}"
            )
        if not owner or not capability or provider is None:
            raise ProviderRegistrationError("provider 注册参数不完整")
        with self._lock:
            if capability in self._records:
                existing = self._records[capability]
                raise ProviderRegistrationError(
                    f"capability {capability} 已有 owner {existing.owner}"
                )
            if any(record.owner == owner for record in self._records.values()):
                raise ProviderRegistrationError(f"owner {owner} 重复注册")
            record = ProviderRecord(
                owner=owner,
                capability=capability,
                provider=provider,
                abi_version=abi,
                ready=False,
                registered_at=time.time(),
            )
            self._records[capability] = record
            return record

    def set_ready(self, owner: str, ready: bool = True) -> bool:
        owner = _text(owner, 100)
        with self._lock:
            for capability, record in list(self._records.items()):
                if record.owner == owner:
                    self._records[capability] = ProviderRecord(
                        owner=record.owner,
                        capability=record.capability,
                        provider=record.provider,
                        abi_version=record.abi_version,
                        ready=bool(ready),
                        registered_at=record.registered_at,
                    )
                    return True
        return False

    def unregister(self, owner: str) -> list[str]:
        owner = _text(owner, 100)
        removed: list[str] = []
        with self._lock:
            for capability, record in list(self._records.items()):
                if record.owner == owner:
                    self._records.pop(capability, None)
                    removed.append(capability)
        return removed

    def get(self, capability: str) -> ProviderRecord | None:
        with self._lock:
            return self._records.get(_text(capability, 100))

    def is_ready(self, capability: str) -> bool:
        record = self.get(capability)
        return bool(record and record.ready)

    def status(self) -> dict[str, Any]:
        with self._lock:
            providers = [
                {
                    "owner": record.owner,
                    "capability": record.capability,
                    "abi_version": record.abi_version,
                    "ready": record.ready,
                }
                for record in self._records.values()
            ]
        return {"abi_version": ABI_VERSION, "providers": providers}


def get_registry(context: Any) -> ProviderRegistry | None:
    for attr in REGISTRY_ATTRS:
        candidate = getattr(context, attr, None)
        if isinstance(candidate, ProviderRegistry):
            return candidate
    return None


def get_or_create_registry(context: Any) -> ProviderRegistry:
    existing = get_registry(context)
    if existing is not None:
        return existing
    registry = ProviderRegistry()
    if context is not None:
        for attr in REGISTRY_ATTRS:
            try:
                setattr(context, attr, registry)
                break
            except Exception:
                continue
    return registry


@dataclass(frozen=True)
class TokenValidation:
    valid: bool
    code: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class ConfirmationTokenIssuer:
    """HMAC tokens binding actor/scope/persona/revision/payload/draft."""

    def __init__(self, secret: str | bytes, *, ttl_seconds: int = 300):
        raw = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        self._key = hashlib.sha256(raw).digest()
        self.ttl_seconds = max(1, int(ttl_seconds))

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    @staticmethod
    def _unb64(text: str) -> bytes:
        padding = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode((text + padding).encode("ascii"))

    def issued_at(self, now: float | None = None) -> float:
        return float(now if now is not None else time.time())

    def issue(
        self,
        ctx: RequestContext,
        *,
        kind: str,
        draft_id: str = "",
        revision: int = 0,
        payload_hash_value: str = "",
        expires_at: float | None = None,
        extra: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> tuple[str, float]:
        issued = self.issued_at(now)
        expiry = float(
            expires_at if expires_at is not None else issued + self.ttl_seconds
        )
        body = {
            "v": ABI_VERSION,
            "kind": _text(kind, 64),
            "draft_id": _text(draft_id, 100),
            "revision": int(revision or 0),
            "payload_hash": _text(payload_hash_value, 100),
            "actor_id": ctx.actor_id,
            "scope_key": scope_key(ctx),
            "persona_id": ctx.persona_id,
            "iat": int(issued),
            "exp": int(expiry),
            "nonce": secrets.token_hex(8),
            "extra": dict(extra or {}),
        }
        encoded = self._b64(canonical_json(body).encode("utf-8"))
        signature = hmac.new(
            self._key, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()[:40]
        return f"{encoded}.{signature}", expiry

    def verify(
        self,
        token: str,
        ctx: RequestContext,
        *,
        kind: str,
        draft_id: str = "",
        revision: int = 0,
        payload_hash_value: str = "",
        now: float | None = None,
    ) -> TokenValidation:
        try:
            encoded, signature = str(token or "").split(".", 1)
            expected = hmac.new(
                self._key, encoded.encode("ascii"), hashlib.sha256
            ).hexdigest()[:40]
            if not hmac.compare_digest(signature, expected):
                return TokenValidation(False, ERR_CONFIRMATION_INVALID)
            body = json.loads(self._unb64(encoded).decode("utf-8"))
            if not isinstance(body, dict):
                return TokenValidation(False, ERR_CONFIRMATION_INVALID)
        except Exception:
            return TokenValidation(False, ERR_CONFIRMATION_INVALID)
        if int(body.get("v", 0) or 0) != ABI_VERSION:
            return TokenValidation(False, ERR_CONFIRMATION_INVALID)
        if _text(body.get("kind"), 64) != _text(kind, 64):
            return TokenValidation(False, ERR_CONFIRMATION_INVALID)
        if _text(body.get("draft_id"), 100) != _text(draft_id, 100):
            return TokenValidation(False, ERR_CONFIRMATION_INVALID)
        if _text(body.get("actor_id"), 200) != ctx.actor_id:
            return TokenValidation(False, ERR_PERMISSION_DENIED)
        if _text(body.get("scope_key"), 100) != scope_key(ctx):
            return TokenValidation(False, ERR_PERMISSION_DENIED)
        if _text(body.get("persona_id"), 200) != ctx.persona_id:
            return TokenValidation(False, ERR_PERMISSION_DENIED)
        try:
            expiry = float(body.get("exp", 0) or 0)
            current = float(now if now is not None else time.time())
        except (TypeError, ValueError):
            return TokenValidation(False, ERR_CONFIRMATION_INVALID)
        if expiry <= current:
            return TokenValidation(False, ERR_CONFIRMATION_EXPIRED)
        if int(body.get("revision", 0) or 0) != int(revision or 0):
            return TokenValidation(False, ERR_REVISION_CONFLICT)
        if _text(body.get("payload_hash"), 100) != _text(
            payload_hash_value, 100
        ):
            return TokenValidation(False, ERR_REVISION_CONFLICT)
        return TokenValidation(True, payload=body)
