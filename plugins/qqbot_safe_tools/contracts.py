"""Pure ABI v1 DTOs, fixed provider operations and validation.

No I/O, registry access, provider calls or model calls live in this module.
All identifiers that affect authorization are supplied by the authenticated
runtime/control plane in :class:`RequestContext`; marker/model text cannot
create or replace them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping

ABI_VERSION = 1
CONFIG_VERSION = 1
PROTOCOL_VERSION = 1

SCOPE_TYPES = ("global", "persona", "conversation", "group", "user")

ERROR_CODES = frozenset(
    {
        "permission_denied",
        "invalid_args",
        "not_ready",
        "revision_conflict",
        "confirmation_expired",
        "confirmation_required",
        "confirmation_invalid",
        "confirmation_replayed",
        "outcome_unknown",
        "dependency_missing",
        "owner_epoch_stale",
        "duplicate_provider",
        "abi_mismatch",
        "idempotency_conflict",
        "deadline_exceeded",
        "round_limit_exceeded",
        "not_found",
        "invalid_state",
        "internal_error",
    }
)
RESULT_STATUSES = frozenset(
    {
        "success",
        "ok",
        "error",
        "pending",
        "denied",
        "not_ready",
        "outcome_unknown",
        "unknown",
    }
)
SIDE_EFFECT_STATES = frozenset(
    {"none", "not_applied", "applied", "unknown"}
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")
_SAFE_OP_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """Stable contract validation failure."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code if code in ERROR_CODES else "invalid_args"
        self.message = str(message or self.code)
        super().__init__(self.message)


def _require_text(value: Any, name: str, *, max_chars: int = 256) -> str:
    if not isinstance(value, str):
        raise ContractError("invalid_args", f"{name} must be a string")
    text = value.strip()
    if not text or len(text) > max_chars:
        raise ContractError("invalid_args", f"{name} has invalid length")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ContractError("invalid_args", f"{name} has control characters")
    return text


def _require_safe_id(value: Any, name: str) -> str:
    text = _require_text(value, name)
    if not _SAFE_ID_RE.fullmatch(text):
        raise ContractError("invalid_args", f"{name} contains unsafe characters")
    return text


def _require_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("invalid_args", f"{name} must be an integer")
    if value < minimum:
        raise ContractError("invalid_args", f"{name} is below minimum")
    return value


def _require_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("invalid_args", f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ContractError("invalid_args", f"{name} must be finite and positive")
    return number


def canonical_json(value: Any) -> str:
    """Canonical JSON for hashes and ledger idempotency."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_args", "value is not canonical JSON") from exc


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def new_call_id() -> str:
    return "call_" + secrets.token_hex(16)


@dataclass(slots=True)
class RequestContext:
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
    requested_scope: str
    owner_epoch: int
    deadline: float

    def validate(self) -> bool:
        if self.protocol_version != ABI_VERSION:
            raise ContractError("abi_mismatch", "unsupported protocol_version")
        _require_safe_id(self.instance_id, "instance_id")
        _require_safe_id(self.platform, "platform")
        _require_safe_id(self.bot_id, "bot_id")
        _require_safe_id(self.actor_id, "actor_id")
        _require_safe_id(self.conversation_id, "conversation_id")
        _require_safe_id(self.persona_id, "persona_id")
        _require_safe_id(self.source_message_id, "source_message_id")
        _require_safe_id(self.turn_id, "turn_id")
        _require_safe_id(self.capability, "capability")
        if self.requested_scope not in SCOPE_TYPES:
            raise ContractError("invalid_args", "requested_scope is not supported")
        _require_int(self.owner_epoch, "owner_epoch", minimum=0)
        _require_number(self.deadline, "deadline")
        return True

    def to_dict(self) -> dict[str, Any]:
        self.validate()
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
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RequestContext":
        if not isinstance(value, Mapping):
            raise ContractError("invalid_args", "request context must be a mapping")
        required = (
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
        missing = [key for key in required if key not in value]
        if missing:
            raise ContractError("invalid_args", "missing request context fields")
        context = cls(**{key: value[key] for key in required})
        context.validate()
        return context


@dataclass(slots=True)
class ToolCall:
    tool_id: str
    call_id: str
    canonical_args: dict[str, Any]
    args_hash: str
    request_context: RequestContext
    confirmation_ref: str = ""

    def validate(self) -> bool:
        _require_safe_id(self.tool_id, "tool_id")
        if not _SAFE_OP_RE.fullmatch(self.tool_id) or not _known_operation(self.tool_id):
            raise ContractError("invalid_args", "unknown fixed tool_id")
        if not isinstance(self.call_id, str) or not _CALL_ID_RE.fullmatch(self.call_id):
            raise ContractError("invalid_args", "call_id is invalid")
        if not isinstance(self.canonical_args, dict):
            raise ContractError("invalid_args", "canonical_args must be an object")
        canonical_json(self.canonical_args)
        if not isinstance(self.args_hash, str) or not _HASH_RE.fullmatch(self.args_hash):
            raise ContractError("invalid_args", "args_hash is invalid")
        if self.args_hash != payload_hash(self.canonical_args):
            raise ContractError("invalid_args", "args_hash does not match arguments")
        if not isinstance(self.request_context, RequestContext):
            raise ContractError("invalid_args", "request_context is required")
        self.request_context.validate()
        spec = PROVIDER_OPERATION_SPECS[self.tool_id]
        if self.confirmation_ref:
            _require_text(self.confirmation_ref, "confirmation_ref", max_chars=4096)
        elif spec.requires_confirmation:
            raise ContractError(
                "confirmation_required", "operation requires confirmation"
            )
        return True

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "tool_id": self.tool_id,
            "call_id": self.call_id,
            "canonical_args": self.canonical_args,
            "args_hash": self.args_hash,
            "request_context": self.request_context.to_dict(),
            "confirmation_ref": self.confirmation_ref,
        }

    @classmethod
    def create(
        cls,
        request_context: RequestContext,
        tool_id: str,
        canonical_args: Mapping[str, Any] | None = None,
        *,
        call_id: str | None = None,
        confirmation_ref: str = "",
    ) -> "ToolCall":
        args = dict(canonical_args or {})
        call = cls(
            tool_id=str(tool_id),
            call_id=call_id or new_call_id(),
            canonical_args=args,
            args_hash=payload_hash(args),
            request_context=request_context,
            confirmation_ref=confirmation_ref,
        )
        call.validate()
        return call


@dataclass(slots=True)
class ToolResult:
    status: str
    call_id: str
    result_type: str = "data"
    data: Any = None
    error_code: str | None = None
    retryable: bool = False
    side_effect_state: str = "none"
    provider_operation_id: str = ""

    def validate(self) -> bool:
        if self.status not in RESULT_STATUSES:
            raise ContractError("invalid_args", "unsupported result status")
        if not isinstance(self.call_id, str) or not _CALL_ID_RE.fullmatch(self.call_id):
            raise ContractError("invalid_args", "result call_id is invalid")
        _require_text(self.result_type, "result_type", max_chars=64)
        if self.error_code is not None and self.error_code not in ERROR_CODES:
            raise ContractError("invalid_args", "unknown result error_code")
        if self.status in {
            "error",
            "denied",
            "not_ready",
            "outcome_unknown",
            "unknown",
        } and not self.error_code:
            raise ContractError("invalid_args", "failure status requires error_code")
        if not isinstance(self.retryable, bool):
            raise ContractError("invalid_args", "retryable must be boolean")
        if self.side_effect_state not in SIDE_EFFECT_STATES:
            raise ContractError("invalid_args", "side_effect_state is invalid")
        if self.status == "outcome_unknown" and self.retryable:
            raise ContractError(
                "invalid_args", "outcome_unknown must not be automatically retryable"
            )
        if not isinstance(self.provider_operation_id, str):
            raise ContractError("invalid_args", "provider_operation_id must be a string")
        if len(self.provider_operation_id) > 256:
            raise ContractError("invalid_args", "provider_operation_id is too long")
        return True

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "status": self.status,
            "call_id": self.call_id,
            "result_type": self.result_type,
            "data": self.data,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "side_effect_state": self.side_effect_state,
            "provider_operation_id": self.provider_operation_id,
        }

    @classmethod
    def success(
        cls,
        call_id: str,
        data: Any = None,
        *,
        result_type: str = "data",
        side_effect_state: str = "none",
        provider_operation_id: str = "",
    ) -> "ToolResult":
        result = cls(
            status="success",
            call_id=call_id,
            result_type=result_type,
            data=data,
            retryable=False,
            side_effect_state=side_effect_state,
            provider_operation_id=provider_operation_id,
        )
        result.validate()
        return result

    @classmethod
    def failure(
        cls,
        call_id: str,
        error_code: str,
        *,
        status: str = "error",
        retryable: bool = False,
        side_effect_state: str = "none",
        provider_operation_id: str = "",
        result_type: str = "error",
    ) -> "ToolResult":
        result = cls(
            status=status,
            call_id=call_id,
            result_type=result_type,
            data=None,
            error_code=error_code,
            retryable=retryable,
            side_effect_state=side_effect_state,
            provider_operation_id=provider_operation_id,
        )
        result.validate()
        return result


@dataclass(slots=True)
class ReplyPlan:
    request_context: RequestContext
    text_segments: list[str]
    media_segments: list[Any]
    terminal: bool
    delivery_key: str

    def validate(self) -> bool:
        if not isinstance(self.request_context, RequestContext):
            raise ContractError("invalid_args", "reply plan requires request_context")
        self.request_context.validate()
        if not isinstance(self.text_segments, list):
            raise ContractError("invalid_args", "text_segments must be a list")
        if len(self.text_segments) > 64:
            raise ContractError("invalid_args", "too many text segments")
        cleaned: list[str] = []
        for segment in self.text_segments:
            if not isinstance(segment, str):
                raise ContractError("invalid_args", "text segment must be a string")
            if len(segment) > 16_384:
                raise ContractError("invalid_args", "text segment is too large")
            cleaned.append(segment)
        if not isinstance(self.media_segments, list):
            raise ContractError("invalid_args", "media_segments must be a list")
        if len(self.media_segments) > 16:
            raise ContractError("invalid_args", "too many media segments")
        if not isinstance(self.terminal, bool):
            raise ContractError("invalid_args", "terminal must be boolean")
        if not isinstance(self.delivery_key, str) or not self.delivery_key:
            raise ContractError("invalid_args", "delivery_key is required")
        if len(self.delivery_key) > 256 or any(
            ord(char) < 32 or ord(char) == 127 for char in self.delivery_key
        ):
            raise ContractError("invalid_args", "delivery_key is invalid")
        self.text_segments = cleaned
        return True

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "request_context": self.request_context.to_dict(),
            "text_segments": list(self.text_segments),
            "media_segments": list(self.media_segments),
            "terminal": self.terminal,
            "delivery_key": self.delivery_key,
        }

    @classmethod
    def from_text(
        cls,
        request_context: RequestContext,
        text: str,
        *,
        delivery_key: str | None = None,
        terminal: bool = True,
        media_segments: list[Any] | None = None,
    ) -> "ReplyPlan":
        key = delivery_key or (
            f"{request_context.instance_id}:{request_context.turn_id}:"
            f"{payload_hash(text)[:24]}"
        )
        plan = cls(
            request_context=request_context,
            text_segments=[str(text)] if str(text) else [],
            media_segments=list(media_segments or []),
            terminal=bool(terminal),
            delivery_key=key,
        )
        plan.validate()
        return plan


@dataclass(frozen=True, slots=True)
class OperationSpec:
    operation: str
    provider_id: str
    write: bool
    requires_confirmation: bool = False


def _op(
    provider: str, operation: str, *, write: bool = False, confirm: bool = False
) -> OperationSpec:
    return OperationSpec(
        operation=operation,
        provider_id=provider,
        write=write,
        requires_confirmation=confirm,
    )


# Authoritative 4.4 fixed method table.
PROVIDER_METHODS: dict[str, tuple[str, ...]] = {
    "qqbot_core_reply": (
        "reply.status",
        "reply.configure_plan",
        "reply.configure_commit",
        "reply.turn_run",
        "reply.cancel_turn",
    ),
    "qqbot_safe_tools": (
        "tool.catalog",
        "tool.process_model_output",
        "tool.dispatch",
        "confirmation.preview",
        "confirmation.confirm",
        "confirmation.cancel",
        "tool.operation_status",
    ),
    "qqbot_web_reader": ("web.read", "web.next_page"),
    "qqbot_image_search": (
        "image.search",
        "image.next_page",
        "image.collect_plan",
    ),
    "qqbot_food": (
        "food.query",
        "food.locations",
        "food.tags",
        "food.set_location",
        "food.change_tags",
        "food.draft_create",
        "food.draft_preview",
        "food.confirm",
        "food.cancel",
        "food.operation_status",
    ),
    "qqbot_stickers": (
        "sticker.collect_plan",
        "sticker.commit",
        "category.list",
        "category.create",
        "category.rename",
        "category.remove",
        "sticker.categorize",
        "sticker.search",
        "sticker.send_plan",
        "sticker.remove",
        "index.rebuild_plan",
        "index.rebuild_commit",
    ),
    "qqbot_memory_commands": (
        "memory.read",
        "memory.search",
        "memory.add",
        "memory.preview_clear",
        "memory.confirm_clear",
        "session.open",
        "session.read",
        "session.append",
        "session.reset",
        "session.switch",
        "persona.list",
        "persona.bind",
        "persona.switch",
    ),
}

_SPECS: list[OperationSpec] = [
    _op("qqbot_core_reply", "reply.status"),
    _op("qqbot_core_reply", "reply.configure_plan", write=True, confirm=True),
    _op("qqbot_core_reply", "reply.configure_commit", write=True, confirm=True),
    _op("qqbot_core_reply", "reply.turn_run", write=True),
    _op("qqbot_core_reply", "reply.cancel_turn", write=True),
    _op("qqbot_safe_tools", "tool.catalog"),
    _op("qqbot_safe_tools", "tool.process_model_output"),
    _op("qqbot_safe_tools", "tool.dispatch", write=True),
    _op("qqbot_safe_tools", "confirmation.preview", write=True),
    _op("qqbot_safe_tools", "confirmation.confirm", write=True),
    _op("qqbot_safe_tools", "confirmation.cancel", write=True),
    _op("qqbot_safe_tools", "tool.operation_status"),
    _op("qqbot_web_reader", "web.read"),
    _op("qqbot_web_reader", "web.next_page"),
    _op("qqbot_image_search", "image.search"),
    _op("qqbot_image_search", "image.next_page"),
    _op("qqbot_image_search", "image.collect_plan"),
    _op("qqbot_food", "food.query"),
    _op("qqbot_food", "food.locations"),
    _op("qqbot_food", "food.tags"),
    _op("qqbot_food", "food.set_location", write=True, confirm=True),
    _op("qqbot_food", "food.change_tags", write=True, confirm=True),
    _op("qqbot_food", "food.draft_create", write=True),
    _op("qqbot_food", "food.draft_preview"),
    _op("qqbot_food", "food.confirm", write=True, confirm=True),
    _op("qqbot_food", "food.cancel", write=True),
    _op("qqbot_food", "food.operation_status"),
    _op("qqbot_stickers", "sticker.collect_plan"),
    _op("qqbot_stickers", "sticker.commit", write=True, confirm=True),
    _op("qqbot_stickers", "category.list"),
    _op("qqbot_stickers", "category.create", write=True, confirm=True),
    _op("qqbot_stickers", "category.rename", write=True, confirm=True),
    _op("qqbot_stickers", "category.remove", write=True, confirm=True),
    _op("qqbot_stickers", "sticker.categorize", write=True, confirm=True),
    _op("qqbot_stickers", "sticker.search"),
    _op("qqbot_stickers", "sticker.send_plan"),
    _op("qqbot_stickers", "sticker.remove", write=True, confirm=True),
    _op("qqbot_stickers", "index.rebuild_plan"),
    _op("qqbot_stickers", "index.rebuild_commit", write=True, confirm=True),
    _op("qqbot_memory_commands", "memory.read"),
    _op("qqbot_memory_commands", "memory.search"),
    _op("qqbot_memory_commands", "memory.add", write=True),
    _op("qqbot_memory_commands", "memory.preview_clear", write=True, confirm=True),
    _op("qqbot_memory_commands", "memory.confirm_clear", write=True, confirm=True),
    _op("qqbot_memory_commands", "session.open", write=True),
    _op("qqbot_memory_commands", "session.read"),
    _op("qqbot_memory_commands", "session.append", write=True),
    _op("qqbot_memory_commands", "session.reset", write=True, confirm=True),
    _op("qqbot_memory_commands", "session.switch", write=True),
    _op("qqbot_memory_commands", "persona.list"),
    _op("qqbot_memory_commands", "persona.bind", write=True, confirm=True),
    _op("qqbot_memory_commands", "persona.switch", write=True),
]
PROVIDER_OPERATION_SPECS: dict[str, OperationSpec] = {
    spec.operation: spec for spec in _SPECS
}
# Ensure the fixed method table and operation specs cannot drift silently.
for _provider_id, _methods in PROVIDER_METHODS.items():
    for _method in _methods:
        if _method not in PROVIDER_OPERATION_SPECS:
            raise RuntimeError(f"missing operation spec: {_method}")
        if PROVIDER_OPERATION_SPECS[_method].provider_id != _provider_id:
            raise RuntimeError(f"provider mismatch: {_method}")
for _operation, _spec in PROVIDER_OPERATION_SPECS.items():
    if _operation not in PROVIDER_METHODS.get(_spec.provider_id, ()):
        raise RuntimeError(f"operation missing from fixed table: {_operation}")

ALL_OPERATIONS = frozenset(PROVIDER_OPERATION_SPECS)
WRITE_OPERATIONS = frozenset(
    operation
    for operation, spec in PROVIDER_OPERATION_SPECS.items()
    if spec.write
)
CONFIRMATION_OPERATIONS = frozenset(
    operation
    for operation, spec in PROVIDER_OPERATION_SPECS.items()
    if spec.requires_confirmation
)


def _known_operation(operation: str) -> bool:
    return operation in PROVIDER_OPERATION_SPECS


def operation_spec(operation: str) -> OperationSpec:
    try:
        return PROVIDER_OPERATION_SPECS[str(operation)]
    except KeyError as exc:
        raise ContractError("invalid_args", "operation is not in the fixed table") from exc


def fixed_methods_for(provider_id: str) -> tuple[str, ...]:
    try:
        return PROVIDER_METHODS[str(provider_id)]
    except KeyError as exc:
        raise ContractError("invalid_args", "provider is not registered in ABI") from exc


def make_error_result(
    call_id: str,
    error_code: str,
    *,
    status: str = "error",
    retryable: bool = False,
    side_effect_state: str = "none",
) -> ToolResult:
    return ToolResult.failure(
        call_id,
        error_code,
        status=status,
        retryable=retryable,
        side_effect_state=side_effect_state,
    )
