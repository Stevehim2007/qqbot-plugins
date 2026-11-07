"""Manifest-driven tool descriptors for the safe-tools marker protocol.

Each provider plugin can ship a ``tools.json`` next to its ``main.py``.  The
safe-tools plugin aggregates those files at runtime, so adding a marker tool no
longer requires editing ``qqbot_safe_tools/main.py``.  The built-in descriptors
are retained as a fail-safe fallback for old installations and unit tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_PLUGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean"})


class ToolRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ToolParameter:
    type: str = "string"
    required: bool = False
    max_chars: int | None = None
    min: float | None = None
    max: float | None = None
    enum: tuple[str, ...] = ()
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "required": self.required}
        if self.max_chars is not None:
            payload["max_chars"] = self.max_chars
        if self.min is not None:
            payload["min"] = self.min
        if self.max is not None:
            payload["max"] = self.max
        if self.enum:
            payload["enum"] = list(self.enum)
        if self.default is not None:
            payload["default"] = self.default
        return payload


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    plugin_id: str
    method: str
    description: str
    parameters: Mapping[str, ToolParameter]
    allow_key: str
    cooldown_key: str
    cooldown_default: int
    operation: str = ""
    call_style: str = "kwargs"
    write: bool = False
    requires_confirmation: bool = False
    marker_example: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "plugin_id": self.plugin_id,
            "method": self.method,
            "description": self.description,
            "parameters": {
                key: value.to_dict() for key, value in self.parameters.items()
            },
            "allow_key": self.allow_key,
            "cooldown_key": self.cooldown_key,
            "cooldown_default": self.cooldown_default,
            "operation": self.operation,
            "call_style": self.call_style,
            "write": self.write,
            "requires_confirmation": self.requires_confirmation,
            "marker_example": self.marker_example,
            "constraints": dict(self.constraints),
        }


def _text(value: Any, label: str, *, limit: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ToolRegistryError(f"{label} cannot be empty")
    if len(text) > limit:
        raise ToolRegistryError(f"{label} is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ToolRegistryError(f"{label} contains control characters")
    return text


def _integer(value: Any, label: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolRegistryError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ToolRegistryError(f"{label} must be an integer") from exc
    if number < minimum or number > maximum:
        raise ToolRegistryError(f"{label} is out of range")
    return number


def _parameter(name: str, raw: Any) -> ToolParameter:
    if not _NAME_RE.fullmatch(name):
        raise ToolRegistryError("invalid parameter name")
    if not isinstance(raw, Mapping):
        raise ToolRegistryError("parameter must be an object")
    ptype = str(raw.get("type") or "string").strip().lower()
    if ptype not in _PARAM_TYPES:
        raise ToolRegistryError(f"unsupported parameter type: {ptype}")
    max_chars = raw.get("max_chars")
    if max_chars is not None:
        max_chars = _integer(max_chars, "max_chars", default=0, minimum=1, maximum=100_000)
    minimum = raw.get("min")
    maximum = raw.get("max")
    if minimum is not None:
        minimum = float(minimum)
    if maximum is not None:
        maximum = float(maximum)
    enum_raw = raw.get("enum") or ()
    if not isinstance(enum_raw, (list, tuple)):
        raise ToolRegistryError("enum must be a list")
    enum = tuple(_text(item, "enum", limit=200, required=True) for item in enum_raw[:64])
    return ToolParameter(
        type=ptype,
        required=bool(raw.get("required", False)),
        max_chars=max_chars,
        min=minimum,
        max=maximum,
        enum=enum,
        default=raw.get("default"),
    )


def parse_tools_document(raw: Any, *, source: str = "") -> tuple[ToolDescriptor, ...]:
    if not isinstance(raw, Mapping):
        raise ToolRegistryError("tools.json must be an object")
    if int(raw.get("schema_version") or 0) != _SCHEMA_VERSION:
        raise ToolRegistryError("unsupported tools.json schema_version")
    plugin_id = _text(raw.get("plugin_id"), "plugin_id", limit=128, required=True)
    if not _PLUGIN_RE.fullmatch(plugin_id):
        raise ToolRegistryError("invalid plugin_id")
    tools = raw.get("tools")
    if not isinstance(tools, list):
        raise ToolRegistryError("tools must be a list")

    result: list[ToolDescriptor] = []
    seen: set[str] = set()
    for item in tools[:128]:
        if not isinstance(item, Mapping):
            raise ToolRegistryError("tool must be an object")
        name = _text(item.get("name"), "tool.name", limit=64, required=True)
        if not _NAME_RE.fullmatch(name):
            raise ToolRegistryError(f"invalid tool name: {name}")
        if name in seen:
            raise ToolRegistryError(f"duplicate tool name: {name}")
        seen.add(name)
        method = _text(item.get("method"), "tool.method", limit=128, required=True)
        if not _METHOD_RE.fullmatch(method):
            raise ToolRegistryError(f"invalid tool method: {method}")
        operation = _text(item.get("operation"), "tool.operation", limit=128)
        if operation and not _OPERATION_RE.fullmatch(operation):
            raise ToolRegistryError(f"invalid tool operation: {operation}")
        params_raw = item.get("parameters") or {}
        if not isinstance(params_raw, Mapping):
            raise ToolRegistryError("tool.parameters must be an object")
        parameters = {
            str(key): _parameter(str(key), value)
            for key, value in list(params_raw.items())[:64]
        }
        constraints_raw = item.get("constraints") or {}
        if not isinstance(constraints_raw, Mapping):
            raise ToolRegistryError("tool.constraints must be an object")
        constraints: dict[str, Any] = {}
        exactly_one = constraints_raw.get("exactly_one") or ()
        if exactly_one:
            if not isinstance(exactly_one, (list, tuple)):
                raise ToolRegistryError("constraints.exactly_one must be a list")
            names = tuple(str(value) for value in exactly_one[:16])
            if any(name not in parameters for name in names):
                raise ToolRegistryError("constraints.exactly_one references unknown parameter")
            constraints["exactly_one"] = list(names)
        result.append(
            ToolDescriptor(
                name=name,
                plugin_id=plugin_id,
                method=method,
                description=_text(item.get("description"), "tool.description", limit=500),
                parameters=parameters,
                allow_key=_text(item.get("allow_key"), "tool.allow_key", limit=128, required=True),
                cooldown_key=_text(item.get("cooldown_key"), "tool.cooldown_key", limit=128, required=True),
                cooldown_default=_integer(
                    item.get("cooldown_default"), "cooldown_default",
                    default=0, minimum=0, maximum=86_400,
                ),
                operation=operation,
                call_style=_text(item.get("call_style") or "kwargs", "call_style", limit=32),
                write=bool(item.get("write", False)),
                requires_confirmation=bool(item.get("requires_confirmation", False)),
                marker_example=_text(item.get("marker_example"), "tool.marker_example", limit=500),
                constraints=constraints,
            )
        )
    return tuple(result)


def load_tools_file(path: str | Path) -> tuple[ToolDescriptor, ...]:
    source = str(path)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ToolRegistryError(f"cannot read {source}") from exc
    return parse_tools_document(raw, source=source)


def discover_from_stars(stars: Iterable[Any]) -> tuple[tuple[ToolDescriptor, ...], tuple[str, ...]]:
    """Read ``tools.json`` from each loaded star's plugin directory.

    Returns descriptors plus human-readable load errors.  A malformed plugin
    manifest is ignored instead of taking down safe-tools or other providers.
    """

    found: dict[str, ToolDescriptor] = {}
    errors: list[str] = []
    for metadata in stars:
        module = (
            metadata.get("module")
            if isinstance(metadata, Mapping)
            else getattr(metadata, "module", None)
        )
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        path = Path(str(module_file)).resolve().parent / "tools.json"
        if not path.is_file():
            continue
        try:
            descriptors = load_tools_file(path)
        except ToolRegistryError as exc:
            errors.append(f"{path.parent.name}: {exc}")
            continue
        for descriptor in descriptors:
            if descriptor.name in found:
                errors.append(f"{path.parent.name}: duplicate tool {descriptor.name}")
                continue
            found[descriptor.name] = descriptor
    return tuple(found.values()), tuple(errors)


def builtin_descriptors() -> tuple[ToolDescriptor, ...]:
    """The seven v1 descriptors, used when no provider ships tools.json."""

    def p(**kwargs: Any) -> ToolParameter:
        return ToolParameter(**kwargs)

    return (
        ToolDescriptor(
            "fetch_webpage", "qqbot_web_reader", "tool_fetch_webpage",
            "读取公开网页正文；只允许公开 HTTP(S)，不登录、不带 Cookie。",
            {"url": p(required=True, max_chars=2048), "max_chars": p(type="integer", max_chars=None, min=1, max=8000, default=4000)},
            "allow_fetch_webpage", "web_fetch_cooldown_seconds", 30,
            "web.read", "kwargs",
            marker_example='[[tool:fetch_webpage url="https://example.com/article" max_chars=3000]]',
        ),
        ToolDescriptor(
            "search_images", "qqbot_image_search", "tool_search_images",
            "按关键词搜索图片；禁止路径/命令。",
            {"keyword": p(required=True, max_chars=100), "count": p(type="integer", min=1, max=5, default=1)},
            "allow_search_images", "image_search_cooldown_seconds", 10,
            "image.search", "kwargs",
            marker_example='[[tool:search_images keyword="关键词" count=1]]',
        ),
        ToolDescriptor(
            "send_sticker", "qqbot_stickers", "tool_send_sticker",
            "发送一张表情；category/emotion/tag 三选一。",
            {"category": p(max_chars=64), "emotion": p(max_chars=64), "tag": p(max_chars=64)},
            "allow_send_sticker", "sticker_cooldown_seconds", 20,
            "sticker.send_plan", "kwargs",
            marker_example='[[tool:send_sticker category="分类"]]',
            constraints={"exactly_one": ["category", "emotion", "tag"]},
        ),
        ToolDescriptor(
            "collect_sticker", "qqbot_stickers", "tool_collect_sticker",
            "收藏当前消息中的表情。",
            {"category": p(max_chars=32)},
            "allow_collect_sticker", "collect_sticker_cooldown_seconds", 30,
            "sticker.collect_plan", "kwargs",
            marker_example='[[tool:collect_sticker category="分类"]]',
        ),
        ToolDescriptor(
            "list_sticker_categories", "qqbot_stickers", "tool_list_categories",
            "列出可用表情分类。",
            {}, "allow_list_categories", "list_categories_cooldown_seconds", 5,
            "category.list", "kwargs",
            marker_example="[[tool:list_sticker_categories()]]",
        ),
        ToolDescriptor(
            "search_food_restaurants", "qqbot_food", "tool_search_restaurants",
            "查询美食店铺；name/tag/location 至少给一个。",
            {"name": p(max_chars=50), "tag": p(max_chars=120), "location": p(max_chars=30), "sort": p(max_chars=12), "order": p(max_chars=5), "limit": p(type="integer", min=1, max=5, default=5)},
            "allow_search_food_restaurants", "food_search_cooldown_seconds", 10,
            "food.query", "food_positional",
            marker_example='[[tool:search_food_restaurants name="店名" tag="火锅" location="南关区" limit=5]]',
        ),
        ToolDescriptor(
            "submit_food_review", "qqbot_food", "tool_submit_food_review",
            "仅在用户明确要求提交到 Food 时使用；缺项留空，不编造。",
            {"restaurant": p(max_chars=200), "location": p(max_chars=60), "taste": p(max_chars=8), "value": p(max_chars=8), "comment": p(max_chars=500), "tags": p(max_chars=120)},
            "allow_submit_food_review", "food_submit_cooldown_seconds", 60,
            "food.draft_create", "food_positional", write=True, requires_confirmation=True,
            marker_example='[[tool:submit_food_review restaurant="店名" location="位置" taste="4.5" value="3.5" comment="评价" tags="火锅,夜宵"]]',
        ),
    )


class ToolRegistry:
    def __init__(self, descriptors: Iterable[ToolDescriptor] = ()) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}
        self.errors: tuple[str, ...] = ()
        for descriptor in descriptors:
            self._descriptors.setdefault(descriptor.name, descriptor)

    @classmethod
    def from_stars(cls, stars: Iterable[Any]) -> "ToolRegistry":
        descriptors, errors = discover_from_stars(stars)
        # Merge over the built-in table: partial rollouts still expose all v1
        # tools, while a provider manifest can override/update its descriptor.
        registry = cls(builtin_descriptors())
        for descriptor in descriptors:
            registry._descriptors[descriptor.name] = descriptor
        registry.errors = errors
        return registry

    def get(self, name: str) -> ToolDescriptor | None:
        return self._descriptors.get(str(name or ""))

    def names(self) -> tuple[str, ...]:
        return tuple(self._descriptors)

    def all(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._descriptors.values())

    def specs(self) -> dict[str, dict[str, Any]]:
        return {descriptor.name: descriptor.spec() for descriptor in self._descriptors.values()}
