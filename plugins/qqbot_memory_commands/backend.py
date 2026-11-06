"""Optional AstrBot public-API backend adapter.

Memory uses the already-patched ``MemoryService`` explicit-scope methods
(``export``/``add``/``forget``); sessions/personas use AstrBot's public
ConversationManager/PersonaManager when present.  There is no fallback to
qqbot_enhancer or qqbot_core_full and no second direct DB writer.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from .contracts import (
    STATUS_DEPENDENCY_MISSING,
    STATUS_INTERNAL_ERROR,
    STATUS_INVALID_ARGS,
    STATUS_NOT_FOUND,
    STATUS_OK,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_REVISION_CONFLICT,
    RequestContext,
    ScopeRef,
    canonical_hash,
    error_result,
    make_result,
)


def _value_or_attr(source: Any, *names: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
        return default
    for name in names:
        try:
            value = getattr(source, name)
        except Exception:  # noqa: BLE001 - duck-typed AstrBot object
            continue
        if value is not None:
            return value
    return default


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _records_from(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, (list, tuple)):
        return None
    records: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            records.append(dict(item))
        else:
            return None
    return records


def _conversation_id(value: Any) -> str:
    found = _value_or_attr(value, "cid", "conversation_id", "id", default="")
    return str(found or "").strip()


def _conversation_title(value: Any) -> str:
    found = _value_or_attr(value, "title", "name", default="")
    return str(found or "").strip()


def _conversation_persona(value: Any) -> str:
    found = _value_or_attr(value, "persona_id", default="")
    return str(found or "").strip()


def _history(value: Any) -> tuple[list[Any] | None, bool]:
    if isinstance(value, Mapping):
        for key in ("history", "messages"):
            if key in value:
                raw = value.get(key)
                if raw is None:
                    return [], True
                if isinstance(raw, (list, tuple)):
                    return list(raw), True
                return None, True
    for name in ("history", "messages"):
        try:
            if hasattr(value, name):
                raw = getattr(value, name)
                if raw is None:
                    return [], True
                if isinstance(raw, (list, tuple)):
                    return list(raw), True
                return None, True
        except Exception:  # noqa: BLE001 - malformed public object
            return None, False
    return None, False


def _message_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return {
            "role": str(message.get("role") or ""),
            "content": message.get("content", ""),
        }
    return {
        "role": str(_value_or_attr(message, "role", default="") or ""),
        "content": _value_or_attr(message, "content", default=""),
    }


class AstrBotBackend:
    """Small adapter over optional, already-resolved AstrBot services."""

    def __init__(
        self,
        *,
        memory_service: Any = None,
        conversation_manager: Any = None,
        persona_manager: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.memory_service = memory_service
        self.conversation_manager = conversation_manager
        self.persona_manager = persona_manager
        self._clock = clock

    @property
    def ready(self) -> bool:
        return bool(
            self.memory_service is not None
            or self.conversation_manager is not None
            or self.persona_manager is not None
        )

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "memory": callable(getattr(self.memory_service, "export", None)),
            "sessions": bool(
                self.conversation_manager is not None
                and callable(
                    getattr(self.conversation_manager, "get_curr_conversation_id", None)
                )
                and callable(
                    getattr(self.conversation_manager, "get_conversation", None)
                )
            ),
            "personas": bool(
                self.persona_manager is not None
                and callable(getattr(self.persona_manager, "get_persona", None))
            ),
        }

    # ------------------------------------------------------------------
    # common helpers
    # ------------------------------------------------------------------
    async def _await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _memory_scope_id(self, scope: ScopeRef) -> str:
        if scope.kind == "global":
            return "all"
        if scope.kind in {"user", "group"}:
            target = str(scope.target_id)
            prefix = f"{scope.platform}:"
            return target if target.startswith(prefix) else f"{prefix}{target}"
        return str(scope.target_id)

    def _memory_service(self) -> Any:
        service = self.memory_service
        if service is None:
            return None
        if callable(getattr(service, "export", None)) is False:
            return None
        enabled = getattr(service, "enabled", True)
        if callable(enabled):
            try:
                enabled = enabled()
            except Exception:  # noqa: BLE001 - disabled/invalid means fail closed
                enabled = False
        if enabled is False:
            return None
        return service

    def _conversation_manager(self) -> Any:
        manager = self.conversation_manager
        if manager is None:
            return None
        if not callable(getattr(manager, "get_conversation", None)):
            return None
        if not callable(getattr(manager, "get_curr_conversation_id", None)):
            return None
        return manager

    def _umo(self, context: RequestContext) -> str:
        value = str(context.unified_msg_origin or "").strip()
        return value or str(context.conversation_id or "").strip()

    async def _current_cid(self, context: RequestContext) -> str:
        manager = self._conversation_manager()
        if manager is None:
            return ""
        try:
            value = await self._await(
                manager.get_curr_conversation_id(self._umo(context))
            )
        except Exception:  # noqa: BLE001 - public API failure is dependency error
            return ""
        return str(value or "").strip()

    async def _get_conversation(self, context: RequestContext, cid: str) -> Any:
        manager = self._conversation_manager()
        if manager is None or not cid:
            return None
        try:
            return await self._await(
                manager.get_conversation(self._umo(context), cid)
            )
        except Exception:  # noqa: BLE001 - fail closed above caller
            return None

    def _conversation_revision(self, conversation: Any, cid: str) -> str:
        explicit = _value_or_attr(
            conversation,
            "revision",
            "updated_at",
            default=None,
        )
        if explicit is not None:
            return str(explicit)
        messages, _available = _history(conversation)
        return canonical_hash(
            {
                "cid": str(cid),
                "persona_id": _conversation_persona(conversation),
                "history": messages or [],
            }
        )

    async def _update_conversation_kwargs(
        self,
        context: RequestContext,
        cid: str,
        **kwargs: Any,
    ) -> Any:
        """Call update_conversation only when the public signature accepts it."""

        manager = self._conversation_manager()
        update = getattr(manager, "update_conversation", None) if manager else None
        if not callable(update):
            return None
        call_kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(update)
        except (TypeError, ValueError):
            signature = None
        if signature is None:
            call_kwargs = dict(kwargs)
        else:
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            for name, value in kwargs.items():
                if accepts_kwargs or name in signature.parameters:
                    call_kwargs[name] = value
        if set(call_kwargs) != set(kwargs):
            return None
        return await self._await(update(self._umo(context), cid, **call_kwargs))

    # ------------------------------------------------------------------
    # memory
    # ------------------------------------------------------------------
    async def memory_read(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        service = self._memory_service()
        if service is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            raw = await self._await(
                service.export(scope.kind, self._memory_scope_id(scope))
            )
        except Exception:  # noqa: BLE001 - do not fake an empty read
            return error_result(STATUS_INTERNAL_ERROR, "backend_error", retryable=True)
        records = _records_from(raw)
        if records is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        return make_result(
            STATUS_OK,
            data={
                "records": records,
                "count": len(records),
                "scope": scope.to_dict(),
                "revision": canonical_hash(records),
            },
        )

    async def memory_search(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        service = self._memory_service()
        if service is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            raw = await self._await(
                service.export(scope.kind, self._memory_scope_id(scope))
            )
        except Exception:  # noqa: BLE001 - do not fake an empty search
            return error_result(STATUS_INTERNAL_ERROR, "backend_error", retryable=True)
        records = _records_from(raw)
        if records is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        needle = query.casefold()
        matches = [
            record
            for record in records
            if needle in str(record.get("content", "")).casefold()
            or needle in str(record.get("key", "")).casefold()
        ]
        matches.sort(
            key=lambda record: (
                -_as_int(record.get("hits", 0)),
                -_as_float(record.get("updated_at", 0)),
            )
        )
        limit = max(1, min(50, _as_int(args.get("limit"), 8)))
        return make_result(
            STATUS_OK,
            data={
                "records": matches[:limit],
                "count": len(matches),
                "returned": min(len(matches), limit),
                "query": query,
                "scope": scope.to_dict(),
            },
        )

    async def memory_add(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        service = self._memory_service()
        if service is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        add = getattr(service, "add", None)
        if not callable(add):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        content = str(args.get("content") or "").strip()
        key = str(args.get("key") or "").strip()
        if not content or not key:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        payload = {
            "kind": str(args.get("kind") or "fact"),
            "importance": min(1.0, max(0.0, _as_float(args.get("importance"), 0.6))),
            "confidence": min(1.0, max(0.0, _as_float(args.get("confidence"), 0.8))),
            "ttl_seconds": (
                max(1, _as_int(args.get("ttl_seconds"), 1))
                if args.get("ttl_seconds") is not None
                else None
            ),
            "source": "memory_commands",
        }
        try:
            action, record = await self._await(
                add(
                    scope.kind,
                    self._memory_scope_id(scope),
                    key,
                    content,
                    **payload,
                )
            )
        except Exception:  # noqa: BLE001 - write outcome is not provably absent
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        record_data = dict(record) if isinstance(record, Mapping) else {}
        return make_result(
            STATUS_OK,
            data={
                "action": str(action or ""),
                "id": _as_int(record_data.get("id"), 0) or None,
                "key": key,
                "scope": scope.to_dict(),
                "revision": canonical_hash(record_data or {"key": key, "action": action}),
            },
        )

    async def _memory_records(self, scope: ScopeRef) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        service = self._memory_service()
        if service is None:
            return None, error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            raw = await self._await(
                service.export(scope.kind, self._memory_scope_id(scope))
            )
        except Exception:  # noqa: BLE001 - read failure is explicit
            return None, error_result(STATUS_INTERNAL_ERROR, "backend_error", retryable=True)
        records = _records_from(raw)
        if records is None:
            return None, error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        return records, None

    def _collection_revision(self, records: list[dict[str, Any]]) -> str:
        return canonical_hash(
            [
                {
                    "id": record.get("id"),
                    "key": record.get("key"),
                    "updated_at": record.get("updated_at"),
                    "content_hash": hashlib.sha256(
                        str(record.get("content", "")).encode("utf-8")
                    ).hexdigest(),
                }
                for record in sorted(
                    records,
                    key=lambda item: (
                        _as_int(item.get("id"), 0),
                        str(item.get("key", "")),
                    ),
                )
            ]
        )

    async def get_memory_revision(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        records, error = await self._memory_records(scope)
        if error is not None:
            return error
        assert records is not None
        return make_result(
            STATUS_OK,
            data={
                "revision": self._collection_revision(records),
                "count": len(records),
                "item_ids": [
                    _as_int(record.get("id"), 0)
                    for record in records
                    if _as_int(record.get("id"), 0) > 0
                ][:200],
                "scope": scope.to_dict(),
            },
        )

    async def memory_preview_clear(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self.get_memory_revision(scope, context, args)

    async def memory_confirm_clear(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        expected_revision = str(args.get("expected_revision") or "")
        records, error = await self._memory_records(scope)
        if error is not None:
            return error
        assert records is not None
        current_revision = self._collection_revision(records)
        if not expected_revision or expected_revision != current_revision:
            return error_result(
                STATUS_REVISION_CONFLICT,
                "stale_revision",
                data={
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )
        service = self._memory_service()
        if service is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        forget = getattr(service, "forget", None)
        if not callable(forget):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            count = await self._await(
                forget(scope.kind, self._memory_scope_id(scope))
            )
        except Exception:  # noqa: BLE001 - clear may have partially happened
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        count = _as_int(count, 0)
        if len(records) > 0 and count <= 0:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        return make_result(
            STATUS_OK,
            data={
                "count": count,
                "cleared": count,
                "scope": scope.to_dict(),
                "revision": canonical_hash({"clear": scope.canonical_id, "count": count}),
            },
        )

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    async def session_open(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        manager = self._conversation_manager()
        if manager is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        new_conversation = getattr(manager, "new_conversation", None)
        switch = getattr(manager, "switch_conversation", None)
        if not callable(new_conversation) or not callable(switch):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        title = str(args.get("title") or "").strip()[:64]
        persona_id = str(args.get("persona_id") or "").strip()
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(new_conversation)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if title and (accepts_kwargs or "title" in signature.parameters):
                kwargs["title"] = title
            if persona_id and (accepts_kwargs or "persona_id" in signature.parameters):
                kwargs["persona_id"] = persona_id
        elif title:
            kwargs["title"] = title
            if persona_id:
                kwargs["persona_id"] = persona_id
        try:
            created = await self._await(new_conversation(self._umo(context), **kwargs))
        except Exception:  # noqa: BLE001 - create may have happened
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        cid = _conversation_id(created)
        if not cid:
            cid = await self._current_cid(context)
        if not cid:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        try:
            await self._await(switch(self._umo(context), cid))
        except Exception:  # noqa: BLE001 - creation happened but switch failed
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
                data={"conversation_id": cid},
            )
        confirmed = await self._current_cid(context)
        if confirmed != cid:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
                data={"conversation_id": cid},
            )
        conversation = await self._get_conversation(context, cid)
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": cid,
                "title": _conversation_title(conversation) or title,
                "persona_id": _conversation_persona(conversation) or persona_id,
                "revision": self._conversation_revision(conversation, cid),
                "scope": scope.to_dict(),
            },
        )

    async def _read_conversation(
        self, context: RequestContext, cid: str
    ) -> tuple[Any | None, list[Any] | None, dict[str, Any] | None]:
        manager = self._conversation_manager()
        if manager is None:
            return None, None, error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        conversation = await self._get_conversation(context, cid)
        if conversation is None:
            return None, None, error_result(STATUS_NOT_FOUND, "not_found")
        history, available = _history(conversation)
        if not available or history is None:
            return conversation, None, error_result(
                STATUS_DEPENDENCY_MISSING,
                "dependency_missing",
            )
        return conversation, history, None

    async def session_read(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        cid = str(args.get("conversation_id") or args.get("cid") or "").strip()
        if not cid:
            cid = await self._current_cid(context)
        if not cid:
            return error_result(STATUS_NOT_FOUND, "not_found")
        conversation, history, error = await self._read_conversation(context, cid)
        if error is not None:
            return error
        assert history is not None
        messages = [_message_dict(message) for message in history]
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": cid,
                "title": _conversation_title(conversation),
                "persona_id": _conversation_persona(conversation),
                "messages": messages,
                "message_count": len(messages),
                "revision": self._conversation_revision(conversation, cid),
                "scope": scope.to_dict(),
            },
        )

    async def get_session_revision(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = args if isinstance(args, Mapping) else {}
        cid = str(args.get("conversation_id") or args.get("cid") or "").strip()
        if not cid:
            cid = await self._current_cid(context)
        if not cid:
            return error_result(STATUS_NOT_FOUND, "not_found")
        conversation, _history_items, error = await self._read_conversation(context, cid)
        if error is not None:
            return error
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": cid,
                "revision": self._conversation_revision(conversation, cid),
                "persona_id": _conversation_persona(conversation),
            },
        )

    async def session_append(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        role = str(args.get("role") or "").strip()
        content = str(args.get("content") or "")
        sequence = _as_int(args.get("sequence"), -1)
        turn_id = str(args.get("turn_id") or context.turn_id or "").strip()
        if role not in {"system", "user", "assistant", "tool"} or not content or sequence < 0:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        if turn_id != context.turn_id:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        cid = await self._current_cid(context)
        if not cid:
            return error_result(STATUS_NOT_FOUND, "not_found")
        conversation, history, error = await self._read_conversation(context, cid)
        if error is not None:
            return error
        assert history is not None
        current_revision = self._conversation_revision(conversation, cid)
        expected_revision = str(args.get("expected_revision") or "").strip()
        if expected_revision and expected_revision != current_revision:
            return error_result(
                STATUS_REVISION_CONFLICT,
                "stale_revision",
                data={
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )
        message: dict[str, Any] = {"role": role, "content": content}
        if role == "tool" and args.get("tool_call_id"):
            message["tool_call_id"] = str(args["tool_call_id"])
        updated = list(history)
        updated.append(message)
        result = await self._update_conversation_kwargs(
            context,
            cid,
            history=updated,
        )
        if result is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        verified_conversation = await self._get_conversation(context, cid)
        verified_history, available = _history(verified_conversation)
        if not available or not verified_history:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        last = _message_dict(verified_history[-1])
        if last.get("role") != role or str(last.get("content")) != content:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": cid,
                "turn_id": turn_id,
                "role": role,
                "sequence": sequence,
                "message_count": len(verified_history),
                "revision": self._conversation_revision(verified_conversation, cid),
                "scope": scope.to_dict(),
            },
        )

    async def session_reset(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        cid = await self._current_cid(context)
        if not cid:
            return error_result(STATUS_NOT_FOUND, "not_found")
        conversation, history, error = await self._read_conversation(context, cid)
        if error is not None:
            return error
        expected_revision = str(args.get("expected_revision") or "").strip()
        current_revision = self._conversation_revision(conversation, cid)
        if expected_revision and expected_revision != current_revision:
            return error_result(
                STATUS_REVISION_CONFLICT,
                "stale_revision",
                data={
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )
        result = await self._update_conversation_kwargs(context, cid, history=[])
        if result is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        verified = await self._get_conversation(context, cid)
        verified_history, available = _history(verified)
        if not available or verified_history is None or len(verified_history) != 0:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": cid,
                "message_count": 0,
                "revision": self._conversation_revision(verified, cid),
                "scope": scope.to_dict(),
            },
        )

    async def session_switch(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        manager = self._conversation_manager()
        if manager is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        switch = getattr(manager, "switch_conversation", None)
        if not callable(switch):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        target = str(
            args.get("conversation_id") or args.get("cid") or scope.target_id or ""
        ).strip()
        if not target:
            return error_result(STATUS_INVALID_ARGS, "invalid_args")
        current_cid = await self._current_cid(context)
        current_conversation = await self._get_conversation(context, current_cid) if current_cid else None
        current_revision = (
            self._conversation_revision(current_conversation, current_cid)
            if current_conversation is not None
            else ""
        )
        expected_revision = str(args.get("expected_revision") or "").strip()
        if expected_revision and expected_revision != current_revision:
            return error_result(
                STATUS_REVISION_CONFLICT,
                "stale_revision",
                data={
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )
        target_conversation = await self._get_conversation(context, target)
        if target_conversation is None:
            return error_result(STATUS_NOT_FOUND, "not_found")
        try:
            result = await self._await(switch(self._umo(context), target))
        except Exception:  # noqa: BLE001 - switch may have happened
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
            )
        if result is False:
            return error_result(STATUS_INTERNAL_ERROR, "backend_error")
        confirmed = await self._current_cid(context)
        if confirmed != target:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
                data={"conversation_id": target},
            )
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": target,
                "title": _conversation_title(target_conversation),
                "persona_id": _conversation_persona(target_conversation),
                "revision": self._conversation_revision(target_conversation, target),
                "scope": scope.to_dict(),
            },
        )

    # ------------------------------------------------------------------
    # personas
    # ------------------------------------------------------------------
    def _persona_public(self, value: Any) -> dict[str, Any]:
        persona_id = str(
            _value_or_attr(value, "persona_id", "id", default="") or ""
        )
        return {
            "persona_id": persona_id,
            "name": str(_value_or_attr(value, "name", "display_name", default="") or ""),
        }

    async def persona_list(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        manager = self.persona_manager
        getter = None
        if manager is not None:
            getter = getattr(manager, "get_personas", None)
            if not callable(getter):
                getter = getattr(manager, "get_all_personas", None)
        if not callable(getter):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        try:
            raw = await self._await(getter())
        except Exception:  # noqa: BLE001 - do not fake empty list
            return error_result(STATUS_INTERNAL_ERROR, "backend_error", retryable=True)
        if not isinstance(raw, (list, tuple)):
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        return make_result(
            STATUS_OK,
            data={
                "personas": [self._persona_public(item) for item in raw],
                "count": len(raw),
                "current_persona_id": str(context.persona_id or ""),
                "scope": scope.to_dict(),
            },
        )

    async def _persona_target(
        self,
        context: RequestContext,
        args: Mapping[str, Any],
        scope: ScopeRef,
    ) -> tuple[str, Any | None, dict[str, Any] | None]:
        target = str(
            args.get("persona_id") or args.get("target_id") or scope.target_id or ""
        ).strip()
        if not target:
            return "", None, error_result(STATUS_INVALID_ARGS, "invalid_args")
        manager = self.persona_manager
        getter = getattr(manager, "get_persona", None) if manager else None
        if not callable(getter):
            return target, None, error_result(
                STATUS_DEPENDENCY_MISSING,
                "dependency_missing",
            )
        try:
            persona = await self._await(getter(target))
        except Exception:  # noqa: BLE001 - fail closed rather than assume exists
            return target, None, error_result(
                STATUS_DEPENDENCY_MISSING,
                "dependency_missing",
            )
        if persona is None:
            return target, None, error_result(STATUS_NOT_FOUND, "not_found")
        return target, persona, None

    async def _set_persona(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
        *,
        create_new: bool,
    ) -> dict[str, Any]:
        target, persona, error = await self._persona_target(context, args, scope)
        if error is not None:
            return error
        manager = self._conversation_manager()
        if manager is None:
            return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        if create_new:
            new_conversation = getattr(manager, "new_conversation", None)
            switch = getattr(manager, "switch_conversation", None)
            if not callable(new_conversation) or not callable(switch):
                return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
            title = str(args.get("title") or "").strip()[:64]
            try:
                created = await self._await(
                    new_conversation(
                        self._umo(context),
                        title=title or None,
                        persona_id=target,
                    )
                )
            except Exception:  # noqa: BLE001 - creation may have happened
                return error_result(
                    STATUS_OUTCOME_UNKNOWN,
                    "backend_error",
                    side_effect_state="unknown",
                )
            cid = _conversation_id(created) or await self._current_cid(context)
            if not cid:
                return error_result(
                    STATUS_OUTCOME_UNKNOWN,
                    "backend_error",
                    side_effect_state="unknown",
                )
            try:
                await self._await(switch(self._umo(context), cid))
            except Exception:  # noqa: BLE001 - creation side effect is unknown
                return error_result(
                    STATUS_OUTCOME_UNKNOWN,
                    "backend_error",
                    side_effect_state="unknown",
                    data={"conversation_id": cid, "persona_id": target},
                )
        else:
            cid = await self._current_cid(context)
            if not cid:
                return error_result(STATUS_NOT_FOUND, "not_found")
            conversation = await self._get_conversation(context, cid)
            if conversation is None:
                return error_result(STATUS_NOT_FOUND, "not_found")
            current_revision = self._conversation_revision(conversation, cid)
            expected_revision = str(args.get("expected_revision") or "").strip()
            if expected_revision and expected_revision != current_revision:
                return error_result(
                    STATUS_REVISION_CONFLICT,
                    "stale_revision",
                    data={
                        "expected_revision": expected_revision,
                        "current_revision": current_revision,
                    },
                )
            result = await self._update_conversation_kwargs(
                context,
                cid,
                persona_id=target,
            )
            if result is None:
                return error_result(STATUS_DEPENDENCY_MISSING, "dependency_missing")
        verified = await self._get_conversation(context, cid)
        if verified is None or _conversation_persona(verified) != target:
            return error_result(
                STATUS_OUTCOME_UNKNOWN,
                "backend_error",
                side_effect_state="unknown",
                data={"conversation_id": cid, "persona_id": target},
            )
        return make_result(
            STATUS_OK,
            data={
                "conversation_id": cid,
                "persona_id": target,
                "name": self._persona_public(persona).get("name", ""),
                "revision": self._conversation_revision(verified, cid),
                "scope": scope.to_dict(),
            },
        )

    async def persona_bind(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._set_persona(scope, context, args, create_new=False)

    async def persona_switch(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._set_persona(
            scope,
            context,
            args,
            create_new=bool(args.get("create_new", False)),
        )

    async def get_persona_revision(
        self,
        scope: ScopeRef,
        context: RequestContext,
        args: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.get_session_revision(scope, context, args)
