"""Five-scope resolution and ACL checks.

The caller passes an authenticated :class:`RequestContext`.  Scope arguments
may select only a target inside that context; they can never self-assert a new
instance, platform, bot, group, conversation or persona authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    SCOPES,
    STATUS_INVALID_ARGS,
    STATUS_PERMISSION_DENIED,
    RequestContext,
    ScopeRef,
    canonical_scope_id,
    coerce_scope_ref,
)

DEFAULT_ALLOWED_SCOPES = SCOPES


class ScopeResolutionError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ScopeAuthorization:
    allowed: bool
    status: str
    error_code: str | None = None
    shared: bool = False
    requires_confirmation: bool = False
    reason: str = ""


def _target_from_requested(value: Any) -> tuple[str, str]:
    if isinstance(value, ScopeRef):
        return value.kind, value.target_id
    if isinstance(value, Mapping):
        kind = str(value.get("kind") or value.get("scope") or "").strip()
        target = str(
            value.get("target_id")
            or value.get("target")
            or value.get("id")
            or ""
        ).strip()
        return kind, target
    if value is None:
        return "", ""
    text = str(value).strip()
    return text, ""


def resolve_scope(
    context: RequestContext,
    requested_scope: Any = None,
    target_id: str | None = None,
) -> ScopeRef:
    """Resolve one canonical scope or raise a deterministic scope error."""

    request = context.requested_scope if requested_scope is None else requested_scope
    kind, requested_target = _target_from_requested(request)
    explicit_target = str(target_id or "").strip()
    if explicit_target and requested_target and explicit_target != requested_target:
        raise ScopeResolutionError("conflicting_scope_targets")
    target = explicit_target or requested_target

    if isinstance(request, ScopeRef):
        expected = (
            context.instance_id,
            context.platform,
            context.bot_id,
        )
        actual = (request.instance_id, request.platform, request.bot_id)
        if actual != expected:
            raise ScopeResolutionError("scope_namespace_mismatch")
    elif isinstance(request, Mapping):
        for field in ("instance_id", "platform", "bot_id"):
            supplied = str(request.get(field) or "").strip()
            if supplied and supplied != str(getattr(context, field) or "").strip():
                raise ScopeResolutionError("scope_namespace_mismatch")

    if kind not in SCOPES:
        raise ScopeResolutionError("invalid_scope_kind")
    if kind == "global":
        if target and target not in {"all", "global"}:
            raise ScopeResolutionError("invalid_global_target")
        target = "all"
    elif kind == "persona":
        target = target or str(context.persona_id or "").strip()
    elif kind == "conversation":
        target = target or str(context.conversation_id or "").strip()
    elif kind == "group":
        target = target or str(context.group_id or "").strip()
    elif kind == "user":
        target = target or str(context.actor_id or "").strip()

    if not target:
        raise ScopeResolutionError("missing_scope_target")
    scope = ScopeRef(
        kind=kind,
        target_id=target,
        instance_id=str(context.instance_id or "").strip(),
        platform=str(context.platform or "").strip(),
        bot_id=str(context.bot_id or "").strip(),
    )
    validation_error = scope.validate()
    if validation_error:
        raise ScopeResolutionError(validation_error)
    return scope


def resolve_requested_scope(
    context: RequestContext,
    args: Mapping[str, Any] | None,
    *,
    default_kind: str,
) -> ScopeRef:
    args = args if isinstance(args, Mapping) else {}
    requested = args.get("scope")
    if not requested:
        requested = context.requested_scope or default_kind
    return resolve_scope(
        context,
        requested,
        str(args.get("target_id") or args.get("scope_target") or "").strip() or None,
    )


def target_scope_id(scope: ScopeRef) -> str:
    """Stable scope id passed to a backend, always fully namespaced."""

    return canonical_scope_id(
        kind=scope.kind,
        target_id=scope.target_id,
        instance_id=scope.instance_id,
        platform=scope.platform,
        bot_id=scope.bot_id,
    )


def authorize_scope(
    context: RequestContext,
    scope: ScopeRef,
    *,
    write: bool = False,
    allowed_scopes: tuple[str, ...] = DEFAULT_ALLOWED_SCOPES,
    admin_shared_edit: bool = False,
) -> ScopeAuthorization:
    """Apply private/group/conversation/persona/global ACL rules."""

    if scope.kind not in allowed_scopes:
        return ScopeAuthorization(
            False,
            STATUS_PERMISSION_DENIED,
            "scope_not_allowed",
            reason="scope_not_allowed",
        )
    if (
        scope.instance_id != context.instance_id
        or scope.platform != context.platform
        or scope.bot_id != context.bot_id
    ):
        return ScopeAuthorization(
            False,
            STATUS_PERMISSION_DENIED,
            "cross_scope_denied",
            reason="namespace_mismatch",
        )

    if scope.kind == "user":
        if scope.target_id != context.actor_id:
            return ScopeAuthorization(
                False,
                STATUS_PERMISSION_DENIED,
                "cross_scope_denied",
                reason="user_scope_is_self_only",
            )
        return ScopeAuthorization(True, "ok")

    if scope.kind == "group":
        if context.is_private or not context.group_id:
            return ScopeAuthorization(
                False,
                STATUS_PERMISSION_DENIED,
                "private_group_forgery",
                reason="group_scope_requires_group_chat",
            )
        allowed = scope.target_id == context.group_id or (
            scope.target_id in context.allowed_group_ids
        )
        if not allowed:
            return ScopeAuthorization(
                False,
                STATUS_PERMISSION_DENIED,
                "cross_scope_denied",
                reason="group_not_authorized",
            )
        return ScopeAuthorization(
            True,
            "ok",
            shared=scope.target_id != context.group_id,
        )

    if scope.kind == "conversation":
        allowed = scope.target_id == context.conversation_id or (
            scope.target_id in context.allowed_conversation_ids
        )
        if not allowed:
            return ScopeAuthorization(
                False,
                STATUS_PERMISSION_DENIED,
                "cross_scope_denied",
                reason="conversation_not_authorized",
            )
        return ScopeAuthorization(
            True,
            "ok",
            shared=scope.target_id != context.conversation_id,
        )

    if scope.kind == "persona":
        if scope.target_id == context.persona_id:
            return ScopeAuthorization(True, "ok")
        explicit_shared = bool(context.allow_shared_persona) and (
            scope.target_id in context.allowed_persona_ids
        )
        admin_shared = bool(
            admin_shared_edit
            and context.admin
            and scope.target_id in context.allowed_persona_ids
        )
        if not (explicit_shared or admin_shared):
            return ScopeAuthorization(
                False,
                STATUS_PERMISSION_DENIED,
                "cross_scope_denied",
                reason="shared_persona_not_authorized",
            )
        return ScopeAuthorization(
            True,
            "ok",
            shared=True,
            requires_confirmation=write,
        )

    if scope.kind == "global":
        if not bool(context.allow_global_scope):
            return ScopeAuthorization(
                False,
                STATUS_PERMISSION_DENIED,
                "cross_scope_denied",
                reason="global_scope_not_granted",
            )
        return ScopeAuthorization(
            True,
            "ok",
            shared=True,
            requires_confirmation=write,
        )

    return ScopeAuthorization(
        False,
        STATUS_INVALID_ARGS,
        "invalid_scope_kind",
        reason="invalid_scope_kind",
    )


def requires_confirmation(
    context: RequestContext,
    scope: ScopeRef,
    *,
    operation: str,
    authorization: ScopeAuthorization | None = None,
    clear_confirm: bool = True,
    reset_confirm: bool = True,
) -> bool:
    """Decide whether an operation must pass preview/confirmation first."""

    auth = authorization or authorize_scope(context, scope, write=True)
    if operation in {"clear", "memory_confirm_clear", "memory_preview_clear"}:
        return bool(clear_confirm)
    if operation in {"session_reset", "reset"}:
        return bool(reset_confirm)
    if operation in {"memory_add", "persona_bind", "persona_switch"}:
        return bool(auth.shared)
    return False


def scope_public_view(scope: ScopeRef) -> dict[str, str]:
    """Return metadata only; memory bodies never enter scope/audit views."""

    return {
        "kind": scope.kind,
        "target_id": scope.target_id,
        "canonical_id": scope.canonical_id,
    }
