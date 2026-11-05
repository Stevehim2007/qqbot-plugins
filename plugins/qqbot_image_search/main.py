"""Standalone image-search plugin for AstrBot >= 4.29.

No model calls.  Canonical commands are ``/找图 QUERY``, ``/找图 下一页`` and
``/找图 收藏 RESULT_ID`` with aliases ``/搜图``, ``/搜图片``, ``/image`` and the
legacy ``/图片搜索``.  The plugin exposes ABI-v1 provider methods
``image_search``, ``image_next_page`` and ``image_collect_plan`` plus the
backward-compatible safe-tools adapter ``tool_search_images``.

Collection never writes sticker data.  ``image_collect_plan`` resolves the
``sticker.collect_plan`` provider on the shared AstrBot Context and routes an
immutable plan to qqbot_stickers; a missing dependency fails closed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
import uuid
from typing import Any, Mapping

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # Loaded as an AstrBot package.
    from . import contracts, image_search, paging
except ImportError:  # Loaded directly by the test suite.
    import contracts  # type: ignore[no-redef]
    import image_search  # type: ignore[no-redef]
    import paging  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_image_search"
PLUGIN_VERSION = "2.0.0"
PLUGIN_DISPLAY_NAME = "图片搜索"
COMMAND_NAMES = ("图片搜索", "搜图片", "找图", "搜图", "image")
DEFAULT_COMMAND_NAME = "找图"
CAPABILITIES = ("image.search", "image.next_page", "image.collect_plan")
IMAGE_SEARCH = "image.search"
IMAGE_NEXT = "image.next_page"
IMAGE_COLLECT = "image.collect_plan"
STICKER_COLLECT = "sticker.collect_plan"

DEFAULTS: dict[str, Any] = {
    # Old flat keys retained.
    "enabled": True,
    "natural_language_enabled": True,
    "max_results": 3,
    "max_keyword_chars": 100,
    "timeout_seconds": 12,
    "max_response_bytes": 2_000_000,
    "max_candidates": image_search.IMAGE_SEARCH_MAX_CANDIDATE_URLS,
    "max_redirects": image_search.IMAGE_SEARCH_MAX_REDIRECTS,
    "safe_search": "strict",
    "baidu_fallback_enabled": True,
    "relevance_enabled": True,
    "min_relevance_score": 0.6,
    "download_timeout_seconds": 20,
    "download_max_bytes": 8_000_000,
    "download_attempts": 8,
    "group_cooldown_seconds": 15,
    "user_cooldown_seconds": 10,
    "user_agent": image_search.IMAGE_SEARCH_USER_AGENT,
    # Canonical dotted groups (flat schema compatible).
    "commands.aliases": ["搜图", "搜图片", "image", "图片搜索"],
    "search.source_id": "bing",
    "search.safe_mode": "strict",
    "search.result_limit": 3,
    "search.max_keyword_chars": 100,
    "search.timeout_seconds": 12,
    "search.max_response_bytes": 2_000_000,
    "search.max_candidates": image_search.IMAGE_SEARCH_MAX_CANDIDATE_URLS,
    "search.user_agent": image_search.IMAGE_SEARCH_USER_AGENT,
    "media.max_bytes": 8_000_000,
    "media.allowed_types": [
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/avif",
    ],
    "media.download_timeout_seconds": 20,
    "media.download_attempts": 8,
    "paging.page_size": 3,
    "cache.ttl_seconds": 300,
    "cache.max_entries": 100,
    "policy.policy_id": "default",
    "policy.owner_epoch": 1,
    # Replies.
    "success_prefix": "为你找到这些图片：",
    "usage_reply": (
        "用法：/找图 <关键词>、/找图 下一页、/找图 收藏 <RESULT_ID>"
        "（别名 /搜图、/搜图片、/image、/图片搜索）"
    ),
    "disabled_reply": "图片搜索功能当前未启用。",
    "invalid_keyword_reply": "请提供有效的图片搜索关键词。",
    "empty_reply": "没有找到合适的图片，换个关键词试试吧。",
    "error_reply": "搜索图片失败了，请稍后再试。",
    "timeout_reply": "搜索图片超时了，请稍后再试。",
    "cooldown_reply": "图片搜索冷却中，请约 {seconds} 秒后再试。",
    "no_cursor_reply": "没有可继续的搜索结果；请先发送 /找图 <关键词>。",
    "cursor_invalid_reply": "搜索分页游标无效或已过期，请重新搜索。",
    "cache_miss_reply": "搜索结果缓存已失效，请重新搜索。",
    "collect_missing_reply": "收藏需要 qqbot_stickers 服务；当前依赖未就绪，未写入任何数据。",
    "collect_not_found_reply": "找不到该 RESULT_ID；请先搜索并只收藏本次结果。",
    "collect_denied_reply": "该结果不属于当前用户或会话，拒绝收藏。",
    "collect_ok_reply": "已生成收藏计划并交给 qqbot_stickers：{result_id}",
    "collect_error_reply": "生成图片收藏计划失败，未写入任何数据。",
}

_CANONICAL_TO_LEGACY = {
    "search.result_limit": "max_results",
    "search.max_keyword_chars": "max_keyword_chars",
    "search.timeout_seconds": "timeout_seconds",
    "search.max_response_bytes": "max_response_bytes",
    "search.max_candidates": "max_candidates",
    "search.safe_mode": "safe_search",
    "search.user_agent": "user_agent",
    "media.max_bytes": "download_max_bytes",
    "media.download_timeout_seconds": "download_timeout_seconds",
    "media.download_attempts": "download_attempts",
}
_COMMAND_PREFIX_RE = re.compile(r"^[/／]\s*")
_CONTRACT_ERROR_REPLY_KEYS = {
    "invalid_args": "invalid_keyword_reply",
    "permission_denied": "collect_denied_reply",
    "not_ready": "error_reply",
    "dependency_missing": "collect_missing_reply",
    "owner_epoch_mismatch": "error_reply",
    "deadline_expired": "timeout_reply",
    "invalid_cursor": "cursor_invalid_reply",
    "cursor_expired": "cursor_invalid_reply",
    "cursor_actor_mismatch": "cursor_invalid_reply",
    "cursor_scope_mismatch": "cursor_invalid_reply",
    "cursor_target_mismatch": "cursor_invalid_reply",
    "cache_miss": "cache_miss_reply",
    "result_not_found": "collect_not_found_reply",
}


def _as_config_mapping(config: Any) -> dict[str, Any]:
    if config is None:
        return {}
    if isinstance(config, dict):
        return dict(config)
    converter = getattr(config, "to_dict", None)
    if callable(converter):
        try:
            converted = converter()
            if isinstance(converted, dict):
                return dict(converted)
        except Exception:
            pass
    try:
        return {key: config[key] for key in config}  # type: ignore[index]
    except Exception:
        pass
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return {key: getter(key, default) for key, default in DEFAULTS.items()}
        except Exception:
            pass
    return {}


def _config_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return default


def _config_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(parsed, maximum))


def _config_float(
    value: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(minimum, min(parsed, maximum))


def _config_str(value: Any, default: str) -> str:
    text = str(value if value is not None else default).strip()
    return text or str(default)


def _command_remainder(message: Any) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    text = _COMMAND_PREFIX_RE.sub("", text, count=1).strip()
    if not text:
        return ""
    for name in sorted(COMMAND_NAMES, key=len, reverse=True):
        if text == name:
            return ""
        if text.startswith(name):
            remainder = text[len(name) :]
            if not remainder or remainder[0].isspace() or remainder[0] in "：:":
                return remainder.lstrip(" ：:").strip()
    if text[:1] in {"/", "／"}:
        return ""
    return text


def parse_image_command(message: Any) -> tuple[str, str]:
    """Return (search|next|collect|collect_missing|usage, argument)."""

    remainder = _command_remainder(message)
    if not remainder:
        return "usage", ""
    lowered = remainder.casefold()
    if lowered in {"下一页", "下页", "next"}:
        return "next", ""
    if lowered.startswith("下一页") or lowered.startswith("下页"):
        return "invalid", ""
    if remainder == "收藏":
        return "collect_missing", ""
    if remainder.startswith("收藏 ") or remainder.startswith("收藏："):
        result_id = remainder[len("收藏") :].lstrip(" ：").strip()
        return ("collect", result_id) if result_id else ("collect_missing", "")
    return "search", remainder


def extract_command_keyword(message: Any) -> str:
    """Compatibility helper: return only a search keyword."""

    action, argument = parse_image_command(message)
    return argument if action == "search" else ""


def _looks_like_command(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value.startswith(("/", "／")):
        return True
    for name in COMMAND_NAMES:
        if (
            value == name
            or value.startswith(name + " ")
            or value.startswith(name + "：")
            or value.startswith(name + ":")
        ):
            return True
    return False


class ImageSearchPlugin(Star):
    """Deterministic image search command + ABI-v1 image provider."""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = _as_config_mapping(config)
        self._cooldowns = image_search.CooldownTracker()
        self._provider_ready = False
        self._terminated = False
        self._registry = contracts.get_shared_registry(context)
        self._owner_epoch = self._cfg_int(
            "policy.owner_epoch", minimum=0, maximum=2**31 - 1
        )
        cache_ttl = self._cfg_int("cache.ttl_seconds", minimum=0, maximum=86400)
        cache_max = self._cfg_int("cache.max_entries", minimum=0, maximum=1000)
        self._search_cache = paging.TTLCache(
            max_entries=cache_max, ttl_seconds=cache_ttl
        )
        self._result_cache = paging.TTLCache(
            max_entries=cache_max, ttl_seconds=cache_ttl
        )
        self._cursor = paging.OpaqueCursor(ttl_seconds=max(1, cache_ttl))
        self._result_secret = secrets.token_bytes(32)
        self._last_cursors: dict[tuple[str, str], str] = {}
        self._register_provider()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _cfg(self, key: str) -> Any:
        if key in self.config:
            return self.config[key]
        legacy = _CANONICAL_TO_LEGACY.get(key)
        if legacy and legacy in self.config:
            return self.config[legacy]
        if key in DEFAULTS:
            return DEFAULTS[key]
        if legacy and legacy in DEFAULTS:
            return DEFAULTS[legacy]
        return None

    def _cfg_bool(self, key: str) -> bool:
        return _config_bool(self._cfg(key), bool(DEFAULTS.get(key, False)))

    def _cfg_int(self, key: str, *, minimum: int, maximum: int) -> int:
        return _config_int(
            self._cfg(key),
            int(DEFAULTS.get(key, minimum)),
            minimum=minimum,
            maximum=maximum,
        )

    def _cfg_float(self, key: str, *, minimum: float, maximum: float) -> float:
        return _config_float(
            self._cfg(key),
            float(DEFAULTS.get(key, minimum)),
            minimum=minimum,
            maximum=maximum,
        )

    def _cfg_str(self, key: str) -> str:
        return _config_str(self._cfg(key), str(DEFAULTS.get(key, "")))

    def _cfg_list(self, key: str) -> list[str]:
        value = self._cfg(key)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        default = DEFAULTS.get(key, [])
        return [str(item) for item in default] if isinstance(default, list) else []

    def _result_limit(self) -> int:
        return self._cfg_int("search.result_limit", minimum=1, maximum=5)

    def _page_size(self) -> int:
        return max(
            1,
            min(
                self._cfg_int("paging.page_size", minimum=1, maximum=10),
                self._result_limit(),
            ),
        )

    def _cache_ttl(self) -> int:
        return self._cfg_int("cache.ttl_seconds", minimum=0, maximum=86400)

    def _cursor_ttl(self) -> int:
        return max(60, self._cache_ttl() + 60)

    def _cache_max_entries(self) -> int:
        return self._cfg_int("cache.max_entries", minimum=0, maximum=1000)

    def _options(self) -> image_search.SearchOptions:
        return image_search.SearchOptions(
            result_count=self._cfg_int(
                "search.result_limit", minimum=1, maximum=5
            ),
            timeout=self._cfg_int("search.timeout_seconds", minimum=1, maximum=60),
            max_response_bytes=self._cfg_int(
                "search.max_response_bytes",
                minimum=1024,
                maximum=image_search.IMAGE_SEARCH_MAX_RESPONSE_BYTES_HARD_LIMIT,
            ),
            max_candidates=self._cfg_int(
                "search.max_candidates",
                minimum=1,
                maximum=image_search.IMAGE_SEARCH_MAX_CANDIDATE_URLS,
            ),
            max_redirects=self._cfg_int("max_redirects", minimum=0, maximum=5),
            relevance_enabled=self._cfg_bool("relevance_enabled"),
            min_relevance_score=self._cfg_float(
                "min_relevance_score", minimum=0.0, maximum=1.0
            ),
            baidu_fallback_enabled=self._cfg_bool("baidu_fallback_enabled"),
            safe_search=self._cfg_str("search.safe_mode"),
            user_agent=self._cfg_str("search.user_agent"),
            download_timeout=self._cfg_int(
                "media.download_timeout_seconds", minimum=1, maximum=120
            ),
            download_max_bytes=self._cfg_int(
                "media.max_bytes",
                minimum=1024,
                maximum=image_search.IMAGE_SEARCH_MAX_DOWNLOAD_BYTES_HARD_LIMIT,
            ),
            download_attempts=self._cfg_int(
                "media.download_attempts", minimum=1, maximum=15
            ),
            allowed_types=tuple(self._cfg_list("media.allowed_types")),
        )

    # ------------------------------------------------------------------
    # Provider lifecycle / ABI v1
    # ------------------------------------------------------------------
    def _register_provider(self) -> None:
        self._registry.register_provider(
            PLUGIN_NAME,
            IMAGE_SEARCH,
            {"image_search": self.image_search},
            owner_epoch=self._owner_epoch,
        )
        self._registry.register_provider(
            PLUGIN_NAME,
            IMAGE_NEXT,
            {"image_next_page": self.image_next_page},
            owner_epoch=self._owner_epoch,
        )
        self._registry.register_provider(
            PLUGIN_NAME,
            IMAGE_COLLECT,
            {"image_collect_plan": self.image_collect_plan},
            owner_epoch=self._owner_epoch,
        )

    async def initialize(self) -> None:
        if self._terminated:
            return
        if not self._registry.has_provider(IMAGE_SEARCH):
            self._register_provider()
        self._registry.mark_ready(PLUGIN_NAME, list(CAPABILITIES))
        self._provider_ready = True

    async def terminate(self) -> None:
        self._terminated = True
        self._provider_ready = False
        try:
            self._registry.unregister_owner(PLUGIN_NAME)
        finally:
            self._search_cache.clear()
            self._result_cache.clear()
            self._last_cursors.clear()
            self._cooldowns.clear()

    def _ensure_local_ready(self) -> bool:
        if self._terminated:
            return False
        if self._provider_ready:
            return True
        try:
            if not self._registry.has_provider(IMAGE_SEARCH):
                self._register_provider()
            self._registry.mark_ready(PLUGIN_NAME, list(CAPABILITIES))
            self._provider_ready = True
            return True
        except Exception:
            logger.warning(
                "qqbot_image_search provider readiness failed", exc_info=True
            )
            return False

    def _require_provider(
        self,
        request_context: Mapping[str, Any],
        capability: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        error = contracts.validate_request_context(
            request_context,
            expected_capability=capability,
        )
        if error:
            return contracts.error_from_context(
                request_context, capability, args, error
            )
        if not self._provider_ready:
            return contracts.error_from_context(
                request_context, capability, args, "not_ready"
            )
        try:
            self._registry.resolve(
                capability,
                owner_epoch=int(request_context.get("owner_epoch", -1)),
                require_ready=True,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "provider_error"))
            return contracts.error_from_context(
                request_context, capability, args, code
            )
        return None

    # ------------------------------------------------------------------
    # Request context / scope helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _sender_id(event: Any) -> str:
        for name in ("get_sender_id", "get_user_id"):
            method = getattr(event, name, None)
            if callable(method):
                try:
                    value = method()
                    if value is not None:
                        return str(value)
                except Exception:
                    pass
        return str(getattr(event, "sender_id", "") or "unknown")

    @staticmethod
    def _bot_id(event: Any) -> str:
        for name in ("get_self_id", "get_bot_id"):
            method = getattr(event, name, None)
            if callable(method):
                try:
                    value = method()
                    if value is not None:
                        return str(value)
                except Exception:
                    pass
        return str(getattr(event, "self_id", "") or "bot")

    @staticmethod
    def _platform(event: Any) -> str:
        method = getattr(event, "get_platform_name", None)
        if callable(method):
            try:
                value = method()
                if value:
                    return str(value)
            except Exception:
                pass
        return "aiocqhttp"

    def _scope_for_event(self, event: Any) -> dict[str, Any]:
        try:
            private = bool(event.is_private_chat())
        except Exception:
            private = False
        sender = self._sender_id(event)
        if private:
            scope_type, scope_id = "private", sender
        else:
            try:
                group_id = str(event.get_group_id() or "")
            except Exception:
                group_id = ""
            scope_type, scope_id = "group", group_id or str(
                getattr(event, "unified_msg_origin", "") or "unknown"
            )
        return {
            "type": scope_type,
            "id": scope_id,
            "platform": self._platform(event),
            "bot_id": self._bot_id(event),
            "instance_id": self._cfg_str("policy.instance_id") or "default",
        }

    def _request_context(
        self,
        event: Any,
        capability: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            conversation_id = str(
                getattr(event, "unified_msg_origin", "") or "conversation"
            )
        except Exception:
            conversation_id = "conversation"
        message_obj = getattr(event, "message_obj", None)
        source_message_id = str(
            getattr(message_obj, "message_id", "")
            or getattr(event, "message_id", "")
            or f"cmd-{uuid.uuid4().hex}"
        )
        timeout = self._cfg_int("search.timeout_seconds", minimum=1, maximum=60)
        return contracts.make_request_context(
            instance_id=self._cfg_str("policy.instance_id") or "default",
            platform=self._platform(event),
            bot_id=self._bot_id(event),
            actor_id=self._sender_id(event),
            conversation_id=conversation_id,
            persona_id=self._cfg_str("policy.persona_id") or "",
            source_message_id=source_message_id,
            turn_id=f"turn-{uuid.uuid4().hex}",
            capability=capability,
            requested_scope=self._scope_for_event(event),
            owner_epoch=self._owner_epoch,
            deadline=time.time() + float(timeout) + 15.0,
        )

    # ------------------------------------------------------------------
    # Message/result helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _plain(event: Any, text: str):
        try:
            method = getattr(event, "plain_result", None)
            if callable(method):
                return method(str(text))
        except Exception:
            logger.debug("qqbot_image_search plain_result failed", exc_info=True)
        return None

    @staticmethod
    def _suppress_llm(event: Any) -> None:
        try:
            method = getattr(event, "should_call_llm", None)
            if callable(method):
                method(True)
        except Exception:
            pass

    @staticmethod
    def _stop_event(event: Any) -> None:
        try:
            method = getattr(event, "stop_event", None)
            if callable(method):
                method()
        except Exception:
            pass

    @staticmethod
    def _message_text(event: Any) -> str:
        try:
            return str(getattr(event, "message_str", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _is_explicit_event(event: Any) -> bool:
        try:
            private = getattr(event, "is_private_chat", None)
            if callable(private) and private():
                return True
        except Exception:
            pass
        for attr in ("is_at_or_wake_command", "is_wake", "is_at_bot"):
            value = getattr(event, attr, False)
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = False
            if value:
                return True
        try:
            messages = list(event.get_messages() or [])
        except Exception:
            messages = []
        try:
            self_id = event.get_self_id()
        except Exception:
            self_id = getattr(event, "self_id", None)
        for component in messages:
            name = type(component).__name__.lower()
            if name not in {"at", "atall"}:
                continue
            qq_value = getattr(component, "qq", None)
            if qq_value is None:
                return True
            rendered = str(qq_value)
            if self_id is not None and rendered == str(self_id):
                return True
            if rendered in {"all", "-1", "0"}:
                return True
        return False

    def _clean_keyword(self, raw: Any) -> str:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        value = "".join(char for char in value if char.isprintable()).strip()
        max_chars = self._cfg_int(
            "search.max_keyword_chars", minimum=1, maximum=300
        )
        value = value[:max_chars].strip()
        if not value or not any(char.isalnum() for char in value):
            return ""
        return value

    def _cooldown_reply(self, remaining: int) -> str:
        template = self._cfg_str("cooldown_reply")
        try:
            return template.format(seconds=max(1, int(remaining)))
        except (KeyError, ValueError, IndexError):
            return template

    def _cooldown_remaining(self, event: Any) -> int:
        group_key = str(getattr(event, "unified_msg_origin", "") or "unknown")
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = str(getattr(event, "sender_id", "") or "")
        user_key = f"{group_key}:{sender}"
        return self._cooldowns.consume(
            group_key,
            user_key,
            group_seconds=self._cfg_int(
                "group_cooldown_seconds", minimum=0, maximum=600
            ),
            user_seconds=self._cfg_int(
                "user_cooldown_seconds", minimum=0, maximum=1800
            ),
        )

    def _reply_for_contract_error(self, code: str, *, reason: str = "") -> str:
        if str(code) == "outcome_unknown" and reason == "timeout":
            return self._cfg_str("timeout_reply")
        key = _CONTRACT_ERROR_REPLY_KEYS.get(str(code or ""))
        return self._cfg_str(key or "error_reply")

    @staticmethod
    def _error_code_for_reason(reason: str) -> str:
        if reason in {"timeout", "network", "dns_failed", "http_error"}:
            return "outcome_unknown"
        if reason in {"blocked_host", "blocked_ip"}:
            return "permission_denied"
        if reason in {"invalid_keyword", "invalid_url", "too_large", "bad_content_type", "bad_magic"}:
            return "invalid_args"
        return "provider_error"

    # ------------------------------------------------------------------
    # Search/download pipeline (provider returns data only; command sends)
    # ------------------------------------------------------------------
    async def _search_candidates_async(
        self,
        keyword: str,
        options: image_search.SearchOptions,
    ) -> list[dict[str, Any]]:
        return await asyncio.wait_for(
            asyncio.to_thread(
                image_search.search_image_candidates,
                keyword,
                options,
            ),
            timeout=float(options.timeout) + 3.0,
        )

    async def _download_images_async(
        self,
        candidates: list[dict[str, Any]],
        *,
        count: int,
        options: image_search.SearchOptions,
    ) -> tuple[list[image_search.DownloadedImage], list[str]]:
        attempts = min(
            len(candidates),
            max(1, int(options.download_attempts)),
        )
        budget = float(options.download_timeout) * max(1, attempts) + 5.0
        return await asyncio.wait_for(
            asyncio.to_thread(
                image_search.download_images,
                candidates,
                result_limit=count,
                options=options,
            ),
            timeout=budget,
        )

    def _result_id(
        self,
        *,
        session_id: str,
        query: str,
        actor_id: str,
        scope_key: str,
        index: int,
        url: str,
    ) -> str:
        material = "\x1f".join(
            (session_id, query, actor_id, scope_key, str(index), url)
        ).encode("utf-8")
        digest = hmac.new(self._result_secret, material, hashlib.sha256).hexdigest()
        return "img_" + digest[:24]

    def _summarize_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        session_id: str,
        query: str,
        actor_id: str,
        scope_key: str,
        index: int,
    ) -> dict[str, Any] | None:
        safe_url = image_search.safe_http_url(candidate.get("url"))
        if not safe_url:
            return None
        result_id = self._result_id(
            session_id=session_id,
            query=query,
            actor_id=actor_id,
            scope_key=scope_key,
            index=index,
            url=safe_url,
        )
        return {
            "result_id": result_id,
            "url": safe_url,
            "title": str(candidate.get("title") or ""),
            "description": str(candidate.get("description") or ""),
            "source": str(candidate.get("source") or self._cfg_str("search.source_id")),
            "referer": str(candidate.get("referer") or ""),
        }

    async def _image_search_payload(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability = IMAGE_SEARCH
        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, capability, {}, "invalid_args"
            )
        unknown = set(args) - {"query", "count"}
        if unknown:
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                "invalid_args",
                data={"unknown_args": sorted(unknown)},
            )
        query = self._clean_keyword(args.get("query"))
        if not query:
            return contracts.error_from_context(
                request_context, capability, args, "invalid_args"
            )
        try:
            requested_count = int(args.get("count", 0) or 0)
        except (TypeError, ValueError):
            requested_count = 0
        page_size = self._page_size()
        if requested_count > 0:
            page_size = max(1, min(page_size, requested_count))
        options = self._options()
        try:
            candidates = await self._search_candidates_async(query, options)
        except asyncio.TimeoutError:
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                "outcome_unknown",
                data={"reason": "timeout", "stale": False},
                retryable=True,
            )
        except image_search.ImageSearchError as exc:
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                self._error_code_for_reason(exc.reason),
                data={"reason": exc.reason, "stale": False},
                retryable=exc.reason in {"network", "timeout", "dns_failed"},
            )
        except Exception:
            logger.warning("qqbot_image_search search failed", exc_info=True)
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                "provider_error",
                data={"reason": "unexpected", "stale": False},
            )

        actor_id = str(request_context.get("actor_id") or "")
        scope_key = contracts.scope_key(request_context)
        session_id = "img_" + secrets.token_urlsafe(12)
        summaries: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            summary = self._summarize_candidate(
                candidate,
                session_id=session_id,
                query=query,
                actor_id=actor_id,
                scope_key=scope_key,
                index=index,
            )
            if summary is not None:
                summaries.append(summary)
        cache_ttl = self._cache_ttl()
        cache_max = self._cache_max_entries()
        if cache_max <= 0:
            summaries_page = summaries[:page_size]
            return contracts.result_from_context(
                request_context,
                capability,
                args,
                result_type="image.search_results",
                data={
                    "query": query,
                    "items": summaries_page,
                    "cursor": None,
                    "has_next": False,
                    "page": 1,
                    "stale": False,
                    "cached": False,
                    "cache_id": "",
                    "source": summaries_page[0]["source"] if summaries_page else self._cfg_str("search.source_id"),
                    "policy_id": self._cfg_str("policy.policy_id"),
                },
            )
        cache_id = session_id
        session = {
            "query": query,
            "items": summaries,
            "actor_id": actor_id,
            "scope_key": scope_key,
            "source": summaries[0]["source"] if summaries else self._cfg_str("search.source_id"),
            "page_size": page_size,
        }
        self._search_cache.put(cache_id, session, ttl_seconds=cache_ttl)
        for summary in summaries:
            record = dict(summary)
            record.update(
                {
                    "query": query,
                    "actor_id": actor_id,
                    "scope_key": scope_key,
                    "session_id": session_id,
                }
            )
            self._result_cache.put(summary["result_id"], record, ttl_seconds=cache_ttl)
        page_items = summaries[:page_size]
        has_next = len(summaries) > page_size
        cursor = ""
        if has_next:
            cursor = self._cursor.encode(
                {
                    "kind": "image.page",
                    "cache_id": cache_id,
                    "offset": page_size,
                    "query": query,
                    "actor_id": actor_id,
                    "scope_key": scope_key,
                    "page_size": page_size,
                },
                ttl_seconds=self._cursor_ttl(),
            )
        actor_key = (actor_id, scope_key)
        if has_next:
            self._last_cursors[actor_key] = cursor
            if len(self._last_cursors) > max(32, cache_max * 2):
                self._last_cursors.pop(next(iter(self._last_cursors)), None)
        else:
            self._last_cursors.pop(actor_key, None)
        data = {
            "query": query,
            "items": page_items,
            "cursor": cursor or None,
            "has_next": has_next,
            "page": 1,
            "stale": False,
            "cached": True,
            "cache_id": cache_id,
            "source": session["source"],
            "policy_id": self._cfg_str("policy.policy_id"),
            "allowed_types": self._cfg_list("media.allowed_types"),
        }
        return contracts.result_from_context(
            request_context,
            capability,
            args,
            result_type="image.search_results",
            data=data,
        )

    async def _image_next_page_payload(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability = IMAGE_NEXT
        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, capability, {}, "invalid_args"
            )
        unknown = set(args) - {"cursor"}
        cursor = str(args.get("cursor") or "").strip()
        if unknown or not cursor:
            return contracts.error_from_context(
                request_context, capability, args, "invalid_args"
            )
        try:
            payload = self._cursor.decode(
                cursor,
                actor_id=str(request_context.get("actor_id") or ""),
                scope=request_context.get("requested_scope", {}),
            )
        except paging.CursorError as exc:
            return contracts.error_from_context(
                request_context, capability, args, exc.code
            )
        if payload.get("kind") != "image.page":
            return contracts.error_from_context(
                request_context, capability, args, "invalid_cursor"
            )
        cache_id = str(payload.get("cache_id") or "")
        session, stale = self._search_cache.get(cache_id)
        if not isinstance(session, dict):
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                "cache_miss",
                data={"stale": True},
                retryable=True,
            )
        if str(session.get("actor_id", "")) != str(request_context.get("actor_id", "")):
            return contracts.error_from_context(
                request_context, capability, args, "cursor_actor_mismatch"
            )
        if str(session.get("scope_key", "")) != contracts.scope_key(request_context):
            return contracts.error_from_context(
                request_context, capability, args, "cursor_scope_mismatch"
            )
        if str(session.get("query", "")) != str(payload.get("query", "")):
            return contracts.error_from_context(
                request_context, capability, args, "cursor_target_mismatch"
            )
        page_size = _config_int(
            payload.get("page_size"),
            self._page_size(),
            minimum=1,
            maximum=10,
        )
        offset = _config_int(payload.get("offset"), 0, minimum=0, maximum=10**9)
        items = list(session.get("items") or [])
        page_items = items[offset : offset + page_size]
        next_offset = offset + len(page_items)
        has_next = next_offset < len(items)
        next_cursor = ""
        actor_key = (
            str(request_context.get("actor_id") or ""),
            contracts.scope_key(request_context),
        )
        if has_next:
            next_cursor = self._cursor.encode(
                {
                    "kind": "image.page",
                    "cache_id": cache_id,
                    "offset": next_offset,
                    "query": session.get("query", ""),
                    "actor_id": actor_key[0],
                    "scope_key": actor_key[1],
                    "page_size": page_size,
                },
                ttl_seconds=self._cursor_ttl(),
            )
            self._last_cursors[actor_key] = next_cursor
        else:
            self._last_cursors.pop(actor_key, None)
        page_number = 1
        if page_size > 0:
            page_number = offset // page_size + 1
        data = {
            "query": session.get("query", ""),
            "items": page_items,
            "cursor": next_cursor or None,
            "has_next": has_next,
            "page": page_number,
            "stale": bool(stale),
            "cached": True,
            "cache_id": cache_id,
            "source": session.get("source", ""),
            "policy_id": self._cfg_str("policy.policy_id"),
        }
        return contracts.result_from_context(
            request_context,
            capability,
            args,
            result_type="image.search_results",
            data=data,
        )

    async def _image_collect_plan_payload(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability = IMAGE_COLLECT
        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, capability, {}, "invalid_args"
            )
        unknown = set(args) - {"result_id"}
        result_id = str(args.get("result_id") or "").strip()
        if unknown or not result_id:
            return contracts.error_from_context(
                request_context, capability, args, "invalid_args"
            )
        record, stale = self._result_cache.get(result_id)
        if not isinstance(record, dict):
            return contracts.error_from_context(
                request_context, capability, args, "result_not_found"
            )
        if str(record.get("actor_id", "")) != str(request_context.get("actor_id", "")):
            return contracts.error_from_context(
                request_context, capability, args, "permission_denied"
            )
        if str(record.get("scope_key", "")) != contracts.scope_key(request_context):
            return contracts.error_from_context(
                request_context, capability, args, "permission_denied"
            )
        plan = {
            "action": "collect_image",
            "result_id": result_id,
            "asset_url": record.get("url", ""),
            "source": "qqbot_image_search",
            "source_query": record.get("query", ""),
            "title": record.get("title", ""),
            "description": record.get("description", ""),
            "mime_hint": "",
            "requested_scope": request_context.get("requested_scope", {}),
            "idempotency_key": contracts.args_hash(
                {
                    "result_id": result_id,
                    "actor_id": request_context.get("actor_id"),
                    "scope": request_context.get("requested_scope"),
                }
            ),
            "policy_id": self._cfg_str("policy.policy_id"),
        }
        call_id = contracts.make_call_id(request_context, capability, args)
        try:
            registration = self._registry.resolve(
                STICKER_COLLECT,
                require_ready=True,
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "dependency_missing"))
            return contracts.tool_result_error(
                call_id,
                code,
                data={
                    "routed": False,
                    "downloaded": False,
                    "plan": plan,
                    "stale": bool(stale),
                    "dependency": "qqbot_stickers",
                },
            )
        provider_result = await contracts.call_provider_async(
            self._registry,
            STICKER_COLLECT,
            request_context,
            plan,
            method_names=("sticker_collect_plan", "collect_plan", "provider"),
            owner_epoch=None,
        )
        if provider_result.get("status") != "ok":
            return contracts.tool_result_error(
                call_id,
                str(provider_result.get("error_code") or "provider_error"),
                data={
                    "routed": True,
                    "downloaded": False,
                    "plan": plan,
                    "provider_result": provider_result,
                    "sticker_owner": registration.owner,
                    "stale": bool(stale),
                },
                retryable=bool(provider_result.get("retryable")),
                side_effect_state=str(
                    provider_result.get("side_effect_state") or "none"
                ),
            )
        return contracts.tool_result_ok(
            call_id,
            "image.collect_plan",
            {
                "routed": True,
                "downloaded": False,
                "plan": plan,
                "provider_result": provider_result,
                "sticker_owner": registration.owner,
                "stale": bool(stale),
            },
            side_effect_state="planned",
        )

    # ------------------------------------------------------------------
    # Fixed ABI v1 provider methods
    # ------------------------------------------------------------------
    async def image_search(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """ABI v1 ``image.search`` provider; returns a ToolResult dict."""

        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, IMAGE_SEARCH, {}, "invalid_args"
            )
        error = self._require_provider(request_context, IMAGE_SEARCH, args)
        if error is not None:
            return error
        return await self._image_search_payload(request_context, args)

    async def image_next_page(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """ABI v1 ``image.next_page`` provider; returns a ToolResult dict."""

        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, IMAGE_NEXT, {}, "invalid_args"
            )
        error = self._require_provider(request_context, IMAGE_NEXT, args)
        if error is not None:
            return error
        return await self._image_next_page_payload(request_context, args)

    async def image_collect_plan(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """ABI v1 ``image.collect_plan`` provider; routes to stickers."""

        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, IMAGE_COLLECT, {}, "invalid_args"
            )
        error = self._require_provider(request_context, IMAGE_COLLECT, args)
        if error is not None:
            return error
        return await self._image_collect_plan_payload(request_context, args)

    # ------------------------------------------------------------------
    # Rendering (commands only; provider methods return data)
    # ------------------------------------------------------------------
    async def _render_items_result(
        self,
        event: Any,
        data: Mapping[str, Any],
        *,
        result_limit: int,
    ):
        items = list(data.get("items") or [])
        candidates: list[dict[str, Any]] = []
        for item in items[:result_limit]:
            safe_url = image_search.safe_http_url(item.get("url"))
            if not safe_url:
                continue
            candidates.append(
                {
                    "url": safe_url,
                    "title": str(item.get("title") or ""),
                    "description": str(item.get("description") or ""),
                    "source": str(item.get("source") or "bing"),
                    "referer": str(item.get("referer") or image_search.BING_IMAGES_REFERER),
                }
            )
        if not candidates:
            return self._plain(event, self._cfg_str("empty_reply"))
        options = self._options()
        try:
            downloaded, remote_urls = await self._download_images_async(
                candidates,
                count=result_limit,
                options=options,
            )
        except asyncio.TimeoutError:
            downloaded, remote_urls = [], [item["url"] for item in candidates]
        except Exception:
            logger.warning("qqbot_image_search download pipeline failed", exc_info=True)
            downloaded, remote_urls = [], [item["url"] for item in candidates]
        components: list[Any] = []
        used_sources: set[str] = set()
        image_factory = getattr(Comp, "Image", None)
        for item in downloaded[:result_limit]:
            if len(components) >= result_limit:
                break
            try:
                component = image_factory.fromFileSystem(str(item.path))
            except Exception:
                logger.warning("qqbot_image_search local image component failed", exc_info=True)
                continue
            components.append(component)
            try:
                used_sources.add(image_search.canonical_url(item.source_url))
            except Exception:
                pass
            tracker = getattr(event, "track_temporary_local_file", None)
            if callable(tracker):
                try:
                    tracker(str(item.path))
                except Exception:
                    pass
        if len(components) < result_limit:
            for remote_url in remote_urls:
                if len(components) >= result_limit:
                    break
                safe_remote = image_search.safe_http_url(remote_url)
                if not safe_remote:
                    continue
                try:
                    source_key = image_search.canonical_url(safe_remote)
                except Exception:
                    source_key = safe_remote
                if source_key in used_sources:
                    continue
                try:
                    component = image_factory.fromURL(safe_remote)
                except Exception:
                    logger.warning("qqbot_image_search remote image component failed", exc_info=True)
                    continue
                components.append(component)
                used_sources.add(source_key)
        if not components:
            return self._plain(event, self._cfg_str("empty_reply"))
        prefix = self._cfg_str("success_prefix")
        if data.get("stale"):
            prefix += "\n（缓存结果已过期）"
        if data.get("has_next"):
            prefix += "\n发送 /找图 下一页 查看更多。"
        if items and items[0].get("result_id"):
            prefix += f"\n发送 /找图 收藏 {items[0]['result_id']} 生成收藏计划。"
        chain: list[Any] = []
        plain_factory = getattr(Comp, "Plain", None)
        if prefix and plain_factory is not None:
            try:
                chain.append(plain_factory(prefix))
            except Exception:
                pass
        chain.extend(components)
        try:
            return event.chain_result(chain)
        except Exception:
            logger.warning("qqbot_image_search chain_result failed", exc_info=True)
            return self._plain(event, self._cfg_str("error_reply"))

    async def _build_image_result(
        self,
        event: Any,
        keyword: str,
        *,
        count: int | None = None,
    ):
        """Search -> page -> download -> one chain reply (legacy-safe)."""

        if not self._cfg_bool("enabled"):
            return self._plain(event, self._cfg_str("disabled_reply"))
        cleaned = self._clean_keyword(keyword)
        if not cleaned:
            return self._plain(event, self._cfg_str("invalid_keyword_reply"))
        limit = self._result_limit()
        try:
            requested = int(count) if count is not None else limit
        except (TypeError, ValueError):
            requested = limit
        result_limit = max(1, min(limit, requested))
        request_context = self._request_context(
            event,
            IMAGE_SEARCH,
            {"query": cleaned, "count": result_limit},
        )
        payload = await self._image_search_payload(
            request_context,
            {"query": cleaned, "count": result_limit},
        )
        if payload.get("status") != "ok":
            error_code = str(payload.get("error_code") or "")
            reason = str((payload.get("data") or {}).get("reason") or "")
            return self._plain(
                event, self._reply_for_contract_error(error_code, reason=reason)
            )
        data = payload.get("data") or {}
        if not data.get("items"):
            return self._plain(event, self._cfg_str("empty_reply"))
        return await self._render_items_result(
            event, data, result_limit=result_limit
        )

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------
    def _latest_cursor_for(self, request_context: Mapping[str, Any]) -> str:
        key = (
            str(request_context.get("actor_id") or ""),
            contracts.scope_key(request_context),
        )
        return self._last_cursors.get(key, "")

    def _collect_reply(self, payload: Mapping[str, Any], result_id: str) -> str:
        if payload.get("status") == "ok":
            template = self._cfg_str("collect_ok_reply")
            try:
                return template.format(result_id=result_id)
            except (KeyError, ValueError):
                return template
        code = str(payload.get("error_code") or "")
        data = payload.get("data") or {}
        if code == "dependency_missing" or data.get("routed") is False:
            return self._cfg_str("collect_missing_reply")
        if code == "permission_denied":
            return self._cfg_str("collect_denied_reply")
        if code == "result_not_found":
            return self._cfg_str("collect_not_found_reply")
        return self._cfg_str("collect_error_reply")

    @filter.command(
        DEFAULT_COMMAND_NAME,
        alias={"搜图", "搜图片", "image", "图片搜索"},
        priority=110,
    )
    async def search_images(self, event: AstrMessageEvent):
        """Canonical image command: /找图 QUERY, 下一页, 收藏 RESULT_ID."""

        self._suppress_llm(event)
        try:
            if not self._cfg_bool("enabled"):
                result = self._plain(event, self._cfg_str("disabled_reply"))
                if result is not None:
                    yield result
                return
            if not self._ensure_local_ready():
                result = self._plain(event, self._cfg_str("error_reply"))
                if result is not None:
                    yield result
                return
            action, argument = parse_image_command(self._message_text(event))
            if action in {"usage", "invalid"}:
                result = self._plain(event, self._cfg_str("usage_reply"))
                if result is not None:
                    yield result
                return
            if action == "collect_missing":
                result = self._plain(event, self._cfg_str("collect_not_found_reply"))
                if result is not None:
                    yield result
                return
            if action == "next":
                context = self._request_context(
                    event, IMAGE_NEXT, {"cursor": ""}
                )
                cursor = self._latest_cursor_for(context)
                if not cursor:
                    result = self._plain(event, self._cfg_str("no_cursor_reply"))
                    if result is not None:
                        yield result
                    return
                payload = await self._image_next_page_payload(
                    context, {"cursor": cursor}
                )
                if payload.get("status") == "ok":
                    result = await self._render_items_result(
                        event,
                        payload.get("data") or {},
                        result_limit=self._result_limit(),
                    )
                else:
                    result = self._plain(
                        event,
                        self._reply_for_contract_error(
                            str(payload.get("error_code") or "")
                        ),
                    )
                if result is not None:
                    yield result
                return
            if action == "collect":
                context = self._request_context(
                    event, IMAGE_COLLECT, {"result_id": argument}
                )
                payload = await self._image_collect_plan_payload(
                    context, {"result_id": argument}
                )
                result = self._plain(event, self._collect_reply(payload, argument))
                if result is not None:
                    yield result
                return
            # Fresh search.
            cleaned = self._clean_keyword(argument)
            if not cleaned:
                result = self._plain(event, self._cfg_str("invalid_keyword_reply"))
                if result is not None:
                    yield result
                return
            remaining = self._cooldown_remaining(event)
            if remaining > 0:
                result = self._plain(event, self._cooldown_reply(remaining))
                if result is not None:
                    yield result
                return
            result = await self._build_image_result(
                event,
                cleaned,
                count=self._result_limit(),
            )
            if result is not None:
                yield result
        except Exception:
            logger.exception("qqbot_image_search command failed")
            result = self._plain(event, self._cfg_str("error_reply"))
            if result is not None:
                yield result
        finally:
            self._stop_event(event)

    # ------------------------------------------------------------------
    # Deterministic natural-language entry (private/@ only)
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def on_natural_language(self, event: AstrMessageEvent):
        """Handle explicit @/private requests such as ``帮我找几张猫图``."""

        handled = False
        try:
            if not self._cfg_bool("enabled") or not self._cfg_bool(
                "natural_language_enabled"
            ):
                return
            text = self._message_text(event)
            if _looks_like_command(text):
                return
            if not self._is_explicit_event(event):
                return
            intent = image_search.detect_natural_language_image_search(
                text,
                default_count=self._result_limit(),
                max_keyword_chars=self._cfg_int(
                    "search.max_keyword_chars", minimum=1, maximum=300
                ),
            )
            if intent is None:
                return
            handled = True
            self._suppress_llm(event)
            remaining = self._cooldown_remaining(event)
            if remaining > 0:
                result = self._plain(event, self._cooldown_reply(remaining))
                if result is not None:
                    yield result
                return
            if not self._ensure_local_ready():
                result = self._plain(event, self._cfg_str("error_reply"))
                if result is not None:
                    yield result
                return
            result = await self._build_image_result(
                event,
                str(intent.get("keyword", "")),
                count=int(intent.get("result_count", self._result_limit())),
            )
            if result is not None:
                yield result
        except Exception:
            logger.exception("qqbot_image_search natural-language handler failed")
        finally:
            if handled:
                self._stop_event(event)

    # ------------------------------------------------------------------
    # Stable safe-tools API (backward compatible)
    # ------------------------------------------------------------------
    async def tool_search_images(
        self,
        event: Any,
        *,
        keyword: str,
        count: int = 1,
    ):
        """Safe-tools entry point.  Returns a result or ``None``; never raises."""

        try:
            if not self._ensure_local_ready():
                return self._plain(event, self._cfg_str("error_reply"))
            return await self._build_image_result(
                event,
                str(keyword or ""),
                count=count,
            )
        except Exception:
            logger.exception("qqbot_image_search tool_search_images failed")
            try:
                return self._plain(event, self._cfg_str("error_reply"))
            except Exception:
                return None
