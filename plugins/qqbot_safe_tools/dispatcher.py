"""Fixed-method ABI dispatcher with bounded parallelism and deadlines.

There is no reflection and no caller-controlled method lookup anywhere in this
module: a ToolCall names one operation from the static 4.4 table, the registry
returns an explicitly registered callable for that exact operation, and the
dispatcher invokes it with a RequestContext.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .auth import AuthorizationError, Authorizer
from .call_ledger import LedgerConflict, LedgerError
from .confirmation import ConfirmationError, ConfirmationManager
from .contracts import (
    ContractError,
    RequestContext,
    ToolCall,
    ToolResult,
    operation_spec,
)
from .registry import ProviderBinding, RegistryError, ServiceRegistry


class DispatcherError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class DispatchLimits:
    deadline_seconds: float = 30.0
    max_parallel: int = 4
    max_rounds: int = 3

    def validate(self) -> bool:
        if isinstance(self.deadline_seconds, bool) or not isinstance(
            self.deadline_seconds, (int, float)
        ):
            raise DispatcherError("invalid_args", "deadline_seconds must be numeric")
        if not 0 < float(self.deadline_seconds) <= 600:
            raise DispatcherError("invalid_args", "deadline_seconds is out of range")
        if isinstance(self.max_parallel, bool) or not isinstance(self.max_parallel, int):
            raise DispatcherError("invalid_args", "max_parallel must be an integer")
        if not 1 <= self.max_parallel <= 64:
            raise DispatcherError("invalid_args", "max_parallel is out of range")
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int):
            raise DispatcherError("invalid_args", "max_rounds must be an integer")
        if not 1 <= self.max_rounds <= 64:
            raise DispatcherError("invalid_args", "max_rounds is out of range")
        return True


class Dispatcher:
    """Invoke only statically registered fixed provider methods."""

    def __init__(
        self,
        registry: ServiceRegistry,
        authorizer: Authorizer,
        *,
        limits: DispatchLimits | None = None,
        ledger: Any = None,
        confirmation_manager: ConfirmationManager | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.registry = registry
        self.authorizer = authorizer
        self.limits = limits or DispatchLimits()
        self.limits.validate()
        self.ledger = ledger
        self.confirmation_manager = confirmation_manager
        self.clock = clock
        self._semaphore = asyncio.Semaphore(self.limits.max_parallel)
        self._round_lock = threading.RLock()
        self._rounds_seen: dict[str, set[str]] = {}
        self._next_round: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------
    def reserve_round(
        self, request_context: RequestContext, round_id: str | int | None = None
    ) -> str:
        request_context.validate()
        turn_key = f"{request_context.instance_id}:{request_context.turn_id}"
        with self._round_lock:
            seen = self._rounds_seen.setdefault(turn_key, set())
            if round_id is None:
                sequence = self._next_round.get(turn_key, 0) + 1
                self._next_round[turn_key] = sequence
                value = str(sequence)
            else:
                value = str(round_id)
                if len(value) > 64:
                    raise DispatcherError("invalid_args", "round_id is too long")
            if value not in seen:
                if len(seen) >= self.limits.max_rounds:
                    raise DispatcherError(
                        "round_limit_exceeded", "tool round limit exceeded"
                    )
                seen.add(value)
            return value

    def reset_turn(self, request_context: RequestContext) -> None:
        key = f"{request_context.instance_id}:{request_context.turn_id}"
        with self._round_lock:
            self._rounds_seen.pop(key, None)
            self._next_round.pop(key, None)

    # ------------------------------------------------------------------
    # Result helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _result_from_mapping(call_id: str, payload: Mapping[str, Any]) -> ToolResult:
        status = str(payload.get("status", "success"))
        if status == "ok":
            status = "success"
        result = ToolResult(
            status=status,
            call_id=str(payload.get("call_id", call_id)),
            result_type=str(payload.get("result_type", "data")),
            data=payload.get("data"),
            error_code=payload.get("error_code"),
            retryable=bool(payload.get("retryable", False)),
            side_effect_state=str(payload.get("side_effect_state", "none")),
            provider_operation_id=str(payload.get("provider_operation_id", "") or ""),
        )
        result.validate()
        return result

    def _result_from_ledger(self, call_id: str, record: Mapping[str, Any] | None) -> ToolResult:
        if not record:
            return ToolResult.failure(call_id, "outcome_unknown", status="outcome_unknown", side_effect_state="unknown")
        full = record.get("result")
        if isinstance(full, Mapping):
            return self._result_from_mapping(call_id, full)
        status = str(record.get("status", "unknown"))
        if status in {"pending"}:
            return ToolResult(
                status="pending",
                call_id=call_id,
                result_type="pending",
                data=None,
                error_code=None,
                retryable=False,
                side_effect_state=str(record.get("side_effect_state", "none")),
                provider_operation_id=str(record.get("provider_operation_id", "") or ""),
            )
        if status in {"outcome_unknown", "unknown"}:
            return ToolResult.failure(
                call_id,
                str(record.get("error_code") or "outcome_unknown"),
                status="outcome_unknown",
                side_effect_state=str(record.get("side_effect_state", "unknown")),
            )
        if status in {"success", "ok"}:
            return ToolResult.success(call_id)
        return ToolResult.failure(
            call_id,
            str(record.get("error_code") or "internal_error"),
            side_effect_state=str(record.get("side_effect_state", "none")),
        )

    @staticmethod
    def _normalise_provider_result(call_id: str, value: Any) -> ToolResult:
        if isinstance(value, ToolResult):
            if value.call_id and value.call_id != call_id:
                raise DispatcherError("invalid_args", "provider returned wrong call_id")
            value.call_id = call_id
            value.validate()
            return value
        if isinstance(value, Mapping):
            if "status" in value:
                result = Dispatcher._result_from_mapping(call_id, value)
                if result.call_id != call_id:
                    raise DispatcherError("invalid_args", "provider returned wrong call_id")
                return result
            return ToolResult.success(call_id, dict(value))
        if isinstance(value, str):
            return ToolResult.success(call_id, value, result_type="text")
        if value is None:
            return ToolResult(
                status="outcome_unknown",
                call_id=call_id,
                result_type="unknown",
                data=None,
                error_code="outcome_unknown",
                retryable=False,
                side_effect_state="unknown",
            )
        # ReplyPlan and other contract objects are JSON-renderable only when
        # they explicitly provide to_dict(); no arbitrary object __str__ is sent.
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            return ToolResult.success(
                call_id, data, result_type=value.__class__.__name__
            )
        raise DispatcherError("invalid_args", "provider returned unsupported result")

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def dispatch(
        self,
        call: ToolCall,
        *,
        round_id: str | int | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        try:
            call.validate()
        except ContractError as exc:
            return ToolResult.failure(call.call_id, exc.code)

        now = float(self.clock())
        if now >= float(call.request_context.deadline):
            return ToolResult.failure(
                call.call_id, "deadline_exceeded", retryable=False
            )
        try:
            self.reserve_round(call.request_context, round_id)
        except DispatcherError as exc:
            return ToolResult.failure(call.call_id, exc.code)

        spec = operation_spec(call.tool_id)
        try:
            self.authorizer.authorize(call.request_context, call.tool_id)
        except AuthorizationError as exc:
            return ToolResult.failure(
                call.call_id, "permission_denied", status="denied"
            )

        if spec.requires_confirmation:
            if not call.confirmation_ref:
                return ToolResult.failure(call.call_id, "confirmation_required")
            if self.confirmation_manager is None:
                return ToolResult.failure(call.call_id, "dependency_missing")
            try:
                revision = call.canonical_args.get("revision")
                self.confirmation_manager.verify_reference(
                    call.confirmation_ref,
                    call.request_context,
                    action=call.tool_id,
                    expected_payload=call.canonical_args,
                    expected_revision=(
                        int(revision) if isinstance(revision, int) else None
                    ),
                    require_consumed=True,
                    now=now,
                )
            except ConfirmationError as exc:
                return ToolResult.failure(call.call_id, exc.code)
            except Exception:
                return ToolResult.failure(call.call_id, "confirmation_invalid")

        dedupe_key = idempotency_key
        if dedupe_key and self.ledger is not None:
            try:
                decision = self.ledger.begin_call(
                    call.request_context,
                    call.tool_id,
                    dedupe_key,
                    call.canonical_args,
                    call_id=call.call_id,
                )
            except LedgerConflict:
                return ToolResult.failure(call.call_id, "idempotency_conflict")
            except LedgerError as exc:
                return ToolResult.failure(call.call_id, exc.code)
            if decision.replayed:
                replay_call_id = decision.call_id
                if decision.result is not None:
                    try:
                        return self._result_from_mapping(
                            replay_call_id, decision.result
                        )
                    except ContractError:
                        pass
                return self._result_from_ledger(
                    replay_call_id, self.ledger.get_call(replay_call_id)
                )

        try:
            binding = self.registry.resolve_operation(
                call.request_context, call.tool_id
            )
            method = binding.get_method(call.tool_id)
        except RegistryError as exc:
            result = ToolResult.failure(call.call_id, exc.code)
            if dedupe_key and self.ledger is not None:
                self.ledger.complete_call(call.call_id, result)
            return result
        except Exception:
            result = ToolResult.failure(call.call_id, "dependency_missing")
            if dedupe_key and self.ledger is not None:
                self.ledger.complete_call(call.call_id, result)
            return result

        timeout = max(
            0.0,
            min(
                float(self.limits.deadline_seconds),
                float(call.request_context.deadline) - float(self.clock()),
            ),
        )
        if timeout <= 0:
            result = ToolResult.failure(call.call_id, "deadline_exceeded")
            if dedupe_key and self.ledger is not None:
                self.ledger.complete_call(call.call_id, result)
            return result

        async def invoke() -> Any:
            async with self._semaphore:
                value = method(call.request_context, dict(call.canonical_args))
                if inspect.isawaitable(value):
                    return await value
                return value

        try:
            value = await asyncio.wait_for(invoke(), timeout=timeout)
            result = self._normalise_provider_result(call.call_id, value)
        except asyncio.TimeoutError:
            result = ToolResult(
                status="outcome_unknown",
                call_id=call.call_id,
                result_type="unknown",
                data=None,
                error_code="outcome_unknown",
                retryable=False,
                side_effect_state="unknown",
            )
        except asyncio.CancelledError:
            raise
        except ContractError as exc:
            result = ToolResult.failure(call.call_id, exc.code)
        except DispatcherError as exc:
            result = ToolResult.failure(call.call_id, exc.code)
        except Exception:
            result = ToolResult(
                status="outcome_unknown",
                call_id=call.call_id,
                result_type="unknown",
                data=None,
                error_code="outcome_unknown",
                retryable=False,
                side_effect_state="unknown",
            )

        if dedupe_key and self.ledger is not None:
            try:
                self.ledger.complete_call(call.call_id, result)
            except LedgerError as exc:
                return ToolResult.failure(call.call_id, exc.code)
        return result

    async def dispatch_operation(
        self,
        request_context: RequestContext,
        operation: str,
        canonical_args: Mapping[str, Any] | None = None,
        *,
        confirmation_ref: str = "",
        call_id: str | None = None,
        idempotency_key: str | None = None,
        round_id: str | int | None = None,
    ) -> ToolResult:
        call = ToolCall.create(
            request_context,
            operation,
            canonical_args or {},
            call_id=call_id,
            confirmation_ref=confirmation_ref,
        )
        return await self.dispatch(
            call, round_id=round_id, idempotency_key=idempotency_key
        )
