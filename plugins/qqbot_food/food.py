"""Pure helpers for the fixed-host food.dlsz.top /food command."""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date
from typing import Any

FOOD_BASE_URL = "https://food.dlsz.top"
FOOD_HOST = "food.dlsz.top"
FOOD_LOCATIONS_PATH = "/api/locations/search"
FOOD_RESTAURANTS_PATH = "/api/restaurants"
FOOD_TAGS_PATH = "/api/tags"
FOOD_TAGS_SEARCH_PATH = "/api/tags/search"
FOOD_ALLOWED_PATHS = {
    FOOD_LOCATIONS_PATH,
    FOOD_RESTAURANTS_PATH,
    FOOD_TAGS_PATH,
    FOOD_TAGS_SEARCH_PATH,
}
FOOD_LOCATION_SUFFIXES = (
    "区",
    "县",
    "市",
    "镇",
    "乡",
    "街道",
    "路",
    "街",
    "省",
    "州",
)
FOOD_MAX_RESULTS_HARD_LIMIT = 5
FOOD_UNSAFE_URL_TOKENS = (
    "://",
    "http:",
    "https:",
    "www.",
    "file:",
    "javascript:",
    "data:",
    "vbscript:",
    "ftp:",
    "mailto:",
)


class FoodAPIError(ValueError):
    """Safe, non-path-bearing API error for /food fallback messages."""


def sanitize_food_query(raw: Any, *, max_chars: int = 50) -> str | None:
    value = str(raw or "").strip()
    if not value or len(value) > max(1, int(max_chars)):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    lowered = value.casefold()
    if any(
        token in lowered
        for token in FOOD_UNSAFE_URL_TOKENS
    ):
        return None
    if any(char in value for char in ("/", "\\", "?", "#", "\x00")):
        return None
    if value in {".", ".."}:
        return None
    return value


def split_food_args(raw: Any) -> list[str]:
    return [part for part in re.split(r"\s+", str(raw or "").strip()) if part]


def likely_location_token(token: str) -> bool:
    value = str(token or "").strip()
    return any(value.endswith(suffix) for suffix in FOOD_LOCATION_SUFFIXES)


def build_food_url(path: str, params: dict[str, Any]) -> str:
    if path not in FOOD_ALLOWED_PATHS:
        raise FoodAPIError("unsupported food API path")
    encoded = urllib.parse.urlencode(
        {key: str(value) for key, value in params.items()}
    )
    return f"{FOOD_BASE_URL}{path}?{encoded}"


def validate_food_final_url(final_url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(final_url)
    except ValueError:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == FOOD_HOST
        and port in (None, 443)
        and parsed.path in FOOD_ALLOWED_PATHS
        and parsed.username is None
        and parsed.password is None
    )


def decode_food_payload(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except (TypeError, ValueError) as exc:
        raise FoodAPIError("food API returned non-JSON") from exc


def extract_food_items(payload: Any) -> list[dict[str, Any]]:
    candidates = payload
    if isinstance(payload, dict):
        candidates = None
        for key in ("data", "results", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict)]


def select_food_location(
    locations: list[dict[str, Any]],
    query: str,
) -> dict[str, Any] | None:
    needle = str(query or "").strip().casefold()
    if not needle:
        return None
    for item in locations:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if name.casefold() == needle:
            return item
    for item in locations:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip().casefold()
        path = str(item.get("path", "") or "").strip().casefold()
        if needle in name or (name and name in needle) or needle in path:
            return item
    return None


def _safe_display(value: Any, *, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = "".join(
        char for char in text if ord(char) >= 32 and ord(char) != 127
    )
    return text[:limit]


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number != number or number in (float("inf"), float("-inf")):
        return "-"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def clamp_food_reply(text: Any, *, max_chars: int = 1800) -> str:
    """Hard-cap every model-visible reply so one bad record cannot flood chat."""

    value = str(text or "").strip()
    limit = max(1, int(max_chars))
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def format_food_results(
    items: list[dict[str, Any]],
    *,
    keyword: str,
    location_name: str = "",
    max_results: int = 5,
    footer: str = "更多：https://food.dlsz.top",
    max_chars: int = 1800,
) -> str:
    limit = max(1, min(FOOD_MAX_RESULTS_HARD_LIMIT, int(max_results or 1)))
    shown = items[:limit]
    location_part = f"（地点：{_safe_display(location_name, limit=40)}）" if location_name else ""
    lines = [
        f"找到 {len(shown)} 家「{_safe_display(keyword, limit=50)}」相关餐厅{location_part}："
    ]
    for index, item in enumerate(shown, 1):
        if not isinstance(item, dict):
            continue
        name = _safe_display(item.get("name"), limit=100) or "未命名餐厅"
        location = _safe_display(item.get("location_name"), limit=60)
        raw_tags = item.get("tags", [])
        if isinstance(raw_tags, (list, tuple)):
            tag_values = list(raw_tags)
        elif isinstance(raw_tags, str):
            tag_values = [part for part in re.split(r"[,，、;；|/]+", raw_tags) if part]
        else:
            tag_values = []
        tags = "、".join(
            tag
            for tag in (
                _safe_display(raw_tag, limit=30)
                for raw_tag in tag_values
            )
            if tag
        )
        taste = _format_number(item.get("taste_avg"))
        total_raw = item.get("total_avg")
        if total_raw in (None, ""):
            total_raw = item.get("score")
        total = _format_number(total_raw)
        value = _format_number(item.get("value_avg"))
        review_raw = item.get("review_count")
        if review_raw in (None, ""):
            reviews = item.get("reviews")
            review_raw = len(reviews) if isinstance(reviews, list) else 0
        try:
            review_count = max(0, int(review_raw or 0))
        except (TypeError, ValueError):
            review_count = 0
        latest_raw = item.get("latest_review_date")
        if latest_raw in (None, ""):
            latest_raw = item.get("date")
        latest = _safe_display(latest_raw, limit=40) or "无"
        lines.append(f"{index}. {name}")
        if location:
            lines.append(f"   地点：{location}")
        if tags:
            lines.append(f"   标签：{tags}")
        lines.append(
            f"   评分：均分 {total} / 口味 {taste} / 性价比 {value}"
        )
        lines.append(f"   评价：{review_count} 条，最新 {latest}")
    lines.append(footer)
    return clamp_food_reply("\n".join(lines), max_chars=max_chars)


# ---------------------------------------------------------------------------
# Natural-language review submission (fixed POST endpoint)
# ---------------------------------------------------------------------------
FOOD_SUBMIT_PATH = "/api/upload"
FOOD_SUBMIT_URL = FOOD_BASE_URL + FOOD_SUBMIT_PATH
FOOD_SCORE_MIN = 0.1
FOOD_SCORE_MAX = 5.0
FOOD_SUBMIT_ARG_KEYS = {
    "restaurant",
    "location",
    "taste",
    "value",
    "comment",
    "tags",
}
# (arg key, user-facing label) in the order a clarification should list them.
FOOD_SUBMIT_REQUIRED = (
    ("restaurant", "店名"),
    ("location", "位置"),
    ("taste", "味道分（0.1-5）"),
    ("value", "性价比分（0.1-5）"),
    ("comment", "评价内容"),
)
FOOD_SUBMIT_LABELS = dict(FOOD_SUBMIT_REQUIRED)


def food_submit_missing_labels(keys: list[str]) -> list[str]:
    return [
        FOOD_SUBMIT_LABELS.get(str(key), str(key))
        for key in keys
    ]
FOOD_ADD_VERBS = (
    "添加",
    "加入",
    "加到",
    "加进",
    "加个",
    "加一个",
    "加一下",
    "帮我加",
    "记一下",
    "记录",
    "记到",
    "记下",
    "上传",
    "收录",
    "提交",
    "登记",
    "存档",
    "存一下",
    "上报",
    "写入",
)
FOOD_ADD_NOUNS = (
    "food",
    "dlsz",
    "评价",
    "点评",
    "评分",
    "餐厅",
    "饭店",
    "饭馆",
    "馆子",
    "这家店",
    "店铺",
    "美食",
)
FOOD_RELEVANCE_KEYWORDS = (
    "好吃",
    "难吃",
    "好吃到",
    "吃了",
    "吃过",
    "吃饭",
    "爱吃",
    "食物",
    "美食",
    "餐厅",
    "饭店",
    "饭馆",
    "馆子",
    "火锅",
    "烧烤",
    "烤肉",
    "串串",
    "麻辣烫",
    "面条",
    "拉面",
    "拌面",
    "牛肉面",
    "米线",
    "米粉",
    "米饭",
    "盖饭",
    "炒饭",
    "炒菜",
    "菜品",
    "饺子",
    "包子",
    "小笼包",
    "汉堡",
    "披萨",
    "寿司",
    "炸鸡",
    "薯条",
    "面包",
    "甜品",
    "蛋糕",
    "冰淇淋",
    "咖啡",
    "奶茶",
    "饮料",
    "海鲜",
    "牛肉",
    "猪肉",
    "鸡肉",
    "羊肉",
    "烤鸭",
    "小吃",
    "外卖",
    "食堂",
    "零食",
    "水果",
    "蔬菜",
    "沙拉",
    "粥",
    "口味",
    "味道",
    "汤底",
    "性价比",
    "探店",
    "菜单",
    "厨师",
    "异食癖",
    "麻辣",
    "香辣",
)
FOOD_TAG_SPLIT_RE = re.compile(r"[,，、;；|/]+")
_FULLWIDTH_NUMBER = str.maketrans("０１２３４５６７８９．", "0123456789.")


def _clean_food_text(raw: Any, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    text = "".join(
        char for char in text if ord(char) >= 32 and ord(char) != 127
    )
    return text[: max(1, int(max_chars))].strip()


def sanitize_food_submit_text(
    raw: Any,
    *,
    max_chars: int,
    forbid_url: bool = True,
) -> str:
    """Return printable single-line text, or "" if it looks like a URL/path."""

    text = _clean_food_text(raw, max_chars=max_chars)
    if not text:
        return ""
    lowered = text.casefold()
    if any(
        token in lowered
        for token in FOOD_UNSAFE_URL_TOKENS
    ):
        return ""
    if forbid_url and any(char in text for char in ("\\", "\x00")):
        return ""
    return text


def parse_food_score(raw: Any) -> float | None:
    """Parse 0.1-5.0 scores; returns None for anything else."""

    text = str(raw or "").strip().translate(_FULLWIDTH_NUMBER)
    text = text.replace("分", "").replace("／", "/").strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number < FOOD_SCORE_MIN or number > FOOD_SCORE_MAX:
        return None
    return round(number, 2)


def split_food_tags(
    raw: Any,
    *,
    max_count: int = 5,
    max_chars: int = 20,
) -> list[str]:
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        text = str(raw or "")
        lowered = text.casefold()
        if any(
            token in lowered
            for token in FOOD_UNSAFE_URL_TOKENS
        ):
            return []
        parts = FOOD_TAG_SPLIT_RE.split(text)
    tags: list[str] = []
    for part in parts:
        tag = sanitize_food_submit_text(part, max_chars=max_chars)
        if not tag or tag in tags:
            continue
        tags.append(tag)
        if len(tags) >= max(1, int(max_count)):
            break
    return tags


def validate_food_submission(
    args: dict[str, Any],
    *,
    name_max: int = 100,
    location_max: int = 60,
    comment_max: int = 500,
    tag_max: int = 20,
    tag_count: int = 5,
) -> dict[str, Any]:
    """Normalize marker args into (fields, missing labels, invalid labels)."""

    raw = args if isinstance(args, dict) else {}
    fields: dict[str, Any] = {}
    missing: list[str] = []
    invalid: list[str] = []

    restaurant = sanitize_food_submit_text(
        raw.get("restaurant"), max_chars=name_max
    )
    if restaurant:
        fields["restaurant"] = restaurant

    location = sanitize_food_submit_text(
        raw.get("location"), max_chars=location_max
    )
    if location:
        fields["location"] = location

    comment = sanitize_food_submit_text(
        raw.get("comment"), max_chars=comment_max, forbid_url=False
    )
    if comment:
        fields["comment"] = comment

    for key in ("taste", "value"):
        raw_score = str(raw.get(key, "") or "").strip()
        score = parse_food_score(raw_score)
        if score is None:
            if raw_score:
                invalid.append(key)
            continue
        fields[key] = score

    tags = split_food_tags(
        raw.get("tags"), max_count=tag_count, max_chars=tag_max
    )
    if tags:
        fields["tags"] = tags

    for key, _label in FOOD_SUBMIT_REQUIRED:
        if key not in fields:
            missing.append(key)
    return {"fields": fields, "missing": missing, "invalid": invalid}



FOOD_SUBMIT_CONFIRM_RE = re.compile(
    r"^(?:好的?|要|可以|行|嗯+|确认(?:提交|添加|一下)?|"
    r"提交(?:一下)?|添加(?:一下)?|加上|记吧|就这样|没问题)"
    r"[吧呗呀啊嘛喵!！。.~～\s]{0,3}$"
)


def is_food_submit_confirmation(text: str) -> bool:
    """True for a bare confirmation that answers our "add it?" question."""

    return bool(FOOD_SUBMIT_CONFIRM_RE.match(str(text or "").strip()))


def detect_food_add_intent(text: str) -> bool:
    """True only for explicit "add/record/upload to food" style requests."""

    lowered = str(text or "").casefold()
    if not lowered.strip():
        return False
    return any(verb in lowered for verb in FOOD_ADD_VERBS) and any(
        noun in lowered for noun in FOOD_ADD_NOUNS
    )


def detect_food_relevance(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(keyword in lowered for keyword in FOOD_RELEVANCE_KEYWORDS)


FOOD_LOCATION_SEPARATOR_RE = re.compile(r"\s*(?:/|\\|>|→|—|–|-)\s*")
FOOD_LOCATION_SUFFIXES = (
    "街道",
    "省",
    "市",
    "区",
    "县",
    "镇",
    "乡",
    "路",
    "街",
    "州",
)
FOOD_LOCATION_SUFFIX_ORDER = ("街道", "省", "市", "区", "县", "镇", "乡", "路", "街", "州")
FOOD_LOCATION_MAX_INPUT_CHARS = 200


def _split_chinese_location_segments(text: str) -> list[str]:
    """Split "吉林省长春市南关区" / "中国牛逼市" into administrative parts."""

    parts: list[str] = []
    rest = str(text or "").strip()
    if not rest:
        return []
    if rest.startswith("中国") and len(rest) > 2:
        parts.append("中国")
        rest = rest[2:]
    while rest:
        cut = 0
        for index in range(2, len(rest) + 1):
            segment = rest[:index]
            if any(segment.endswith(suffix) for suffix in FOOD_LOCATION_SUFFIX_ORDER):
                cut = index
                break
        if cut <= 0:
            parts.append(rest)
            break
        parts.append(rest[:cut])
        rest = rest[cut:]
    return parts


def split_food_location_path(
    raw: Any,
    *,
    max_parts: int = 6,
    max_part_chars: int = 30,
) -> list[str]:
    """Return location components, or [full original] as a safe fallback.

    Accepts "/", "\\", ">", "→", "-", "—" as separators and splits plain
    Chinese addresses at administrative suffixes. URLs/control chars are
    rejected (empty list) so nothing unsafe reaches the fixed-site API.
    """

    text = str(raw or "").strip()
    if not text or len(text) > FOOD_LOCATION_MAX_INPUT_CHARS:
        return []
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return []
    lowered = text.casefold()
    if any(
        token in lowered
        for token in FOOD_UNSAFE_URL_TOKENS
    ):
        return []
    normalised = FOOD_LOCATION_SEPARATOR_RE.sub("/", text)
    parts = [part.strip() for part in normalised.split("/") if part.strip()]
    if not parts:
        return []
    if len(parts) == 1:
        parts = _split_chinese_location_segments(parts[0])
    if not parts:
        return []
    limit_parts = max(1, int(max_parts))
    limit_chars = max(1, int(max_part_chars))
    if len(parts) > limit_parts or any(
        len(part) > limit_chars for part in parts
    ):
        # Preserve the full original as the documented fallback.
        return [text]
    return parts


def format_food_location_path(parts: list[str]) -> str:
    return "/".join(
        str(part).strip() for part in parts if str(part).strip()
    )


def _food_location_path_parts(item: dict[str, Any]) -> tuple[str, ...]:
    raw = str(item.get("path") or item.get("name") or "").strip()
    raw = raw.replace("\\", "/").replace("／", "/")
    return tuple(
        part.strip().casefold() for part in raw.split("/") if part.strip()
    )


def match_food_location(
    parts: list[str],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Match parsed components against site locations.

    Returns {"kind": "full"|"prefix"|"ambiguous"|"none"} where "prefix" means
    the deepest existing ancestor plus the remaining tail to create.
    """

    original = [str(part).strip() for part in parts if str(part).strip()]
    norm = tuple(part.casefold() for part in original)
    valid = [
        item
        for item in items
        if isinstance(item, dict) and item.get("id") is not None
    ]
    if not norm or not valid:
        return {"kind": "none"}
    matches: list[tuple[int, Any, str, str, bool]] = []
    for item in valid:
        path = _food_location_path_parts(item)
        if not path:
            continue
        depth = 0
        exact = False
        # An ancestor returned by the location search is a prefix we can use
        # as the parent of the not-yet-existing tail.
        if len(path) <= len(norm) and path == norm[: len(path)]:
            depth = len(path)
            exact = depth == len(norm)
        else:
            # A leaf-name search can return the full ancestor path while the
            # user typed only the leaf ("南关区").  Match that suffix too.
            for candidate in range(min(len(norm), len(path)), 0, -1):
                if path[-candidate:] == norm[:candidate]:
                    depth = candidate
                    exact = candidate == len(norm)
                    break
        if depth > 0:
            matches.append(
                (
                    depth,
                    item.get("id"),
                    str(item.get("name", "") or "").strip(),
                    "/".join(path),
                    exact,
                )
            )
    if not matches:
        return {"kind": "none"}
    exact_matches = [match for match in matches if match[4]]
    if exact_matches:
        if len({match[1] for match in exact_matches}) > 1:
            return {
                "kind": "ambiguous",
                "options": [match[3] or match[2] for match in exact_matches[:3]],
            }
        depth, ident, name, path, _exact = exact_matches[0]
        return {"kind": "full", "id": ident, "name": name or path}
    best_depth = max(match[0] for match in matches)
    best = [match for match in matches if match[0] == best_depth]
    if len({match[1] for match in best}) > 1:
        return {
            "kind": "ambiguous",
            "options": [match[3] or match[2] for match in best[:3]],
        }
    depth, ident, name, path, _exact = best[0]
    return {
        "kind": "prefix",
        "id": ident,
        "name": name,
        "depth": depth,
        "tail": original[depth:],
    }


# ---------------------------------------------------------------------------
# Per-group location context (explicit map + safe group-name inference)
# ---------------------------------------------------------------------------
FOOD_GROUP_LOCATION_JSON_MAX_CHARS = 20_000
FOOD_GROUP_LOCATION_MAX_ENTRIES = 200
FOOD_GROUP_LOCATION_MAX_PARTS = 6
FOOD_GROUP_NAME_MAX_CHARS = 60
FOOD_GROUP_CITY_SUFFIXES = ("省", "市", "州")


def parse_food_group_locations(
    raw: Any,
    *,
    max_parts: int = FOOD_GROUP_LOCATION_MAX_PARTS,
    max_part_chars: int = 30,
) -> dict[str, list[str]]:
    """Parse the ``food_group_locations`` JSON mapping (text or dict) safely.

    Invalid JSON, non-object payloads, unsafe group ids and unsafe location
    values are dropped instead of raising, so one bad editor entry can never
    break food handling for every group.
    """

    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text or len(text) > FOOD_GROUP_LOCATION_JSON_MAX_CHARS:
            return {}
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, list[str]] = {}
    for index, (key, value) in enumerate(data.items()):
        if index >= FOOD_GROUP_LOCATION_MAX_ENTRIES:
            break
        group_id = sanitize_food_submit_text(key, max_chars=40)
        if not group_id:
            continue
        if isinstance(value, (list, tuple)):
            parts = [
                part
                for part in (
                    sanitize_food_submit_text(
                        item, max_chars=max_part_chars
                    )
                    for item in value
                )
                if part
            ][: max(1, int(max_parts))]
        else:
            parts = split_food_location_path(
                value,
                max_parts=max_parts,
                max_part_chars=max_part_chars,
            )
        if parts:
            result[group_id] = parts
    return result


def infer_food_group_location_parts(
    raw: Any,
    *,
    max_parts: int = FOOD_GROUP_LOCATION_MAX_PARTS,
    max_part_chars: int = 30,
) -> list[str]:
    """Infer a city/省 context from an AstrBot group name.

    Only segments that explicitly end with 省/市/州 count, e.g.
    "长春市火锅交流群" -> ["长春市"]; "长春火锅群" carries no administrative
    suffix and is ignored on purpose (configure ``food_group_locations``).
    """

    text = sanitize_food_submit_text(
        raw, max_chars=FOOD_GROUP_NAME_MAX_CHARS
    )
    if not text:
        return []
    limit_chars = max(1, int(max_part_chars))
    parts = [
        segment
        for segment in _split_chinese_location_segments(text)
        if segment
        and len(segment) <= limit_chars
        and any(
            segment.endswith(suffix) for suffix in FOOD_GROUP_CITY_SUFFIXES
        )
    ]
    return parts[: max(1, int(max_parts))]


def merge_food_location_with_context(
    parts: list[str],
    context: list[str],
) -> list[str]:
    """Merge a group-city context into a user-provided location path.

    - user path already contains the context -> user path unchanged
    - user starts with 中国 or another 省/市/州 -> user path wins
    - otherwise prepend the context, de-duplicating overlapping segments
    """

    user = [str(part).strip() for part in (parts or []) if str(part).strip()]
    ctx = [str(part).strip() for part in (context or []) if str(part).strip()]
    if not ctx:
        return user
    if not user:
        return ctx
    norm_user = [part.casefold() for part in user]
    norm_ctx = [part.casefold() for part in ctx]
    if "/".join(norm_ctx) in "/".join(norm_user):
        return user
    if user[0] == "中国":
        return user
    if norm_ctx[0] == norm_user[0]:
        # Same province named by both sides: keep the more specific context.
        return ctx + user[1:]
    if len(ctx) > 1 and norm_ctx[-1] == norm_user[0]:
        # "长春市/南关区" plus context "吉林省/长春市": reuse the city.
        return ctx + user[1:]
    if any(
        user[0].endswith(suffix) for suffix in FOOD_GROUP_CITY_SUFFIXES
    ):
        return user
    if norm_ctx[: len(norm_user)] == norm_user:
        return ctx
    for depth in range(min(len(norm_ctx), len(norm_user)), 0, -1):
        if norm_ctx[-depth:] == norm_user[:depth]:
            return ctx + user[depth:]
    return ctx + user


def strip_food_group_city_prefix(
    keyword: str,
    context: list[str],
) -> tuple[str, bool]:
    """Split "长春火锅" into ("火锅", True) when the group city is 长春."""

    text = str(keyword or "").strip()
    if not text or not context:
        return text, False
    leaf = str(context[-1]).strip()
    root = leaf
    for suffix in FOOD_GROUP_CITY_SUFFIXES:
        if root.endswith(suffix) and len(root) > len(suffix):
            root = root[: -len(suffix)]
            break
    for candidate in (leaf, root):
        if (
            candidate
            and len(text) > len(candidate)
            and text.startswith(candidate)
        ):
            rest = text[len(candidate):].strip(" 的，,。.")
            if rest:
                return rest, True
    return text, False


def validate_food_submit_url(url: str) -> bool:
    """Only the one fixed https://food.dlsz.top/api/upload endpoint is valid."""

    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == FOOD_HOST
        and port in (None, 443)
        and parsed.path == FOOD_SUBMIT_PATH
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


def build_food_submit_payload(
    fields: dict[str, Any],
    *,
    username: str,
    password: str,
    location_id: Any = None,
    new_location_name: str = "",
    new_location_parent_id: Any = None,
) -> dict[str, Any]:
    """Build the documented POST body; review_date is always today (local)."""

    payload: dict[str, Any] = {
        "name": str(fields.get("restaurant", "")),
        "comment": str(fields.get("comment", "")),
        "taste_score": float(fields.get("taste", 0.0)),
        "value_score": float(fields.get("value", 0.0)),
    }
    tags = fields.get("tags")
    # Always emit an array so the fixed endpoint never has to guess at types.
    payload["tags"] = (
        [str(tag) for tag in tags if str(tag).strip()]
        if isinstance(tags, list)
        else []
    )
    # The site defaults to "now" when omitted; the contract requires the
    # plugin to send today's date explicitly.
    payload["review_date"] = date.today().isoformat()
    if username:
        payload["username"] = username
    # Injected from the sender QQ id; never model-visible or logged.
    if password:
        payload["password"] = password
    if location_id is not None:
        try:
            payload["location_id"] = int(location_id)
        except (TypeError, ValueError):
            return {}
    elif new_location_name:
        payload["new_location_name"] = str(new_location_name)
        if new_location_parent_id is not None:
            try:
                payload["new_location_parent_id"] = int(
                    new_location_parent_id
                )
            except (TypeError, ValueError):
                return {}
    else:
        return {}
    return payload


def _food_submit_error_kind(status: int, code: str, error: str) -> str:
    """Stable error kind; prefers v1.1 ``code`` and falls back to v1 text."""

    lowered = error.casefold()
    if code == "DUPLICATE_USERNAME" or (
        "已存在" in error and ("用户" in error or "username" in lowered)
    ):
        return "duplicate_username"
    if code == "LOCATION_EXISTS" or ("位置" in error and "已存在" in error):
        return "location_exists"
    if code == "RESTAURANT_NOT_FOUND" or status == 404 or "餐厅不存在" in error:
        return "restaurant_not_found"
    if code.startswith("MISSING_") or "不能为空" in error:
        return "missing_field"
    if code.startswith("INVALID_") or "格式不正确" in error or "之间" in error:
        return "invalid_score"
    if 300 <= status < 400:
        return "redirect"
    if status >= 500:
        return "server"
    return "rejected"


def parse_food_submit_response(status: Any, payload: Any) -> dict[str, Any]:
    """Interpret the upload response; success requires 200 + success:true."""

    body = payload if isinstance(payload, dict) else {}
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        status_code = 0
    if status_code == 200 and body.get("success") is True:
        tags = body.get("tags")
        return {
            "ok": True,
            "restaurant_id": body.get("restaurant_id"),
            "restaurant_name": _clean_food_text(
                body.get("restaurant_name"), max_chars=100
            ),
            "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
        }
    error = _clean_food_text(body.get("error"), max_chars=200)
    code = str(body.get("code", "") or "").strip().upper()
    return {
        "ok": False,
        "status": status_code,
        "code": code,
        "kind": _food_submit_error_kind(status_code, code, error),
        "error": error,
    }


def food_submit_error_reason(result: dict[str, Any]) -> str:
    """Short safe reason for generic templates; never a URL or traceback."""

    return safe_food_error_reason(result, max_chars=80)


# ---------------------------------------------------------------------------
# Natural-language restaurant search (read-only GET, sorted locally)
# ---------------------------------------------------------------------------
FOOD_SEARCH_SORTS = {
    "total": "total_avg",
    "taste": "taste_avg",
    "value": "value_avg",
    "reviews": "review_count",
    "name": "name",
    "latest": "latest_review_date",
}
FOOD_SEARCH_DEFAULT_SORT = "total"
FOOD_SEARCH_HARD_LIMIT = 5
FOOD_SEARCH_SORT_ALIASES = {
    "total": "total",
    "score": "total",
    "rating": "total",
    "均分": "total",
    "评分": "total",
    "综合": "total",
    "taste": "taste",
    "口味": "taste",
    "味道": "taste",
    "value": "value",
    "性价比": "value",
    "reviews": "reviews",
    "review": "reviews",
    "评价": "reviews",
    "评论": "reviews",
    "name": "name",
    "名称": "name",
    "店名": "name",
    "latest": "latest",
    "最新": "latest",
    "日期": "latest",
}
FOOD_SEARCH_ORDER_ALIASES = {
    "asc": "asc",
    "ascending": "asc",
    "升序": "asc",
    "正序": "asc",
    "从低到高": "asc",
    "desc": "desc",
    "descending": "desc",
    "降序": "desc",
    "倒序": "desc",
    "从高到低": "desc",
}


def _bounded_int(
    raw: Any,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        base = int(default)
    except (TypeError, ValueError):
        base = minimum
    top = maximum if maximum is not None else max(base, minimum)
    base = max(minimum, min(int(top), base))
    text = str(raw or "").strip()
    if not text:
        return base
    try:
        value = int(text)
    except (TypeError, ValueError):
        return base
    if value < minimum:
        value = minimum
    return max(minimum, min(int(top), value))


def normalize_food_search_sort(raw: Any) -> str:
    value = str(raw or "").strip().casefold()
    if not value:
        return ""
    value = FOOD_SEARCH_SORT_ALIASES.get(value, value)
    return value if value in FOOD_SEARCH_SORTS else ""


def normalize_food_search_order_value(raw: Any) -> str:
    value = str(raw or "").strip().casefold()
    if not value:
        return ""
    value = FOOD_SEARCH_ORDER_ALIASES.get(value, value)
    return value if value in ("asc", "desc") else ""


def normalize_food_search_order(raw: Any, *, sort: str) -> str:
    value = normalize_food_search_order_value(raw)
    if value:
        return value
    # Ratings/review counts/latest dates default to best-first; names A-Z.
    return "asc" if sort == "name" else "desc"


def normalize_food_search_limit(
    raw: Any,
    *,
    default: int = FOOD_SEARCH_HARD_LIMIT,
    hard: int = FOOD_SEARCH_HARD_LIMIT,
) -> int:
    top = max(1, int(hard or FOOD_SEARCH_HARD_LIMIT))
    try:
        bounded_default = max(1, min(top, int(default)))
    except (TypeError, ValueError):
        bounded_default = top
    text = str(raw or "").strip().translate(_FULLWIDTH_NUMBER)
    if not text:
        return bounded_default
    try:
        value = int(text)
    except (TypeError, ValueError):
        return bounded_default
    if value < 1 or value > bounded_default:
        return bounded_default
    return value


FOOD_SEARCH_FLAG_ALIASES = {
    "--name": "name",
    "--keyword": "name",
    "--q": "name",
    "-q": "name",
    "--tag": "tag",
    "--tags": "tag",
    "-t": "tag",
    "--location": "location",
    "--loc": "location",
    "-l": "location",
    "--sort": "sort",
    "-s": "sort",
    "--order": "order",
    "-o": "order",
    "--limit": "limit",
    "-n": "limit",
}


def _split_flag_token(token: str) -> tuple[str, str]:
    if "=" in token:
        head, value = token.split("=", 1)
        return head.strip(), value.strip()
    return token.strip(), ""


def _flag_name(token: str) -> str:
    head, _value = _split_flag_token(token)
    return FOOD_SEARCH_FLAG_ALIASES.get(head.casefold(), "")


def normalize_food_search_request(
    values: dict[str, Any] | None,
    *,
    name_max_chars: int = 50,
    location_max_chars: int = 30,
    tag_max_chars: int = 20,
    tag_count: int = 3,
    default_limit: int = 5,
    default_sort: str = FOOD_SEARCH_DEFAULT_SORT,
    default_order: str = "",
) -> dict[str, Any]:
    """Validate one search request from a command or a safe-tool mapping.

    Returns ``ok`` plus normalized fields; ``error`` is a stable machine code
    (``invalid_name``, ``invalid_location``, ``invalid_tag``, ``invalid_sort``,
    ``invalid_order``, ``invalid_limit`` or ``empty``) suitable for a fixed
    Chinese reply. Nothing here touches the network.
    """

    raw = values if isinstance(values, dict) else {}
    tags_source = raw.get("tags")
    if tags_source in (None, "", []):
        tags_source = raw.get("tag")
    tags = split_food_tags(
        tags_source,
        max_count=max(1, int(tag_count)),
        max_chars=max(1, int(tag_max_chars)),
    )
    tag_was_given = bool(
        tags_source
        if isinstance(tags_source, (list, tuple))
        else str(tags_source or "").strip()
    )
    if tag_was_given and not tags:
        return {"ok": False, "error": "invalid_tag"}

    name_raw = raw.get("name")
    if name_raw in (None, ""):
        name_raw = raw.get("q")
    name = sanitize_food_submit_text(
        name_raw, max_chars=max(1, int(name_max_chars))
    )
    if str(name_raw or "").strip() and not name:
        return {"ok": False, "error": "invalid_name"}

    location_raw = raw.get("location")
    location = sanitize_food_submit_text(
        location_raw, max_chars=max(1, int(location_max_chars))
    )
    if str(location_raw or "").strip() and not location:
        return {"ok": False, "error": "invalid_location"}

    sort_raw = str(raw.get("sort") or "").strip()
    if sort_raw:
        sort = normalize_food_search_sort(sort_raw)
        if not sort:
            return {"ok": False, "error": "invalid_sort"}
    else:
        sort = normalize_food_search_sort(default_sort) or FOOD_SEARCH_DEFAULT_SORT

    order_raw = str(raw.get("order") or "").strip()
    if order_raw:
        order = normalize_food_search_order_value(order_raw)
        if not order:
            return {"ok": False, "error": "invalid_order"}
    else:
        fallback_order = normalize_food_search_order_value(default_order)
        order = fallback_order or (
            "asc" if sort == "name" else "desc"
        )

    limit_raw = str(raw.get("limit") or "").strip()
    if limit_raw:
        try:
            int(limit_raw.translate(_FULLWIDTH_NUMBER))
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_limit"}
    limit = normalize_food_search_limit(
        limit_raw,
        default=default_limit,
        hard=FOOD_SEARCH_HARD_LIMIT,
    )

    if not (name or location or tags):
        return {"ok": False, "error": "empty"}
    return {
        "ok": True,
        "error": "",
        "name": name,
        "location": location,
        "tags": tags,
        "sort": sort,
        "order": order,
        "limit": limit,
    }


def parse_food_search_command(
    raw_args: Any,
    *,
    name_max_chars: int = 50,
    location_max_chars: int = 30,
    tag_max_chars: int = 20,
    tag_count: int = 3,
    default_limit: int = 5,
    default_sort: str = FOOD_SEARCH_DEFAULT_SORT,
    default_order: str = "",
) -> dict[str, Any]:
    """Deterministically parse ``/food`` arguments.

    Supported forms include the legacy positional shapes
    ``/food 火锅``, ``/food 南关区 火锅`` and ``/food 火锅 南关区`` plus
    ``--tag/--location/--sort/--order/--limit`` flags. No network access or
    guessing beyond the documented location-suffix/two-token fallback.
    """

    tokens = split_food_args(raw_args)
    if not tokens:
        return {"ok": False, "error": "empty"}

    assigned: dict[str, Any] = {"tags": []}
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        flag = _flag_name(token)
        if flag:
            head, inline = _split_flag_token(token)
            if inline:
                value = inline
                index += 1
            else:
                if index + 1 >= len(tokens):
                    return {"ok": False, "error": "missing_value"}
                value = tokens[index + 1]
                index += 2
            if flag == "tag":
                assigned["tags"].append(value)
            else:
                assigned[flag] = value
            continue
        if token.startswith("--"):
            return {"ok": False, "error": "unknown_flag"}
        if token.startswith("#") and len(token) > 1:
            assigned["tags"].append(token[1:])
            index += 1
            continue
        lowered = token.casefold()
        if lowered.startswith(("tag:", "tag=", "标签:", "标签=")):
            assigned["tags"].append(token.split(":", 1)[-1].split("=", 1)[-1])
            index += 1
            continue
        positional.append(token)
        index += 1

    name = str(assigned.get("name") or "").strip()
    location = str(assigned.get("location") or "").strip()
    if positional:
        if not name and not location:
            location_indices = [
                idx for idx, token in enumerate(positional)
                if likely_location_token(token)
            ]
            if len(positional) == 1:
                if likely_location_token(positional[0]):
                    location = positional[0]
                else:
                    name = positional[0]
            elif location_indices:
                location = positional[location_indices[0]]
                name = " ".join(
                    token for idx, token in enumerate(positional)
                    if idx != location_indices[0]
                )
            elif len(positional) == 2:
                # Legacy compatibility: "/food 火锅 南关" => name, location.
                name, location = positional[0], positional[1]
            else:
                name = " ".join(positional)
        elif name and not location:
            if len(positional) == 1 and likely_location_token(positional[0]):
                location = positional[0]
            else:
                name = " ".join([name, *positional])
        elif location and not name:
            name = " ".join(positional)

    values = {
        "name": name,
        "location": location,
        "tags": assigned.get("tags", []),
        "sort": assigned.get("sort", ""),
        "order": assigned.get("order", ""),
        "limit": assigned.get("limit", ""),
    }
    return normalize_food_search_request(
        values,
        name_max_chars=name_max_chars,
        location_max_chars=location_max_chars,
        tag_max_chars=tag_max_chars,
        tag_count=tag_count,
        default_limit=default_limit,
        default_sort=default_sort,
        default_order=default_order,
    )


def sort_food_restaurants(
    items: list[dict[str, Any]],
    *,
    sort: str = FOOD_SEARCH_DEFAULT_SORT,
    order: str = "desc",
) -> list[dict[str, Any]]:
    """Sort locally; missing values always go last regardless of direction."""

    key = FOOD_SEARCH_SORTS.get(sort, FOOD_SEARCH_SORTS["total"])
    present: list[tuple[Any, dict[str, Any]]] = []
    missing: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get(key)
        if key == "total_avg" and raw in (None, ""):
            raw = item.get("score")
        elif key == "review_count" and raw in (None, ""):
            reviews = item.get("reviews")
            raw = len(reviews) if isinstance(reviews, list) else raw
        elif key == "latest_review_date" and raw in (None, ""):
            raw = item.get("date")
        if key == "name":
            value: Any = str(raw or "").strip().casefold()
            if value:
                present.append((value, item))
            else:
                missing.append(item)
        elif key == "latest_review_date":
            value = str(raw or "").strip()
            if value:
                present.append((value, item))
            else:
                missing.append(item)
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = None
            if value is None or value != value or value in (
                float("inf"),
                float("-inf"),
            ):
                missing.append(item)
            else:
                present.append((value, item))
    present.sort(key=lambda pair: pair[0], reverse=order == "desc")
    return [item for _value, item in present] + missing


def resolve_food_search_tag(
    items: list[dict[str, Any]],
    tag: str,
) -> dict[str, Any]:
    """Exact tag name -> id; ambiguous/unknown never resolves to a guess."""

    needle = str(tag or "").strip().casefold()
    tagged = [
        item
        for item in items
        if isinstance(item, dict) and item.get("id") is not None
    ]
    exact = []
    for item in tagged:
        name = str(item.get("name", "") or "").strip()
        if name.casefold() == needle:
            exact.append((item, name))
    if exact:
        if len({item.get("id") for item, _name in exact}) == 1:
            item, name = exact[0]
            return {"id": item.get("id"), "name": name}
        return {
            "ambiguous": [
                name
                for _item, name in exact[:5]
                if name
            ]
        }
    candidates = [
        str(item.get("name", "") or "").strip()
        for item in tagged
        if needle
        and (
            needle in str(item.get("name", "") or "").casefold()
            or str(item.get("name", "") or "").casefold() in needle
        )
    ]
    if candidates:
        return {"ambiguous": candidates[:5]}
    return {"not_found": True}


def resolve_food_search_location(
    items: list[dict[str, Any]],
    query: str,
) -> dict[str, Any]:
    """Exact location name -> id; ambiguous lists candidates instead of guessing."""

    needle = str(query or "").strip().casefold()
    valid = [
        item
        for item in items
        if isinstance(item, dict) and item.get("id") is not None
    ]
    exact = []
    for item in valid:
        name = str(item.get("name", "") or "").strip()
        if name.casefold() == needle:
            exact.append((item, name))
    if exact:
        if len({item.get("id") for item, _name in exact}) == 1:
            item, name = exact[0]
            return {"id": item.get("id"), "name": name}
        return {
            "ambiguous": [
                str(item.get("path", "") or name or "").strip()
                for item, name in exact[:5]
            ]
        }

    # A user normally says the city without its administrative suffix:
    # "长春" should resolve to the existing location "长春市".  Keep the
    # deliberate one-character behaviour below (it is ambiguous by design),
    # and only add suffix-tolerant matching for real place names.
    if len(needle) >= 2:
        fuzzy: list[tuple[int, int, str, dict[str, Any], str]] = []
        for item in valid:
            name = str(item.get("name", "") or "").strip()
            folded = name.casefold()
            path_parts = _food_location_path_parts(item)
            depth = len(path_parts)
            name_match = folded.startswith(needle) and any(
                folded[len(needle) :] == suffix
                for suffix in FOOD_LOCATION_SUFFIXES
            )
            path_match = any(
                part.startswith(needle) and any(
                    part[len(needle) :] == suffix
                    for suffix in FOOD_LOCATION_SUFFIXES
                )
                for part in path_parts
            )
            if name_match or path_match:
                fuzzy.append(
                    (
                        depth,
                        len(folded),
                        str(item.get("path", "") or folded).strip(),
                        item,
                        name,
                    )
                )
        if fuzzy:
            fuzzy.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
            best = fuzzy[0]
            best_key = (best[0], best[1])
            tied = [entry for entry in fuzzy if (entry[0], entry[1]) == best_key]
            unique_ids = {entry[3].get("id") for entry in tied}
            if len(unique_ids) == 1:
                return {
                    "id": best[3].get("id"),
                    "name": best[4] or best[2],
                }
            return {
                "ambiguous": [
                    entry[2] or entry[4] for entry in tied[:5] if entry[2] or entry[4]
                ]
            }

    if len(valid) == 1:
        return {
            "id": valid[0].get("id"),
            "name": str(valid[0].get("name", "") or "").strip(),
        }
    if valid:
        options = [
            str(item.get("path", "") or item.get("name", "") or "").strip()
            for item in valid[:3]
        ]
        return {"ambiguous": [option for option in options if option]}
    return {"not_found": True}


def format_food_search_results(
    items: list[dict[str, Any]],
    *,
    label: str,
    location_name: str = "",
    max_results: int = FOOD_SEARCH_HARD_LIMIT,
    footer: str = "更多：https://food.dlsz.top",
    max_chars: int = 1800,
) -> str:
    return format_food_results(
        items,
        keyword=label,
        location_name=location_name,
        max_results=max_results,
        footer=footer,
        max_chars=max_chars,
    )


def safe_food_error_reason(
    result: dict[str, Any],
    *,
    forbidden: str = "",
    max_chars: int = 80,
) -> str:
    """Bound/sanitize a site error for a Chinese reply.

    URLs, local paths, control characters and any caller-provided secret are
    removed before truncation.  The fallback is a stable HTTP status, never a
    traceback or exception string.
    """

    raw = result.get("error") if isinstance(result, dict) else ""
    text = _clean_food_text(raw, max_chars=200)
    secret = str(forbidden or "")
    if secret:
        text = text.replace(secret, "")
    # Never echo URLs or filesystem paths, even in transformed form.
    if re.search(
        r"(?i)https?://|www\.|file:|javascript:|data:|vbscript:|ftp:|mailto:|"
        r"[A-Za-z]:[\\/]|[/\\]",
        text,
    ):
        return "站点拒绝了请求"
    text = _clean_food_text(text, max_chars=max(1, int(max_chars)))
    if text:
        return text
    try:
        status = int(result.get("status", 0) or 0) if isinstance(result, dict) else 0
    except (TypeError, ValueError):
        status = 0
    return f"HTTP {status}" if status else "站点未返回成功"
