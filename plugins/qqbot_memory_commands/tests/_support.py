"""Shared fakes for memory ABI/scope/session/provider tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from qqbot_memory_commands import contracts


def make_context(
    method: str,
    *,
    scope: str = "user",
    instance: str = "inst",
    platform: str = "aiocqhttp",
    bot: str = "bot-1",
    actor: str = "actor-1",
    conversation: str = "aiocqhttp:GroupMessage:group-1",
    persona: str = "persona-1",
    private: bool = False,
    group_id: str = "group-1",
    admin: bool = False,
    roles: tuple[str, ...] = (),
    allow_shared_persona: bool = False,
    allow_global_scope: bool = False,
    allowed_group_ids: tuple[str, ...] = (),
    allowed_conversation_ids: tuple[str, ...] = (),
    allowed_persona_ids: tuple[str, ...] = (),
    source: str = "msg-1",
    turn: str = "turn-1",
    owner_epoch: int = 0,
    deadline_seconds: float = 60.0,
) -> contracts.RequestContext:
    return contracts.RequestContext(
        protocol_version=contracts.PROTOCOL_VERSION,
        instance_id=instance,
        platform=platform,
        bot_id=bot,
        actor_id=actor,
        conversation_id=conversation,
        persona_id=persona,
        source_message_id=source,
        turn_id=turn,
        capability=contracts.METHOD_CAPABILITIES[method],
        requested_scope=scope,
        owner_epoch=owner_epoch,
        deadline=time.time() + deadline_seconds,
        is_private=private,
        group_id="" if private else group_id,
        unified_msg_origin=conversation,
        roles=roles,
        admin=admin,
        allow_shared_persona=allow_shared_persona,
        allow_global_scope=allow_global_scope,
        allowed_group_ids=allowed_group_ids,
        allowed_conversation_ids=allowed_conversation_ids,
        allowed_persona_ids=allowed_persona_ids,
    )


class FakeBackend:
    """In-memory backend with explicit revision checks and call counters."""

    def __init__(self) -> None:
        self.ready = True
        self.memory_records: dict[str, list[dict[str, Any]]] = {}
        self.memory_revisions: dict[str, int] = {}
        self.leak_records: list[dict[str, Any]] = []
        self.add_calls = 0
        self.clear_calls = 0
        self.append_calls = 0
        self.reset_calls = 0
        self.switch_calls = 0
        self.open_calls = 0
        self.persona_calls = 0
        self.sessions: dict[str, dict[str, Any]] = {}
        self.personas = [
            {"persona_id": "persona-1", "name": "Current"},
            {"persona_id": "persona-2", "name": "Shared"},
            {"persona_id": "persona-3", "name": "Other"},
        ]
        self.next_message_id = 1

    # -- memory -----------------------------------------------------
    def scope_id(self, scope: contracts.ScopeRef) -> str:
        if scope.kind == "global":
            return "all"
        if scope.kind in {"user", "group"}:
            prefix = f"{scope.platform}:"
            return (
                scope.target_id
                if scope.target_id.startswith(prefix)
                else prefix + scope.target_id
            )
        return scope.target_id

    def _revision(self, scope: contracts.ScopeRef) -> int:
        return self.memory_revisions.get(scope.canonical_id, 0)

    def _bump(self, scope: contracts.ScopeRef) -> int:
        revision = self._revision(scope) + 1
        self.memory_revisions[scope.canonical_id] = revision
        return revision

    def _memory_result(self, scope: contracts.ScopeRef, method: str) -> dict[str, Any]:
        records = list(self.memory_records.get(scope.canonical_id, []))
        if self.leak_records:
            records.extend(self.leak_records)
        if method == "read":
            return contracts.make_result(
                "ok",
                data={"records": records, "count": len(records)},
            )
        return contracts.make_result(
            "ok",
            data={"records": records, "count": len(records), "query": ""},
        )

    async def memory_read(self, scope, context, args):
        result = self._memory_result(scope, "read")
        return result

    async def memory_search(self, scope, context, args):
        result = self._memory_result(scope, "search")
        result["data"]["query"] = args.get("query", "")
        return result

    async def memory_add(self, scope, context, args):
        self.add_calls += 1
        revision = self._bump(scope)
        record = {
            "id": self.next_message_id,
            "scope_type": scope.kind,
            "scope_id": self.scope_id(scope),
            "key": args.get("key", "k"),
            "content": args.get("content", ""),
            "hits": 0,
            "updated_at": float(revision),
        }
        self.next_message_id += 1
        self.memory_records.setdefault(scope.canonical_id, []).append(record)
        return contracts.make_result(
            "ok",
            data={
                "action": "created",
                "id": record["id"],
                "key": record["key"],
                "revision": revision,
            },
        )

    async def get_memory_revision(self, scope, context, args=None):
        records = self.memory_records.get(scope.canonical_id, [])
        return contracts.make_result(
            "ok",
            data={
                "revision": f"memory-{self._revision(scope)}",
                "count": len(records),
                "item_ids": [record["id"] for record in records],
            },
        )

    async def memory_preview_clear(self, scope, context, args):
        return await self.get_memory_revision(scope, context, args)

    async def memory_confirm_clear(self, scope, context, args):
        self.clear_calls += 1
        current = f"memory-{self._revision(scope)}"
        expected = str(args.get("expected_revision") or "")
        if expected != current:
            return contracts.error_result("revision_conflict", "stale_revision")
        count = len(self.memory_records.get(scope.canonical_id, []))
        self.memory_records[scope.canonical_id] = []
        self._bump(scope)
        return contracts.make_result(
            "ok",
            data={"count": count, "cleared": count},
        )

    # -- sessions ---------------------------------------------------
    def session(self, scope: contracts.ScopeRef) -> dict[str, Any]:
        state = self.sessions.get(scope.target_id)
        if state is None:
            state = {
                "current": "conv-1",
                "revision": 1,
                "conversations": {
                    "conv-1": {
                        "cid": "conv-1",
                        "title": "会话一",
                        "persona_id": "persona-1",
                        "history": [{"role": "user", "content": "旧消息"}],
                    },
                    "conv-2": {
                        "cid": "conv-2",
                        "title": "会话二",
                        "persona_id": "persona-1",
                        "history": [],
                    },
                },
            }
            self.sessions[scope.target_id] = state
        return state

    async def get_session_revision(self, scope, context, args=None):
        state = self.session(scope)
        return contracts.make_result(
            "ok",
            data={
                "revision": state["revision"],
                "conversation_id": state["current"],
                "persona_id": state["conversations"][state["current"]]["persona_id"],
            },
        )

    async def session_read(self, scope, context, args):
        state = self.session(scope)
        conversation = state["conversations"][state["current"]]
        return contracts.make_result(
            "ok",
            data={
                "conversation_id": conversation["cid"],
                "title": conversation["title"],
                "persona_id": conversation["persona_id"],
                "messages": list(conversation["history"]),
                "message_count": len(conversation["history"]),
                "revision": state["revision"],
            },
        )

    async def session_append(self, scope, context, args):
        self.append_calls += 1
        state = self.session(scope)
        expected = args.get("expected_revision")
        if expected is not None and str(expected) != str(state["revision"]):
            return contracts.error_result("revision_conflict", "stale_revision")
        conversation = state["conversations"][state["current"]]
        conversation["history"].append(
            {"role": args["role"], "content": args["content"]}
        )
        state["revision"] += 1
        return contracts.make_result(
            "ok",
            data={
                "conversation_id": conversation["cid"],
                "turn_id": args["turn_id"],
                "role": args["role"],
                "sequence": args["sequence"],
                "message_count": len(conversation["history"]),
                "revision": state["revision"],
            },
        )

    async def session_reset(self, scope, context, args):
        self.reset_calls += 1
        state = self.session(scope)
        expected = args.get("expected_revision")
        if expected is not None and str(expected) != str(state["revision"]):
            return contracts.error_result("revision_conflict", "stale_revision")
        state["conversations"][state["current"]]["history"] = []
        state["revision"] += 1
        return contracts.make_result(
            "ok",
            data={
                "conversation_id": state["current"],
                "message_count": 0,
                "revision": state["revision"],
            },
        )

    async def session_switch(self, scope, context, args):
        self.switch_calls += 1
        await asyncio.sleep(0)
        state = self.session(scope)
        expected = args.get("expected_revision")
        if expected is not None and str(expected) != str(state["revision"]):
            return contracts.error_result("revision_conflict", "stale_revision")
        target = str(args.get("conversation_id") or "")
        if target not in state["conversations"]:
            return contracts.error_result("not_found", "not_found")
        state["current"] = target
        state["revision"] += 1
        return contracts.make_result(
            "ok",
            data={
                "conversation_id": target,
                "revision": state["revision"],
                "title": state["conversations"][target]["title"],
            },
        )

    async def session_open(self, scope, context, args):
        self.open_calls += 1
        state = self.session(scope)
        state["revision"] += 1
        cid = f"conv-new-{state['revision']}"
        state["conversations"][cid] = {
            "cid": cid,
            "title": args.get("title", ""),
            "persona_id": args.get("persona_id") or context.persona_id,
            "history": [],
        }
        state["current"] = cid
        return contracts.make_result(
            "ok",
            data={
                "conversation_id": cid,
                "title": args.get("title", ""),
                "persona_id": state["conversations"][cid]["persona_id"],
                "revision": state["revision"],
            },
        )

    # -- personas ---------------------------------------------------
    async def persona_list(self, scope, context, args):
        return contracts.make_result(
            "ok",
            data={"personas": list(self.personas), "count": len(self.personas)},
        )

    async def get_persona_revision(self, scope, context, args=None):
        return await self.get_session_revision(scope, context, args)

    async def _set_persona(self, scope, context, args):
        self.persona_calls += 1
        await asyncio.sleep(0)
        state = self.session(
            contracts.ScopeRef(
                "conversation",
                context.conversation_id,
                context.instance_id,
                context.platform,
                context.bot_id,
            )
        )
        expected = args.get("expected_revision")
        if expected is not None and str(expected) != str(state["revision"]):
            return contracts.error_result("revision_conflict", "stale_revision")
        target = str(args.get("persona_id") or "")
        if target not in {item["persona_id"] for item in self.personas}:
            return contracts.error_result("not_found", "not_found")
        state["conversations"][state["current"]]["persona_id"] = target
        state["revision"] += 1
        return contracts.make_result(
            "ok",
            data={
                "conversation_id": state["current"],
                "persona_id": target,
                "revision": state["revision"],
            },
        )

    async def persona_bind(self, scope, context, args):
        return await self._set_persona(scope, context, args)

    async def persona_switch(self, scope, context, args):
        return await self._set_persona(scope, context, args)
