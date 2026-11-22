"""``config.overlay.v1`` — per-event config overlay for the capability policy.

The policy store is global, but a group override must only change what that
*group's* event reads.  The implementation is a ``ContextVar`` that the event
entry point fills once and that the plugin's own config reader consults before
its own settings.

Two properties matter and both are enforced here:

* **no implicit inheritance** — a background task created inside an event gets a
  copy of the context, which would silently pin one group's prompt into every
  later group.  ``current_overlay()`` therefore requires
  ``asyncio.current_task()`` to be the task that opened the scope; child tasks
  see ``None`` and fall back to global config.  ``detached()`` is the explicit
  opt-in for work that is *meant* to keep the scope (e.g. a follow-up send).
* **falsy values win** — ``False``/``0``/``""`` are real overrides; only a
  missing key falls back to the plugin's own settings.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Mapping

OVERLAY_VERSION = "config.overlay.v1"
_MISSING = object()

_CURRENT: contextvars.ContextVar["PolicyOverlay | None"] = contextvars.ContextVar(
    "qqbot_policy_overlay", default=None
)


@dataclass(frozen=True, slots=True)
class PolicyOverlay:
    """Resolved per-event view of one plugin's group override."""

    plugin_id: str
    params: Mapping[str, Any] = field(default_factory=dict)
    config_values: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    revision: int = 0
    scope: str = "global"
    scope_id: str = ""
    creator_task: Any = None

    def has(self, key: str) -> bool:
        return key in self.config_values or key in self.params

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.config_values:
            return self.config_values[key]
        if key in self.params:
            return self.params[key]
        return default

    def lookup(self, key: str) -> Any:
        """Like :meth:`get`, but returns a sentinel for "not overridden"."""
        if key in self.config_values:
            return self.config_values[key]
        if key in self.params:
            return self.params[key]
        return _MISSING

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "params": dict(self.params),
            "config_values": dict(self.config_values),
            "sources": list(self.sources),
            "revision": int(self.revision),
            "scope": self.scope,
            "scope_id": self.scope_id,
            "version": OVERLAY_VERSION,
        }


def build(
    plugin_id: str,
    policy: Mapping[str, Any] | None,
    *,
    scope: str = "group",
    scope_id: str = "",
) -> PolicyOverlay | None:
    """Build an overlay from a ``policy_effective`` payload.

    Returns ``None`` when there is nothing to override, so callers can skip the
    context entirely instead of installing an empty scope.
    """
    if not isinstance(policy, Mapping):
        return None
    raw_params = policy.get("params")
    params = {
        str(key): value for key, value in dict(raw_params).items()
    } if isinstance(raw_params, Mapping) else {}
    raw_config = policy.get("config_values")
    config_values = {
        str(key): value for key, value in dict(raw_config).items()
    } if isinstance(raw_config, Mapping) else {}
    if not config_values:
        # Older safe_tools builds do not send config_values; the policy names are
        # the best available fallback and the config reader also queries them.
        config_values = dict(params)
    if not params and not config_values:
        return None
    return PolicyOverlay(
        plugin_id=str(plugin_id),
        params=params,
        config_values=config_values,
        sources=tuple(str(item) for item in (policy.get("sources") or ())),
        revision=int(policy.get("revision") or 0),
        scope=str(scope or "global"),
        scope_id=str(scope_id or ""),
    )


def current_overlay() -> PolicyOverlay | None:
    """The overlay that applies to *this* task, or ``None``."""
    overlay = _CURRENT.get()
    if overlay is None:
        return None
    creator = overlay.creator_task
    if creator is None:
        return overlay
    try:
        running = asyncio.current_task()
    except RuntimeError:
        return None
    if running is not creator:
        return None
    return overlay


def current_value(key: str, default: Any = None) -> Any:
    overlay = current_overlay()
    return default if overlay is None else overlay.get(key, default)


def activate(overlay: PolicyOverlay | None) -> Any:
    if overlay is None:
        return None
    if overlay.creator_task is None:
        try:
            creator = asyncio.current_task()
        except RuntimeError:
            creator = None
        if creator is not None:
            overlay = replace(overlay, creator_task=creator)
    return _CURRENT.set(overlay)


def deactivate(token: Any) -> None:
    if token is not None:
        _CURRENT.reset(token)


@contextmanager
def policy_scope(overlay: PolicyOverlay | None) -> Iterator[PolicyOverlay | None]:
    """Install ``overlay`` for the current task; always reset on exit."""
    token = activate(overlay)
    try:
        yield overlay
    finally:
        deactivate(token)


def overlay_handler(func: Any) -> Any:
    """Decorator: install ``self._policy_overlay_for(event)`` for one handler.

    Works for both plain coroutines and async generators (AstrBot handlers are
    frequently async generators, and a ``with`` block cannot span a ``yield``).
    The host class must provide ``_policy_overlay_for(event)``.
    """
    if inspect.isasyncgenfunction(func):

        @functools.wraps(func)
        async def async_gen_wrapper(self: Any, event: Any, *args: Any, **kwargs: Any):
            token = activate(self._policy_overlay_for(event))
            try:
                async for item in func(self, event, *args, **kwargs):
                    yield item
            finally:
                deactivate(token)

        return async_gen_wrapper

    @functools.wraps(func)
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any):
        with policy_scope(self._policy_overlay_for(event)):
            return await func(self, event, *args, **kwargs)

    return wrapper


@contextmanager
def detached(overlay: PolicyOverlay | None = None) -> Iterator[PolicyOverlay | None]:
    """Opt a new task into an explicit overlay (background work only).

    Without an argument the overlay inherited through the context copy is
    re-stamped onto the *current* task: that is the documented way for a child
    task to keep its parent event's scope.
    """
    base = overlay if overlay is not None else _CURRENT.get()
    if base is None:
        yield None
        return
    try:
        creator = asyncio.current_task()
    except RuntimeError:
        creator = None
    fresh = replace(base, creator_task=creator)
    token = _CURRENT.set(fresh)
    try:
        yield fresh
    finally:
        _CURRENT.reset(token)
