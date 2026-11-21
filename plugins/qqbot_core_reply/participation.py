"""Per-message participation policy: explicit, probability and ambient."""

from __future__ import annotations

import re
from typing import Any

try:  # package import
    from .cooldown import CooldownGate
    from .state import RuntimeState
except ImportError:  # test harness
    from cooldown import CooldownGate
    from state import RuntimeState


class ParticipationPolicy:
    def __init__(self, config: Any, state: RuntimeState) -> None:
        self.config = config
        self.state = state

    # ------------------------------------------------------------------
    # Explicit / group eligibility
    # ------------------------------------------------------------------
    def private_enabled(self) -> bool:
        return self.config.effective_bool(
            "participation.private_enabled",
            default=True,
        )

    def mention_enabled(self) -> bool:
        return self.config.effective_bool(
            "participation.mention_enabled",
            default=True,
        )

    def group_probability(self) -> float:
        return self.config.effective_float(
            "participation.group_probability",
            "reply_probability",
            default=0.12,
            minimum=0.0,
            maximum=1.0,
        )

    def group_allowed(self, *, group_id: str, actor_id: str, bot_id: str) -> bool:
        if self.config.effective_bool("participation.ignore_self", default=True):
            if actor_id and bot_id and str(actor_id) == str(bot_id):
                return False
        if self.config.effective_bool("participation.ignore_bot", default=True):
            if actor_id and bot_id and str(actor_id) == str(bot_id):
                return False
        allowed = set(self.config.effective_list("participation.group_allow"))
        denied = set(self.config.effective_list("participation.group_deny"))
        target = str(group_id or "")
        if target and target in denied:
            return False
        if allowed and target not in allowed:
            return False
        return True

    @staticmethod
    def explicitly_addressed(
        *,
        is_private: bool,
        mentioned: bool,
        wake: bool,
        private_enabled: bool,
        mention_enabled: bool,
    ) -> bool:
        if is_private:
            return bool(private_enabled)
        return bool((mention_enabled and mentioned) or wake)

    def unified_worth_considering(
        self, message_text: str, *, active_name: str = ""
    ) -> bool:
        """Cheap social gate before the full unified generation.

        This is not a probability decision: it only filters obvious
        statements that are not addressed to the bot at all.  The model still
        decides whether to speak for questions/mentions/name calls.
        """
        text = str(message_text or "").strip()
        if not text:
            return False
        folded = text.casefold()
        names = [
            str(name).strip()
            for name in self.config.effective_list("participation.unified_names")
            if str(name).strip()
        ]
        active_name = str(active_name or "").strip()
        if active_name and active_name not in names:
            names.insert(0, active_name)
        # An old/global account name must not keep waking a different persona.
        blocked = [
            str(name).strip()
            for name in self.config.effective_list(
                "participation.unified_block_names"
            )
            if str(name).strip() and str(name).strip() != active_name
        ]
        if any(name.casefold() in folded for name in blocked):
            return False
        if not self.config.effective_bool(
            "participation.unified_addressed_only", default=True
        ):
            return True
        named = any(name.casefold() in folded for name in names)
        if not named and self.config.effective_bool(
            "participation.unified_require_name", default=False
        ):
            return False
        if named:
            # A bare name call ("电子羊") still deserves a short "在".
            remainder = text
            for name in names:
                remainder = re.sub(re.escape(name), "", remainder, flags=re.IGNORECASE)
            remainder = re.sub(r"[\s，。！？、,.!?;；:：~～]+", "", remainder)
            if not remainder:
                return True
        markers = self.config.effective_list(
            "participation.unified_address_markers"
        )
        if any(str(marker) and str(marker) in text for marker in markers):
            return True
        request_words = (
            "帮", "请", "能不能", "可以", "怎么", "为什么", "要不要", "建议",
            "怎么办", "教我", "告诉我", "吗", "呢",
        )
        if any(word in text for word in request_words):
            return True
        # A name plus a plain statement ("电子羊，今天天气不错") is not
        # enough; quiet personas should be allowed to stay silent.
        return False

    def unified_cooldown_remaining(self, event: Any) -> int:
        session_key, user_key = self.cooldown_keys(event)
        group_cd, user_cd = CooldownGate.effective_cooldowns(self.config)
        return CooldownGate(self.state).remaining(
            session_key=session_key,
            user_key=user_key,
            group_cooldown=group_cd,
            user_cooldown=user_cd,
        )

    def mark_unified_reply(self, event: Any) -> None:
        now = self.state.clock()
        session_key, user_key = self.cooldown_keys(event)
        self.state.last_group_reply[session_key] = now
        self.state.last_user_reply[user_key] = now

    def group_random_eligible(
        self,
        message_text: str,
        *,
        has_image: bool,
        has_face: bool,
    ) -> bool:
        stripped = str(message_text or "").strip()
        if stripped:
            return len(stripped) >= self.config.int("min_message_chars", 0)
        if has_image and not self.config.bool("ignore_pure_image"):
            return True
        if has_face and not self.config.bool("ignore_pure_face"):
            return True
        return False

    # ------------------------------------------------------------------
    # Ambient participation
    # ------------------------------------------------------------------
    def ambient_should_participate(
        self,
        event: Any,
        message_text: str,
        *,
        has_image: bool,
        has_face: bool,
        session_key: str,
    ) -> bool:
        if not self.config.bool("ambient_group_chat_enabled"):
            return False
        if self._is_private(event):
            return False
        if not self.group_random_eligible(
            message_text,
            has_image=has_image,
            has_face=has_face,
        ):
            return False
        now = self.state.clock()
        self.state.prune(now)
        min_interval = self.config.int("ambient_group_min_interval_seconds", 0)
        last = self.state.ambient_last_at.get(session_key)
        if last is not None and now - last < min_interval:
            return False
        round_interval = self.config.int("ambient_group_round_interval", 0)
        count = self.state.ambient_message_count.get(session_key, 0)
        if round_interval > 0 and count % round_interval != 0:
            return False
        keywords = self.config.string_list("ambient_group_keywords")
        folded = str(message_text or "").casefold()
        keyword_hit = any(
            keyword.casefold() in folded
            for keyword in keywords
            if keyword
        )
        probability = (
            self.config.float(
                "ambient_group_keyword_probability",
                0.0,
                1.0,
            )
            if keyword_hit
            else self.group_probability()
        )
        if probability <= 0:
            return False
        if probability < 1.0 and self.state.rng.random() >= probability:
            return False
        self.state.ambient_last_at[session_key] = now
        return True

    @staticmethod
    def _is_private(event: Any) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Cooldown keys
    # ------------------------------------------------------------------
    def cooldown_keys(self, event: Any) -> tuple[str, str]:
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        return session_key, f"{session_key}:{sender_id}"
