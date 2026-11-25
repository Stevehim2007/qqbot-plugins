"""Runtime state, bounded history and the single-turn reply controller.

This module contains no AstrBot imports.  It owns the in-process cooldown
slots, bounded sender history, ambient counters, turn claims, reply leases and
delivery records.  Persistent conversation/memory storage remains owned by
AstrBot's public ConversationManager/memory services.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
        self.ignored_group_messages: dict[str, int] = {}
        self.recent_inbound: dict[str, float] = {}
        self.model_participation_counts: dict[str, int] = {}
        self.member_language_prefs: dict[str, dict[str, str]] = {}
        self._language_prefs_path: Any = None
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
        if self.config.bool("group_context_enabled", True):
            limit = max(limit, self.config.int("group_context_limit", 1, 100))
        return limit

    def history_ttl(self) -> int:
        ttl = self.config.int("group_identity_history_ttl_seconds", 30, 3600)
        if self.config.bool("ambient_group_chat_enabled"):
            ttl = self.config.int("ambient_group_cache_ttl_seconds", 30, 3600)
        if self.config.bool("group_context_enabled", True):
            ttl = max(ttl, self.config.int("group_context_ttl_seconds", 30, 86400))
        return ttl

    def history_enabled(self) -> bool:
        return bool(
            self.config.bool("group_identity_context_enabled")
            or self.config.bool("ambient_group_chat_enabled")
            or self.config.bool("group_context_enabled", True)
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
        actor_hash: str = "",
        role: str = "",
        reply_to: str = "",
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
        safe_qq = str(qq).strip()
        if not actor_hash:
            actor_hash = hashlib.sha256(
                f"{session_key}|{safe_qq}|{_safe_text(nickname, max_chars=32)}".encode(
                    "utf-8", "ignore"
                )
            ).hexdigest()[:16]
        entry = {
            "nickname": _safe_text(nickname, max_chars=32),
            "display_name": _safe_text(nickname, max_chars=32),
            "actor_hash": str(actor_hash)[:32],
            "role": _safe_text(role, max_chars=32),
            "reply_to": str(reply_to)[:32],
            "qq": str(qq).strip()
            if str(qq).strip()
            and all(ch.isalnum() or ch in "_.:-" for ch in str(qq).strip())
            and len(str(qq).strip()) <= 128
            else "",
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

    def attach_language_prefs(self, path: Any) -> None:
        """Load/persist per-member language preferences across restarts."""
        from pathlib import Path

        self._language_prefs_path = Path(path)
        try:
            raw = json.loads(self._language_prefs_path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        clean: dict[str, dict[str, str]] = {}
        for session, bucket in raw.items():
            if not isinstance(bucket, dict):
                continue
            clean[str(session)] = {
                str(key): str(value)
                for key, value in bucket.items()
                if value in {"zh", "en", "ja"}
            }
        self.member_language_prefs.update(clean)

    def _persist_member_languages(self) -> None:
        path = self._language_prefs_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(self.member_language_prefs, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            pass

    def set_member_language(
        self, session_key: str, actor_key: str, language: str
    ) -> None:
        if not session_key or not actor_key or not language:
            return
        self.member_language_prefs.setdefault(str(session_key), {})[
            str(actor_key)
        ] = str(language)
        self._persist_member_languages()

    def member_language(
        self, session_key: str, actor_key: str = "", qq: str = ""
    ) -> str:
        bucket = self.member_language_prefs.get(str(session_key), {})
        if actor_key and bucket.get(str(actor_key)):
            return str(bucket[str(actor_key)])
        if qq and bucket.get(f"qq:{qq}"):
            return str(bucket[f"qq:{qq}"])
        return ""

    def claim_model_participation(self, session_key: str, limit: int) -> bool:
        """Return True when this group may spend one model participation call."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return True
        day = time.strftime("%Y%m%d")
        key = f"{day}:{session_key}"
        used = self.model_participation_counts.get(key, 0)
        if used >= limit:
            return False
        self.model_participation_counts[key] = used + 1
        if len(self.model_participation_counts) > 512:
            for stale in list(self.model_participation_counts)[:-256]:
                self.model_participation_counts.pop(stale, None)
        return True

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

    def mark_inbound(self, key: str, ttl: float = 15.0) -> bool:
        """Return True for the first delivery of a message, False for repeats."""

        if not key:
            return True
        now = self.clock()
        seen = self.recent_inbound.get(key)
        if seen is not None and (now - seen) < ttl:
            return False
        self.recent_inbound[key] = now
        for stale, stamp in list(self.recent_inbound.items()):
            if now - stamp >= ttl:
                self.recent_inbound.pop(stale, None)
        return True

    def bump_ignored_group_message(self, session_key: str) -> int:
        count = self.ignored_group_messages.get(session_key, 0) + 1
        self.ignored_group_messages[session_key] = count
        if len(self.ignored_group_messages) > 256:
            for stale in list(self.ignored_group_messages)[:-256]:
                self.ignored_group_messages.pop(stale, None)
        return count

    def reset_ignored_group_message(self, session_key: str) -> None:
        self.ignored_group_messages.pop(session_key, None)

    def ignored_group_count(self, session_key: str) -> int:
        return int(self.ignored_group_messages.get(session_key, 0))

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
