"""ABI v1 contracts and the shared provider-readiness registry.

Only Python's standard library is used.  DTOs are dict-compatible so the same
objects can travel through AstrBot's public Context without a shared pip
package.  Runtime data is never trusted from marker/model text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

ABI_VERSION = 1
PROTOCOL_VERSION = 1
SUCCESS_STATUS = "ok"

ERROR_CODES = frozenset(
    {
        "permission_denied",
        "invalid_args",
        "not_ready",
        "revision_conflict",
        "confirmation_expired",
        "outcome_unknown",
        "dependency_missing",
        "duplicate_owner",
        "abi_mismatch",
        "owner_epoch_mismatch",
        "owner_epoch_stale",
        "turn_conflict",
        "turn_cancelled",
        "deadline_exceeded",
        "timeout",
        "delivery_unknown",
        "invalid_state",
        "duplicate_call",
        "not_found",
        "conflict",
        "internal_error",
    }
)
WRITE_ERROR_CODES = ERROR_CODES - {
    "not_found",
    "conflict",
    "internal_error",
    "timeout",
    "invalid_state",
}


class ContractError(Exception):
    """Validated ABI error; callers render ``error_code`` as data, not text."""

    def __init__(
        self,
        error_code: str = "invalid_args",
        message: str = "",
        *,
        data: Any = None,
    ) -> None:
        code = str(error_code or "invalid_args")
        if code not in ERROR_CODES:
            code = "invalid_args"
        super().__init__(message or code)
        self.error_code = code
        self.message = str(message or "")
        self.data = data

    @property
    def code(self) -> str:
        return self.error_code

    def to_dict(self) -> dict[str, Any]:
        output = {"status": "error", "error_code": self.error_code}
        if self.message:
            output["message"] = self.message
        if self.data is not None:
            output["data"] = self.data
        return output


class _DictMixin:
    """Small mapping facade shared by every ABI object."""

    _dict_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self._dict_fields}

    def __getitem__(self, key: str) -> Any:
        if key not in self._dict_fields:
            raise KeyError(key)
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        return key in self._dict_fields

    def keys(self):
        return self._dict_fields

    def values(self):
        return [getattr(self, name) for name in self._dict_fields]

    def items(self):
        return [(name, getattr(self, name)) for name in self._dict_fields]

    def __iter__(self):
        return iter(self._dict_fields)

    def __len__(self) -> int:
        return len(self._dict_fields)


@dataclass
class RequestContext(_DictMixin):
    protocol_version: int = PROTOCOL_VERSION
    instance_id: str = ""
    platform: str = ""
    bot_id: str = ""
    actor_id: str = ""
    conversation_id: str = ""
    persona_id: str = ""
    source_message_id: str = ""
    turn_id: str = ""
    capability: str = ""
    requested_scope: Any = field(default_factory=dict)
    owner_epoch: int = 0
    deadline: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    _dict_fields = (
        "protocol_version",
        "instance_id",
        "platform",
        "bot_id",
        "actor_id",
        "conversation_id",
        "persona_id",
        "source_message_id",
        "turn_id",
        "capability",
        "requested_scope",
        "owner_epoch",
        "deadline",
    )

    def __post_init__(self) -> None:
        try:
            self.protocol_version = int(self.protocol_version)
        except (TypeError, ValueError):
            pass
        try:
            self.owner_epoch = int(self.owner_epoch)
        except (TypeError, ValueError):
            pass
        try:
            self.deadline = float(self.deadline)
        except (TypeError, ValueError):
            pass
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
        ):
            value = getattr(self, name)
            if value is not None:
                setattr(self, name, str(value))
        if not isinstance(self.metadata, dict):
            self.metadata = dict(self.metadata or {})

    @classmethod
    def from_dict(cls, value: Any) -> "RequestContext":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContractError("invalid_args", "request_context must be a mapping")
        known = {item.name for item in fields(cls)}
        payload = {key: value[key] for key in value if key in known}
        extras = {str(key): value[key] for key in value if key not in known}
        context = cls(**payload)
        context.metadata.update(extras)
        return context


@dataclass
class ToolCall(_DictMixin):
    tool_id: str = ""
    call_id: str = ""
    canonical_args: dict[str, Any] = field(default_factory=dict)
    args_hash: str = ""
    request_context: Any = field(default_factory=dict)
    confirmation_ref: str = ""
    _dict_fields = (
        "tool_id",
        "call_id",
        "canonical_args",
        "args_hash",
        "request_context",
        "confirmation_ref",
    )

    @classmethod
    def from_dict(cls, value: Any) -> "ToolCall":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContractError("invalid_args", "tool_call must be a mapping")
        known = {item.name for item in fields(cls)}
        payload = {key: value[key] for key in value if key in known}
        call = cls(**payload)
        call.request_context = load_request_context(call.request_context)
        return call


def _require_text(context: RequestContext, name: str, *, allow_empty: bool = False) -> None:
    value = getattr(context, name)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ContractError("invalid_args", f"request_context.{name} is required")


def load_request_context(value: Any, *, require_deadline: bool = False) -> RequestContext:
    """Load + validate a RequestContext; raise ``ContractError`` on failure."""

    context = RequestContext.from_dict(value)
    if context.protocol_version != PROTOCOL_VERSION:
        raise ContractError("abi_mismatch", "unsupported protocol_version")
    for name in (
        "instance_id",
        "platform",
        "bot_id",
        "actor_id",
        "conversation_id",
        "source_message_id",
        "turn_id",
        "capability",
    ):
        _require_text(context, name)
    _require_text(context, "persona_id", allow_empty=True)
    if not isinstance(context.owner_epoch, int) or context.owner_epoch < 0:
        raise ContractError("invalid_args", "request_context.owner_epoch invalid")
    if not isinstance(context.deadline, (int, float)) or context.deadline < 0:
        raise ContractError("invalid_args", "request_context.deadline invalid")
    if require_deadline and context.deadline <= 0:
        raise ContractError("invalid_args", "request_context.deadline is required")
    if context.requested_scope is None:
        raise ContractError("invalid_args", "request_context.requested_scope is required")
    return context


validate_request_context = load_request_context


def parse_error_code(value: Any) -> str:
    code = str(value or "")
    return code if code in ERROR_CODES else "internal_error"


def is_valid_error_code(value: Any) -> bool:
    return str(value or "") in ERROR_CODES


@dataclass
class ToolResult(_DictMixin):
    status: str = "ok"
    call_id: str = ""
    result_type: str = "data"
    data: Any = field(default_factory=dict)
    error_code: str = ""
    retryable: bool = False
    side_effect_state: str = "none"
    provider_operation_id: str = ""
    _dict_fields = (
        "status",
        "call_id",
        "result_type",
        "data",
        "error_code",
        "retryable",
        "side_effect_state",
        "provider_operation_id",
    )

    @classmethod
    def from_dict(cls, value: Any) -> "ToolResult":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContractError("invalid_args", "tool_result must be a mapping")
        known = {item.name for item in fields(cls)}
        payload = {key: value[key] for key in value if key in known}
        return cls(**payload)


def validate_tool_result(value: Any) -> ToolResult:
    result = ToolResult.from_dict(value)
    status = str(result.status or "").strip().lower()
    if status not in {
        "ok",
        "success",
        "error",
        "failed",
        "rejected",
        "outcome_unknown",
        "delivery_unknown",
        "cancelled",
    }:
        raise ContractError("invalid_args", "tool_result.status invalid")
    result.status = status
    if not str(result.call_id or "").strip():
        raise ContractError("invalid_args", "tool_result.call_id required")
    if status not in {"ok", "success"}:
        if not is_valid_error_code(result.error_code):
            raise ContractError(
                "invalid_args",
                "tool_result.error_code invalid for non-success status",
            )
    elif result.error_code and not is_valid_error_code(result.error_code):
        raise ContractError("invalid_args", "tool_result.error_code invalid")
    return result


@dataclass
class ReplyPlan(_DictMixin):
    request_context: Any = field(default_factory=dict)
    text_segments: list[str] = field(default_factory=list)
    media_segments: list[dict[str, Any]] = field(default_factory=list)
    terminal: bool = True
    delivery_key: str = ""
    _dict_fields = (
        "request_context",
        "text_segments",
        "media_segments",
        "terminal",
        "delivery_key",
    )

    def to_dict(self) -> dict[str, Any]:
        request_context = self.request_context
        if isinstance(request_context, RequestContext):
            request_context = request_context.to_dict()
        return {
            "request_context": request_context,
            "text_segments": list(self.text_segments),
            "media_segments": list(self.media_segments),
            "terminal": bool(self.terminal),
            "delivery_key": str(self.delivery_key or ""),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ReplyPlan":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ContractError("invalid_args", "reply_plan must be a mapping")
        known = {item.name for item in fields(cls)}
        payload = {key: value[key] for key in value if key in known}
        plan = cls(**payload)
        plan.request_context = load_request_context(plan.request_context)
        return plan


def validate_reply_plan(value: Any) -> ReplyPlan:
    plan = ReplyPlan.from_dict(value)
    if not isinstance(plan.text_segments, (list, tuple)):
        raise ContractError("invalid_args", "reply_plan.text_segments must be a list")
    texts = []
    for segment in plan.text_segments:
        if not isinstance(segment, str):
            raise ContractError("invalid_args", "reply_plan text segment must be str")
        texts.append(segment)
    plan.text_segments = texts
    if not isinstance(plan.media_segments, (list, tuple)):
        raise ContractError("invalid_args", "reply_plan.media_segments must be a list")
    plan.media_segments = [dict(item) for item in plan.media_segments if isinstance(item, Mapping)]
    plan.terminal = bool(plan.terminal)
    if not isinstance(plan.delivery_key, str):
        raise ContractError("invalid_args", "reply_plan.delivery_key must be str")
    return plan


@dataclass
class ContractResponse(_DictMixin):
    status: str = SUCCESS_STATUS
    data: Any = field(default_factory=dict)
    error_code: str = ""
    message: str = ""
    _dict_fields = ("status", "data", "error_code", "message")

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "success"}

    def to_dict(self) -> dict[str, Any]:
        output: dict[str, Any] = {"status": self.status, "data": self.data}
        if self.error_code:
            output["error_code"] = self.error_code
        if self.message:
            output["message"] = self.message
        return output


def ok(data: Any = None, message: str = "") -> ContractResponse:
    return ContractResponse("ok", {} if data is None else data, message=message)


def fail(error_code: str, message: str = "", data: Any = None) -> ContractResponse:
    code = parse_error_code(error_code)
    return ContractResponse("error", {} if data is None else data, code, message)


# ---------------------------------------------------------------------------
# Shared Context registry
# ---------------------------------------------------------------------------
@dataclass
class ProviderDescriptor:
    provider_id: str
    plugin_id: str
    capability: str
    abi_version: int
    owner: str
    methods: dict[str, Any] = field(default_factory=dict)
    owner_epoch: int = 0
    ready: bool = False
    service: Any = None
    registered_at: float = field(default_factory=time.time)

    @property
    def star_cls(self) -> Any:
        return self.service

    @property
    def name(self) -> str:
        return self.provider_id

    def method_names(self) -> list[str]:
        return sorted(self.methods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "plugin_id": self.plugin_id,
            "capability": self.capability,
            "abi_version": self.abi_version,
            "owner": self.owner,
            "owner_epoch": self.owner_epoch,
            "ready": self.ready,
            "methods": self.method_names(),
            "registered_at": self.registered_at,
        }


class RuntimeRegistry:
    """Small in-process provider registry with exclusive owners.

    The registry never dispatches arbitrary method names and never calls an
    LLM.  It is deliberately attached to the shared AstrBot ``Context`` object
    because 4.29.3 has no public generic service locator.
    """

    def __init__(self) -> None:
        self.providers: dict[str, ProviderDescriptor] = {}

    def register_provider(
        self,
        provider_id: str,
        *,
        plugin_id: str | None = None,
        capability: str | None = None,
        abi_version: int = ABI_VERSION,
        owner: str | None = None,
        methods: Mapping[str, Any] | None = None,
        owner_epoch: int = 0,
        ready: bool = False,
        service: Any = None,
    ) -> ProviderDescriptor:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            raise ContractError("invalid_args", "provider_id required")
        if int(abi_version) != ABI_VERSION:
            raise ContractError(
                "abi_mismatch",
                f"provider ABI {abi_version!r} != {ABI_VERSION}",
            )
        owner = str(owner or provider_id)
        existing = self.providers.get(provider_id)
        if existing is not None and existing.owner != owner:
            raise ContractError(
                "duplicate_owner",
                f"provider {provider_id!r} already owned by {existing.owner!r}",
            )
        method_map = dict(methods or {})
        if existing is not None:
            # Same owner re-registration is idempotent and keeps object
            # identity for consumers that cached the descriptor.
            existing.plugin_id = str(plugin_id or existing.plugin_id)
            existing.capability = str(capability or existing.capability)
            existing.methods = method_map or existing.methods
            existing.owner_epoch = int(owner_epoch)
            existing.ready = bool(ready)
            if service is not None:
                existing.service = service
            return existing
        descriptor = ProviderDescriptor(
            provider_id=provider_id,
            plugin_id=str(plugin_id or provider_id),
            capability=str(capability or provider_id),
            abi_version=ABI_VERSION,
            owner=owner,
            methods=method_map,
            owner_epoch=int(owner_epoch),
            ready=bool(ready),
            service=service,
        )
        self.providers[provider_id] = descriptor
        return descriptor

    register = register_provider
    register_service = register_provider

    def get_provider(self, provider_id: str) -> ProviderDescriptor | None:
        return self.providers.get(str(provider_id or ""))

    get = get_provider

    def require_provider(
        self,
        provider_id: str,
        *,
        ready: bool = False,
    ) -> ProviderDescriptor:
        descriptor = self.get_provider(provider_id)
        if descriptor is None:
            raise ContractError("dependency_missing", "provider not registered")
        if ready and not descriptor.ready:
            raise ContractError("not_ready", "provider is not ready")
        return descriptor

    def set_ready(
        self,
        provider_id: str,
        ready: bool,
        *,
        owner: str | None = None,
        owner_epoch: int | None = None,
    ) -> bool:
        descriptor = self.get_provider(provider_id)
        if descriptor is None:
            raise ContractError("not_found", "provider not registered")
        if owner is not None and descriptor.owner != str(owner):
            raise ContractError("permission_denied", "provider owner mismatch")
        if owner_epoch is not None and descriptor.owner_epoch != int(owner_epoch):
            raise ContractError("owner_epoch_mismatch", "owner epoch mismatch")
        descriptor.ready = bool(ready)
        return descriptor.ready

    def unregister(
        self,
        provider_id: str,
        *,
        owner: str | None = None,
    ) -> bool:
        descriptor = self.get_provider(provider_id)
        if descriptor is None:
            return False
        if owner is not None and descriptor.owner != str(owner):
            raise ContractError("permission_denied", "provider owner mismatch")
        self.providers.pop(descriptor.provider_id, None)
        return True

    def is_ready(self, provider_id: str) -> bool:
        descriptor = self.get_provider(provider_id)
        return bool(descriptor is not None and descriptor.ready)

    def snapshot(self) -> dict[str, Any]:
        return {
            key: descriptor.to_dict()
            for key, descriptor in self.providers.items()
        }


_REGISTRY_ATTR = "qqbot_service_registry"
_REGISTRY_ALIASES = (
    _REGISTRY_ATTR,
    "qqbot_services",
    "qqbot_contract_registry",
    "qqbot_runtime_registry",
)


def ensure_registry(context: Any) -> RuntimeRegistry:
    """Return the Context-scoped registry, creating it exactly once."""

    existing = getattr(context, _REGISTRY_ATTR, None)
    if existing is not None and hasattr(existing, "register_provider"):
        return existing
    registry = RuntimeRegistry()
    for name in _REGISTRY_ALIASES:
        try:
            setattr(context, name, registry)
        except Exception:
            pass
    return registry


def register_provider_readiness(
    context: Any,
    provider_id: str,
    **kwargs: Any,
) -> ProviderDescriptor:
    return ensure_registry(context).register_provider(provider_id, **kwargs)


def mark_provider_ready(
    context: Any,
    provider_id: str,
    ready: bool,
    **kwargs: Any,
) -> bool:
    return ensure_registry(context).set_ready(provider_id, ready, **kwargs)


def unregister_provider(context: Any, provider_id: str, **kwargs: Any) -> bool:
    return ensure_registry(context).unregister(provider_id, **kwargs)


def get_provider(context: Any, provider_id: str) -> ProviderDescriptor | None:
    return ensure_registry(context).get_provider(provider_id)
