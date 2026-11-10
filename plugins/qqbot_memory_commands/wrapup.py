"""Deterministic conversation wrap-up for memory cards v3.

The wrap-up layer never calls a model and never writes the raw memory store.
It derives episode/open-topic cards from already available messages and uses
the card store's ``wrapup_ledger`` for idempotency.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import cards

MAX_SUMMARY_CHARS = 240
MAX_PHASE_CHARS = 160
MAX_OPEN_TOPIC_CHARS = 120

_QUESTION_RE = re.compile(r"[?？]\s*$")
_SPACE_RE = re.compile(r"\s+")


def _content(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, Mapping):
        return str(message.get("content") or message.get("text") or "").strip()
    return str(getattr(message, "content", "") or "").strip()


def _role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or "user").strip().lower()
    return str(getattr(message, "role", "user") or "user").strip().lower()


def _compact(text: str, max_chars: int) -> str:
    text = _SPACE_RE.sub(" ", str(text or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def summarize_messages(messages: Iterable[Any], *, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Build a deterministic, bounded summary from user/assistant turns."""

    parts: list[str] = []
    for message in list(messages)[-12:]:
        role = _role(message)
        if role not in {"user", "assistant"}:
            continue
        text = _content(message)
        if not text:
            continue
        parts.append(("用户：" if role == "user" else "助手：") + text)
    return _compact("；".join(parts), max_chars)


def phase_summary(messages: Iterable[Any], *, max_chars: int = MAX_PHASE_CHARS) -> str:
    """Short phase summary for the last six visible turns."""

    return _compact(summarize_messages(list(messages)[-6:], max_chars=max_chars), max_chars)


def last_open_topic(messages: Iterable[Any], *, max_chars: int = MAX_OPEN_TOPIC_CHARS) -> str:
    for message in reversed(list(messages)[-20:]):
        if _role(message) != "user":
            continue
        text = _compact(_content(message), max_chars)
        if text and (_QUESTION_RE.search(text) or text.endswith("吗") or "怎么" in text or "如何" in text):
            return text
    return ""


def preview(
    *,
    conversation_id: str,
    session_revision: int,
    scope: str,
    messages: Iterable[Any],
    group_id: str = "",
    persona_id: str = "",
    actor_id: str = "",
    purpose_enabled: bool = False,
) -> dict[str, Any]:
    """Return a deterministic wrap-up plan; no persistence."""

    visible = list(messages)
    summary = summarize_messages(visible)
    phase = phase_summary(visible)
    open_topic = last_open_topic(visible)
    planned: list[dict[str, Any]] = []
    if summary:
        planned.append(
            {
                "kind": "episode",
                "subtype": "session_summary",
                "title": "会话摘要",
                "content": summary,
                "importance": 0.45,
                "confidence": 0.6,
            }
        )
    if phase and phase != summary:
        planned.append(
            {
                "kind": "episode",
                "subtype": "phase_summary",
                "title": "阶段摘要",
                "content": phase,
                "importance": 0.4,
                "confidence": 0.6,
            }
        )
    if open_topic:
        planned.append(
            {
                "kind": "open_topic",
                "subtype": "",
                "title": "未完成话题",
                "content": open_topic,
                "importance": 0.55,
                "confidence": 0.55,
            }
        )
    if purpose_enabled and open_topic:
        planned.append(
            {
                "kind": "purpose",
                "subtype": "",
                "title": "待办目的",
                "content": _compact("跟进：" + open_topic, MAX_OPEN_TOPIC_CHARS),
                "importance": 0.5,
                "confidence": 0.5,
            }
        )
    return {
        "conversation_id": str(conversation_id),
        "session_revision": int(session_revision),
        "scope": scope,
        "group_id": str(group_id or ""),
        "persona_id": str(persona_id or ""),
        "actor_id": str(actor_id or ""),
        "phase_summary": phase,
        "open_topic": open_topic,
        "cards": planned,
    }


def apply_plan(store: cards.CardStore, plan: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Apply a preview plan exactly once for its conversation revision."""

    conversation_id = str(plan.get("conversation_id") or "").strip()
    revision = int(plan.get("session_revision") or 0)
    if not conversation_id:
        raise cards.CardError("conversation_id 不能为空。")
    previous = store.get_wrapup(conversation_id, revision)
    if previous and previous.get("status") == "applied":
        return {"applied": False, "reason": "already_applied", "wrapup": previous}
    created: list[dict[str, Any]] = []
    try:
        for item in plan.get("cards") or []:
            if not isinstance(item, Mapping):
                continue
            card = store.create(
                scope=str(plan.get("scope") or "conversation"),
                content=str(item.get("content") or ""),
                kind=str(item.get("kind") or "episode"),
                subtype=str(item.get("subtype") or ""),
                title=str(item.get("title") or ""),
                group_id=str(plan.get("group_id") or ""),
                persona_id=str(plan.get("persona_id") or ""),
                actor_id=str(plan.get("actor_id") or ""),
                importance=float(item.get("importance") or 0.5),
                confidence=float(item.get("confidence") or 0.5),
                evidence={"source": "wrapup", "conversation_id": conversation_id, "session_revision": revision},
                now=now,
            )
            created.append(card)
        record = store.put_wrapup(
            conversation_id=conversation_id,
            session_revision=revision,
            status="applied",
            payload=plan,
            result={"card_ids": [card["id"] for card in created]},
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - record failure without masking it
        store.put_wrapup(
            conversation_id=conversation_id,
            session_revision=revision,
            status="failed",
            payload=plan,
            result={"error": type(exc).__name__},
            now=now,
        )
        raise
    return {"applied": True, "cards": created, "wrapup": record}
