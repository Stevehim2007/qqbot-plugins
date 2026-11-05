"""Standalone image-search plugin for AstrBot >= 4.29.

No model calls.  The plugin offers four command spellings (``/图片搜索``,
``/搜图片``, ``/找图``, ``/搜图``), deterministic natural-language detection for
private chats/@ messages, and the stable safe-tools API
``tool_search_images``.  Network parsing/download/validation lives in the
stdlib-only :mod:`image_search` engine.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # Loaded as an AstrBot package.
    from . import image_search
except ImportError:  # Loaded directly by the test suite.
    import image_search  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_image_search"
PLUGIN_VERSION = "2.0.0"
PLUGIN_DISPLAY_NAME = "图片搜索"
COMMAND_NAMES = ("图片搜索", "搜图片", "找图", "搜图")
DEFAULT_COMMAND_NAME = "图片搜索"

DEFAULTS: dict[str, Any] = {
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
    "success_prefix": "为你找到这些图片：",
    "usage_reply": "用法：/图片搜索 关键词（别名 /搜图片；也可用 /找图、/搜图）",
    "disabled_reply": "图片搜索功能当前未启用。",
    "invalid_keyword_reply": "请提供有效的图片搜索关键词。",
    "empty_reply": "没有找到合适的图片，换个关键词试试吧。",
    "error_reply": "搜索图片失败了，请稍后再试。",
    "timeout_reply": "搜索图片超时了，请稍后再试。",
    "cooldown_reply": "图片搜索冷却中，请约 {seconds} 秒后再试。",
}

_COMMAND_PREFIX_RE = re.compile(r"^[/／]\s*")
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


def extract_command_keyword(message: Any) -> str:
    """Extract the keyword from any supported command spelling."""

    text = str(message or "").strip()
    if not text:
        return ""
    text = _COMMAND_PREFIX_RE.sub("", text, count=1).strip()
    if not text:
        return ""
    # Longest names first so /图片搜索 wins before a shorter prefix.
    for name in sorted(COMMAND_NAMES, key=len, reverse=True):
        if text == name:
            return ""
        if text.startswith(name):
            remainder = text[len(name) :]
            if not remainder or remainder[0].isspace() or remainder[0] in "：:":
                return remainder.lstrip(" ：:").strip()
    # The command filter may already have consumed the command token.
    if "/" in text[:1] or "／" in text[:1]:
        return ""
    return text


def _looks_like_command(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value.startswith(("/", "／")):
        return True
    for name in COMMAND_NAMES:
        if value == name or value.startswith(name + " ") or value.startswith(name + "：") or value.startswith(name + ":"):
            return True
    return False


class ImageSearchPlugin(Star):
    """Deterministic image search command + safe-tools API."""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = _as_config_mapping(config)
        self._cooldowns = image_search.CooldownTracker()

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

    def _cfg_float(self, key: str, *, minimum: float, maximum: float) -> float:
        return _config_float(
            self._cfg(key),
            float(DEFAULTS[key]),
            minimum=minimum,
            maximum=maximum,
        )

    def _cfg_str(self, key: str) -> str:
        return _config_str(self._cfg(key), str(DEFAULTS[key]))

    def _result_limit(self) -> int:
        return self._cfg_int("max_results", minimum=1, maximum=5)

    def _options(self) -> image_search.SearchOptions:
        return image_search.SearchOptions(
            result_count=self._result_limit(),
            timeout=self._cfg_int("timeout_seconds", minimum=1, maximum=60),
            max_response_bytes=self._cfg_int(
                "max_response_bytes",
                minimum=1024,
                maximum=image_search.IMAGE_SEARCH_MAX_RESPONSE_BYTES_HARD_LIMIT,
            ),
            max_candidates=self._cfg_int(
                "max_candidates",
                minimum=1,
                maximum=image_search.IMAGE_SEARCH_MAX_CANDIDATE_URLS,
            ),
            max_redirects=self._cfg_int("max_redirects", minimum=0, maximum=5),
            relevance_enabled=self._cfg_bool("relevance_enabled"),
            min_relevance_score=self._cfg_float(
                "min_relevance_score", minimum=0.0, maximum=1.0
            ),
            baidu_fallback_enabled=self._cfg_bool("baidu_fallback_enabled"),
            safe_search=self._cfg_str("safe_search"),
            user_agent=self._cfg_str("user_agent"),
            download_timeout=self._cfg_int(
                "download_timeout_seconds", minimum=1, maximum=120
            ),
            download_max_bytes=self._cfg_int(
                "download_max_bytes",
                minimum=1024,
                maximum=image_search.IMAGE_SEARCH_MAX_DOWNLOAD_BYTES_HARD_LIMIT,
            ),
            download_attempts=self._cfg_int(
                "download_attempts", minimum=1, maximum=15
            ),
        )

    # ------------------------------------------------------------------
    # Message/event helpers
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
        """True for private chats or events that explicitly mention the bot."""

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
                # An At component without a resolvable target is still an
                # explicit mention in the adapters this plugin supports.
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
        max_chars = self._cfg_int("max_keyword_chars", minimum=1, maximum=300)
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
            group_seconds=self._cfg_int("group_cooldown_seconds", minimum=0, maximum=600),
            user_seconds=self._cfg_int("user_cooldown_seconds", minimum=0, maximum=1800),
        )

    # ------------------------------------------------------------------
    # Core search/reply pipeline
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

    async def _build_image_result(
        self,
        event: Any,
        keyword: str,
        *,
        count: int | None = None,
    ):
        """Shared search/download/send pipeline (always returns a result|None)."""

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
        options = self._options()

        try:
            candidates = await self._search_candidates_async(cleaned, options)
        except asyncio.TimeoutError:
            return self._plain(event, self._cfg_str("timeout_reply"))
        except image_search.ImageSearchError as exc:
            key = "timeout_reply" if exc.reason == "timeout" else "error_reply"
            return self._plain(event, self._cfg_str(key))
        except Exception:
            logger.warning("qqbot_image_search search failed", exc_info=True)
            return self._plain(event, self._cfg_str("error_reply"))

        if not candidates:
            return self._plain(event, self._cfg_str("empty_reply"))

        try:
            downloaded, remote_urls = await self._download_images_async(
                candidates,
                count=result_limit,
                options=options,
            )
        except asyncio.TimeoutError:
            downloaded, remote_urls = [], [
                str(candidate.get("url", "") or "")
                for candidate in candidates
                if candidate.get("url")
            ]
        except Exception:
            logger.warning("qqbot_image_search download pipeline failed", exc_info=True)
            downloaded, remote_urls = [], [
                str(candidate.get("url", "") or "")
                for candidate in candidates
                if candidate.get("url")
            ]

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

        # Remote URL fallback: the protocol adapter can load these directly.
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

        chain: list[Any] = []
        prefix = self._cfg_str("success_prefix")
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

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------
    @filter.command(
        DEFAULT_COMMAND_NAME,
        alias={"搜图片", "找图", "搜图"},
        priority=110,
    )
    async def search_images(self, event: AstrMessageEvent):
        """Search and send images: /图片搜索, /搜图片, /找图, /搜图."""

        self._suppress_llm(event)
        try:
            if not self._cfg_bool("enabled"):
                result = self._plain(event, self._cfg_str("disabled_reply"))
                if result is not None:
                    yield result
                return
            keyword = extract_command_keyword(self._message_text(event))
            if not keyword:
                result = self._plain(event, self._cfg_str("usage_reply"))
                if result is not None:
                    yield result
                return
            cleaned = self._clean_keyword(keyword)
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
                    "max_keyword_chars", minimum=1, maximum=300
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
            # Only a real, detected NL request stops the default LLM path.
            if handled:
                self._stop_event(event)

    # ------------------------------------------------------------------
    # Stable safe-tools API
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
