"""Configuration defaults and defensive readers for qqbot_food.

Only the Python standard library is used here.  AstrBot passes the per-plugin
configuration mapping as the ``config`` argument to the Star constructor; we
never call ``context.get_config()`` because that returns AstrBot's global
configuration rather than this plugin's config.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULTS: dict[str, Any] = {
    # ---- read-only /food command ---------------------------------------
    "food_enabled": True,
    "food_timeout_seconds": 12,
    "food_max_response_bytes": 500_000,
    "food_max_results": 5,
    "food_cooldown_seconds": 30,
    "food_keyword_max_chars": 50,
    "food_location_max_chars": 30,
    "food_reply_max_chars": 1800,
    "food_group_locations": "{}",
    "food_group_location_infer_from_name": True,
    "food_usage_reply": "用法：/food 火锅 或 /food 南关区 火锅",
    "food_disabled_reply": "餐厅查询功能当前未启用。",
    "food_empty_reply": "没有找到相关餐厅，换个关键词或地点试试吧。",
    "food_location_not_found_reply": "没有找到地点「{location}」，可以只发关键词试试。",
    "food_timeout_reply": "餐厅查询超时，请稍后再试。",
    "food_error_reply": "餐厅查询暂时失败，请稍后再试。",
    "food_cooldown_reply": "餐厅查询太频繁，请 {seconds} 秒后再试。",
    "food_invalid_query_reply": "关键词不合法，只允许普通文字，不要输入 URL 或路径。",
    "food_ambiguous_location_reply": "「{location}」匹配到多个位置：{options}。请说得更具体一点。",
    # ---- natural-language search tool ----------------------------------
    "food_search_enabled": True,
    "safe_tools_allow_search_food_restaurants": True,
    "food_search_max_tags": 3,
    "food_search_default_limit": 5,
    "food_search_default_sort": "total",
    "food_search_default_order": "",
    "food_search_name_max_chars": 50,
    "food_search_tag_max_chars": 20,
    "food_search_location_max_chars": 30,
    "food_search_cooldown_seconds": 0,
    "food_search_usage_reply": "想找哪家店？告诉我店名、标签（如火锅、烤肉）或地点（如南关区）就行。",
    "food_search_unknown_tag_reply": "没有找到标签「{tags}」。换一个标签，或只按店名/地点找。",
    "food_search_ambiguous_tag_reply": "标签「{tag}」匹配到多个：{options}。请说得更精确一点。",
    "food_search_location_not_found_reply": "没有找到地点「{location}」。换个写法，或只按店名/标签找。",
    "food_search_ambiguous_location_reply": "「{location}」匹配到多个位置：{options}。请说得更具体一点。",
    "food_search_empty_reply": "没有符合条件的结果，换个店名/标签/地点试试。",
    "food_search_timeout_reply": "餐厅搜索超时，请稍后再试。",
    "food_search_error_reply": "餐厅搜索暂时失败，请稍后再试。",
    "food_search_invalid_reply": "搜索条件不合法，只允许普通文字，不要输入 URL、路径或异常排序参数。",
    # ---- review submission ---------------------------------------------
    "food_submit_enabled": True,
    "safe_tools_allow_submit_food_review": True,
    "food_submit_timeout_seconds": 12,
    "food_submit_max_response_bytes": 100_000,
    "food_submit_cooldown_seconds": 60,
    "food_submit_draft_ttl_seconds": 600,
    "food_submit_name_max_chars": 100,
    "food_submit_location_max_chars": 60,
    "food_submit_location_max_parts": 6,
    "food_submit_location_part_max_chars": 30,
    "food_submit_comment_max_chars": 500,
    "food_submit_tag_max_chars": 20,
    "food_submit_tags_max_count": 5,
    "food_submit_username_max_chars": 32,
    "food_submit_username_fallback": "群友",
    "food_submit_missing_reply": "还差这些信息：{missing}。补全后我再提交到 food.dlsz.top。",
    "food_submit_success_reply": "已提交到 food.dlsz.top：{name}（{location}），口味 {taste} / 性价比 {value}。",
    "food_submit_ask_add_reply": "要把这条评价添加到 food.dlsz.top 吗？回复「添加」我就用刚才的信息提交。",
    "food_submit_confirm_reply": "请确认是否提交到 food.dlsz.top：{name}（{location}），口味 {taste} / 性价比 {value}。回复「确认」或「提交」即可。",
    "food_submit_already_submitted_reply": "这条评价已经提交过，本次没有重复写入。",
    "food_submit_processing_reply": "这条评价正在提交或已经提交过，请勿重复操作。",
    "food_submit_duplicate_username_reply": "这家店已有同名评价（可能你之前提交过），本次没有重复写入。",
    "food_submit_location_exists_reply": "位置「{location}」已存在，换个更具体的写法（或带上所属区县）我再提交。",
    "food_submit_restaurant_not_found_reply": "站点没有找到这家店，请确认店名和位置后再试。",
    "food_submit_invalid_reply": "站点没有接受这次提交：{reason}",
    "food_submit_error_reply": "提交评价失败，请稍后再试。",
    "food_submit_timeout_reply": "提交评价超时，请稍后再试。",
    "food_submit_disabled_reply": "自动提交美食评价功能当前未启用。",
    "food_submit_cooldown_reply": "提交太频繁，请 {seconds} 秒后再试。",
    "food_submit_score_reply": "分数需要在 0.1-5 之间：{fields}。",
    "food_submit_ambiguous_location_reply": "「{location}」匹配到多个位置：{options}。请说得更具体一点，我再提交。",
    "food_submit_location_invalid_reply": "位置格式不合法，请不要带 URL 或控制字符，换个写法我再提交。",
    "food_submit_image_prompt_reply": "这是美食图吗喵？想记录评价的话，告诉我店名、位置、味道分（0.1-5）、性价比分（0.1-5）和一段评价，我再帮你提交。",
    "food_submit_no_sender_reply": "无法确认你的身份，暂时不能提交评价；请稍后再试。",
    "food_submit_tool_rejected_reply": "这次提交没有生效：请用普通文字告诉我店名、位置、味道分（0.1-5）、性价比分（0.1-5）和评价内容，不要带链接、密码或额外参数。",
    # ---- legacy natural-language participation knobs -------------------
    "food_nl_enabled": True,
    "food_nl_probability": 0.35,
    "food_nl_cooldown_seconds": 120,
    "food_nl_min_message_chars": 4,
    "food_nl_image_enabled": True,
    "food_nl_image_probability": 0.25,
    "food_nl_image_without_caption_enabled": False,
    # ---- canonical ABI v1 groups (flat schema equivalents) -------------
    "provider_instance_id": "astrbot",
    "provider_owner_epoch": 1,
    "query_limit": 5,
    "query_name_max_chars": 50,
    "query_default_location": "",
    "query_default_tags": "",
    "query_cache_ttl_seconds": 0,
    "submit_backend_id": "food.dlsz.top",
    "submit_timeout_seconds": 12,
    "submit_confirm_required": True,
    "submit_idempotency_mode": "lookup_by_business_key",
    "submit_unknown_result_mode": "query_then_manual",
    "submit_username_max_chars": 32,
    "drafts_ttl_seconds": 600,
    "drafts_confirmation_ttl_seconds": 300,
    "draft_name_max_chars": 100,
    "draft_location_max_chars": 60,
    "draft_comment_max_chars": 500,
    "location_visibility": "private",
    "tags_max_count": 5,
    "tags_name_length": 20,
    "policy_id": "",
}

_TRUE_VALUES = {"1", "true", "yes", "y", "on", "是"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "否", ""}


class PluginConfig:
    """Small typed facade over the injected AstrBot plugin config mapping."""

    def __init__(self, values: Mapping[str, Any] | None = None):
        mapped: Any = {}
        if isinstance(values, Mapping):
            mapped = values
        elif values is not None:
            for attr in ("to_dict", "model_dump", "dict"):
                method = getattr(values, attr, None)
                if callable(method):
                    try:
                        candidate = method()
                    except Exception:
                        continue
                    if isinstance(candidate, Mapping):
                        mapped = candidate
                        break
            if not isinstance(mapped, Mapping) and hasattr(values, "keys") and hasattr(values, "get"):
                try:
                    mapped = {key: values.get(key) for key in values.keys()}
                except Exception:
                    mapped = {}
        self.values: dict[str, Any] = dict(mapped) if isinstance(mapped, Mapping) else {}

    def raw(self, key: str) -> Any:
        if key in self.values:
            return self.values[key]
        return DEFAULTS.get(key)

    # Mapping-like compatibility so callers that expect the injected config
    # object to still behave like a dict are not surprised.
    def get(self, key: str, default: Any = None) -> Any:
        if key in self.values:
            return self.values[key]
        return default

    def __getitem__(self, key: str) -> Any:
        if key in self.values:
            return self.values[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in self.values or key in DEFAULTS

    def __iter__(self):
        return iter({**DEFAULTS, **self.values})

    def __len__(self) -> int:
        return len({**DEFAULTS, **self.values})

    def keys(self):
        return {**DEFAULTS, **self.values}.keys()

    def items(self):
        return {**DEFAULTS, **self.values}.items()

    def values_list(self):
        return list(self.values.values())

    def bool(self, key: str) -> bool:
        value = self.raw(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().casefold()
            if lowered in _TRUE_VALUES:
                return True
            if lowered in _FALSE_VALUES:
                return False
        return bool(DEFAULTS.get(key, False))

    def int(
        self,
        key: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        default: int | None = None,
    ) -> int:
        fallback = default if default is not None else DEFAULTS.get(key, 0)
        try:
            base = int(fallback)
        except (TypeError, ValueError):
            base = 0
        try:
            value = int(self.raw(key))
        except (TypeError, ValueError):
            value = base
        if minimum is not None:
            value = max(int(minimum), value)
        if maximum is not None:
            value = min(int(maximum), value)
        return value

    def float(
        self,
        key: str,
        *,
        minimum: float = 0.0,
        maximum: float = 1.0,
    ) -> float:
        fallback = DEFAULTS.get(key, 0.0)
        try:
            base = float(fallback)
        except (TypeError, ValueError):
            base = minimum
        try:
            value = float(self.raw(key))
        except (TypeError, ValueError):
            value = base
        return max(minimum, min(maximum, value))

    def string(self, key: str) -> str:
        value = self.raw(key)
        if value is None:
            value = DEFAULTS.get(key, "")
        return str(value)

    def string_list(self, key: str) -> list[str]:
        value = self.raw(key)
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        return [item.strip() for item in text.split(",") if item.strip()]
