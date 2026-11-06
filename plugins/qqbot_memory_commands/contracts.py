"""Pure ABI-v1 DTOs and provider registry for memory/session/persona.

Only fixed provider operations may cross this boundary.  This module performs
validation and lifecycle bookkeeping only: it has no AstrBot imports, no
business storage and no model calls.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

ABI_VERSION = 1
PROTOCOL_VERSION = 1
SCOPES = ("global", "persona", "conversation", "group", "user")
ROLES = ("system", "user", "assistant", "tool")

FIXED_METHODS = (
    "memory_read",
    "memory_search",
    "memory_add",
    "memory_preview_clear",
    "memory_confirm_clear",
    "session_open",
    "session_read",
    "session_append",
    "session_reset",
    "session_switch",
    "persona_list",
    "persona_bind",
    "persona_switch",
)

METHOD_CAPABILITIES = {
    "memory_read": "memory.read",
    "memory_search": "memory.search",
    "memory_add": "memory.add",
    "memory_preview_clear": "memory.preview_clear",
    "memory_confirm_clear": "memory.confirm_clear",
    "session_open": "session.open",
    "session_read": "session.read",
    "session_append": "session.append",
    "session_reset": "session.reset",
    "session_switch": "session.switch",
    "persona_list": "persona.list",
    "persona_bind": "persona.bind",
    "persona_switch": "persona.switch",
}
CAPABILITY_TO_METHOD = {value: key for key, value in METHOD_CAPABILITIES.items()}

STATUS_OK = "ok"
STATUS_CONFIRMATION_REQUIRED = "confirmation_required"
STATUS_PERMISSION_DENIED = "permission_denied"
STATUS_INVALID_ARGS = "invalid_args"
STATUS_NOT_READY = "not_ready"
STATUS_REVISION_CONFLICT = "revision_conflict"
STATUS_CONFIRMATION_EXPIRED = "confirmation_expired"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"
STATUS_DEPENDENCY_MISSING = "dependency_missing"
STATUS_NOT_FOUND = "not_found"
STATUS_INTERNAL_ERROR = "internal_error"

STATUSES = frozenset(
    {
        STATUS_OK,
        STATUS_CONFIRMATION_REQUIRED,
        STATUS_PERMISSION_DENIED,
        STATUS_INVALID_ARGS,
        STATUS_NOT_READY,
        STATUS_REVISION_CONFLICT,
        STATUS_CONFIRMATION_EXPIRED,
        STATUS_OUTCOME_UNKNOWN,
        STATUS_DEPENDENCY_MISSING,
        STATUS_NOT_FOUND,
        STATUS_INTERNAL_ERROR,
    }
)

ERROR_CODES = frozenset(
    {
        "invalid_protocol",
        "invalid_request_context",
        "invalid_scope",
        "invalid_args",
        "deadline_exceeded",
        "owner_epoch_conflict",
        "permission_denied",
        "cross_scope_denied",
        "private_group_forgery",
        "provider_not_ready",
        "provider_missing",
        "duplicate_provider",
        "abi_mismatch",
        "revision_conflict",
        "stale_revision",
        "idempotency_payload_mismatch",
        "idempotency_in_progress",
        "confirmation_not_found",
        "confirmation_actor_mismatch",
        "confirmation_scope_mismatch",
        "confirmation_payload_mismatch",
        "confirmation_expired",
        "confirmation_replayed",
        "outcome_unknown",
        "dependency_missing",
        "backend_error",
        "invalid_backend_result",
        "content_too_long",
        "turn_key_conflict",
        "not_found",
    }
)


class ContractError(Exception):
    """Base class for ABI validation/lifecycle errors."""

    code = "abi_mismatch"


class ProviderABIError(ContractError):
    """The provider does not implement the fixed ABI-v1 method set."""


class DuplicateProviderError(ContractError):
    """A provider id or fixed method is already owned."""

    code = "duplicate_provider"


class ProviderNotFoundError(ContractError):
    code = "provider_missing"


def canonical_json(value: Any) -> str:
    """Stable JSON used only for hashes; never log the returned payload."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_scope_id(
    *,
    kind: str,
    target_id: str,
    instance_id: str,
    platform: str,
    bot_id: str,
) -> str:
    return ":".join(
        (
            str(instance_id).strip(),
            str(platform).strip(),
            str(bot_id).strip(),
            str(kind).strip(),
            str(target_id).strip(),
        )
    )


def make_result(
    status: str,
    *,
    data: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    retryable: bool = False,
    side_effect_state: str = "none",
    provider_operation_id: str | None = None,
) -> dict[str, Any]:
    """Build the one result shape allowed across the provider ABI."""

    if status not in STATUSES:
        status = STATUS_INTERNAL_ERROR
        error_code = error_code or "invalid_backend_result"
    return {
        "status": status,
        "data": dict(data or {}),
        "error_code": error_code,
        "retryable": bool(retryable),
        "side_effect_state": str(side_effect_state or "none"),
        "provider_operation_id": provider_operation_id,
    }


def error_result(
    status: str,
    error_code: str,
    *,
    retryable: bool = False,
    side_effect_state: str = "none",
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return make_result(
        status,
        data=data,
        error_code=error_code,
        retryable=retryable,
        side_effect_state=side_effect_state,
    )


def normalize_result(raw: Any) -> dict[str, Any]:
    """Validate/coerce one backend/provider result without leaking exceptions."""

    if not isinstance(raw, Mapping):
        return error_result(
            STATUS_INTERNAL_ERROR,
            "invalid_backend_result",
            side_effect_state="unknown",
        )
    status = str(raw.get("status") or "").strip()
    if status not in STATUSES:
        return error_result(
            STATUS_INTERNAL_ERROR,
            "invalid_backend_result",
            side_effect_state="unknown",
        )
    data = raw.get("data")
    if not isinstance(data, Mapping):
        data = {}
    error_code = raw.get("error_code")
    if error_code is not None:
        error_code = str(error_code).strip()[:96] or None
    side_effect = str(raw.get("side_effect_state") or "none").strip()[:32]
    normalized = make_result(
        status,
        data=data,
        error_code=error_code,
        retryable=bool(raw.get("retryable", False)),
        side_effect_state=side_effect,
        provider_operation_id=(
            str(raw.get("provider_operation_id"))[:128]
            if raw.get("provider_operation_id")
            else None
        ),
    )
    if raw.get("idempotent_replay"):
        normalized["idempotent_replay"] = True
    return normalized


def is_ok(result: Mapping[str, Any]) -> bool:
    return str(result.get("status") or "") == STATUS_OK


@dataclass(frozen=True)
class ScopeRef:
    """One canonical scope, namespaced by instance/platform/bot."""

    kind: str
    target_id: str
    instance_id: str
    platform: str
    bot_id: str

    @property
    def canonical_id(self) -> str:
        return canonical_scope_id(
            kind=self.kind,
            target_id=self.target_id,
            instance_id=self.instance_id,
            platform=self.platform,
            bot_id=self.bot_id,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "target_id": self.target_id,
            "instance_id": self.instance_id,
            "platform": self.platform,
            "bot_id": self.bot_id,
            "canonical_id": self.canonical_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScopeRef":
        return cls(
            kind=str(value.get("kind") or "").strip(),
            target_id=str(value.get("target_id") or value.get("id") or "").strip(),
            instance_id=str(value.get("instance_id") or "").strip(),
            platform=str(value.get("platform") or "").strip(),
            bot_id=str(value.get("bot_id") or "").strip(),
        )

    def validate(self) -> str | None:
        if self.kind not in SCOPES:
            return "invalid_scope_kind"
        if not self.target_id:
            return "missing_scope_target"
        if not self.instance_id:
            return "missing_instance_id"
        if not self.platform:
            return "missing_platform"
        if not self.bot_id:
            return "missing_bot_id"
        return None


def coerce_scope_ref(value: Any) -> ScopeRef | None:
    if isinstance(value, ScopeRef):
        return value
    if isinstance(value, Mapping):
        return ScopeRef.from_mapping(value)
    return None


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _strict_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed


@dataclass(frozen=True)
class RequestContext:
    """Authenticated request context; caller cannot self-assert new authority."""

    protocol_version: int
    instance_id: str
    platform: str
    bot_id: str
    actor_id: str
    conversation_id: str
    persona_id: str
    source_message_id: str
    turn_id: str
    capability: str
    requested_scope: Any
    owner_epoch: int
    deadline: float
    is_private: bool = False
    group_id: str = ""
    unified_msg_origin: str = ""
    roles: tuple[str, ...] = field(default_factory=tuple)
    admin: bool = False
    allow_shared_persona: bool = False
    allow_global_scope: bool = False
    allowed_group_ids: tuple[str, ...] = field(default_factory=tuple)
    allowed_conversation_ids: tuple[str, ...] = field(default_factory=tuple)
    allowed_persona_ids: tuple[str, ...] = field(default_factory=tuple)

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
            "requested_scope": self.requested_scope,
            "owner_epoch": self.owner_epoch,
            "deadline": self.deadline,
            "is_private": self.is_private,
            "group_id": self.group_id,
            "unified_msg_origin": self.unified_msg_origin,
            "roles": list(self.roles),
            "admin": self.admin,
            "allow_shared_persona": self.allow_shared_persona,
            "allow_global_scope": self.allow_global_scope,
            "allowed_group_ids": list(self.allowed_group_ids),
            "allowed_conversation_ids": list(self.allowed_conversation_ids),
            "allowed_persona_ids": list(self.allowed_persona_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RequestContext":
        return cls(
            protocol_version=_strict_int(value.get("protocol_version"), -1),
            instance_id=str(value.get("instance_id") or "").strip(),
            platform=str(value.get("platform") or "").strip(),
            bot_id=str(value.get("bot_id") or "").strip(),
            actor_id=str(value.get("actor_id") or "").strip(),
            conversation_id=str(value.get("conversation_id") or "").strip(),
            persona_id=str(value.get("persona_id") or "").strip(),
            source_message_id=str(value.get("source_message_id") or "").strip(),
            turn_id=str(value.get("turn_id") or "").strip(),
            capability=str(value.get("capability") or "").strip(),
            requested_scope=value.get("requested_scope"),
            owner_epoch=_strict_int(value.get("owner_epoch"), -1),
            deadline=float(value.get("deadline") or 0.0),
            is_private=bool(value.get("is_private", False)),
            group_id=str(value.get("group_id") or "").strip(),
            unified_msg_origin=str(value.get("unified_msg_origin") or "").strip(),
            roles=_tuple_of_str(value.get("roles")),
            admin=bool(value.get("admin", False)),
            allow_shared_persona=bool(value.get("allow_shared_persona", False)),
            allow_global_scope=bool(value.get("allow_global_scope", False)),
            allowed_group_ids=_tuple_of_str(value.get("allowed_group_ids")),
            allowed_conversation_ids=_tuple_of_str(
                value.get("allowed_conversation_ids")
            ),
            allowed_persona_ids=_tuple_of_str(value.get("allowed_persona_ids")),
        )


def coerce_request_context(value: Any) -> RequestContext | None:
    if isinstance(value, RequestContext):
        return value
    if isinstance(value, Mapping):
        try:
            return RequestContext.from_mapping(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def validate_request_context(
    value: Any,
    *,
    expected_method: str | None = None,
    now: float | None = None,
) -> tuple[RequestContext | None, dict[str, Any] | None]:
    """Return ``(context, None)`` or ``(None, terminal_error_result)``."""

    context = coerce_request_context(value)
    if context is None:
        return None, error_result(
            STATUS_INVALID_ARGS,
            "invalid_request_context",
        )
    if context.protocol_version != PROTOCOL_VERSION:
        return None, error_result(STATUS_INVALID_ARGS, "invalid_protocol")
    required = (
        "instance_id",
        "platform",
        "bot_id",
        "actor_id",
        "conversation_id",
        "persona_id",
        "source_message_id",
        "turn_id",
        "capability",
    )
    missing = [name for name in required if not str(getattr(context, name) or "").strip()]
    if missing:
        return None, error_result(
            STATUS_INVALID_ARGS,
            "invalid_request_context",
            data={"missing": missing},
        )
    try:
        owner_epoch = int(context.owner_epoch)
        deadline = float(context.deadline)
    except (TypeError, ValueError, OverflowError):
        return None, error_result(STATUS_INVALID_ARGS, "invalid_request_context")
    if isinstance(context.owner_epoch, bool) or owner_epoch < 0:
        return None, error_result(STATUS_INVALID_ARGS, "owner_epoch_conflict")
    if not math.isfinite(deadline) or deadline <= 0:
        return None, error_result(STATUS_INVALID_ARGS, "invalid_request_context")
    now = time.time() if now is None else float(now)
    if deadline <= now:
        return None, error_result(
            STATUS_INVALID_ARGS,
            "deadline_exceeded",
            data={"deadline": deadline},
        )
    if expected_method:
        expected_capability = METHOD_CAPABILITIES.get(expected_method)
        if not expected_capability:
            return None, error_result(STATUS_INVALID_ARGS, "abi_mismatch")
        accepted = {expected_capability, expected_method}
        if context.capability not in accepted:
            return None, error_result(
                STATUS_INVALID_ARGS,
                "abi_mismatch",
                data={"expected_capability": expected_capability},
            )
    return context, None


def provider_public_methods(provider: Any) -> set[str]:
    methods: set[str] = set()
    try:
        members = dir(provider)
    except Exception:  # noqa: BLE001 - malformed provider is an ABI error
        return methods
    for name in members:
        if name.startswith("_"):
            continue
        try:
            value = getattr(provider, name)
        except Exception:  # noqa: BLE001 - a broken descriptor is not callable
            continue
        if callable(value):
            methods.add(name)
    return methods


def validate_fixed_provider(provider: Any) -> None:
    missing = [
        method
        for method in FIXED_METHODS
        if not callable(getattr(provider, method, None))
    ]
    if missing:
        raise ProviderABIError(f"missing fixed methods: {','.join(missing)}")
    extras = provider_public_methods(provider) - set(FIXED_METHODS)
    if extras:
        raise ProviderABIError(
            "unexpected public methods: " + ",".join(sorted(extras))
        )


@dataclass
class _ProviderRecord:
    provider_id: str
    provider: Any
    methods: frozenset[str]
    registered_at: float
    ready: bool = False


class ProviderRegistry:
    """Exclusive per-context ABI-v1 registry.

    One fixed method has exactly one owner.  Registration is all-or-nothing;
    readiness is separate and terminate removes every owned method at once.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._providers: dict[str, _ProviderRecord] = {}
        self._owners: dict[str, str] = {}

    def register(self, provider: Any, *, provider_id: str) -> str:
        owner = str(provider_id or "").strip()
        if not owner:
            raise ProviderABIError("empty provider id")
        validate_fixed_provider(provider)
        with self._lock:
            if owner in self._providers:
                raise DuplicateProviderError(f"provider already registered: {owner}")
            conflicts = sorted(
                method
                for method in FIXED_METHODS
                if method in self._owners
            )
            if conflicts:
                raise DuplicateProviderError(
                    "fixed method already owned: " + ",".join(conflicts)
                )
            record = _ProviderRecord(
                provider_id=owner,
                provider=provider,
                methods=frozenset(FIXED_METHODS),
                registered_at=float(self._clock()),
                ready=False,
            )
            # Commit only after every check passed; no partial registration.
            self._providers[owner] = record
            for method in FIXED_METHODS:
                self._owners[method] = owner
            return owner

    def mark_ready(self, provider_id: str, *, ready: bool = True) -> None:
        owner = str(provider_id or "").strip()
        with self._lock:
            record = self._providers.get(owner)
            if record is None:
                raise ProviderNotFoundError(f"provider not registered: {owner}")
            record.ready = bool(ready)

    def is_ready(self, provider_id: str | None = None) -> bool:
        with self._lock:
            if provider_id is None:
                return bool(self._providers) and all(
                    record.ready for record in self._providers.values()
                )
            record = self._providers.get(str(provider_id or "").strip())
            return bool(record and record.ready)

    def readiness(self) -> dict[str, Any]:
        with self._lock:
            providers = {
                owner: {
                    "ready": record.ready,
                    "methods": list(FIXED_METHODS),
                }
                for owner, record in self._providers.items()
            }
            return {
                "abi_version": ABI_VERSION,
                "providers": providers,
                "methods": {
                    method: self._providers[owner].ready
                    for method, owner in self._owners.items()
                },
                "ready": bool(providers)
                and all(item["ready"] for item in providers.values()),
            }

    def provider_for(self, method: str) -> Any | None:
        with self._lock:
            owner = self._owners.get(str(method or "").strip())
            record = self._providers.get(owner) if owner else None
            return record.provider if record is not None else None

    async def dispatch(
        self,
        method: str,
        request_context: Any,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = str(method or "").strip()
        if operation not in FIXED_METHODS:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        with self._lock:
            owner = self._owners.get(operation)
            record = self._providers.get(owner) if owner else None
            if record is None or not record.ready:
                return error_result(
                    STATUS_NOT_READY,
                    "provider_not_ready",
                    retryable=True,
                )
            provider = record.provider
        context, validation_error = validate_request_context(
            request_context,
            expected_method=operation,
            now=float(self._clock()),
        )
        if validation_error is not None:
            return validation_error
        try:
            value = getattr(provider, operation)(context, dict(args or {}))
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001 - provider exceptions never escape
            return error_result(
                STATUS_INTERNAL_ERROR,
                "backend_error",
                side_effect_state="unknown",
            )
        return normalize_result(value)

    def terminate(self, provider_id: str) -> bool:
        owner = str(provider_id or "").strip()
        with self._lock:
            record = self._providers.pop(owner, None)
            if record is None:
                return False
            for method in record.methods:
                if self._owners.get(method) == owner:
                    del self._owners[method]
            return True

    def snapshot(self) -> dict[str, Any]:
        """Secret-free lifecycle snapshot for operator diagnostics."""

        with self._lock:
            return {
                "abi_version": ABI_VERSION,
                "providers": [
                    {
                        "provider_id": record.provider_id,
                        "ready": record.ready,
                        "methods": list(FIXED_METHODS),
                        "registered_at": record.registered_at,
                    }
                    for record in self._providers.values()
                ],
                "owners": dict(self._owners),
            }
