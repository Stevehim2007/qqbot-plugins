"""Reply orchestration: one turn, one model gateway, one delivery lease."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

try:  # package import
    from . import command_exclusion as command_engine
    from . import contracts
    from .contracts import ContractError, ContractResponse, ReplyPlan, RequestContext
    from .model_gateway import ModelGateway
    from .persona import PersonaService
    from .state import RuntimeState
except ImportError:  # test harness
    import command_exclusion as command_engine
    import contracts
    from contracts import ContractError, ContractResponse, ReplyPlan, RequestContext
    from model_gateway import ModelGateway
    from persona import PersonaService
    from state import RuntimeState


CANDIDATE_EXTRA = "qqbot_core_reply_candidate"
ALLOW_STICKER_EXTRA = "qqbot_core_reply_allow_sticker"
TEXT_EXTRA = "qqbot_core_reply_text"
AMBIENT_EXTRA = "qqbot_core_reply_ambient"
TURN_EXTRA = "qqbot_core_reply_turn_id"
UNIFIED_PARTICIPATION_EXTRA = "qqbot_core_reply_unified_participation"
REQUEST_CONTEXT_EXTRA = "qqbot_core_reply_request_context"
MODEL_ROUND_EXTRA = "qqbot_core_reply_model_round"
MEMORY_MODE_EXTRA = "qqbot_core_reply_memory_mode"
OUTPUT_PROCESSED_EXTRA = "qqbot_core_reply_output_processed"
OUTPUT_ERROR_EXTRA = "qqbot_core_reply_output_error"
MEMORY_TURN_GRACE_SECONDS = 15
logger = logging.getLogger(__name__)


@dataclass
class HandleOutcome:
    results: list[Any] = field(default_factory=list)
    stop: bool = False
    turn_id: str = ""
    mode: str = ""
    memory_mode: bool = False
    blocked_native: bool = False


class ReplyPipeline:
    def __init__(
        self,
        context: Any,
        config: Any,
        state: RuntimeState,
        participation: Any,
        cooldown: Any,
        persona: PersonaService,
        gateway: ModelGateway,
        owner_token: str,
    ) -> None:
        self.context = context
        self.config = config
        self.state = state
        self.participation = participation
        self.cooldown = cooldown
        self.persona = persona
        self.gateway = gateway
        self.owner_token = owner_token

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------
    @staticmethod
    def components(event: Any) -> list[Any]:
        try:
            value = event.get_messages()
        except Exception:
            return []
        try:
            return list(value)
        except TypeError:
            return []

    @staticmethod
    def is_component(component: Any, name: str) -> bool:
        try:
            from astrbot.api import message_components as Comp
        except Exception:
            return False
        cls = getattr(Comp, name, None)
        return bool(cls is not None and isinstance(component, cls))

    def unified_metadata(self, event: Any, active_name: str = "") -> str:
        """Compact, privacy-safe addressee metadata for unified participation.

        The model cannot reliably infer whether a short question was aimed at
        it from text alone.  In particular, "@someone-else" and quoted
        conversations look like ordinary questions.  Give the model the
        social facts, not raw QQ/openid values, and let it decide.
        """
        components = self.components(event)
        at_you = False
        try:
            at_you = self.is_bot_mentioned(event, components)
        except Exception:
            at_you = False
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        roster: dict[str, str] = {}
        try:
            session = str(getattr(event, "unified_msg_origin", "") or "")
            for item in self.state.history_for(session)[-20:]:
                qq = str(item.get("qq") or "").strip()
                display = str(
                    item.get("display_name") or item.get("nickname") or ""
                ).strip()
                if qq and display:
                    roster[qq] = display[:20]
        except Exception:
            roster = {}
        at_others = 0
        at_targets: list[str] = []
        quoted = False
        for component in components:
            try:
                if self.is_component(component, "AtAll"):
                    at_others += 1
                    if "全体成员" not in at_targets:
                        at_targets.append("全体成员")
                    continue
                if self.is_component(component, "At"):
                    qq = str(getattr(component, "qq", "") or "").strip()
                    if qq and qq != self_id and qq.lower() not in {"all", "everyone"}:
                        at_others += 1
                        name = str(getattr(component, "name", "") or "").strip()
                        name = name or roster.get(qq, "")
                        name = name[:20] or "其他成员"
                        if name not in at_targets and len(at_targets) < 4:
                            at_targets.append(name)
                    continue
                if self.is_component(component, "Reply"):
                    quoted = True
            except Exception:
                continue
        try:
            sender = str(event.get_sender_name() or "").strip()[:32] or "群友"
        except Exception:
            sender = "群友"
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        name = str(active_name or "").strip() or "当前人格"
        return (
            f"[本条消息元数据] 当前人格={name}；发送者={sender}；"
            f"@你={'是' if at_you else '否'}；@其他人数量={at_others}；"
            f"@对象={('、'.join(at_targets) if at_targets else '无')}；"
            f"引用消息={'是' if quoted else '否'}；"
            f"会话类型={'私聊' if is_private else '群聊'}。"
        )

    def is_private(self, event: Any) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    def valid_chat(self, event: Any) -> bool:
        if self.is_private(event):
            return True
        return bool(self.persona._safe_group_id(event))

    def original_text(self, event: Any, fallback: str) -> str:
        raw = str(getattr(event, "message_str", "") or "")
        return raw if raw else str(fallback or "")

    def message_text(self, event: Any) -> str:
        parts: list[str] = []
        for component in self.components(event):
            if self.is_component(component, "Plain"):
                text = getattr(component, "text", "")
                if text:
                    parts.append(str(text))
        text = " ".join(parts).strip()
        if not text:
            text = str(getattr(event, "message_str", "") or "").strip()
        limits = [
            self.config.effective_int(
                "context.max_chars",
                "max_input_chars",
                default=2000,
                minimum=0,
                maximum=10000,
            ),
            self.config.int("max_input_chars", 0, 10000),
            self.config.int("max_message_chars", 0, 10000),
        ]
        positive = [limit for limit in limits if limit > 0]
        return text[: min(positive)] if positive else text

    def is_bot_mentioned(self, event: Any, components: list[Any]) -> bool:
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            return False
        if not self_id:
            return False
        for component in components:
            if self.is_component(component, "AtAll"):
                continue
            if not self.is_component(component, "At"):
                continue
            qq = str(getattr(component, "qq", "") or "").strip()
            if not qq or qq.lower() in {"all", "everyone"}:
                continue
            if qq == self_id:
                return True
        return False

    def command_prefixes(self, event: Any) -> list[str]:
        global_wake = []
        getter = getattr(self.context, "get_config", None)
        if callable(getter):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            try:
                try:
                    global_config = getter(umo=umo)
                except TypeError:
                    global_config = getter()
                if isinstance(global_config, dict):
                    global_wake = global_config.get("wake_prefix", [])
                else:
                    global_wake = getattr(global_config, "wake_prefix", [])
            except Exception:
                global_wake = []
        return command_engine.command_prefixes(
            self.config.string_list("ignore_command_prefixes"),
            inherit_wake_prefix=self.config.bool("inherit_astrbot_wake_prefix", True),
            global_wake_prefix=global_wake,
        )

    def command_match(self, event: Any, text: str) -> Any:
        raw = str(getattr(event, "message_str", "") or "")
        prefixes = self.command_prefixes(event)
        extra = self.config.string_list("commands.exclude_aliases")
        for value in (text, raw):
            match = command_engine.parse_root_command(
                value,
                prefixes,
                extra_aliases=extra,
            )
            if match is not None:
                return match
        return None

    def block_default_llm(self, event: Any) -> None:
        # Must happen before any participation/command/readiness return so the
        # native AstrBot chat path cannot spend a second model call.
        try:
            event.should_call_llm(True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Candidate contract
    # ------------------------------------------------------------------
    def mark_candidate(self, event: Any, original_text: str, *, ambient: bool) -> None:
        event.set_extra(CANDIDATE_EXTRA, True)
        event.set_extra(ALLOW_STICKER_EXTRA, True)
        event.set_extra(TEXT_EXTRA, str(original_text or ""))
        if ambient:
            event.set_extra(AMBIENT_EXTRA, True)

    def remember_history(
        self,
        event: Any,
        message_text: str,
        *,
        has_image: bool,
        has_face: bool,
    ) -> None:
        if self.is_private(event):
            return
        self.state.remember_history(
            str(getattr(event, "unified_msg_origin", "") or ""),
            nickname=self.persona._safe_identity_nickname(event),
            qq=self.persona._safe_identity_qq(event),
            text=message_text,
            has_image=has_image,
            has_face=has_face,
            actor_hash=self.persona.actor_hash(event),
            role=str(getattr(event, "member_role", "") or ""),
        )

    _LANGUAGE_LABELS = {
        "中文": "zh",
        "汉语": "zh",
        "英文": "en",
        "英语": "en",
        "日文": "ja",
        "日语": "ja",
    }

    def record_member_language_preferences(
        self, event: Any, text: str, session_key: str
    ) -> None:
        """Remember explicit language requests per group member.

        The model can only reliably attribute who a language preference belongs
        to when the instruction and the member are available as structured
        facts.  This parser is deliberately limited to explicit language
        requests; it does not decide whether to reply or change favorability.
        """
        value = str(text or "")
        if not value or not session_key:
            return
        language_re = r"(中文|汉语|英文|英语|日文|日语)"
        self_match = re.search(
            r"(?:跟我|和我|对我说|跟我说话)[^，。；,;\n]{0,8}?" + language_re,
            value,
        )
        if self_match:
            try:
                qq = str(event.get_sender_id() or "").strip()
            except Exception:
                qq = ""
            actor = self.persona.actor_hash(event)
            key = actor or (f"qq:{qq}" if qq else "")
            if key:
                self.state.set_member_language(
                    session_key, key, self._LANGUAGE_LABELS[self_match.group(1)]
                )
        at_targets: list[str] = []
        try:
            for component in self.components(event):
                if self.is_component(component, "At"):
                    qq = str(getattr(component, "qq", "") or "").strip()
                    if qq:
                        try:
                            self_id = str(event.get_self_id() or "").strip()
                        except Exception:
                            self_id = ""
                        if qq != self_id:
                            at_targets.append(qq)
        except Exception:
            at_targets = []
        if not at_targets:
            return
        target_matches = re.findall(
            r"(?:跟|和|对)[^，。；,;\n]{0,24}?" + language_re,
            value,
        )
        if not target_matches:
            return
        # The last clause is the one that follows the At target in
        # "跟我说中文，跟 @X 说英语".  The first "跟" belongs to self.
        target_qq = at_targets[0]
        language = self._LANGUAGE_LABELS[target_matches[-1]]
        target_actor = ""
        try:
            for item in reversed(self.state.history_for(session_key)):
                if str(item.get("qq") or "") == target_qq:
                    target_actor = str(item.get("actor_hash") or "")
                    if target_actor:
                        break
        except Exception:
            target_actor = ""
        self.state.set_member_language(
            session_key, target_actor or f"qq:{target_qq}", language
        )

    def history_for(self, event: Any) -> list[dict[str, Any]]:
        if self.is_private(event):
            return []
        return self.state.history_for(str(getattr(event, "unified_msg_origin", "") or ""))

    async def model_participation(
        self,
        event: Any,
        text: str,
        history: list[dict[str, Any]],
        policy_params: dict[str, Any] | None = None,
    ) -> str:
        """Return speak/observe/fallback for model-driven group participation."""
        params = policy_params or {}
        selected_mode = str(
            params.get("participation.mode")
            or self.config.string("participation.mode", "probability")
        ).lower()
        if selected_mode != "model":
            return "fallback"
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        if not self.state.claim_model_participation(
            session_key,
            self.config.int("participation.model_daily_limit", 0, 5000),
        ):
            logger.info("model participation daily limit reached group=%s", session_key)
            return "observe"
        active_name = ""
        persona_prompt = ""
        try:
            persona_prompt, active_name, _source = self.persona.resolve_event_persona(
                event
            )
        except Exception:
            pass
        metadata = self.unified_metadata(event, active_name)
        decision = await self.gateway.decide_participation(
            event,
            message_text=text,
            chat_history=history,
            persona_prompt=str(persona_prompt or ""),
            active_name=str(active_name or ""),
            metadata=metadata,
        )
        action = str(decision.get("action") or "")
        if action not in {"speak", "observe"}:
            logger.info(
                "model participation fallback: %s", decision.get("reason_code", "")
            )
            return "fallback"
        if action == "observe":
            logger.info(
                "model participation observe confidence=%.3f reason=%s",
                float(decision.get("confidence", 0) or 0),
                decision.get("reason_code", ""),
            )
            return "observe"
        threshold = float(
            params.get(
                "participation.model_confidence",
                self.config.float("participation.model_confidence", 0.0, 1.0),
            )
        )
        if float(decision.get("confidence", 0) or 0) < threshold:
            logger.info(
                "model participation low confidence=%.3f threshold=%.3f",
                float(decision.get("confidence", 0) or 0),
                threshold,
            )
            return "observe"
        group_cooldown, user_cooldown = self.cooldown.effective_cooldowns(
            self.config,
            self.config.string("cooldown.key_mode", "legacy_compatible"),
        )
        if not self.cooldown.try_claim_random(
            session_key=session_key,
            user_key=self.participation.cooldown_keys(event)[1],
            group_cooldown=group_cooldown,
            user_cooldown=user_cooldown,
            probability=1.0,
        ):
            return "observe"
        logger.info(
            "model participation speak confidence=%.3f reason=%s",
            float(decision.get("confidence", 0) or 0),
            decision.get("reason_code", ""),
        )
        return "speak"


    # ------------------------------------------------------------------
    # Request/turn construction
    # ------------------------------------------------------------------
    def request_context(
        self,
        event: Any,
        *,
        model_round: int = 0,
        capability: str = "chat.reply",
    ) -> RequestContext:
        platform = str(getattr(event, "get_platform_id", lambda: "")() or "")
        try:
            bot_id = str(event.get_self_id() or "")
        except Exception:
            bot_id = ""
        try:
            actor_id = str(event.get_sender_id() or "")
        except Exception:
            actor_id = ""
        group_id = self.persona._safe_group_id(event)
        conversation_id = group_id or str(getattr(event, "unified_msg_origin", "") or "")
        persona_id = ""
        try:
            _prompt, _name, source = self.persona.resolve_event_persona(event)
            persona_id = source
        except Exception:
            persona_id = ""
        source_message_id = ""
        for getter_name in ("get_message_id", "message_id"):
            value = getattr(event, getter_name, None)
            if callable(value):
                try:
                    source_message_id = str(value() or "")
                except Exception:
                    source_message_id = ""
            elif value is not None:
                source_message_id = str(value)
            if source_message_id:
                break
        if not source_message_id:
            source_message_id = hashlib.sha256(
                f"{getattr(event, 'unified_msg_origin', '')}:{self.state.clock()}".encode(
                    "utf-8", "ignore"
                )
            ).hexdigest()[:24]
        turn_id = uuid.uuid4().hex
        deadline = self.state.clock() + max(
            1, self.config.effective_int(
                "llm.timeout_seconds",
                "llm_timeout_seconds",
                default=45,
                minimum=1,
                maximum=300,
            )
        )
        context = RequestContext(
            protocol_version=contracts.PROTOCOL_VERSION,
            instance_id=str(getattr(self.context, "instance_id", "") or "default"),
            platform=platform or "qq",
            bot_id=bot_id,
            actor_id=actor_id,
            conversation_id=conversation_id,
            persona_id=persona_id,
            source_message_id=source_message_id,
            turn_id=turn_id,
            capability=capability,
            requested_scope={
                "kind": "private" if self.is_private(event) else "group",
                "group_id": group_id,
                "actor_name": str(
                    getattr(event, "get_sender_name", lambda: "")() or ""
                )[:64],
            },
            owner_epoch=int(self.state.owner_epoch),
            deadline=deadline,
        )
        context.call_id = f"{turn_id}:{model_round}"
        context.model_round = int(model_round)
        context.delivery_key = f"reply:{turn_id}"
        return context

    # ------------------------------------------------------------------
    # Capability policy
    # ------------------------------------------------------------------
    def _capability_policy(
        self,
        plugin_id: str,
        *,
        group_id: str = "",
        user_id: str = "",
        is_private: bool = False,
    ) -> dict[str, Any] | None:
        star = self.gateway.safe_tools_star()
        getter = getattr(star, "policy_effective", None)
        if not callable(getter):
            return None
        try:
            value = getter(
                plugin_id,
                group_id=group_id or None,
                user_id=user_id or None,
                is_private=is_private,
            )
        except TypeError:
            try:
                value = getter(plugin_id)
            except Exception:
                return None
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    # ------------------------------------------------------------------
    # Message entry
    # ------------------------------------------------------------------
    async def handle_message(self, event: Any) -> HandleOutcome | None:
        enabled = self.config.bool("enabled", False)
        if not enabled or self.state.terminated:
            self.block_default_llm(event)
            return None
        try:
            components = self.components(event)
            if not self.valid_chat(event):
                self.block_default_llm(event)
                return None
            text = self.message_text(event)
            has_image = any(self.is_component(c, "Image") for c in components)
            has_face = any(self.is_component(c, "Face") for c in components)
            original = self.original_text(event, text)
            if not str(text or "").strip() and not has_image and not has_face:
                # A bare @ mention (or an empty event) must not claim the
                # single memory turn.  It otherwise blocks the next real
                # message until the watchdog expires.
                self.block_default_llm(event)
                return None

            match = self.command_match(event, text)
            if match is not None:
                # Registered command handlers own their output; unknown
                # command-like input is terminal and never becomes a prompt.
                self.block_default_llm(event)
                return None

            is_private = self.is_private(event)
            try:
                actor_id = str(event.get_sender_id() or "")
                bot_id = str(event.get_self_id() or "")
            except Exception:
                actor_id = bot_id = ""
            group_id = self.persona._safe_group_id(event)
            policy = self._capability_policy(
                "qqbot_core_reply",
                group_id=group_id,
                user_id=actor_id,
                is_private=is_private,
            )
            if policy is not None and not bool(policy.get("enabled", True)):
                self.block_default_llm(event)
                return None
            if not is_private and not self.participation.group_allowed(
                group_id=group_id,
                actor_id=actor_id,
                bot_id=bot_id,
            ):
                self.block_default_llm(event)
                return None
            if is_private and not self.participation.private_enabled():
                self.block_default_llm(event)
                return None
            mentioned = self.is_bot_mentioned(event, components)
            wake = bool(getattr(event, "is_at_or_wake_command", False))
            explicit = self.participation.explicitly_addressed(
                is_private=is_private,
                mentioned=mentioned,
                wake=wake,
                private_enabled=self.participation.private_enabled(),
                mention_enabled=self.participation.mention_enabled(),
            )
            policy_params = (
                dict(policy.get("params") or {}) if isinstance(policy, dict) else {}
            )
            group_probability = self.participation.group_probability()
            if policy_params:
                try:
                    group_probability = float(
                        policy_params.get(
                            "participation.reply_probability",
                            group_probability,
                        )
                    )
                except (TypeError, ValueError):
                    pass
            participation_mode = str(
                policy_params.get("participation.mode")
                or self.config.string("participation.mode", "probability")
            ).lower()
            if (
                not is_private
                and not mentioned
                and group_probability <= 0
                and not self.config.bool("ambient_group_chat_enabled")
                and participation_mode not in {"model", "unified"}
            ):
                logger.info(
                    "group reply blocked: no mention and probability=0 group=%s text=%r",
                    group_id,
                    str(text or "")[:80],
                )
                self.block_default_llm(event)
                return None
            history = self.history_for(event)
            self.remember_history(
                event,
                text,
                has_image=has_image,
                has_face=has_face,
            )
            mode = ""
            if not explicit and not is_private and participation_mode == "unified":
                try:
                    _persona_prompt, active_persona_name, _persona_source = (
                        self.persona.resolve_event_persona(event)
                    )
                except Exception:
                    active_persona_name = ""
                if not self.participation.unified_worth_considering(
                    text,
                    active_name=str(active_persona_name or ""),
                    policy_params=policy_params,
                ):
                    logger.info(
                        "unified participation skipped: not addressed group=%s text=%r",
                        group_id,
                        str(text or "")[:80],
                    )
                    self.block_default_llm(event)
                    return None
                remaining = self.participation.unified_cooldown_remaining(event)
                if remaining > 0:
                    logger.info(
                        "unified participation skipped: cooldown remaining=%ss group=%s",
                        remaining,
                        group_id,
                    )
                    self.block_default_llm(event)
                    return None
                event.set_extra(UNIFIED_PARTICIPATION_EXTRA, True)
                mode = "unified"
            elif not explicit and not is_private and participation_mode == "model":
                decision = await self.model_participation(
                    event, text, history, policy_params=policy_params
                )
                if decision == "observe":
                    self.block_default_llm(event)
                    return None
                if decision == "speak":
                    mode = "model"
                else:
                    decision = ""
                if not mode:
                    # Model failed: fall through to the old probability path.
                    pass
            if mode:
                # unified/model already selected a participation path.
                pass
            elif not explicit and not is_private:
                session_key = str(getattr(event, "unified_msg_origin", "") or "")
                if self.config.bool("ambient_group_chat_enabled"):
                    self.state.bump_ambient_message_count(session_key)
                    if not self.participation.ambient_should_participate(
                        event,
                        text,
                        has_image=has_image,
                        has_face=has_face,
                        session_key=session_key,
                    ):
                        self.block_default_llm(event)
                        return None
                    mode = "ambient"
                else:
                    if not self.participation.group_random_eligible(
                        text,
                        has_image=has_image,
                        has_face=has_face,
                    ):
                        self.block_default_llm(event)
                        return None
                    group_cooldown, user_cooldown = self.cooldown.effective_cooldowns(
                        self.config,
                        self.config.string("cooldown.key_mode", "legacy_compatible"),
                    )
                    probability = self.participation.group_probability()
                    if policy is not None:
                        try:
                            probability = float(
                                policy.get("params", {}).get(
                                    "participation.reply_probability",
                                    probability,
                                )
                            )
                        except (TypeError, ValueError):
                            pass
                    if not self.cooldown.try_claim_random(
                        session_key=session_key,
                        user_key=self.participation.cooldown_keys(event)[1],
                        group_cooldown=group_cooldown,
                        user_cooldown=user_cooldown,
                        probability=probability,
                    ):
                        self.block_default_llm(event)
                        return None
                    mode = "random"
            else:
                if (
                    (has_image or has_face)
                    and not text.strip()
                    and self.config.bool("user_sticker_reaction_enabled", False)
                ):
                    probability = self.config.float(
                        "user_sticker_reaction_probability", 0.0, 1.0
                    )
                    if probability > 0 and (
                        probability >= 1.0 or self.state.rng.random() < probability
                    ):
                        reply_text = self.gateway.finalize_text(
                            self._pick_reaction_reply()
                        )
                        self.block_default_llm(event)
                        self.mark_candidate(event, original, ambient=False)
                        return HandleOutcome(
                            results=[event.plain_result(reply_text)],
                            stop=True,
                            mode="explicit",
                        )
                mode = "explicit"

            if self.persona.memory_mode() == "astrbot":
                return await self.run_memory_turn(event, original, text, mode)

            self.block_default_llm(event)
            request_context = self.request_context(event)
            response = await self.turn_run(
                request_context,
                {
                    "event": event,
                    "message_text": text,
                    "has_face": has_face,
                    "has_image": has_image,
                    "chat_history": history,
                    "delivery_key": f"reply:{request_context.turn_id}",
                    "unified_participation": bool(
                        event.get_extra(UNIFIED_PARTICIPATION_EXTRA)
                    ),
                },
            )
            plan = self._plan_from_response(response)
            if plan is None:
                fallback = self.gateway.finalize_text(
                    self.config.string("llm_error_reply", "核心回复失败。")
                )
                self.mark_candidate(event, original, ambient=(mode == "ambient"))
                return HandleOutcome(
                    results=[event.plain_result(fallback)],
                    stop=True,
                    mode=mode,
                )
            if (
                event.get_extra(UNIFIED_PARTICIPATION_EXTRA) is True
                and "[[NO_REPLY]]" in self.gateway.plan_text(plan)
            ):
                try:
                    self.state.finish_turn(
                        request_context.turn_id,
                        status="cancelled",
                        delivery_state="not_sent",
                    )
                except Exception:
                    pass
                self.block_default_llm(event)
                return HandleOutcome(
                    results=[],
                    stop=True,
                    turn_id=request_context.turn_id,
                    mode=mode,
                )
            self.mark_candidate(event, original, ambient=(mode == "ambient"))
            if event.get_extra(UNIFIED_PARTICIPATION_EXTRA) is True:
                self.participation.mark_unified_reply(event)
            result = self.render_plan(event, plan)
            return HandleOutcome(
                results=[result],
                stop=True,
                turn_id=request_context.turn_id,
                mode=mode,
            )
        except Exception:
            self.block_default_llm(event)
            return None

    async def run_memory_turn(
        self,
        event: Any,
        original_text: str,
        message_text: str,
        mode: str,
    ) -> HandleOutcome:
        try:
            await self.persona.sync_agent_persona(event)
        except Exception:
            logger.warning("qqbot_core_reply agent persona sync failed; continuing")
        request_context = self.request_context(
            event,
            capability="chat.memory",
            model_round=0,
        )
        try:
            record, created = self.state.claim_turn(
                request_context,
                delivery_key=f"reply:{request_context.turn_id}",
                controller="astrbot_default_agent",
                model_round=0,
            )
        except ContractError:
            self.block_default_llm(event)
            return None
        self.mark_candidate(event, original_text, ambient=(mode == "ambient"))
        logger.debug("qqbot_core_reply memory turn: created=%s mode=%s", created, mode)
        if created:
            timeout = self.config.effective_int(
                "llm.timeout_seconds",
                "llm_timeout_seconds",
                default=45,
                minimum=1,
                maximum=300,
            )
            watchdog = asyncio.create_task(
                self._expire_memory_turn(
                    request_context.turn_id,
                    float(timeout) + MEMORY_TURN_GRACE_SECONDS,
                )
            )
            record.task = watchdog
            self.state.register_task(watchdog)
        event.set_extra(MEMORY_MODE_EXTRA, True)
        event.set_extra(TURN_EXTRA, request_context.turn_id)
        event.set_extra(MODEL_ROUND_EXTRA, 0)
        event.set_extra(REQUEST_CONTEXT_EXTRA, request_context.to_dict())
        try:
            event.is_at_or_wake_command = True
        except Exception:
            pass
        return HandleOutcome(
            results=[],
            stop=False,
            turn_id=request_context.turn_id,
            mode=mode,
            memory_mode=True,
        )

    def _pick_reaction_reply(self) -> str:
        replies = [
            str(item).strip()
            for item in self.config.string_list("user_sticker_reaction_replies")
            if str(item).strip()
        ]
        if not replies:
            return "收到啦～"
        return self.state.rng.choice(replies)

    def _plan_from_response(self, response: ContractResponse) -> ReplyPlan | None:
        if not isinstance(response, ContractResponse):
            return None
        data = response.data if isinstance(response.data, dict) else {}
        plan_payload = data.get("reply_plan")
        if plan_payload is None:
            return None
        try:
            return contracts.validate_reply_plan(plan_payload)
        except ContractError:
            return None

    # ------------------------------------------------------------------
    # The single turn controller (shared by event path and provider ABI)
    # ------------------------------------------------------------------
    async def turn_run(
        self,
        request_context: RequestContext,
        args: dict[str, Any] | None = None,
    ) -> ContractResponse:
        args = dict(args or {})
        try:
            request_context = contracts.load_request_context(request_context)
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        if self.state.terminated or not self.state.ready:
            return contracts.fail("not_ready", "core_reply provider is not ready")
        if int(request_context.owner_epoch) != int(self.state.owner_epoch):
            return contracts.fail(
                "owner_epoch_mismatch",
                "owner_epoch does not match the committed owner",
            )
        now = self.state.clock()
        if request_context.deadline and now > request_context.deadline:
            return contracts.fail("deadline_exceeded", "request deadline exceeded")
        model_round = int(args.get("model_round", getattr(request_context, "model_round", 0)) or 0)
        delivery_key = str(
            args.get("delivery_key")
            or getattr(request_context, "delivery_key", "")
            or f"reply:{request_context.turn_id}"
        )
        expected_call_id = str(
            args.get("call_id")
            or getattr(request_context, "call_id", "")
            or f"{request_context.turn_id}:{model_round}"
        )
        try:
            record, created = self.state.claim_turn(
                request_context,
                delivery_key=delivery_key,
                model_round=model_round,
            )
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        if not created:
            if record.delivery_state in {"outcome_unknown", "delivery_unknown"}:
                return contracts.ContractResponse(
                    "outcome_unknown",
                    {
                        "turn_id": record.turn_id,
                        "delivery_key": record.delivery_key,
                        "delivery_state": record.delivery_state,
                        "reply_plan": (
                            record.plan.to_dict()
                            if isinstance(record.plan, ReplyPlan)
                            else record.plan
                        ),
                    },
                    "delivery_unknown",
                    "previous delivery outcome is unknown; do not resend",
                )
            if record.plan is not None:
                return contracts.ok(
                    {
                        "turn_id": record.turn_id,
                        "delivery_key": record.delivery_key,
                        "reply_plan": (
                            record.plan.to_dict()
                            if isinstance(record.plan, ReplyPlan)
                            else record.plan
                        ),
                        "text": (
                            self.gateway.plan_text(record.plan)
                            if isinstance(record.plan, ReplyPlan)
                            else ""
                        ),
                        "idempotent": True,
                    }
                )
            return contracts.fail("turn_conflict", "turn is already running")
        try:
            record.task = asyncio.current_task()
            if record.task is not None:
                self.state.register_task(record.task)
        except Exception:
            record.task = None
        try:
            timeout = None
            if request_context.deadline:
                timeout = max(0.001, float(request_context.deadline) - self.state.clock())
            generation = self.gateway.generate_from_request(request_context, args)
            if inspect.isawaitable(generation):
                if timeout is not None:
                    model_output = await asyncio.wait_for(generation, timeout=timeout)
                else:
                    model_output = await generation
            else:
                model_output = generation
        except asyncio.TimeoutError:
            self.state.finish_turn(
                request_context.turn_id,
                status="error",
                delivery_state="not_sent",
                error_code="timeout",
            )
            return contracts.fail("timeout", "model call timed out before delivery")
        except ContractError as exc:
            self.state.finish_turn(
                request_context.turn_id,
                status="error",
                delivery_state="not_sent",
                error_code=exc.error_code,
            )
            return contracts.fail(exc.error_code, exc.message)
        except Exception:
            self.state.finish_turn(
                request_context.turn_id,
                status="error",
                delivery_state="not_sent",
                error_code="internal_error",
            )
            return contracts.fail("internal_error", "model call failed")
        try:
            processed = await self.gateway.process_model_output(
                request_context,
                model_output,
                expected_call_id=expected_call_id,
                expected_delivery_key=delivery_key,
            )
        except ContractError as exc:
            fallback_text = self.gateway.finalize_text(
                self.config.string("empty_reply_fallback", "嗯嗯，我在听。")
            )
            fallback_plan = self.gateway._fallback_plan(
                request_context, fallback_text, delivery_key
            )
            self.state.finish_turn(
                request_context.turn_id,
                status="error",
                delivery_state="not_sent",
                plan=fallback_plan,
                error_code=exc.error_code,
            )
            return contracts.ContractResponse(
                "error",
                {
                    "turn_id": request_context.turn_id,
                    "delivery_key": delivery_key,
                    "reply_plan": fallback_plan.to_dict(),
                    "fallback": True,
                },
                exc.error_code,
                exc.message,
            )
        plan = processed.plan
        plan.delivery_key = delivery_key
        plan.request_context = request_context
        self.state.record_plan(request_context.turn_id, plan)
        return contracts.ok(
            {
                "turn_id": request_context.turn_id,
                "delivery_key": delivery_key,
                "reply_plan": plan.to_dict(),
                "text": self.gateway.plan_text(plan),
                "processed_by_safe_tools": processed.processed_by_safe_tools,
                "model_round": model_round,
            }
        )

    def render_plan(self, event: Any, plan: ReplyPlan) -> Any:
        text = self.gateway.finalize_text(self.gateway.plan_text(plan))
        media_components = self.media_components(plan.media_segments)
        if not media_components:
            return event.plain_result(text)
        try:
            from astrbot.api import message_components as Comp
        except Exception:
            return event.plain_result(text)
        chain: list[Any] = []
        if text:
            chain.append(Comp.Plain(text))
        chain.extend(media_components)
        try:
            return event.chain_result(chain)
        except Exception:
            return event.plain_result(text)

    def media_components(self, segments: list[dict[str, Any]]) -> list[Any]:
        try:
            from astrbot.api import message_components as Comp
        except Exception:
            return []
        output: list[Any] = []
        for segment in segments or []:
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type", "") or "").lower()
            url = str(segment.get("url", "") or "")
            path = str(segment.get("path", segment.get("file", "")) or "")
            try:
                if kind in {"image", "img"}:
                    if url and callable(getattr(Comp.Image, "fromURL", None)):
                        output.append(Comp.Image.fromURL(url))
                    elif path and callable(getattr(Comp.Image, "fromFileSystem", None)):
                        output.append(Comp.Image.fromFileSystem(path))
                elif kind in {"record", "audio", "voice"}:
                    if path and callable(getattr(Comp.Record, "fromFileSystem", None)):
                        output.append(Comp.Record.fromFileSystem(path))
            except Exception:
                continue
        return output

    async def _expire_memory_turn(self, turn_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, float(delay)))
        except asyncio.CancelledError:
            return
        record = self.state.get_turn(turn_id)
        if record is None or record.status != "running":
            return
        self._finish_memory_turn(turn_id, "outcome_unknown")

    def _finish_memory_turn(self, turn_id: str, delivery_state: str) -> None:
        try:
            self.state.record_delivery(turn_id, delivery_state)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Memory-mode complete-output adapter
    # ------------------------------------------------------------------
    async def handle_decorating_result(self, event: Any) -> None:
        if self.persona.memory_mode() != "astrbot":
            return
        try:
            if event.get_extra(CANDIDATE_EXTRA) is not True:
                return
        except Exception:
            return
        turn_id = str(event.get_extra(TURN_EXTRA, "") or "")
        if not turn_id:
            return
        model_round = int(event.get_extra(MODEL_ROUND_EXTRA, 0) or 0)
        fingerprint = f"{turn_id}:{model_round}"
        if not self.state.mark_output_processed(fingerprint):
            return
        try:
            result = getattr(event, "get_result", lambda: None)()
            chain = getattr(result, "chain", None)
            if not isinstance(chain, list):
                self._finish_memory_turn(turn_id, "outcome_unknown")
                return
            plain_text = ""
            try:
                from astrbot.api import message_components as Comp
            except Exception:
                Comp = None
            for component in chain:
                if Comp is not None and isinstance(component, Comp.Plain):
                    plain_text += str(getattr(component, "text", "") or "")
            if not plain_text:
                self._finish_memory_turn(turn_id, "delivered")
                return
            if (
                event.get_extra(UNIFIED_PARTICIPATION_EXTRA) is True
                and "[[NO_REPLY]]" in plain_text
            ):
                chain[:] = []
                event.set_extra(OUTPUT_PROCESSED_EXTRA, True)
                try:
                    self.state.finish_turn(
                        turn_id,
                        status="cancelled",
                        delivery_state="not_sent",
                    )
                except Exception:
                    pass
                return
            if event.get_extra(UNIFIED_PARTICIPATION_EXTRA) is True:
                self.participation.mark_unified_reply(event)
            processor = self.gateway.safe_tools_star()
            if processor is None or not callable(
                getattr(processor, "process_model_output", None)
            ):
                self._finish_memory_turn(turn_id, "delivered")
                return
            raw_context = event.get_extra(REQUEST_CONTEXT_EXTRA, {})
            try:
                request_context = contracts.load_request_context(raw_context)
                processed = await self.gateway.process_model_output(
                    request_context,
                    plain_text,
                    expected_call_id=f"{turn_id}:{model_round}",
                    expected_delivery_key=str(
                        getattr(request_context, "delivery_key", "")
                        or f"reply:{turn_id}"
                    ),
                )
            except ContractError as exc:
                event.set_extra(OUTPUT_ERROR_EXTRA, exc.error_code)
                event.set_extra(OUTPUT_PROCESSED_EXTRA, False)
                self._finish_memory_turn(turn_id, "delivered")
                return
            text = self.gateway.finalize_text(self.gateway.plan_text(processed.plan))
            replacement = []
            if text and Comp is not None:
                replacement.append(Comp.Plain(text))
            replacement.extend(self.media_components(processed.plan.media_segments))
            if replacement:
                chain[:] = replacement
            elif Comp is not None:
                chain[:] = [Comp.Plain(self.gateway.finalize_text(""))]
            event.set_extra(OUTPUT_PROCESSED_EXTRA, True)
            event.set_extra("qqbot_core_reply_safe_tools_processed", True)
            self._finish_memory_turn(turn_id, "delivered")
        except Exception:
            self._finish_memory_turn(turn_id, "outcome_unknown")

    # ------------------------------------------------------------------
    # Memory request hook + lifecycle
    # ------------------------------------------------------------------
    async def handle_memory_context(self, event: Any, req: Any) -> None:
        if self.persona.memory_mode() != "astrbot":
            return
        candidate = False
        try:
            candidate = event.get_extra(CANDIDATE_EXTRA) is True
        except Exception:
            candidate = False
        if not candidate:
            logger.debug("qqbot_core_reply llm hook skipped: candidate=false")
            return
        try:
            # AstrBot's own persona list is often empty.  In AstrBot memory
            # mode the active qqbot persona profile must still reach the
            # provider, otherwise a configured profile such as "麻衣" is
            # silently replaced by AstrBot's default assistant prompt.
            if event.get_extra("qqbot_core_reply_persona_injected") is not True:
                persona_prompt, _persona_name, _source = (
                    self.persona.resolve_event_persona(event)
                )
                persona_prompt = str(persona_prompt or "").strip()
                if persona_prompt:
                    logger.info(
                        "qqbot persona injection: name=%s source=%s chars=%d",
                        _persona_name, _source, len(persona_prompt),
                    )
                    base_prompt = str(getattr(req, "system_prompt", "") or "")
                    if persona_prompt not in base_prompt:
                        # Replace AstrBot's generic assistant prompt instead
                        # of prepending to it: two competing personalities
                        # make the model drift back to the assistant voice.
                        req.system_prompt = persona_prompt
                    event.set_extra("qqbot_core_reply_persona_injected", True)
        except Exception:
            logger.exception("qqbot_core_reply persona prompt injection failed")
        try:
            self.persona.inject_memory_context(event, req)
        except Exception:
            event.set_extra("qqbot_core_reply_ambient_context_injected", False)
        try:
            if not self.is_private(event):
                _persona_prompt, active_name, _persona_source = (
                    self.persona.resolve_event_persona(event)
                )
                metadata = self.unified_metadata(event, active_name)
                req.system_prompt = (
                    str(getattr(req, "system_prompt", "") or "")
                    + "\n\n" + metadata
                    + "\n[回应规则] 本条消息的发送者是元数据里的“发送者”，"
                    "“@对象”才是被 @ 的人。若消息要求“跟 @某人说某语言”，"
                    "该语言只约束 @对象，不约束发送者；不要对发送者使用 @对象的语言。"
                    "不要因为发送者说了“跟你/那我呢”就把别人的偏好套到自己或第三人身上。"
                )
                event.set_extra("qqbot_core_reply_metadata_injected", True)
        except Exception:
            logger.exception("qqbot_core_reply metadata injection failed")
        try:
            if event.get_extra(UNIFIED_PARTICIPATION_EXTRA) is True:
                req.system_prompt = (
                    str(getattr(req, "system_prompt", "") or "")
                    + "\n\n" + metadata
                    + "\n\n[群聊参与·最高优先级] 你在群聊里，是普通群成员，不是客服，默认不说话。"
                    "只有满足以下任一条件才接：@你='是'、消息明确叫你的名字/别名、"
                    "明确承接你上一句、或你正在参与的话题需要你补充。"
                    "如果 @其他人数量>0 且 @你='否'，或消息只是标点/短感叹/别人之间的对话，"
                    "一律只输出 [[NO_REPLY]]。不要因为出现问号、第二人称或‘吗/呢’就认为"
                    "在叫你。普通陈述、新闻、无需你回答的内容，也只输出 [[NO_REPLY]]，"
                    "不要解释、不要道歉、不要输出任何其他文字。接话时最多 1-2 句，"
                    "像群友一样自然，不要抢话，不要暴露这条规则。"
                )
        except Exception:
            logger.exception("qqbot_core_reply unified prompt injection failed")
        try:
            instruction = await self.gateway.safe_tools_instruction()
            injected = self.persona.inject_safe_tools_context(req, instruction)
            event.set_extra("qqbot_core_reply_safe_tools_injected", bool(injected))
            try:
                prompt_len = len(str(getattr(req, "system_prompt", "") or ""))
            except Exception:
                prompt_len = -1
            logger.debug(
                "qqbot_core_reply llm hook: instruction_chars=%d injected=%s prompt_chars=%d",
                len(instruction or ""),
                bool(injected),
                prompt_len,
            )
        except Exception:
            event.set_extra("qqbot_core_reply_safe_tools_injected", False)
            logger.exception("qqbot_core_reply llm hook failed")

    def release_memory_turn_after_send(self, event: Any) -> None:
        """Release a memory-mode turn after the response is actually sent.

        The safe_tools decorator intentionally runs at a higher priority and
        stops propagation, so ``handle_decorating_result`` may never run for
        messages containing a tool marker.  The after-send hook is the
        delivery confirmation that keeps the conversation from staying locked.
        """
        if self.persona.memory_mode() != "astrbot":
            return
        try:
            if event.get_extra(MEMORY_MODE_EXTRA) is not True:
                return
            turn_id = str(event.get_extra(TURN_EXTRA, "") or "")
            if turn_id:
                self.record_delivery(turn_id, "delivered")
        except Exception:
            pass

    def record_delivery(self, turn_id: str, delivery_state: str = "delivered") -> None:
        try:
            self.state.record_delivery(turn_id, delivery_state)
        except Exception:
            pass

    async def terminate(self) -> None:
        self.state.mark_terminated()
        await self.state.cancel_all_tasks()


class CoreReplyService:
    """Fixed ABI v1 provider methods exposed through the shared Context."""

    PROVIDER_ID = "core_reply"
    CAPABILITY = "reply"
    METHOD_NAMES = (
        "reply_status",
        "reply_configure_plan",
        "reply_configure_commit",
        "reply_turn_run",
        "reply_cancel_turn",
    )

    CONFIG_ALLOWLIST = {
        "enabled": "enabled",
        "memory_mode": "memory_mode",
        "participation.private_enabled": "participation.private_enabled",
        "participation.mention_enabled": "participation.mention_enabled",
        "participation.group_probability": "participation.group_probability",
        "participation.group_allow": "participation.group_allow",
        "participation.group_deny": "participation.group_deny",
        "participation.ignore_self": "participation.ignore_self",
        "participation.ignore_bot": "participation.ignore_bot",
        "cooldown.seconds": "cooldown.seconds",
        "cooldown.key_mode": "cooldown.key_mode",
        "cooldown.admin_bypass": "cooldown.admin_bypass",
        "concurrency.per_conversation": "concurrency.per_conversation",
        "commands.exclude_aliases": "commands.exclude_aliases",
        "commands.unknown_command_mode": "commands.unknown_command_mode",
        "persona.default_id": "persona.default_id",
        "persona.binding_mode": "persona.binding_mode",
        "context.max_messages": "context.max_messages",
        "context.max_chars": "context.max_chars",
        "llm.provider_id": "llm.provider_id",
        "llm.timeout_seconds": "llm.timeout_seconds",
        "llm.tool_round_limit": "llm.tool_round_limit",
        "reply.max_segments": "reply.max_segments",
        "reply.streaming_mode": "reply.streaming_mode",
        "reply.error_mode": "reply.error_mode",
        "session.key_mode": "session.key_mode",
        # Legacy aliases are accepted for migration tooling.
        "reply_probability": "participation.group_probability",
        "group_cooldown_seconds": "cooldown.seconds",
        "user_cooldown_seconds": "user_cooldown_seconds",
        "min_message_chars": "min_message_chars",
        "max_input_chars": "context.max_chars",
        "max_message_chars": "max_input_chars",
        "max_reply_chars": "max_reply_chars",
        "system_prompt": "system_prompt",
        "llm_timeout_seconds": "llm.timeout_seconds",
    }
    ENUM_VALUES = {
        "memory_mode": {"direct", "astrbot", "memory"},
        "cooldown.key_mode": {
            "instance",
            "group",
            "user",
            "conversation",
            "conversation_persona",
            "legacy_compatible",
        },
        "commands.unknown_command_mode": {"ignore", "terminal_error"},
        "persona.binding_mode": {"legacy_compatible", "conversation", "user"},
        "reply.streaming_mode": {"buffer_until_validated", "disabled"},
        "reply.error_mode": {"terminal_error", "silent_with_audit"},
        "session.key_mode": {"legacy_compatible", "platform_bot_conversation"},
    }

    def __init__(
        self,
        pipeline: ReplyPipeline,
        *,
        registry: Any = None,
        owner_token: str = "",
        provider_id: str = PROVIDER_ID,
    ) -> None:
        self.pipeline = pipeline
        self.config = pipeline.config
        self.state = pipeline.state
        self.registry = registry
        self.owner_token = owner_token
        self.provider_id = provider_id

    # ------------------------------------------------------------------
    def _request(self, value: Any) -> RequestContext:
        return contracts.load_request_context(value)

    def _epoch_error(self, context: RequestContext) -> ContractResponse | None:
        if int(context.owner_epoch) != int(self.state.owner_epoch):
            return contracts.fail(
                "owner_epoch_mismatch",
                "owner_epoch does not match committed owner",
                {"expected": self.state.owner_epoch},
            )
        return None

    def _deadline_error(self, context: RequestContext) -> ContractResponse | None:
        if context.deadline and self.state.clock() > context.deadline:
            return contracts.fail("deadline_exceeded", "request deadline exceeded")
        return None

    def _not_ready(self) -> ContractResponse:
        if self.state.terminated:
            return contracts.fail("not_ready", "runtime terminated")
        return contracts.fail("not_ready", "core_reply provider is not ready")

    # ------------------------------------------------------------------
    async def reply_status(self, request_context: Any, args: Any = None) -> ContractResponse:
        try:
            context = self._request(request_context)
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        epoch_error = self._epoch_error(context)
        if epoch_error:
            return epoch_error
        data = {
            "provider_id": self.provider_id,
            "reply_owner": self.state.model_controller,
            "enabled": self.config.bool("enabled", False),
            "ready": bool(self.state.ready and not self.state.terminated),
            "terminated": self.state.terminated,
            "memory_mode": self.pipeline.persona.memory_mode(),
            "owner_epoch": self.state.owner_epoch,
            "config_revision": self.state.config_revision,
            "active_turns": self.state.active_turn_count(),
            "concurrency_per_conversation": self.config.effective_int(
                "concurrency.per_conversation",
                default=1,
                minimum=1,
                maximum=1,
            ),
            "group_probability": self.config.effective_float(
                "participation.group_probability",
                "reply_probability",
                default=0.12,
                minimum=0.0,
                maximum=1.0,
            ),
            "cooldown_seconds": self.config.effective_float(
                "cooldown.seconds",
                "group_cooldown_seconds",
                default=30.0,
                minimum=0.0,
                maximum=3600.0,
            ),
            "ready_dependencies": {
                "safe_tools_process_model_output": self.pipeline.gateway.process_output_available(),
                "safe_tools_tool_instruction": self.pipeline.gateway.safe_tools_available(),
            },
        }
        return contracts.ok(data)

    async def reply_configure_plan(
        self,
        request_context: Any,
        args: Any = None,
    ) -> ContractResponse:
        try:
            context = self._request(request_context)
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        epoch_error = self._epoch_error(context)
        if epoch_error:
            return epoch_error
        data = args if isinstance(args, dict) else {}
        changes = data.get("changes")
        if not isinstance(changes, dict) or not changes:
            return contracts.fail("invalid_args", "changes must be a non-empty object")
        normalized, error = self._normalize_changes(changes)
        if error:
            return error
        plan_id = uuid.uuid4().hex
        plan = {
            "plan_id": plan_id,
            "changes": normalized,
            "config_revision": self.state.config_revision,
            "owner_epoch": self.state.owner_epoch,
            "actor_id": context.actor_id,
            "turn_id": context.turn_id,
            "created_at": self.state.clock(),
        }
        self.state.pending_plans[plan_id] = plan
        return contracts.ok({"plan": plan, "plan_id": plan_id})

    async def reply_configure_commit(
        self,
        request_context: Any,
        args: Any = None,
    ) -> ContractResponse:
        try:
            context = self._request(request_context)
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        epoch_error = self._epoch_error(context)
        if epoch_error:
            return epoch_error
        data = args if isinstance(args, dict) else {}
        plan_id = str(data.get("plan_id", "") or "")
        if plan_id:
            plan = self.state.pending_plans.get(plan_id)
            if plan is None:
                return contracts.fail("not_found", "configure plan not found")
            changes = plan.get("changes", {})
            expected_revision = data.get("expected_revision", plan.get("config_revision"))
        else:
            changes = data.get("changes")
            expected_revision = data.get("expected_revision")
        if not isinstance(changes, dict) or not changes:
            return contracts.fail("invalid_args", "changes must be a non-empty object")
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError):
            return contracts.fail("invalid_args", "expected_revision is required")
        if expected != int(self.state.config_revision):
            return contracts.fail(
                "revision_conflict",
                "config revision changed",
                {
                    "expected": expected,
                    "current": self.state.config_revision,
                },
            )
        normalized, error = self._normalize_changes(changes)
        if error:
            return error
        self.config.set_overrides(normalized)
        self.state.config_revision += 1
        if plan_id:
            self.state.pending_plans.pop(plan_id, None)
        enabled = self.config.bool("enabled", False)
        self.state.set_ready(enabled)
        self._set_registry_ready(enabled)
        return contracts.ok(
            {
                "plan_id": plan_id,
                "changes": normalized,
                "config_revision": self.state.config_revision,
                "owner_epoch": self.state.owner_epoch,
                "ready": bool(self.state.ready),
            }
        )

    async def reply_turn_run(
        self,
        request_context: Any,
        args: Any = None,
    ) -> ContractResponse:
        try:
            context = self._request(request_context)
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        epoch_error = self._epoch_error(context)
        if epoch_error:
            return epoch_error
        deadline_error = self._deadline_error(context)
        if deadline_error:
            return deadline_error
        if not self.state.ready or self.state.terminated:
            return self._not_ready()
        return await self.pipeline.turn_run(context, args if isinstance(args, dict) else {})

    async def reply_cancel_turn(
        self,
        request_context: Any,
        args: Any = None,
    ) -> ContractResponse:
        try:
            context = self._request(request_context)
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        epoch_error = self._epoch_error(context)
        if epoch_error:
            return epoch_error
        data = args if isinstance(args, dict) else {}
        turn_id = str(data.get("turn_id") or context.turn_id)
        try:
            record = self.state.cancel_turn(
                turn_id,
                owner_epoch=context.owner_epoch,
            )
        except ContractError as exc:
            return contracts.fail(exc.error_code, exc.message)
        return contracts.ok(
            {
                "turn_id": record.turn_id,
                "cancelled": record.status == "cancelled",
                "already_delivered": record.delivery_state == "delivered",
                "resent": False,
                "delivery_key": record.delivery_key,
            }
        )

    # ------------------------------------------------------------------
    def _normalize_changes(
        self,
        changes: dict[str, Any],
    ) -> tuple[dict[str, Any], ContractResponse | None]:
        normalized: dict[str, Any] = {}
        for raw_key, value in changes.items():
            key = str(raw_key or "").strip()
            target = self.CONFIG_ALLOWLIST.get(key)
            if target is None:
                return {}, contracts.fail(
                    "invalid_args",
                    f"config key not allowed: {key}",
                )
            if target == "enabled":
                if not isinstance(value, bool):
                    return {}, contracts.fail("invalid_args", "enabled must be bool")
                normalized["enabled"] = value
                continue
            if target == "memory_mode":
                mode = str(value or "").strip().lower()
                if mode not in self.ENUM_VALUES["memory_mode"]:
                    return {}, contracts.fail("invalid_args", "memory_mode invalid")
                normalized["memory_mode"] = "astrbot" if mode in {"astrbot", "memory"} else "direct"
                continue
            if target in self.ENUM_VALUES:
                text = str(value or "").strip()
                if text not in self.ENUM_VALUES[target]:
                    return {}, contracts.fail(
                        "invalid_args", f"{target} invalid enum value"
                    )
                normalized[target] = text
                continue
            if target in {
                "participation.private_enabled",
                "participation.mention_enabled",
                "participation.ignore_self",
                "participation.ignore_bot",
                "cooldown.admin_bypass",
            }:
                if not isinstance(value, bool):
                    return {}, contracts.fail("invalid_args", f"{target} must be bool")
                normalized[target] = value
                continue
            if target in {"participation.group_probability"}:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    return {}, contracts.fail("invalid_args", f"{target} must be number")
                if not 0.0 <= number <= 1.0:
                    return {}, contracts.fail("invalid_args", f"{target} out of range")
                normalized[target] = number
                continue
            if target in {
                "participation.group_allow",
                "participation.group_deny",
                "commands.exclude_aliases",
            }:
                if not isinstance(value, (list, tuple)):
                    return {}, contracts.fail("invalid_args", f"{target} must be list")
                normalized[target] = [str(item).strip() for item in value if str(item).strip()]
                continue
            if target in {"llm.provider_id", "persona.default_id", "system_prompt"}:
                if not isinstance(value, str):
                    return {}, contracts.fail("invalid_args", f"{target} must be string")
                normalized[target] = value
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                return {}, contracts.fail("invalid_args", f"{target} must be int")
            if target in {"context.max_messages", "max_input_chars"} and number <= 0:
                return {}, contracts.fail("invalid_args", f"{target} must be > 0")
            if target in {"cooldown.seconds", "llm.timeout_seconds"} and number <= 0:
                return {}, contracts.fail("invalid_args", f"{target} must be > 0")
            if number < 0:
                return {}, contracts.fail("invalid_args", f"{target} must be >= 0")
            normalized[target] = number
        return normalized, None

    def _set_registry_ready(self, ready: bool) -> None:
        registry = self.registry
        if registry is None:
            return
        setter = getattr(registry, "set_ready", None)
        if callable(setter):
            try:
                setter(
                    self.provider_id,
                    bool(ready),
                    owner=self.owner_token,
                    owner_epoch=self.state.owner_epoch,
                )
            except TypeError:
                try:
                    setter(self.provider_id, bool(ready))
                except Exception:
                    pass
            except Exception:
                pass
