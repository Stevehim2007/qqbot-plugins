"""Server-side actor/scope authorization for ABI v1.

The caller can only pass an authenticated :class:`RequestContext`; there is no
``actor_id``/``is_admin``/``scope`` argument that model output could spoof.
Policy rules are configuration owned by the plugin/host and are never accepted
from QQ users or model-generated marker arguments.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .contracts import (
    SCOPE_TYPES,
    ContractError,
    RequestContext,
    operation_spec,
)


class AuthorizationError(ValueError):
    def __init__(self, code: str = "permission_denied", message: str = "") -> None:
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ScopeRef:
    kind: str
    target_id: str

    def validate(self) -> bool:
        if self.kind not in SCOPE_TYPES:
            raise AuthorizationError("permission_denied", "unsupported scope")
        if not self.target_id or len(self.target_id) > 256:
            raise AuthorizationError("permission_denied", "scope target is invalid")
        return True


def scope_for_context(context: RequestContext) -> ScopeRef:
    context.validate()
    kind = context.requested_scope
    target = {
        "global": context.instance_id,
        "persona": context.persona_id,
        "conversation": context.conversation_id,
        "group": context.conversation_id,
        "user": context.actor_id,
    }[kind]
    return ScopeRef(kind, target)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    actor_ids: tuple[str, ...]
    scope_types: tuple[str, ...]
    operations: tuple[str, ...]
    allow_read: bool = True
    allow_write: bool = False
    cross_scope: bool = False

    def validate(self) -> bool:
        if not self.actor_ids:
            raise AuthorizationError("permission_denied", "rule needs actors")
        if not self.scope_types:
            raise AuthorizationError("permission_denied", "rule needs scopes")
        for scope in self.scope_types:
            if scope != "*" and scope not in SCOPE_TYPES:
                raise AuthorizationError("permission_denied", "rule scope is invalid")
        if not self.operations:
            raise AuthorizationError("permission_denied", "rule needs operations")
        return True

    def matches(
        self,
        context: RequestContext,
        operation: str,
        *,
        write: bool,
        needs_cross_scope: bool,
    ) -> bool:
        self.validate()
        if "*" not in self.actor_ids and context.actor_id not in self.actor_ids:
            return False
        if "*" not in self.scope_types and context.requested_scope not in self.scope_types:
            return False
        if "*" not in self.operations and operation not in self.operations:
            return False
        if write and not self.allow_write:
            return False
        if not write and not self.allow_read:
            return False
        if needs_cross_scope and not self.cross_scope:
            return False
        return True


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    policy_version: int = 1
    admin_actor_ids: frozenset[str] = frozenset()
    rules: tuple[PolicyRule, ...] = ()
    cross_scope_default: str = "deny"

    def validate(self) -> bool:
        if isinstance(self.policy_version, bool) or not isinstance(
            self.policy_version, int
        ):
            raise AuthorizationError("permission_denied", "policy_version invalid")
        if self.policy_version < 1:
            raise AuthorizationError("permission_denied", "policy_version invalid")
        if self.cross_scope_default != "deny":
            raise AuthorizationError(
                "permission_denied", "cross_scope_default must be deny"
            )
        for actor in self.admin_actor_ids:
            if not isinstance(actor, str) or not actor or len(actor) > 256:
                raise AuthorizationError("permission_denied", "admin actor invalid")
        for rule in self.rules:
            if not isinstance(rule, PolicyRule):
                raise AuthorizationError("permission_denied", "policy rule invalid")
            rule.validate()
        return True


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    operation: str
    write: bool
    scope: ScopeRef
    actor_id: str
    policy_version: int

    def validate(self) -> bool:
        if not self.allowed:
            raise AuthorizationError("permission_denied")
        return True


class Authorizer:
    """Evaluates the configured policy; no identity is accepted from callers."""

    def __init__(self, policy: AuthorizationPolicy | None = None) -> None:
        self.policy = policy or AuthorizationPolicy()
        self.policy.validate()

    @property
    def policy_version(self) -> int:
        return self.policy.policy_version

    def _resource_scope(self, resource_scope: Any) -> ScopeRef | None:
        if resource_scope is None:
            return None
        if isinstance(resource_scope, ScopeRef):
            resource_scope.validate()
            return resource_scope
        if isinstance(resource_scope, (tuple, list)) and len(resource_scope) == 2:
            ref = ScopeRef(str(resource_scope[0]), str(resource_scope[1]))
            ref.validate()
            return ref
        if isinstance(resource_scope, str) and resource_scope:
            if ":" in resource_scope:
                kind, target = resource_scope.split(":", 1)
                ref = ScopeRef(scope_for_scope_name(kind), target)
            else:
                ref = ScopeRef(scope_for_scope_name(resource_scope), resource_scope)
            ref.validate()
            return ref
        raise AuthorizationError("permission_denied", "resource scope is invalid")

    def authorize(
        self,
        context: RequestContext,
        operation: str,
        *,
        resource_actor_id: str | None = None,
        resource_scope: Any = None,
    ) -> AuthorizationDecision:
        """Authorize one fixed operation using only server-derived context."""

        try:
            context.validate()
            spec = operation_spec(operation)
        except ContractError as exc:
            raise AuthorizationError(exc.code) from exc
        current_scope = scope_for_context(context)
        resource_ref = self._resource_scope(resource_scope)
        cross_scope = bool(
            (resource_actor_id is not None and resource_actor_id != context.actor_id)
            or (resource_ref is not None and resource_ref != current_scope)
        )
        if cross_scope and self.policy.cross_scope_default != "deny":
            raise AuthorizationError("permission_denied")

        if context.actor_id in self.policy.admin_actor_ids:
            return AuthorizationDecision(
                allowed=True,
                operation=operation,
                write=spec.write,
                scope=current_scope,
                actor_id=context.actor_id,
                policy_version=self.policy.policy_version,
            )

        for rule in self.policy.rules:
            if rule.matches(
                context,
                operation,
                write=spec.write,
                needs_cross_scope=cross_scope,
            ):
                return AuthorizationDecision(
                    allowed=True,
                    operation=operation,
                    write=spec.write,
                    scope=current_scope,
                    actor_id=context.actor_id,
                    policy_version=self.policy.policy_version,
                )
        raise AuthorizationError("permission_denied", "operation is not authorized")

    def can(self, context: RequestContext, operation: str, **kwargs: Any) -> bool:
        try:
            self.authorize(context, operation, **kwargs)
            return True
        except AuthorizationError:
            return False


def scope_for_scope_name(value: str) -> str:
    kind = str(value).split(":", 1)[0]
    return kind if kind in SCOPE_TYPES else str(value)


def _parse_string_list(value: Any, *, max_items: int = 128, max_chars: int = 256) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise AuthorizationError("permission_denied", "policy list invalid") from exc
            if not isinstance(parsed, list):
                raise AuthorizationError("permission_denied", "policy list invalid")
            items: Iterable[Any] = parsed
        else:
            items = [part.strip() for part in text.split(",")]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise AuthorizationError("permission_denied", "policy list invalid")
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > max_chars:
            raise AuthorizationError("permission_denied", "policy item too long")
        if text not in result:
            result.append(text)
        if len(result) > max_items:
            raise AuthorizationError("permission_denied", "policy list too long")
    return tuple(result)


def policy_from_config(config: Mapping[str, Any] | Any) -> AuthorizationPolicy:
    """Build a policy from injected plugin config (flat AstrBot keys)."""

    def lookup(key: str, default: Any = None) -> Any:
        if isinstance(config, Mapping):
            return config.get(key, default)
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                return default
        return getattr(config, key, default)

    raw_rules = lookup("auth_rules", "[]")
    if isinstance(raw_rules, str):
        try:
            parsed_rules = json.loads(raw_rules) if raw_rules.strip() else []
        except ValueError as exc:
            raise AuthorizationError("permission_denied", "auth_rules is invalid JSON") from exc
    else:
        parsed_rules = raw_rules
    if parsed_rules is None:
        parsed_rules = []
    if not isinstance(parsed_rules, list):
        raise AuthorizationError("permission_denied", "auth_rules must be a list")
    rules: list[PolicyRule] = []
    for raw in parsed_rules[:128]:
        if not isinstance(raw, Mapping):
            raise AuthorizationError("permission_denied", "auth rule must be an object")
        rule = PolicyRule(
            actor_ids=_parse_string_list(raw.get("actor_ids", ["*"])),
            scope_types=_parse_string_list(raw.get("scope_types", ["*"])),
            operations=_parse_string_list(raw.get("operations", ["*"])),
            allow_read=bool(raw.get("allow_read", True)),
            allow_write=bool(raw.get("allow_write", False)),
            cross_scope=bool(raw.get("cross_scope", False)),
        )
        rule.validate()
        rules.append(rule)
    policy = AuthorizationPolicy(
        policy_version=int(lookup("auth_policy_version", 1) or 1),
        admin_actor_ids=frozenset(
            _parse_string_list(lookup("auth_admin_actor_ids", []))
        ),
        rules=tuple(rules),
        cross_scope_default=str(lookup("auth_cross_scope_default", "deny") or "deny"),
    )
    policy.validate()
    return policy
