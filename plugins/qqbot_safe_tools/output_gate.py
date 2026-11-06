"""One-shot complete-output gate for the sole marker owner.

The gate caches by ``instance_id + turn_id + model_round``.  The parser/dispatch
factory runs once for a fingerprint; duplicate callbacks/cached hook calls get
the same ReplyPlan and cannot execute a marker twice.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .contracts import ReplyPlan, RequestContext


class OutputGateError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


@dataclass(slots=True)
class GateOutcome:
    reply_plan: ReplyPlan
    had_marker: bool
    legacy_chain: list[Any] | None = None
    reason: str = ""

    def validate(self) -> bool:
        self.reply_plan.validate()
        if not isinstance(self.had_marker, bool):
            raise OutputGateError("invalid_state", "had_marker must be boolean")
        if self.legacy_chain is not None and not isinstance(self.legacy_chain, list):
            raise OutputGateError("invalid_state", "legacy_chain must be a list")
        return True


class OneShotOutputGate:
    """At-most-once processing keyed only by turn + model round."""

    def __init__(self, *, max_entries: int = 2_048) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise OutputGateError("invalid_state", "max_entries must be an integer")
        if not 1 <= max_entries <= 100_000:
            raise OutputGateError("invalid_state", "max_entries is out of range")
        self.max_entries = max_entries
        self._cache: OrderedDict[str, GateOutcome] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def fingerprint(request_context: RequestContext, model_round: int) -> str:
        request_context.validate()
        if isinstance(model_round, bool) or not isinstance(model_round, int):
            raise OutputGateError("invalid_state", "model_round must be an integer")
        if model_round < 0:
            raise OutputGateError("invalid_state", "model_round must be non-negative")
        return (
            f"{request_context.instance_id}:{request_context.bot_id}:"
            f"{request_context.turn_id}:{int(model_round)}"
        )

    def cached(self, fingerprint: str) -> GateOutcome | None:
        return self._cache.get(str(fingerprint))

    async def process(
        self,
        fingerprint: str,
        factory: Callable[[], Awaitable[GateOutcome]],
    ) -> GateOutcome:
        key = str(fingerprint or "")
        if not key or len(key) > 512:
            raise OutputGateError("invalid_state", "fingerprint is invalid")
        existing = self._cache.get(key)
        if existing is not None:
            self._cache.move_to_end(key)
            return existing
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) > self.max_entries:
                for stale_key, stale_lock in list(self._locks.items()):
                    if stale_key not in self._cache and not stale_lock.locked():
                        self._locks.pop(stale_key, None)
            lock = asyncio.Lock()
            self._locks[key] = lock
        async with lock:
            existing = self._cache.get(key)
            if existing is not None:
                self._cache.move_to_end(key)
                return existing
            outcome = await factory()
            if not isinstance(outcome, GateOutcome):
                raise OutputGateError("invalid_state", "factory returned invalid outcome")
            outcome.validate()
            self._cache[key] = outcome
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
        self._locks.pop(key, None)
        return outcome

    def clear(self) -> None:
        self._cache.clear()
        self._locks.clear()

    def __len__(self) -> int:
        return len(self._cache)
