"""Deterministic command and alias exclusion.

No AstrBot imports.  Every actual command/alias is registered here so the
chat path can exclude it before probability, cooldown, memory or model entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

# Canonical operation id -> registered aliases.  Longest match wins.
COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "reply": ("回复", "reply"),
    "cooldown": ("冷却", "cooldown"),
    "session.list": ("会话列表",),
    "session.switch": ("切换会话", "会话"),
    "session.new": ("新会话",),
    "session.delete": ("删除会话", "会话删除"),
    "option": ("option", "插件用法", "用法"),
    "song": ("唱歌", "点歌"),
}

ALL_ALIASES: tuple[str, ...] = tuple(
    alias for aliases in COMMAND_ALIASES.values() for alias in aliases
)


@dataclass
class CommandMatch:
    name: str
    operation: str
    arguments: str
    prefixed: bool
    known: bool


class CommandParseError(ValueError):
    """Terminal Chinese error text for a command or subcommand."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = str(message)


@dataclass
class ReplySubcommand:
    action: str
    arguments: str


def _sorted_aliases(extra: Iterable[str] | None = None) -> list[str]:
    values = set(ALL_ALIASES)
    for item in extra or ():
        text = str(item).strip()
        if text:
            values.add(text)
    return sorted(values, key=len, reverse=True)


def command_names(extra_aliases: Iterable[str] | None = None) -> list[str]:
    return _sorted_aliases(extra_aliases)


def command_prefixes(
    configured: Iterable[str] | None,
    *,
    inherit_wake_prefix: bool,
    global_wake_prefix: Any = None,
) -> list[str]:
    prefixes: list[str] = [str(item) for item in (configured or []) if str(item)]
    if inherit_wake_prefix:
        if isinstance(global_wake_prefix, str):
            prefixes.append(global_wake_prefix)
        elif isinstance(global_wake_prefix, (list, tuple, set)):
            prefixes.extend(str(item) for item in global_wake_prefix if str(item))
    return sorted({prefix for prefix in prefixes if prefix}, key=len, reverse=True)


def strip_command_prefix(text: str, prefixes: Iterable[str]) -> tuple[str, bool]:
    value = str(text or "").lstrip()
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix and value.startswith(prefix):
            return value[len(prefix) :].lstrip(), True
    return value, False


def _match_alias(text: str, aliases: Iterable[str]) -> tuple[str, str] | None:
    for alias in sorted(aliases, key=len, reverse=True):
        if text == alias:
            return alias, ""
        if text.startswith(alias):
            rest = text[len(alias) :]
            if rest and rest[0].isspace():
                return alias, rest.strip()
    return None


def operation_for_alias(alias: str) -> str:
    for operation, aliases in COMMAND_ALIASES.items():
        if alias in aliases:
            return operation
    return ""


def parse_root_command(
    message_str: str,
    prefixes: Iterable[str],
    *,
    extra_aliases: Iterable[str] | None = None,
) -> CommandMatch | None:
    """Return a deterministic command match, or None for ordinary chat."""

    raw = str(message_str or "").strip()
    if not raw:
        return None
    text, prefixed = strip_command_prefix(raw, prefixes)
    if not text:
        return None
    aliases = command_names(extra_aliases)
    matched = _match_alias(text, aliases)
    if matched is not None:
        alias, arguments = matched
        return CommandMatch(
            name=alias,
            operation=operation_for_alias(alias),
            arguments=arguments,
            prefixed=prefixed,
            known=True,
        )
    if prefixed:
        # An unknown slash/wake-prefixed message is still terminal command-ish
        # input and must never become a model prompt.
        token = text.split(None, 1)[0]
        arguments = text[len(token) :].strip()
        return CommandMatch(
            name=token,
            operation="unknown",
            arguments=arguments,
            prefixed=True,
            known=False,
        )
    return None


def is_command_message(
    message_str: str,
    prefixes: Iterable[str],
    *,
    extra_aliases: Iterable[str] | None = None,
) -> bool:
    text = str(message_str or "").lstrip()
    if not text:
        return False
    if any(text.startswith(prefix) for prefix in prefixes if prefix):
        return True
    return parse_root_command(
        text,
        prefixes,
        extra_aliases=extra_aliases,
    ) is not None


def collect_argument(
    message_str: str,
    command_names_wanted: Iterable[str],
    prefixes: Iterable[str],
    *,
    extra_aliases: Iterable[str] | None = None,
) -> str:
    raw = str(message_str or "").strip()
    text, _ = strip_command_prefix(raw, prefixes)
    for name in sorted(set(command_names_wanted), key=len, reverse=True):
        if text == name:
            return ""
        if text.startswith(name):
            rest = text[len(name) :]
            if rest and rest[0].isspace():
                argument = re.sub(r"\s+", " ", rest).strip()
                return "".join(char for char in argument if char.isprintable()).strip()
    return ""


def parse_reply_subcommand(arguments: str) -> ReplySubcommand:
    text = str(arguments or "").strip()
    if not text:
        raise CommandParseError(
            "用法：/回复 状态|设置|启用|停用"
        )
    token, _, rest = text.partition(" ")
    action = {
        "状态": "status",
        "status": "status",
        "设置": "set",
        "set": "set",
        "启用": "enable",
        "enable": "enable",
        "停用": "disable",
        "disable": "disable",
    }.get(token.casefold())
    if action is None:
        raise CommandParseError(
            f"未知的回复子命令「{token}」。用法：/回复 状态|设置|启用|停用"
        )
    return ReplySubcommand(action=action, arguments=rest.strip())


def parse_cooldown_subcommand(arguments: str) -> str:
    text = str(arguments or "").strip()
    if not text:
        raise CommandParseError("用法：/冷却 状态")
    token = text.split(None, 1)[0]
    if token.casefold() in {"状态", "status"} and len(text.split()) == 1:
        return "status"
    raise CommandParseError(f"未知的冷却子命令「{token}」。用法：/冷却 状态")


def excluded_aliases(configured: Iterable[str] | None = None) -> list[str]:
    values = set(ALL_ALIASES)
    for item in configured or ():
        text = str(item).strip()
        if text:
            values.add(text)
    return sorted(values, key=len, reverse=True)


def terminal_unknown_command(command: str) -> str:
    return f"未知命令「{command}」。请使用 /option 查看可用命令。"
