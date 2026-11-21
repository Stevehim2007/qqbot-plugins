"""Knowledge memory bank: model-callable add/search, no default prompt injection."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from . import store as bank_store
except ImportError:  # AstrBot loads plugin modules as top-level
    import store as bank_store  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_memory_bank"
PLUGIN_VERSION = "0.1.0"
DEFAULTS = {
    "enabled": True,
    "auto_extract_enabled": False,
    "aux_enabled": True,
    "extract_every_turns": 4,
    "max_facts_per_extract": 5,
    "max_entries": 2000,
    "search_limit": 5,
    "allow_forget": True,
}


def _platform(event: Any) -> str:
    text = str(getattr(event, "unified_msg_origin", "") or "")
    if "qqbot-onebot" in text or "aiocqhttp" in text:
        return "onebot"
    if "qqbot-official" in text or "qq_official" in text:
        return "qq_official"
    return "default"


def event_scope(event: Any) -> str:
    try:
        group = str(event.get_group_id() or "").strip()
    except Exception:
        group = ""
    try:
        user = str(event.get_sender_id() or "").strip()
    except Exception:
        user = ""
    platform = _platform(event)
    if group:
        return f"group:{platform}:{group}"[:200]
    return f"user:{platform}:{user or 'unknown'}"[:200]


def event_persona(event: Any) -> str:
    try:
        return str(event.get_extra("qqbot_core_reply_persona_id", "") or "")[:128]
    except Exception:
        return ""


def _provider_id(value: Any) -> str:
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
        found = meta.get("id") if isinstance(meta, dict) else getattr(meta, "id", None)
        if found:
            return str(found)
    for attr in ("id", "provider_id"):
        found = getattr(value, attr, None)
        if found:
            return str(found)
    return ""


def parse_facts(raw: str, *, limit: int = 5) -> list[str]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("remember")
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        content = item.get("content") if isinstance(item, dict) else item
        fact = bank_store.normalise_fact_subject(str(content or ""))
        if not fact or fact.endswith(("?", "？")):
            continue
        if fact not in out:
            out.append(fact)
        if len(out) >= max(1, int(limit)):
            break
    return out


@register(PLUGIN_NAME, "QQ Bot Studio", "可检索的知识记忆库；模型按需 search/add", PLUGIN_VERSION)
class MemoryBankPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config_obj = config if isinstance(config, dict) else {}
        self._store: bank_store.KnowledgeStore | None = None
        self._store_path: Path | None = None
        self._turns: dict[str, list[str]] = {}

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self.config_obj.get(key, DEFAULTS.get(key, default))
        return DEFAULTS.get(key, default) if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path("data/plugin_data") / PLUGIN_NAME

    def _get_store(self) -> bank_store.KnowledgeStore:
        path = self._data_dir() / "knowledge.db"
        if self._store is None or self._store_path != path:
            self._store = bank_store.KnowledgeStore(path)
            self._store_path = path
        return self._store

    async def initialize(self) -> None:
        if self._enabled():
            try:
                self._get_store()
            except Exception:
                logger.exception("qqbot_memory_bank 初始化失败")

    async def terminate(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    # ------------------------------------------------------------------
    # Model-callable tools: the knowledge stays out of system prompt until
    # the model explicitly asks for it.
    # ------------------------------------------------------------------
    @filter.llm_tool(name="memory_add")
    async def memory_add(self, event: AstrMessageEvent, content: str, tags: str = "") -> str:
        """记住一条用户明确说出的长期知识或偏好。

        Args:
            content(string): 要记住的简洁事实，例如“用户喜欢喝无糖可乐”。
            tags(string): 可选标签，逗号分隔，例如“饮食,偏好”。
        """
        if not self._enabled():
            return "知识记忆库未启用。"
        result = self._get_store().add(
            scope=event_scope(event),
            persona_id=event_persona(event),
            content=str(content or ""),
            tags=str(tags or ""),
            source="model",
            max_entries=int(self._cfg("max_entries", 2000)),
        )
        if not result.get("ok"):
            return "没有保存：内容为空、噪声过多或记忆库已满。"
        return "已记住。" if not result.get("duplicate") else "这条记忆已经存在。"

    @filter.llm_tool(name="memory_search")
    async def memory_search(self, event: AstrMessageEvent, query: str, limit: str = "5") -> str:
        """按需检索知识记忆库；只在需要回忆用户事实时调用。

        Args:
            query(string): 要检索的关键词或问题，例如“用户喜欢喝什么”。
            limit(string): 最多返回几条，默认 "5"。
        """
        if not self._enabled():
            return "知识记忆库未启用。"
        try:
            requested = int(str(limit or "5"))
        except (TypeError, ValueError):
            requested = 5
        requested = max(1, min(10, requested))
        rows = self._get_store().search(
            scope=event_scope(event),
            persona_id=event_persona(event),
            query=str(query or ""),
            limit=requested,
        )
        if not rows:
            return "没有找到相关记忆。"
        lines = ["[知识记忆]"]
        for index, row in enumerate(rows, 1):
            tag_text = f"（{row['tags']}）" if row.get("tags") else ""
            lines.append(f"{index}. [{row['id']}] {row['content']}{tag_text}")
        return "\n".join(lines)

    @filter.llm_tool(name="memory_forget")
    async def memory_forget(self, event: AstrMessageEvent, entry_id: str) -> str:
        """删除一条错误或过期的知识记忆。

        Args:
            entry_id(string): memory_search 结果中的记忆 id。
        """
        if not self._enabled() or not bool(self._cfg("allow_forget", True)):
            return "不允许删除记忆。"
        ok = self._get_store().forget(
            scope=event_scope(event),
            persona_id=event_persona(event),
            entry_id=str(entry_id or ""),
        )
        return "已删除。" if ok else "没有找到可删除的记忆。"

    # ------------------------------------------------------------------
    # Optional automatic extraction.  Auxiliary LLM first, deterministic
    # rule fallback and validation second.
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=118)
    async def observe_message(self, event: AstrMessageEvent):
        """Buffer incoming user messages; extraction is independent of whether
        the main bot chose to reply."""
        if not self._enabled() or not bool(self._cfg("auto_extract_enabled", False)):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith(("/", "[[", "【")):
            return
        try:
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return
        except Exception:
            pass
        scope = event_scope(event)
        buffer = self._turns.setdefault(scope, [])
        buffer.append(text[:500])
        del buffer[:-10]
        every = max(2, min(20, int(self._cfg("extract_every_turns", 4))))
        if len(buffer) < every:
            return
        messages = list(buffer)
        self._turns[scope] = []
        asyncio.create_task(
            self._extract_and_store(
                scope,
                event_persona(event),
                str(getattr(event, "unified_msg_origin", "") or ""),
                messages,
            )
        )

    async def _extract_and_store(
        self,
        scope: str,
        persona_id: str,
        umo: str,
        messages: list[str],
    ) -> None:
        try:
            facts: list[str] = []
            used_aux = bool(self._cfg("aux_enabled", True))
            if used_aux:
                facts = await self._aux_facts(umo, messages)
            if not facts:
                facts = bank_store.heuristic_facts("\n".join(messages))
                used_aux = False
            limit = max(1, min(10, int(self._cfg("max_facts_per_extract", 5))))
            for fact in facts[:limit]:
                self._get_store().add(
                    scope=scope,
                    persona_id=persona_id,
                    content=fact,
                    source="aux" if used_aux else "rule",
                    confidence=0.8,
                    max_entries=int(self._cfg("max_entries", 2000)),
                )
        except Exception:
            logger.debug("qqbot_memory_bank extraction failed", exc_info=True)

    async def _aux_facts(self, umo: str, messages: list[str]) -> list[str]:
        try:
            getter = getattr(self.context, "get_using_provider", None)
            if not callable(getter):
                return []
            try:
                provider = getter(umo=str(umo or ""))
            except TypeError:
                provider = getter()
            if inspect.isawaitable(provider):
                provider = await provider
            provider_id = _provider_id(provider)
            generator = getattr(self.context, "llm_generate", None)
            if not provider_id or not callable(generator):
                return []
            system = (
                "你是记忆提取器。只提取用户明确说出的、以后仍然有用的长期事实："
                "身份、偏好、厌恶、住址/工作/上学、习惯、计划、约定、生日、过敏等。"
                "不要记录寒暄、问题、助手说的话、临时情绪、图片链接或任何猜测。"
                "只输出 JSON：{\"remember\":[{\"content\":\"...\",\"confidence\":0.0}]}。"
                "没有可记的就输出 {\"remember\":[]}。每条不超过 100 字。"
            )
            prompt = "用户消息：\n" + "\n".join(f"- {m}" for m in messages)
            pending = generator(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system,
                max_tokens=220,
                temperature=0.1,
            )
            response = await asyncio.wait_for(pending, timeout=10) if inspect.isawaitable(pending) else pending
            raw = (
                getattr(response, "completion_text", None)
                or getattr(response, "text", None)
                or getattr(response, "_completion_text", None)
                or response
            )
            return parse_facts(str(raw or ""), limit=int(self._cfg("max_facts_per_extract", 5)))
        except Exception:
            logger.debug("qqbot_memory_bank auxiliary extraction failed", exc_info=True)
            return []
