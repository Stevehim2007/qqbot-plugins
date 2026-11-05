"""Cooldown and reply-slot reservation logic."""

from __future__ import annotations

import math
from typing import Any

try:  # package import
    from .state import RuntimeState
except ImportError:  # test harness
    from state import RuntimeState


class CooldownGate:
    """Atomic in-process cooldown slots before a slow model call."""

    def __init__(self, state: RuntimeState) -> None:
        self.state = state

    @staticmethod
    def remaining_from(last: float | None, now: float, cooldown: float) -> float:
        if last is None:
            return 0.0
        return max(0.0, float(cooldown) - (float(now) - float(last)))

    def remaining(
        self,
        *,
        session_key: str,
        user_key: str,
        group_cooldown: float,
        user_cooldown: float,
    ) -> int:
        now = self.state.clock()
        remaining = max(
            self.remaining_from(
                self.state.last_group_reply.get(session_key),
                now,
                group_cooldown,
            ),
            self.remaining_from(
                self.state.last_user_reply.get(user_key),
                now,
                user_cooldown,
            ),
        )
        if remaining <= 0:
            return 0
        return max(1, int(math.ceil(remaining)))

    def try_claim_random(
        self,
        *,
        session_key: str,
        user_key: str,
        group_cooldown: float,
        user_cooldown: float,
        probability: float,
    ) -> bool:
        now = self.state.clock()
        self.state.prune(now)
        if self.remaining(
            session_key=session_key,
            user_key=user_key,
            group_cooldown=group_cooldown,
            user_cooldown=user_cooldown,
        ):
            return False
        probability = max(0.0, min(1.0, float(probability)))
        if probability <= 0:
            return False
        if probability < 1.0 and self.state.rng.random() >= probability:
            return False
        # Reserve both slots before the slow model call.
        self.state.last_group_reply[session_key] = now
        self.state.last_user_reply[user_key] = now
        return True

    def song_remaining(
        self,
        *,
        session_key: str,
        user_key: str,
        group_cooldown: float,
        user_cooldown: float,
    ) -> int:
        remaining = self.remaining(
            session_key=session_key,
            user_key=user_key,
            group_cooldown=group_cooldown,
            user_cooldown=user_cooldown,
        )
        return remaining

    def claim_song(
        self,
        *,
        session_key: str,
        user_key: str,
        group_cooldown: float,
        user_cooldown: float,
    ) -> int:
        remaining = self.song_remaining(
            session_key=session_key,
            user_key=user_key,
            group_cooldown=group_cooldown,
            user_cooldown=user_cooldown,
        )
        if remaining > 0:
            return remaining
        now = self.state.clock()
        self.state.last_song_group[session_key] = now
        self.state.last_song_user[user_key] = now
        return 0

    @staticmethod
    def effective_cooldowns(config: Any, key_mode: str = "legacy_compatible") -> tuple[float, float]:
        """Return (group, user) seconds for canonical and legacy config."""

        group = config.effective_float(
            "cooldown.seconds",
            "group_cooldown_seconds",
            default=30.0,
            minimum=0.0,
            maximum=3600.0,
        )
        user = float(config.int("user_cooldown_seconds", 0, 3600))
        mode = str(key_mode or "legacy_compatible").strip().lower()
        if mode in {"user"}:
            group = 0.0
        elif mode in {"group", "conversation", "conversation_persona", "instance"}:
            user = 0.0
        return group, user
