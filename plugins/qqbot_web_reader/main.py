"""Standalone public-web reader plugin for AstrBot >= 4.29.

The plugin owns no model calls, no cookies and no browser.  It exposes one
explicit command (``/网页``, alias ``/读网页``) and the stable safe-tools API
``tool_fetch_webpage``.  The heavy lifting lives in the stdlib-only
:mod:`web_fetch` security engine next to this file.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # Loaded as an AstrBot package.
    from . import web_fetch
except ImportError:  # Loaded directly by the test suite.
    import web_fetch  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_web_reader"
PLUGIN_VERSION = "2.0.0"
PLUGIN_DISPLAY_NAME = "网页读取"
MAX_TOOL_CHARS_HARD_LIMIT = web_fetch.WEB_FETCH_MAX_CHARS_HARD_LIMIT
COMMAND_NAMES = ("网页", "读网页")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "allow_http": False,
    "max_chars": 4000,
    "max_bytes": 262_144,
    "timeout_seconds": 10,
    "max_redirects": 3,
    "user_agent": web_fetch.DEFAULT_USER_AGENT,
    "usage_reply": "用法：/网页 https://example.com（别名 /读网页）",
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
            return {
                key: getter(key, default)
                for key, default in DEFAULTS.items()
            }
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


def extract_command_url(message: Any) -> str:
    """Extract the URL argument from ``/网页``/``/读网页`` command text."""

    text = str(message or "").strip()
    if not text:
        return ""
    match = _URL_RE.search(text)
    if match:
        return match.group(0).rstrip(_TRAILING_URL_CHARS).strip()
    for name in COMMAND_NAMES:
        for prefix in (f"/{name}", name):
            if text.startswith(prefix):
                remainder = text[len(prefix) :].strip()
                if remainder:
                    return remainder.split()[0].rstrip(_TRAILING_URL_CHARS).strip()
    return ""


class WebReaderPlugin(Star):
    """Explicit command + safe-tools webpage reader."""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = _as_config_mapping(config)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _cfg(self, key: str) -> Any:
        if key in self.config:
            return self.config[key]
        return DEFAULTS.get(key)

    def _cfg_bool(self, key: str) -> bool:
        return _config_bool(self._cfg(key), bool(DEFAULTS[key]))

    def _cfg_int(self, key: str, *, minimum: int, maximum: int) -> int:
        return _config_int(
            self._cfg(key),
            int(DEFAULTS[key]),
            minimum=minimum,
            maximum=maximum,
        )

    def _cfg_str(self, key: str) -> str:
        return _config_str(self._cfg(key), str(DEFAULTS[key]))

    def _reply_for_reason(self, reason: str) -> str:
        key = _REASON_REPLY_KEYS.get(str(reason or ""))
        if key:
            return self._cfg_str(key)
        return self._cfg_str("error_reply")

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

    # ------------------------------------------------------------------
    # Core fetch pipeline
    # ------------------------------------------------------------------
    async def _fetch_result(
        self,
        event: Any,
        url: str,
        *,
        max_chars_override: int = 0,
    ):
        """Fetch and format one page.  Always returns a result or ``None``."""

        if not self._cfg_bool("enabled"):
            return self._plain(event, self._cfg_str("disabled_reply"))

        default_chars = self._cfg_int(
            "max_chars",
            minimum=1,
            maximum=MAX_TOOL_CHARS_HARD_LIMIT,
        )
        try:
            requested = int(max_chars_override or 0)
        except (TypeError, ValueError):
            requested = 0
        if requested > 0:
            max_chars = max(1, min(requested, MAX_TOOL_CHARS_HARD_LIMIT))
        else:
            max_chars = default_chars

        timeout = self._cfg_int("timeout_seconds", minimum=1, maximum=60)
        max_bytes = self._cfg_int(
            "max_bytes",
            minimum=1024,
            maximum=web_fetch.WEB_FETCH_MAX_BYTES_HARD_LIMIT,
        )
        max_redirects = self._cfg_int("max_redirects", minimum=0, maximum=5)
        allow_http = self._cfg_bool("allow_http")
        user_agent = self._cfg_str("user_agent")

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
                ),
                timeout=float(timeout) + 2.0,
            )
        except asyncio.TimeoutError:
            return self._plain(event, self._reply_for_reason("timeout"))
        except web_fetch.WebFetchError as exc:
            return self._plain(event, self._reply_for_reason(exc.reason))
        except Exception:
            logger.warning(
                "qqbot_web_reader fetch failed for %s",
                type(url).__name__,
                exc_info=True,
            )
            return self._plain(event, self._cfg_str("error_reply"))

        try:
            return self._plain(event, web_fetch.format_page_result(page))
        except Exception:
            logger.warning("qqbot_web_reader formatting failed", exc_info=True)
            return self._plain(event, self._cfg_str("error_reply"))

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------
    @filter.command("网页", alias={"读网页"}, priority=110)
    async def read_web(self, event: AstrMessageEvent):
        """Read one public HTTPS web page: /网页 <url>, alias /读网页."""

        self._suppress_llm(event)
        try:
            if not self._cfg_bool("enabled"):
                result = self._plain(event, self._cfg_str("disabled_reply"))
                if result is not None:
                    yield result
                return
            url = extract_command_url(getattr(event, "message_str", ""))
            if not url:
                result = self._plain(event, self._cfg_str("usage_reply"))
                if result is not None:
                    yield result
                return
            result = await self._fetch_result(event, url)
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
    # Stable safe-tools API
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
