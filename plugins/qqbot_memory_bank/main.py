"""Knowledge memory bank: model-callable add/search, no default prompt injection."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from . import aux_ai
    from . import memory_router as router
    from . import store as bank_store
except ImportError:  # AstrBot loads plugin modules as top-level
    import aux_ai  # type: ignore[no-redef]
    import memory_router as router  # type: ignore[no-redef]
    import store as bank_store  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_memory_bank"
PLUGIN_VERSION = "0.6.0"
DEFAULTS = {
    "enabled": True,
    "auto_extract_enabled": False,
    "aux_enabled": True,
    "extract_every_turns": 4,
    "max_facts_per_extract": 5,
    "max_entries": 2000,
    "search_limit": 5,
    "knowledge_search_limit": 3,
    "allow_forget": True,
    "auto_recall_enabled": True,
    "auto_recall_top_k": 3,
    "auto_recall_max_chars": 1200,
    "auto_recall_personal_enabled": True,
    "memory_layer_max_items": 6,
    "memory_layer_max_chars": 600,
    "routing_mode": "ai",
    "routing_min_confidence": 0.6,
    "auto_write_core_memory": False,
    "auto_recall_cards_enabled": True,
    "auto_recall_cards_limit": 3,
    "auto_recall_cards_max_chars": 420,
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


def event_actor(event: Any) -> tuple[str, str]:
    scope = event_scope(event)
    try:
        sender = str(event.get_sender_id() or "").strip()
    except Exception:
        sender = ""
    try:
        label = str(event.get_sender_name() or "").strip()
    except Exception:
        label = ""
    if not sender:
        return "", label[:80]
    digest = hashlib.sha256(f"{scope}\n{sender}".encode("utf-8")).hexdigest()[:16]
    return digest, label[:80]


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


@register(PLUGIN_NAME, "QQ Bot Studio", "用户导入知识库（只读检索）+ AI 记忆（按用户隔离）；模型按需调用工具", PLUGIN_VERSION)
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
    @filter.llm_tool(name="knowledge_search")
    async def knowledge_search(self, event: AstrMessageEvent, query: str, limit: str = "3") -> str:
        """检索用户导入的知识库资料；资料只读，模型不能修改。

        Args:
            query(string): 要查的知识关键词或问题，例如“宿舍门禁时间”。
            limit(string): 最多返回几条，默认 "3"。
        """
        if not self._enabled():
            return "知识库未启用。"
        try:
            requested = int(str(limit or "3"))
        except (TypeError, ValueError):
            requested = int(self._cfg("knowledge_search_limit", 3))
        requested = max(1, min(8, requested))
        rows = self._get_store().search_docs(
            scope=event_scope(event),
            persona_id=event_persona(event),
            query=str(query or ""),
            limit=requested,
        )
        if not rows:
            return "知识库里没有找到相关资料。"
        lines = ["[用户导入知识库]"]
        for index, row in enumerate(rows, 1):
            title = f"{row['title']}：" if row.get("title") else ""
            tag_text = f"（{row['tags']}）" if row.get("tags") else ""
            lines.append(f"{index}. [{row['id']}] {title}{row['content']}{tag_text}")
        return "\n".join(lines)

    @filter.llm_tool(name="memory_add")
    async def memory_add(self, event: AstrMessageEvent, content: str, tags: str = "") -> str:
        """记住一条用户明确说出的长期知识或偏好。

        Args:
            content(string): 要记住的简洁事实，例如“用户喜欢喝无糖可乐”。
            tags(string): 可选标签，逗号分隔，例如“饮食,偏好”。
        """
        if not self._enabled():
            return "知识记忆库未启用。"
        actor_hash, actor_label = event_actor(event)
        result = self._get_store().add(
            scope=event_scope(event),
            persona_id=event_persona(event),
            actor_hash=actor_hash,
            actor_label=actor_label,
            entry_kind="ai",
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
        actor_hash, _actor_label = event_actor(event)
        rows = self._get_store().search(
            scope=event_scope(event),
            persona_id=event_persona(event),
            actor_hash=actor_hash,
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
        actor_hash, _actor_label = event_actor(event)
        ok = self._get_store().forget(
            scope=event_scope(event),
            persona_id=event_persona(event),
            actor_hash=actor_hash,
            entry_id=str(entry_id or ""),
        )
        return "已删除。" if ok else "没有找到可删除的记忆。"

    def _cards_db_path(self) -> Path:
        return self._data_dir().parent / "qqbot_memory_commands" / "memory_cards.db"

    def _read_cards(self, *, scope: str, persona_id: str, limit: int) -> list[str]:
        path = self._cards_db_path()
        if not path.exists():
            return []
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT content, kind, importance FROM card "
                "WHERE status='active' AND scope=? AND (persona_id=? OR persona_id='') "
                "AND kind IN ('episode','open_topic','fact','preference') "
                "ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (scope, persona_id, max(1, min(8, int(limit)))),
            ).fetchall()
            connection.close()
            return [str(row["content"] or "").strip() for row in rows if str(row["content"] or "").strip()]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Automatic recall: small core memories + retrieved documents.
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_memory_context(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._enabled() or not bool(self._cfg("auto_recall_enabled", True)):
            return
        try:
            query = str(getattr(event, "message_str", "") or "").strip()
            if len(query) < 4 or query.startswith(("/", "[[", "【")):
                return
            store = self._get_store()
            scope = event_scope(event)
            persona_id = event_persona(event) or "default"
            max_items = max(1, min(20, int(self._cfg("memory_layer_max_items", 6))))
            max_layer_chars = max(160, min(2400, int(self._cfg("memory_layer_max_chars", 600))))
            max_total = max(400, min(6000, int(self._cfg("auto_recall_max_chars", 1200))))
            sections: list[str] = []
            used = 0

            def key(text: str) -> str:
                return re.sub(r"\W+", "", str(text or "").casefold())[:80]

            seen: set[str] = set()

            def add_section(label: str, lines: list[str]) -> bool:
                nonlocal used
                if not lines:
                    return False
                body = "\n".join(lines)
                remaining = max_total - used
                if remaining <= 0:
                    return False
                if len(body) > remaining:
                    body = body[: max(0, remaining - 1)].rstrip() + "…"
                block = f"[{label}]\n{body}"
                sections.append(block)
                used += len(block)
                return True

            rows = store.layers_for_prompt(persona_id=persona_id, scope=scope, limit=max_items)
            for layer, label in (("persona", "人格记忆"), ("group", "群记忆")):
                lines: list[str] = []
                for row in [item for item in rows if item.get("layer") == layer]:
                    text = str(row.get("content") or "").strip()
                    if not text:
                        continue
                    if row.get("title"):
                        text = f"{row['title']}：{text}"
                    marker = key(text)
                    if not marker or marker in seen:
                        continue
                    seen.add(marker)
                    lines.append(f"- {text[:400]}")
                    if len("\n".join(lines)) >= max_layer_chars:
                        break
                add_section(label, lines)

            top_k = max(1, min(8, int(self._cfg("auto_recall_top_k", 3))))
            docs = store.search_docs(scope=scope, persona_id=persona_id, query=query, limit=top_k)
            doc_lines: list[str] = []
            for row in docs:
                title = f"{row.get('title')}：" if row.get("title") else ""
                text = f"{title}{str(row.get('content') or '')[:420]}"
                marker = key(text)
                if not marker or marker in seen:
                    continue
                seen.add(marker)
                doc_lines.append(f"- {text}")
            add_section("共享知识库参考", doc_lines)

            card_count = 0
            if bool(self._cfg("auto_recall_cards_enabled", True)):
                card_limit = max(1, min(8, int(self._cfg("auto_recall_cards_limit", 3))))
                card_lines: list[str] = []
                for text in self._read_cards(scope=scope, persona_id=persona_id, limit=card_limit):
                    marker = key(text)
                    if not marker or marker in seen:
                        continue
                    if any(marker in old or old in marker for old in seen if len(old) >= 8):
                        continue
                    seen.add(marker)
                    card_lines.append(f"- {text[:360]}")
                card_count = len(card_lines)
                add_section("记忆卡片", card_lines)

            personal_facts = 0
            if bool(self._cfg("auto_recall_personal_enabled", True)):
                actor_hash, _actor_label = event_actor(event)
                facts = store.search(
                    scope=scope,
                    persona_id=persona_id,
                    actor_hash=actor_hash,
                    query=query,
                    limit=3,
                )
                add_section("个人记忆", [f"- {str(row.get('content') or '')[:300]}" for row in facts])
                personal_facts = len(facts)

            if sections:
                prompt = str(getattr(req, "system_prompt", "") or "")
                req.system_prompt = prompt + "\n\n" + "\n\n".join(sections)
                event.set_extra("qqbot_memory_bank_auto_recall", True)
                logger.info(
                    "memory auto recall: layers=%d docs=%d cards=%d facts=%d chars=%d",
                    sum(1 for _ in rows),
                    len(docs),
                    card_count,
                    personal_facts,
                    len(req.system_prompt) - len(prompt),
                )
        except Exception:
            logger.exception("qqbot_memory_bank auto recall failed")

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
        actor_hash, actor_label = event_actor(event)
        buffer_key = f"{scope}|{actor_hash}"
        buffer = self._turns.setdefault(buffer_key, [])
        buffer.append(text[:500])
        del buffer[:-10]
        every = max(2, min(20, int(self._cfg("extract_every_turns", 4))))
        if len(buffer) < every:
            return
        messages = list(buffer)
        self._turns[buffer_key] = []
        asyncio.create_task(
            self._extract_and_store(
                scope,
                event_persona(event),
                str(getattr(event, "unified_msg_origin", "") or ""),
                messages,
                actor_hash,
                actor_label,
            )
        )

    async def _extract_and_store(
        self,
        scope: str,
        persona_id: str,
        umo: str,
        messages: list[str],
        actor_hash: str = "",
        actor_label: str = "",
    ) -> None:
        try:
            mode = str(self._cfg("routing_mode", "ai") or "ai").strip().lower()
            routes: list[dict[str, Any]] = []
            source = "aux"
            if mode == "ai":
                routes = await self._aux_routes(umo, messages)
            if not routes:
                routes = router.heuristic_routes(messages)
                source = "rule"
            limit = max(1, min(10, int(self._cfg("max_facts_per_extract", 5))))
            min_conf = max(0.0, min(1.0, float(self._cfg("routing_min_confidence", 0.6))))
            auto_core = bool(self._cfg("auto_write_core_memory", False))
            for item in routes[:limit]:
                route = str(item.get("route") or "ignore").strip().lower()
                content = str(item.get("content") or "").strip()
                confidence = float(item.get("confidence") or 0.0)
                reason = str(item.get("reason") or "")[:160]
                if not content or route == "ignore":
                    continue
                if route == "user_fact":
                    self._get_store().add(
                        scope=scope,
                        persona_id=persona_id,
                        actor_hash=actor_hash,
                        actor_label=actor_label,
                        entry_kind="ai",
                        content=content,
                        source=source,
                        confidence=max(0.0, min(1.0, confidence)),
                        max_entries=int(self._cfg("max_entries", 2000)),
                    )
                    continue
                if route in {"persona_memory", "group_memory"} and auto_core and confidence >= min_conf:
                    layer = "persona" if route == "persona_memory" else "group"
                    owner = persona_id if layer == "persona" else scope
                    if owner:
                        self._get_store().add_layer(
                            layer=layer,
                            owner_id=owner,
                            title="AI 路由",
                            content=content,
                            tags="ai-routed",
                        )
                    continue
                # Cards/docs remain human-owned; AI can only leave a reviewable
                # suggestion, never silently rewrite them.
                self._get_store().add_inbox(
                    scope=scope,
                    persona_id=persona_id,
                    actor_hash=actor_hash,
                    actor_label=actor_label,
                    route=route,
                    content=content,
                    confidence=confidence,
                    reason=reason,
                    source=source,
                )
        except Exception:
            logger.debug("qqbot_memory_bank extraction failed", exc_info=True)

    async def _aux_routes(self, umo: str, messages: list[str]) -> list[dict[str, Any]]:
        try:
            prompt = "用户消息：\n" + "\n".join(f"- {m}" for m in messages[-12:])
            dedicated = await aux_ai.generate(
                plugin_data_dir=self._data_dir().parent,
                system_prompt=router.route_prompt(),
                prompt=prompt,
                max_tokens=500,
                temperature=0.1,
                timeout=float(self._cfg("aux_timeout_seconds", 15) or 15),
            )
            if dedicated:
                routed = router.parse_routes(dedicated, limit=int(self._cfg("max_facts_per_extract", 5)))
                if routed:
                    return routed
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
            prompt = "用户消息：\n" + "\n".join(f"- {m}" for m in messages[-12:])
            pending = generator(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=router.route_prompt(),
                max_tokens=500,
                temperature=0.1,
            )
            response = await asyncio.wait_for(pending, timeout=12) if inspect.isawaitable(pending) else pending
            raw = (
                getattr(response, "completion_text", None)
                or getattr(response, "text", None)
                or getattr(response, "_completion_text", None)
                or response
            )
            return router.parse_routes(str(raw or ""), limit=int(self._cfg("max_facts_per_extract", 5)))
        except Exception:
            logger.debug("qqbot_memory_bank auxiliary routing failed", exc_info=True)
            return []

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
