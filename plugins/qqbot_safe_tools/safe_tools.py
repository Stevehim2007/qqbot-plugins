"""Deterministic, whitelisted tool-marker parsing and validation.

This module is intentionally boring: it performs no I/O, no model calls and no
dynamic Python execution.  The plugin's decorating hook gives it one candidate
reply at a time, then dispatches at most one validated intent to another
plugin's declared ``tool_*`` method.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

try:  # Loaded as a package by AstrBot.
    from . import food as food_engine
    from . import web_fetch as web_fetch_engine
except ImportError:  # Loaded directly by a test harness.
    import food as food_engine
    import web_fetch as web_fetch_engine

# A strict envelope: one tool name, one physical line of arguments, no nested
# square brackets.  Detection/treatment of malformed leftovers is handled
# separately so a broken marker is removed but never executed.
TOOL_MARKER_RE = re.compile(
    r"\[\[\s*tool\s*:\s*([a-z_]{1,64})([^\[\]\r\n]*)\]\]",
    re.IGNORECASE,
)
_LOOSE_MARKER_RE = re.compile(r"\[\[\s*tool\s*:", re.IGNORECASE)
_DANGLING_MARKER_RE = re.compile(
    r"\[\[\s*tool\s*:.*?(?:\]\]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_ARGUMENT_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]{0,63})\s*=\s*"
    r"(\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\s\"']+)"
)
# Backwards-compatible public name used by the legacy parser/tests.
ARG_RE = _ARGUMENT_RE
SAFE_CATEGORY_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]{1,32}$")
_MAX_MARKER_SOURCE_CHARS = 200_000
_MAX_ARG_BODY_CHARS = 4_000
_MAX_ARGS = 16

TOOL_ARG_KEYS: dict[str, frozenset[str]] = {
    "search_images": frozenset({"keyword", "count"}),
    "send_sticker": frozenset({"category", "emotion", "tag"}),
    "collect_sticker": frozenset({"category"}),
    "list_sticker_categories": frozenset(),
    "fetch_webpage": frozenset({"url", "max_chars"}),
    "submit_food_review": frozenset(
        {"restaurant", "location", "taste", "value", "comment", "tags"}
    ),
    "search_food_restaurants": frozenset(
        {"name", "tag", "location", "sort", "order", "limit"}
    ),
}
ALLOWED_TOOLS = tuple(TOOL_ARG_KEYS)

_FOOD_SORTS = frozenset(
    {"total", "taste", "value", "reviews", "name", "latest"}
)
_FOOD_ORDERS = frozenset({"asc", "desc"})
_UNSAFE_TOKENS = (
    "..",
    "/",
    "\\",
    "://",
    "file:",
    "http:",
    "https:",
    "www.",
    "javascript:",
    "command",
    "subprocess",
    "powershell",
    "cmd.exe",
)
_UNSAFE_COMMAND_CHARS = frozenset(";&|`$<>\x00")
_UNSAFE_COMMAND_WORDS = re.compile(
    r"(?:^|\s)(?:sudo|rm|curl|wget|nc|bash|sh|cmd|powershell|python|perl)(?:\s|$)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ToolIntent:
    name: str
    args: dict[str, str] = field(default_factory=dict)
    valid: bool = True
    error: str = ""


@dataclass(slots=True)
class ToolParseResult:
    clean_text: str
    intents: list[ToolIntent]
    had_marker: bool


def _invalid(intent: ToolIntent, error: str) -> ToolIntent:
    intent.valid = False
    intent.error = error
    return intent


def _decode_value(raw: str) -> str | None:
    """Decode one already-tokenized argument value, or return ``None``."""

    if raw.startswith('"'):
        if not raw.endswith('"') or len(raw) < 2:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, str) else None
    if raw.startswith("'"):
        if not raw.endswith("'") or len(raw) < 2:
            return None
        inner = raw[1:-1]
        out: list[str] = []
        escaped = False
        for char in inner:
            if escaped:
                if char not in ("'", "\\"):
                    return None
                out.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                out.append(char)
        if escaped:
            return None
        return "".join(out)
    return raw


def _parse_args(body: str) -> tuple[dict[str, str], str]:
    """Parse ``key=value`` pairs, rejecting duplicate/unknown/trailing junk.

    Values can be JSON-style double quoted strings, simple single quoted strings
    (with ``\\'``/``\\\\`` escapes), or whitespace-free bare tokens.  A whitespace
    separator is mandatory between pairs, which avoids ``a=1b=2`` ambiguity.
    """

    text = str(body or "")
    if len(text) > _MAX_ARG_BODY_CHARS:
        return {}, "参数过长"
    if re.fullmatch(r"\(\s*\)", text):
        # Empty parentheses are an optional readability convenience for
        # zero-argument tools; they are not a general call-expression parser.
        return {}, ""
    args: dict[str, str] = {}
    position = 0
    length = len(text)
    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        match = _ARGUMENT_RE.match(text, position)
        if match is None:
            return {}, "参数格式无效"
        key = match.group(1)
        if key in args:
            return {}, "参数重复"
        decoded = _decode_value(match.group(2))
        if decoded is None:
            return {}, "参数格式无效"
        args[key] = decoded
        if len(args) > _MAX_ARGS:
            return {}, "参数过多"
        position = match.end()
        if position < length and not text[position].isspace():
            return {}, "参数格式无效"
    return args, ""


def _contains_unsafe_reference(value: str) -> bool:
    """Conservatively reject filesystem/URL/command-looking free text."""

    lowered = value.casefold()
    if any(token in lowered for token in _UNSAFE_TOKENS):
        return True
    if any(char in _UNSAFE_COMMAND_CHARS for char in value):
        return True
    if _UNSAFE_COMMAND_WORDS.search(value):
        return True
    if re.search(r"(?i)(?:^|[^a-z0-9])[a-z][a-z0-9+.-]{1,15}:", value):
        return True
    return bool(re.search(r"[\x00-\x1f\x7f]", value))


def _has_control_chars(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _check_generic_value(
    intent: ToolIntent,
    key: str,
    *,
    max_chars: int,
    required: bool = False,
) -> str | None:
    value = str(intent.args.get(key, "") or "")
    if required and not value.strip():
        _invalid(intent, "缺少必填参数")
        return None
    if len(value) > max_chars:
        _invalid(intent, "字段过长")
        return None
    if _has_control_chars(value):
        _invalid(intent, "字段含控制字符")
        return None
    if value and _contains_unsafe_reference(value):
        _invalid(intent, "禁止路径/URL/命令参数")
        return None
    return value


def _parse_bounded_int(
    raw: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    value = str(raw or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,3}", value):
        return None
    number = int(value)
    if number < minimum or number > maximum:
        return None
    return number


def validate_intent(
    intent: ToolIntent,
    *,
    max_keyword_chars: int = 100,
    max_count: int = 5,
    max_web_chars: int = 4000,
) -> ToolIntent:
    """Validate one whitelisted intent in place and return it."""

    intent.name = str(intent.name or "").strip().lower()
    if not intent.valid:
        return intent
    if intent.name not in TOOL_ARG_KEYS:
        return _invalid(intent, "工具不在白名单")
    allowed = TOOL_ARG_KEYS[intent.name]
    if set(intent.args) - allowed:
        return _invalid(intent, "参数不在白名单")
    if len(intent.args) > len(allowed):
        return _invalid(intent, "参数过多")

    if intent.name == "search_images":
        keyword = _check_generic_value(
            intent, "keyword", max_chars=max(1, int(max_keyword_chars)), required=True
        )
        if keyword is None:
            return intent
        if not keyword.strip():
            return _invalid(intent, "关键词不能为空")
        count = _parse_bounded_int(
            intent.args.get("count", "1"), minimum=1, maximum=max(1, int(max_count))
        )
        if count is None:
            return _invalid(intent, "数量超出范围")
        return intent

    if intent.name == "send_sticker":
        values: list[str] = []
        for key in ("category", "emotion", "tag"):
            value = _check_generic_value(intent, key, max_chars=64)
            if value is None:
                return intent
            if value.strip():
                values.append(value.strip())
        if len(values) != 1:
            return _invalid(intent, "必须且只能指定 category/emotion/tag 之一")
        if not SAFE_CATEGORY_RE.fullmatch(values[0]):
            return _invalid(intent, "表情分类/情绪/标签非法")
        return intent

    if intent.name == "collect_sticker":
        category = _check_generic_value(intent, "category", max_chars=32)
        if category is None:
            return intent
        if category.strip() and not SAFE_CATEGORY_RE.fullmatch(category.strip()):
            return _invalid(intent, "收藏分类非法")
        return intent

    if intent.name == "list_sticker_categories":
        if intent.args:
            return _invalid(intent, "该工具不接受参数")
        return intent

    if intent.name == "fetch_webpage":
        url = str(intent.args.get("url", "") or "").strip()
        if not url:
            return _invalid(intent, "缺少网页地址")
        if len(url) > web_fetch_engine.WEB_FETCH_MAX_URL_CHARS:
            return _invalid(intent, "网页地址过长")
        if _has_control_chars(url):
            return _invalid(intent, "网页地址含控制字符")
        try:
            web_fetch_engine.validate_fetch_url(url)
        except web_fetch_engine.WebFetchError:
            return _invalid(intent, "网页地址不安全")
        raw_max_chars = str(intent.args.get("max_chars", "") or "").strip()
        if raw_max_chars:
            parsed = _parse_bounded_int(
                raw_max_chars,
                minimum=1,
                maximum=max(1, int(max_web_chars)),
            )
            if parsed is None:
                return _invalid(intent, "max_chars 超出范围")
        return intent

    if intent.name == "search_food_restaurants":
        limits = {
            "name": 50,
            "tag": 120,
            "location": 30,
            "sort": 12,
            "order": 5,
            "limit": 4,
        }
        for key, limit in limits.items():
            value = str(intent.args.get(key, "") or "")
            if len(value) > limit:
                return _invalid(intent, "字段过长")
            if _has_control_chars(value):
                return _invalid(intent, "字段含控制字符")
        for key in ("name", "tag", "location"):
            value = str(intent.args.get(key, "") or "").strip()
            if value and not food_engine.sanitize_food_submit_text(
                value, max_chars=limits[key]
            ):
                return _invalid(intent, "字段含链接/路径")
            if value and _contains_food_path_escape(value):
                return _invalid(intent, "字段含路径穿越")
            if value and _contains_command_reference(value):
                return _invalid(intent, "字段含命令字符")
        selectors = [
            str(intent.args.get(key, "") or "").strip()
            for key in ("name", "tag", "location")
        ]
        if not any(selectors):
            return _invalid(intent, "缺少查询条件")
        sort = str(intent.args.get("sort", "") or "").strip().lower()
        if sort and sort not in _FOOD_SORTS:
            return _invalid(intent, "排序字段无效")
        order = str(intent.args.get("order", "") or "").strip().lower()
        if order and order not in _FOOD_ORDERS:
            return _invalid(intent, "排序方向无效")
        raw_limit = str(intent.args.get("limit", "") or "").strip()
        if raw_limit and _parse_bounded_int(raw_limit, minimum=1, maximum=5) is None:
            return _invalid(intent, "数量超出范围")
        return intent

    if intent.name == "submit_food_review":
        limits = {
            "restaurant": 200,
            "location": 60,
            "comment": 500,
            "tags": 120,
            "taste": 8,
            "value": 8,
        }
        for key, limit in limits.items():
            value = str(intent.args.get(key, "") or "")
            if len(value) > limit:
                return _invalid(intent, "字段过长")
            if _has_control_chars(value):
                return _invalid(intent, "字段含控制字符")
        if not any(
            str(intent.args.get(key, "") or "").strip()
            for key in limits
        ):
            return _invalid(intent, "缺少餐厅信息")
        for key in ("restaurant", "location", "comment"):
            value = str(intent.args.get(key, "") or "").strip()
            if value and not food_engine.sanitize_food_submit_text(
                value,
                max_chars=limits[key],
                forbid_url=False if key == "comment" else True,
            ):
                return _invalid(intent, "字段含链接/路径")
            if value and _contains_food_path_escape(value):
                return _invalid(intent, "字段含路径穿越")
            if value and _contains_command_reference(value):
                return _invalid(intent, "字段含命令字符")
        tag_text = str(intent.args.get("tags", "") or "").strip()
        if tag_text:
            raw_tag_parts = [
                part.strip()
                for part in food_engine.FOOD_TAG_SPLIT_RE.split(tag_text)
                if part.strip()
            ]
            if (
                not raw_tag_parts
                or len(raw_tag_parts) > 5
                or any(
                    len(tag) > 20
                    or not food_engine.sanitize_food_submit_text(
                        tag, max_chars=20
                    )
                    or _contains_food_path_escape(tag)
                    for tag in raw_tag_parts
                )
            ):
                return _invalid(intent, "标签非法")
            if _contains_command_reference(tag_text):
                return _invalid(intent, "标签含命令字符")
        for key in ("taste", "value"):
            raw_score = str(intent.args.get(key, "") or "").strip()
            if raw_score and food_engine.parse_food_score(raw_score) is None:
                return _invalid(intent, "评分超出范围")
        return intent

    return _invalid(intent, "工具不在白名单")


def _contains_food_path_escape(value: str) -> bool:
    """Reject traversal/absolute/Windows paths while allowing address slashes."""

    text = str(value or "")
    if ".." in text or "\\" in text:
        return True
    if re.match(r"^[A-Za-z]:[/\\]", text) or text.startswith(("/", "\\")):
        return True
    return bool(re.search(r"(?:^|/)\.{1,2}(?:/|$)", text))


def _contains_command_reference(value: str) -> bool:
    """Reject shell syntax without banning ordinary punctuation in prose."""

    return bool(
        any(char in value for char in _UNSAFE_COMMAND_CHARS)
        or "`" in value
        or "$(" in value
        or _UNSAFE_COMMAND_WORDS.search(value)
    )


def _clean_after_marker_removal(raw: str) -> str:
    clean = TOOL_MARKER_RE.sub("", raw)
    clean = _DANGLING_MARKER_RE.sub("", clean)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def strip_tool_markers(text: str) -> str:
    """Remove marker envelopes/leftovers from trusted candidate plain text."""

    raw = str(text or "")
    if len(raw) > _MAX_MARKER_SOURCE_CHARS:
        raw = raw[:_MAX_MARKER_SOURCE_CHARS]
    return _clean_after_marker_removal(raw)


def parse_tool_markers(
    text: str,
    *,
    max_keyword_chars: int = 100,
    max_count: int = 5,
    max_web_chars: int = 4000,
) -> ToolParseResult:
    """Parse at most one physical line per marker and sanitize the plain text.

    All well-formed envelopes are returned for the caller's action-count check.
    A malformed ``[[tool:`` envelope still marks the reply as tainted and is
    stripped, but produces an invalid intent that can never be dispatched.
    """

    raw = str(text or "")
    if len(raw) > _MAX_MARKER_SOURCE_CHARS:
        raw = raw[:_MAX_MARKER_SOURCE_CHARS]
    matches = list(TOOL_MARKER_RE.finditer(raw))
    loose_openers = len(_LOOSE_MARKER_RE.findall(raw))
    had_loose = loose_openers > 0
    if not matches and not had_loose:
        return ToolParseResult(raw, [], False)

    intents: list[ToolIntent] = []
    for match in matches:
        name = match.group(1).strip().lower()
        body = match.group(2).strip()
        args, error = _parse_args(body)
        intent = ToolIntent(name=name, args=args, valid=not error, error=error)
        intents.append(
            validate_intent(
                intent,
                max_keyword_chars=max_keyword_chars,
                max_count=max_count,
                max_web_chars=max_web_chars,
            )
        )

    malformed = loose_openers > len(matches)
    clean = strip_tool_markers(raw)
    # A second opener that did not form a complete envelope taints the whole
    # reply.  This makes mixed valid + malformed replies fail closed.
    if malformed or _LOOSE_MARKER_RE.search(clean):
        intents.append(
            ToolIntent(name="", args={}, valid=False, error="标记格式无效")
        )
        clean = strip_tool_markers(clean)
    return ToolParseResult(clean, intents, True)


def tool_instruction(enabled_tools: list[str] | None = None) -> str:
    """Return marker grammar/catalog for the enabled names; never executes."""

    if enabled_tools is None:
        selected = list(ALLOWED_TOOLS)
    else:
        selected = [
            name for name in ALLOWED_TOOLS if str(name or "") in enabled_tools
        ]
    if not selected:
        return ""

    descriptions: list[str] = []
    if "fetch_webpage" in selected:
        descriptions.append(
            '[[tool:fetch_webpage url="https://example.com/article" '
            'max_chars=3000]] 读取公开网页；只允许公开 HTTP(S)，'
            "不登录、不带 Cookie"
        )
    if "search_images" in selected:
        descriptions.append(
            '[[tool:search_images keyword="关键词" count=1]] '
            "搜索图片，禁止路径/命令"
        )
    if "send_sticker" in selected:
        descriptions.append(
            '[[tool:send_sticker category="分类"]]、'
            '[[tool:send_sticker emotion="happy"]] 或 '
            '[[tool:send_sticker tag="标签"]] 三选一；仍需保留正常文字回复'
        )
    if "collect_sticker" in selected:
        descriptions.append(
            '[[tool:collect_sticker category="分类"]] 收藏当前消息中的表情'
        )
    if "list_sticker_categories" in selected:
        descriptions.append(
            "[[tool:list_sticker_categories()]] 列出可用表情分类"
        )
    if "search_food_restaurants" in selected:
        descriptions.append(
            '[[tool:search_food_restaurants name="店名" tag="火锅" '
            'location="南关区" sort="total" order="desc" limit=5]] '
            "name/tag/location 至少给一个"
        )
    if "submit_food_review" in selected:
        descriptions.append(
            '[[tool:submit_food_review restaurant="店名" location="位置" '
            'taste="4.5" value="3.5" comment="评价" tags="火锅,夜宵"]] '
            "仅在用户明确要求提交到 Food 时使用；缺项留空，不编造"
        )

    marker = '[[tool:工具名 参数="值"]]'
    url_rule = (
        "只有 fetch_webpage 可携带公开网页 URL。"
        if "fetch_webpage" in selected
        else "当前工具均不接受 URL。"
    )
    return (
        "安全工具协议：只可使用下列白名单工具。"
        "每条回复最多一个工具标记，标记必须位于回复末尾；"
        "不需要工具时不要写标记，也不要解释协议。"
        "禁止文件路径、终端命令、系统控制；"
        + url_rule
        + "可选工具："
        + "；".join(descriptions)
        + f"。格式：{marker}"
    )
