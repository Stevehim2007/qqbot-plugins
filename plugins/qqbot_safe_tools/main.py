"""Sole owner of ``[[tool:...]]`` marker parsing, permissioning and dispatch.

The AstrBot core-reply plugin only marks a generated result as a candidate.  A
decorating hook in this plugin consumes that candidate exactly once, strips the
private marker, and invokes one declared ``tool_*`` method on another installed
plugin.  No model calls, dynamic code execution or shell access occur here.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot supplies this module in normal operation and in tests.
    import astrbot.api.message_components as Comp
except ImportError:  # Keep import failure bounded; dispatch will still fail safe.
    Comp = None  # type: ignore[assignment]

try:  # Loaded as an AstrBot package in production.
    from . import safe_tools
except ImportError:  # Loaded directly by a small test harness.
    import safe_tools  # type: ignore[no-redef]

_PROCESSED_EXTRA = "qqbot_safe_tools_processed"
_CANDIDATE_EXTRA = "qqbot_core_reply_candidate"
_ALLOW_STICKER_EXTRA = "qqbot_core_reply_allow_sticker"

_PLUGIN_METHODS: dict[str, tuple[str, str]] = {
    "fetch_webpage": ("qqbot_web_reader", "tool_fetch_webpage"),
    "search_images": ("qqbot_image_search", "tool_search_images"),
    "search_food_restaurants": ("qqbot_food", "tool_search_restaurants"),
    "submit_food_review": ("qqbot_food", "tool_submit_food_review"),
    "send_sticker": ("qqbot_stickers", "tool_send_sticker"),
    "collect_sticker": ("qqbot_stickers", "tool_collect_sticker"),
    "list_sticker_categories": ("qqbot_stickers", "tool_list_categories"),
}

_ALLOW_KEYS: dict[str, str] = {
    "fetch_webpage": "allow_fetch_webpage",
    "search_images": "allow_search_images",
    "search_food_restaurants": "allow_search_food_restaurants",
    "submit_food_review": "allow_submit_food_review",
    "send_sticker": "allow_send_sticker",
    "collect_sticker": "allow_collect_sticker",
    "list_sticker_categories": "allow_list_categories",
}

_COOLDOWN_KEYS: dict[str, str] = {
    "fetch_webpage": "web_fetch_cooldown_seconds",
    "search_images": "image_search_cooldown_seconds",
    "search_food_restaurants": "food_search_cooldown_seconds",
    "submit_food_review": "food_submit_cooldown_seconds",
    "send_sticker": "sticker_cooldown_seconds",
    "collect_sticker": "collect_sticker_cooldown_seconds",
    "list_sticker_categories": "list_categories_cooldown_seconds",
}
_COOLDOWN_DEFAULTS: dict[str, int] = {
    "fetch_webpage": 30,
    "search_images": 10,
    "search_food_restaurants": 10,
    "submit_food_review": 60,
    "send_sticker": 20,
    "collect_sticker": 30,
    "list_sticker_categories": 5,
}

_FALLBACK_DEFAULTS: dict[str, str] = {
    "disabled": "工具协议已关闭，本次调用已忽略。",
    "marker_disabled": "工具标记功能已关闭，本次调用已忽略。",
    "too_many": "每条回复最多只能使用一个工具，本次调用已忽略。",
    "invalid": "工具调用格式无效，本次调用已忽略。",
    "permission": "该工具未启用，本次调用已忽略。",
    "cooldown": "该工具正在冷却，请稍后再试。",
    "unavailable": "所需工具插件暂时不可用，本次调用已忽略。",
    "timeout": "工具执行超时，本次已回退为普通回复。",
    "failed": "工具执行失败，本次已回退为普通回复。",
    "empty": "工具没有返回可用结果，本次已回退为普通回复。",
    "generic": "工具暂时不可用，本次已回退为普通回复。",
}
_FALLBACK_CONFIG_KEYS: dict[str, str] = {
    "disabled": "fallback_disabled",
    "marker_disabled": "fallback_marker_disabled",
    "too_many": "fallback_too_many",
    "invalid": "fallback_invalid",
    "permission": "fallback_permission",
    "cooldown": "fallback_cooldown",
    "unavailable": "fallback_unavailable",
    "timeout": "fallback_timeout",
    "failed": "fallback_failed",
    "empty": "fallback_empty",
    "generic": "fallback_generic",
}


class SafeToolsPlugin(Star):
    """AstrBot plugin object; one instance owns marker parsing for the bot."""

    def __init__(self, context: Context, config: Any = None):
        # AstrBot 4.29 injects per-plugin settings as ``config``.  Do not read
        # context.get_config(): that is the bot-global configuration.
        super().__init__(context)
        self.config = config if config is not None else {}
        self._clock = time.monotonic
        self._last_tool_at: dict[tuple[str, str], float] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Configuration helpers (injected, bounded, never raw exceptions)
    # ------------------------------------------------------------------
    def _config_lookup(self, key: str, default: Any = None) -> Any:
        cfg = getattr(self, "config", {})
        if isinstance(cfg, Mapping):
            return cfg.get(key, default)
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                return default
        try:
            return getattr(cfg, key, default)
        except Exception:
            return default

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._config_lookup(key, default)
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value).strip().casefold()
        if not text:
            return bool(default)
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
        return bool(default)

    def _cfg_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = self._config_lookup(key, default)
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            number = int(default)
        return max(int(minimum), min(int(maximum), number))

    def _cfg_str(self, key: str, default: str, *, max_chars: int = 500) -> str:
        value = self._config_lookup(key, default)
        text = str(value if value is not None else default)
        text = "".join(
            char
            for char in text
            if char in ("\n", "\t") or (ord(char) >= 32 and ord(char) != 127)
        ).strip()
        if not text:
            text = str(default)
        return text[: max(1, int(max_chars))]

    def _configured_max_actions(self) -> int:
        return self._cfg_int("max_actions", 1, minimum=0, maximum=1)

    def _effective_max_actions(self) -> int:
        # The protocol has a hard one-action invariant.  The setting can only
        # lower the limit to zero; values above one are schema-clamped.
        return min(1, max(0, self._configured_max_actions()))

    def _max_keyword_chars(self) -> int:
        return self._cfg_int(
            "max_keyword_chars", 100, minimum=1, maximum=200
        )

    def _max_image_count(self) -> int:
        return self._cfg_int(
            "max_search_count", 1, minimum=1, maximum=5
        )

    def _max_web_chars(self) -> int:
        hard_limit = int(
            getattr(
                safe_tools.web_fetch_engine,
                "WEB_FETCH_MAX_CHARS_HARD_LIMIT",
                8000,
            )
        )
        return self._cfg_int(
            "max_web_chars", 4000, minimum=1, maximum=max(1, hard_limit)
        )

    def _tool_timeout_seconds(self) -> int:
        return self._cfg_int(
            "tool_timeout_seconds", 30, minimum=1, maximum=120
        )

    def _max_fallback_chars(self) -> int:
        return self._cfg_int(
            "max_fallback_chars", 500, minimum=80, maximum=2_000
        )

    def _enabled_tool_names(self) -> list[str]:
        if not self._cfg_bool("enabled", True):
            return []
        if not self._cfg_bool("marker_enabled", True):
            return []
        if self._effective_max_actions() < 1:
            return []
        return [
            name
            for name in safe_tools.ALLOWED_TOOLS
            if self._cfg_bool(_ALLOW_KEYS[name], True)
        ]

    def _permission_message(self, tool_name: str) -> str:
        if not self._cfg_bool("enabled", True):
            return "disabled"
        if not self._cfg_bool("marker_enabled", True):
            return "marker_disabled"
        if self._effective_max_actions() < 1:
            return "marker_disabled"
        if tool_name in safe_tools.ALLOWED_TOOLS:
            return "permission"
        return "invalid"

    def tool_instruction(self, enabled_tools: list[str] | None = None) -> str:
        """Expose marker grammar/catalog for core_reply; no execution."""

        available = self._enabled_tool_names()
        if enabled_tools is not None:
            requested = {str(name or "") for name in enabled_tools}
            available = [name for name in available if name in requested]
        try:
            return safe_tools.tool_instruction(available)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Plugin discovery and stable method dispatch
    # ------------------------------------------------------------------
    def _registered_star(self, plugin_id: str) -> Any | None:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return None
        try:
            metadata = getter(plugin_id)
        except Exception:
            return None
        if metadata is None:
            return None
        if isinstance(metadata, Mapping):
            activated = metadata.get("activated", True)
            star = metadata.get("star_cls")
        else:
            try:
                activated = getattr(metadata, "activated", True)
            except Exception:
                return None
            try:
                star = getattr(metadata, "star_cls", None)
            except Exception:
                return None
        try:
            if not bool(activated):
                return None
        except Exception:
            return None
        return star

    def _stable_method(self, tool_name: str) -> Any | None:
        mapping = _PLUGIN_METHODS.get(tool_name)
        if mapping is None:
            return None
        plugin_id, method_name = mapping
        star = self._registered_star(plugin_id)
        if star is None:
            return None
        try:
            method = getattr(star, method_name, None)
        except Exception:
            return None
        return method if callable(method) else None

    def _plugin_availability(self) -> dict[str, bool]:
        result = {
            "qqbot_web_reader": False,
            "qqbot_image_search": False,
            "qqbot_food": False,
            "qqbot_stickers": False,
        }
        for plugin_id in result:
            star = self._registered_star(plugin_id)
            if star is None:
                result[plugin_id] = False
                continue
            methods = [
                method_name
                for owner, method_name in _PLUGIN_METHODS.values()
                if owner == plugin_id
            ]
            result[plugin_id] = any(
                callable(getattr(star, method_name, None))
                for method_name in methods
            )
        return result

    async def _await_if_needed(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _invoke_stable_method(
        self,
        method: Any,
        event: AstrMessageEvent,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[bool, Any | None, str]:
        timeout = self._tool_timeout_seconds()

        async def invoke() -> Any:
            return await self._await_if_needed(method(event, *args, **kwargs))

        try:
            return True, await asyncio.wait_for(invoke(), timeout=timeout), ""
        except asyncio.TimeoutError:
            return False, None, "timeout"
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never expose exception text, class names or stack traces to QQ.
            return False, None, "failed"

    # ------------------------------------------------------------------
    # Cooldowns and per-session scheduling
    # ------------------------------------------------------------------
    def _event_session_key(self, event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id", "group_id"):
            try:
                value = getattr(event, attr, None)
            except Exception:
                value = None
            if value:
                return str(value)[:200]
        try:
            sender = getattr(event, "sender_id", None)
            if sender:
                return "sender:" + str(sender)[:100]
        except Exception:
            pass
        return "default"

    def _cooldown_remaining(self, event: AstrMessageEvent, tool_name: str) -> int:
        config_key = _COOLDOWN_KEYS.get(tool_name)
        if config_key is None:
            return 0
        seconds = self._cfg_int(
            config_key,
            _COOLDOWN_DEFAULTS.get(tool_name, 0),
            minimum=0,
            maximum=86_400,
        )
        if seconds <= 0:
            return 0
        key = (self._event_session_key(event), tool_name)
        last = self._last_tool_at.get(key)
        if last is None:
            return 0
        remaining = seconds - (self._clock() - last)
        if remaining <= 0:
            return 0
        return max(1, int(remaining + 0.999))

    def _mark_tool_used(self, event: AstrMessageEvent, tool_name: str) -> None:
        config_key = _COOLDOWN_KEYS.get(tool_name)
        if config_key is None:
            return
        if (
            self._cfg_int(
                config_key,
                _COOLDOWN_DEFAULTS.get(tool_name, 0),
                minimum=0,
                maximum=86_400,
            )
            <= 0
        ):
            return
        if len(self._last_tool_at) > 2_048:
            self._last_tool_at.clear()
        self._last_tool_at[(self._event_session_key(event), tool_name)] = (
            self._clock()
        )

    def _session_lock(self, event: AstrMessageEvent) -> asyncio.Lock:
        key = self._event_session_key(event)
        lock = self._session_locks.get(key)
        if lock is None:
            if len(self._session_locks) > 1_024:
                for old_key, old_lock in list(self._session_locks.items()):
                    if not old_lock.locked():
                        self._session_locks.pop(old_key, None)
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    # ------------------------------------------------------------------
    # Intent dispatch
    # ------------------------------------------------------------------
    async def _execute_intent(
        self,
        event: AstrMessageEvent,
        intent: safe_tools.ToolIntent,
        clean_text: str,
    ) -> tuple[bool, Any | None, str]:
        method = self._stable_method(intent.name)
        if method is None:
            return False, None, "unavailable"
        remaining = self._cooldown_remaining(event, intent.name)
        if remaining > 0:
            return False, None, f"cooldown:{remaining}"

        name = intent.name
        args = intent.args
        kwargs: dict[str, Any] = {}
        positional: tuple[Any, ...] = ()
        if name == "fetch_webpage":
            url = str(args.get("url", "") or "").strip()
            requested = 0
            raw_requested = str(args.get("max_chars", "") or "").strip()
            if raw_requested:
                try:
                    requested = int(raw_requested)
                except (TypeError, ValueError):
                    requested = 0
            max_chars = self._max_web_chars()
            if requested > 0:
                max_chars = min(max_chars, requested)
            kwargs = {"url": url, "max_chars": max(1, max_chars)}
        elif name == "search_images":
            keyword = str(args.get("keyword", "") or "").strip()
            try:
                count = int(args.get("count", "1") or "1")
            except (TypeError, ValueError):
                count = 1
            count = max(1, min(count, self._max_image_count()))
            kwargs = {"keyword": keyword, "count": count}
        elif name == "search_food_restaurants":
            positional = (
                {str(key): str(value) for key, value in args.items()},
                str(clean_text),
            )
        elif name == "submit_food_review":
            positional = (
                {str(key): str(value) for key, value in args.items()},
                str(clean_text),
            )
        elif name == "send_sticker":
            kwargs = {
                "category": str(args.get("category", "") or "").strip(),
                "emotion": str(args.get("emotion", "") or "").strip(),
                "tag": str(args.get("tag", "") or "").strip(),
            }
        elif name == "collect_sticker":
            kwargs = {"category": str(args.get("category", "") or "").strip()}
        elif name == "list_sticker_categories":
            kwargs = {}
        else:
            return False, None, "invalid"

        # The timestamp is set before executing so a failing network-backed
        # tool cannot be hammered by a retry storm.  Resolution happens first:
        # a missing plugin must not consume the cooldown.
        self._mark_tool_used(event, name)
        async with self._session_lock(event):
            return await self._invoke_stable_method(
                method,
                event,
                *positional,
                **kwargs,
            )

    async def _handle_parsed_marker(
        self,
        event: AstrMessageEvent,
        parsed: safe_tools.ToolParseResult,
    ) -> tuple[bool, Any | None, str]:
        if not self._cfg_bool("enabled", True):
            return False, None, "disabled"
        if not self._cfg_bool("marker_enabled", True):
            return False, None, "marker_disabled"
        if self._effective_max_actions() < 1:
            return False, None, "marker_disabled"
        if len(parsed.intents) != 1:
            if not parsed.intents:
                return False, None, "invalid"
            return False, None, "too_many"
        intent = parsed.intents[0]
        if not intent.valid:
            return False, None, "invalid"
        if intent.name not in self._enabled_tool_names():
            return False, None, self._permission_message(intent.name)
        return await self._execute_intent(event, intent, parsed.clean_text)

    # ------------------------------------------------------------------
    # Result chain and fallback formatting
    # ------------------------------------------------------------------
    def _get_extra(self, event: AstrMessageEvent, key: str, default: Any = None) -> Any:
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return default
        try:
            return getter(key, default)
        except TypeError:
            try:
                value = getter(key)
                return default if value is None else value
            except Exception:
                return default
        except Exception:
            return default

    def _set_extra(self, event: AstrMessageEvent, key: str, value: Any) -> None:
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            try:
                setter(key, value)
            except Exception:
                pass

    async def _stop_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if not callable(stopper):
            return
        try:
            result = stopper()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    def _is_plain_component(self, component: Any) -> bool:
        plain_type = getattr(Comp, "Plain", None) if Comp is not None else None
        if plain_type is None:
            return component.__class__.__name__ == "Plain"
        return isinstance(component, plain_type)

    def _plain_component(self, text: str) -> Any:
        plain_type = getattr(Comp, "Plain", None) if Comp is not None else None
        if plain_type is None:
            raise RuntimeError("message component API unavailable")
        return plain_type(text)

    def _result_chain(self, result: Any) -> list[Any] | None:
        if result is None:
            return None
        if isinstance(result, (list, tuple)):
            chain = list(result)
        else:
            chain = getattr(result, "chain", None)
            if not isinstance(chain, (list, tuple)):
                if isinstance(result, str):
                    try:
                        return [self._plain_component(result)]
                    except Exception:
                        return None
                return None
            chain = list(chain)
        if not chain:
            return None
        for component in chain:
            if component is None:
                continue
            if self._is_plain_component(component):
                if str(getattr(component, "text", "") or "").strip():
                    return chain
            else:
                return chain
        return None

    def _bounded_plain_text(self, text: Any, *, max_chars: int) -> str:
        value = str(text or "")
        value = "".join(
            char
            for char in value
            if char in ("\n", "\t") or (ord(char) >= 32 and ord(char) != 127)
        )
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = "\n".join(line.rstrip() for line in value.split("\n"))
        value = value.strip()
        return value[: max(0, int(max_chars))].rstrip()

    def _fallback_reason(self, reason: str) -> tuple[str, int | None]:
        seconds: int | None = None
        base = str(reason or "generic")
        if base.startswith("cooldown:"):
            try:
                seconds = max(0, int(base.split(":", 1)[1]))
            except (TypeError, ValueError):
                seconds = None
            base = "cooldown"
        if base not in _FALLBACK_DEFAULTS:
            base = "generic"
        config_key = _FALLBACK_CONFIG_KEYS.get(base, "fallback_generic")
        text = self._cfg_str(
            config_key,
            _FALLBACK_DEFAULTS[base],
            max_chars=self._max_fallback_chars(),
        )
        if seconds is not None:
            text = f"{text}（约 {seconds} 秒后可重试）"
        return text, seconds

    def _build_fallback_text(self, clean_text: str, reason: str) -> str:
        reason_text, _ = self._fallback_reason(reason)
        total_limit = self._max_fallback_chars()
        reason_text = self._bounded_plain_text(reason_text, max_chars=total_limit)
        if not reason_text:
            reason_text = _FALLBACK_DEFAULTS["generic"]
        clean = self._bounded_plain_text(
            clean_text,
            max_chars=max(0, total_limit - len(reason_text) - 1),
        )
        return f"{clean}\n{reason_text}" if clean else reason_text

    # ------------------------------------------------------------------
    # Sole marker-consuming decorating hook
    # ------------------------------------------------------------------
    @filter.on_decorating_result(priority=200)
    async def consume_safe_tools_marker(self, event: AstrMessageEvent):
        """Consume a marker only from a core-reply candidate, exactly once."""

        if self._get_extra(event, _CANDIDATE_EXTRA, False) is not True:
            return
        if self._get_extra(event, _PROCESSED_EXTRA, False) is True:
            return
        try:
            result = event.get_result()
        except Exception:
            return
        chain = getattr(result, "chain", None) if result is not None else None
        if not isinstance(chain, list):
            return

        plain_text = "".join(
            str(getattr(component, "text", "") or "")
            for component in chain
            if self._is_plain_component(component)
        )
        if not plain_text:
            return
        try:
            parsed = safe_tools.parse_tool_markers(
                plain_text,
                max_keyword_chars=self._max_keyword_chars(),
                max_count=self._max_image_count(),
                max_web_chars=self._max_web_chars(),
            )
        except Exception:
            # Any marker-looking text must fail closed even if parsing failed.
            if not safe_tools._LOOSE_MARKER_RE.search(plain_text):  # noqa: SLF001
                return
            parsed = safe_tools.ToolParseResult(
                clean_text=self._bounded_plain_text(
                    safe_tools.strip_tool_markers(plain_text),
                    max_chars=self._max_fallback_chars(),
                ),
                intents=[
                    safe_tools.ToolIntent(
                        name="", args={}, valid=False, error="标记格式无效"
                    )
                ],
                had_marker=True,
            )
        if not parsed.had_marker:
            return

        # Set the terminal handshake before awaiting plugin code so neither this
        # decorator nor the sticker decorator can ever consume the result twice.
        self._set_extra(event, _PROCESSED_EXTRA, True)
        self._set_extra(event, _ALLOW_STICKER_EXTRA, False)
        try:
            ok, tool_result, reason = await self._handle_parsed_marker(
                event, parsed
            )
            replacement = self._result_chain(tool_result) if ok else None
            if ok and replacement is None:
                reason = "empty"
        except asyncio.CancelledError:
            raise
        except Exception:
            replacement = None
            reason = "failed"
        if replacement is None:
            try:
                fallback_text = self._build_fallback_text(parsed.clean_text, reason)
            except Exception:
                fallback_text = _FALLBACK_DEFAULTS["generic"]
            try:
                replacement = [self._plain_component(fallback_text)]
            except Exception:
                # Last-resort chain surgery: the marker text came from a Plain
                # component, so reusing that component guarantees it is removed
                # even if the component API is temporarily unavailable.
                plain_components = [
                    component
                    for component in chain
                    if self._is_plain_component(component)
                ]
                if plain_components:
                    plain_components[0].text = fallback_text
                    replacement = [plain_components[0]]
                else:
                    replacement = []
        chain[:] = replacement
        try:
            await self._stop_event(event)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # /工具协议 diagnostic command
    # ------------------------------------------------------------------
    def _diagnostic_text(self) -> str:
        enabled = self._cfg_bool("enabled", True)
        marker_enabled = self._cfg_bool("marker_enabled", True)
        configured_max = self._cfg_int("max_actions", 1, minimum=0, maximum=1)
        active = self._enabled_tool_names()
        availability = self._plugin_availability()
        state = lambda value: "可用" if value else "缺失"  # noqa: E731
        lines = [
            "安全工具协议（唯一 marker owner）",
            f"总开关：{'开' if enabled else '关'}；"
            f"marker：{'开' if marker_enabled else '关'}；"
            f"最大动作数：当前硬限制 1（配置 {configured_max}）",
            "已启用工具：" + ("、".join(active) if active else "无"),
            "允许开关："
            + "；".join(
                f"{name}={'开' if self._cfg_bool(key, True) else '关'}"
                for name, key in _ALLOW_KEYS.items()
            ),
            "依赖插件："
            + "；".join(
                f"{plugin_id}={state(ready)}"
                for plugin_id, ready in availability.items()
            ),
        ]
        instruction = self.tool_instruction(active)
        if instruction:
            lines.append(instruction)
        else:
            lines.append("当前没有可下发的工具目录。")
        return "\n".join(lines)

    @filter.command("工具协议", priority=110)
    async def status(self, event: AstrMessageEvent):
        try:
            text = self._diagnostic_text()
        except Exception:
            text = "安全工具协议：状态读取失败，诊断命令仍处于安全模式。"
        yield event.plain_result(text)
        await self._stop_event(event)
