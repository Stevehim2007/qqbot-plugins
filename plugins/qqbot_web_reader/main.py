"""Standalone public-web reader plugin for AstrBot >= 4.29.

The plugin owns no model calls, no cookies and no browser.  It exposes the
canonical commands ``/网页读取 URL`` and ``/网页读取 下一页`` (aliases
``/网页``, ``/读网页``, ``/web``), the legacy-safe tool API
``tool_fetch_webpage``, and ABI-v1 provider methods ``web_read`` /
``web_next_page``.  Fetching, cursoring and caching stay stdlib-only.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
import uuid
from typing import Any, Mapping

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # Loaded as an AstrBot package.
    from . import contracts, paging, web_fetch
except ImportError:  # Loaded directly by the test suite.
    import contracts  # type: ignore[no-redef]
    import paging  # type: ignore[no-redef]
    import web_fetch  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_web_reader"
PLUGIN_VERSION = "2.0.0"
PLUGIN_DISPLAY_NAME = "网页读取"
MAX_TOOL_CHARS_HARD_LIMIT = web_fetch.WEB_FETCH_MAX_CHARS_HARD_LIMIT
COMMAND_NAMES = ("网页读取", "读网页", "网页", "web")
CAPABILITIES = ("web.read", "web.next_page")
WEB_READ = "web.read"
WEB_NEXT = "web.next_page"

DEFAULTS: dict[str, Any] = {
    # Existing flat keys remain authoritative fallbacks.
    "enabled": True,
    "allow_http": False,
    "max_chars": 4000,
    "max_bytes": 262_144,
    "timeout_seconds": 10,
    "max_redirects": 3,
    "user_agent": web_fetch.DEFAULT_USER_AGENT,
    # Canonical dotted groups (flat schema compatible).
    "commands.aliases": ["网页", "读网页", "web"],
    "fetch.timeout_seconds": 10,
    "fetch.max_bytes": 262_144,
    "fetch.max_redirects": 3,
    "fetch.allowed_hosts": [],
    "fetch.allow_http": False,
    "fetch.user_agent": web_fetch.DEFAULT_USER_AGENT,
    "extract.mode": "text",
    "extract.max_chars": 4000,
    "paging.page_chars": 1200,
    "cache.ttl_seconds": 600,
    "cache.max_entries": 100,
    "policy.policy_id": "default",
    "policy.owner_epoch": 1,
    # User-facing fallback replies.
    "usage_reply": (
        "用法：/网页读取 <URL> 或 /网页读取 下一页"
        "（别名 /网页、/读网页、/web）"
    ),
    "disabled_reply": "网页读取功能当前未启用。",
    "invalid_url_reply": "链接格式不正确，只支持公开 HTTPS 网页链接。",
    "blocked_reply": "这个地址不是公开网页，已拒绝访问。",
    "dns_failed_reply": "无法解析该网页域名，请检查链接。",
    "timeout_reply": "读取网页超时，请稍后再试。",
    "network_reply": "连接网页失败，请稍后再试。",
    "redirect_reply": "网页跳转异常或跳转到了不安全的地址。",
    "http_error_reply": "网页返回了错误状态，无法读取。",
    "too_large_reply": "网页内容超过大小限制，无法读取。",
    "bad_content_type_reply": "该链接不是可读取的文本网页。",
    "bad_encoding_reply": "网页使用了不受支持的压缩格式。",
    "empty_reply": "网页没有可读正文。",
    "error_reply": "读取网页失败，请稍后再试。",
    "no_cursor_reply": "没有可继续的网页分页；请先发送 /网页读取 <URL>。",
    "cursor_invalid_reply": "分页游标无效或已过期，请重新读取网页。",
    "cache_miss_reply": "网页分页缓存已失效，请重新读取网页。",
}

_CANONICAL_TO_LEGACY = {
    "fetch.timeout_seconds": "timeout_seconds",
    "fetch.max_bytes": "max_bytes",
    "fetch.max_redirects": "max_redirects",
    "fetch.allow_http": "allow_http",
    "fetch.user_agent": "user_agent",
    "extract.max_chars": "max_chars",
}
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING_URL_CHARS = "。，、；：！？）】》」』”’\"'.,;:!?)]}>…"
_REASON_REPLY_KEYS = {
    "invalid_url": "invalid_url_reply",
    "blocked_host": "blocked_reply",
    "blocked_ip": "blocked_reply",
    "dns_failed": "dns_failed_reply",
    "network": "network_reply",
    "timeout": "timeout_reply",
    "too_many_redirects": "redirect_reply",
    "bad_redirect": "redirect_reply",
    "http_error": "http_error_reply",
    "too_large": "too_large_reply",
    "bad_content_type": "bad_content_type_reply",
    "bad_encoding": "bad_encoding_reply",
    "empty": "empty_reply",
}
_CONTRACT_REPLY_KEYS = {
    "invalid_args": "invalid_url_reply",
    "permission_denied": "blocked_reply",
    "not_ready": "error_reply",
    "dependency_missing": "error_reply",
    "owner_epoch_mismatch": "error_reply",
    "deadline_expired": "timeout_reply",
    "invalid_cursor": "cursor_invalid_reply",
    "cursor_expired": "cursor_invalid_reply",
    "cursor_actor_mismatch": "cursor_invalid_reply",
    "cursor_scope_mismatch": "cursor_invalid_reply",
    "cursor_target_mismatch": "cursor_invalid_reply",
    "cache_miss": "cache_miss_reply",
}


def _as_config_mapping(config: Any) -> dict[str, Any]:
    """Normalise the config object injected by AstrBot into a plain dict."""

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


def _config_str(value: Any, default: str) -> str:
    text = str(value if value is not None else default).strip()
    return text or str(default)


def _extract_remainder(message: Any) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    text = text.lstrip("/／").strip()
    for name in sorted(COMMAND_NAMES, key=len, reverse=True):
        if text == name:
            return ""
        if text.startswith(name):
            remainder = text[len(name) :]
            if not remainder or remainder[0].isspace() or remainder[0] in "：:":
                return remainder.lstrip(" ：:").strip()
    return text


def parse_command(message: Any) -> tuple[str, str]:
    """Return ``(action, argument)`` for /网页读取 URL or 下一页."""

    remainder = _extract_remainder(message)
    if not remainder:
        return "usage", ""
    lowered = remainder.casefold()
    if lowered in {"下一页", "下页", "next"}:
        return "next", ""
    if lowered.startswith("下一页") or lowered.startswith("下页"):
        return "invalid", ""
    match = _URL_RE.search(remainder)
    if match:
        return "read", match.group(0).rstrip(_TRAILING_URL_CHARS).strip()
    return "read", remainder.split()[0].rstrip(_TRAILING_URL_CHARS).strip()


def extract_command_url(message: Any) -> str:
    """Compatibility helper: return the URL argument, not the paging action."""

    action, argument = parse_command(message)
    return argument if action == "read" else ""


class WebReaderPlugin(Star):
    """Canonical command + ABI-v1 web provider + safe-tools adapter."""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = _as_config_mapping(config)
        self._provider_ready = False
        self._terminated = False
        self._registry = contracts.get_shared_registry(context)
        self._owner_epoch = self._cfg_int(
            "policy.owner_epoch", minimum=0, maximum=2**31 - 1
        )
        self._cache = paging.TTLCache(
            max_entries=self._cfg_int("cache.max_entries", minimum=0, maximum=1000),
            ttl_seconds=self._cfg_int("cache.ttl_seconds", minimum=0, maximum=86400),
        )
        self._cursor = paging.OpaqueCursor(
            ttl_seconds=max(1, self._cfg_int("cache.ttl_seconds", minimum=0, maximum=86400))
        )
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
        default = bool(DEFAULTS.get(key, False))
        return _config_bool(self._cfg(key), default)

    def _cfg_int(self, key: str, *, minimum: int, maximum: int) -> int:
        default = int(DEFAULTS.get(key, minimum))
        return _config_int(self._cfg(key), default, minimum=minimum, maximum=maximum)

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

    def _reply_for_reason(self, reason: str) -> str:
        key = _REASON_REPLY_KEYS.get(str(reason or ""))
        if key:
            return self._cfg_str(key)
        return self._cfg_str("error_reply")

    def _reply_for_contract_error(self, code: str, *, reason: str = "") -> str:
        if reason:
            return self._reply_for_reason(reason)
        key = _CONTRACT_REPLY_KEYS.get(str(code or ""))
        return self._cfg_str(key or "error_reply")

    # ------------------------------------------------------------------
    # Provider lifecycle / ABI v1
    # ------------------------------------------------------------------
    def _register_provider(self) -> None:
        self._registry.register_provider(
            PLUGIN_NAME,
            WEB_READ,
            {"web_read": self.web_read},
            owner_epoch=self._owner_epoch,
        )
        self._registry.register_provider(
            PLUGIN_NAME,
            WEB_NEXT,
            {"web_next_page": self.web_next_page},
            owner_epoch=self._owner_epoch,
        )

    async def initialize(self) -> None:
        """Register (if needed) and mark this provider ready."""

        if self._terminated:
            return
        if not self._registry.has_provider(WEB_READ):
            self._register_provider()
        self._registry.mark_ready(PLUGIN_NAME, list(CAPABILITIES))
        self._provider_ready = True

    async def terminate(self) -> None:
        """Unregister capabilities and drop private caches/cursors."""

        self._terminated = True
        self._provider_ready = False
        try:
            self._registry.unregister_owner(PLUGIN_NAME)
        finally:
            self._cache.clear()
            self._last_cursors.clear()

    def _ensure_local_ready(self) -> bool:
        if self._terminated:
            return False
        if self._provider_ready:
            return True
        try:
            if not self._registry.has_provider(WEB_READ):
                self._register_provider()
            self._registry.mark_ready(PLUGIN_NAME, list(CAPABILITIES))
            self._provider_ready = True
            return True
        except Exception:
            logger.warning("qqbot_web_reader provider readiness failed", exc_info=True)
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
        timeout = self._cfg_int("fetch.timeout_seconds", minimum=1, maximum=60)
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
    # Result helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _plain(event: Any, text: str):
        try:
            method = getattr(event, "plain_result", None)
            if callable(method):
                return method(str(text))
        except Exception:
            logger.debug("qqbot_web_reader plain_result failed", exc_info=True)
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

    def _extract_text(self, page: web_fetch.PageResult) -> str:
        mode = self._cfg_str("extract.mode").strip().lower()
        if mode == "html_text":
            return str(page.text or "")
        return re.sub(r"\s+", " ", str(page.text or "")).strip()

    def _page_chars(self) -> int:
        return self._cfg_int("paging.page_chars", minimum=1, maximum=MAX_TOOL_CHARS_HARD_LIMIT)

    def _cache_ttl(self) -> int:
        return self._cfg_int("cache.ttl_seconds", minimum=0, maximum=86400)

    def _cursor_ttl(self) -> int:
        # Cursors outlive the content TTL so an expired page can still be
        # served with an explicit stale=True flag (or a stable cache_miss).
        return max(60, self._cache_ttl() + 60)

    def _cache_max_entries(self) -> int:
        return self._cfg_int("cache.max_entries", minimum=0, maximum=1000)

    # ------------------------------------------------------------------
    # Core fetch pipeline
    # ------------------------------------------------------------------
    async def _fetch_page_data(
        self,
        url: str,
        *,
        max_chars_override: int = 0,
    ) -> tuple[dict[str, Any] | None, str, str]:
        """Fetch one page and return ``(data, error_code, reason)``."""

        default_chars = self._cfg_int(
            "extract.max_chars",
            minimum=1,
            maximum=MAX_TOOL_CHARS_HARD_LIMIT,
        )
        try:
            requested = int(max_chars_override or 0)
        except (TypeError, ValueError):
            requested = 0
        max_chars = (
            max(1, min(requested, MAX_TOOL_CHARS_HARD_LIMIT))
            if requested > 0
            else default_chars
        )
        timeout = self._cfg_int("fetch.timeout_seconds", minimum=1, maximum=60)
        max_bytes = self._cfg_int(
            "fetch.max_bytes",
            minimum=1024,
            maximum=web_fetch.WEB_FETCH_MAX_BYTES_HARD_LIMIT,
        )
        max_redirects = self._cfg_int("fetch.max_redirects", minimum=0, maximum=5)
        allow_http = self._cfg_bool("fetch.allow_http")
        user_agent = self._cfg_str("fetch.user_agent")
        allowed_hosts = self._cfg_list("fetch.allowed_hosts")

        try:
            page = await asyncio.wait_for(
                asyncio.to_thread(
                    web_fetch.fetch_page_text,
                    url,
                    timeout=float(timeout),
                    max_bytes=max_bytes,
                    max_chars=max_chars,
                    max_redirects=max_redirects,
                    allow_http=allow_http,
                    user_agent=user_agent,
                    allowed_hosts=allowed_hosts,
                ),
                timeout=float(timeout) + 2.0,
            )
        except asyncio.TimeoutError:
            return None, "outcome_unknown", "timeout"
        except web_fetch.WebFetchError as exc:
            return None, self._error_code_for_reason(exc.reason), exc.reason
        except Exception:
            logger.warning(
                "qqbot_web_reader fetch failed for %s",
                type(url).__name__,
                exc_info=True,
            )
            return None, "provider_error", "network"

        text = self._extract_text(page)
        return (
            {
                "url": page.url,
                "title": page.title,
                "text": text,
                "truncated": bool(page.truncated),
                "content_type": page.content_type,
                "policy_id": self._cfg_str("policy.policy_id"),
            },
            "",
            "",
        )

    @staticmethod
    def _error_code_for_reason(reason: str) -> str:
        if reason in {"invalid_url", "too_many_redirects", "bad_redirect"}:
            return "invalid_args"
        if reason in {"blocked_host", "blocked_ip"}:
            return "permission_denied"
        if reason in {"dns_failed", "network", "timeout", "http_error"}:
            return "outcome_unknown"
        if reason in {"too_large", "bad_content_type", "bad_encoding"}:
            return "invalid_args"
        return "provider_error"

    async def _fetch_result(
        self,
        event: Any,
        url: str,
        *,
        max_chars_override: int = 0,
    ):
        """Legacy-safe formatted fetch used by ``tool_fetch_webpage``."""

        if not self._cfg_bool("enabled"):
            return self._plain(event, self._cfg_str("disabled_reply"))
        data, error_code, reason = await self._fetch_page_data(
            url,
            max_chars_override=max_chars_override,
        )
        if data is None:
            if error_code == "provider_error":
                return self._plain(event, self._cfg_str("error_reply"))
            return self._plain(event, self._reply_for_contract_error(error_code, reason=reason))
        page = web_fetch.PageResult(
            data["url"],
            data["title"],
            data["text"],
            bool(data.get("truncated")),
            str(data.get("content_type") or ""),
        )
        return self._plain(event, web_fetch.format_page_result(page))

    # ------------------------------------------------------------------
    # Provider payloads
    # ------------------------------------------------------------------
    def _format_web_data(self, data: Mapping[str, Any]) -> str:
        title = str(data.get("title") or "").strip()
        header = f"网页：{title}" if title else f"网页：{data.get('url', '')}"
        body = str(data.get("text") or "").strip() or "（网页没有可读正文）"
        parts = [header, "", body]
        if data.get("stale"):
            parts.extend(["", "（缓存内容已过期）"])
        if data.get("has_next"):
            parts.extend(
                [
                    "",
                    "（发送 /网页读取 下一页 继续；也可使用 /web 下一页）",
                ]
            )
        return "\n".join(parts)

    async def _web_read_payload(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability = WEB_READ
        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, capability, {}, "invalid_args"
            )
        unknown = set(args) - {"url", "max_chars"}
        if unknown:
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                "invalid_args",
                data={"unknown_args": sorted(unknown)},
            )
        url = str(args.get("url") or "").strip()
        if not url:
            return contracts.error_from_context(
                request_context, capability, args, "invalid_args"
            )
        data, error_code, reason = await self._fetch_page_data(
            url,
            max_chars_override=args.get("max_chars", 0),
        )
        if data is None:
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                error_code,
                data={"reason": reason, "stale": False},
                retryable=error_code == "outcome_unknown",
            )

        full_text = str(data.get("text") or "")
        page_chars = self._page_chars()
        chunk, next_offset, has_next = paging.paginate_text(
            full_text, page_chars, offset=0
        )
        cache_id = ""
        cursor = ""
        stale = False
        max_entries = self._cache_max_entries()
        if has_next and max_entries > 0:
            cache_id = "web_" + secrets.token_urlsafe(12)
            entry = {
                "url": data["url"],
                "title": data["title"],
                "text": full_text,
                "content_type": data["content_type"],
                "truncated": data["truncated"],
                "actor_id": str(request_context.get("actor_id") or ""),
                "scope_key": contracts.scope_key(request_context),
            }
            self._cache.put(cache_id, entry, ttl_seconds=self._cache_ttl())
            cursor = self._cursor.encode(
                {
                    "kind": "web.page",
                    "cache_id": cache_id,
                    "offset": next_offset,
                    "url": data["url"],
                    "actor_id": str(request_context.get("actor_id") or ""),
                    "scope_key": contracts.scope_key(request_context),
                    "page_chars": page_chars,
                },
                ttl_seconds=self._cursor_ttl(),
            )
            actor_key = (
                str(request_context.get("actor_id") or ""),
                contracts.scope_key(request_context),
            )
            if has_next:
                self._last_cursors[actor_key] = cursor
                if len(self._last_cursors) > max(32, max_entries * 2):
                    # Deterministic bounded map: drop oldest insertion.
                    self._last_cursors.pop(next(iter(self._last_cursors)), None)
            else:
                self._last_cursors.pop(actor_key, None)
        result_data = {
            "url": data["url"],
            "title": data["title"],
            "text": chunk,
            "cursor": cursor or None,
            "has_next": has_next,
            "page": 1,
            "stale": stale,
            "cached": bool(cache_id),
            "cache_id": cache_id,
            "total_chars": len(full_text),
            "truncated": bool(data.get("truncated")),
            "content_type": data.get("content_type", ""),
            "policy_id": data.get("policy_id", ""),
        }
        return contracts.result_from_context(
            request_context,
            capability,
            args,
            result_type="web.page",
            data=result_data,
        )

    async def _web_next_payload(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability = WEB_NEXT
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
        if payload.get("kind") != "web.page":
            return contracts.error_from_context(
                request_context, capability, args, "invalid_cursor"
            )
        cache_id = str(payload.get("cache_id") or "")
        entry, stale = self._cache.get(cache_id)
        if not isinstance(entry, dict):
            return contracts.error_from_context(
                request_context,
                capability,
                args,
                "cache_miss",
                data={"stale": True},
                retryable=True,
            )
        if str(entry.get("url", "")) != str(payload.get("url", "")):
            return contracts.error_from_context(
                request_context, capability, args, "cursor_target_mismatch"
            )
        if str(entry.get("actor_id", "")) != str(request_context.get("actor_id", "")):
            return contracts.error_from_context(
                request_context, capability, args, "cursor_actor_mismatch"
            )
        if str(entry.get("scope_key", "")) != contracts.scope_key(request_context):
            return contracts.error_from_context(
                request_context, capability, args, "cursor_scope_mismatch"
            )
        page_chars = _config_int(
            payload.get("page_chars"),
            self._page_chars(),
            minimum=1,
            maximum=MAX_TOOL_CHARS_HARD_LIMIT,
        )
        offset = _config_int(payload.get("offset"), 0, minimum=0, maximum=10**9)
        chunk, next_offset, has_next = paging.paginate_text(
            str(entry.get("text") or ""),
            page_chars,
            offset=offset,
        )
        next_cursor = ""
        if has_next:
            next_cursor = self._cursor.encode(
                {
                    "kind": "web.page",
                    "cache_id": cache_id,
                    "offset": next_offset,
                    "url": entry.get("url", ""),
                    "actor_id": str(request_context.get("actor_id") or ""),
                    "scope_key": contracts.scope_key(request_context),
                    "page_chars": page_chars,
                },
                ttl_seconds=self._cursor_ttl(),
            )
            actor_key = (
                str(request_context.get("actor_id") or ""),
                contracts.scope_key(request_context),
            )
            self._last_cursors[actor_key] = next_cursor
        else:
            actor_key = (
                str(request_context.get("actor_id") or ""),
                contracts.scope_key(request_context),
            )
            self._last_cursors.pop(actor_key, None)
        page_number = 1
        try:
            page_number = max(1, offset // max(1, page_chars) + 1)
        except Exception:
            pass
        data = {
            "url": entry.get("url", ""),
            "title": entry.get("title", ""),
            "text": chunk,
            "cursor": next_cursor or None,
            "has_next": has_next,
            "page": page_number,
            "stale": bool(stale),
            "cached": True,
            "cache_id": cache_id,
            "total_chars": len(str(entry.get("text") or "")),
            "truncated": bool(entry.get("truncated")),
            "content_type": entry.get("content_type", ""),
            "policy_id": self._cfg_str("policy.policy_id"),
        }
        return contracts.result_from_context(
            request_context,
            capability,
            args,
            result_type="web.page",
            data=data,
        )

    # ------------------------------------------------------------------
    # Fixed ABI v1 provider methods
    # ------------------------------------------------------------------
    async def web_read(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """ABI v1 ``web.read`` provider; returns a ToolResult dict."""

        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, WEB_READ, {}, "invalid_args"
            )
        error = self._require_provider(request_context, WEB_READ, args)
        if error is not None:
            return error
        return await self._web_read_payload(request_context, args)

    async def web_next_page(
        self,
        request_context: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """ABI v1 ``web.next_page`` provider; returns a ToolResult dict."""

        if not isinstance(args, Mapping):
            return contracts.error_from_context(
                request_context, WEB_NEXT, {}, "invalid_args"
            )
        error = self._require_provider(request_context, WEB_NEXT, args)
        if error is not None:
            return error
        return await self._web_next_payload(request_context, args)

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------
    def _latest_cursor_for(self, request_context: Mapping[str, Any]) -> str:
        key = (
            str(request_context.get("actor_id") or ""),
            contracts.scope_key(request_context),
        )
        return self._last_cursors.get(key, "")

    @filter.command(
        "网页读取",
        alias={"网页", "读网页", "web"},
        priority=110,
    )
    async def read_web(self, event: AstrMessageEvent):
        """Read a public page or its next cached page."""

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
            action, argument = parse_command(getattr(event, "message_str", ""))
            if action in {"usage", "invalid"}:
                result = self._plain(event, self._cfg_str("usage_reply"))
                if result is not None:
                    yield result
                return
            if action == "next":
                context = self._request_context(event, WEB_NEXT, {"cursor": ""})
                cursor = self._latest_cursor_for(context)
                if not cursor:
                    result = self._plain(event, self._cfg_str("no_cursor_reply"))
                    if result is not None:
                        yield result
                    return
                contract_result = await self._web_next_payload(
                    context, {"cursor": cursor}
                )
                if contract_result.get("status") == "ok":
                    text = self._format_web_data(contract_result.get("data", {}))
                else:
                    text = self._reply_for_contract_error(
                        str(contract_result.get("error_code") or "")
                    )
                result = self._plain(event, text)
                if result is not None:
                    yield result
                return
            context = self._request_context(
                event, WEB_READ, {"url": argument}
            )
            contract_result = await self._web_read_payload(
                context, {"url": argument}
            )
            if contract_result.get("status") == "ok":
                text = self._format_web_data(contract_result.get("data", {}))
            else:
                error_code = str(contract_result.get("error_code") or "")
                reason = str((contract_result.get("data") or {}).get("reason") or "")
                text = self._reply_for_contract_error(error_code, reason=reason)
            result = self._plain(event, text)
            if result is not None:
                yield result
        except Exception:
            logger.exception("qqbot_web_reader command failed")
            result = self._plain(event, self._cfg_str("error_reply"))
            if result is not None:
                yield result
        finally:
            self._stop_event(event)

    # ------------------------------------------------------------------
    # Stable safe-tools API (backward compatible)
    # ------------------------------------------------------------------
    async def tool_fetch_webpage(
        self,
        event: Any,
        *,
        url: str,
        max_chars: int = 0,
    ):
        """Safe-tools entry point.  Returns a result or ``None``; never raises."""

        try:
            if not self._ensure_local_ready():
                return self._plain(event, self._cfg_str("error_reply"))
            return await self._fetch_result(
                event,
                str(url or ""),
                max_chars_override=max_chars,
            )
        except Exception:
            logger.exception("qqbot_web_reader tool_fetch_webpage failed")
            try:
                return self._plain(event, self._cfg_str("error_reply"))
            except Exception:
                return None
