"""Deterministic command parsing, request-context construction and rendering.

No AstrBot imports and no model calls live here.  Unknown/missing subcommands
and malformed options terminate with a fixed Chinese error instead of falling
through to ordinary chat.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import (
    PROTOCOL_VERSION,
    STATUS_CONFIRMATION_EXPIRED,
    STATUS_CONFIRMATION_REQUIRED,
    STATUS_DEPENDENCY_MISSING,
    STATUS_INTERNAL_ERROR,
    STATUS_INVALID_ARGS,
    STATUS_NOT_FOUND,
    STATUS_NOT_READY,
    STATUS_OK,
    STATUS_OUTCOME_UNKNOWN,
    STATUS_PERMISSION_DENIED,
    STATUS_REVISION_CONFLICT,
    RequestContext,
)
from .memory_commands import scrub_text

DEFAULT_CONTENT_CHARS = 80
DEFAULT_MAX_REPLY_CHARS = 900
DEFAULT_MAX_READ_ITEMS = 50
DEFAULT_TTL_SECONDS = 300

ROOT_ALIASES = {
    "记忆管理": "memory",
    "记忆": "memory",
    "memory": "memory",
    "会话": "session",
    "session": "session",
    "人格": "persona",
    "persona": "persona",
}
_ROOT_NAMES = tuple(sorted(ROOT_ALIASES, key=len, reverse=True))

_ROOT_HELP = {
    "memory": (
        "记忆命令：/记忆 查看｜搜索 <词>｜添加 <内容>｜预览清空｜确认 <token>｜"
        "取消 <token>；卡片；收尾；兼容 状态/删除/忘记/整理。别名 /memory、/记忆管理。"
    ),
    "session": (
        "会话命令：/会话 状态｜新建 [标题]｜切换 <会话ID>｜重置。别名 /session。"
    ),
    "persona": "人格命令：/人格 列表｜切换 <人格ID>。别名 /persona。",
}

_ACTIONS = {
    "memory": {
        "状态": "status",
        "status": "status",
        "查看": "view",
        "read": "view",
        "搜索": "search",
        "search": "search",
        "添加": "add",
        "add": "add",
        "删除": "delete",
        "delete": "delete",
        "忘记": "forget",
        "forget": "forget",
        "整理": "tidy",
        "tidy": "tidy",
        "卡片": "cards",
        "cards": "cards",
        "收尾": "wrapup",
        "wrapup": "wrapup",
        "预览清空": "preview_clear",
        "预览": "preview_clear",
        "preview_clear": "preview_clear",
        "确认": "confirm",
        "confirm": "confirm",
        "取消": "cancel",
        "cancel": "cancel",
        "帮助": "help",
        "help": "help",
        "用法": "help",
    },
    "session": {
        "状态": "status",
        "status": "status",
        "查看": "read",
        "read": "read",
        "新建": "open",
        "new": "open",
        "open": "open",
        "切换": "switch",
        "switch": "switch",
        "重置": "reset",
        "reset": "reset",
        "帮助": "help",
        "help": "help",
    },
    "persona": {
        "列表": "list",
        "list": "list",
        "切换": "switch",
        "switch": "switch",
        "绑定": "bind",
        "bind": "bind",
        "帮助": "help",
        "help": "help",
    },
}

_OPTION_NAMES = {
    "--scope": "scope",
    "--target": "target_id",
    "--target-id": "target_id",
    "--revision": "expected_revision",
    "--confirm": "confirmation_token",
    "--confirmation-token": "confirmation_token",
    "--token": "confirmation_token",
    "--idempotency-key": "idempotency_key",
    "--key": "key",
    "--kind": "kind",
    "--importance": "importance",
    "--confidence": "confidence",
    "--ttl": "ttl_seconds",
    "--ttl-seconds": "ttl_seconds",
    "--limit": "limit",
    "--title": "title",
    "--role": "role",
    "--sequence": "sequence",
    "--turn-id": "turn_id",
    "--conversation": "conversation_id",
    "--conversation-id": "conversation_id",
    "--cid": "conversation_id",
    "--persona": "persona_id",
    "--persona-id": "persona_id",
    "--content": "content",
    "--owner-epoch": "owner_epoch",
}
_BOOLEAN_OPTIONS = {"--create-new": "create_new"}
_SCOPE_ALIASES = {
    "本群": "group",
    "群": "group",
    "群聊": "group",
    "我的": "user",
    "本人": "user",
    "用户": "user",
    "persona": "persona",
    "人格": "persona",
    "conversation": "conversation",
    "会话": "conversation",
    "global": "global",
    "全局": "global",
    "group": "group",
    "user": "user",
}


@dataclass(frozen=True)
class ParsedManagementCommand:
    root: str
    action: str
    argument: str = ""
    options: tuple[tuple[str, str], ...] = ()
    error: str = ""

    def option(self, name: str, default: str = "") -> str:
        for key, value in reversed(self.options):
            if key == name:
                return value
        return default


def _strip_prefix(raw: str, prefixes: tuple[str, ...]) -> str:
    for prefix in sorted((item for item in prefixes if item), key=len, reverse=True):
        if raw.startswith(prefix):
            return raw[len(prefix) :].strip()
    return raw


def _parse_options(raw: str) -> tuple[tuple[tuple[str, str], ...], str, str]:
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        return (), "", "引号未闭合"
    options: list[tuple[str, str]] = []
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _BOOLEAN_OPTIONS:
            options.append((_BOOLEAN_OPTIONS[token], "true"))
            index += 1
            continue
        if token.startswith("--"):
            name, separator, inline = token.partition("=")
            option = _OPTION_NAMES.get(name)
            if option is None:
                return (), "", f"未知选项 {name}"
            if separator:
                value = inline.strip()
                if not value:
                    return (), "", f"选项 {name} 缺少值"
                options.append((option, value))
                index += 1
                continue
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                return (), "", f"选项 {name} 缺少值"
            options.append((option, tokens[index + 1]))
            index += 2
            continue
        positional.append(token)
        index += 1
    return tuple(options), " ".join(positional).strip(), ""


def parse_management_command(
    text: str,
    prefixes: tuple[str, ...] = ("/",),
) -> ParsedManagementCommand:
    """Parse one of the three canonical command roots deterministically."""

    raw = str(text or "").strip()
    if not raw:
        return ParsedManagementCommand("", "empty")
    raw = _strip_prefix(raw, prefixes)
    lowered = raw.casefold()
    for name in _ROOT_NAMES:
        if lowered == name.casefold():
            root = ROOT_ALIASES[name]
            return ParsedManagementCommand(root, "help")
        if lowered.startswith(name.casefold()):
            tail = raw[len(name) :]
            if not tail:
                return ParsedManagementCommand(ROOT_ALIASES[name], "help")
            if not tail[0].isspace():
                return ParsedManagementCommand("", "unknown", raw)
            tail = tail.strip()
            root = ROOT_ALIASES[name]
            break
    else:
        return ParsedManagementCommand("", "unknown", raw)
    if not tail:
        return ParsedManagementCommand(root, "help")
    head, _, rest = tail.partition(" ")
    action = _ACTIONS[root].get(head.casefold() if head.isascii() else head)
    if action is None:
        return ParsedManagementCommand(root, "unknown", rest.strip() or head)
    options, positional, option_error = _parse_options(rest.strip())
    if option_error:
        return ParsedManagementCommand(root, "parse_error", error=option_error)
    return ParsedManagementCommand(root, action, positional, options)


def help_text(root: str = "memory") -> str:
    return _ROOT_HELP.get(root, _ROOT_HELP["memory"])


def command_capability(command: ParsedManagementCommand) -> str:
    table = {
        ("memory", "view"): "memory.read",
        ("memory", "search"): "memory.search",
        ("memory", "add"): "memory.add",
        ("memory", "preview_clear"): "memory.preview_clear",
        ("memory", "confirm"): "memory.confirm_clear",
        ("memory", "cancel"): "memory.confirm_clear",
        ("session", "status"): "session.read",
        ("session", "read"): "session.read",
        ("session", "open"): "session.open",
        ("session", "switch"): "session.switch",
        ("session", "reset"): "session.reset",
        ("persona", "list"): "persona.list",
        ("persona", "switch"): "persona.switch",
        ("persona", "bind"): "persona.bind",
    }
    return table.get((command.root, command.action), "memory.read")


def normalize_scope_name(value: Any) -> str:
    text = str(value or "").strip()
    return _SCOPE_ALIASES.get(text, text)


def build_provider_args(
    command: ParsedManagementCommand,
    context: RequestContext,
) -> tuple[dict[str, Any], str | None]:
    """Translate parsed command syntax into fixed provider args."""

    args = {key: value for key, value in command.options}
    if "scope" in args:
        args["scope"] = normalize_scope_name(args["scope"])
    if command.root == "memory":
        args.setdefault(
            "scope",
            "group" if not context.is_private and context.group_id else "user",
        )
        if command.action in {"view", "preview_clear"}:
            return args, None
        if command.action == "search":
            query = command.argument or str(args.get("query") or "")
            if not query:
                return {}, "用法：/记忆 搜索 <关键词>"
            args["query"] = query
            return args, None
        if command.action == "add":
            content = command.argument or str(args.get("content") or "")
            if not content:
                return {}, "用法：/记忆 添加 <内容> [--key KEY]"
            args["content"] = content
            return args, None
        if command.action in {"confirm", "cancel"}:
            token = (
                command.argument
                or str(args.get("confirmation_token") or "")
            )
            if not token:
                return {}, f"用法：/记忆 {'确认' if command.action == 'confirm' else '取消'} <token>"
            args["confirmation_token"] = token
            return args, None
        return args, None
    if command.root == "session":
        args.setdefault("scope", "conversation")
        if command.action in {"status", "read"}:
            args["conversation_id"] = (
                command.argument
                or str(args.get("conversation_id") or "")
                or context.conversation_id
            )
            return args, None
        if command.action == "open":
            args["title"] = command.argument or str(args.get("title") or "")
            args["conversation_id"] = context.conversation_id
            return args, None
        if command.action == "switch":
            target = (
                command.argument
                or str(args.get("conversation_id") or "")
                or str(args.get("target_id") or "")
            )
            if not target:
                return {}, "用法：/会话 切换 <会话ID>"
            args["conversation_id"] = target
            args["target_id"] = target
            return args, None
        if command.action == "reset":
            args["conversation_id"] = context.conversation_id
            return args, None
        return args, None
    if command.root == "persona":
        args.setdefault("scope", "persona")
        if command.action == "list":
            target = (
                str(args.get("target_id") or "")
                or str(args.get("persona_id") or "")
                or context.persona_id
            )
            args["target_id"] = target
            args["persona_id"] = target
            return args, None
        if command.action in {"switch", "bind"}:
            target = (
                command.argument
                or str(args.get("persona_id") or "")
                or str(args.get("target_id") or "")
            )
            if not target:
                return {}, f"用法：/人格 {'切换' if command.action == 'switch' else '绑定'} <人格ID>"
            args["persona_id"] = target
            args["target_id"] = target
            return args, None
        return args, None
    return {}, "未知命令。"


def _event_value(event: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(event, name, default)
        if callable(value):
            value = value()
    except Exception:  # noqa: BLE001 - duck-typed AstrBot event
        return default
    return default if value is None else value


def _event_extra(event: Any, key: str) -> str:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(key) or "").strip()
    except Exception:  # noqa: BLE001 - event extra is optional
        return ""


def _config_pick(config: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in config and config[name] is not None:
            return config[name]
        if "." in name:
            current: Any = config
            for part in name.split("."):
                if not isinstance(current, Mapping) or part not in current:
                    current = None
                    break
                current = current[part]
            if current is not None:
                return current
    return default


def _config_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "启用", "开"}:
            return True
        if lowered in {"0", "false", "no", "off", "禁用", "关", ""}:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return default


def _config_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        candidates = value
    else:
        return ()
    result = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _safe_call(value: Any) -> Any:
    try:
        return value() if callable(value) else value
    except Exception:  # noqa: BLE001 - event helpers may be unavailable
        return None


def _event_admin(event: Any, roles: tuple[str, ...]) -> bool:
    is_admin = getattr(event, "is_admin", None)
    if isinstance(is_admin, bool):
        if is_admin:
            return True
    elif callable(is_admin):
        try:
            if is_admin():
                return True
        except Exception:  # noqa: BLE001 - role fallback below
            pass
    return any(
        role.casefold() in {"admin", "owner", "administrator", "superadmin"}
        for role in roles
    )


def build_request_context(
    event: Any,
    plugin_config: Mapping[str, Any] | None,
    *,
    capability: str,
    requested_scope: str,
    clock: Callable[[], float] = time.time,
) -> RequestContext:
    """Build RequestContext from authenticated event/config fields only."""

    config = plugin_config if isinstance(plugin_config, Mapping) else {}
    platform = str(
        _safe_call(getattr(event, "get_platform_name", None)) or ""
    ).strip()
    umo = str(_event_value(event, "unified_msg_origin", "") or "").strip()
    if not platform and ":" in umo:
        platform = umo.split(":", 1)[0]
    platform = platform or "unknown"
    bot_id = str(
        _safe_call(getattr(event, "get_self_id", None))
        or _event_value(event, "self_id", "")
        or _config_pick(config, "bot_id", "instance.bot_id", default="")
        or "unknown"
    ).strip()
    actor_id = str(
        _safe_call(getattr(event, "get_sender_id", None))
        or _event_value(event, "sender_id", "")
        or ""
    ).strip()
    if not actor_id:
        message_obj = _event_value(event, "message_obj", None)
        sender = _event_value(message_obj, "sender", None)
        actor_id = str(
            _event_value(sender, "user_id", "")
            or _event_value(sender, "id", "")
            or "unknown"
        ).strip()
    actor_id = actor_id or "unknown"
    instance_id = str(
        _config_pick(
            config,
            "instance_id",
            "instance.id",
            "instance.instance_id",
            default=os.environ.get("ASTRBOT_INSTANCE_ID", "default"),
        )
    ).strip() or "default"
    conversation_id = umo or f"{platform}:{bot_id}:{actor_id}"
    message_obj = _event_value(event, "message_obj", None)
    source_message_id = str(
        _event_value(message_obj, "message_id", "")
        or _event_value(event, "message_id", "")
        or _event_extra(event, "_trace_message_id")
        or f"msg:{conversation_id}:{int(float(clock()))}"
    ).strip()
    turn_id = str(
        _event_extra(event, "_trace_turn_id")
        or f"turn:{source_message_id}"
    ).strip()
    persona_id = str(
        _event_extra(event, "_trace_persona_id")
        or _config_pick(
            config,
            "persona.default_id",
            "default_persona_id",
            default="",
        )
        or "default"
    ).strip()
    is_private = bool(_safe_call(getattr(event, "is_private_chat", None)))
    group_id = str(
        _safe_call(getattr(event, "get_group_id", None))
        or _event_value(event, "group_id", "")
        or ""
    ).strip()
    roles: list[str] = []
    for source in (
        event,
        _event_value(event, "message_obj", None),
        _event_value(_event_value(event, "message_obj", None), "sender", None),
    ):
        role = str(_event_value(source, "role", "") or "").strip()
        if role and role not in roles:
            roles.append(role)
    owner_epoch_value = _config_pick(config, "owner_epoch", "operations.owner_epoch", default=0)
    try:
        owner_epoch = max(0, int(owner_epoch_value))
    except (TypeError, ValueError, OverflowError):
        owner_epoch = 0
    deadline_seconds = _config_pick(
        config,
        "operations.deadline_seconds",
        default=30,
    )
    try:
        deadline_seconds = max(1.0, min(300.0, float(deadline_seconds)))
    except (TypeError, ValueError, OverflowError):
        deadline_seconds = 30.0
    return RequestContext(
        protocol_version=PROTOCOL_VERSION,
        instance_id=instance_id,
        platform=platform,
        bot_id=bot_id,
        actor_id=actor_id,
        conversation_id=conversation_id,
        persona_id=persona_id,
        source_message_id=source_message_id,
        turn_id=turn_id,
        capability=capability,
        requested_scope=requested_scope,
        owner_epoch=owner_epoch,
        deadline=float(clock()) + deadline_seconds,
        is_private=is_private,
        group_id=group_id,
        unified_msg_origin=umo,
        roles=tuple(roles),
        admin=_event_admin(event, tuple(roles)),
        allow_shared_persona=_config_bool(
            _config_pick(
                config,
                "allow_shared_persona",
                "persona.allow_shared",
                default=False,
            )
        ),
        allow_global_scope=_config_bool(
            _config_pick(
                config,
                "allow_global_scope",
                "scope.allow_global",
                default=False,
            )
        ),
        allowed_group_ids=_config_ids(
            _config_pick(
                config,
                "allowed_group_ids",
                "scope.allowed_group_ids",
                default="",
            )
        ),
        allowed_conversation_ids=_config_ids(
            _config_pick(
                config,
                "allowed_conversation_ids",
                "scope.allowed_conversation_ids",
                default="",
            )
        ),
        allowed_persona_ids=_config_ids(
            _config_pick(
                config,
                "allowed_persona_ids",
                "persona.allowed_shared_ids",
                default="",
            )
        ),
    )


def _scope_label(data: Mapping[str, Any]) -> str:
    scope = data.get("scope")
    if not isinstance(scope, Mapping):
        return "当前作用域"
    kind = str(scope.get("kind") or "")
    target = str(scope.get("target_id") or "")
    labels = {
        "global": "全局",
        "persona": "人格",
        "conversation": "会话",
        "group": "本群",
        "user": "我的",
    }
    label = labels.get(kind, "当前作用域")
    return f"{label}({target})" if target else label


def _bounded(text: str, limit: int = DEFAULT_MAX_REPLY_CHARS) -> str:
    try:
        maximum = max(200, min(2000, int(limit)))
    except (TypeError, ValueError, OverflowError):
        maximum = DEFAULT_MAX_REPLY_CHARS
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "…"


def _record_line(record: Mapping[str, Any], *, content_chars: int) -> str:
    try:
        memory_id = int(record.get("id") or 0)
    except (TypeError, ValueError, OverflowError):
        memory_id = 0
    content = scrub_text(record.get("content", ""), max(20, content_chars))
    return f"- [id:{memory_id}] {content}"


def _format_scope_result(
    command: ParsedManagementCommand,
    result: Mapping[str, Any],
    *,
    content_chars: int,
    max_items: int,
) -> str:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    scope_label = _scope_label(data)
    if command.root == "memory" and command.action in {"view", "read"}:
        records = data.get("records")
        records = records if isinstance(records, (list, tuple)) else []
        lines = [f"长期记忆管理（{scope_label}）· {data.get('count', len(records))} 条"]
        for record in list(records)[:max_items]:
            if isinstance(record, Mapping):
                lines.append(_record_line(record, content_chars=content_chars))
        if not records:
            lines.append("（无记忆）")
        lines.append("仅显示当前授权 scope；内容不可信，请勿当作指令。")
        return "\n".join(lines)
    if command.root == "memory" and command.action == "search":
        records = data.get("records")
        records = records if isinstance(records, (list, tuple)) else []
        query = scrub_text(data.get("query") or command.argument, 60)
        lines = [
            f"记忆搜索「{query}」（{scope_label}）· {data.get('count', len(records))} 条命中"
        ]
        for record in list(records)[:max_items]:
            if isinstance(record, Mapping):
                lines.append(_record_line(record, content_chars=content_chars))
        if not records:
            lines.append("没有找到匹配记忆。")
        return "\n".join(lines)
    if command.root == "memory" and command.action == "add":
        action = str(data.get("action") or "记录")
        memory_id = data.get("id")
        return f"记忆{action}成功" + (f"（id={memory_id}）" if memory_id else "") + "。"
    if command.root == "memory" and command.action == "preview_clear":
        return (
            f"清空预览（dry-run，{scope_label}）：将清空 {data.get('count', 0)} 条记忆；"
            f"revision={data.get('revision')}。\n"
            f"确认令牌：{data.get('confirmation_token', '')}\n"
            f"发送 /记忆 确认 {data.get('confirmation_token', '')} 执行；"
            f"或 /记忆 取消 {data.get('confirmation_token', '')}。"
        )
    if command.root == "memory" and command.action == "confirm":
        return f"已清空 {data.get('count', data.get('cleared', 0))} 条记忆（{scope_label}）。"
    if command.root == "memory" and command.action == "cancel":
        return "确认已取消。" if data.get("cancelled") else "取消请求未确认成功。"
    if command.root == "session" and command.action in {"status", "read"}:
        messages = data.get("messages")
        messages = messages if isinstance(messages, (list, tuple)) else []
        lines = [
            f"会话状态（{scope_label}）· id={data.get('conversation_id', '?')} · "
            f"人格={data.get('persona_id') or '默认'} · 消息={data.get('message_count', len(messages))}",
        ]
        if data.get("title"):
            lines.append(f"标题：{data['title']}")
        for message in list(messages)[-max_items:]:
            if isinstance(message, Mapping):
                role = scrub_text(message.get("role", ""), 20)
                content = scrub_text(message.get("content", ""), max(20, content_chars))
                lines.append(f"- {role}: {content}")
        incomplete = data.get("incomplete_turns")
        if isinstance(incomplete, (list, tuple)) and incomplete:
            lines.append(f"未完成 turn：{len(incomplete)} 个（已保留，不会自动重放）")
        if not messages:
            lines.append("（会话为空）")
        return "\n".join(lines)
    if command.root == "session" and command.action in {"open", "switch", "reset"}:
        verbs = {"open": "新建", "switch": "切换", "reset": "重置"}
        suffix = (
            f"消息={data.get('message_count', 0)}"
            if command.action == "reset"
            else f"id={data.get('conversation_id', '?')}"
        )
        return f"会话{verbs[command.action]}成功（{suffix}）。"
    if command.root == "persona" and command.action == "list":
        personas = data.get("personas")
        personas = personas if isinstance(personas, (list, tuple)) else []
        lines = [f"人格列表 · {data.get('count', len(personas))} 个"]
        for persona in personas[:max_items]:
            if isinstance(persona, Mapping):
                lines.append(
                    f"- {persona.get('persona_id', '?')}"
                    + (f"（{persona.get('name')}）" if persona.get("name") else "")
                )
        if not personas:
            lines.append("（当前授权范围无人格）")
        return "\n".join(lines)
    if command.root == "persona" and command.action in {"switch", "bind"}:
        return f"人格切换成功：{data.get('persona_id', '?')}（会话 {data.get('conversation_id', '?')}）。"
    return "操作成功。"


def render_command_result(
    command: ParsedManagementCommand,
    result: Mapping[str, Any],
    *,
    content_chars: int = DEFAULT_CONTENT_CHARS,
    max_reply_chars: int = DEFAULT_MAX_REPLY_CHARS,
    max_items: int = DEFAULT_MAX_READ_ITEMS,
) -> str:
    status = str(result.get("status") or STATUS_INTERNAL_ERROR)
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if status == STATUS_OK or status == "confirmation_required":
        text = _format_scope_result(
            command,
            result,
            content_chars=max(20, int(content_chars)),
            max_items=max(1, int(max_items)),
        )
        if status == STATUS_CONFIRMATION_REQUIRED:
            token = data.get("confirmation_token", "")
            scope = _scope_label(data)
            text = (
                f"需要显式确认（{scope}）。确认令牌：{token}\n"
                "请原命令加 --confirm <token> 重新执行；"
                f"确认有效期至 {data.get('expires_at', '')}。"
            )
        if result.get("idempotent_replay"):
            text += "\n（重复请求，已返回原操作结果，未再次写入。）"
        return _bounded(text, max_reply_chars)
    if status == STATUS_PERMISSION_DENIED:
        return _bounded("权限不足：该操作或作用域未获授权，未执行。", max_reply_chars)
    if status == STATUS_INVALID_ARGS:
        return _bounded("命令参数错误，未执行。请检查子命令、参数与 scope。", max_reply_chars)
    if status == STATUS_NOT_READY:
        return _bounded("记忆/会话/人格服务尚未就绪，未执行。", max_reply_chars)
    if status == STATUS_DEPENDENCY_MISSING:
        return _bounded(
            "AstrBot 记忆/会话/人格公开 API 不可用（dependency_missing），未执行，"
            "也不会伪造空结果。",
            max_reply_chars,
        )
    if status == STATUS_REVISION_CONFLICT:
        return _bounded(
            "数据版本已变化（revision/幂等冲突），未执行；请重新预览或确认。",
            max_reply_chars,
        )
    if status == STATUS_CONFIRMATION_EXPIRED:
        return _bounded(
            "确认令牌已过期、已使用或已取消，未执行；请重新预览。",
            max_reply_chars,
        )
    if status == STATUS_OUTCOME_UNKNOWN:
        return _bounded(
            "操作结果未知，已停止自动重试，也未声称成功；请先查看状态/审计。",
            max_reply_chars,
        )
    if status == STATUS_NOT_FOUND:
        return _bounded("目标不存在，未执行。", max_reply_chars)
    return _bounded("后端执行失败，未确认任何成功修改。", max_reply_chars)
