"""qqbot_core_reply: the only main-reply / LLM owner for QQ chat.

The plugin is self-contained and uses only the Python standard library plus
public AstrBot >=4.29 plugin APIs.  It never imports the legacy monolith or
another new plugin.  Cross-plugin discovery is restricted to the dynamic
``Context.get_registered_star("qqbot_safe_tools")`` handshake.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # Loaded as a package by AstrBot.
    from .config import ConfigReader, DEFAULTS
    from . import conversations as conversation_engine
    from . import media as media_engine
    from . import persona_bridge as persona_bridge_engine
    from . import personas as persona_engine
    from .songs import SONG_SUFFIXES, list_song_files, resolve_song_file
except ImportError:  # Loaded directly by a test harness.
    import sys as _sys

    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    from config import ConfigReader, DEFAULTS
    import conversations as conversation_engine
    import media as media_engine
    import persona_bridge as persona_bridge_engine
    import personas as persona_engine
    from songs import SONG_SUFFIXES, list_song_files, resolve_song_file

PLUGIN_NAME = "qqbot_core_reply"
PLUGIN_DISPLAY_NAME = "QQ 核心回复"
PLUGIN_VERSION = "4.0.0"
PLUGIN_DIR = Path(__file__).resolve().parent
SONG_DIR = PLUGIN_DIR / "songs"

CANDIDATE_EXTRA = "qqbot_core_reply_candidate"
ALLOW_STICKER_EXTRA = "qqbot_core_reply_allow_sticker"
TEXT_EXTRA = "qqbot_core_reply_text"
AMBIENT_EXTRA = "qqbot_core_reply_ambient"


def _platform_types() -> Any:
    adapter = getattr(filter, "PlatformAdapterType", None)
    if adapter is None:
        return None
    names = ("AIOCQHTTP", "QQOFFICIAL", "QQOFFICIAL_WEBHOOK")
    values = [getattr(adapter, name) for name in names if hasattr(adapter, name)]
    if not values:
        return None
    combined = values[0]
    for value in values[1:]:
        try:
            combined = combined | value
        except TypeError:
            break
    return combined


_PLATFORM_TYPES = _platform_types()


def _is_component(component: Any, name: str) -> bool:
    component_cls = getattr(Comp, name, None)
    return bool(component_cls is not None and isinstance(component, component_cls))


def _safe_text(value: Any, *, max_chars: int, keep_newlines: bool = False) -> str:
    text = str(value or "")
    allowed = ("\n", "\t", "\r") if keep_newlines else ()
    text = "".join(
        char
        for char in text
        if ord(char) >= 32 or char in allowed
    )
    return text[: max(0, int(max_chars))].strip()


def _mapping_value(mapping: Any, key: str, default: Any = None) -> Any:
    if isinstance(mapping, dict):
        return mapping.get(key, default)
    getter = getattr(mapping, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return getattr(mapping, key, default)


class CoreReplyPlugin(Star):
    """Single owner of ordinary QQ replies and direct model calls."""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        # AstrBot injects the plugin config here.  Do NOT read
        # context.get_config() for plugin settings: that is global config.
        self.config = config if config is not None else {}
        self._config_reader = ConfigReader(self.config)

        self._rng = random.Random()
        self._clock = time.monotonic
        self._last_group_reply: dict[str, float] = {}
        self._last_user_reply: dict[str, float] = {}
        self._recent_history: dict[str, list[dict[str, Any]]] = {}
        self._ambient_last_at: dict[str, float] = {}
        self._ambient_message_count: dict[str, int] = {}
        self._last_song_group: dict[str, float] = {}
        self._last_song_user: dict[str, float] = {}
        self._conversation_switch_lock = asyncio.Lock()
        self._prompt_profile_parse_error = False
        self._song_dir = SONG_DIR

        self._agent_persona_bridge = persona_bridge_engine.AgentPersonaBridge(
            persona_manager=getattr(context, "persona_manager", None),
            conversation_manager=getattr(context, "conversation_manager", None),
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        return self._config_reader.bool(key, default=default)

    def _cfg_int(
        self,
        key: str,
        minimum: int = 0,
        maximum: int | None = None,
    ) -> int:
        return self._config_reader.int(key, minimum=minimum, maximum=maximum)

    def _cfg_float(
        self,
        key: str,
        minimum: float = 0.0,
        maximum: float = 1.0,
    ) -> float:
        return self._config_reader.float(key, minimum=minimum, maximum=maximum)

    def _cfg_str(self, key: str, default: str = "") -> str:
        return self._config_reader.string(key, default=default)

    def _cfg_list(self, key: str) -> list[str]:
        return self._config_reader.string_list(key)

    def _memory_mode(self) -> str:
        value = self._cfg_str("memory_mode").strip().lower()
        if value in {"astrbot", "memory", "conversation", "long-term", "long_term"}:
            return "astrbot"
        return "direct"

    # ------------------------------------------------------------------
    # Main event handler
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=100)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        if not self._cfg_bool("enabled"):
            return

        direct_decision = False
        mode = ""
        original_text = ""
        try:
            components = self._components(event)
            if not self._valid_chat(event):
                return
            message_text = self._message_text(event)
            has_image = any(_is_component(comp, "Image") for comp in components)
            has_face = any(_is_component(comp, "Face") for comp in components)
            original_text = self._original_text(event, message_text)

            # Command exclusion must happen before probability, ambient, memory
            # wake flags or candidate extras.  Command owners stay authoritative.
            if self._is_command_message(event, message_text):
                return

            is_private = self._is_private(event)
            explicit = (
                is_private
                or self._is_bot_mentioned(event, components)
                or bool(getattr(event, "is_at_or_wake_command", False))
            )
            history = self._history_for(event)

            if not explicit and not is_private:
                if self._cfg_bool("ambient_group_chat_enabled"):
                    self._bump_ambient_message_count(event)
                self._remember_history(
                    event,
                    message_text,
                    has_image=has_image,
                    has_face=has_face,
                )
                if self._cfg_bool("ambient_group_chat_enabled"):
                    if not self._ambient_should_participate(
                        event,
                        message_text,
                        components,
                    ):
                        self._block_group_default_llm(event)
                        return
                    mode = "ambient"
                else:
                    if not self._group_random_eligible(
                        message_text,
                        components,
                    ) or not self._random_reply_allowed(event):
                        self._block_group_default_llm(event)
                        return
                    mode = "random"
            else:
                self._remember_history(
                    event,
                    message_text,
                    has_image=has_image,
                    has_face=has_face,
                )
                # Explicit pure-image / pure-face reaction.  This is local and
                # never calls an LLM; it still yields one candidate result.
                if (
                    (has_image or has_face)
                    and not message_text.strip()
                    and self._cfg_bool("user_sticker_reaction_enabled")
                ):
                    probability = self._cfg_float(
                        "user_sticker_reaction_probability",
                        0.0,
                        1.0,
                    )
                    if probability > 0 and (
                        probability >= 1.0
                        or self._rng.random() < probability
                    ):
                        result_text = self._finalize_reply(
                            self._pick_user_sticker_reaction_reply()
                        )
                        direct_decision = True
                        self._mark_candidate(event, original_text, ambient=False)
                        yield event.plain_result(result_text)
                        return
                mode = "explicit"

            if self._memory_mode() == "astrbot":
                # Let AstrBot's default Agent produce the single reply.  We
                # never call a provider directly in this mode, but we do mark
                # the candidate for sticker/safe-tools result hooks.
                self._mark_candidate(
                    event,
                    original_text,
                    ambient=(mode == "ambient"),
                )
                event.set_extra("qqbot_core_reply_memory_mode", True)
                try:
                    event.is_at_or_wake_command = True
                except Exception:
                    pass
                return

            direct_decision = True
            try:
                event.should_call_llm(True)
            except Exception:
                pass
            reply = await self._generate_reply(
                event,
                message_text=message_text,
                has_face=has_face,
                has_image=has_image,
                chat_history=history,
            )
            final_text = self._finalize_reply(reply)
            self._mark_candidate(
                event,
                original_text,
                ambient=(mode == "ambient"),
            )
            yield event.plain_result(final_text)
        except Exception:
            logger.exception("qqbot_core_reply 处理消息失败")
            if direct_decision:
                try:
                    fallback = self._finalize_reply(
                        self._cfg_str(
                            "llm_error_reply",
                            "我暂时没想好怎么回复，稍后再试试吧。",
                        )
                    )
                    self._mark_candidate(
                        event,
                        original_text,
                        ambient=(mode == "ambient"),
                    )
                    yield event.plain_result(fallback)
                except Exception:
                    logger.exception("qqbot_core_reply 发送降级回复失败")
        finally:
            if direct_decision:
                try:
                    event.stop_event()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _components(event: AstrMessageEvent) -> list[Any]:
        try:
            value = event.get_messages()
        except Exception:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        try:
            return list(value)
        except TypeError:
            return []

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False

    def _valid_chat(self, event: AstrMessageEvent) -> bool:
        if self._is_private(event):
            return True
        return bool(self._safe_group_id(event))

    @staticmethod
    def _original_text(event: AstrMessageEvent, fallback: str) -> str:
        raw = str(getattr(event, "message_str", "") or "")
        return raw if raw else str(fallback or "")

    def _message_text(self, event: AstrMessageEvent) -> str:
        parts: list[str] = []
        for component in self._components(event):
            if _is_component(component, "Plain"):
                text = getattr(component, "text", "")
                if text:
                    parts.append(str(text))
        text = " ".join(parts).strip()
        if not text:
            text = str(getattr(event, "message_str", "") or "").strip()
        limits = [
            self._cfg_int(key, 0, 10000)
            for key in ("max_input_chars", "max_message_chars")
        ]
        positive = [limit for limit in limits if limit > 0]
        if positive:
            text = text[: min(positive)]
        return text

    def _is_bot_mentioned(
        self,
        event: AstrMessageEvent,
        components: list[Any],
    ) -> bool:
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            return False
        if not self_id:
            return False
        for component in components:
            if _is_component(component, "AtAll"):
                continue
            if not _is_component(component, "At"):
                continue
            qq = str(getattr(component, "qq", "") or "").strip()
            if not qq or qq.lower() in {"all", "everyone"}:
                continue
            if qq == self_id:
                return True
        return False

    def _group_random_eligible(
        self,
        message_text: str,
        components: list[Any],
    ) -> bool:
        has_image = any(_is_component(comp, "Image") for comp in components)
        has_face = any(_is_component(comp, "Face") for comp in components)
        stripped = str(message_text or "").strip()
        if stripped:
            return len(stripped) >= self._cfg_int("min_message_chars", 0)
        if has_image and not self._cfg_bool("ignore_pure_image"):
            return True
        if has_face and not self._cfg_bool("ignore_pure_face"):
            return True
        return False

    def _random_reply_allowed(self, event: AstrMessageEvent) -> bool:
        now = self._clock()
        self._prune_reply_state(now)
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        user_key = f"{session_key}:{sender_id}"
        group_cooldown = self._cfg_int("group_cooldown_seconds", 0)
        user_cooldown = self._cfg_int("user_cooldown_seconds", 0)

        last_group = self._last_group_reply.get(session_key)
        if last_group is not None and now - last_group < group_cooldown:
            return False
        last_user = self._last_user_reply.get(user_key)
        if last_user is not None and now - last_user < user_cooldown:
            return False

        probability = self._cfg_float("reply_probability", 0.0, 1.0)
        if probability <= 0:
            return False
        if probability < 1.0 and self._rng.random() >= probability:
            return False

        # Reserve cooldown before the slow provider call to avoid bursts.
        self._last_group_reply[session_key] = now
        self._last_user_reply[user_key] = now
        return True

    def _block_group_default_llm(self, event: AstrMessageEvent) -> None:
        if not self._cfg_bool("block_default_llm_for_group_non_at"):
            return
        try:
            event.should_call_llm(True)
        except Exception:
            pass

    def _prune_reply_state(self, now: float) -> None:
        total = (
            len(self._last_group_reply)
            + len(self._last_user_reply)
            + len(self._ambient_last_at)
            + len(self._ambient_message_count)
            + len(self._recent_history)
            + len(self._last_song_group)
            + len(self._last_song_user)
        )
        if total < 4096:
            return
        cutoff = now - 86400
        for mapping in (
            self._last_group_reply,
            self._last_user_reply,
            self._ambient_last_at,
            self._last_song_group,
            self._last_song_user,
        ):
            for key, timestamp in list(mapping.items()):
                if timestamp < cutoff:
                    mapping.pop(key, None)
        if len(self._ambient_message_count) > 256:
            for key, _ in sorted(
                self._ambient_message_count.items(),
                key=lambda pair: pair[1],
            )[: len(self._ambient_message_count) - 256]:
                self._ambient_message_count.pop(key, None)
        self._prune_history(now)

    # ------------------------------------------------------------------
    # Bounded sender identity history
    # ------------------------------------------------------------------
    def _history_limit(self) -> int:
        limit = max(0, self._cfg_int("group_identity_history_limit", 0, 30))
        if self._cfg_bool("ambient_group_chat_enabled"):
            limit = max(
                limit,
                self._cfg_int("ambient_group_cache_limit", 0, 100),
            )
        return limit

    def _history_ttl(self) -> int:
        ttl = self._cfg_int("group_identity_history_ttl_seconds", 30, 3600)
        if self._cfg_bool("ambient_group_chat_enabled"):
            ttl = self._cfg_int("ambient_group_cache_ttl_seconds", 30, 3600)
        return ttl

    def _history_enabled(self) -> bool:
        return bool(
            self._cfg_bool("group_identity_context_enabled")
            or self._cfg_bool("ambient_group_chat_enabled")
        )

    def _prune_history(self, now: float) -> None:
        cutoff = now - self._history_ttl()
        limit = self._history_limit()
        for session, items in list(self._recent_history.items()):
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
                self._recent_history[session] = kept
            else:
                self._recent_history.pop(session, None)
        if len(self._recent_history) > 128:
            oldest = sorted(
                self._recent_history.items(),
                key=lambda pair: float(
                    pair[1][-1].get("timestamp", 0) if pair[1] else 0
                ),
            )
            for session, _ in oldest[: len(self._recent_history) - 128]:
                self._recent_history.pop(session, None)

    def _history_for(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        if not self._history_enabled() or self._is_private(event):
            return []
        self._prune_history(self._clock())
        session = str(getattr(event, "unified_msg_origin", "") or "")
        return [dict(item) for item in self._recent_history.get(session, [])]

    def _remember_history(
        self,
        event: AstrMessageEvent,
        message_text: str,
        *,
        has_image: bool,
        has_face: bool,
    ) -> None:
        if not self._history_enabled() or self._is_private(event):
            return
        text = str(message_text or "").strip()
        if not text:
            if has_image:
                text = "[图片]"
            elif has_face:
                text = "[QQ表情]"
            else:
                text = "[空消息]"
        text = _safe_text(
            text,
            max_chars=self._cfg_int("group_identity_message_max_chars", 20, 500),
            keep_newlines=True,
        )
        entry = {
            "nickname": self._safe_identity_nickname(event),
            "qq": self._safe_identity_qq(event),
            "text": text,
            "timestamp": self._clock(),
        }
        session = str(getattr(event, "unified_msg_origin", "") or "")
        items = self._recent_history.setdefault(session, [])
        items.append(entry)
        limit = self._history_limit()
        if limit <= 0:
            self._recent_history.pop(session, None)
        else:
            del items[:-limit]
        self._prune_history(self._clock())

    def _safe_identity_nickname(self, event: AstrMessageEvent) -> str:
        try:
            raw = str(event.get_sender_name() or "")
        except Exception:
            return ""
        return _safe_text(
            raw,
            max_chars=self._cfg_int(
                "group_identity_nickname_max_chars",
                1,
                64,
            ),
        )

    @staticmethod
    def _safe_identity_qq(event: AstrMessageEvent) -> str:
        try:
            raw = str(event.get_sender_id() or "").strip()
        except Exception:
            return ""
        return raw if re.fullmatch(r"[0-9]{1,20}", raw) else ""

    def _safe_group_id(self, event: AstrMessageEvent) -> str:
        if self._is_private(event):
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

    # ------------------------------------------------------------------
    # Persona / prompt profile resolution
    # ------------------------------------------------------------------
    def _parse_group_personas(self):
        return persona_engine.parse_group_personas(
            self._config_reader.value("group_personas"),
            max_groups=self._cfg_int("group_persona_max_groups", 0, 200),
            max_prompt_chars=self._cfg_int(
                "group_persona_max_prompt_chars",
                1,
                10_000,
            ),
            max_name_chars=self._cfg_int(
                "group_persona_max_name_chars",
                1,
                128,
            ),
        )

    def _parse_prompt_profiles(self):
        config = persona_engine.parse_prompt_profiles(
            self._config_reader.value("prompt_profiles"),
            self._cfg_str("prompt_profile_active_id"),
            max_profiles=100,
            max_prompt_chars=persona_engine.PROMPT_PROFILE_MAX_PROMPT_CHARS,
            max_name_chars=80,
        )
        self._prompt_profile_parse_error = bool(config.errors)
        return config

    def _resolve_event_persona(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str, str]:
        """Direct-mode system prompt: group > active profile > default > legacy."""

        legacy_prompt = self._cfg_str("system_prompt")
        parsed = None
        group_id = ""
        try:
            if self._cfg_bool("group_persona_enabled"):
                parsed = self._parse_group_personas()
                group_id = (
                    ""
                    if self._is_private(event)
                    else str(self._safe_group_id(event))
                )
                if (
                    group_id
                    and persona_engine.GROUP_ID_RE.fullmatch(group_id)
                    and group_id in parsed.groups
                ):
                    persona = parsed.groups[group_id]
                    return persona.prompt, persona.name, "group"
        except Exception:
            logger.warning(
                "qqbot_core_reply 群人格配置解析失败，回退旧 system_prompt"
            )
        try:
            profiles = self._parse_prompt_profiles()
            prompt, name, _profile_id, source = (
                persona_engine.resolve_prompt_profile(
                    profiles,
                    legacy_prompt="",
                )
            )
            if source == "profile" and prompt:
                return prompt, name, "profile"
        except Exception:
            logger.warning("qqbot_core_reply 提示词 profiles 解析失败")
        if parsed is not None and parsed.default is not None:
            return parsed.default.prompt, parsed.default.name, "default"
        return legacy_prompt, "", "legacy"

    def _agent_group_persona_target(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str, str, str, str] | None:
        parsed = persona_engine.PersonaConfig(default=None, groups={})
        try:
            if self._cfg_bool("group_persona_enabled"):
                parsed = self._parse_group_personas()
            is_private = self._is_private(event)
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
            profiles = self._parse_prompt_profiles()
            prompt, name, profile_id, source = (
                persona_engine.resolve_prompt_profile(profiles, legacy_prompt="")
            )
            if source == "profile" and prompt:
                persona_id = persona_bridge_engine.persona_id_for_profile(
                    profile_id
                )
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

    def _persona_option_line(self) -> str:
        try:
            parsed = self._parse_group_personas()
        except Exception:
            return "群人格：配置解析失败，已回退旧 system_prompt。"
        try:
            profiles = self._parse_prompt_profiles()
        except Exception:
            profiles = None
        if profiles is not None and profiles.errors:
            return (
                "提示词 profiles：格式非法或超过上限，已安全回退旧 system_prompt。"
            )
        if profiles is not None and profiles.active is not None:
            return (
                f"提示词：已加载 {len(profiles.profiles)} 个模板；"
                "active 用于 direct，pinned/active 作为 /新会话 默认模板。"
            )
        return persona_engine.persona_summary(parsed)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------
    def _build_prompt(
        self,
        event: AstrMessageEvent,
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
        scene = "私聊" if self._is_private(event) else "群聊"
        identity_enabled = self._cfg_bool("group_identity_context_enabled")
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
                self._cfg_int("group_identity_history_limit", 0, 30),
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
            if self._cfg_bool("ambient_group_chat_enabled") and chat_history:
                lines.append("最近群消息（无发送者标识）：")
                for item in chat_history:
                    history_text = str(item.get("text", "") or "")[:200]
                    if history_text:
                        lines.append(f"- {history_text}")

        if has_image:
            image_count = self._cfg_int("max_images_per_message", 1, 10)
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

    def _effective_system_prompt(
        self,
        event: AstrMessageEvent,
        tool_instruction: str = "",
    ) -> str:
        prompt, _persona_name, _source = self._resolve_event_persona(event)
        if tool_instruction:
            prompt = persona_bridge_engine.inject_safe_tools_instruction(
                prompt,
                tool_instruction,
            )
        return prompt

    def _finalize_reply(self, text: Any) -> str:
        if not isinstance(text, str):
            text = str(text or "")
        text = text.strip()
        if not text:
            text = self._cfg_str("empty_reply_fallback", "嗯嗯，我在听。")
        limit = self._cfg_int("max_reply_chars", 1, 4000)
        if limit > 0 and len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text

    def _pick_user_sticker_reaction_reply(self) -> str:
        replies = [
            str(item).strip()
            for item in self._cfg_list("user_sticker_reaction_replies")
            if str(item).strip()
        ]
        if not replies:
            return "收到啦～"
        return self._rng.choice(replies)

    def _scrub_identity_from_reply(
        self,
        reply: str,
        event: AstrMessageEvent,
        *,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        text = str(reply or "")
        if not self._cfg_bool("group_identity_redact_in_reply"):
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
    # Provider selection and direct LLM calls
    # ------------------------------------------------------------------
    async def _await_maybe(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _provider_id_from_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        meta = getattr(value, "meta", None)
        if callable(meta):
            try:
                meta = meta()
            except Exception:
                meta = None
        if meta is not None:
            if isinstance(meta, dict):
                found = meta.get("id")
            else:
                found = getattr(meta, "id", None)
            if found:
                return str(found)
        for attr in ("id", "provider_id"):
            found = getattr(value, attr, None)
            if found:
                return str(found)
        return ""

    async def _get_current_provider_id(self, event: AstrMessageEvent) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(getter):
            try:
                value = await self._await_maybe(getter(umo=umo))
                provider_id = self._provider_id_from_value(value)
                if provider_id:
                    return provider_id
            except Exception:
                logger.warning(
                    "qqbot_core_reply 获取当前 provider 失败", exc_info=True
                )
        for name in ("get_using_provider_async", "get_using_provider"):
            getter = getattr(self.context, name, None)
            if not callable(getter):
                continue
            try:
                value = await self._await_maybe(getter(umo=umo))
            except TypeError:
                try:
                    value = await self._await_maybe(getter())
                except Exception:
                    value = None
            except Exception:
                value = None
            provider_id = self._provider_id_from_value(value)
            if provider_id:
                return provider_id
        return ""

    def _provider_exists(self, provider_id: str) -> bool:
        getter = getattr(self.context, "get_provider_by_id", None)
        if not callable(getter):
            return True
        try:
            return getter(provider_id) is not None
        except Exception:
            return True

    def _provider_image_capability(self, provider: Any) -> bool | None:
        provider_config = getattr(provider, "provider_config", None)
        if not isinstance(provider_config, dict):
            return None
        modalities = provider_config.get("modalities", None)
        if modalities == []:
            return True
        if isinstance(modalities, (list, tuple)) and modalities:
            return "image" in modalities
        return None

    async def _llm_generate(self, **kwargs: Any) -> str:
        generator = getattr(self.context, "llm_generate", None)
        if not callable(generator):
            raise RuntimeError("AstrBot Context.llm_generate 不可用")
        timeout = self._cfg_int("llm_timeout_seconds", 1, 300)
        pending = generator(**kwargs)
        if inspect.isawaitable(pending):
            response = await asyncio.wait_for(pending, timeout=timeout)
        else:
            response = pending
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        text = getattr(response, "completion_text", "")
        if text is None:
            return ""
        return str(text).strip()

    async def _generate_reply(
        self,
        event: AstrMessageEvent,
        *,
        message_text: str,
        has_face: bool,
        has_image: bool = False,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        tool_instruction = await self._safe_tools_instruction()
        system_prompt = self._effective_system_prompt(event, tool_instruction)
        image_refs = (
            await self._collect_image_refs(event) if has_image else []
        )
        if has_image and not image_refs:
            reply = self._cfg_str(
                "image_unavailable_reply",
                "我收到图片啦，但暂时无法读取图片内容。",
            )
        elif image_refs:
            reply = await self._reply_to_images(
                event,
                image_refs=image_refs,
                message_text=message_text,
                has_face=has_face,
                system_prompt=system_prompt,
                chat_history=chat_history,
            )
        else:
            reply = await self._reply_to_text(
                event,
                message_text=message_text,
                has_face=has_face,
                system_prompt=system_prompt,
                chat_history=chat_history,
            )
        return self._scrub_identity_from_reply(
            reply,
            event,
            chat_history=chat_history,
        )

    async def _reply_to_text(
        self,
        event: AstrMessageEvent,
        *,
        message_text: str,
        has_face: bool,
        system_prompt: str | None = None,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        if system_prompt is None:
            system_prompt = self._effective_system_prompt(event)
        provider_id = await self._get_current_provider_id(event)
        if not provider_id:
            return self._cfg_str(
                "llm_error_reply",
                "我暂时没想好怎么回复，稍后再试试吧。",
            )
        prompt = self._build_prompt(
            event,
            message_text,
            has_face=has_face,
            has_image=False,
            chat_history=chat_history,
        )
        try:
            reply = await self._llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
        except Exception:
            logger.warning("qqbot_core_reply 文本 LLM 调用失败", exc_info=True)
            return self._cfg_str(
                "llm_error_reply",
                "我暂时没想好怎么回复，稍后再试试吧。",
            )
        return reply or self._cfg_str("empty_reply_fallback", "嗯嗯，我在听。")

    async def _reply_to_images(
        self,
        event: AstrMessageEvent,
        *,
        image_refs: list[str],
        message_text: str,
        has_face: bool,
        system_prompt: str | None = None,
        chat_history: list[dict[str, Any]] | None = None,
    ) -> str:
        if system_prompt is None:
            system_prompt = self._effective_system_prompt(event)
        provider_id = await self._get_current_provider_id(event)
        prompt = self._build_prompt(
            event,
            message_text,
            has_face=has_face,
            has_image=True,
            chat_history=chat_history,
        )
        native_attempted = False
        if (
            provider_id
            and self._cfg_bool("native_vision_first")
        ):
            provider = None
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                try:
                    provider = getter(provider_id)
                except Exception:
                    provider = None
            capability = self._provider_image_capability(provider)
            if capability is not False:
                native_attempted = True
                try:
                    reply = await self._llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        image_urls=image_refs,
                        system_prompt=system_prompt,
                    )
                    if reply:
                        return reply
                    logger.warning(
                        "qqbot_core_reply 原生视觉调用返回空内容，尝试图片转述降级"
                    )
                except Exception:
                    logger.warning(
                        "qqbot_core_reply 原生视觉调用失败，尝试图片转述降级",
                        exc_info=True,
                    )

        caption = await self._caption_images(event, image_refs)
        if caption:
            if provider_id:
                try:
                    caption_prompt = self._build_prompt(
                        event,
                        message_text,
                        has_face=has_face,
                        has_image=True,
                        image_caption=caption,
                        chat_history=chat_history,
                    )
                    reply = await self._llm_generate(
                        chat_provider_id=provider_id,
                        prompt=caption_prompt,
                        system_prompt=system_prompt,
                    )
                    if reply:
                        return reply
                except Exception:
                    logger.warning(
                        "qqbot_core_reply 基于图片转述生成回复失败，直接返回描述",
                        exc_info=True,
                    )
            return f"我看了一下图片：{caption}"

        if native_attempted:
            logger.warning(
                "qqbot_core_reply 原生视觉和图片转述均不可用，使用兜底回复"
            )
        return self._cfg_str(
            "image_unavailable_reply",
            "我收到图片啦，但暂时无法读取图片内容。",
        )

    async def _caption_images(
        self,
        event: AstrMessageEvent,
        image_refs: list[str],
    ) -> str:
        candidates = self._caption_provider_candidates(event)
        if not candidates:
            return ""
        caption_prompt = self._cfg_str(
            "image_caption_prompt",
            "请用中文客观描述这张图片的内容。",
        )
        max_caption = self._cfg_int("max_caption_chars", 1, 5000)
        for provider_id in candidates:
            if not self._provider_exists(provider_id):
                continue
            try:
                caption = await self._llm_generate(
                    chat_provider_id=provider_id,
                    prompt=caption_prompt,
                    image_urls=image_refs,
                    system_prompt="你只负责客观描述图片，不要编造不存在的内容。",
                )
            except Exception:
                logger.warning(
                    "qqbot_core_reply 图片转述 provider %s 调用失败",
                    provider_id,
                    exc_info=True,
                )
                continue
            if caption:
                return caption[:max_caption].strip()
        return ""

    def _caption_provider_candidates(
        self,
        event: AstrMessageEvent,
    ) -> list[str]:
        candidates: list[str] = []
        plugin_provider = self._cfg_str("vision_provider_id").strip()
        if plugin_provider:
            candidates.append(plugin_provider)
        if self._cfg_bool("auto_use_astrbot_caption_provider"):
            global_config = self._global_config(event)
            provider_settings = _mapping_value(
                global_config,
                "provider_settings",
                {},
            )
            if isinstance(provider_settings, dict):
                global_provider = str(
                    provider_settings.get(
                        "default_image_caption_provider_id",
                        "",
                    )
                    or ""
                ).strip()
                if global_provider:
                    candidates.append(global_provider)
        return list(dict.fromkeys(candidates))

    def _global_config(self, event: AstrMessageEvent) -> Any:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return {}
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            return getter(umo=umo)
        except TypeError:
            try:
                return getter()
            except Exception:
                return {}
        except Exception:
            return {}

    async def _collect_image_refs(
        self,
        event: AstrMessageEvent,
    ) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        max_images = self._cfg_int("max_images_per_message", 1, 10)
        for component in self._components(event):
            if not _is_component(component, "Image"):
                continue
            if len(refs) >= max_images:
                break
            chosen = ""
            for candidate in (
                getattr(component, "url", None),
                getattr(component, "file", None),
                getattr(component, "path", None),
            ):
                if isinstance(candidate, str) and media_engine.is_media_ref(
                    candidate
                ):
                    chosen = candidate
                    break
            if not chosen:
                converter = getattr(component, "convert_to_base64", None)
                if callable(converter):
                    try:
                        data = await self._await_maybe(converter())
                    except Exception:
                        data = None
                    if isinstance(data, bytes):
                        data = base64.b64encode(data).decode("ascii")
                    if isinstance(data, str) and data.strip():
                        chosen = (
                            data
                            if data.startswith(("base64://", "data:image/"))
                            else f"base64://{data.strip()}"
                        )
            if chosen and chosen not in seen:
                seen.add(chosen)
                refs.append(chosen)
        return refs

    # ------------------------------------------------------------------
    # Safe-tools handshake (dynamic only; never import the plugin)
    # ------------------------------------------------------------------
    def _safe_tools_star(self) -> Any:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            meta = getter("qqbot_safe_tools")
        except Exception:
            return None
        if meta is None:
            return None
        if getattr(meta, "activated", True) is False:
            return None
        return getattr(meta, "star_cls", None) or meta

    def _safe_tools_available(self) -> bool:
        star = self._safe_tools_star()
        return bool(star is not None and callable(getattr(star, "tool_instruction", None)))

    async def _safe_tools_instruction(self) -> str:
        if not self._cfg_bool("safe_tools_marker_enabled"):
            return ""
        star = self._safe_tools_star()
        instruction_fn = (
            getattr(star, "tool_instruction", None) if star is not None else None
        )
        if not callable(instruction_fn):
            # No handshake => do not offer a marker protocol at all.
            return ""
        try:
            value = self._call_tool_instruction(instruction_fn)
            value = await self._await_maybe(value)
        except Exception:
            logger.warning(
                "qqbot_core_reply 读取 qqbot_safe_tools.tool_instruction() 失败",
                exc_info=True,
            )
            return ""
        if not isinstance(value, str):
            return ""
        return value.strip()

    @staticmethod
    def _call_tool_instruction(instruction_fn: Any) -> Any:
        """Call the stable no-arg interface once, accepting a one-arg variant."""

        try:
            signature = inspect.signature(instruction_fn)
        except (TypeError, ValueError):
            return instruction_fn()
        required = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and parameter.default is inspect.Parameter.empty
        ]
        if required:
            return instruction_fn(None)
        return instruction_fn()

    # ------------------------------------------------------------------
    # Candidate contract + command exclusion
    # ------------------------------------------------------------------
    def _mark_candidate(
        self,
        event: AstrMessageEvent,
        original_text: str,
        *,
        ambient: bool,
    ) -> None:
        event.set_extra(CANDIDATE_EXTRA, True)
        event.set_extra(ALLOW_STICKER_EXTRA, True)
        event.set_extra(TEXT_EXTRA, str(original_text or ""))
        if ambient:
            event.set_extra(AMBIENT_EXTRA, True)

    def _command_prefixes(self, event: AstrMessageEvent) -> list[str]:
        prefixes = list(self._cfg_list("ignore_command_prefixes"))
        if self._cfg_bool("inherit_astrbot_wake_prefix"):
            global_config = self._global_config(event)
            wake_prefixes = _mapping_value(global_config, "wake_prefix", [])
            if isinstance(wake_prefixes, str):
                wake_prefixes = [wake_prefixes]
            if isinstance(wake_prefixes, (list, tuple)):
                prefixes.extend(
                    str(prefix)
                    for prefix in wake_prefixes
                    if str(prefix).strip()
                )
        return sorted(
            {prefix for prefix in prefixes if prefix},
            key=len,
            reverse=True,
        )

    def _is_command_message(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        stripped = str(text or "").lstrip()
        raw = str(getattr(event, "message_str", "") or "").lstrip()
        candidates = [value for value in (stripped, raw) if value]
        if not candidates:
            return False
        prefixes = self._command_prefixes(event)
        for value in candidates:
            if any(value.startswith(prefix) for prefix in prefixes):
                return True
            stripped_prefixes = value
            for prefix in prefixes:
                if stripped_prefixes.startswith(prefix):
                    stripped_prefixes = stripped_prefixes[len(prefix) :].lstrip()
                    break
            if self._is_builtin_command_text(stripped_prefixes):
                return True
        return False

    @staticmethod
    def _is_builtin_command_text(text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        names = (
            "会话列表",
            "切换会话",
            "会话",
            "新会话",
            "删除会话",
            "会话删除",
            "option",
            "插件用法",
            "用法",
            "唱歌",
            "点歌",
        )
        for name in names:
            if stripped == name:
                return True
            if stripped.startswith(name) and len(stripped) > len(name):
                if stripped[len(name)].isspace():
                    return True
        return False

    def _collect_command_argument(
        self,
        event: AstrMessageEvent,
        command_names: tuple[str, ...],
    ) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        for prefix in self._command_prefixes(event):
            if prefix and text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        for command_name in command_names:
            if text == command_name:
                return ""
            if text.startswith(command_name):
                rest = text[len(command_name) :]
                if rest and rest[0].isspace():
                    argument = re.sub(r"\s+", " ", rest).strip()
                    return "".join(
                        char for char in argument if char.isprintable()
                    ).strip()
        return ""

    # ------------------------------------------------------------------
    # AstrBot ConversationManager helpers
    # ------------------------------------------------------------------
    async def _await_conversation_api(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _conversation_manager(self) -> Any:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        if not callable(getattr(manager, "get_conversations", None)):
            return None
        if not callable(getattr(manager, "switch_conversation", None)):
            return None
        return manager

    async def _list_conversations(self, umo: str) -> list[Any]:
        manager = self._conversation_manager()
        if manager is None:
            return []
        try:
            value = await self._await_conversation_api(
                manager.get_conversations(umo)
            )
        except Exception:
            return []
        return list(value) if isinstance(value, (list, tuple)) else []

    async def _current_conversation_id(self, umo: str) -> str:
        manager = self._conversation_manager()
        getter = (
            getattr(manager, "get_curr_conversation_id", None)
            if manager is not None
            else None
        )
        if not callable(getter):
            return ""
        try:
            value = await self._await_conversation_api(getter(umo))
        except Exception:
            return ""
        return str(value or "")

    async def _get_conversation(self, event: AstrMessageEvent, cid: str) -> Any:
        manager = self._conversation_manager()
        getter = (
            getattr(manager, "get_conversation", None)
            if manager is not None
            else None
        )
        if not callable(getter) or not cid:
            return None
        try:
            return await self._await_conversation_api(
                getter(str(event.unified_msg_origin), cid)
            )
        except Exception:
            return None

    def _conversation_limit(self) -> int:
        return min(
            50,
            max(1, self._cfg_int("conversation_max_per_group", 1, 50)),
        )

    def _conversation_list_limit(self) -> int:
        return min(
            50,
            max(1, self._cfg_int("conversation_list_limit", 1, 50)),
        )

    async def _prepare_conversation_slot(
        self,
        event: AstrMessageEvent,
    ) -> tuple[bool, str]:
        manager = self._conversation_manager()
        if manager is None:
            return False, "unsupported"
        conversations = await self._list_conversations(
            str(event.unified_msg_origin)
        )
        limit = self._conversation_limit()
        if len(conversations) < limit:
            return True, "ok"
        if not self._cfg_bool("conversation_auto_prune_enabled"):
            return False, "limit"
        current = await self._current_conversation_id(
            str(event.unified_msg_origin)
        )
        if not current:
            return False, "no_candidate"
        delete = getattr(manager, "delete_conversation", None)
        if not callable(delete):
            return False, "unsupported_delete"
        needed = len(conversations) - limit + 1
        candidates = [
            conversation
            for conversation in conversations
            if conversation_engine.conversation_id(conversation) != current
        ]
        candidates.sort(key=conversation_engine.conversation_created_at)
        if len(candidates) < needed:
            return False, "no_candidate"
        for conversation in candidates[:needed]:
            cid = conversation_engine.conversation_id(conversation)
            if not cid:
                return False, "no_candidate"
            try:
                result = await self._await_conversation_api(
                    delete(str(event.unified_msg_origin), cid)
                )
                if result is False:
                    return False, "delete_failed"
            except Exception:
                return False, "delete_failed"
        return True, "pruned"

    async def _delete_conversation(
        self,
        event: AstrMessageEvent,
        target: Any,
    ) -> tuple[str, str]:
        manager = self._conversation_manager()
        if manager is None:
            return "unsupported", ""
        delete = getattr(manager, "delete_conversation", None)
        if not callable(delete):
            return "unsupported", ""
        target_cid = conversation_engine.conversation_id(target)
        if not target_cid:
            return "not_found", ""
        title = conversation_engine.conversation_title(target) or "（未命名会话）"
        conversations = await self._list_conversations(
            str(event.unified_msg_origin)
        )
        current = await self._current_conversation_id(
            str(event.unified_msg_origin)
        )
        switched = False
        if target_cid == current:
            remaining = [
                conversation
                for conversation in conversations
                if conversation_engine.conversation_id(conversation) != target_cid
            ]
            if not remaining:
                return "last", title
            remaining.sort(
                key=conversation_engine.conversation_created_at,
                reverse=True,
            )
            replacement = conversation_engine.conversation_id(remaining[0])
            if not replacement:
                return "last", title
            try:
                await self._await_conversation_api(
                    manager.switch_conversation(
                        str(event.unified_msg_origin),
                        replacement,
                    )
                )
            except Exception:
                return "switch_failed", title
            switched = True
            confirmed = await self._current_conversation_id(
                str(event.unified_msg_origin)
            )
            if confirmed and confirmed != replacement:
                return "switch_failed", title
        try:
            result = await self._await_conversation_api(
                delete(str(event.unified_msg_origin), target_cid)
            )
            if result is False:
                raise RuntimeError("delete returned false")
        except Exception:
            if switched:
                try:
                    await self._await_conversation_api(
                        manager.switch_conversation(
                            str(event.unified_msg_origin),
                            target_cid,
                        )
                    )
                except Exception:
                    pass
            return "delete_failed", title
        return "deleted", title

    async def _new_agent_conversation(
        self,
        event: AstrMessageEvent,
        title: str,
        prompt_profile: Any = None,
    ) -> str:
        manager = self._conversation_manager()
        if manager is None or not callable(
            getattr(manager, "new_conversation", None)
        ):
            raise RuntimeError("conversation manager unsupported")
        bridge = self._agent_persona_bridge
        persona_id = ""
        if prompt_profile is not None:
            candidate_id = persona_bridge_engine.persona_id_for_profile(
                prompt_profile.id
            )
            if not candidate_id or bridge is None:
                raise RuntimeError("prompt persona unsupported")
            if not await bridge.ensure_persona(candidate_id, prompt_profile.prompt):
                raise RuntimeError("prompt persona unavailable")
            persona_id = candidate_id
        else:
            profiles = self._parse_prompt_profiles()
            pinned = profiles.default
            if pinned is not None:
                candidate_id = persona_bridge_engine.persona_id_for_profile(
                    pinned.id
                )
                if candidate_id and bridge is not None:
                    if await bridge.ensure_persona(candidate_id, pinned.prompt):
                        persona_id = candidate_id
            if not persona_id:
                target = self._agent_group_persona_target(event)
                if target is not None:
                    prompt, _name, _source, candidate_id = target
                    if bridge is not None and await bridge.ensure_persona(
                        candidate_id,
                        prompt,
                    ):
                        persona_id = candidate_id

        new_conversation = manager.new_conversation
        kwargs: dict[str, Any] = {"title": title}
        try:
            parameters = inspect.signature(new_conversation).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if not parameters or "persona_id" in parameters or accepts_kwargs:
            kwargs["persona_id"] = persona_id or None
        value = await self._await_conversation_api(
            new_conversation(str(event.unified_msg_origin), **kwargs)
        )
        cid = conversation_engine.conversation_id(value)
        if not cid:
            cid = await self._current_conversation_id(
                str(event.unified_msg_origin)
            )
        if not cid:
            raise RuntimeError("new conversation returned no id")
        await self._await_conversation_api(
            manager.switch_conversation(str(event.unified_msg_origin), cid)
        )
        if persona_id:
            current = await self._get_conversation(event, cid)
            if (
                current is not None
                and conversation_engine.conversation_persona_id(current)
                != persona_id
            ):
                raise RuntimeError("created conversation persona mismatch")
        return cid

    async def _switch_agent_conversation(
        self,
        event: AstrMessageEvent,
        cid: str,
    ) -> bool:
        manager = self._conversation_manager()
        if manager is None or not cid:
            return False
        try:
            await self._await_conversation_api(
                manager.switch_conversation(
                    str(event.unified_msg_origin),
                    cid,
                )
            )
            return True
        except Exception:
            return False

    def _conversation_title_from_command(
        self,
        event: AstrMessageEvent,
        command_names: tuple[str, ...],
    ) -> str | None:
        raw = self._collect_command_argument(event, command_names)
        return conversation_engine.normalize_title(
            raw,
            max_chars=self._cfg_int("conversation_title_max_chars", 1, 64),
        )

    def _new_conversation_arguments(
        self,
        event: AstrMessageEvent,
    ) -> tuple[str | None, str | None]:
        raw = self._collect_command_argument(event, ("新会话",)).strip()
        if not raw:
            return None, None
        parts = raw.split(None, 1)
        title = conversation_engine.normalize_title(
            parts[0],
            max_chars=self._cfg_int("conversation_title_max_chars", 1, 64),
        )
        prompt_title = parts[1].strip() if len(parts) > 1 else ""
        return title, prompt_title or None

    def _prompt_profile_for_template(self, reference: str):
        try:
            config = self._parse_prompt_profiles()
        except Exception:
            return None
        return persona_engine.find_prompt_profile(config, reference)

    def _default_prompt_profile(self):
        try:
            return self._parse_prompt_profiles().default
        except Exception:
            return None

    def _conversation_prompt_label(self, persona_id: str) -> str:
        value = str(persona_id or "").strip()
        if not value:
            return "默认/未绑定"
        try:
            config = self._parse_prompt_profiles()
        except Exception:
            config = None
        if config is not None:
            for profile in config.profiles:
                if (
                    persona_bridge_engine.persona_id_for_profile(profile.id)
                    == value
                ):
                    return profile.name
        if value.startswith(persona_bridge_engine.AGENT_PERSONA_PREFIX):
            return "群人格"
        return "自定义人格"

    def _conversation_usage_reply(self) -> str:
        return (
            "用法：/会话列表；/会话 名称（只切换已有）；"
            "/新会话 名称 [提示词标题]；/删除会话 名称|短ID"
        )

    def _conversation_unsupported_reply(self) -> str:
        return "当前 AstrBot 后端没有可用的官方会话管理 API，未执行切换。"

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("会话列表", priority=110)
    async def conversation_list_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = ""
        try:
            if not self._cfg_bool("conversation_switch_enabled"):
                text = "会话切换功能当前未启用。"
            elif self._is_private(event):
                text = "私聊默认使用当前会话，无需切换。"
            else:
                manager = self._conversation_manager()
                if manager is None:
                    text = self._conversation_unsupported_reply()
                else:
                    conversations = await self._list_conversations(
                        str(event.unified_msg_origin)
                    )
                    if not conversations:
                        text = "当前群还没有可用会话。"
                    else:
                        current = await self._current_conversation_id(
                            str(event.unified_msg_origin)
                        )
                        limit = self._conversation_list_limit()
                        total_limit = self._conversation_limit()
                        lines = [
                            f"当前群会话：{len(conversations)} / 上限 {total_limit} "
                            f"（列表最多显示 {limit} 个）"
                        ]
                        if len(conversations) >= total_limit:
                            lines.append(
                                "已达到上限；请用 /删除会话 清理。"
                                if not self._cfg_bool(
                                    "conversation_auto_prune_enabled"
                                )
                                else "已达到上限；新建时会自动清理最老非当前会话。"
                            )
                        for conversation in conversations[:limit]:
                            cid = conversation_engine.conversation_id(conversation)
                            title = (
                                conversation_engine.conversation_title(conversation)
                                or "（未命名会话）"
                            )
                            marker = " ← 当前" if cid and cid == current else ""
                            prompt_label = self._conversation_prompt_label(
                                conversation_engine.conversation_persona_id(
                                    conversation
                                )
                            )
                            lines.append(
                                f"- {title} [id:{conversation_engine.short_id(cid)}] "
                                f"提示词：{prompt_label}{marker}"
                            )
                        text = "\n".join(lines)
        except Exception:
            logger.info("qqbot_core_reply /会话列表 查询失败")
            text = "会话列表读取失败，请稍后再试。"
        result = event.plain_result(text)
        yield result
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("会话", alias={"切换会话"}, priority=110)
    async def conversation_switch_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = ""
        try:
            if not self._cfg_bool("conversation_switch_enabled"):
                text = "会话切换功能当前未启用。"
            elif self._is_private(event):
                text = "私聊默认使用当前会话，无需切换。"
            else:
                title = self._conversation_title_from_command(
                    event,
                    ("会话", "切换会话"),
                )
                if title is None:
                    text = self._conversation_usage_reply()
                else:
                    manager = self._conversation_manager()
                    if manager is None:
                        text = self._conversation_unsupported_reply()
                    else:
                        async with self._conversation_switch_lock:
                            conversations = await self._list_conversations(
                                str(event.unified_msg_origin)
                            )
                            matches = conversation_engine.find_all_by_title(
                                conversations,
                                title,
                            )
                            if len(matches) > 1:
                                ids = "、".join(
                                    conversation_engine.short_id(
                                        conversation_engine.conversation_id(item)
                                    )
                                    for item in matches[:5]
                                )
                                text = (
                                    f"找到多个同名会话「{title}」（id:{ids}），"
                                    "请先在 AstrBot 会话列表改名后再切换；"
                                    "本次未自动取第一个。"
                                )
                            elif matches:
                                cid = conversation_engine.conversation_id(
                                    matches[0]
                                )
                                if await self._switch_agent_conversation(
                                    event,
                                    cid,
                                ):
                                    text = f"已切换到会话「{title}」。"
                                else:
                                    text = self._conversation_unsupported_reply()
                            else:
                                text = (
                                    f"没有找到会话「{title}」，未执行切换；"
                                    f"请用 /新会话 {title} 创建。"
                                )
        except Exception:
            logger.info("qqbot_core_reply /会话 切换失败")
            text = "会话切换失败，当前会话保持不变。"
        result = event.plain_result(text)
        yield result
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("新会话", priority=110)
    async def conversation_new_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = ""
        try:
            if not self._cfg_bool("conversation_switch_enabled"):
                text = "会话切换功能当前未启用。"
            elif self._is_private(event):
                text = "私聊默认使用当前会话，无需切换。"
            else:
                title, prompt_title = self._new_conversation_arguments(event)
                if title is None:
                    text = self._conversation_usage_reply()
                else:
                    prompt_profile = (
                        self._prompt_profile_for_template(prompt_title)
                        if prompt_title
                        else self._default_prompt_profile()
                    )
                    if prompt_title and prompt_profile is None:
                        text = (
                            f"未找到提示词模板「{prompt_title}」，未创建会话。"
                        )
                    else:
                        manager = self._conversation_manager()
                        if manager is None:
                            text = self._conversation_unsupported_reply()
                        else:
                            async with self._conversation_switch_lock:
                                conversations = await self._list_conversations(
                                    str(event.unified_msg_origin)
                                )
                                if conversation_engine.find_all_by_title(
                                    conversations,
                                    title,
                                ):
                                    text = (
                                        f"同名会话「{title}」已存在，"
                                        f"可用 /会话 {title} 切换。"
                                    )
                                else:
                                    prepared, reason = (
                                        await self._prepare_conversation_slot(
                                            event
                                        )
                                    )
                                    if not prepared:
                                        if reason == "limit":
                                            text = (
                                                "已达到当前会话上限，自动清理未启用；"
                                                "请先 /删除会话 或调高上限。"
                                            )
                                        elif reason in (
                                            "delete_failed",
                                            "unsupported_delete",
                                        ):
                                            text = (
                                                "会话数量已达上限，自动删除最老会话失败；"
                                                "已中止创建，现有会话保持不变。"
                                            )
                                        else:
                                            text = (
                                                "会话数量已达上限，且没有可安全删除的"
                                                "非当前会话；已中止创建。"
                                            )
                                    else:
                                        await self._new_agent_conversation(
                                            event,
                                            title,
                                            prompt_profile,
                                        )
                                        suffix = (
                                            f"\n提示词：{prompt_profile.name}"
                                            if prompt_profile is not None
                                            else ""
                                        )
                                        text = (
                                            f"已新建并切换到会话「{title}」。"
                                            f"{suffix}"
                                        )
        except Exception:
            logger.info("qqbot_core_reply /新会话 创建失败")
            text = "新会话创建失败，当前会话保持不变。"
        result = event.plain_result(text)
        yield result
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("删除会话", alias={"会话删除"}, priority=110)
    async def conversation_delete_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = ""
        try:
            if not self._cfg_bool("conversation_switch_enabled"):
                text = "会话切换功能当前未启用。"
            elif self._is_private(event):
                text = "私聊默认使用当前会话，无需切换。"
            else:
                raw = self._collect_command_argument(
                    event,
                    ("删除会话", "会话删除"),
                ).strip()
                if not raw:
                    text = "用法：/删除会话 名称|短ID"
                else:
                    manager = self._conversation_manager()
                    delete_allowed = manager is not None and callable(
                        getattr(manager, "delete_conversation", None)
                    )
                    if not delete_allowed:
                        text = (
                            "当前 AstrBot 后端没有可用的官方会话删除 API，"
                            "未删除任何会话。"
                        )
                    else:
                        async with self._conversation_switch_lock:
                            conversations = await self._list_conversations(
                                str(event.unified_msg_origin)
                            )
                            title_matches = conversation_engine.find_all_by_title(
                                conversations,
                                raw,
                            )
                            if len(title_matches) > 1:
                                ids = "、".join(
                                    conversation_engine.short_id(
                                        conversation_engine.conversation_id(item)
                                    )
                                    for item in title_matches[:5]
                                )
                                text = (
                                    f"找到多个同名会话「{raw}」（id:{ids}），"
                                    "请使用唯一短 ID 删除，未删除任何会话。"
                                )
                            else:
                                matches = title_matches
                                if not matches:
                                    wanted = raw.casefold()
                                    matches = [
                                        conversation
                                        for conversation in conversations
                                        if (
                                            conversation_engine.short_id(
                                                conversation_engine.conversation_id(
                                                    conversation
                                                )
                                            ).casefold()
                                            == wanted
                                            or conversation_engine.conversation_id(
                                                conversation
                                            ).casefold()
                                            == wanted
                                        )
                                    ]
                                if not matches:
                                    text = (
                                        f"没有找到会话「{raw}」，"
                                        "未删除任何会话。"
                                    )
                                elif len(matches) > 1:
                                    text = (
                                        "短 ID 匹配到多个会话，请使用更完整的 ID；"
                                        "未删除任何会话。"
                                    )
                                else:
                                    status, deleted_title = (
                                        await self._delete_conversation(
                                            event,
                                            matches[0],
                                        )
                                    )
                                    if status == "last":
                                        text = (
                                            "不能删除最后一个会话；"
                                            "当前会话保持不变。"
                                        )
                                    elif status == "switch_failed":
                                        text = (
                                            "删除当前会话前切换失败，"
                                            "未删除任何会话。"
                                        )
                                    elif status in (
                                        "unsupported",
                                        "not_found",
                                    ):
                                        text = (
                                            "当前 AstrBot 后端没有可用的官方"
                                            "会话删除 API，未删除任何会话。"
                                        )
                                    elif status == "delete_failed":
                                        text = (
                                            "删除会话失败，未伪造删除成功；"
                                            "请稍后再试。"
                                        )
                                    else:
                                        remaining = await self._list_conversations(
                                            str(event.unified_msg_origin)
                                        )
                                        text = (
                                            f"已删除会话「{deleted_title}」；"
                                            f"当前剩余 {len(remaining)} 个会话。"
                                        )
        except Exception:
            logger.info("qqbot_core_reply /删除会话 处理失败")
            text = "删除会话失败，未删除任何会话。"
        result = event.plain_result(text)
        yield result
        event.stop_event()

    # ------------------------------------------------------------------
    # Ambient group mode
    # ------------------------------------------------------------------
    def _bump_ambient_message_count(self, event: AstrMessageEvent) -> None:
        session = str(getattr(event, "unified_msg_origin", "") or "")
        count = self._ambient_message_count.get(session, 0) + 1
        self._ambient_message_count[session] = count
        if len(self._ambient_message_count) > 256:
            for key, _ in sorted(
                self._ambient_message_count.items(),
                key=lambda pair: pair[1],
            )[: len(self._ambient_message_count) - 256]:
                self._ambient_message_count.pop(key, None)

    def _ambient_group_should_participate(
        self,
        event: AstrMessageEvent,
        message_text: str,
        components: list[Any],
    ) -> bool:
        """Legacy-compatible alias for the ambient decision."""

        return self._ambient_should_participate(
            event,
            message_text,
            components,
        )

    def _ambient_should_participate(
        self,
        event: AstrMessageEvent,
        message_text: str,
        components: list[Any],
    ) -> bool:
        if not self._cfg_bool("ambient_group_chat_enabled"):
            return False
        if self._is_private(event):
            return False
        if not self._group_random_eligible(message_text, components):
            return False
        session = str(getattr(event, "unified_msg_origin", "") or "")
        now = self._clock()
        self._prune_reply_state(now)
        min_interval = self._cfg_int(
            "ambient_group_min_interval_seconds",
            0,
        )
        last = self._ambient_last_at.get(session)
        if last is not None and now - last < min_interval:
            return False
        round_interval = self._cfg_int("ambient_group_round_interval", 0)
        message_count = self._ambient_message_count.get(session, 0)
        if round_interval > 0 and message_count % round_interval != 0:
            return False
        keywords = self._cfg_list("ambient_group_keywords")
        folded = str(message_text or "").casefold()
        keyword_hit = any(
            keyword.casefold() in folded
            for keyword in keywords
            if keyword
        )
        probability = (
            self._cfg_float(
                "ambient_group_keyword_probability",
                0.0,
                1.0,
            )
            if keyword_hit
            else self._cfg_float("reply_probability", 0.0, 1.0)
        )
        if probability <= 0:
            return False
        if probability < 1.0 and self._rng.random() >= probability:
            return False
        self._ambient_last_at[session] = now
        return True

    # ------------------------------------------------------------------
    # /option usage and diagnostics
    # ------------------------------------------------------------------
    def _loaded_star_metadata(self) -> list[Any]:
        getter = getattr(self.context, "get_all_stars", None)
        if not callable(getter):
            return []
        try:
            stars = getter()
        except Exception:
            return []
        if not isinstance(stars, (list, tuple)):
            return []
        return [star for star in stars if star is not None]

    def _identity_option_line(self) -> str:
        if not self._cfg_bool("group_identity_context_enabled"):
            return "群聊身份上下文：关闭。"
        if self._memory_mode() == "astrbot":
            return (
                "群聊身份上下文：memory 模式通过官方 on_llm_request "
                "注入当前发送者；历史消息仍无逐消息身份前缀。"
            )
        return (
            "群聊身份上下文：direct 模式已启用，最近消息带 "
            "[昵称(QQ)] 前缀；QQ 仅用于区分，不展示。"
        )

    def _build_option_text(self) -> str:
        display_name = PLUGIN_DISPLAY_NAME
        version = PLUGIN_VERSION
        for star in self._loaded_star_metadata():
            star_name = str(getattr(star, "name", "") or "")
            if (
                getattr(star, "star_cls", None) is self
                or star_name == PLUGIN_NAME
                or getattr(star, "root_dir_name", "") == PLUGIN_NAME
            ):
                display_name = (
                    str(getattr(star, "display_name", "") or "")
                    or star_name
                    or display_name
                )
                version = str(getattr(star, "version", "") or version)
                break
        mode = self._memory_mode()
        probability = self._cfg_float("reply_probability", 0.0, 1.0)
        group_cooldown = self._cfg_int("group_cooldown_seconds", 0)
        user_cooldown = self._cfg_int("user_cooldown_seconds", 0)
        ambient = "开启" if self._cfg_bool("ambient_group_chat_enabled") else "关闭"
        safe_tools = (
            "已检测到 qqbot_safe_tools.tool_instruction()，direct prompt 会追加工具说明。"
            if self._safe_tools_available()
            else "未检测到 qqbot_safe_tools.tool_instruction()；不提供 marker 协议。"
        )
        lines = [
            f"{display_name} 用法速览（v{version}）",
            "",
            "唯一主回复 / LLM owner：qqbot_core_reply。",
            "本插件命令：",
            "/唱歌 [文件名关键词]（别名 /点歌）",
            "/会话列表（群聊查看 AstrBot 会话）",
            "/会话 <名称>（别名 /切换会话；只切换已有）",
            "/新会话 <名称> [提示词标题]（群聊新建并切换）",
            "/删除会话 <名称|短ID>（别名 /会话删除）",
            "/option（别名 /插件用法、/用法）",
            "",
            (
                f"当前行为：memory_mode={mode}；群 @/私聊必回；"
                f"非 @ 群消息概率 {probability:.0%}；"
                f"群冷却 {group_cooldown}s / 用户冷却 {user_cooldown}s；"
                f"最短 {self._cfg_int('min_message_chars', 0)} 字；"
                f"ambient {ambient}。"
            ),
            "配置入口：AstrBot WebUI → 插件 → QQ 核心回复 → 配置。",
            (
                "概率键：非 @ 群消息 reply_probability；ambient 命中关键词用 "
                "ambient_group_keyword_probability。"
            ),
            self._persona_option_line(),
            self._identity_option_line(),
            safe_tools,
            (
                "表情装饰：本插件只设置 qqbot_core_reply_candidate / "
                "qqbot_core_reply_allow_sticker extras，由 qqbot_stickers "
                "在结果钩子中决定，不自行附加图片。"
            ),
            (
                "工具 marker：本插件不解析、不校验、不剥离、不执行 "
                "[[tool:...]]；只使用 safe_tools 提供的说明文本。"
            ),
            (
                "限制：一次最多一条回复；不展示 QQ/群号/绝对路径；"
                "一个 AstrBot conversation 独立人格/提示词/记忆。"
            ),
        ]
        if self._cfg_bool("option_include_other_plugins"):
            stars = self._loaded_star_metadata()
            max_plugins = max(1, self._cfg_int("option_max_plugins", 1, 50))
            if stars:
                lines.extend(
                    [
                        "",
                        f"已加载插件元数据（共 {len(stars)} 个，"
                        f"最多显示 {max_plugins} 个）：",
                    ]
                )
                for star in stars[:max_plugins]:
                    star_name = str(getattr(star, "name", "") or "")
                    star_display = (
                        str(getattr(star, "display_name", "") or "")
                        or star_name
                        or "未命名插件"
                    )
                    star_version = str(
                        getattr(star, "version", "") or "unknown"
                    )
                    star_desc = re.sub(
                        r"\s+",
                        " ",
                        str(getattr(star, "desc", "") or ""),
                    ).strip()
                    activated = bool(getattr(star, "activated", True))
                    suffix = "" if activated else "（未激活）"
                    line = f"- {star_display} v{star_version}{suffix}"
                    if star_desc:
                        line += f"：{star_desc[:80]}"
                    lines.append(line)
        return "\n".join(lines)

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("option", alias={"插件用法", "用法"}, priority=110)
    async def option_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = ""
        try:
            text = self._build_option_text()
            limit = self._cfg_int("option_max_chars", 1, 4000)
            if limit > 0 and len(text) > limit:
                text = text[: max(1, limit - 1)].rstrip() + "…"
        except Exception:
            logger.exception("qqbot_core_reply /option 命令处理失败")
            text = "插件用法暂时读取失败，请到 AstrBot WebUI → 插件 查看。"
        result = event.plain_result(text)
        yield result
        event.stop_event()

    # ------------------------------------------------------------------
    # Local /唱歌 /点歌 command
    # ------------------------------------------------------------------
    def _extract_local_song_keyword(self, event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        for prefix in self._command_prefixes(event):
            if prefix and text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        for command_name in ("唱歌", "点歌"):
            if text == command_name:
                return ""
            if text.startswith(command_name):
                rest = text[len(command_name) :]
                if rest and rest[0].isspace():
                    keyword = re.sub(r"\s+", " ", rest).strip()
                    return _safe_text(keyword, max_chars=100)
        return ""

    def _song_cooldown_remaining(self, event: AstrMessageEvent) -> int:
        now = self._clock()
        self._prune_reply_state(now)
        session = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        user_key = f"{session}:{sender_id}"
        group_cooldown = self._cfg_int("song_group_cooldown_seconds", 0)
        user_cooldown = self._cfg_int("song_user_cooldown_seconds", 0)
        remaining = 0.0
        last_group = self._last_song_group.get(session)
        last_user = self._last_song_user.get(user_key)
        if last_group is not None:
            remaining = max(remaining, group_cooldown - (now - last_group))
        if last_user is not None:
            remaining = max(remaining, user_cooldown - (now - last_user))
        if remaining > 0:
            return max(1, int(math.ceil(remaining)))
        self._last_song_group[session] = now
        self._last_song_user[user_key] = now
        return 0

    def _resolve_song_file(self, filename: str) -> Path | None:
        return resolve_song_file(
            getattr(self, "_song_dir", SONG_DIR),
            filename,
            suffixes=SONG_SUFFIXES,
        )

    def _list_song_files(self) -> tuple[list[Path], bool]:
        max_bytes = self._cfg_int("song_max_file_bytes", 1)
        song_files, oversized = list_song_files(
            getattr(self, "_song_dir", SONG_DIR),
            max_bytes=max_bytes,
            suffixes=SONG_SUFFIXES,
        )
        return song_files, bool(oversized)

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("唱歌", alias={"点歌"}, priority=110)
    async def local_song_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        result = None
        try:
            if not self._cfg_bool("song_enabled"):
                result = event.plain_result(
                    self._cfg_str("song_disabled_reply", "本地点歌功能当前未启用。")
                )
            else:
                keyword = self._extract_local_song_keyword(event)
                song_files, has_oversized = self._list_song_files()
                if not song_files:
                    result = event.plain_result(
                        self._cfg_str(
                            "song_too_large_reply"
                            if has_oversized
                            else "song_empty_reply"
                        )
                    )
                else:
                    if keyword:
                        folded = keyword.casefold()
                        matched = [
                            path
                            for path in song_files
                            if folded in path.name.casefold()
                        ]
                        if not matched:
                            result = event.plain_result(
                                self._cfg_str("song_not_found_reply")
                            )
                            yield result
                            event.stop_event()
                            return
                        song_files = matched
                    remaining = self._song_cooldown_remaining(event)
                    if remaining > 0:
                        template = self._cfg_str("song_cooldown_reply")
                        try:
                            cooldown_text = template.format(seconds=remaining)
                        except (KeyError, ValueError):
                            cooldown_text = template
                        result = event.plain_result(cooldown_text)
                    else:
                        chosen = self._rng.choice(song_files)
                        record_cls = getattr(Comp, "Record", None)
                        record = None
                        if record_cls is not None and callable(
                            getattr(record_cls, "fromFileSystem", None)
                        ):
                            try:
                                record = await self._await_maybe(
                                    record_cls.fromFileSystem(str(chosen))
                                )
                            except Exception:
                                record = None
                        if record is None:
                            result = event.plain_result(
                                self._cfg_str("song_error_reply")
                            )
                        else:
                            prefix_template = self._cfg_str(
                                "song_success_prefix",
                                "🎵 本地歌曲：{name}",
                            )
                            try:
                                prefix = prefix_template.format(
                                    name=chosen.stem
                                )
                            except (KeyError, ValueError):
                                prefix = prefix_template
                            result = event.chain_result(
                                [Comp.Plain(prefix), record]
                            )
        except Exception:
            logger.exception("qqbot_core_reply /唱歌 命令处理失败")
            try:
                result = event.plain_result(
                    self._cfg_str("song_error_reply", "本地点歌失败。")
                )
            except Exception:
                result = None
        if result is not None:
            yield result
            event.stop_event()
        else:
            event.stop_event()

    # ------------------------------------------------------------------
    # Memory-mode prompt hooks (never call an LLM)
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_memory_mode_context(
        self,
        event: AstrMessageEvent,
        req: Any,
    ) -> None:
        if self._memory_mode() != "astrbot":
            return
        try:
            if event.get_extra(CANDIDATE_EXTRA) is not True:
                return
        except Exception:
            return
        try:
            injected_identity = False
            if self._cfg_bool("group_identity_context_enabled"):
                injected_identity = (
                    persona_bridge_engine.inject_current_sender_identity(
                        req,
                        group_id=self._safe_group_id(event),
                        nickname=self._safe_identity_nickname(event),
                        qq=self._safe_identity_qq(event),
                        max_chars=self._cfg_int(
                            "group_identity_agent_prompt_max_chars",
                            1,
                            2000,
                        ),
                    )
                )
            event.set_extra(
                "qqbot_core_reply_identity_injected",
                bool(injected_identity),
            )
            if self._cfg_bool("ambient_group_chat_enabled"):
                if not self._is_private(event) and self._safe_group_id(event):
                    history = self._history_for(event)
                    limit = max(
                        1,
                        self._cfg_int("ambient_group_cache_limit", 1, 100),
                    )
                    history = history[-limit:]
                    injected_ambient = (
                        persona_bridge_engine.inject_ambient_group_context(
                            req,
                            history,
                            now=self._clock(),
                            max_chars=self._cfg_int(
                                "ambient_group_context_max_chars",
                                1,
                                6000,
                            ),
                        )
                    )
                    event.set_extra(
                        "qqbot_core_reply_ambient_context_injected",
                        bool(injected_ambient),
                    )
        except Exception:
            event.set_extra(
                "qqbot_core_reply_ambient_context_injected",
                False,
            )
            logger.warning(
                "qqbot_core_reply memory 身份/全群上下文注入失败，安全降级"
            )

    async def terminate(self) -> None:
        """No background tasks to stop; kept for AstrBot lifecycle parity."""

        return None
