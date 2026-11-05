"""AstrBot registration/adapter entrypoint for qqbot_core_reply.

All business logic lives in the pure modules (participation, command_exclusion,
cooldown, conversation, persona, model_gateway, reply_pipeline, state,
contracts, commands).  This file only wires decorators, Context services and
AstrBot result objects.
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # Loaded as a package by AstrBot.
    from . import command_exclusion as command_engine
    from . import contracts
    from .commands import CommandReply, CommandService
    from .config import RuntimeConfig
    from .conversation import ConversationService
    from .cooldown import CooldownGate
    from .model_gateway import ModelGateway
    from .participation import ParticipationPolicy
    from .persona import PersonaService
    from .reply_pipeline import (
        AMBIENT_EXTRA,
        ALLOW_STICKER_EXTRA,
        CANDIDATE_EXTRA,
        TEXT_EXTRA,
        CoreReplyService,
        ReplyPipeline,
    )
    from .state import RuntimeState
except ImportError:  # Loaded directly by a test harness.
    import sys as _sys

    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    import command_exclusion as command_engine
    import contracts
    from commands import CommandReply, CommandService
    from config import RuntimeConfig
    from conversation import ConversationService
    from cooldown import CooldownGate
    from model_gateway import ModelGateway
    from participation import ParticipationPolicy
    from persona import PersonaService
    from reply_pipeline import (
        AMBIENT_EXTRA,
        ALLOW_STICKER_EXTRA,
        CANDIDATE_EXTRA,
        TEXT_EXTRA,
        CoreReplyService,
        ReplyPipeline,
    )
    from state import RuntimeState

PLUGIN_NAME = "qqbot_core_reply"
PLUGIN_DISPLAY_NAME = "QQ 核心回复"
PLUGIN_VERSION = "4.0.0"
PLUGIN_DIR = Path(__file__).resolve().parent
SONG_DIR = PLUGIN_DIR / "songs"
PROVIDER_ID = "core_reply"


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


class CoreReplyPlugin(Star):
    """Thin AstrBot adapter around the single reply controller."""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.config_obj = config if config is not None else {}
        self.config = RuntimeConfig(self.config_obj)
        self.state = RuntimeState(
            self.config,
            rng=random.Random(),
            clock=time.monotonic,
        )
        self._song_dir = SONG_DIR
        self.owner_token = f"{PLUGIN_NAME}:{id(self)}"

        self.participation = ParticipationPolicy(self.config, self.state)
        self.cooldown = CooldownGate(self.state)
        self.persona = PersonaService(context, self.config, self.state, logger)
        self.gateway = ModelGateway(context, self.config, self.state, self.persona)
        self.pipeline = ReplyPipeline(
            context,
            self.config,
            self.state,
            self.participation,
            self.cooldown,
            self.persona,
            self.gateway,
            self.owner_token,
        )
        self.conversation = ConversationService(context, self.config, self.persona)
        self.commands = CommandService(
            context,
            self.config,
            self.state,
            self.persona,
            self.conversation,
            self.gateway,
            song_dir_getter=lambda: self._song_dir,
            set_ready=self._set_ready,
        )
        self.service = CoreReplyService(
            self.pipeline,
            registry=None,
            owner_token=self.owner_token,
            provider_id=PROVIDER_ID,
        )
        self.registry = contracts.ensure_registry(context)
        self.service.registry = self.registry
        methods = {
            name: getattr(self.service, name)
            for name in CoreReplyService.METHOD_NAMES
        }
        enabled = self.config.bool("enabled", False)
        self.state.set_ready(enabled)
        self.descriptor = self.registry.register_provider(
            PROVIDER_ID,
            plugin_id=PLUGIN_NAME,
            capability="reply",
            abi_version=contracts.ABI_VERSION,
            owner=self.owner_token,
            methods=methods,
            owner_epoch=self.state.owner_epoch,
            ready=enabled,
            service=self.service,
        )
        self._publish_readiness(enabled)

    # ------------------------------------------------------------------
    # Compatibility properties / tiny adapter helpers
    # ------------------------------------------------------------------
    @property
    def _clock(self):
        return self.state.clock

    @_clock.setter
    def _clock(self, value):
        self.state.clock = value

    @property
    def _rng(self):
        return self.state.rng

    @_rng.setter
    def _rng(self, value):
        self.state.rng = value

    def _cfg_str(self, key: str, default: str = "") -> str:
        return self.config.string(key, default=default)

    def _memory_mode(self) -> str:
        return self.persona.memory_mode()

    def _mark_candidate(
        self,
        event: AstrMessageEvent,
        original_text: str,
        *,
        ambient: bool,
    ) -> None:
        self.pipeline.mark_candidate(event, original_text, ambient=ambient)

    def _remember_history(
        self,
        event: AstrMessageEvent,
        message_text: str,
        *,
        has_image: bool,
        has_face: bool,
    ) -> None:
        self.pipeline.remember_history(
            event,
            message_text,
            has_image=has_image,
            has_face=has_face,
        )

    def _build_option_text(self) -> str:
        return self.commands.option_text()

    def _finalize_reply(self, text: Any) -> str:
        return self.gateway.finalize_text(text)

    def _build_prompt(self, event: AstrMessageEvent, message_text: str, **kwargs: Any) -> str:
        return self.persona.build_prompt(event, message_text, **kwargs)

    def _resolve_event_persona(self, event: AstrMessageEvent):
        return self.persona.resolve_event_persona(event)

    def _group_random_eligible(self, message_text: str, components: list[Any]) -> bool:
        return self.participation.group_random_eligible(
            message_text,
            has_image=any(
                self.pipeline.is_component(item, "Image") for item in components
            ),
            has_face=any(
                self.pipeline.is_component(item, "Face") for item in components
            ),
        )

    def _ambient_group_should_participate(
        self,
        event: AstrMessageEvent,
        message_text: str,
        components: list[Any],
    ) -> bool:
        return self.participation.ambient_should_participate(
            event,
            message_text,
            has_image=any(
                self.pipeline.is_component(item, "Image") for item in components
            ),
            has_face=any(
                self.pipeline.is_component(item, "Face") for item in components
            ),
            session_key=str(getattr(event, "unified_msg_origin", "") or ""),
        )

    _ambient_should_participate = _ambient_group_should_participate

    def _command_prefixes(self, event: AstrMessageEvent) -> list[str]:
        return self.pipeline.command_prefixes(event)

    def _is_command_message(self, event: AstrMessageEvent, text: str) -> bool:
        return self.pipeline.command_match(event, text) is not None

    def _safe_tools_available(self) -> bool:
        return self.gateway.safe_tools_available()

    def _get_current_provider_id(self, event: AstrMessageEvent):
        return self.gateway.current_provider_id(event)

    def _llm_generate(self, **kwargs: Any):
        return self.gateway.llm_generate(**kwargs)

    def _collect_image_refs(self, event: AstrMessageEvent):
        return self.gateway.collect_image_refs(event)

    def _provider_image_capability(self, provider: Any):
        return self.gateway.provider_image_capability(provider)

    def _set_ready(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.state.set_ready(enabled)
        try:
            self.registry.set_ready(
                PROVIDER_ID,
                enabled,
                owner=self.owner_token,
                owner_epoch=self.state.owner_epoch,
            )
        except Exception:
            pass
        self._publish_readiness(enabled)

    def _publish_readiness(self, enabled: bool) -> None:
        for name, value in (
            ("qqbot_core_reply_ready", bool(enabled)),
            ("qqbot_core_reply_owner_epoch", self.state.owner_epoch),
            ("qqbot_reply_owner", PROVIDER_ID),
        ):
            try:
                setattr(self.context, name, value)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Main event handler (single reply path)
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=100)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent):
        outcome = await self.pipeline.handle_message(event)
        if outcome is None:
            return
        for result in outcome.results:
            yield result
        if outcome.turn_id and not outcome.memory_mode:
            self.pipeline.record_delivery(outcome.turn_id, "delivered")
        if outcome.stop:
            event.stop_event()

    @filter.on_llm_request()
    async def inject_memory_mode_context(
        self,
        event: AstrMessageEvent,
        req: Any,
    ) -> None:
        await self.pipeline.handle_memory_context(event, req)

    @filter.on_decorating_result()
    async def decorate_memory_result(self, event: AstrMessageEvent) -> None:
        await self.pipeline.handle_decorating_result(event)

    # ------------------------------------------------------------------
    # Canonical commands
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("回复", alias={"reply"}, priority=110)
    async def reply_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = await self.commands.reply_control(event)
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("冷却", alias={"cooldown"}, priority=110)
    async def cooldown_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = await self.commands.cooldown_status(event)
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("会话列表", priority=110)
    async def conversation_list_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = await self.conversation.run_list(event)
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("会话", alias={"切换会话"}, priority=110)
    async def conversation_switch_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = await self.conversation.run_switch(event)
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("新会话", priority=110)
    async def conversation_new_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = await self.conversation.run_new(event)
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("删除会话", alias={"会话删除"}, priority=110)
    async def conversation_delete_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = await self.conversation.run_delete(event)
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("option", alias={"插件用法", "用法"}, priority=110)
    async def option_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        text = ""
        try:
            text = self.commands.option_text()
            limit = self.config.int("option_max_chars", 1, 4000)
            if limit > 0 and len(text) > limit:
                text = text[: max(1, limit - 1)].rstrip() + "…"
        except Exception:
            logger.exception("qqbot_core_reply /option 命令处理失败")
            text = "插件用法暂时读取失败，请到 AstrBot WebUI → 插件 查看。"
        yield event.plain_result(text)
        event.stop_event()

    @filter.platform_adapter_type(_PLATFORM_TYPES, priority=110)
    @filter.command("唱歌", alias={"点歌"}, priority=110)
    async def local_song_command(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        reply = await self.commands.song_reply(event)
        if reply.kind == "chain":
            yield event.chain_result(reply.chain)
        else:
            yield event.plain_result(reply.text)
        event.stop_event()

    # ------------------------------------------------------------------
    # Fixed ABI v1 provider methods (delegate to CoreReplyService)
    # ------------------------------------------------------------------
    async def reply_status(self, request_context: Any, args: Any = None):
        return await self.service.reply_status(request_context, args)

    async def reply_configure_plan(self, request_context: Any, args: Any = None):
        return await self.service.reply_configure_plan(request_context, args)

    async def reply_configure_commit(self, request_context: Any, args: Any = None):
        return await self.service.reply_configure_commit(request_context, args)

    async def reply_turn_run(self, request_context: Any, args: Any = None):
        return await self.service.reply_turn_run(request_context, args)

    async def reply_cancel_turn(self, request_context: Any, args: Any = None):
        return await self.service.reply_cancel_turn(request_context, args)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        await self.pipeline.terminate()
        self.state.set_ready(False)
        try:
            self.registry.unregister(PROVIDER_ID, owner=self.owner_token)
        except Exception:
            pass
        self._publish_readiness(False)
