"""Persona/profile resolution, prompt construction and identity context."""

from __future__ import annotations

import re
from typing import Any

try:  # package import
    from . import persona_bridge as persona_bridge_engine
    from . import personas as persona_engine
except ImportError:  # test harness
    import persona_bridge as persona_bridge_engine
    import personas as persona_engine


def _safe_text(value: Any, *, max_chars: int, keep_newlines: bool = False) -> str:
    text = str(value or "")
    allowed = ("\n", "\t", "\r") if keep_newlines else ()
    text = "".join(char for char in text if ord(char) >= 32 or char in allowed)
    return text[: max(0, int(max_chars))].strip()


class PersonaService:
    def __init__(self, context: Any, config: Any, state: Any, logger: Any = None) -> None:
        self.context = context
        self.config = config
        self.state = state
        self.logger = logger
        self.bridge = persona_bridge_engine.AgentPersonaBridge(
            persona_manager=getattr(context, "persona_manager", None),
            conversation_manager=getattr(context, "conversation_manager", None),
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Parsing / resolution
    # ------------------------------------------------------------------
    def parse_group_personas(self):
        return persona_engine.parse_group_personas(
            self.config.value("group_personas"),
            max_groups=self.config.int("group_persona_max_groups", 0, 200),
            max_prompt_chars=self.config.int(
                "group_persona_max_prompt_chars", 1, 10_000
            ),
            max_name_chars=self.config.int("group_persona_max_name_chars", 1, 128),
        )

    def parse_prompt_profiles(self):
        return persona_engine.parse_prompt_profiles(
            self.config.value("prompt_profiles"),
            self.config.string("prompt_profile_active_id"),
            max_profiles=100,
            max_prompt_chars=persona_engine.PROMPT_PROFILE_MAX_PROMPT_CHARS,
            max_name_chars=80,
        )

    def _safe_identity_nickname(self, event: Any) -> str:
        try:
            raw = str(event.get_sender_name() or "")
        except Exception:
            return ""
        return _safe_text(
            raw,
            max_chars=self.config.int("group_identity_nickname_max_chars", 1, 64),
        )

    @staticmethod
    def _safe_identity_qq(event: Any) -> str:
        try:
            raw = str(event.get_sender_id() or "").strip()
        except Exception:
            return ""
        return raw if re.fullmatch(r"[0-9]{1,20}", raw) else ""

    def _safe_group_id(self, event: Any) -> str:
        try:
            if event.is_private_chat():
                return ""
        except Exception:
            return ""
        try:
            raw = str(event.get_group_id() or "").strip()
        except Exception:
            return ""
        return raw if re.fullmatch(r"[0-9]{1,20}", raw) else ""

    @staticmethod
    def _identity_label(nickname: str, qq: str) -> str:
        nickname = str(nickname or "").strip()
        qq = str(qq or "").strip()
        if nickname and qq:
            return f"{nickname}({qq})"
        if nickname:
            return nickname
        if qq:
            return f"未知({qq})"
        return "未知用户"

    def resolve_event_persona(self, event: Any) -> tuple[str, str, str]:
        legacy_prompt = self.config.string("system_prompt")
        parsed = None
        group_id = ""
        try:
            if self.config.bool("group_persona_enabled", True):
                parsed = self.parse_group_personas()
                group_id = self._safe_group_id(event)
                if (
                    group_id
                    and persona_engine.GROUP_ID_RE.fullmatch(group_id)
                    and group_id in parsed.groups
                ):
                    persona = parsed.groups[group_id]
                    return persona.prompt, persona.name, "group"
        except Exception:
            if self.logger:
                self.logger.warning(
                    "qqbot_core_reply 群人格配置解析失败，回退旧 system_prompt"
                )
        try:
            profiles = self.parse_prompt_profiles()
            prompt, name, _profile_id, source = persona_engine.resolve_prompt_profile(
                profiles,
                legacy_prompt="",
            )
            if source == "profile" and prompt:
                return prompt, name, "profile"
        except Exception:
            if self.logger:
                self.logger.warning("qqbot_core_reply 提示词 profiles 解析失败")
        if parsed is not None and parsed.default is not None:
            return parsed.default.prompt, parsed.default.name, "default"
        default_id = self.config.string("persona.default_id").strip()
        if default_id:
            manager = getattr(self.context, "persona_manager", None)
            getter = getattr(manager, "get_persona", None)
            if callable(getter):
                persona = None
                try:
                    persona = getter(default_id)
                    if hasattr(persona, "__await__"):
                        persona = None
                except Exception:
                    persona = None
                prompt = str(getattr(persona, "system_prompt", "") or "")
                if prompt:
                    return prompt, str(getattr(persona, "name", "") or ""), "persona_id"
        return legacy_prompt, "", "legacy"

    def agent_group_persona_target(
        self,
        event: Any,
    ) -> tuple[str, str, str, str] | None:
        parsed = persona_engine.PersonaConfig(default=None, groups={})
        try:
            if self.config.bool("group_persona_enabled", True):
                parsed = self.parse_group_personas()
            is_private = bool(event.is_private_chat())
            group_id = "" if is_private else self._safe_group_id(event)
        except Exception:
            return None
        if (
            not is_private
            and group_id
            and persona_engine.GROUP_ID_RE.fullmatch(group_id)
            and group_id in parsed.groups
        ):
            persona = parsed.groups[group_id]
            return (
                persona.prompt,
                persona.name,
                "group",
                persona_bridge_engine.persona_id_for(group_id, "group"),
            )
        try:
            profiles = self.parse_prompt_profiles()
            prompt, name, profile_id, source = persona_engine.resolve_prompt_profile(
                profiles,
                legacy_prompt="",
            )
            if source == "profile" and prompt:
                persona_id = persona_bridge_engine.persona_id_for_profile(profile_id)
                if persona_id:
                    return prompt, name, "profile", persona_id
        except Exception:
            pass
        if parsed.default is not None:
            return (
                parsed.default.prompt,
                parsed.default.name,
                "default",
                persona_bridge_engine.AGENT_DEFAULT_PERSONA_ID,
            )
        return None

    def persona_option_line(self) -> str:
        try:
            parsed = self.parse_group_personas()
        except Exception:
            return "群人格：配置解析失败，已回退旧 system_prompt。"
        try:
            profiles = self.parse_prompt_profiles()
        except Exception:
            profiles = None
        if profiles is not None and profiles.errors:
            return "提示词 profiles：格式非法或超过上限，已安全回退旧 system_prompt。"
        if profiles is not None and profiles.active is not None:
            return (
                f"提示词：已加载 {len(profiles.profiles)} 个模板；"
                "active 用于 direct，pinned/active 作为 /新会话 默认模板。"
            )
        return persona_engine.persona_summary(parsed)

    def identity_option_line(self) -> str:
        if not self.config.bool("group_identity_context_enabled", True):
            return "群聊身份上下文：关闭。"
        if self.memory_mode() == "astrbot":
            return (
                "群聊身份上下文：memory 模式通过官方 on_llm_request "
                "注入当前发送者；历史消息仍无逐消息身份前缀。"
            )
        return (
            "群聊身份上下文：direct 模式已启用，最近消息带 "
            "[昵称(QQ)] 前缀；QQ 仅用于区分，不展示。"
        )

    def memory_mode(self) -> str:
        text = self.config.string("memory_mode").strip().lower()
        if text in {"astrbot", "memory", "conversation", "long-term", "long_term"}:
            return "astrbot"
        return "direct"

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------
    def build_prompt(
        self,
        event: Any,
        message_text: str,
        *,
        has_face: bool,
        has_image: bool,
        image_caption: str = "",
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        sender_name = self._safe_identity_nickname(event)
        sender_qq = self._safe_identity_qq(event)
        group_id = self._safe_group_id(event)
        try:
            scene = "私聊" if event.is_private_chat() else "群聊"
        except Exception:
            scene = "群聊"
        identity_enabled = self.config.bool("group_identity_context_enabled", True)
        label = self._identity_label(sender_name, sender_qq)
        lines: list[str] = [f"场景：{scene}"]
        if identity_enabled:
            lines.extend(
                [
                    f"群号：{group_id if group_id else '无（私聊）'}",
                    f"发送者昵称：{sender_name or '未知'}",
                    f"发送者QQ：{sender_qq or '未知'}",
                    (
                        "身份说明：昵称和QQ号仅用于区分当前会话中的群成员，"
                        "禁止在回复中输出QQ号、群号或发送者昵称。"
                    ),
                ]
            )
            history_limit = max(
                0,
                self.config.effective_int(
                    "context.max_messages",
                    "group_identity_history_limit",
                    default=8,
                    minimum=0,
                    maximum=30,
                ),
            )
            recent_history = (
                (chat_history or [])[-history_limit:] if history_limit else []
            )
            if recent_history:
                lines.append("最近用户消息（每行格式 [昵称(QQ)] 内容）：")
                for item in recent_history:
                    history_label = self._identity_label(
                        str(item.get("nickname", "") or ""),
                        str(item.get("qq", "") or ""),
                    )
                    history_text = str(item.get("text", "") or "")[:200]
                    lines.append(f"- [{history_label}] {history_text}")
        else:
            lines.append(f"用户昵称：{sender_name or '未知'}")
            if self.config.bool("ambient_group_chat_enabled") and chat_history:
                lines.append("最近群消息（无发送者标识）：")
                for item in chat_history:
                    history_text = str(item.get("text", "") or "")[:200]
                    if history_text:
                        lines.append(f"- {history_text}")

        if has_image:
            image_count = self.config.int("max_images_per_message", 1, 10)
            lines.append(f"用户发来了图片（最多附带 {image_count} 张）。")
        if message_text.strip():
            if identity_enabled:
                lines.append(f"当前用户消息：[{label}] {message_text.strip()}")
            else:
                lines.append(f"用户文字：{message_text.strip()}")
        elif has_image:
            if identity_enabled:
                lines.append(f"当前用户消息：[{label}] [图片，无文字]")
            else:
                lines.append("用户没有附带文字，请观察图片后直接自然回复。")
        elif has_face:
            if identity_enabled:
                lines.append(f"当前用户消息：[{label}] [QQ 表情，无文字]")
            else:
                lines.append("用户发送了一个 QQ 表情，没有附带文字。")
        else:
            if identity_enabled:
                lines.append(f"当前用户消息：[{label}] [空消息]")
            else:
                lines.append("用户没有输入文字内容。")
        if image_caption:
            lines.extend(
                [
                    "以下是图片转述模型得到的客观描述，请基于它回复用户：",
                    f"<image_caption>{image_caption}</image_caption>",
                ]
            )
        lines.append("请直接输出要发给用户的回复正文，不要输出分析过程。")
        return "\n".join(lines)

    def effective_system_prompt(
        self,
        event: Any,
        tool_instruction: str = "",
    ) -> str:
        prompt, _name, _source = self.resolve_event_persona(event)
        if tool_instruction:
            prompt = persona_bridge_engine.inject_safe_tools_instruction(
                prompt,
                tool_instruction,
            )
        return prompt

    def scrub_identity_from_reply(
        self,
        reply: str,
        event: Any,
        *,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        text = str(reply or "")
        if not self.config.bool("group_identity_redact_in_reply", True):
            return text
        terms: set[str] = set()
        nickname = self._safe_identity_nickname(event)
        qq = self._safe_identity_qq(event)
        group_id = self._safe_group_id(event)
        for value in (nickname, qq, group_id):
            if len(value) >= 2:
                terms.add(value)
        for item in chat_history or []:
            for value in (
                str(item.get("nickname", "") or ""),
                str(item.get("qq", "") or ""),
            ):
                if len(value) >= 2:
                    terms.add(value)
        for term in sorted(terms, key=len, reverse=True):
            text = text.replace(term, "")
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Memory-mode hooks (never call an LLM)
    # ------------------------------------------------------------------
    def inject_memory_context(self, event: Any, req: Any) -> None:
        injected_identity = False
        if self.config.bool("group_identity_context_enabled", True):
            injected_identity = persona_bridge_engine.inject_current_sender_identity(
                req,
                group_id=self._safe_group_id(event),
                nickname=self._safe_identity_nickname(event),
                qq=self._safe_identity_qq(event),
                max_chars=self.config.int(
                    "group_identity_agent_prompt_max_chars", 1, 2000
                ),
            )
        event.set_extra(
            "qqbot_core_reply_identity_injected",
            bool(injected_identity),
        )
        if self.config.bool("ambient_group_chat_enabled"):
            session = str(getattr(event, "unified_msg_origin", "") or "")
            if not self._is_private(event) and self._safe_group_id(event):
                history = self.state.history_for(session)
                limit = max(1, self.config.int("ambient_group_cache_limit", 1, 100))
                history = history[-limit:]
                injected_ambient = persona_bridge_engine.inject_ambient_group_context(
                    req,
                    history,
                    now=self.state.clock(),
                    max_chars=self.config.int(
                        "ambient_group_context_max_chars", 1, 6000
                    ),
                )
                event.set_extra(
                    "qqbot_core_reply_ambient_context_injected",
                    bool(injected_ambient),
                )

    @staticmethod
    def _is_private(event: Any) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False
