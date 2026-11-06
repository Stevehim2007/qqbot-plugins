"""Self-contained provider ABI v1, DTOs, confirmation and registry.

Only the Python standard library is used.  Business providers return plain
dictionaries shaped like ToolResult/ReplyPlan; they never return AstrBot result
objects and never send a message from inside a provider method.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

ABI_VERSION = 1
CAPABILITY_STICKERS = "stickers"
STICKER_METHODS: tuple[str, ...] = (
    "sticker_collect_plan",
    "sticker_collect_commit",
    "category_list",
    "category_create",
    "category_rename",
    "category_remove",
    "sticker_categorize",
    "sticker_search",
    "sticker_send_plan",
    "sticker_remove",
    "index_rebuild_plan",
    "index_rebuild_commit",
)
FIXED_PROVIDER_METHODS: dict[str, tuple[str, ...]] = {
    CAPABILITY_STICKERS: STICKER_METHODS,
}
ERROR_CODES = frozenset(
    {
        "permission_denied",
        "invalid_args",
        "not_ready",
        "revision_conflict",
        "confirmation_expired",
        "confirmation_required",
        "outcome_unknown",
        "dependency_missing",
        "deadline_exceeded",
        "not_found",
        "storage_error",
        "already_delivered",
        "unsupported",
    }
)
SIDE_EFFECT_STATES = frozenset({"none", "planned", "committed", "unknown"})
REQUIRED_CONTEXT_FIELDS = (
    "instance_id",
    "platform",
    "bot_id",
    "actor_id",
    "conversation_id",
    "source_message_id",
    "turn_id",
    "capability",
    "requested_scope",
    "owner_epoch",
    "deadline",
)


class ContractError(ValueError):
    """Base error for malformed ABI requests."""

    error_code = "invalid_args"


class ProviderError(RuntimeError):
    error_code = "dependency_missing"


class DuplicateProviderError(ProviderError):
    error_code = "dependency_missing"


class DuplicateOwnerError(ProviderError):
    error_code = "dependency_missing"


class AbiVersionError(ProviderError):
    error_code = "unsupported"


class ProviderAbiError(ProviderError):
    error_code = "unsupported"


class ProviderNotFound(ProviderError):
    error_code = "dependency_missing"


class ProviderNotReady(ProviderError):
    error_code = "not_ready"


class OwnerEpochMismatch(ProviderError):
    error_code = "permission_denied"


def stable_hash(value: Any) -> str:
    """Stable SHA256 over JSON-normalizable data with deterministic key order."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", "replace")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__bytes_sha256__": hashlib.sha256(bytes(value)).hexdigest(),
            "size": len(value),
        }
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return repr(value)


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{name} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ContractError(f"{name} contains control characters")
    if len(text) > 512:
        raise ContractError(f"{name} is too long")
    return text


def canonical_scope(value: Any) -> str:
    """Normalize a requested scope without accepting a filesystem path."""

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ContractError("requested_scope is required")
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise ContractError("requested_scope contains control characters")
        if "/" in text or "\\" in text or ".." in text:
            raise ContractError("requested_scope must be an ID, not a path")
        if ":" not in text:
            text = "conversation:" + text
        kind, target = text.split(":", 1)
        kind = kind.strip().casefold()
        target = target.strip()
    elif isinstance(value, Mapping):
        kind = str(value.get("type", value.get("kind", "")) or "").strip().casefold()
        target = str(
            value.get("id", value.get("target", value.get("value", ""))) or ""
        ).strip()
    else:
        raise ContractError("requested_scope must be a string or mapping")
    if kind not in {"global", "persona", "conversation", "group", "user"}:
        raise ContractError("unknown requested_scope type")
    if kind == "global":
        # Global remains instance-local; a target is optional but never a path.
        target = target or "instance"
    if not target:
        raise ContractError("requested_scope target is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        raise ContractError("requested_scope target contains control characters")
    if "/" in target or "\\" in target or ".." in target:
        raise ContractError("requested_scope target must not be a path")
    if len(kind) + len(target) + 1 > 256:
        raise ContractError("requested_scope is too long")
    return f"{kind}:{target}"


@dataclass(frozen=True)
class RequestContext:
    instance_id: str
    platform: str
    bot_id: str
    actor_id: str
    conversation_id: str
    source_message_id: str
    turn_id: str
    capability: str
    requested_scope: str
    owner_epoch: int
    deadline: float
    protocol_version: int = ABI_VERSION
    persona_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Any) -> "RequestContext":
        if isinstance(raw, cls):
            raw.validate()
            return raw
        if not isinstance(raw, Mapping):
            raise ContractError("request_context must be a mapping")
        missing = [name for name in REQUIRED_CONTEXT_FIELDS if name not in raw]
        if missing:
            raise ContractError("missing request_context fields: " + ",".join(missing))
        try:
            epoch = int(raw.get("owner_epoch"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ContractError("owner_epoch must be an integer") from exc
        if epoch < 0:
            raise ContractError("owner_epoch must be non-negative")
        try:
            deadline = float(raw.get("deadline"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ContractError("deadline must be a timestamp") from exc
        try:
            protocol_version = int(raw.get("protocol_version", ABI_VERSION))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ContractError("protocol_version must be an integer") from exc
        if protocol_version != ABI_VERSION:
            raise AbiVersionError("unsupported request protocol_version")
        extra = raw.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        context = cls(
            instance_id=_required_text(raw.get("instance_id"), "instance_id"),
            platform=_required_text(raw.get("platform"), "platform"),
            bot_id=_required_text(raw.get("bot_id"), "bot_id"),
            actor_id=_required_text(raw.get("actor_id"), "actor_id"),
            conversation_id=_required_text(raw.get("conversation_id"), "conversation_id"),
            source_message_id=_required_text(
                raw.get("source_message_id"), "source_message_id"
            ),
            turn_id=_required_text(raw.get("turn_id"), "turn_id"),
            capability=_required_text(raw.get("capability"), "capability"),
            requested_scope=canonical_scope(raw.get("requested_scope")),
            owner_epoch=epoch,
            deadline=deadline,
            protocol_version=protocol_version,
            persona_id=str(raw.get("persona_id", "") or "").strip(),
            extra=dict(extra),
        )
        context.validate()
        return context

    def validate(self) -> None:
        if self.protocol_version != ABI_VERSION:
            raise AbiVersionError("unsupported protocol_version")
        if self.owner_epoch < 0:
            raise ContractError("owner_epoch must be non-negative")
        if self.capability != CAPABILITY_STICKERS:
            raise ContractError("request capability must be stickers for this provider")
        canonical_scope(self.requested_scope)

    @property
    def owner_scope(self) -> str:
        return canonical_scope(self.requested_scope)

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
            "requested_scope": self.owner_scope,
            "owner_epoch": self.owner_epoch,
            "deadline": self.deadline,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class ToolCall:
    tool_id: str
    call_id: str
    canonical_args: dict[str, Any]
    args_hash: str
    request_context: RequestContext
    confirmation_ref: str = ""

    @classmethod
    def from_mapping(cls, raw: Any) -> "ToolCall":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, Mapping):
            raise ContractError("tool_call must be a mapping")
        context = RequestContext.from_mapping(raw.get("request_context"))
        canonical_args = raw.get("canonical_args", {})
        if not isinstance(canonical_args, dict):
            raise ContractError("canonical_args must be a mapping")
        return cls(
            tool_id=_required_text(raw.get("tool_id"), "tool_id"),
            call_id=_required_text(raw.get("call_id"), "call_id"),
            canonical_args=dict(canonical_args),
            args_hash=str(raw.get("args_hash") or stable_hash(canonical_args)),
            request_context=context,
            confirmation_ref=str(raw.get("confirmation_ref", "") or ""),
        )


def _effective_call_id(context: RequestContext, args: Mapping[str, Any]) -> str:
    existing = str(args.get("call_id", "") or "").strip()
    if existing:
        return existing
    return "call-" + stable_hash(
        {
            "instance": context.instance_id,
            "actor": context.actor_id,
            "turn": context.turn_id,
            "source": context.source_message_id,
            "args": args,
        }
    )[:24]


def _provider_operation_id(
    context: RequestContext, operation: str, args: Mapping[str, Any]
) -> str:
    return (
        operation
        + "-"
        + stable_hash(
            {
                "instance": context.instance_id,
                "scope": context.owner_scope,
                "actor": context.actor_id,
                "turn": context.turn_id,
                "args": args,
            }
        )[:24]
    )


def tool_result(
    context: RequestContext,
    *,
    status: str,
    result_type: str,
    data: dict[str, Any] | None = None,
    error_code: str = "",
    retryable: bool = False,
    side_effect_state: str = "none",
    provider_operation_id: str = "",
    call_id: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Construct the plain-dict ToolResult required by ABI v1."""

    if side_effect_state not in SIDE_EFFECT_STATES:
        side_effect_state = "unknown"
    result = {
        "status": str(status),
        "call_id": call_id or _effective_call_id(context, data or {}),
        "result_type": str(result_type),
        "data": dict(data or {}),
        "error_code": str(error_code or ""),
        "retryable": bool(retryable),
        "side_effect_state": side_effect_state,
        "provider_operation_id": provider_operation_id,
    }
    result.update(extra)
    return result


def success_result(
    context: RequestContext,
    *,
    result_type: str,
    data: dict[str, Any] | None = None,
    operation: str = "",
    args: Mapping[str, Any] | None = None,
    call_id: str = "",
) -> dict[str, Any]:
    args_map = dict(args or {})
    return tool_result(
        context,
        status="succeeded",
        result_type=result_type,
        data=data,
        error_code="",
        retryable=False,
        side_effect_state="committed",
        provider_operation_id=_provider_operation_id(
            context, operation or result_type, args_map
        ),
        call_id=call_id or _effective_call_id(context, args_map),
    )


def error_result(
    context: RequestContext,
    *,
    error_code: str,
    result_type: str = "error",
    data: dict[str, Any] | None = None,
    status: str = "error",
    retryable: bool = False,
    side_effect_state: str = "none",
    operation: str = "",
    args: Mapping[str, Any] | None = None,
    call_id: str = "",
) -> dict[str, Any]:
    args_map = dict(args or {})
    return tool_result(
        context,
        status=status,
        result_type=result_type,
        data=data,
        error_code=error_code,
        retryable=retryable,
        side_effect_state=side_effect_state,
        provider_operation_id=_provider_operation_id(
            context, operation or result_type, args_map
        ),
        call_id=call_id or _effective_call_id(context, args_map),
    )


def reply_plan(
    context: RequestContext,
    *,
    text_segments: list[Any] | None = None,
    media_segments: list[Any] | None = None,
    terminal: bool = True,
    delivery_key: str = "",
    data: dict[str, Any] | None = None,
    call_id: str = "",
) -> dict[str, Any]:
    """Construct a ToolResult/ReplyPlan-shaped dict; no message is sent."""

    plan = tool_result(
        context,
        status="planned",
        result_type="reply_plan",
        data=data,
        error_code="",
        retryable=False,
        side_effect_state="planned",
        provider_operation_id=delivery_key,
        call_id=call_id,
    )
    plan.update(
        {
            "request_context": context.to_dict(),
            "text_segments": list(text_segments or []),
            "media_segments": list(media_segments or []),
            "terminal": bool(terminal),
            "delivery_key": str(delivery_key),
        }
    )
    return plan


@dataclass
class ConfirmationRecord:
    token: str
    action: str
    payload_hash: str
    instance_id: str
    actor_id: str
    requested_scope: str
    conversation_id: str
    persona_id: str
    owner_epoch: int
    created_at: float
    expires_at: float
    consumed_at: float | None = None


@dataclass(frozen=True)
class ConfirmationDecision:
    ok: bool
    error_code: str = ""
    record: ConfirmationRecord | None = None
    reason: str = ""


class ConfirmationManager:
    """Short-lived, payload/actor/scope-bound confirmation tokens."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        default_ttl_seconds: int = 300,
        max_records: int = 1024,
    ) -> None:
        self.clock = clock
        self.default_ttl_seconds = max(1, min(600, int(default_ttl_seconds)))
        self.max_records = max(1, int(max_records))
        self._records: dict[str, ConfirmationRecord] = {}
        self._lock = threading.RLock()

    def _prune_locked(self, now: float | None = None) -> None:
        current = self.clock() if now is None else now
        expired = [
            token
            for token, record in self._records.items()
            if record.consumed_at is not None or current > record.expires_at
        ]
        for token in expired:
            self._records.pop(token, None)
        if len(self._records) > self.max_records:
            ordered = sorted(
                self._records.items(), key=lambda pair: pair[1].created_at
            )
            for token, _record in ordered[: len(self._records) - self.max_records]:
                self._records.pop(token, None)

    def issue(
        self,
        context: RequestContext,
        *,
        action: str,
        payload: Mapping[str, Any],
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        now = float(self.clock())
        ttl = self.default_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        ttl = max(1, min(600, ttl))
        token = "cf1_" + secrets.token_urlsafe(32)
        record = ConfirmationRecord(
            token=token,
            action=str(action),
            payload_hash=stable_hash(dict(payload)),
            instance_id=context.instance_id,
            actor_id=context.actor_id,
            requested_scope=context.owner_scope,
            conversation_id=context.conversation_id,
            persona_id=context.persona_id,
            owner_epoch=int(context.owner_epoch),
            created_at=now,
            expires_at=now + ttl,
        )
        with self._lock:
            self._prune_locked(now)
            self._records[token] = record
        return {
            "confirmation_ref": token,
            "expires_at": record.expires_at,
            "ttl_seconds": ttl,
            "action": record.action,
            "payload_hash": record.payload_hash,
        }

    def validate(
        self,
        token: str,
        context: RequestContext,
        *,
        action: str,
        payload: Mapping[str, Any],
        consume: bool = False,
    ) -> ConfirmationDecision:
        token = str(token or "").strip()
        if not token:
            return ConfirmationDecision(False, "confirmation_required", reason="missing")
        now = float(self.clock())
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(token)
            if record is None:
                return ConfirmationDecision(
                    False, "confirmation_expired", reason="missing_or_expired"
                )
            if record.consumed_at is not None or now > record.expires_at:
                return ConfirmationDecision(
                    False, "confirmation_expired", reason="expired_or_consumed"
                )
            if (
                record.instance_id != context.instance_id
                or record.actor_id != context.actor_id
                or record.requested_scope != context.owner_scope
                or record.conversation_id != context.conversation_id
                or record.persona_id != context.persona_id
                or record.owner_epoch != int(context.owner_epoch)
            ):
                return ConfirmationDecision(
                    False, "permission_denied", reason="binding_mismatch", record=record
                )
            if record.action != str(action):
                return ConfirmationDecision(
                    False, "revision_conflict", reason="action_changed", record=record
                )
            if record.payload_hash != stable_hash(dict(payload)):
                return ConfirmationDecision(
                    False, "revision_conflict", reason="payload_changed", record=record
                )
            if consume:
                record.consumed_at = now
            return ConfirmationDecision(True, record=record)


@dataclass
class ProviderRecord:
    provider_id: str
    capability: str
    abi_version: int
    methods: dict[str, Callable[..., Any]]
    owner_epoch: int
    ready: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    active: bool = True


class ProviderRegistration:
    def __init__(self, registry: "ProviderRegistry", record: ProviderRecord) -> None:
        self.registry = registry
        self.record = record

    @property
    def provider_id(self) -> str:
        return self.record.provider_id

    @property
    def ready(self) -> bool:
        return bool(self.record.active and self.record.ready)

    def mark_ready(self, owner_epoch: int | None = None) -> None:
        self.registry.mark_ready(self.provider_id, owner_epoch=owner_epoch)

    def mark_not_ready(self) -> None:
        self.registry.mark_not_ready(self.provider_id)

    def set_owner_epoch(self, owner_epoch: int) -> None:
        self.registry.set_owner_epoch(self.provider_id, owner_epoch)

    def close(self) -> None:
        self.registry.unregister(self.provider_id)


class ProviderRegistry:
    """One Context-local registry with duplicate-provider and owner checks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: dict[str, ProviderRecord] = {}
        self._owners: dict[str, str] = {}

    def register_provider(
        self,
        provider_id: str,
        capability: str,
        methods: Mapping[str, Callable[..., Any]],
        *,
        abi_version: int = ABI_VERSION,
        owner_epoch: int = 0,
        ready: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderRegistration:
        pid = _required_text(provider_id, "provider_id")
        cap = _required_text(capability, "capability")
        if int(abi_version) != ABI_VERSION:
            raise AbiVersionError("unsupported provider ABI version")
        expected = FIXED_PROVIDER_METHODS.get(cap)
        if expected is not None:
            if set(methods) != set(expected):
                missing = sorted(set(expected) - set(methods))
                extra = sorted(set(methods) - set(expected))
                raise ProviderAbiError(
                    f"provider method set mismatch; missing={missing}, extra={extra}"
                )
        if not methods:
            raise ProviderAbiError("provider must register at least one method")
        for name, callable_ in methods.items():
            if not str(name).isidentifier():
                raise ProviderAbiError("provider method names must be identifiers")
            if not callable(callable_):
                raise ProviderAbiError(f"provider method is not callable: {name}")
        with self._lock:
            current = self._providers.get(pid)
            if current is not None and current.active:
                raise DuplicateProviderError(f"provider already registered: {pid}")
            owner = self._owners.get(cap)
            if owner and owner != pid:
                active_owner = self._providers.get(owner)
                if active_owner is not None and active_owner.active:
                    raise DuplicateOwnerError(
                        f"capability {cap} already has owner {owner}"
                    )
            record = ProviderRecord(
                provider_id=pid,
                capability=cap,
                abi_version=int(abi_version),
                methods=dict(methods),
                owner_epoch=int(owner_epoch),
                ready=bool(ready),
                metadata=dict(metadata or {}),
            )
            self._providers[pid] = record
            self._owners[cap] = pid
            return ProviderRegistration(self, record)

    def resolve_provider(
        self,
        capability: str,
        *,
        provider_id: str = "",
        require_ready: bool = False,
    ) -> ProviderRecord | None:
        with self._lock:
            if provider_id:
                record = self._providers.get(provider_id)
            else:
                owner = self._owners.get(capability)
                record = self._providers.get(owner) if owner else None
            if record is None or not record.active:
                return None
            if record.capability != capability:
                return None
            if require_ready and not record.ready:
                return None
            return record

    def mark_ready(self, provider_id: str, owner_epoch: int | None = None) -> None:
        with self._lock:
            record = self._providers.get(provider_id)
            if record is None or not record.active:
                raise ProviderNotFound(provider_id)
            if owner_epoch is not None:
                record.owner_epoch = int(owner_epoch)
            record.ready = True

    def mark_not_ready(self, provider_id: str) -> None:
        with self._lock:
            record = self._providers.get(provider_id)
            if record is not None and record.active:
                record.ready = False

    def set_owner_epoch(self, provider_id: str, owner_epoch: int) -> None:
        with self._lock:
            record = self._providers.get(provider_id)
            if record is None or not record.active:
                raise ProviderNotFound(provider_id)
            record.owner_epoch = int(owner_epoch)

    def unregister(self, provider_id: str) -> bool:
        with self._lock:
            record = self._providers.get(provider_id)
            if record is None:
                return False
            record.active = False
            record.ready = False
            if self._owners.get(record.capability) == provider_id:
                self._owners.pop(record.capability, None)
            self._providers.pop(provider_id, None)
            return True

    def provider_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._providers))

    def readiness(self) -> dict[str, bool]:
        with self._lock:
            return {
                record.capability: bool(record.ready and record.active)
                for record in self._providers.values()
            }

    async def invoke(
        self,
        capability: str,
        method: str,
        request_context: RequestContext | Mapping[str, Any],
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            context = RequestContext.from_mapping(request_context)
        except ContractError as exc:
            return {
                "status": "error",
                "call_id": "invalid-context",
                "result_type": "error",
                "data": {},
                "error_code": exc.error_code,
                "retryable": False,
                "side_effect_state": "none",
                "provider_operation_id": "",
            }
        record = self.resolve_provider(capability, require_ready=True)
        if record is None:
            return error_result(
                context,
                error_code="not_ready",
                result_type="error",
                operation=method,
                args=args or {},
            )
        if context.owner_epoch != record.owner_epoch:
            return error_result(
                context,
                error_code="permission_denied",
                result_type="error",
                data={"reason": "stale_owner_epoch"},
                operation=method,
                args=args or {},
            )
        callable_ = record.methods.get(method)
        if callable_ is None:
            return error_result(
                context,
                error_code="invalid_args",
                result_type="error",
                data={"reason": "unknown_method", "method": method},
                operation=method,
                args=args or {},
            )
        try:
            value = callable_(context, dict(args or {}))
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, dict):
                return value
            return error_result(
                context,
                error_code="unsupported",
                result_type="error",
                data={"reason": "provider_returned_non_dict"},
                operation=method,
                args=args or {},
            )
        except ContractError as exc:
            return error_result(
                context,
                error_code=exc.error_code,
                result_type="error",
                operation=method,
                args=args or {},
            )
        except OwnerEpochMismatch:
            return error_result(
                context,
                error_code="permission_denied",
                result_type="error",
                operation=method,
                args=args or {},
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                result_type="error",
                side_effect_state="unknown",
                retryable=False,
                operation=method,
                args=args or {},
            )

    def close(self) -> None:
        with self._lock:
            records = list(self._providers.values())
        for record in records:
            self.unregister(record.provider_id)


_CONTEXT_REGISTRY_ATTR = "_qqbot_provider_registry_abi_v1"
_FALLBACK_REGISTRIES: dict[int, ProviderRegistry] = {}
_FALLBACK_LOCK = threading.RLock()


def get_registry(context: Any) -> ProviderRegistry:
    """Return the one registry attached to a shared AstrBot Context.

    Real AstrBot versions may expose a public service locator.  When they do
    not, the contract explicitly permits an explicitly named object attached to
    the already-shared Context.  A process-local fallback keyed by Context is
    used only if the Context rejects attributes.
    """

    locator = getattr(context, "get_service_registry", None)
    if callable(locator):
        try:
            registry = locator()
            if isinstance(registry, ProviderRegistry):
                return registry
        except Exception:
            pass
    existing = getattr(context, _CONTEXT_REGISTRY_ATTR, None)
    if isinstance(existing, ProviderRegistry):
        return existing
    with _FALLBACK_LOCK:
        registry = ProviderRegistry()
        try:
            setattr(context, _CONTEXT_REGISTRY_ATTR, registry)
            return registry
        except Exception:
            key = id(context)
            current = _FALLBACK_REGISTRIES.get(key)
            if current is None:
                current = registry
                _FALLBACK_REGISTRIES[key] = current
            return current


def close_context_registry(context: Any) -> None:
    """Best-effort registry cleanup used by plugin terminate()."""

    registry = getattr(context, _CONTEXT_REGISTRY_ATTR, None)
    if isinstance(registry, ProviderRegistry):
        registry.close()
    with _FALLBACK_LOCK:
        current = _FALLBACK_REGISTRIES.pop(id(context), None)
        if current is not None:
            current.close()


__all__ = [
    "ABI_VERSION",
    "CAPABILITY_STICKERS",
    "STICKER_METHODS",
    "FIXED_PROVIDER_METHODS",
    "ERROR_CODES",
    "ContractError",
    "DuplicateProviderError",
    "DuplicateOwnerError",
    "AbiVersionError",
    "ProviderAbiError",
    "ProviderNotFound",
    "ProviderNotReady",
    "OwnerEpochMismatch",
    "RequestContext",
    "ToolCall",
    "ConfirmationManager",
    "ConfirmationDecision",
    "ProviderRecord",
    "ProviderRegistration",
    "ProviderRegistry",
    "canonical_scope",
    "stable_hash",
    "tool_result",
    "success_result",
    "error_result",
    "reply_plan",
    "get_registry",
    "close_context_registry",
]
