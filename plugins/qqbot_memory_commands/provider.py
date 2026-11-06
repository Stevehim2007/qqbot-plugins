"""Fixed ABI-v1 provider implementation for memory/session/persona.

All public methods are exactly the 13 names in ``contracts.FIXED_METHODS``.
Business revisions are checked by the backend adapter; confirmation tokens and
idempotency intents are committed in the local ledger before any write.
"""

from __future__ import annotations

import inspect
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .authorization import (
    ScopeAuthorization,
    ScopeResolutionError,
    authorize_scope,
    resolve_requested_scope,
    resolve_scope,
    scope_public_view,
)
from .contracts import (
    PROTOCOL_VERSION,
    SCOPES,
    STATUS_CONFIRMATION_EXPIRED,
    STATUS_CONFIRMATION_REQUIRED,
    STATUS_DEPENDENCY_MISSING,
    STATUS_INTERNAL_ERROR,
    STATUS_INVALID_ARGS,
    STATUS_NOT_READY,
    STATUS_OK,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PERMISSION_DENIED,
    STATUS_REVISION_CONFLICT,
    RequestContext,
    ScopeRef,
    canonical_hash,
    coerce_scope_ref,
    error_result,
    is_ok,
    make_result,
    normalize_result,
    validate_request_context,
)
from .store import AsyncKeyedLocks, OperationStore, safe_audit_summary

_VALID_SESSION_KEY_MODES = {"legacy_compatible", "platform_bot_conversation"}
_VALID_PERSONA_SWITCH_MODES = {"cancel_inflight", "finish_bound_turn"}
_VALID_RETENTION_MODES = {"preserve", "legacy_compatible"}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "启用", "开"}:
            return True
        if lowered in {"0", "false", "no", "off", "禁用", "关", ""}:
            return False
    return default


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = value
    else:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _nested(mapping: Mapping[str, Any], section: str, key: str, default: Any) -> Any:
    section_value = mapping.get(section)
    if isinstance(section_value, Mapping) and key in section_value:
        return section_value[key]
    dotted = f"{section}.{key}"
    return mapping.get(dotted, default)


def _pick(
    mapping: Mapping[str, Any],
    flat: str,
    section: str,
    key: str,
    default: Any,
) -> Any:
    if flat in mapping:
        return mapping[flat]
    return _nested(mapping, section, key, default)


@dataclass(frozen=True)
class ProviderConfig:
    backend_kind: str = "astrbot_public_api"
    backend_store_id: str = "memory_commands_ledger"
    allowed_scopes: tuple[str, ...] = SCOPES
    default_scope: str = "conversation"
    policy_id: str = ""
    session_key_mode: str = "platform_bot_conversation"
    session_persona_isolation: bool = True
    session_reset_confirm: bool = True
    memory_max_read_items: int = 50
    memory_max_write_chars: int = 2000
    memory_clear_confirm: bool = True
    memory_retention_mode: str = "preserve"
    persona_switch_mode: str = "finish_bound_turn"
    persona_admin_shared_edit: bool = False
    operations_idempotency_ttl_seconds: int = 86400
    operations_confirmation_ttl_seconds: int = 300

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "ProviderConfig":
        data = _mapping(mapping)
        allowed = _strings(
            _pick(data, "scope_allowed", "scope", "allowed", SCOPES)
        )
        valid_allowed = tuple(item for item in allowed if item in SCOPES)
        if not valid_allowed:
            valid_allowed = SCOPES
        default_scope = str(
            _pick(data, "scope_default", "scope", "default", "conversation")
        ).strip()
        if default_scope not in SCOPES:
            default_scope = "conversation"
        session_key_mode = str(
            _pick(
                data,
                "session_key_mode",
                "session",
                "key_mode",
                "platform_bot_conversation",
            )
        ).strip()
        if session_key_mode not in _VALID_SESSION_KEY_MODES:
            session_key_mode = "platform_bot_conversation"
        persona_switch_mode = str(
            _pick(
                data,
                "persona_switch_mode",
                "persona",
                "switch_mode",
                "finish_bound_turn",
            )
        ).strip()
        if persona_switch_mode not in _VALID_PERSONA_SWITCH_MODES:
            persona_switch_mode = "finish_bound_turn"
        retention = str(
            _pick(
                data,
                "memory_retention_mode",
                "memory",
                "retention_mode",
                "preserve",
            )
        ).strip()
        if retention not in _VALID_RETENTION_MODES:
            retention = "preserve"
        return cls(
            backend_kind=str(
                _pick(
                    data,
                    "backend_kind",
                    "backend",
                    "kind",
                    "astrbot_public_api",
                )
            ).strip()
            or "astrbot_public_api",
            backend_store_id=str(
                _pick(
                    data,
                    "backend_store_id",
                    "backend",
                    "store_id",
                    "memory_commands_ledger",
                )
            ).strip()
            or "memory_commands_ledger",
            allowed_scopes=valid_allowed,
            default_scope=default_scope,
            policy_id=str(
                _pick(data, "policy_id", "scope", "policy_id", "")
            ).strip(),
            session_key_mode=session_key_mode,
            session_persona_isolation=_bool(
                _pick(
                    data,
                    "session_persona_isolation",
                    "session",
                    "persona_isolation",
                    True,
                ),
                True,
            ),
            session_reset_confirm=_bool(
                _pick(
                    data,
                    "session_reset_confirm",
                    "session",
                    "reset_confirm",
                    True,
                ),
                True,
            ),
            memory_max_read_items=_int(
                _pick(
                    data,
                    "memory_max_read_items",
                    "memory",
                    "max_read_items",
                    data.get("max_results", 50),
                ),
                50,
                1,
                100,
            ),
            memory_max_write_chars=_int(
                _pick(
                    data,
                    "memory_max_write_chars",
                    "memory",
                    "max_write_chars",
                    2000,
                ),
                2000,
                1,
                10000,
            ),
            memory_clear_confirm=_bool(
                _pick(
                    data,
                    "memory_clear_confirm",
                    "memory",
                    "clear_confirm",
                    True,
                ),
                True,
            ),
            memory_retention_mode=retention,
            persona_switch_mode=persona_switch_mode,
            persona_admin_shared_edit=_bool(
                _pick(
                    data,
                    "persona_admin_shared_edit",
                    "persona",
                    "admin_shared_edit",
                    False,
                ),
                False,
            ),
            operations_idempotency_ttl_seconds=_int(
                _pick(
                    data,
                    "operations_idempotency_ttl_seconds",
                    "operations",
                    "idempotency_ttl_seconds",
                    86400,
                ),
                86400,
                60,
                604800,
            ),
            operations_confirmation_ttl_seconds=_int(
                _pick(
                    data,
                    "operations_confirmation_ttl_seconds",
                    "operations",
                    "confirmation_ttl_seconds",
                    300,
                ),
                300,
                1,
                600,
            ),
        )


_LEDGER_DATA_KEYS = frozenset(
    {
        "id",
        "key",
        "action",
        "count",
        "cleared",
        "revision",
        "conversation_id",
        "persona_id",
        "title",
        "message_count",
        "turn_id",
        "role",
        "sequence",
        "persona_ids",
        "removed_count",
        "scope_key",
    }
)


def _ledger_result(result: Mapping[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    data = data if isinstance(data, Mapping) else {}
    safe_data: dict[str, Any] = {}
    for key, value in data.items():
        if key in _LEDGER_DATA_KEYS:
            safe_data[str(key)] = value
    return make_result(
        str(result.get("status") or STATUS_INTERNAL_ERROR),
        data=safe_data,
        error_code=(
            str(result.get("error_code"))[:96]
            if result.get("error_code")
            else None
        ),
        retryable=bool(result.get("retryable", False)),
        side_effect_state=str(result.get("side_effect_state") or "none"),
        provider_operation_id=(
            str(result.get("provider_operation_id"))[:128]
            if result.get("provider_operation_id")
            else None
        ),
    )


def _audit_data(result: Mapping[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    return safe_audit_summary(data if isinstance(data, Mapping) else {})


class MemoryCommandsProvider:
    """The one fixed ABI-v1 provider for memory, sessions and personas."""

    def __init__(
        self,
        backend: Any,
        store: OperationStore | None = None,
        *,
        config: Mapping[str, Any] | ProviderConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        self.store = store if store is not None else OperationStore(":memory:", clock=clock)
        self.config = (
            config
            if isinstance(config, ProviderConfig)
            else ProviderConfig.from_mapping(config)
        )
        self._clock = clock
        self._locks = AsyncKeyedLocks()

    # ------------------------------------------------------------------
    # fixed public methods
    # ------------------------------------------------------------------
    async def memory_read(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("memory_read", request_context, args, self._memory_read)

    async def memory_search(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("memory_search", request_context, args, self._memory_search)

    async def memory_add(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("memory_add", request_context, args, self._memory_add)

    async def memory_preview_clear(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute(
            "memory_preview_clear",
            request_context,
            args,
            self._memory_preview_clear,
        )

    async def memory_confirm_clear(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute(
            "memory_confirm_clear",
            request_context,
            args,
            self._memory_confirm_clear,
        )

    async def session_open(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("session_open", request_context, args, self._session_open)

    async def session_read(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("session_read", request_context, args, self._session_read)

    async def session_append(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("session_append", request_context, args, self._session_append)

    async def session_reset(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("session_reset", request_context, args, self._session_reset)

    async def session_switch(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("session_switch", request_context, args, self._session_switch)

    async def persona_list(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("persona_list", request_context, args, self._persona_list)

    async def persona_bind(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("persona_bind", request_context, args, self._persona_bind)

    async def persona_switch(self, request_context: Any, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._execute("persona_switch", request_context, args, self._persona_switch)

    # ------------------------------------------------------------------
    # common execution
    # ------------------------------------------------------------------
    async def _execute(
        self,
        method: str,
        request_context: Any,
        args: Mapping[str, Any],
        handler: Callable[[RequestContext, dict[str, Any]], Any],
    ) -> dict[str, Any]:
        context, validation_error = validate_request_context(
            request_context,
            expected_method=method,
            now=float(self._clock()),
        )
        if validation_error is not None:
            return validation_error
        assert context is not None
        if not isinstance(args, Mapping):
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        if not self._backend_ready():
            return error_result(
                STATUS_NOT_READY,
                "provider_not_ready",
                retryable=True,
            )
        epoch_error = self._epoch_error(context, args)
        if epoch_error is not None:
            return epoch_error
        try:
            value = await handler(context, dict(args))
        except Exception:  # noqa: BLE001 - provider never leaks tracebacks/bodies
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        return normalize_result(value)

    def _backend_ready(self) -> bool:
        backend = self.backend
        if backend is None:
            return False
        if not hasattr(backend, "ready"):
            return True
        try:
            ready = getattr(backend, "ready")
            if callable(ready):
                ready = ready()
            return bool(ready)
        except Exception:  # noqa: BLE001 - broken readiness is fail-closed
            return False

    def _epoch_error(
        self,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        raw = args.get("owner_epoch", args.get("expected_owner_epoch"))
        if raw is None:
            return None
        try:
            expected = int(raw)
        except (TypeError, ValueError, OverflowError):
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        if expected != context.owner_epoch:
            return error_result(STATUS_REVISION_CONFLICT, "owner_epoch_conflict")
        return None

    def _default_memory_kind(self, context: RequestContext) -> str:
        if not context.is_private and context.group_id:
            return "group"
        return "user"

    def _authorize(
        self,
        context: RequestContext,
        args: Mapping[str, Any],
        *,
        default_kind: str,
        write: bool,
    ) -> tuple[ScopeRef | None, ScopeAuthorization | None, dict[str, Any] | None]:
        try:
            scope = resolve_requested_scope(context, args, default_kind=default_kind)
        except ScopeResolutionError as exc:
            status = (
                STATUS_PERMISSION_DENIED
                if exc.code
                in {
                    "private_group_forgery",
                    "scope_namespace_mismatch",
                }
                else STATUS_INVALID_ARGS
            )
            return None, None, error_result(status, exc.code)
        authorization = authorize_scope(
            context,
            scope,
            write=write,
            allowed_scopes=self.config.allowed_scopes,
            admin_shared_edit=self.config.persona_admin_shared_edit,
        )
        if not authorization.allowed:
            return (
                None,
                authorization,
                error_result(
                    authorization.status,
                    authorization.error_code or "permission_denied",
                ),
            )
        return scope, authorization, None

    async def _backend_call(
        self,
        method: str,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
        *,
        write: bool = False,
    ) -> dict[str, Any]:
        function = getattr(self.backend, method, None)
        if not callable(function):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            value = function(scope, context, dict(args))
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001 - write may have unknown side effect
            return error_result(
                STATUS_OUTCOME_UNKNOWN if write else STATUS_INTERNAL_ERROR,
                "backend_error",
                retryable=not write,
                side_effect_state="unknown" if write else "none",
            )
        return normalize_result(value)

    def _business_key(
        self,
        context: RequestContext,
        args: Mapping[str, Any],
        *,
        prefix: str,
    ) -> str:
        explicit = str(args.get("idempotency_key") or "").strip()
        if explicit:
            return explicit[:256]
        source = str(context.source_message_id or context.turn_id or "").strip()
        return f"{prefix}:{source}"[:256]

    def _payload_hash(
        self,
        operation: str,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
        *extra: Any,
    ) -> str:
        payload = {
            "operation": operation,
            "scope": scope.canonical_id,
            "persona": str(context.persona_id or ""),
            "args": {
                key: value
                for key, value in args.items()
                if key
                not in {
                    "idempotency_key",
                    "confirmation_token",
                    "owner_epoch",
                    "expected_owner_epoch",
                }
            },
            "extra": list(extra),
        }
        return canonical_hash(payload)

    def _begin_idempotent(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        payload_hash: str,
        scope: ScopeRef,
    ) -> dict[str, Any]:
        result = self.store.begin_idempotent(
            context,
            operation=operation,
            business_key=business_key,
            payload_hash=payload_hash,
            scope_key=scope.canonical_id,
        )
        if result.get("status") == "proceed":
            return result
        normalized = normalize_result(result)
        if result.get("idempotent_replay"):
            normalized["idempotent_replay"] = True
        return normalized

    def _finish_idempotent(
        self,
        context: RequestContext,
        *,
        operation: str,
        business_key: str,
        payload_hash: str,
        scope: ScopeRef,
        result: Mapping[str, Any],
    ) -> None:
        self.store.complete_idempotent(
            context,
            operation=operation,
            business_key=business_key,
            payload_hash=payload_hash,
            scope_key=scope.canonical_id,
            result=_ledger_result(result),
        )

    def _audit(
        self,
        context: RequestContext,
        *,
        operation: str,
        scope: ScopeRef,
        result: Mapping[str, Any],
        payload_hash: str | None = None,
    ) -> None:
        self.store.append_audit(
            context,
            operation=operation,
            scope_key=scope.canonical_id,
            status=str(result.get("status") or ""),
            error_code=result.get("error_code"),
            payload_hash=payload_hash,
            summary=_audit_data(result),
        )

    def _scope_memory_id(self, scope: ScopeRef) -> str:
        if scope.kind == "global":
            return "all"
        if scope.kind in {"user", "group"}:
            prefix = f"{scope.platform}:"
            return (
                scope.target_id
                if scope.target_id.startswith(prefix)
                else f"{prefix}{scope.target_id}"
            )
        return scope.target_id

    def _filter_records(
        self,
        scope: ScopeRef,
        records: Any,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not isinstance(records, (list, tuple)):
            return []
        expected_scope_id = self._scope_memory_id(scope)
        filtered: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            scope_type = record.get("scope_type")
            scope_id = record.get("scope_id")
            if scope_type is not None and str(scope_type) != scope.kind:
                continue
            if scope_id is not None and str(scope_id) != expected_scope_id:
                continue
            filtered.append(dict(record))
            if len(filtered) >= limit:
                break
        return filtered

    async def _memory_revision_result(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        function = getattr(self.backend, "get_memory_revision", None)
        if callable(function):
            try:
                value = function(scope, context, dict(args or {}))
                if inspect.isawaitable(value):
                    value = await value
            except Exception:  # noqa: BLE001 - fail closed, no fake revision
                return error_result(
                    STATUS_INTERNAL_ERROR,
                    "backend_error",
                    retryable=True,
                )
            return normalize_result(value)
        value = await self._backend_call(
            "memory_preview_clear",
            scope,
            context,
            dict(args or {}),
            write=False,
        )
        return value

    async def _session_revision_result(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        function = getattr(self.backend, "get_session_revision", None)
        if not callable(function):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            value = function(scope, context, dict(args or {}))
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001 - no fabricated revision
            return error_result(
                STATUS_INTERNAL_ERROR,
                "backend_error",
                retryable=True,
            )
        return normalize_result(value)

    async def _persona_revision_result(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        function = getattr(self.backend, "get_persona_revision", None)
        if not callable(function):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            value = function(scope, context, dict(args or {}))
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001 - no fabricated revision
            return error_result(
                STATUS_INTERNAL_ERROR,
                "backend_error",
                retryable=True,
            )
        return normalize_result(value)

    def _revision_from(self, result: Mapping[str, Any]) -> Any:
        data = result.get("data")
        return data.get("revision") if isinstance(data, Mapping) else None

    def _scope_from_token(self, token_record: Mapping[str, Any]) -> ScopeRef | None:
        scope = token_record.get("scope")
        return coerce_scope_ref(scope) if isinstance(scope, Mapping) else None

    def _token_owned_by_context(
        self,
        token_record: Mapping[str, Any],
        context: RequestContext,
    ) -> bool:
        return (
            str(token_record.get("instance_id") or "") == context.instance_id
            and str(token_record.get("platform") or "") == context.platform
            and str(token_record.get("bot_id") or "") == context.bot_id
            and str(token_record.get("actor_id") or "") == context.actor_id
        )

    def _confirmation_error_from_record(
        self,
        token_record: Mapping[str, Any] | None,
        context: RequestContext,
    ) -> dict[str, Any] | None:
        if token_record is None:
            return error_result(
                STATUS_CONFIRMATION_EXPIRED,
                "confirmation_not_found",
            )
        if not self._token_owned_by_context(token_record, context):
            return error_result(
                STATUS_PERMISSION_DENIED,
                "confirmation_actor_mismatch",
            )
        if float(token_record.get("expires_at") or 0) <= float(self._clock()):
            return error_result(
                STATUS_CONFIRMATION_EXPIRED,
                "confirmation_expired",
            )
        if str(token_record.get("status") or "") != "pending":
            return error_result(
                STATUS_CONFIRMATION_EXPIRED,
                "confirmation_replayed",
                side_effect_state="unknown",
            )
        return None

    async def _issue_confirmation(
        self,
        context: RequestContext,
        scope: ScopeRef,
        *,
        payload_hash: str,
        revision: Any,
        metadata: Mapping[str, Any] | None = None,
        status: str = STATUS_CONFIRMATION_REQUIRED,
    ) -> dict[str, Any]:
        created = self.store.create_confirmation(
            context,
            scope_key=scope.canonical_id,
            scope_view=scope.to_dict(),
            persona_id=str(context.persona_id or ""),
            payload_hash=payload_hash,
            revision=revision,
            ttl_seconds=self.config.operations_confirmation_ttl_seconds,
            metadata=metadata,
        )
        normalized = normalize_result(created)
        if not is_ok(normalized):
            return normalized
        data = dict(normalized.get("data") or {})
        data.update(
            {
                "scope": scope_public_view(scope),
                "requires_confirmation": True,
            }
        )
        return make_result(status, data=data)

    async def _consume_existing_confirmation(
        self,
        context: RequestContext,
        scope: ScopeRef,
        *,
        token: str,
        payload_hash: str,
        revision: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        record = self.store.get_confirmation(token)
        token_error = self._confirmation_error_from_record(record, context)
        if token_error is not None:
            return None, None, token_error
        assert record is not None
        record_scope = self._scope_from_token(record)
        if (
            record_scope is None
            or record_scope.canonical_id != scope.canonical_id
            or str(record.get("persona_id") or "") != str(context.persona_id or "")
        ):
            return (
                None,
                None,
                error_result(STATUS_PERMISSION_DENIED, "confirmation_scope_mismatch"),
            )
        consumed = self.store.consume_confirmation(
            token,
            context,
            scope_key=scope.canonical_id,
            persona_id=str(context.persona_id or ""),
            payload_hash=payload_hash,
            revision=revision,
        )
        normalized = normalize_result(consumed)
        if not is_ok(normalized):
            return None, None, normalized
        return record, normalized, None

    # ------------------------------------------------------------------
    # memory handlers
    # ------------------------------------------------------------------
    async def _memory_read(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        scope, _authorization, error = self._authorize(
            context,
            args,
            default_kind=self._default_memory_kind(context),
            write=False,
        )
        if error is not None:
            return error
        assert scope is not None
        result = await self._backend_call("memory_read", scope, context, args)
        if not is_ok(result):
            self._audit(context, operation="memory.read", scope=scope, result=result)
            return result
        data = dict(result.get("data") or {})
        records = self._filter_records(
            scope,
            data.get("records"),
            limit=self.config.memory_max_read_items,
        )
        data["records"] = records
        data["returned"] = len(records)
        data["scope"] = scope_public_view(scope)
        normalized = make_result(STATUS_OK, data=data)
        self._audit(context, operation="memory.read", scope=scope, result=normalized)
        return normalized

    async def _memory_search(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query or len(query) > 200:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        scope, _authorization, error = self._authorize(
            context,
            args,
            default_kind=self._default_memory_kind(context),
            write=False,
        )
        if error is not None:
            return error
        assert scope is not None
        backend_args = dict(args)
        backend_args["query"] = query
        backend_args["limit"] = self.config.memory_max_read_items
        result = await self._backend_call("memory_search", scope, context, backend_args)
        if not is_ok(result):
            self._audit(context, operation="memory.search", scope=scope, result=result)
            return result
        data = dict(result.get("data") or {})
        records = self._filter_records(
            scope,
            data.get("records"),
            limit=self.config.memory_max_read_items,
        )
        data["records"] = records
        data["returned"] = len(records)
        data["scope"] = scope_public_view(scope)
        normalized = make_result(STATUS_OK, data=data)
        self._audit(context, operation="memory.search", scope=scope, result=normalized)
        return normalized

    async def _memory_add(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        content = str(args.get("content") or "").strip()
        if not content:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        if len(content) > self.config.memory_max_write_chars:
            return error_result(STATUS_INVALID_ARGS, "content_too_long")
        key = str(args.get("key") or "").strip()
        if not key:
            key = "cmd-" + canonical_hash(
                {"scope": str(args.get("scope") or ""), "content": content}
            )[:16]
        if len(key) > 128 or any(not (char.isalnum() or char in "_.:-") for char in key):
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        args = dict(args)
        args["key"] = key
        args["content"] = content
        scope, authorization, error = self._authorize(
            context,
            args,
            default_kind=self._default_memory_kind(context),
            write=True,
        )
        if error is not None:
            return error
        assert scope is not None and authorization is not None
        payload_hash = self._payload_hash("memory_add", scope, context, args)
        token = str(args.get("confirmation_token") or "").strip()
        if authorization.shared:
            revision_result = await self._memory_revision_result(scope, context, args)
            if not is_ok(revision_result):
                return revision_result
            current_revision = self._revision_from(revision_result)
            if not token:
                return await self._issue_confirmation(
                    context,
                    scope,
                    payload_hash=payload_hash,
                    revision=current_revision,
                    metadata={"count": revision_result.get("data", {}).get("count", 0)},
                )
            _record, _consumed, token_error = await self._consume_existing_confirmation(
                context,
                scope,
                token=token,
                payload_hash=payload_hash,
                revision=current_revision,
            )
            if token_error is not None:
                return token_error
        elif token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        business_key = self._business_key(context, args, prefix="memory_add")
        begun = self._begin_idempotent(
            context,
            operation="memory_add",
            business_key=business_key,
            payload_hash=payload_hash,
            scope=scope,
        )
        if begun.get("status") != "proceed":
            return begun
        async with self._locks.lock(f"memory:{scope.canonical_id}"):
            result = await self._backend_call(
                "memory_add",
                scope,
                context,
                args,
                write=True,
            )
            result = _ledger_result(result)
            self._finish_idempotent(
                context,
                operation="memory_add",
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
                result=result,
            )
            self._audit(
                context,
                operation="memory.add",
                scope=scope,
                result=result,
                payload_hash=payload_hash,
            )
            return result

    async def _memory_preview_clear(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        scope, _authorization, error = self._authorize(
            context,
            args,
            default_kind=self._default_memory_kind(context),
            write=True,
        )
        if error is not None:
            return error
        assert scope is not None
        preview = await self._backend_call(
            "memory_preview_clear",
            scope,
            context,
            args,
            write=False,
        )
        if not is_ok(preview):
            self._audit(context, operation="memory.preview_clear", scope=scope, result=preview)
            return preview
        data = dict(preview.get("data") or {})
        revision = data.get("revision")
        item_ids = data.get("item_ids")
        if not isinstance(item_ids, (list, tuple)):
            item_ids = []
        payload_hash = canonical_hash(
            {
                "operation": "memory.clear",
                "scope": scope.canonical_id,
                "persona": context.persona_id,
                "revision": revision,
                "item_ids": sorted(str(item) for item in item_ids),
                "count": data.get("count", 0),
            }
        )
        issued = await self._issue_confirmation(
            context,
            scope,
            payload_hash=payload_hash,
            revision=revision,
            metadata={"count": data.get("count", 0)},
            status=STATUS_OK,
        )
        if not is_ok(issued):
            return issued
        issued_data = dict(issued.get("data") or {})
        issued_data.update(
            {
                "count": data.get("count", 0),
                "preview_count": data.get("count", 0),
                "revision": revision,
                "payload_hash": payload_hash,
                "dry_run": True,
            }
        )
        result = make_result(STATUS_OK, data=issued_data)
        self._audit(context, operation="memory.preview_clear", scope=scope, result=result)
        return result

    async def _memory_confirm_clear(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        token = str(args.get("confirmation_token") or args.get("token") or "").strip()
        if not token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        record = self.store.get_confirmation(token)
        token_error = self._confirmation_error_from_record(record, context)
        if token_error is not None:
            return token_error
        assert record is not None
        scope = self._scope_from_token(record)
        if scope is None:
            return error_result(STATUS_PERMISSION_DENIED, "confirmation_scope_mismatch")
        authorization = authorize_scope(
            context,
            scope,
            write=True,
            allowed_scopes=self.config.allowed_scopes,
            admin_shared_edit=self.config.persona_admin_shared_edit,
        )
        if not authorization.allowed:
            return error_result(
                authorization.status,
                authorization.error_code or "permission_denied",
            )
        business_key = self._business_key(
            context,
            args,
            prefix=f"memory_confirm_clear:{token}",
        )
        begun = self._begin_idempotent(
            context,
            operation="memory_confirm_clear",
            business_key=business_key,
            payload_hash=canonical_hash({"token": token}),
            scope=scope,
        )
        if begun.get("status") != "proceed":
            return begun
        async with self._locks.lock(f"memory:{scope.canonical_id}"):
            preview = await self._backend_call(
                "memory_preview_clear",
                scope,
                context,
                {},
                write=False,
            )
            if not is_ok(preview):
                self._finish_idempotent(
                    context,
                    operation="memory_confirm_clear",
                    business_key=business_key,
                    payload_hash=canonical_hash({"token": token}),
                    scope=scope,
                    result=preview,
                )
                return preview
            preview_data = dict(preview.get("data") or {})
            current_revision = preview_data.get("revision")
            if current_revision != record.get("revision"):
                conflict = error_result(
                    STATUS_REVISION_CONFLICT,
                    "stale_revision",
                    data={
                        "expected_revision": record.get("revision"),
                        "current_revision": current_revision,
                    },
                )
                self._finish_idempotent(
                    context,
                    operation="memory_confirm_clear",
                    business_key=business_key,
                    payload_hash=canonical_hash({"token": token}),
                    scope=scope,
                    result=conflict,
                )
                self._audit(
                    context,
                    operation="memory.confirm_clear",
                    scope=scope,
                    result=conflict,
                )
                return conflict
            item_ids = preview_data.get("item_ids")
            if not isinstance(item_ids, (list, tuple)):
                item_ids = []
            current_payload_hash = canonical_hash(
                {
                    "operation": "memory.clear",
                    "scope": scope.canonical_id,
                    "persona": context.persona_id,
                    "revision": current_revision,
                    "item_ids": sorted(str(item) for item in item_ids),
                    "count": preview_data.get("count", 0),
                }
            )
            _record, _consumed, consume_error = await self._consume_existing_confirmation(
                context,
                scope,
                token=token,
                payload_hash=current_payload_hash,
                revision=current_revision,
            )
            if consume_error is not None:
                self._finish_idempotent(
                    context,
                    operation="memory_confirm_clear",
                    business_key=business_key,
                    payload_hash=canonical_hash({"token": token}),
                    scope=scope,
                    result=consume_error,
                )
                return consume_error
            clear_args = {
                "expected_revision": current_revision,
                "payload_hash": current_payload_hash,
            }
            result = await self._backend_call(
                "memory_confirm_clear",
                scope,
                context,
                clear_args,
                write=True,
            )
            result = _ledger_result(result)
            self._finish_idempotent(
                context,
                operation="memory_confirm_clear",
                business_key=business_key,
                payload_hash=canonical_hash({"token": token}),
                scope=scope,
                result=result,
            )
            self._audit(
                context,
                operation="memory.confirm_clear",
                scope=scope,
                result=result,
            )
            return result

    # ------------------------------------------------------------------
    # session handlers
    # ------------------------------------------------------------------
    def _conversation_scope(
        self,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> ScopeRef | None:
        # The stable conversation scope is the current chat namespace (UMO);
        # a manager-issued conversation id is a target argument inside it.
        target = str(context.conversation_id or "").strip()
        if not target:
            return None
        return ScopeRef(
            kind="conversation",
            target_id=target,
            instance_id=context.instance_id,
            platform=context.platform,
            bot_id=context.bot_id,
        )

    def _authorize_conversation_operation(
        self,
        context: RequestContext,
        actual_scope: ScopeRef,
        *,
        write: bool,
    ) -> dict[str, Any] | None:
        """Authorize the current chat namespace, then let the public manager
        verify that a selected conversation id belongs to that chat.

        Conversation ids are manager-issued and never interpreted as paths or
        SQL.  Memory scopes remain strict and do not use this relaxation.
        """

        auth_scope = ScopeRef(
            kind="conversation",
            target_id=str(context.conversation_id),
            instance_id=context.instance_id,
            platform=context.platform,
            bot_id=context.bot_id,
        )
        authorization = authorize_scope(
            context,
            auth_scope,
            write=write,
            allowed_scopes=self.config.allowed_scopes,
            admin_shared_edit=self.config.persona_admin_shared_edit,
        )
        if not authorization.allowed:
            return error_result(
                authorization.status,
                authorization.error_code or "permission_denied",
            )
        if actual_scope.kind != "conversation":
            return error_result(STATUS_INVALID_ARGS, "invalid_scope")
        return None

    async def _session_open(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        scope = self._conversation_scope(context, args)
        if scope is None:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        auth_error = self._authorize_conversation_operation(context, scope, write=True)
        if auth_error is not None:
            return auth_error
        result = await self._backend_call("session_open", scope, context, args, write=True)
        result = _ledger_result(result)
        self._audit(context, operation="session.open", scope=scope, result=result)
        return result

    async def _session_read(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        scope = self._conversation_scope(context, args)
        if scope is None:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        auth_error = self._authorize_conversation_operation(context, scope, write=False)
        if auth_error is not None:
            return auth_error
        result = await self._backend_call("session_read", scope, context, args)
        if not is_ok(result):
            self._audit(context, operation="session.read", scope=scope, result=result)
            return result
        data = dict(result.get("data") or {})
        messages = data.get("messages")
        if isinstance(messages, (list, tuple)):
            data["messages"] = list(messages)[-self.config.memory_max_read_items :]
            data["returned"] = len(data["messages"])
        data["incomplete_turns"] = self.store.incomplete_turns(
            context,
            scope_key=scope.canonical_id,
            conversation_id=str(scope.target_id),
            persona_id=str(context.persona_id or ""),
        )[-self.config.memory_max_read_items :]
        normalized = make_result(STATUS_OK, data=data)
        self._audit(context, operation="session.read", scope=scope, result=normalized)
        return normalized

    async def _session_append(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        scope = self._conversation_scope(context, args)
        if scope is None:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        auth_error = self._authorize_conversation_operation(context, scope, write=True)
        if auth_error is not None:
            return auth_error
        role = str(args.get("role") or "").strip()
        content = str(args.get("content") or "")
        try:
            sequence = int(args.get("sequence"))
        except (TypeError, ValueError, OverflowError):
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        turn_id = str(args.get("turn_id") or context.turn_id or "").strip()
        if (
            role not in {"system", "user", "assistant", "tool"}
            or not content
            or len(content) > self.config.memory_max_write_chars
            or sequence < 0
            or turn_id != context.turn_id
        ):
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        business_key = f"append:{turn_id}:{role}:{sequence}"
        payload_hash = self._payload_hash(
            "session_append",
            scope,
            context,
            args,
            turn_id,
            role,
            sequence,
        )
        async with self._locks.lock(f"conversation:{scope.canonical_id}"):
            begun = self.store.begin_append(
                context,
                operation="session_append",
                business_key=business_key,
                payload_hash=payload_hash,
                scope_key=scope.canonical_id,
                conversation_id=str(scope.target_id),
                persona_id=str(context.persona_id or ""),
                turn_id=turn_id,
                role=role,
                sequence=sequence,
            )
            if begun.get("status") != "proceed":
                normalized = normalize_result(begun)
                if begun.get("idempotent_replay"):
                    normalized["idempotent_replay"] = True
                return normalized
            result = await self._backend_call(
                "session_append",
                scope,
                context,
                args,
                write=True,
            )
            result = _ledger_result(result)
            self.store.complete_append(
                context,
                operation="session_append",
                business_key=business_key,
                payload_hash=payload_hash,
                scope_key=scope.canonical_id,
                conversation_id=str(scope.target_id),
                persona_id=str(context.persona_id or ""),
                turn_id=turn_id,
                role=role,
                sequence=sequence,
                result=result,
            )
            self._audit(
                context,
                operation="session.append",
                scope=scope,
                result=result,
                payload_hash=payload_hash,
            )
            return result

    async def _session_reset(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        scope = self._conversation_scope(context, args)
        if scope is None:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        auth_error = self._authorize_conversation_operation(context, scope, write=True)
        if auth_error is not None:
            return auth_error
        token = str(args.get("confirmation_token") or args.get("token") or "").strip()
        revision_result = await self._session_revision_result(scope, context, args)
        if not is_ok(revision_result):
            return revision_result
        current_revision = self._revision_from(revision_result)
        payload_hash = self._payload_hash("session_reset", scope, context, args)
        if self.config.session_reset_confirm:
            if not token:
                return await self._issue_confirmation(
                    context,
                    scope,
                    payload_hash=payload_hash,
                    revision=current_revision,
                    metadata={"conversation_id": scope.target_id},
                )
            _record, _consumed, token_error = await self._consume_existing_confirmation(
                context,
                scope,
                token=token,
                payload_hash=payload_hash,
                revision=current_revision,
            )
            if token_error is not None:
                return token_error
        elif token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        business_key = self._business_key(context, args, prefix="session_reset")
        async with self._locks.lock(f"conversation:{scope.canonical_id}"):
            begun = self._begin_idempotent(
                context,
                operation="session_reset",
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
            )
            if begun.get("status") != "proceed":
                return begun
            backend_args = dict(args)
            backend_args["expected_revision"] = current_revision
            result = await self._backend_call(
                "session_reset",
                scope,
                context,
                backend_args,
                write=True,
            )
            result = _ledger_result(result)
            self._finish_idempotent(
                context,
                operation="session_reset",
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
                result=result,
            )
            self._audit(
                context,
                operation="session.reset",
                scope=scope,
                result=result,
                payload_hash=payload_hash,
            )
            return result

    async def _session_switch(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        target = str(
            args.get("conversation_id")
            or args.get("cid")
            or args.get("target_id")
            or ""
        ).strip()
        if not target:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        scope = self._conversation_scope(context, args)
        if scope is None:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        auth_error = self._authorize_conversation_operation(context, scope, write=True)
        if auth_error is not None:
            return auth_error
        revision_result = await self._session_revision_result(scope, context, args)
        if not is_ok(revision_result):
            return revision_result
        current_revision = self._revision_from(revision_result)
        expected_revision = str(
            args.get("expected_revision") or current_revision or ""
        )
        payload_hash = self._payload_hash(
            "session_switch",
            scope,
            context,
            args,
            expected_revision,
        )
        business_key = self._business_key(context, args, prefix="session_switch")
        async with self._locks.lock(f"conversation:{scope.canonical_id}"):
            begun = self._begin_idempotent(
                context,
                operation="session_switch",
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
            )
            if begun.get("status") != "proceed":
                return begun
            backend_args = dict(args)
            backend_args["conversation_id"] = target
            backend_args["expected_revision"] = expected_revision
            result = await self._backend_call(
                "session_switch",
                scope,
                context,
                backend_args,
                write=True,
            )
            result = _ledger_result(result)
            self._finish_idempotent(
                context,
                operation="session_switch",
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
                result=result,
            )
            self._audit(
                context,
                operation="session.switch",
                scope=scope,
                result=result,
                payload_hash=payload_hash,
            )
            return result

    # ------------------------------------------------------------------
    # persona handlers
    # ------------------------------------------------------------------
    async def _persona_list(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        target = str(
            args.get("persona_id")
            or args.get("target_id")
            or context.persona_id
            or ""
        ).strip()
        if not target:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        scope = ScopeRef(
            kind="persona",
            target_id=target,
            instance_id=context.instance_id,
            platform=context.platform,
            bot_id=context.bot_id,
        )
        authorization = authorize_scope(
            context,
            scope,
            write=False,
            allowed_scopes=self.config.allowed_scopes,
            admin_shared_edit=self.config.persona_admin_shared_edit,
        )
        if not authorization.allowed:
            return error_result(
                authorization.status,
                authorization.error_code or "permission_denied",
            )
        result = await self._backend_call(
            "persona_list",
            scope,
            context,
            args,
            write=False,
        )
        if not is_ok(result):
            self._audit(context, operation="persona.list", scope=scope, result=result)
            return result
        data = dict(result.get("data") or {})
        personas = data.get("personas")
        if not isinstance(personas, (list, tuple)):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        allowed: set[str] = set()
        if context.persona_id:
            allowed.add(context.persona_id)
        if context.allow_shared_persona:
            allowed.update(context.allowed_persona_ids)
        filtered = [
            dict(persona)
            for persona in personas
            if isinstance(persona, Mapping)
            and str(persona.get("persona_id") or "") in allowed
        ]
        if not filtered and not context.allow_shared_persona:
            filtered = [
                dict(persona)
                for persona in personas
                if isinstance(persona, Mapping)
                and str(persona.get("persona_id") or "") == context.persona_id
            ]
        data["personas"] = filtered
        data["count"] = len(filtered)
        data["scope"] = scope_public_view(scope)
        normalized = make_result(STATUS_OK, data=data)
        self._audit(context, operation="persona.list", scope=scope, result=normalized)
        return normalized

    async def _persona_change(
        self,
        context: RequestContext,
        args: dict[str, Any],
        *,
        backend_method: str,
    ) -> dict[str, Any]:
        target = str(
            args.get("persona_id") or args.get("target_id") or context.persona_id or ""
        ).strip()
        if not target:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        scope = ScopeRef(
            kind="persona",
            target_id=target,
            instance_id=context.instance_id,
            platform=context.platform,
            bot_id=context.bot_id,
        )
        authorization = authorize_scope(
            context,
            scope,
            write=True,
            allowed_scopes=self.config.allowed_scopes,
            admin_shared_edit=self.config.persona_admin_shared_edit,
        )
        if not authorization.allowed:
            return error_result(
                authorization.status,
                authorization.error_code or "permission_denied",
            )
        payload_hash = self._payload_hash(backend_method, scope, context, args)
        token = str(args.get("confirmation_token") or args.get("token") or "").strip()
        revision_result = await self._persona_revision_result(scope, context, args)
        if not is_ok(revision_result):
            return revision_result
        current_revision = self._revision_from(revision_result)
        if authorization.shared:
            if not token:
                return await self._issue_confirmation(
                    context,
                    scope,
                    payload_hash=payload_hash,
                    revision=current_revision,
                    metadata={"persona_id": target},
                )
            _record, _consumed, token_error = await self._consume_existing_confirmation(
                context,
                scope,
                token=token,
                payload_hash=payload_hash,
                revision=current_revision,
            )
            if token_error is not None:
                return token_error
        elif token:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        business_key = self._business_key(context, args, prefix=backend_method)
        async with self._locks.lock(f"conversation:{context.conversation_id}"):
            begun = self._begin_idempotent(
                context,
                operation=backend_method,
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
            )
            if begun.get("status") != "proceed":
                return begun
            backend_args = dict(args)
            backend_args["persona_id"] = target
            backend_args["expected_revision"] = current_revision
            result = await self._backend_call(
                backend_method,
                scope,
                context,
                backend_args,
                write=True,
            )
            result = _ledger_result(result)
            self._finish_idempotent(
                context,
                operation=backend_method,
                business_key=business_key,
                payload_hash=payload_hash,
                scope=scope,
                result=result,
            )
            self._audit(
                context,
                operation=backend_method.replace("_", "."),
                scope=scope,
                result=result,
                payload_hash=payload_hash,
            )
            return result

    async def _persona_bind(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._persona_change(context, args, backend_method="persona_bind")

    async def _persona_switch(
        self,
        context: RequestContext,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._persona_change(context, args, backend_method="persona_switch")


def cancel_pending_confirmation(
    store: OperationStore,
    context: RequestContext,
    token: str,
) -> dict[str, Any]:
    """Command-layer cancel; no provider fixed method is invented for it."""

    result = store.cancel_confirmation(token, context)
    normalized = normalize_result(result)
    summary = None
    try:
        record = store.get_confirmation(token)
    except Exception:  # noqa: BLE001 - audit must never turn cancel into failure
        record = None
    if isinstance(record, Mapping):
        summary = {
            "scope_key": str(record.get("scope_key") or ""),
            "status": normalized.get("status"),
        }
    try:
        store.append_audit(
            context,
            operation="confirmation.cancel",
            scope_key=str(record.get("scope_key") or "") if record else "",
            status=str(normalized.get("status") or ""),
            error_code=normalized.get("error_code"),
            summary=summary,
        )
    except Exception:  # noqa: BLE001 - cancel result remains authoritative
        pass
    return normalized
