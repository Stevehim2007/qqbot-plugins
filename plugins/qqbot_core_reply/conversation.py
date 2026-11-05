"""AstrBot ConversationManager adapter for core commands and sessions."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

try:  # package import
    from . import conversations as conversation_engine
    from . import persona_bridge as persona_bridge_engine
    from . import personas as persona_engine
    from . import command_exclusion as command_engine
except ImportError:  # test harness
    import conversations as conversation_engine
    import persona_bridge as persona_bridge_engine
    import personas as persona_engine
    import command_exclusion as command_engine


class ConversationService:
    def __init__(self, context: Any, config: Any, persona_service: Any) -> None:
        self.context = context
        self.config = config
        self.persona = persona_service
        self.switch_lock = asyncio.Lock()

    async def await_api(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def manager(self) -> Any:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return None
        if not callable(getattr(manager, "get_conversations", None)):
            return None
        if not callable(getattr(manager, "switch_conversation", None)):
            return None
        return manager

    async def list_conversations(self, umo: str) -> list[Any]:
        manager = self.manager()
        if manager is None:
            return []
        try:
            value = await self.await_api(manager.get_conversations(umo))
        except Exception:
            return []
        return list(value) if isinstance(value, (list, tuple)) else []

    async def current_id(self, umo: str) -> str:
        manager = self.manager()
        getter = getattr(manager, "get_curr_conversation_id", None) if manager else None
        if not callable(getter):
            return ""
        try:
            value = await self.await_api(getter(umo))
        except Exception:
            return ""
        return str(value or "")

    async def get_conversation(self, event: Any, cid: str) -> Any:
        manager = self.manager()
        getter = getattr(manager, "get_conversation", None) if manager else None
        if not callable(getter) or not cid:
            return None
        try:
            return await self.await_api(
                getter(str(event.unified_msg_origin), cid)
            )
        except Exception:
            return None

    def limit(self) -> int:
        return min(50, max(1, self.config.int("conversation_max_per_group", 1, 50)))

    def list_limit(self) -> int:
        return min(50, max(1, self.config.int("conversation_list_limit", 1, 50)))

    def title_max_chars(self) -> int:
        return max(1, self.config.int("conversation_title_max_chars", 1, 64))

    async def prepare_slot(self, event: Any) -> tuple[bool, str]:
        manager = self.manager()
        if manager is None:
            return False, "unsupported"
        conversations = await self.list_conversations(str(event.unified_msg_origin))
        limit = self.limit()
        if len(conversations) < limit:
            return True, "ok"
        if not self.config.bool("conversation_auto_prune_enabled", True):
            return False, "limit"
        current = await self.current_id(str(event.unified_msg_origin))
        if not current:
            return False, "no_candidate"
        delete = getattr(manager, "delete_conversation", None)
        if not callable(delete):
            return False, "unsupported_delete"
        needed = len(conversations) - limit + 1
        candidates = [
            item
            for item in conversations
            if conversation_engine.conversation_id(item) != current
        ]
        candidates.sort(key=conversation_engine.conversation_created_at)
        if len(candidates) < needed:
            return False, "no_candidate"
        for item in candidates[:needed]:
            cid = conversation_engine.conversation_id(item)
            if not cid:
                return False, "no_candidate"
            try:
                result = await self.await_api(
                    delete(str(event.unified_msg_origin), cid)
                )
                if result is False:
                    return False, "delete_failed"
            except Exception:
                return False, "delete_failed"
        return True, "pruned"

    async def delete_conversation(self, event: Any, target: Any) -> tuple[str, str]:
        manager = self.manager()
        if manager is None:
            return "unsupported", ""
        delete = getattr(manager, "delete_conversation", None)
        if not callable(delete):
            return "unsupported", ""
        target_cid = conversation_engine.conversation_id(target)
        if not target_cid:
            return "not_found", ""
        title = conversation_engine.conversation_title(target) or "（未命名会话）"
        conversations = await self.list_conversations(str(event.unified_msg_origin))
        current = await self.current_id(str(event.unified_msg_origin))
        switched = False
        if target_cid == current:
            remaining = [
                item
                for item in conversations
                if conversation_engine.conversation_id(item) != target_cid
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
                await self.await_api(
                    manager.switch_conversation(
                        str(event.unified_msg_origin), replacement
                    )
                )
            except Exception:
                return "switch_failed", title
            switched = True
            confirmed = await self.current_id(str(event.unified_msg_origin))
            if confirmed and confirmed != replacement:
                return "switch_failed", title
        try:
            result = await self.await_api(
                delete(str(event.unified_msg_origin), target_cid)
            )
            if result is False:
                raise RuntimeError("delete returned false")
        except Exception:
            if switched:
                try:
                    await self.await_api(
                        manager.switch_conversation(
                            str(event.unified_msg_origin), target_cid
                        )
                    )
                except Exception:
                    pass
            return "delete_failed", title
        return "deleted", title

    async def new_conversation(
        self,
        event: Any,
        title: str,
        prompt_profile: Any = None,
    ) -> str:
        manager = self.manager()
        if manager is None or not callable(getattr(manager, "new_conversation", None)):
            raise RuntimeError("conversation manager unsupported")
        bridge = self.persona.bridge
        persona_id = ""
        if prompt_profile is not None:
            candidate_id = persona_bridge_engine.persona_id_for_profile(prompt_profile.id)
            if not candidate_id or bridge is None:
                raise RuntimeError("prompt persona unsupported")
            if not await bridge.ensure_persona(candidate_id, prompt_profile.prompt):
                raise RuntimeError("prompt persona unavailable")
            persona_id = candidate_id
        else:
            profiles = self.persona.parse_prompt_profiles()
            pinned = profiles.default
            if pinned is not None:
                candidate_id = persona_bridge_engine.persona_id_for_profile(pinned.id)
                if candidate_id and bridge is not None:
                    if await bridge.ensure_persona(candidate_id, pinned.prompt):
                        persona_id = candidate_id
            if not persona_id:
                target = self.persona.agent_group_persona_target(event)
                if target is not None:
                    prompt, _name, _source, candidate_id = target
                    if bridge is not None and await bridge.ensure_persona(candidate_id, prompt):
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
        value = await self.await_api(
            new_conversation(str(event.unified_msg_origin), **kwargs)
        )
        cid = conversation_engine.conversation_id(value)
        if not cid:
            cid = await self.current_id(str(event.unified_msg_origin))
        if not cid:
            raise RuntimeError("new conversation returned no id")
        await self.await_api(
            manager.switch_conversation(str(event.unified_msg_origin), cid)
        )
        if persona_id:
            current = await self.get_conversation(event, cid)
            if current is not None and conversation_engine.conversation_persona_id(current) != persona_id:
                raise RuntimeError("created conversation persona mismatch")
        return cid

    async def switch(self, event: Any, cid: str) -> bool:
        manager = self.manager()
        if manager is None or not cid:
            return False
        try:
            await self.await_api(
                manager.switch_conversation(str(event.unified_msg_origin), cid)
            )
            return True
        except Exception:
            return False

    def title_from_command(self, event: Any, command_names: tuple[str, ...]) -> str | None:
        raw = self._collect_argument(event, command_names)
        return conversation_engine.normalize_title(raw, max_chars=self.title_max_chars())

    def new_arguments(self, event: Any) -> tuple[str | None, str | None]:
        raw = self._collect_argument(event, ("新会话",)).strip()
        if not raw:
            return None, None
        parts = raw.split(None, 1)
        title = conversation_engine.normalize_title(parts[0], max_chars=self.title_max_chars())
        prompt_title = parts[1].strip() if len(parts) > 1 else ""
        return title, prompt_title or None

    def _collect_argument(self, event: Any, names: tuple[str, ...]) -> str:
        prefixes = command_engine.command_prefixes(
            self.config.string_list("ignore_command_prefixes"),
            inherit_wake_prefix=self.config.bool("inherit_astrbot_wake_prefix", True),
            global_wake_prefix=self._global_wake_prefix(event),
        )
        return command_engine.collect_argument(
            str(getattr(event, "message_str", "") or ""),
            names,
            prefixes,
            extra_aliases=self.config.string_list("commands.exclude_aliases"),
        )

    def _global_wake_prefix(self, event: Any) -> Any:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return []
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            config = getter(umo=umo)
        except TypeError:
            try:
                config = getter()
            except Exception:
                return []
        except Exception:
            return []
        if isinstance(config, dict):
            return config.get("wake_prefix", [])
        try:
            return config.get("wake_prefix", [])
        except Exception:
            return getattr(config, "wake_prefix", [])

    def prompt_profile_for(self, reference: str):
        return persona_engine.find_prompt_profile(
            self.persona.parse_prompt_profiles(), reference
        )

    def default_prompt_profile(self):
        return self.persona.parse_prompt_profiles().default

    def prompt_label(self, persona_id: str) -> str:
        value = str(persona_id or "").strip()
        if not value:
            return "默认/未绑定"
        try:
            profiles = self.persona.parse_prompt_profiles()
        except Exception:
            profiles = None
        if profiles is not None:
            for profile in profiles.profiles:
                if persona_bridge_engine.persona_id_for_profile(profile.id) == value:
                    return profile.name
        if value.startswith(persona_bridge_engine.AGENT_PERSONA_PREFIX):
            return "群人格"
        return "自定义人格"

    def usage_reply(self) -> str:
        return (
            "用法：/会话列表；/会话 名称（只切换已有）；"
            "/新会话 名称 [提示词标题]；/删除会话 名称|短ID"
        )

    def unsupported_reply(self) -> str:
        return "当前 AstrBot 后端没有可用的官方会话管理 API，未执行切换。"

    # ------------------------------------------------------------------
    # Command text results (main adapter sends them)
    # ------------------------------------------------------------------
    async def run_list(self, event: Any) -> str:
        try:
            if not self.config.bool("conversation_switch_enabled", True):
                return "会话切换功能当前未启用。"
            if self._is_private(event):
                return "私聊默认使用当前会话，无需切换。"
            manager = self.manager()
            if manager is None:
                return self.unsupported_reply()
            conversations = await self.list_conversations(str(event.unified_msg_origin))
            if not conversations:
                return "当前群还没有可用会话。"
            current = await self.current_id(str(event.unified_msg_origin))
            limit = self.list_limit()
            total_limit = self.limit()
            lines = [
                f"当前群会话：{len(conversations)} / 上限 {total_limit} "
                f"（列表最多显示 {limit} 个）"
            ]
            if len(conversations) >= total_limit:
                lines.append(
                    "已达到上限；新建时会自动清理最老非当前会话。"
                    if self.config.bool("conversation_auto_prune_enabled", True)
                    else "已达到上限；请用 /删除会话 清理。"
                )
            for conversation in conversations[:limit]:
                cid = conversation_engine.conversation_id(conversation)
                title = conversation_engine.conversation_title(conversation) or "（未命名会话）"
                marker = " ← 当前" if cid and cid == current else ""
                label = self.prompt_label(
                    conversation_engine.conversation_persona_id(conversation)
                )
                lines.append(f"- {title} [id:{conversation_engine.short_id(cid)}] 提示词：{label}{marker}")
            return "\n".join(lines)
        except Exception:
            return "会话列表读取失败，请稍后再试。"

    async def run_switch(self, event: Any) -> str:
        try:
            if not self.config.bool("conversation_switch_enabled", True):
                return "会话切换功能当前未启用。"
            if self._is_private(event):
                return "私聊默认使用当前会话，无需切换。"
            title = self.title_from_command(event, ("会话", "切换会话"))
            if title is None:
                return self.usage_reply()
            if self.manager() is None:
                return self.unsupported_reply()
            async with self.switch_lock:
                conversations = await self.list_conversations(str(event.unified_msg_origin))
                matches = conversation_engine.find_all_by_title(conversations, title)
                if len(matches) > 1:
                    ids = "、".join(
                        conversation_engine.short_id(
                            conversation_engine.conversation_id(item)
                        )
                        for item in matches[:5]
                    )
                    return (
                        f"找到多个同名会话「{title}」（id:{ids}），"
                        "请先在 AstrBot 会话列表改名后再切换；本次未自动取第一个。"
                    )
                if not matches:
                    return (
                        f"没有找到会话「{title}」，未执行切换；"
                        f"请用 /新会话 {title} 创建。"
                    )
                cid = conversation_engine.conversation_id(matches[0])
                if not await self.switch(event, cid):
                    return self.unsupported_reply()
                return f"已切换到会话「{title}」。"
        except Exception:
            return "会话切换失败，当前会话保持不变。"

    async def run_new(self, event: Any) -> str:
        try:
            if not self.config.bool("conversation_switch_enabled", True):
                return "会话切换功能当前未启用。"
            if self._is_private(event):
                return "私聊默认使用当前会话，无需切换。"
            title, prompt_title = self.new_arguments(event)
            if title is None:
                return self.usage_reply()
            prompt_profile = (
                self.prompt_profile_for(prompt_title)
                if prompt_title
                else self.default_prompt_profile()
            )
            if prompt_title and prompt_profile is None:
                return f"未找到提示词模板「{prompt_title}」，未创建会话。"
            if self.manager() is None:
                return self.unsupported_reply()
            async with self.switch_lock:
                conversations = await self.list_conversations(str(event.unified_msg_origin))
                if conversation_engine.find_all_by_title(conversations, title):
                    return f"同名会话「{title}」已存在，可用 /会话 {title} 切换。"
                prepared, reason = await self.prepare_slot(event)
                if not prepared:
                    if reason == "limit":
                        return "已达到当前会话上限，自动清理未启用；请先 /删除会话 或调高上限。"
                    if reason in ("delete_failed", "unsupported_delete"):
                        return "会话数量已达上限，自动删除最老会话失败；已中止创建，现有会话保持不变。"
                    return "会话数量已达上限，且没有可安全删除的非当前会话；已中止创建。"
                await self.new_conversation(event, title, prompt_profile)
                suffix = f"\n提示词：{prompt_profile.name}" if prompt_profile is not None else ""
                return f"已新建并切换到会话「{title}」。{suffix}"
        except Exception:
            return "新会话创建失败，当前会话保持不变。"

    async def run_delete(self, event: Any) -> str:
        try:
            if not self.config.bool("conversation_switch_enabled", True):
                return "会话切换功能当前未启用。"
            if self._is_private(event):
                return "私聊默认使用当前会话，无需切换。"
            raw = self._collect_argument(event, ("删除会话", "会话删除")).strip()
            if not raw:
                return "用法：/删除会话 名称|短ID"
            manager = self.manager()
            if manager is None or not callable(getattr(manager, "delete_conversation", None)):
                return "当前 AstrBot 后端没有可用的官方会话删除 API，未删除任何会话。"
            async with self.switch_lock:
                conversations = await self.list_conversations(str(event.unified_msg_origin))
                title_matches = conversation_engine.find_all_by_title(conversations, raw)
                if len(title_matches) > 1:
                    ids = "、".join(
                        conversation_engine.short_id(
                            conversation_engine.conversation_id(item)
                        )
                        for item in title_matches[:5]
                    )
                    return (
                        f"找到多个同名会话「{raw}」（id:{ids}），"
                        "请使用唯一短 ID 删除，未删除任何会话。"
                    )
                matches = title_matches
                if not matches:
                    wanted = raw.casefold()
                    matches = [
                        item
                        for item in conversations
                        if (
                            conversation_engine.short_id(
                                conversation_engine.conversation_id(item)
                            ).casefold()
                            == wanted
                            or conversation_engine.conversation_id(item).casefold()
                            == wanted
                        )
                    ]
                if not matches:
                    return f"没有找到会话「{raw}」，未删除任何会话。"
                if len(matches) > 1:
                    return "短 ID 匹配到多个会话，请使用更完整的 ID；未删除任何会话。"
                status, title = await self.delete_conversation(event, matches[0])
                if status == "last":
                    return "不能删除最后一个会话；当前会话保持不变。"
                if status == "switch_failed":
                    return "删除当前会话前切换失败，未删除任何会话。"
                if status in ("unsupported", "not_found"):
                    return "当前 AstrBot 后端没有可用的官方会话删除 API，未删除任何会话。"
                if status == "delete_failed":
                    return "删除会话失败，未伪造删除成功；请稍后再试。"
                remaining = await self.list_conversations(str(event.unified_msg_origin))
                return f"已删除会话「{title}」；当前剩余 {len(remaining)} 个会话。"
        except Exception:
            return "删除会话失败，未删除任何会话。"

    @staticmethod
    def _is_private(event: Any) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return False
