"""AI-first memory routing.

The router deliberately does not decide by keywords.  The auxiliary model
receives the recent user messages and chooses one of the canonical owners;
deterministic code only validates the JSON, clamps confidence and provides a
fallback when the auxiliary model is unavailable.
"""

from __future__ import annotations

import json
import re
from typing import Any

ROUTES = (
    "user_fact",
    "persona_memory",
    "group_memory",
    "episode",
    "open_topic",
    "knowledge_doc",
    "ignore",
)
_AUTO_OWNERS = {"user_fact", "persona_memory", "group_memory"}


def route_prompt() -> str:
    return (
        "你是 QQ 群机器人的记忆路由助手。你只做分类，不写最终回复。"
        "根据用户最近消息，判断每条值得长期保留的内容应该归到哪一层：\n"
        "- user_fact：某个用户稳定的事实、偏好、厌恶、住址/工作/学校、习惯、计划、约定、生日、过敏。\n"
        "- persona_memory：用户明确要求机器人人格记住的身份、背景、关系或固定设定，不是普通聊天。\n"
        "- group_memory：整个群长期有效的规则、约定、群文化、固定活动，不是某一个人的偏好。\n"
        "- episode：已经发生的具体事件，值得以后回忆‘当时发生了什么’。\n"
        "- open_topic：还没有解决、以后需要继续追问或跟进的话题/任务。\n"
        "- knowledge_doc：用户明确要求长期保存的资料、文档、群规原文；这种内容只能建议人工导入。\n"
        "- ignore：寒暄、问句、助手的话、临时情绪、猜测、图片/链接、一次性闲聊、敏感凭据。\n"
        "不要用关键词表；按语义和长期价值判断。"
        "每条内容不超过 120 字，不要编造，不要把助手回复当成用户事实。"
        "最多返回 5 条。只输出 JSON："
        '{"items":[{"route":"user_fact|persona_memory|group_memory|episode|open_topic|knowledge_doc|ignore",'
        '"content":"...","confidence":0.0,"reason":"..."}]}。'
        "没有值得保留的内容就输出 {\"items\":[]}。"
    )


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def parse_routes(raw: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        route = _text(item.get("route")).lower()
        content = _text(item.get("content"))[:120]
        if route not in ROUTES or route == "ignore" or len(content) < 2:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        key = (route, content.casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "route": route,
                "content": content,
                "confidence": confidence,
                "reason": _text(item.get("reason"))[:160],
            }
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


def heuristic_routes(messages: list[str]) -> list[dict[str, Any]]:
    """Conservative fallback only; the normal path is the auxiliary model."""
    try:
        from . import store
    except ImportError:  # AstrBot loads modules as top-level
        import store  # type: ignore[no-redef]
    out: list[dict[str, Any]] = []
    for fact in store.heuristic_facts("\n".join(messages)):
        out.append(
            {
                "route": "user_fact",
                "content": fact[:120],
                "confidence": 0.55,
                "reason": "aux_unavailable_rule_fallback",
            }
        )
    return out[:5]


def should_auto_write(route: str, confidence: float, *, min_confidence: float) -> bool:
    return route in _AUTO_OWNERS and float(confidence) >= float(min_confidence)
