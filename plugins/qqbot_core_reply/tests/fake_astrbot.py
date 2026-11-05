"""Minimal fake AstrBot package used only by the unit tests.

The fake exposes the public surface this plugin is allowed to use so tests run
without installing AstrBot or any third-party dependency.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any


def install() -> None:
    if getattr(sys.modules.get("astrbot"), "_qqbot_fake", False):
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # mark as package
    astrbot._qqbot_fake = True
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    comp_mod = types.ModuleType("astrbot.api.message_components")

    class PlatformAdapterType:
        AIOCQHTTP = 1
        QQOFFICIAL = 2
        QQOFFICIAL_WEBHOOK = 4

    class EventMessageType:
        ALL = 1

    class _Filter:
        @staticmethod
        def platform_adapter_type(*args: Any, **kwargs: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def event_message_type(*args: Any, **kwargs: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def command(*args: Any, **kwargs: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def on_llm_request(*args: Any, **kwargs: Any):
            def decorator(func):
                return func

            return decorator

        @staticmethod
        def on_decorating_result(*args: Any, **kwargs: Any):
            def decorator(func):
                return func

            return decorator

    class AstrMessageEvent:
        pass

    _Filter.PlatformAdapterType = PlatformAdapterType
    _Filter.EventMessageType = EventMessageType
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    event_mod.PlatformAdapterType = PlatformAdapterType
    event_mod.EventMessageType = EventMessageType

    class Star:
        def __init__(self, context: Any = None) -> None:
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(plugin_name: str | None = None):
            import tempfile
            from pathlib import Path

            return Path(tempfile.gettempdir()) / "qqbot-core-reply-tests" / str(
                plugin_name or "qqbot_core_reply"
            )

    class Context:
        pass

    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.Context = Context

    class Plain:
        def __init__(self, text: str = "") -> None:
            self.text = str(text)

        def __repr__(self) -> str:
            return f"Plain({self.text!r})"

    class At:
        def __init__(self, qq: str = "") -> None:
            self.qq = str(qq)

    class AtAll(At):
        def __init__(self) -> None:
            super().__init__("all")

    class Image:
        def __init__(
            self,
            url: str | None = None,
            file: str | None = None,
            path: str | None = None,
            base64_data: str | None = None,
        ) -> None:
            self.url = url
            self.file = file
            self.path = path
            self._base64_data = base64_data

        async def convert_to_base64(self) -> str:
            return self._base64_data or ""

    class Face:
        pass

    class Record:
        def __init__(self, path: str = "") -> None:
            self.path = path

        @classmethod
        def fromFileSystem(cls, path: str):
            return cls(path)

    comp_mod.Plain = Plain
    comp_mod.At = At
    comp_mod.AtAll = AtAll
    comp_mod.Image = Image
    comp_mod.Face = Face
    comp_mod.Record = Record

    api.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    api.event = event_mod
    api.star = star_mod
    api.message_components = comp_mod
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = comp_mod


@dataclass
class FakeResult:
    kind: str
    text: str = ""
    chain: list[Any] = field(default_factory=list)


class FakeEvent:
    def __init__(
        self,
        text: str = "",
        *,
        private: bool = False,
        group_id: str = "123456789",
        self_id: str = "999000",
        sender_id: str = "1001",
        sender_name: str = "Alice",
        components: list[Any] | None = None,
        at_bot: bool = False,
        wake: bool = False,
        platform_id: str = "aiocqhttp",
    ) -> None:
        from astrbot.api import message_components as Comp

        self.message_str = str(text)
        self._private = bool(private)
        self._group_id = str(group_id)
        self._self_id = str(self_id)
        self._sender_id = str(sender_id)
        self._sender_name = str(sender_name)
        self._platform_id = platform_id
        self.is_at_or_wake_command = bool(wake)
        if components is None:
            components = []
            if at_bot:
                components.append(Comp.At(self._self_id))
            if text:
                components.append(Comp.Plain(text))
        self._components = list(components)
        suffix = "FriendMessage" if private else "GroupMessage"
        origin_id = self._sender_id if private else self._group_id
        self.unified_msg_origin = f"aiocqhttp:{suffix}:{origin_id}"
        self.extras: dict[str, Any] = {}
        self.stopped = False
        self.llm_blocked = False
        self.llm_requested = False
        self._result: Any = None

    def get_messages(self) -> list[Any]:
        return list(self._components)

    def is_private_chat(self) -> bool:
        return self._private

    def get_group_id(self) -> str:
        return "" if self._private else self._group_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_platform_id(self) -> str:
        return self._platform_id

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def should_call_llm(self, value: bool = True) -> None:
        self.llm_blocked = bool(value)

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> FakeResult:
        self._result = FakeResult("plain", text=str(text))
        return self._result

    def get_result(self):
        return self._result

    def set_result(self, result: Any) -> None:
        self._result = result

    def chain_result(self, chain: list[Any]) -> FakeResult:
        return FakeResult("chain", chain=list(chain))


@dataclass
class FakeProvider:
    provider_id: str = "prov-1"
    modalities: list[str] | None = None

    def meta(self):
        return types.SimpleNamespace(id=self.provider_id)

    @property
    def provider_config(self) -> dict[str, Any]:
        if self.modalities is None:
            return {}
        return {"modalities": list(self.modalities)}


class FakeStarMeta:
    def __init__(self, star_cls: Any, activated: bool = True) -> None:
        self.star_cls = star_cls
        self.activated = activated


class FakeContext:
    def __init__(
        self,
        *,
        provider_id: str = "prov-1",
        provider: Any = None,
        providers: dict[str, Any] | None = None,
        llm_responses: list[Any] | None = None,
        llm_handler: Any = None,
        global_config: dict[str, Any] | None = None,
        stars: list[Any] | None = None,
        registered_stars: dict[str, Any] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider = provider or FakeProvider(provider_id or "prov-1")
        self.providers = dict(providers or {})
        if provider_id:
            self.providers.setdefault(provider_id, self.provider)
        self.llm_responses = list(llm_responses or [])
        self.llm_handler = llm_handler
        self.llm_calls: list[dict[str, Any]] = []
        self.global_config = dict(global_config or {})
        self.stars = list(stars or [])
        self.registered_stars = dict(registered_stars or {})
        self.persona_manager = None
        self.conversation_manager = None

    async def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        return self.provider_id

    def get_using_provider(self, umo: str | None = None):
        return self.provider if self.provider_id else None

    def get_provider_by_id(self, provider_id: str):
        return self.providers.get(provider_id)

    async def llm_generate(self, **kwargs: Any) -> Any:
        self.llm_calls.append(dict(kwargs))
        if self.llm_handler is not None:
            value = self.llm_handler(kwargs)
            if hasattr(value, "__await__"):
                return await value
            return value
        if self.llm_responses:
            item = self.llm_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                value = item(kwargs)
                if hasattr(value, "__await__"):
                    return await value
                return value
            return item
        return "默认回复"

    def get_config(self, umo: str | None = None) -> dict[str, Any]:
        return self.global_config

    def get_registered_star(self, name: str):
        return self.registered_stars.get(name)

    def get_all_stars(self) -> list[Any]:
        return list(self.stars)


class FakePersona:
    def __init__(self, persona_id: str, system_prompt: str = "") -> None:
        self.persona_id = persona_id
        self.system_prompt = system_prompt


class FakePersonaManager:
    def __init__(self) -> None:
        self.personas: dict[str, FakePersona] = {}

    async def get_persona(self, persona_id: str):
        return self.personas.get(persona_id)

    async def create_persona(self, persona_id: str, prompt: str = "", **kwargs: Any):
        if persona_id in self.personas:
            raise ValueError("already exists")
        persona = FakePersona(persona_id, prompt)
        self.personas[persona_id] = persona
        return persona

    async def update_persona(self, persona_id: str, system_prompt: str = "", **kwargs: Any):
        persona = self.personas.get(persona_id)
        if persona is None:
            persona = FakePersona(persona_id, system_prompt)
            self.personas[persona_id] = persona
        persona.system_prompt = system_prompt
        return persona


@dataclass
class FakeConversation:
    cid: str
    title: str = ""
    persona_id: str = ""
    created_at: float = 0.0


class FakeConversationManager:
    def __init__(self, conversations: list[FakeConversation] | None = None) -> None:
        self.conversations = list(conversations or [])
        self.current: dict[str, str] = {}
        self.new_calls: list[dict[str, Any]] = []
        self.switch_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    async def get_conversations(self, umo: str) -> list[FakeConversation]:
        return list(self.conversations)

    async def get_curr_conversation_id(self, umo: str) -> str:
        return self.current.get(umo, "")

    async def get_conversation(self, umo: str, cid: str):
        return next((item for item in self.conversations if item.cid == cid), None)

    async def new_conversation(
        self,
        umo: str,
        title: str | None = None,
        persona_id: str | None = None,
        platform_id: str | None = None,
    ) -> FakeConversation:
        self.new_calls.append(
            {
                "umo": umo,
                "title": title,
                "persona_id": persona_id,
                "platform_id": platform_id,
            }
        )
        cid = f"cid-{len(self.conversations) + 1}"
        conversation = FakeConversation(
            cid,
            title=title or "",
            persona_id=persona_id or "",
            created_at=float(len(self.conversations) + 1),
        )
        self.conversations.append(conversation)
        self.current[umo] = cid
        return conversation

    async def switch_conversation(self, umo: str, cid: str) -> bool:
        self.switch_calls.append((umo, cid))
        if not any(item.cid == cid for item in self.conversations):
            raise ValueError("missing conversation")
        self.current[umo] = cid
        return True

    async def delete_conversation(self, umo: str, cid: str) -> bool:
        self.delete_calls.append((umo, cid))
        before = len(self.conversations)
        self.conversations = [item for item in self.conversations if item.cid != cid]
        if self.current.get(umo) == cid:
            self.current.pop(umo, None)
        return len(self.conversations) != before

    async def update_conversation(self, umo: str, cid: str, **kwargs: Any) -> bool:
        conversation = next((item for item in self.conversations if item.cid == cid), None)
        if conversation is None:
            return False
        for key, value in kwargs.items():
            setattr(conversation, key, value)
        return True


async def collect(async_generator) -> list[Any]:
    output: list[Any] = []
    async for item in async_generator:
        output.append(item)
    return output


def plain_texts(results: list[Any]) -> list[str]:
    return [item.text for item in results if getattr(item, "kind", "") == "plain"]


install()
