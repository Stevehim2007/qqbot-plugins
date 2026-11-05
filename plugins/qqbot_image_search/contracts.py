"""Self-contained ABI v1 provider contracts shared through AstrBot Context.

Both qqbot_web_reader and qqbot_image_search ship an identical copy of this
module.  The module deliberately depends only on the Python standard library
and does not import AstrBot or any sibling plugin.  Provider readiness is
separate from registration: a capability can be registered before its owner
has completed startup, but consumers only receive it after ``mark_ready``.

The registry is attached to the *shared* AstrBot ``Context`` instance under a
stable attribute.  No process-global singleton is used, so separate instances
cannot accidentally share providers.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

ABI_VERSION = 1
REGISTRY_ATTRIBUTE = "_qqbot_service_registry_v1"
REQUIRED_REQUEST_CONTEXT_FIELDS = (
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
REQUIRED_TOOL_RESULT_FIELDS = (
    "status",
    "call_id",
    "result_type",
    "data",
    "error_code",
    "retryable",
    "side_effect_state",
    "provider_operation_id",
)
STABLE_ERROR_CODES = frozenset(
    {
        "permission_denied",
        "invalid_args",
        "not_ready",
        "revision_conflict",
        "confirmation_expired",
        "outcome_unknown",
        "dependency_missing",
        "owner_epoch_mismatch",
        "duplicate_provider",
        "abi_mismatch",
        "invalid_cursor",
        "cursor_expired",
        "cursor_actor_mismatch",
        "cursor_scope_mismatch",
        "cursor_target_mismatch",
        "cache_miss",
        "result_not_found",
        "provider_error",
        "deadline_expired",
    }
)


class ContractError(Exception):
    """Stable ABI error; ``code`` is safe to return to a caller."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or str(code))
        self.code = str(code)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_request_context(**overrides: Any) -> dict[str, Any]:
    """Build a complete RequestContext suitable for tests or adapters."""

    context: dict[str, Any] = {
        "protocol_version": ABI_VERSION,
        "instance_id": "instance-1",
        "platform": "aiocqhttp",
        "bot_id": "bot-1",
        "actor_id": "actor-1",
        "conversation_id": "conversation-1",
        "persona_id": "",
        "source_message_id": "message-1",
        "turn_id": "turn-1",
        "capability": "",
        "requested_scope": {"type": "private", "id": "actor-1"},
        "owner_epoch": 1,
        "deadline": time.time() + 60.0,
    }
    context.update(overrides)
    return context


def validate_request_context(
    request_context: Any,
    *,
    expected_capability: str | None = None,
    now: float | None = None,
) -> str | None:
    """Return a stable error code, or ``None`` when the context is valid."""

    if not isinstance(request_context, Mapping):
        return "invalid_args"
    for field in REQUIRED_REQUEST_CONTEXT_FIELDS:
        if field not in request_context:
            return "invalid_args"
    try:
        protocol_version = int(request_context.get("protocol_version"))
    except (TypeError, ValueError):
        return "abi_mismatch"
    if protocol_version != ABI_VERSION:
        return "abi_mismatch"
    for field in (
        "instance_id",
        "platform",
        "bot_id",
        "actor_id",
        "conversation_id",
        "turn_id",
    ):
        value = request_context.get(field)
        if not isinstance(value, str) or not value:
            return "invalid_args"
    if not isinstance(request_context.get("requested_scope"), (Mapping, str)):
        return "invalid_args"
    try:
        owner_epoch = int(request_context.get("owner_epoch"))
    except (TypeError, ValueError):
        return "invalid_args"
    if owner_epoch < 0:
        return "invalid_args"
    try:
        deadline = float(request_context.get("deadline"))
    except (TypeError, ValueError):
        return "invalid_args"
    if deadline <= (time.time() if now is None else float(now)):
        return "deadline_expired"
    capability = request_context.get("capability")
    if not isinstance(capability, str) or not capability:
        return "invalid_args"
    if expected_capability and capability != expected_capability:
        return "invalid_args"
    return None


def scope_key(request_context: Mapping[str, Any]) -> str:
    return canonical_json(request_context.get("requested_scope", {}))


def args_hash(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(args)).encode("utf-8")).hexdigest()


def make_call_id(
    request_context: Mapping[str, Any],
    capability: str,
    args: Mapping[str, Any],
) -> str:
    material = {
        "instance_id": request_context.get("instance_id"),
        "bot_id": request_context.get("bot_id"),
        "actor_id": request_context.get("actor_id"),
        "conversation_id": request_context.get("conversation_id"),
        "turn_id": request_context.get("turn_id"),
        "capability": capability,
        "args_hash": args_hash(args),
    }
    return "call_" + hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:32]


def tool_result_ok(
    call_id: str,
    result_type: str,
    data: Any,
    *,
    side_effect_state: str = "none",
    provider_operation_id: str = "",
) -> dict[str, Any]:
    return {
        "status": "ok",
        "call_id": str(call_id),
        "result_type": str(result_type),
        "data": data if data is not None else {},
        "error_code": None,
        "retryable": False,
        "side_effect_state": str(side_effect_state or "none"),
        "provider_operation_id": str(provider_operation_id or ""),
    }


def tool_result_error(
    call_id: str,
    error_code: str,
    *,
    data: Any = None,
    retryable: bool = False,
    side_effect_state: str = "none",
    provider_operation_id: str = "",
) -> dict[str, Any]:
    return {
        "status": "error",
        "call_id": str(call_id),
        "result_type": "error",
        "data": data if data is not None else {},
        "error_code": str(error_code or "provider_error"),
        "retryable": bool(retryable),
        "side_effect_state": str(side_effect_state or "none"),
        "provider_operation_id": str(provider_operation_id or ""),
    }


def result_from_context(
    request_context: Mapping[str, Any],
    capability: str,
    args: Mapping[str, Any],
    *,
    result_type: str,
    data: Any,
) -> dict[str, Any]:
    call_id = make_call_id(request_context, capability, args)
    return tool_result_ok(call_id, result_type, data)


def error_from_context(
    request_context: Any,
    capability: str,
    args: Mapping[str, Any] | None,
    error_code: str,
    *,
    data: Any = None,
    retryable: bool = False,
) -> dict[str, Any]:
    safe_args = dict(args or {})
    if isinstance(request_context, Mapping):
        call_id = make_call_id(request_context, capability, safe_args)
    else:
        call_id = "call_invalid"
    return tool_result_error(
        call_id,
        error_code,
        data=data,
        retryable=retryable,
    )


@dataclass
class ProviderRegistration:
    capability: str
    owner: str
    methods: dict[str, Callable[..., Any]]
    owner_epoch: int = 0
    abi_version: int = ABI_VERSION
    ready: bool = False
    registered_at: float = field(default_factory=time.time)

    def get_method(self, *names: str) -> Callable[..., Any] | None:
        for name in names:
            candidate = self.methods.get(name)
            if callable(candidate):
                return candidate
        return None

    def to_status(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "owner": self.owner,
            "owner_epoch": int(self.owner_epoch),
            "abi_version": int(self.abi_version),
            "ready": bool(self.ready),
            "methods": sorted(self.methods),
            "registered_at": self.registered_at,
        }


class SharedRegistry:
    """Per-Context service registry with duplicate-owner protection."""

    abi_version = ABI_VERSION

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._providers: dict[str, ProviderRegistration] = {}
        self._owners: dict[str, set[str]] = {}

    def register_provider(
        self,
        owner: str,
        capability: str,
        methods: Mapping[str, Callable[..., Any]] | Callable[..., Any],
        *,
        owner_epoch: int = 1,
        abi_version: int = ABI_VERSION,
    ) -> ProviderRegistration:
        owner = str(owner or "").strip()
        capability = str(capability or "").strip()
        if not owner or not capability:
            raise ContractError("invalid_args")
        if int(abi_version) != ABI_VERSION:
            raise ContractError("abi_mismatch")
        if callable(methods):
            normalized: dict[str, Callable[..., Any]] = {"provider": methods}
        elif isinstance(methods, Mapping):
            normalized = {
                str(name): method
                for name, method in methods.items()
                if callable(method)
            }
        else:
            raise ContractError("invalid_args")
        if not normalized:
            raise ContractError("invalid_args")
        try:
            epoch = int(owner_epoch)
        except (TypeError, ValueError) as exc:
            raise ContractError("invalid_args") from exc
        if epoch < 0:
            raise ContractError("invalid_args")
        with self._lock:
            existing = self._providers.get(capability)
            if existing is not None:
                # Duplicate owner/provider is always a hard error.  A plugin
                # must not silently replace a provider from a second load.
                raise ContractError("duplicate_provider")
            registration = ProviderRegistration(
                capability=capability,
                owner=owner,
                methods=normalized,
                owner_epoch=epoch,
                abi_version=int(abi_version),
            )
            self._providers[capability] = registration
            self._owners.setdefault(owner, set()).add(capability)
            return registration

    def has_provider(self, capability: str) -> bool:
        with self._lock:
            return str(capability) in self._providers

    def mark_ready(
        self,
        owner: str,
        capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        with self._lock:
            targets = list(capabilities or self._owners.get(str(owner), set()))
            if not targets:
                raise ContractError("dependency_missing")
            for capability in targets:
                registration = self._providers.get(str(capability))
                if registration is None or registration.owner != str(owner):
                    raise ContractError("permission_denied")
                registration.ready = True

    def mark_not_ready(
        self,
        owner: str,
        capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        with self._lock:
            targets = list(capabilities or self._owners.get(str(owner), set()))
            for capability in targets:
                registration = self._providers.get(str(capability))
                if registration is not None and registration.owner == str(owner):
                    registration.ready = False

    def get_provider(self, capability: str) -> ProviderRegistration | None:
        with self._lock:
            return self._providers.get(str(capability))

    def resolve(
        self,
        capability: str,
        *,
        owner_epoch: int | None = None,
        require_ready: bool = True,
    ) -> ProviderRegistration:
        with self._lock:
            registration = self._providers.get(str(capability))
            if registration is None:
                raise ContractError("dependency_missing")
            if int(registration.abi_version) != ABI_VERSION:
                raise ContractError("abi_mismatch")
            if owner_epoch is not None and int(owner_epoch) != int(
                registration.owner_epoch
            ):
                raise ContractError("owner_epoch_mismatch")
            if require_ready and not registration.ready:
                raise ContractError("not_ready")
            return registration

    def unregister_owner(self, owner: str) -> int:
        owner = str(owner)
        with self._lock:
            capabilities = list(self._owners.pop(owner, set()))
            for capability in capabilities:
                registration = self._providers.get(capability)
                if registration is not None and registration.owner == owner:
                    self._providers.pop(capability, None)
            return len(capabilities)

    def owner_capabilities(self, owner: str) -> list[str]:
        with self._lock:
            return sorted(self._owners.get(str(owner), set()))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "abi_version": ABI_VERSION,
                "providers": {
                    capability: registration.to_status()
                    for capability, registration in sorted(self._providers.items())
                },
                "owners": {
                    owner: sorted(capabilities)
                    for owner, capabilities in sorted(self._owners.items())
                },
            }


_ATTACH_LOCK = threading.RLock()
_FALLBACK_REGISTRIES: dict[int, SharedRegistry] = {}


def _registry_is_compatible(candidate: Any) -> bool:
    if candidate is None:
        return True
    if getattr(candidate, "abi_version", None) != ABI_VERSION:
        return False
    return all(
        callable(getattr(candidate, name, None))
        for name in (
            "register_provider",
            "mark_ready",
            "resolve",
            "unregister_owner",
            "status",
        )
    )


def get_shared_registry(context: Any) -> SharedRegistry:
    """Return the registry attached to ``context``.

    A plain object with the ABI v1 interface (for example the copy of this
    module loaded by another plugin) is reused as-is.  Contexts that reject
    attribute assignment fall back to an id-keyed registry, which still gives
    duplicate protection within this plugin module.
    """

    if context is None:
        return SharedRegistry()
    with _ATTACH_LOCK:
        try:
            existing = getattr(context, REGISTRY_ATTRIBUTE, None)
        except Exception:
            existing = None
        if existing is not None:
            if not _registry_is_compatible(existing):
                raise ContractError("abi_mismatch")
            return existing
        registry = SharedRegistry()
        try:
            setattr(context, REGISTRY_ATTRIBUTE, registry)
            return registry
        except Exception:
            pass
        try:
            namespace = getattr(context, "__dict__", None)
            if isinstance(namespace, dict):
                namespace[REGISTRY_ATTRIBUTE] = registry
                return registry
        except Exception:
            pass
        key = id(context)
        registry = _FALLBACK_REGISTRIES.setdefault(key, registry)
        return registry


async def call_provider_async(
    registry: Any,
    capability: str,
    request_context: Mapping[str, Any],
    args: Mapping[str, Any],
    *,
    method_names: tuple[str, ...] = (
        "sticker_collect_plan",
        "collect_plan",
        "provider",
    ),
    owner_epoch: int | None = None,
) -> dict[str, Any]:
    """Resolve and invoke a dependency provider, normalising its result."""

    try:
        registration = registry.resolve(
            capability,
            owner_epoch=owner_epoch,
            require_ready=True,
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code:
            return tool_result_error(
                make_call_id(request_context, capability, args),
                str(code),
            )
        return tool_result_error(
            make_call_id(request_context, capability, args),
            "provider_error",
        )
    method = registration.get_method(*method_names)
    if method is None:
        # A registration with exactly one callable is acceptable even when
        # the provider chose a different internal name.
        callables = [
            item for item in registration.methods.values() if callable(item)
        ]
        method = callables[0] if len(callables) == 1 else None
    if method is None:
        return tool_result_error(
            make_call_id(request_context, capability, args),
            "dependency_missing",
        )
    try:
        result = method(request_context, dict(args))
        if inspect.isawaitable(result):
            result = await result
    except ContractError as exc:
        return tool_result_error(
            make_call_id(request_context, capability, args),
            exc.code,
        )
    except Exception:
        return tool_result_error(
            make_call_id(request_context, capability, args),
            "provider_error",
            retryable=True,
            side_effect_state="unknown",
        )
    if not isinstance(result, dict):
        return tool_result_error(
            make_call_id(request_context, capability, args),
            "provider_error",
        )
    return result
