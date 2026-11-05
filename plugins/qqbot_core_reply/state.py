"""Runtime state, bounded history and the single-turn reply controller.

This module contains no AstrBot imports.  It owns the in-process cooldown
slots, bounded sender history, ambient counters, turn claims, reply leases and
delivery records.  Persistent conversation/memory storage remains owned by
AstrBot's public ConversationManager/memory services.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

try:  # Loaded as a package by AstrBot.
    from .contracts import ContractError, RequestContext
    from .config import RuntimeConfig
except ImportError:  # Loaded directly by a test harness.
    from contracts import ContractError, RequestContext
    from config import RuntimeConfig

MAX_STATE_ENTRIES = 4096


def _safe_text(value: Any, *, max_chars: int, keep_newlines: bool = False) -> str:
    text = str(value or "")
    allowed = ("\n", "\t", "\r") if keep_newlines else ()
    text = "".join(char for char in text if ord(char) >= 32 or char in allowed)
    return text[: max(0, int(max_chars))].strip()


@dataclass
class TurnRecord:
    turn_id: str
    conversation_key: str
    delivery_key: str
    owner_epoch: int
    request_context: RequestContext
    status: str = "running"
    delivery_state: str = "pending"
    plan: Any = None
    error_code: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    task: Any = None
    model_round: int = 0
    controller: str = "qqbot_core_reply"

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "conversation_key": self.conversation_key,
            "delivery_key": self.delivery_key,
            "owner_epoch": self.owner_epoch,
            "status": self.status,
            "delivery_state": self.delivery_state,
            "error_code": self.error_code,
            "model_round": self.model_round,
            "controller": self.controller,
            "has_plan": self.plan is not None,
        }


class RuntimeState:
    """All mutable runtime state owned by qqbot_core_reply."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        rng: Any = None,
        clock: Any = None,
    ) -> None:
        self.config = config
        self.rng = rng
        self.clock = clock or time.monotonic
        self.last_group_reply: dict[str, float] = {}
        self.last_user_reply: dict[str, float] = {}
        self.recent_history: dict[str, list[dict[str, Any]]] = {}
        self.ambient_last_at: dict[str, float] = {}
        self.ambient_message_count: dict[str, int] = {}
        self.last_song_group: dict[str, float] = {}
        self.last_song_user: dict[str, float] = {}
        self.turns: dict[str, TurnRecord] = {}
        self.active_turns: dict[str, str] = {}
        self.processed_outputs: set[str] = set()
        self.pending_plans: dict[str, dict[str, Any]] = {}
        self.delivery_turns: dict[str, str] = {}
        self.tasks: set[asyncio.Task] = set()
        self.ready = False
        self.terminated = False
        self.model_controller = "qqbot_core_reply"
        try:
            self.owner_epoch = int(config.value("owner_epoch") or 1)
        except (TypeError, ValueError):
            self.owner_epoch = 1
        self.config_revision = 1
        self.delivery_revision = 1

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def set_ready(self, ready: bool) -> None:
        self.ready = bool(ready) and not self.terminated

    def mark_terminated(self) -> None:
        self.terminated = True
        self.ready = False

    def register_task(self, task: asyncio.Task) -> None:
        self.tasks.add(task)
        task.add_done_callback(lambda item: self.tasks.discard(item))

    async def cancel_all_tasks(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in list(self.tasks) if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()

    # ------------------------------------------------------------------
    # Cooldown / ambient bookkeeping
    # ------------------------------------------------------------------
    def prune(self, now: float | None = None) -> None:
        now = self.clock() if now is None else float(now)
        total = (
            len(self.last_group_reply)
            + len(self.last_user_reply)
            + len(self.ambient_last_at)
            + len(self.ambient_message_count)
            + len(self.recent_history)
            + len(self.last_song_group)
            + len(self.last_song_user)
            + len(self.turns)
        )
        if total < MAX_STATE_ENTRIES:
            return
        cutoff = now - 86400
        for mapping in (
            self.last_group_reply,
            self.last_user_reply,
            self.ambient_last_at,
            self.last_song_group,
            self.last_song_user,
        ):
            for key, timestamp in list(mapping.items()):
                if timestamp < cutoff:
                    mapping.pop(key, None)
        if len(self.ambient_message_count) > 256:
            for key, _ in sorted(
                self.ambient_message_count.items(),
                key=lambda pair: pair[1],
            )[: len(self.ambient_message_count) - 256]:
                self.ambient_message_count.pop(key, None)
        self.prune_history(now)
        self.prune_turns(now)

    def prune_turns(self, now: float) -> None:
        cutoff = now - 86400
        for key, record in list(self.turns.items()):
            if record.status in {"planned", "delivered", "cancelled", "error", "outcome_unknown"} and record.updated_at < cutoff:
                self.turns.pop(key, None)

    # ------------------------------------------------------------------
    # Bounded sender history
    # ------------------------------------------------------------------
    def history_limit(self) -> int:
        limit = self.config.effective_int(
            "context.max_messages",
            "group_identity_history_limit",
            default=8,
            minimum=0,
            maximum=30,
        )
        if self.config.bool("ambient_group_chat_enabled"):
            limit = max(limit, self.config.int("ambient_group_cache_limit", 0, 100))
        return limit

    def history_ttl(self) -> int:
        ttl = self.config.int("group_identity_history_ttl_seconds", 30, 3600)
        if self.config.bool("ambient_group_chat_enabled"):
            ttl = self.config.int("ambient_group_cache_ttl_seconds", 30, 3600)
        return ttl

    def history_enabled(self) -> bool:
        return bool(
            self.config.bool("group_identity_context_enabled")
            or self.config.bool("ambient_group_chat_enabled")
        )

    def prune_history(self, now: float | None = None) -> None:
        now = self.clock() if now is None else float(now)
        cutoff = now - self.history_ttl()
        limit = self.history_limit()
        for session, items in list(self.recent_history.items()):
            kept = [
                item
                for item in items
                if float(item.get("timestamp", 0) or 0) >= cutoff
            ]
            if limit <= 0:
                kept = []
            else:
                kept = kept[-limit:]
            if kept:
                self.recent_history[session] = kept
            else:
                self.recent_history.pop(session, None)
        if len(self.recent_history) > 128:
            oldest = sorted(
                self.recent_history.items(),
                key=lambda pair: float(
                    pair[1][-1].get("timestamp", 0) if pair[1] else 0
                ),
            )
            for session, _ in oldest[: len(self.recent_history) - 128]:
                self.recent_history.pop(session, None)

    def history_for(self, session_key: str) -> list[dict[str, Any]]:
        if not self.history_enabled():
            return []
        self.prune_history(self.clock())
        return [dict(item) for item in self.recent_history.get(session_key, [])]

    def remember_history(
        self,
        session_key: str,
        *,
        nickname: str,
        qq: str,
        text: str,
        has_image: bool = False,
        has_face: bool = False,
    ) -> None:
        if not self.history_enabled():
            return
        value = str(text or "").strip()
        if not value:
            value = "[图片]" if has_image else "[QQ表情]" if has_face else "[空消息]"
        value = _safe_text(
            value,
            max_chars=self.config.int("group_identity_message_max_chars", 20, 500),
            keep_newlines=True,
        )
        entry = {
            "nickname": _safe_text(nickname, max_chars=32),
            "qq": qq if str(qq).isdigit() else "",
            "text": value,
            "timestamp": self.clock(),
        }
        items = self.recent_history.setdefault(session_key, [])
        items.append(entry)
        limit = self.history_limit()
        if limit <= 0:
            self.recent_history.pop(session_key, None)
        else:
            del items[:-limit]
        self.prune_history(self.clock())

    def bump_ambient_message_count(self, session_key: str) -> int:
        count = self.ambient_message_count.get(session_key, 0) + 1
        self.ambient_message_count[session_key] = count
        if len(self.ambient_message_count) > 256:
            for key, _ in sorted(
                self.ambient_message_count.items(),
                key=lambda pair: pair[1],
            )[: len(self.ambient_message_count) - 256]:
                self.ambient_message_count.pop(key, None)
        return count

    # ------------------------------------------------------------------
    # Single turn controller / reply lease
    # ------------------------------------------------------------------
    @staticmethod
    def conversation_key(request_context: RequestContext) -> str:
        return "|".join(
            (
                str(request_context.instance_id),
                str(request_context.platform),
                str(request_context.bot_id),
                str(request_context.conversation_id),
                str(request_context.persona_id),
            )
        )

    def claim_turn(
        self,
        request_context: RequestContext,
        *,
        delivery_key: str,
        controller: str = "qqbot_core_reply",
        model_round: int = 0,
    ) -> tuple[TurnRecord, bool]:
        """Return (record, created).  Enforce one active turn per conversation."""

        if self.terminated:
            raise ContractError("not_ready", "runtime terminated")
        self.prune(self.clock())
        existing = self.turns.get(request_context.turn_id)
        if existing is not None:
            return existing, False
        used_by = self.delivery_turns.get(str(delivery_key or ""))
        if used_by and used_by != request_context.turn_id:
            raise ContractError(
                "duplicate_call",
                "reply delivery key already belongs to another turn",
            )
        key = self.conversation_key(request_context)
        active_id = self.active_turns.get(key)
        if active_id:
            raise ContractError("turn_conflict", "conversation already has an active turn")
        now = self.clock()
        record = TurnRecord(
            turn_id=request_context.turn_id,
            conversation_key=key,
            delivery_key=delivery_key,
            owner_epoch=int(request_context.owner_epoch),
            request_context=request_context,
            status="running",
            delivery_state="pending",
            created_at=now,
            updated_at=now,
            controller=controller,
            model_round=int(model_round),
        )
        self.turns[record.turn_id] = record
        self.active_turns[key] = record.turn_id
        if delivery_key:
            self.delivery_turns[str(delivery_key)] = record.turn_id
        return record, True

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        return self.turns.get(str(turn_id or ""))

    def finish_turn(
        self,
        turn_id: str,
        *,
        status: str,
        delivery_state: str | None = None,
        plan: Any = None,
        error_code: str = "",
    ) -> TurnRecord:
        record = self.get_turn(turn_id)
        if record is None:
            raise ContractError("not_found", "turn not found")
        record.status = str(status)
        if delivery_state is not None:
            record.delivery_state = str(delivery_state)
        if plan is not None:
            record.plan = plan
        record.error_code = str(error_code)
        record.updated_at = self.clock()
        if record.status in {
            "planned",
            "delivered",
            "cancelled",
            "error",
            "outcome_unknown",
        }:
            if self.active_turns.get(record.conversation_key) == record.turn_id:
                self.active_turns.pop(record.conversation_key, None)
        return record

    def record_plan(self, turn_id: str, plan: Any) -> TurnRecord:
        return self.finish_turn(
            turn_id,
            status="planned",
            delivery_state="pending",
            plan=plan,
        )

    def record_delivery(self, turn_id: str, delivery_state: str = "delivered") -> TurnRecord:
        return self.finish_turn(
            turn_id,
            status="delivered" if delivery_state == "delivered" else "outcome_unknown",
            delivery_state=delivery_state,
        )

    def mark_delivery_unknown(self, turn_id: str) -> TurnRecord:
        record = self.get_turn(turn_id)
        if record is None:
            raise ContractError("not_found", "turn not found")
        record.status = "outcome_unknown"
        record.delivery_state = "outcome_unknown"
        record.error_code = "delivery_unknown"
        record.updated_at = self.clock()
        if self.active_turns.get(record.conversation_key) == record.turn_id:
            self.active_turns.pop(record.conversation_key, None)
        return record

    def cancel_turn(self, turn_id: str, *, owner_epoch: int | None = None) -> TurnRecord:
        record = self.get_turn(turn_id)
        if record is None:
            raise ContractError("not_found", "turn not found")
        if owner_epoch is not None and int(owner_epoch) != int(record.owner_epoch):
            raise ContractError("owner_epoch_mismatch", "owner epoch mismatch")
        if record.delivery_state in {"delivered", "outcome_unknown"}:
            record.updated_at = self.clock()
            return record
        record.status = "cancelled"
        record.delivery_state = "delivered"
        record.error_code = "turn_cancelled"
        record.updated_at = self.clock()
        if self.active_turns.get(record.conversation_key) == record.turn_id:
            self.active_turns.pop(record.conversation_key, None)
        task = record.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        return record

    def mark_output_processed(self, fingerprint: str) -> bool:
        value = str(fingerprint or "")
        if not value or value in self.processed_outputs:
            return False
        self.processed_outputs.add(value)
        return True

    def active_turn_count(self) -> int:
        return len(self.active_turns)
