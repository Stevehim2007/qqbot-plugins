"""Sole owner of ``[[tool:...]]`` marker parsing, permissioning and dispatch.

The AstrBot core-reply plugin only marks a generated result as a candidate.  A
decorating hook in this plugin consumes that candidate exactly once, strips the
private marker, and invokes one declared ``tool_*`` method on another installed
plugin.  No model calls, dynamic code execution or shell access occur here.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # AstrBot supplies this module in normal operation and in tests.
    import astrbot.api.message_components as Comp
except ImportError:  # Keep import failure bounded; dispatch will still fail safe.
    Comp = None  # type: ignore[assignment]

try:  # Loaded as an AstrBot package in production.
    from . import (
        auth,
        aux_budget,
        call_ledger,
        capability_policy,
        confirmation,
        contracts,
        control_server,
        dispatcher,
        output_gate,
        registry as service_registry,
        safe_tools,
        tool_registry,
    )
except ImportError:  # Loaded directly by a small test harness.
    import auth  # type: ignore[no-redef]
    import aux_budget  # type: ignore[no-redef]
    import capability_policy  # type: ignore[no-redef]
    import call_ledger  # type: ignore[no-redef]
    import confirmation  # type: ignore[no-redef]
    import contracts  # type: ignore[no-redef]
    import control_server  # type: ignore[no-redef]
    import dispatcher  # type: ignore[no-redef]
    import output_gate  # type: ignore[no-redef]
    import registry as service_registry  # type: ignore[no-redef]
    import safe_tools  # type: ignore[no-redef]
    import tool_registry  # type: ignore[no-redef]

try:  # AstrBot public data-dir helper; optional for direct unit imports.
    from astrbot.api.star import StarTools
except ImportError:
    StarTools = None  # type: ignore[assignment]

_PROCESSED_EXTRA = "qqbot_safe_tools_processed"
logger = logging.getLogger(__name__)
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
    "confirmation_required": "该操作需要预览确认，请先完成确认。",
    "confirmation_expired": "确认凭证已过期，请重新预览。",
    "revision_conflict": "操作内容或版本已变化，请重新预览确认。",
    "unknown": "操作结果不明确，已停止自动重试。",
    "limit": "工具调用次数已达到本轮上限。",
    "generic": "工具暂时不可用，本次已回退为普通回复。",
}
_MARKER_TO_OPERATION: dict[str, str] = {
    "fetch_webpage": "web.read",
    "search_images": "image.search",
    "search_food_restaurants": "food.query",
    "submit_food_review": "food.draft_create",
    "send_sticker": "sticker.send_plan",
    "collect_sticker": "sticker.collect_plan",
    "list_sticker_categories": "category.list",
}

_ERROR_TO_REASON: dict[str, str] = {
    "permission_denied": "permission",
    "not_ready": "unavailable",
    "dependency_missing": "unavailable",
    "confirmation_required": "confirmation_required",
    "confirmation_expired": "confirmation_expired",
    "confirmation_invalid": "invalid",
    "confirmation_replayed": "invalid",
    "revision_conflict": "revision_conflict",
    "idempotency_conflict": "revision_conflict",
    "outcome_unknown": "unknown",
    "deadline_exceeded": "timeout",
    "round_limit_exceeded": "limit",
    "owner_epoch_stale": "permission",
    "abi_mismatch": "unavailable",
    "invalid_args": "invalid",
}

_CONTROL_RPC_ARGUMENTS: dict[str, frozenset[str]] = {
    "policy.snapshot": frozenset(),
    "policy.set": frozenset(
        {
            "plugin_id",
            "group_id",
            "user_id",
            "is_private",
            "enabled",
            "params",
            "expected_revision",
        }
    ),
    "policy.reset": frozenset(
        {
            "plugin_id",
            "group_id",
            "user_id",
            "is_private",
            "expected_revision",
        }
    ),
    "aux_budget.get": frozenset(),
    "aux_budget.set": frozenset({"tier", "enabled", "expected_revision"}),
    "aux_budget.usage": frozenset({"cycle"}),
    "aux_budget.feature_set": frozenset(
        {"feature_id", "enabled", "group_allowlist", "expected_revision"}
    ),
    "aux_budget.cycle_reset": frozenset({"expected_revision"}),
    "tool.catalog": frozenset({"include_unready", "conversation_id", "persona_id"}),
    "tool.dispatch": frozenset(
        {
            "tool_id",
            "args",
            "confirmation_ref",
            "idempotency_key",
            "round_id",
            "conversation_id",
            "persona_id",
        }
    ),
    "confirmation.preview": frozenset(
        {"action", "payload", "revision", "conversation_id", "persona_id"}
    ),
    "confirmation.confirm": frozenset(
        {
            "confirmation_id",
            "expected_payload",
            "expected_revision",
            "conversation_id",
            "persona_id",
        }
    ),
    "confirmation.cancel": frozenset(
        {"confirmation_id", "conversation_id", "persona_id"}
    ),
    "tool.operation_status": frozenset(
        {"operation_id", "conversation_id", "persona_id"}
    ),
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
    "confirmation_required": "fallback_confirmation_required",
    "confirmation_expired": "fallback_confirmation_expired",
    "revision_conflict": "fallback_revision_conflict",
    "unknown": "fallback_outcome_unknown",
    "limit": "fallback_limit",
    "generic": "fallback_generic",
}


class SafeToolsServiceError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


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
        self.registry = service_registry.get_or_create_registry(context)
        try:
            self._authorizer = self._build_authorizer()
        except Exception:
            self._authorizer = auth.Authorizer(auth.AuthorizationPolicy())
        self.output_gate = output_gate.OneShotOutputGate()
        self._ledger: call_ledger.CallLedger | None = None
        self._dispatcher: dispatcher.Dispatcher | None = None
        self._confirmation_manager: confirmation.ConfirmationManager | None = None
        self._confirmation_error_code = ""
        self._control_server: control_server.ControlServer | None = None
        self._control_start_lock = asyncio.Lock()
        self._control_error_code = ""
        self._control_started = False
        self._tool_registry_cache: tool_registry.ToolRegistry | None = None
        self._capability_policy: capability_policy.CapabilityPolicy | None = None
        self._policy_degraded = False
        self._aux_budget_obj: aux_budget.AuxBudget | None = None

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

    def _tool_registry(self) -> tool_registry.ToolRegistry:
        cached = self._tool_registry_cache
        if cached is not None:
            return cached
        stars: Any = ()
        getter = getattr(self.context, "get_all_stars", None)
        if callable(getter):
            try:
                stars = getter() or ()
            except Exception:
                stars = ()
        try:
            cached = tool_registry.ToolRegistry.from_stars(stars)
        except Exception:
            cached = tool_registry.ToolRegistry(tool_registry.builtin_descriptors())
        self._tool_registry_cache = cached
        return cached

    def _tool_descriptor(self, tool_name: str) -> tool_registry.ToolDescriptor | None:
        return self._tool_registry().get(tool_name)

    def _tool_specs(self) -> dict[str, dict[str, Any]]:
        return self._tool_registry().specs()

    def _operation_for(self, tool_name: str) -> str:
        descriptor = self._tool_descriptor(tool_name)
        if descriptor is not None and descriptor.operation:
            return descriptor.operation
        return str(_MARKER_TO_OPERATION.get(tool_name, "") or "")

    def _enabled_tool_names(self) -> list[str]:
        if not self._cfg_bool("enabled", True):
            return []
        marker_allow = self._cfg_list("marker_allowed_tools")
        descriptors = self._tool_registry().all()
        configured_allow = set(marker_allow) if marker_allow else {d.name for d in descriptors}
        return [
            descriptor.name
            for descriptor in descriptors
            if descriptor.name in configured_allow
            and self._cfg_bool(descriptor.allow_key, True)
        ]

    def _permission_message(self, tool_name: str) -> str:
        if not self._cfg_bool("enabled", True):
            return "disabled"
        if not self._cfg_bool("marker_enabled", True):
            return "marker_disabled"
        if self._effective_max_actions() < 1:
            return "marker_disabled"
        if self._tool_descriptor(tool_name) is not None:
            return "permission"
        return "invalid"

    # ------------------------------------------------------------------
    # ABI v1 service setup (all local, no provider imports)
    # ------------------------------------------------------------------
    def _cfg_list(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        value = self._config_lookup(key, None)
        if value is None:
            return tuple(default)
        if isinstance(value, (list, tuple)):
            items = value
        else:
            text = str(value).strip()
            if not text:
                return tuple(default)
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    return tuple(default)
                if not isinstance(parsed, list):
                    return tuple(default)
                items = parsed
            else:
                items = [part.strip() for part in text.split(",")]
        result: list[str] = []
        for item in list(items)[:128]:
            text = str(item or "").strip()
            if not text or len(text) > 256:
                continue
            if text not in result:
                result.append(text)
        return tuple(result)

    def _build_authorizer(self) -> auth.Authorizer:
        raw_rules = self._config_lookup("auth_rules", None)
        if raw_rules is None:
            raw_rules = json.dumps(
                [
                    {
                        "actor_ids": ["*"],
                        "scope_types": ["*"],
                        "operations": ["*"],
                        "allow_read": True,
                        "allow_write": False,
                        "cross_scope": False,
                    }
                ],
                ensure_ascii=False,
            )
        policy_config = {
            "auth_policy_version": self._config_lookup("auth_policy_version", 1),
            "auth_rules": raw_rules,
            "auth_admin_actor_ids": self._cfg_list("auth_admin_actor_ids"),
            "auth_cross_scope_default": self._config_lookup(
                "auth_cross_scope_default", "deny"
            ),
        }
        return auth.Authorizer(auth.policy_from_config(policy_config))

    def _data_dir(self) -> Path:
        try:
            if StarTools is not None:
                raw = StarTools.get_data_dir("qqbot_safe_tools")
                if raw:
                    return Path(str(raw)).expanduser()
        except Exception:
            pass
        return Path.cwd() / ".qqbot_safe_tools_data"

    def _policy(self) -> capability_policy.CapabilityPolicy:
        if self._capability_policy is not None:
            return self._capability_policy
        path = self._data_dir() / "capability_policy.json"
        try:
            self._capability_policy = capability_policy.CapabilityPolicy(path)
            self._policy_degraded = False
        except capability_policy.PolicyError:
            logger.exception("capability policy is invalid; using safe defaults")
            self._capability_policy = capability_policy.CapabilityPolicy(None)
            self._policy_degraded = True
        return self._capability_policy

    def policy_effective(
        self,
        plugin_id: str,
        *,
        group_id: str | None = None,
        user_id: str | None = None,
        is_private: bool = False,
    ) -> dict[str, Any]:
        effective = self._policy().effective(
            plugin_id,
            group_id=group_id,
            user_id=user_id,
            is_private=is_private,
        )
        result = effective.as_dict()
        result["degraded"] = bool(self._policy_degraded)
        return result

    def _aux_budget(self) -> aux_budget.AuxBudget:
        if self._aux_budget_obj is None:
            self._aux_budget_obj = aux_budget.AuxBudget(self._data_dir())
        return self._aux_budget_obj

    def aux_budget_get(self) -> dict[str, Any]:
        ledger = self._aux_budget()
        return {
            "ok": not ledger.degraded,
            "degraded": ledger.degraded,
            "status": ledger.status(),
            "config": ledger.public_config(),
        }

    def aux_budget_set(
        self,
        *,
        tier: str | None = None,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        ledger = self._aux_budget()
        if ledger.degraded:
            return {"ok": False, "error": "store_degraded", "degraded": True}
        try:
            if tier is not None:
                return {"ok": True, "status": ledger.set_tier(tier, expected_revision=expected_revision)}
            if enabled is not None:
                return {"ok": True, "status": ledger.set_enabled(enabled, expected_revision=expected_revision)}
            raise aux_budget.AuxBudgetError("invalid_args", "tier or enabled required")
        except aux_budget.AuxBudgetError as exc:
            return exc.as_dict()

    def aux_budget_usage(self, *, cycle: str | None = None) -> dict[str, Any]:
        return self._aux_budget().usage(cycle=cycle)

    def aux_budget_feature_set(
        self,
        *,
        feature_id: str,
        enabled: bool,
        group_allowlist: list[str] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        ledger = self._aux_budget()
        if ledger.degraded:
            return {"ok": False, "error": "store_degraded", "degraded": True}
        try:
            return {
                "ok": True,
                "status": ledger.set_feature(
                    feature_id,
                    enabled=enabled,
                    group_allowlist=group_allowlist,
                    expected_revision=expected_revision,
                ),
            }
        except aux_budget.AuxBudgetError as exc:
            return exc.as_dict()

    def aux_budget_cycle_reset(self, *, expected_revision: int | None = None) -> dict[str, Any]:
        ledger = self._aux_budget()
        if ledger.degraded:
            return {"ok": False, "error": "store_degraded", "degraded": True}
        try:
            return {"ok": True, "status": ledger.reset_cycle(expected_revision=expected_revision)}
        except aux_budget.AuxBudgetError as exc:
            return exc.as_dict()

    def policy_snapshot(self) -> dict[str, Any]:
        result = self._policy().snapshot()
        result["degraded"] = bool(self._policy_degraded)
        return result

    def policy_set(
        self,
        *,
        plugin_id: str,
        group_id: str | None = None,
        user_id: str | None = None,
        is_private: bool = False,
        enabled: bool = True,
        params: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if self._policy_degraded:
            return {"ok": False, "error": "store_degraded"}
        try:
            effective = self._policy().set_override(
                plugin_id=plugin_id,
                group_id=group_id,
                user_id=user_id,
                is_private=is_private,
                enabled=enabled,
                params=params or {},
                expected_revision=expected_revision,
            )
        except capability_policy.PolicyError as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": str(exc),
                "revision": self._policy().revision,
            }
        return {
            "ok": True,
            "effective": effective.as_dict(),
            "revision": self._policy().revision,
        }

    def policy_reset(
        self,
        *,
        plugin_id: str,
        group_id: str | None = None,
        user_id: str | None = None,
        is_private: bool = False,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if self._policy_degraded:
            return {"ok": False, "error": "store_degraded"}
        try:
            effective = self._policy().reset_override(
                plugin_id=plugin_id,
                group_id=group_id,
                user_id=user_id,
                is_private=is_private,
                expected_revision=expected_revision,
            )
        except capability_policy.PolicyError as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": str(exc),
                "revision": self._policy().revision,
            }
        return {
            "ok": True,
            "effective": effective.as_dict(),
            "revision": self._policy().revision,
        }

    def _ensure_ledger(self) -> call_ledger.CallLedger:
        if self._ledger is not None:
            return self._ledger
        data_dir = self._data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(data_dir, 0o700)
        except OSError:
            pass
        busy_timeout = self._cfg_int(
            "ledger_busy_timeout_ms", 5000, minimum=100, maximum=120000
        )
        self._ledger = call_ledger.CallLedger(
            data_dir / "safe_tools_ledger.sqlite3",
            busy_timeout_ms=busy_timeout,
        )
        return self._ledger

    def _confirmation_secret(self) -> bytes:
        try:
            return control_server.load_control_secret(data_dir=self._data_dir())
        except control_server.ControlConfigError as exc:
            self._confirmation_error_code = exc.code
            raise SafeToolsServiceError("dependency_missing", "confirmation secret unavailable") from exc

    def _ensure_confirmation_manager(self) -> confirmation.ConfirmationManager:
        if self._confirmation_manager is not None:
            return self._confirmation_manager
        secret = self._confirmation_secret()
        ttl = self._cfg_int(
            "confirmation_ttl_seconds", 120, minimum=1, maximum=600
        )
        self._confirmation_manager = confirmation.ConfirmationManager(
            secret,
            ttl_seconds=ttl,
            ledger=self._ensure_ledger(),
            clock=time.time,
        )
        return self._confirmation_manager

    def _ensure_dispatcher(self) -> dispatcher.Dispatcher:
        if self._dispatcher is not None:
            return self._dispatcher
        limits = dispatcher.DispatchLimits(
            deadline_seconds=float(
                self._cfg_int(
                    "dispatch_deadline_seconds", 30, minimum=1, maximum=600
                )
            ),
            max_parallel=self._cfg_int(
                "dispatch_max_parallel", 4, minimum=1, maximum=64
            ),
            max_rounds=self._cfg_int(
                "dispatch_max_rounds", 3, minimum=1, maximum=64
            ),
        )
        try:
            confirmation_manager = self._ensure_confirmation_manager()
        except Exception:
            confirmation_manager = None
        self._dispatcher = dispatcher.Dispatcher(
            self.registry,
            self._authorizer,
            limits=limits,
            ledger=self._ensure_ledger(),
            confirmation_manager=confirmation_manager,
            clock=self._clock,
        )
        return self._dispatcher

    def _abi_mode(self, strict_abi: bool | None = None) -> bool:
        if strict_abi is not None:
            return bool(strict_abi)
        # The bridge registers itself as ``qqbot_safe_tools`` so that native
        # callers can discover it.  That self-registration must not switch
        # marker execution to ABI dispatch: marker providers are ordinary
        # plugin stars and are not registered in this process registry.
        provider_ids = self.registry.provider_ids()
        external_provider = any(
            provider_id != "qqbot_safe_tools" for provider_id in provider_ids
        )
        return bool(
            external_provider
            and self._cfg_int("registry_abi_version", 1, minimum=1, maximum=1)
            == contracts.ABI_VERSION
        )

    def _marker_tool_ready(self, marker_tool: str) -> bool:
        """Marker tools dispatch directly to their provider plugin.

        The service registry only owns the safe-tools bridge provider, so it
        cannot be used to decide whether a marker provider (food, stickers,
        web, image) is loaded.  Resolve the actual provider method instead.
        """
        return self._stable_method(marker_tool) is not None

    def _advertised_tool_names(self) -> list[str]:
        configured = self._enabled_tool_names()
        if not self.registry.has_providers():
            return configured
        advertised = [name for name in configured if self._marker_tool_ready(name)]
        if len(advertised) != len(configured):
            for name in configured:
                if name in advertised:
                    continue
                descriptor = self._tool_descriptor(name)
                plugin_id = descriptor.plugin_id if descriptor is not None else "?"
                method_name = descriptor.method if descriptor is not None else "?"
                star = self._registered_star(plugin_id)
                logger.debug(
                    "safe_tools tool not ready: name=%s plugin=%s method=%s star=%s callable=%s",
                    name,
                    plugin_id,
                    method_name,
                    bool(star),
                    callable(getattr(star, method_name, None)) if star is not None else False,
                )
        return advertised

    def _safe_identity(self, raw: Any, prefix: str, *, length: int = 64) -> str:
        text = str(raw or "").strip()
        if (
            text
            and len(text) <= 200
            and all(
                char.isalnum() or char in "_.:@/-"
                for char in text
            )
        ):
            return text
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        return f"{prefix}_{digest[:length]}"

    def _event_turn_id(self, event: AstrMessageEvent) -> str:
        for key in ("qqbot_core_reply_turn_id", "qqbot_turn_id"):
            value = self._get_extra(event, key, None)
            if value:
                return self._safe_identity(value, "turn")
        return "legacy_" + hashlib.sha256(
            str(id(event)).encode("ascii")
        ).hexdigest()[:24]

    def _event_model_round(self, event: AstrMessageEvent) -> int:
        value = self._get_extra(event, "qqbot_core_reply_model_round", 0)
        if isinstance(value, bool):
            return 0
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, min(number, 10_000))

    def _event_request_context(
        self, event: AstrMessageEvent, capability: str = "tool.process_model_output"
    ) -> contracts.RequestContext:
        sender = None
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                sender = getter()
            except Exception:
                sender = None
        if sender in (None, ""):
            sender = getattr(event, "sender_id", None)
        if sender in (None, ""):
            raise SafeToolsServiceError("permission_denied", "actor is unavailable")
        instance_id = self._safe_identity(
            self._config_lookup("instance_id", None)
            or self._get_extra(event, "qqbot_instance_id", None)
            or "default",
            "instance",
        )
        platform = self._safe_identity(
            self._get_extra(event, "qqbot_platform", None) or "aiocqhttp",
            "platform",
        )
        bot_id = self._safe_identity(
            self._get_extra(event, "qqbot_bot_id", None)
            or getattr(event, "self_id", None)
            or "bot",
            "bot",
        )
        conversation = self._safe_identity(
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session_id", None)
            or getattr(event, "group_id", None)
            or "conversation",
            "conversation",
        )
        persona_id = self._safe_identity(
            self._get_extra(event, "qqbot_persona_id", None) or "default",
            "persona",
        )
        source_message_id = self._safe_identity(
            self._get_extra(event, "qqbot_source_message_id", None)
            or getattr(getattr(event, "message_obj", None), "message_id", None)
            or getattr(event, "message_id", None)
            or "legacy_" + hashlib.sha256(str(id(event)).encode("ascii")).hexdigest()[:20],
            "message",
        )
        requested_scope = str(
            self._get_extra(event, "qqbot_requested_scope", None) or ""
        ).strip()
        if requested_scope not in contracts.SCOPE_TYPES:
            requested_scope = "conversation"
            try:
                if callable(getattr(event, "is_private_chat", None)) and event.is_private_chat():
                    requested_scope = "user"
            except Exception:
                pass
        owner_epoch = self._get_extra(event, "qqbot_owner_epoch", 0)
        if isinstance(owner_epoch, bool) or not isinstance(owner_epoch, int):
            owner_epoch = 0
        deadline = self._clock() + float(
            self._cfg_int("dispatch_deadline_seconds", 30, minimum=1, maximum=600)
        )
        return contracts.RequestContext(
            protocol_version=contracts.ABI_VERSION,
            instance_id=instance_id,
            platform=platform,
            bot_id=bot_id,
            actor_id=self._safe_identity(sender, "actor"),
            conversation_id=conversation,
            persona_id=persona_id,
            source_message_id=source_message_id,
            turn_id=self._event_turn_id(event),
            capability=capability,
            requested_scope=requested_scope,
            owner_epoch=max(0, owner_epoch),
            deadline=deadline,
        )

    def _context_for_operation(
        self, request_context: contracts.RequestContext, operation: str
    ) -> contracts.RequestContext:
        spec = contracts.operation_spec(operation)
        epoch = self.registry.current_owner_epoch(spec.provider_id)
        return replace(
            request_context,
            capability=operation,
            owner_epoch=int(epoch) if epoch is not None else request_context.owner_epoch,
        )

    def tool_instruction(self, enabled_tools: list[str] | None = None) -> str:
        """Expose marker grammar/catalog for core_reply; no execution."""

        if not self._cfg_bool("marker_enabled", True):
            return ""
        if self._effective_max_actions() < 1:
            return ""
        available = self._advertised_tool_names()
        if enabled_tools is not None:
            requested = {str(name or "") for name in enabled_tools}
            available = [name for name in available if name in requested]
        try:
            text = safe_tools.tool_instruction(available, tool_specs=self._tool_specs())
            logger.debug(
                "safe_tools catalog: configured=%d advertised=%d chars=%d tools=%s",
                len(self._enabled_tool_names()),
                len(available),
                len(text or ""),
                ",".join(available),
            )
            return text
        except Exception:
            logger.exception("safe_tools catalog failed")
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
        descriptor = self._tool_descriptor(tool_name)
        if descriptor is not None:
            plugin_id, method_name = descriptor.plugin_id, descriptor.method
        else:
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
        descriptors = self._tool_registry().all()
        plugin_ids = {descriptor.plugin_id for descriptor in descriptors}
        result = {plugin_id: False for plugin_id in sorted(plugin_ids)}
        for descriptor in descriptors:
            star = self._registered_star(descriptor.plugin_id)
            if star is None:
                continue
            if callable(getattr(star, descriptor.method, None)):
                result[descriptor.plugin_id] = True
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
        descriptor = self._tool_descriptor(tool_name)
        config_key = descriptor.cooldown_key if descriptor is not None else _COOLDOWN_KEYS.get(tool_name)
        if config_key is None:
            return 0
        default = descriptor.cooldown_default if descriptor is not None else _COOLDOWN_DEFAULTS.get(tool_name, 0)
        seconds = self._cfg_int(
            config_key,
            default,
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
        descriptor = self._tool_descriptor(tool_name)
        config_key = descriptor.cooldown_key if descriptor is not None else _COOLDOWN_KEYS.get(tool_name)
        if config_key is None:
            return
        default = descriptor.cooldown_default if descriptor is not None else _COOLDOWN_DEFAULTS.get(tool_name, 0)
        if (
            self._cfg_int(
                config_key,
                default,
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
    def _tool_policy_allows(
        self, plugin_id: str, event: AstrMessageEvent | None
    ) -> bool:
        descriptor = None
        for candidate in self._tool_registry().all():
            if candidate.plugin_id == plugin_id:
                descriptor = candidate
                break
        if descriptor is None or event is None:
            return True
        try:
            getter = getattr(self.context, "get_registered_star", None)
            meta = getter("qqbot_safe_tools") if callable(getter) else None
            star = getattr(meta, "star_cls", None) or meta if meta is not None else None
            policy_fn = getattr(star, "policy_effective", None)
            if not callable(policy_fn):
                return True
            private = bool(event.is_private_chat())
            group_id = "" if private else str(event.get_group_id() or "").strip()
            user_id = str(event.get_sender_id() or "").strip()
            policy = policy_fn(
                plugin_id,
                group_id=group_id or None,
                user_id=user_id or None,
                is_private=private,
            )
            return not isinstance(policy, dict) or bool(policy.get("enabled", True))
        except Exception:
            return True

    async def _execute_intent(
        self,
        event: AstrMessageEvent,
        intent: safe_tools.ToolIntent,
        clean_text: str,
    ) -> tuple[bool, Any | None, str]:
        method = self._stable_method(intent.name)
        if method is None:
            return False, None, "unavailable"
        descriptor = self._tool_descriptor(intent.name)
        if descriptor is not None and not self._tool_policy_allows(
            descriptor.plugin_id, event
        ):
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
            descriptor = self._tool_descriptor(name)
            if descriptor is None or descriptor.call_style != "kwargs":
                return False, None, "invalid"
            for key, value in args.items():
                param = descriptor.parameters.get(key)
                if param is not None and param.type in {"integer", "number"}:
                    try:
                        kwargs[key] = int(value) if param.type == "integer" else float(value)
                    except (TypeError, ValueError):
                        return False, None, "invalid"
                elif param is not None and param.type == "boolean":
                    kwargs[key] = str(value).strip().casefold() in {"true", "1"}
                else:
                    kwargs[key] = str(value)

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
    # Single complete-output gate and ABI dispatch
    # ------------------------------------------------------------------
    def _canonical_operation_args(
        self, marker_tool: str, intent: safe_tools.ToolIntent
    ) -> tuple[str, dict[str, Any]]:
        operation = self._operation_for(marker_tool)
        if not operation:
            raise SafeToolsServiceError("invalid_args", "unknown marker tool")
        descriptor = self._tool_descriptor(marker_tool)
        args = dict(intent.args)
        if marker_tool == "fetch_webpage":
            max_chars = str(args.get("max_chars", "") or "").strip()
            canonical: dict[str, Any] = {
                "url": str(args.get("url", "") or "").strip(),
                "max_chars": int(max_chars) if max_chars else 0,
            }
        elif marker_tool == "search_images":
            canonical = {
                "keyword": str(args.get("keyword", "") or "").strip(),
                "count": int(str(args.get("count", "1") or "1")),
            }
        elif marker_tool == "search_food_restaurants":
            canonical = {str(key): str(value) for key, value in args.items()}
            if "limit" in canonical:
                canonical["limit"] = int(canonical["limit"])
        elif marker_tool == "submit_food_review":
            canonical = {str(key): str(value) for key, value in args.items()}
        elif marker_tool == "send_sticker":
            canonical = {
                "category": str(args.get("category", "") or "").strip(),
                "emotion": str(args.get("emotion", "") or "").strip(),
                "tag": str(args.get("tag", "") or "").strip(),
            }
        elif marker_tool == "collect_sticker":
            canonical = {"category": str(args.get("category", "") or "").strip()}
        elif marker_tool == "list_sticker_categories":
            canonical = {}
        else:
            canonical = {}
            params = descriptor.parameters if descriptor is not None else {}
            for key, value in args.items():
                param = params.get(key)
                if param is not None and param.type in {"integer", "number"}:
                    try:
                        canonical[key] = int(value) if param.type == "integer" else float(value)
                    except (TypeError, ValueError):
                        raise SafeToolsServiceError("invalid_args", "invalid argument")
                elif param is not None and param.type == "boolean":
                    canonical[key] = str(value).strip().casefold() in {"true", "1"}
                else:
                    canonical[key] = str(value)
        return operation, canonical

    async def _handle_marker_abi(
        self,
        request_context: contracts.RequestContext,
        parsed: safe_tools.ToolParseResult,
        *,
        model_round: int,
    ) -> tuple[bool, contracts.ToolResult | None, str]:
        if not self._cfg_bool("enabled", True):
            return False, None, "disabled"
        if not self._cfg_bool("marker_enabled", True):
            return False, None, "marker_disabled"
        if self._effective_max_actions() < 1:
            return False, None, "marker_disabled"
        if len(parsed.intents) != 1:
            return False, None, "too_many" if parsed.intents else "invalid"
        intent = parsed.intents[0]
        if not intent.valid:
            return False, None, "invalid"
        enabled = self._enabled_tool_names()
        if intent.name not in enabled:
            return False, None, self._permission_message(intent.name)
        if not self._operation_for(intent.name):
            return False, None, "invalid"
        if not self._marker_tool_ready(intent.name):
            return False, None, "unavailable"
        try:
            operation, canonical_args = self._canonical_operation_args(
                intent.name, intent
            )
            operation_context = self._context_for_operation(
                request_context, operation
            )
            call = contracts.ToolCall.create(
                operation_context, operation, canonical_args
            )
        except (SafeToolsServiceError, contracts.ContractError):
            return False, None, "invalid"
        idempotency_key = (
            f"model:{request_context.turn_id}:{model_round}:"
            f"{operation.replace('.', '_')}:{call.args_hash[:16]}"
        )
        try:
            engine = self._ensure_dispatcher()
            result = await engine.dispatch(
                call,
                round_id=model_round,
                idempotency_key=idempotency_key,
            )
        except Exception:
            return False, None, "failed"
        if result.status in {"success", "ok"}:
            return True, result, ""
        reason = _ERROR_TO_REASON.get(str(result.error_code or ""), "failed")
        return False, result, reason

    def _reply_plan_delivery_key(
        self,
        context: contracts.RequestContext,
        model_round: int,
        status: str,
        call_id: str = "",
    ) -> str:
        digest = hashlib.sha256(
            (
                f"{context.instance_id}:{context.turn_id}:{model_round}:"
                f"{status}:{call_id}"
            ).encode("utf-8")
        ).hexdigest()[:40]
        return f"reply_{digest}"

    def _safe_result_text(self, value: Any, *, limit: int = 8_000) -> str:
        def scrub(item: Any, depth: int = 0) -> Any:
            if depth > 5:
                return "[truncated]"
            if isinstance(item, Mapping):
                out: dict[str, Any] = {}
                for index, (key, child) in enumerate(item.items()):
                    if index >= 32:
                        out["..."] = "[truncated]"
                        break
                    key_text = str(key)[:64]
                    if any(
                        token in key_text.casefold()
                        for token in (
                            "secret",
                            "password",
                            "token",
                            "api_key",
                            "authorization",
                            "cookie",
                        )
                    ):
                        out[key_text] = "[redacted]"
                    else:
                        out[key_text] = scrub(child, depth + 1)
                return out
            if isinstance(item, (list, tuple)):
                return [scrub(child, depth + 1) for child in list(item)[:32]]
            if isinstance(item, str):
                return item[:2_000]
            return item

        if isinstance(value, str):
            return value[:limit]
        try:
            rendered = json.dumps(
                scrub(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return ""
        return rendered[:limit]

    def _plan_from_abi_result(
        self,
        context: contracts.RequestContext,
        result: contracts.ToolResult,
        clean_text: str,
        model_round: int,
    ) -> contracts.ReplyPlan:
        delivery_key = self._reply_plan_delivery_key(
            context, model_round, result.status, result.call_id
        )
        if result.status in {"success", "ok"}:
            data = result.data
            if isinstance(data, contracts.ReplyPlan):
                return data
            if isinstance(data, Mapping) and (
                "text_segments" in data or "media_segments" in data
            ):
                try:
                    plan = contracts.ReplyPlan(
                        request_context=context,
                        text_segments=[
                            str(item)
                            for item in list(data.get("text_segments", []))[:64]
                        ],
                        media_segments=list(data.get("media_segments", []))[:16],
                        terminal=bool(data.get("terminal", True)),
                        delivery_key=str(data.get("delivery_key") or delivery_key),
                    )
                    plan.validate()
                    return plan
                except contracts.ContractError:
                    pass
            text = self._safe_result_text(data)
            if not text and clean_text:
                text = clean_text
            return contracts.ReplyPlan(
                request_context=context,
                text_segments=[text] if text else [],
                media_segments=[],
                terminal=True,
                delivery_key=delivery_key,
            )
        reason = _ERROR_TO_REASON.get(str(result.error_code or ""), "failed")
        fallback_text = self._build_fallback_text(clean_text, reason)
        return contracts.ReplyPlan(
            request_context=context,
            text_segments=[fallback_text] if fallback_text else [],
            media_segments=[],
            terminal=True,
            delivery_key=delivery_key,
        )

    async def _process_output_once(
        self,
        request_context: contracts.RequestContext,
        text: str,
        *,
        event: AstrMessageEvent | None,
        model_round: int,
        strict_abi: bool | None,
    ) -> output_gate.GateOutcome:
        raw_text = str(text or "")
        if not isinstance(text, str):
            raw_text = str(text)
        marker_max_bytes = self._cfg_int(
            "marker_max_bytes", 65_536, minimum=256, maximum=65_536
        )
        oversized = len(raw_text.encode("utf-8", "replace")) > marker_max_bytes
        try:
            parsed = safe_tools.parse_tool_markers(
                raw_text,
                max_keyword_chars=self._max_keyword_chars(),
                max_count=self._max_image_count(),
                max_web_chars=self._max_web_chars(),
                tool_specs=self._tool_specs(),
            )
        except Exception:
            if not safe_tools._LOOSE_MARKER_RE.search(raw_text):  # noqa: SLF001
                return output_gate.GateOutcome(
                    reply_plan=contracts.ReplyPlan.from_text(
                        request_context,
                        raw_text[:marker_max_bytes],
                        delivery_key=self._reply_plan_delivery_key(
                            request_context, model_round, "text"
                        ),
                    ),
                    had_marker=False,
                )
            parsed = safe_tools.ToolParseResult(
                clean_text=self._bounded_plain_text(
                    safe_tools.strip_tool_markers(raw_text[:marker_max_bytes]),
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
            return output_gate.GateOutcome(
                reply_plan=contracts.ReplyPlan.from_text(
                    request_context,
                    raw_text,
                    delivery_key=self._reply_plan_delivery_key(
                        request_context, model_round, "text"
                    ),
                ),
                had_marker=False,
            )
        if oversized:
            fallback_text = self._build_fallback_text(parsed.clean_text, "invalid")
            return output_gate.GateOutcome(
                reply_plan=contracts.ReplyPlan(
                    request_context=request_context,
                    text_segments=[fallback_text],
                    media_segments=[],
                    terminal=True,
                    delivery_key=self._reply_plan_delivery_key(
                        request_context, model_round, "invalid"
                    ),
                ),
                had_marker=True,
                reason="invalid",
            )
        if self._abi_mode(strict_abi):
            ok, result, reason = await self._handle_marker_abi(
                request_context, parsed, model_round=model_round
            )
            if ok and isinstance(result, contracts.ToolResult):
                return output_gate.GateOutcome(
                    reply_plan=self._plan_from_abi_result(
                        request_context, result, parsed.clean_text, model_round
                    ),
                    had_marker=True,
                )
            if isinstance(result, contracts.ToolResult):
                return output_gate.GateOutcome(
                    reply_plan=self._plan_from_abi_result(
                        request_context, result, parsed.clean_text, model_round
                    ),
                    had_marker=True,
                    reason=reason or "failed",
                )
            fallback_text = self._build_fallback_text(parsed.clean_text, reason)
            return output_gate.GateOutcome(
                reply_plan=contracts.ReplyPlan(
                    request_context=request_context,
                    text_segments=[fallback_text],
                    media_segments=[],
                    terminal=True,
                    delivery_key=self._reply_plan_delivery_key(
                        request_context, model_round, "fallback"
                    ),
                ),
                had_marker=True,
                reason=reason,
            )

        if event is None:
            fallback_text = self._build_fallback_text(
                parsed.clean_text, "dependency_missing"
            )
            return output_gate.GateOutcome(
                reply_plan=contracts.ReplyPlan(
                    request_context=request_context,
                    text_segments=[fallback_text],
                    media_segments=[],
                    terminal=True,
                    delivery_key=self._reply_plan_delivery_key(
                        request_context, model_round, "fallback"
                    ),
                ),
                had_marker=True,
                reason="dependency_missing",
            )
        try:
            ok, legacy_result, reason = await self._handle_parsed_marker(
                event, parsed
            )
            legacy_chain = self._result_chain(legacy_result) if ok else None
            if ok and legacy_chain is None:
                reason = "empty"
        except asyncio.CancelledError:
            raise
        except Exception:
            ok, legacy_chain, reason = False, None, "failed"
        if legacy_chain:
            text_segments = [
                str(getattr(component, "text", "") or "")
                for component in legacy_chain
                if self._is_plain_component(component)
                and str(getattr(component, "text", "") or "")
            ]
            media_segments = [
                component
                for component in legacy_chain
                if not self._is_plain_component(component)
            ]
            plan = contracts.ReplyPlan(
                request_context=request_context,
                text_segments=text_segments,
                media_segments=media_segments,
                terminal=True,
                delivery_key=self._reply_plan_delivery_key(
                    request_context, model_round, "success"
                ),
            )
            plan.validate()
            return output_gate.GateOutcome(
                reply_plan=plan,
                had_marker=True,
                legacy_chain=list(legacy_chain),
            )
        fallback_text = self._build_fallback_text(parsed.clean_text, reason)
        return output_gate.GateOutcome(
            reply_plan=contracts.ReplyPlan(
                request_context=request_context,
                text_segments=[fallback_text],
                media_segments=[],
                terminal=True,
                delivery_key=self._reply_plan_delivery_key(
                    request_context, model_round, "fallback"
                ),
            ),
            had_marker=True,
            reason=reason,
        )

    async def _run_output_gate(
        self,
        request_context: contracts.RequestContext,
        text: str,
        *,
        event: AstrMessageEvent | None = None,
        model_round: int = 0,
        strict_abi: bool | None = None,
    ) -> output_gate.GateOutcome:
        request_context.validate()
        fingerprint = self.output_gate.fingerprint(request_context, model_round)
        return await self.output_gate.process(
            fingerprint,
            lambda: self._process_output_once(
                request_context,
                text,
                event=event,
                model_round=model_round,
                strict_abi=strict_abi,
            ),
        )

    async def process_model_output(
        self,
        request_context: contracts.RequestContext,
        text: str,
        *,
        model_round: int = 0,
        event: AstrMessageEvent | None = None,
        strict_abi: bool | None = None,
    ) -> contracts.ReplyPlan:
        """Process one complete model output exactly once for its turn/round."""

        outcome = await self._run_output_gate(
            request_context,
            text,
            event=event,
            model_round=model_round,
            strict_abi=strict_abi,
        )
        return outcome.reply_plan

    async def tool_process_model_output(
        self,
        request_context: contracts.RequestContext,
        text: str,
        *,
        model_round: int = 0,
        event: AstrMessageEvent | None = None,
        strict_abi: bool | None = None,
    ) -> contracts.ReplyPlan:
        return await self.process_model_output(
            request_context,
            text,
            model_round=model_round,
            event=event,
            strict_abi=strict_abi,
        )

    def tool_catalog(
        self,
        request_context: contracts.RequestContext | None = None,
        *,
        include_unready: bool = False,
    ) -> dict[str, Any]:
        if request_context is not None:
            request_context.validate()
        ready_marker_tools = self._advertised_tool_names()
        tools: list[dict[str, Any]] = []
        for marker_tool in ready_marker_tools:
            descriptor = self._tool_descriptor(marker_tool)
            operation = self._operation_for(marker_tool)
            if descriptor is None or not operation:
                continue
            spec = contracts.PROVIDER_OPERATION_SPECS.get(operation)
            if spec is None:
                continue
            tools.append(
                {
                    "marker_tool": marker_tool,
                    "operation": operation,
                    "provider_id": spec.provider_id,
                    "write": spec.write,
                    "requires_confirmation": spec.requires_confirmation,
                }
            )
        providers = self.registry.snapshot()
        if not include_unready:
            providers = [row for row in providers if row.get("ready")]
        return {
            "protocol_version": contracts.ABI_VERSION,
            "marker_protocol_version": self._cfg_int(
                "marker_protocol_version", 1, minimum=1, maximum=1
            ),
            "tools": tools,
            "providers": providers,
            "legacy_model": not self.registry.has_providers(),
        }

    async def tool_dispatch(
        self,
        request_context: contracts.RequestContext,
        tool_id: str | contracts.ToolCall,
        canonical_args: Mapping[str, Any] | None = None,
        *,
        call_id: str | None = None,
        confirmation_ref: str = "",
        idempotency_key: str | None = None,
        round_id: str | int | None = None,
    ) -> contracts.ToolResult:
        if isinstance(tool_id, contracts.ToolCall):
            call = tool_id
        else:
            operation_context = self._context_for_operation(
                request_context, str(tool_id)
            )
            call = contracts.ToolCall.create(
                operation_context,
                str(tool_id),
                canonical_args or {},
                call_id=call_id,
                confirmation_ref=confirmation_ref,
            )
        key = idempotency_key or (
            f"dispatch:{call.request_context.turn_id}:"
            f"{call.tool_id.replace('.', '_')}:{call.args_hash[:16]}"
        )
        engine = self._ensure_dispatcher()
        return await engine.dispatch(
            call, round_id=round_id, idempotency_key=key
        )

    def confirmation_preview(
        self,
        request_context: contracts.RequestContext,
        action: str,
        payload: Mapping[str, Any] | None,
        revision: int,
    ) -> confirmation.ConfirmationPreview:
        contracts.operation_spec(action)
        self._authorizer.authorize(request_context, action)
        manager = self._ensure_confirmation_manager()
        return manager.preview(request_context, action, payload or {}, revision)

    async def confirmation_confirm(
        self,
        request_context: contracts.RequestContext,
        confirmation_id: str,
        *,
        expected_payload: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> contracts.ToolResult:
        manager = self._ensure_confirmation_manager()
        try:
            decision = manager.confirm(
                confirmation_id,
                request_context,
                expected_payload=expected_payload,
                expected_revision=expected_revision,
            )
        except confirmation.ConfirmationError as exc:
            return contracts.ToolResult.failure(
                f"confirm_{hashlib.sha256(str(confirmation_id).encode()).hexdigest()[:16]}",
                exc.code,
            )
        return await self._confirm_and_dispatch(request_context, decision)

    async def _confirm_and_dispatch(
        self,
        request_context: contracts.RequestContext,
        decision: confirmation.ConfirmationDecision,
    ) -> contracts.ToolResult:
        try:
            operation_context = self._context_for_operation(
                request_context, decision.action
            )
            call = contracts.ToolCall.create(
                operation_context,
                decision.action,
                decision.canonical_args,
                confirmation_ref=decision.confirmation_ref,
            )
        except contracts.ContractError as exc:
            return contracts.ToolResult.failure(
                f"confirm_{hashlib.sha256(decision.confirmation_id.encode()).hexdigest()[:16]}",
                exc.code,
            )
        engine = self._ensure_dispatcher()
        return await engine.dispatch(
            call,
            round_id=None,
            idempotency_key=f"confirm:{decision.confirmation_id}",
        )

    def confirmation_cancel(
        self,
        request_context: contracts.RequestContext,
        confirmation_id: str,
    ) -> confirmation.ConfirmationDecision:
        manager = self._ensure_confirmation_manager()
        return manager.cancel(confirmation_id, request_context)

    def tool_operation_status(
        self,
        request_context: contracts.RequestContext,
        operation_id: str,
    ) -> contracts.ToolResult:
        request_context.validate()
        ledger = self._ensure_ledger()
        record = ledger.get_call(str(operation_id))
        if record is None:
            return contracts.ToolResult(
                status="unknown",
                call_id=str(operation_id),
                result_type="status",
                data={"status": "unknown"},
                error_code="not_found",
                retryable=False,
                side_effect_state="unknown",
            )
        is_admin = request_context.actor_id in self._authorizer.policy.admin_actor_ids
        same_actor = (
            record.get("actor_id") == request_context.actor_id
            and record.get("instance_id") == request_context.instance_id
            and record.get("bot_id") == request_context.bot_id
            and record.get("scope") == request_context.requested_scope
        )
        if not is_admin and not same_actor:
            return contracts.ToolResult.failure(
                str(operation_id),
                "permission_denied",
                status="denied",
            )
        return contracts.ToolResult.success(
            str(operation_id),
            {
                "operation": record.get("operation"),
                "status": record.get("status"),
                "error_code": record.get("error_code"),
                "retryable": bool(record.get("retryable")),
                "side_effect_state": record.get("side_effect_state"),
                "provider_operation_id": record.get("provider_operation_id", ""),
                "updated_at": record.get("updated_at"),
            },
            result_type="operation_status",
        )

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

    def _chain_from_reply_plan(self, plan: contracts.ReplyPlan) -> list[Any]:
        plan.validate()
        chain: list[Any] = []
        for text in plan.text_segments:
            chain.append(self._plain_component(str(text)))
        chain.extend(plan.media_segments[:16])
        return chain

    def _fallback_chain_for_text(self, chain: list[Any], text: str) -> list[Any]:
        try:
            return [self._plain_component(text)]
        except Exception:
            plain_components = [
                component for component in chain if self._is_plain_component(component)
            ]
            if plain_components:
                plain_components[0].text = text
                return [plain_components[0]]
            return []

    # ------------------------------------------------------------------
    # Sole marker-consuming decorating hook (compatibility adapter only)
    # ------------------------------------------------------------------
    @filter.llm_tool(name="qqbot_safe_tool")
    async def native_safe_tool(
        self,
        event: AstrMessageEvent,
        tool_name: str,
        args_json: str = "{}",
    ) -> str:
        """调用 QQ Bot 安全工具目录中的工具。

        Args:
            tool_name(string): 工具名，必须来自系统提示中的安全工具目录。
            args_json(string): JSON 对象字符串，例如 {"query": "长春火锅"}。
        """
        if not self._cfg_bool("enabled", True):
            return "工具功能当前未启用。"
        name = str(tool_name or "").strip()
        if name not in self._enabled_tool_names():
            return "该工具未启用或不存在。"
        try:
            raw_args = json.loads(str(args_json or "{}"))
        except (TypeError, ValueError):
            return "工具参数不是合法 JSON。"
        if not isinstance(raw_args, dict):
            return "工具参数必须是 JSON 对象。"
        args: dict[str, str] = {}
        for key, value in raw_args.items():
            if isinstance(value, bool):
                args[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                args[str(key)] = str(value)
            else:
                return "工具参数只支持字符串、数字或布尔值。"
        intent = safe_tools.validate_intent(
            safe_tools.ToolIntent(name=name, args=args),
            tool_specs=self._tool_specs(),
        )
        if not intent.valid:
            return "工具参数无效。"
        ok, result, reason = await self._execute_intent(event, intent, "")
        if not ok:
            reason_text, _ = self._fallback_reason(reason)
            return reason_text or "工具执行失败。"
        return self._native_result_text(result)

    def _native_result_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value[:8_000]
        chain = getattr(value, "chain", None)
        if isinstance(value, (list, tuple)) or isinstance(chain, (list, tuple)):
            items = value if isinstance(value, (list, tuple)) else chain
            parts = [
                str(getattr(item, "text", item))
                for item in items
                if isinstance(getattr(item, "text", item), str)
            ]
            return "\n".join(parts)[:8_000]
        text = getattr(value, "text", None)
        if isinstance(text, str):
            return text[:8_000]
        return self._safe_result_text(value)[:8_000]

    @filter.on_decorating_result(priority=200)
    async def consume_safe_tools_marker(self, event: AstrMessageEvent):
        """Compatibility adapter that delegates to the same one-shot gate."""

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
            request_context = self._event_request_context(event)
        except Exception:
            # No authenticated actor means no tool may execute.  Still remove
            # marker-looking text from the visible compatibility reply.
            try:
                parsed = safe_tools.parse_tool_markers(
                    plain_text,
                    max_keyword_chars=self._max_keyword_chars(),
                    max_count=self._max_image_count(),
                    max_web_chars=self._max_web_chars(),
                    tool_specs=self._tool_specs(),
                )
            except Exception:
                parsed = safe_tools.ToolParseResult(
                    clean_text=safe_tools.strip_tool_markers(plain_text),
                    intents=[],
                    had_marker=bool(safe_tools._LOOSE_MARKER_RE.search(plain_text)),
                )
            if not parsed.had_marker:
                return
            self._set_extra(event, _PROCESSED_EXTRA, True)
            self._set_extra(event, _ALLOW_STICKER_EXTRA, False)
            fallback_text = self._build_fallback_text(
                parsed.clean_text, "permission"
            )
            chain[:] = self._fallback_chain_for_text(chain, fallback_text)
            await self._stop_event(event)
            return

        model_round = self._event_model_round(event)
        try:
            outcome = await self._run_output_gate(
                request_context,
                plain_text,
                event=event,
                model_round=model_round,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome = None
        if outcome is None:
            try:
                parsed = safe_tools.parse_tool_markers(
                    plain_text, tool_specs=self._tool_specs()
                )
            except Exception:
                parsed = safe_tools.ToolParseResult(
                    clean_text=safe_tools.strip_tool_markers(plain_text),
                    intents=[],
                    had_marker=bool(safe_tools._LOOSE_MARKER_RE.search(plain_text)),
                )
            if not parsed.had_marker:
                return
            self._set_extra(event, _PROCESSED_EXTRA, True)
            self._set_extra(event, _ALLOW_STICKER_EXTRA, False)
            fallback_text = self._build_fallback_text(parsed.clean_text, "failed")
            chain[:] = self._fallback_chain_for_text(chain, fallback_text)
            await self._stop_event(event)
            return
        if not outcome.had_marker:
            return

        # Terminal handshake before any provider work is awaited.
        self._set_extra(event, _PROCESSED_EXTRA, True)
        self._set_extra(event, _ALLOW_STICKER_EXTRA, False)
        replacement: list[Any] | None = None
        if outcome.legacy_chain is not None:
            replacement = list(outcome.legacy_chain)
        else:
            try:
                replacement = self._chain_from_reply_plan(outcome.reply_plan)
            except Exception:
                fallback_text = (
                    outcome.reply_plan.text_segments[0]
                    if outcome.reply_plan.text_segments
                    else _FALLBACK_DEFAULTS["generic"]
                )
                replacement = self._fallback_chain_for_text(
                    chain, fallback_text
                )
        chain[:] = replacement or []
        try:
            await self._stop_event(event)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # In-plugin loopback control bridge lifecycle
    # ------------------------------------------------------------------
    def _instance_id(self) -> str:
        return self._safe_identity(
            self._config_lookup("instance_id", None) or "default", "instance"
        )

    def _control_context(self, request: control_server.ControlRequest) -> contracts.RequestContext:
        body = request.body
        conversation_id = self._safe_identity(
            body.get("conversation_id", "control"), "conversation"
        )
        persona_id = self._safe_identity(
            body.get("persona_id", "control"), "persona"
        )
        epoch = self.registry.current_owner_epoch("qqbot_safe_tools")
        return contracts.RequestContext(
            protocol_version=contracts.ABI_VERSION,
            instance_id=request.instance_id,
            platform="control",
            bot_id="control",
            actor_id=request.actor_id,
            conversation_id=conversation_id,
            persona_id=persona_id,
            source_message_id=self._safe_identity(request.request_id, "request"),
            turn_id=self._safe_identity("control_" + request.request_id, "turn"),
            capability=request.method,
            requested_scope=request.scope,
            owner_epoch=int(epoch) if epoch is not None else 0,
            deadline=self._clock()
            + float(
                self._cfg_int(
                    "dispatch_deadline_seconds", 30, minimum=1, maximum=600
                )
            ),
        )

    async def _control_rpc(self, request: control_server.ControlRequest) -> dict[str, Any]:
        allowed_keys = _CONTROL_RPC_ARGUMENTS.get(request.method)
        if allowed_keys is None:
            raise control_server.ControlError(
                "unsupported_method", http_status=400
            )
        unknown = set(request.body) - set(allowed_keys)
        if unknown:
            raise control_server.ControlError("invalid_args", http_status=400)
        context = self._control_context(request)
        body = request.body
        try:
            if request.method == "tool.catalog":
                include_unready = body.get("include_unready", False)
                if not isinstance(include_unready, bool):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                return self.tool_catalog(
                    context, include_unready=include_unready
                )
            if request.method == "tool.dispatch":
                tool_id = body.get("tool_id")
                args = body.get("args", {})
                if not isinstance(tool_id, str) or not isinstance(args, Mapping):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                confirmation_ref = str(body.get("confirmation_ref", "") or "")
                idempotency_key = body.get("idempotency_key")
                if idempotency_key is not None:
                    idempotency_key = str(idempotency_key)
                round_id = body.get("round_id")
                result = await self.tool_dispatch(
                    context,
                    tool_id,
                    dict(args),
                    confirmation_ref=confirmation_ref,
                    idempotency_key=idempotency_key,
                    round_id=round_id,
                )
                return result.to_dict()
            if request.method == "confirmation.preview":
                action = body.get("action")
                payload = body.get("payload", {})
                revision = body.get("revision")
                if not isinstance(action, str) or not isinstance(payload, Mapping):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                if isinstance(revision, bool) or not isinstance(revision, int):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                preview = self.confirmation_preview(
                    context, action, dict(payload), revision
                )
                return preview.to_dict()
            if request.method == "confirmation.confirm":
                confirmation_id = body.get("confirmation_id")
                if not isinstance(confirmation_id, str):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                expected_payload = body.get("expected_payload")
                if expected_payload is not None and not isinstance(
                    expected_payload, Mapping
                ):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                expected_revision = body.get("expected_revision")
                if expected_revision is not None and (
                    isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                ):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                result = await self.confirmation_confirm(
                    context,
                    confirmation_id,
                    expected_payload=(
                        dict(expected_payload)
                        if expected_payload is not None
                        else None
                    ),
                    expected_revision=expected_revision,
                )
                return result.to_dict()
            if request.method == "confirmation.cancel":
                confirmation_id = body.get("confirmation_id")
                if not isinstance(confirmation_id, str):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                decision = self.confirmation_cancel(context, confirmation_id)
                return decision.to_dict()
            if request.method == "policy.snapshot":
                return self.policy_snapshot()
            if request.method == "aux_budget.get":
                return self.aux_budget_get()
            if request.method == "aux_budget.usage":
                cycle = body.get("cycle")
                if cycle is not None and not isinstance(cycle, str):
                    raise control_server.ControlError("invalid_args", http_status=400)
                return self.aux_budget_usage(cycle=cycle)
            if request.method == "aux_budget.set":
                tier = body.get("tier")
                enabled = body.get("enabled")
                if tier is not None and (not isinstance(tier, str) or not tier):
                    raise control_server.ControlError("invalid_args", http_status=400)
                if enabled is not None and not isinstance(enabled, bool):
                    raise control_server.ControlError("invalid_args", http_status=400)
                expected_revision = body.get("expected_revision")
                if expected_revision is not None and (
                    isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                ):
                    raise control_server.ControlError("invalid_args", http_status=400)
                return self.aux_budget_set(
                    tier=tier, enabled=enabled, expected_revision=expected_revision
                )
            if request.method == "aux_budget.feature_set":
                feature_id = body.get("feature_id")
                enabled = body.get("enabled")
                if not isinstance(feature_id, str) or not feature_id:
                    raise control_server.ControlError("invalid_args", http_status=400)
                if not isinstance(enabled, bool):
                    raise control_server.ControlError("invalid_args", http_status=400)
                allowlist = body.get("group_allowlist")
                if allowlist is not None and not isinstance(allowlist, list):
                    raise control_server.ControlError("invalid_args", http_status=400)
                expected_revision = body.get("expected_revision")
                if expected_revision is not None and (
                    isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                ):
                    raise control_server.ControlError("invalid_args", http_status=400)
                return self.aux_budget_feature_set(
                    feature_id=feature_id,
                    enabled=enabled,
                    group_allowlist=allowlist,
                    expected_revision=expected_revision,
                )
            if request.method == "aux_budget.cycle_reset":
                expected_revision = body.get("expected_revision")
                if expected_revision is not None and (
                    isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                ):
                    raise control_server.ControlError("invalid_args", http_status=400)
                return self.aux_budget_cycle_reset(expected_revision=expected_revision)
            if request.method in {"policy.set", "policy.reset"}:
                plugin_id = body.get("plugin_id")
                if not isinstance(plugin_id, str) or not plugin_id:
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                group_id = body.get("group_id")
                user_id = body.get("user_id")
                if group_id is not None and not isinstance(group_id, str):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                if user_id is not None and not isinstance(user_id, str):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                is_private = body.get("is_private", False)
                if not isinstance(is_private, bool):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                expected_revision = body.get("expected_revision")
                if expected_revision is not None and (
                    isinstance(expected_revision, bool)
                    or not isinstance(expected_revision, int)
                ):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                if request.method == "policy.reset":
                    return self.policy_reset(
                        plugin_id=plugin_id,
                        group_id=group_id or None,
                        user_id=user_id or None,
                        is_private=is_private,
                        expected_revision=expected_revision,
                    )
                enabled = body.get("enabled", True)
                params = body.get("params", {})
                if not isinstance(enabled, bool) or not isinstance(params, Mapping):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                return self.policy_set(
                    plugin_id=plugin_id,
                    group_id=group_id or None,
                    user_id=user_id or None,
                    is_private=is_private,
                    enabled=enabled,
                    params=dict(params),
                    expected_revision=expected_revision,
                )
            if request.method == "tool.operation_status":
                operation_id = body.get("operation_id")
                if not isinstance(operation_id, str):
                    raise control_server.ControlError(
                        "invalid_args", http_status=400
                    )
                result = self.tool_operation_status(context, operation_id)
                return result.to_dict()
        except control_server.ControlError:
            raise
        except contracts.ContractError as exc:
            raise control_server.ControlError(exc.code, http_status=400) from exc
        except auth.AuthorizationError as exc:
            raise control_server.ControlError(exc.code, http_status=403) from exc
        except confirmation.ConfirmationError as exc:
            raise control_server.ControlError(
                exc.code, http_status=exc.http_status
            ) from exc
        except SafeToolsServiceError as exc:
            raise control_server.ControlError(exc.code, http_status=409) from exc
        except Exception as exc:
            raise control_server.ControlError(
                "internal_error", http_status=500
            ) from exc
        raise control_server.ControlError("unsupported_method", http_status=400)

    async def _register_self_provider(self) -> None:
        if self.registry.get_registered_provider("qqbot_safe_tools") is not None:
            return
        owner_epoch = self._cfg_int(
            "safe_tools_owner_epoch", 1, minimum=0, maximum=2**31 - 1
        )
        methods = {
            "tool.catalog": self.tool_catalog,
            "tool.process_model_output": self.tool_process_model_output,
            "tool.dispatch": self.tool_dispatch,
            "confirmation.preview": self.confirmation_preview,
            "confirmation.confirm": self.confirmation_confirm,
            "confirmation.cancel": self.confirmation_cancel,
            "tool.operation_status": self.tool_operation_status,
        }
        try:
            self.registry.register_provider(
                "qqbot_safe_tools",
                methods,
                owner_epoch=owner_epoch,
                ready=True,
                exclusive=True,
            )
            self._self_provider_registered = True
        except service_registry.RegistryError as exc:
            self._control_error_code = exc.code

    async def start_control_bridge(self) -> dict[str, Any]:
        async with self._control_start_lock:
            if self._control_server is not None and self._control_server.running:
                return self._control_server.status()
            self._control_started = True
            if not self._cfg_bool("control_bridge_enabled", False):
                return {
                    "running": False,
                    "state": "disabled",
                    "protocol_version": control_server.CONTROL_PROTOCOL_VERSION,
                }
            try:
                secret = control_server.load_control_secret(
                    data_dir=self._data_dir()
                )
            except control_server.ControlConfigError as exc:
                self._control_error_code = exc.code
                return {
                    "running": False,
                    "state": "blocked",
                    "error": exc.code,
                    "protocol_version": control_server.CONTROL_PROTOCOL_VERSION,
                }
            instance_id = self._instance_id()
            allowed_instances = self._cfg_list(
                "control_bridge_instance_allowlist", (instance_id,)
            )
            allowed_actors = self._cfg_list("control_bridge_actor_allowlist")
            if not allowed_actors:
                allowed_actors = self._cfg_list("auth_admin_actor_ids")
            allowed_scopes = self._cfg_list(
                "control_bridge_scope_allowlist", ("global",)
            )
            if not allowed_actors:
                self._control_error_code = "control_actor_allowlist_empty"
                return {
                    "running": False,
                    "state": "blocked",
                    "error": self._control_error_code,
                    "protocol_version": control_server.CONTROL_PROTOCOL_VERSION,
                }
            port = self._cfg_int(
                "control_bridge_port",
                control_server.CONTROL_DEFAULT_PORT,
                minimum=0,
                maximum=65_535,
            )
            server = control_server.ControlServer(
                secret,
                self._control_rpc,
                port=port,
                expected_instance=instance_id,
                allowed_instances=allowed_instances,
                allowed_actors=allowed_actors,
                allowed_scopes=allowed_scopes,
            )
            try:
                status = await server.start()
            except Exception:
                self._control_error_code = "control_bind_failed"
                await server.stop()
                return {
                    "running": False,
                    "state": "blocked",
                    "error": self._control_error_code,
                    "protocol_version": control_server.CONTROL_PROTOCOL_VERSION,
                }
            self._control_server = server
            self._control_error_code = ""
            return status

    async def initialize(self) -> dict[str, Any]:
        """Idempotent AstrBot startup hook: register services and bridge."""

        self._self_provider_registered = getattr(
            self, "_self_provider_registered", False
        )
        await self._register_self_provider()
        try:
            self.tool_instruction()
            asyncio.create_task(self._delayed_catalog_check())
        except Exception:
            logger.exception("safe_tools startup catalog self-check failed")
        return await self.start_control_bridge()

    async def _delayed_catalog_check(self) -> None:
        """Log the catalogue after all sibling plugins have finished loading."""
        await asyncio.sleep(3)
        try:
            self.tool_instruction()
        except Exception:
            logger.exception("safe_tools delayed catalog self-check failed")

    async def terminate(self) -> None:
        """Stop owned bridge/ledger and unregister this plugin's capability."""

        server = self._control_server
        self._control_server = None
        if server is not None:
            try:
                await server.stop()
            except Exception:
                pass
        if self._ledger is not None:
            try:
                self._ledger.close()
            except Exception:
                pass
            self._ledger = None
        self._dispatcher = None
        self._confirmation_manager = None
        if getattr(self, "_self_provider_registered", False):
            try:
                self.registry.unregister_provider("qqbot_safe_tools")
            except Exception:
                pass
            self._self_provider_registered = False
        self._control_started = False

    def _format_tool_result(self, result: contracts.ToolResult) -> str:
        if result.status in {"success", "ok"}:
            text = self._safe_result_text(result.data, limit=2_000)
            return text or "操作已完成。"
        reason = _ERROR_TO_REASON.get(str(result.error_code or ""), "failed")
        reason_text, _ = self._fallback_reason(reason)
        if result.error_code == "outcome_unknown":
            reason_text = "操作结果不明确，已停止自动重试；请查询操作状态。"
        return f"{reason_text}（{result.error_code or 'error'}）"

    def _format_confirmation_decision(
        self, decision: confirmation.ConfirmationDecision
    ) -> str:
        if decision.status == "cancelled":
            return f"已取消确认 {decision.confirmation_id}。"
        return f"已确认 {decision.confirmation_id}。"

    def _command_context(
        self, event: AstrMessageEvent, capability: str
    ) -> contracts.RequestContext:
        return self._event_request_context(event, capability=capability)

    @filter.command("工具", alias={"tools"}, priority=110)
    async def tools_command(self, event: AstrMessageEvent):
        raw = str(getattr(event, "message_str", "") or "").strip()
        tokens = raw.split()
        body = tokens[1:] if tokens else []
        if body and body[0].lstrip("/") in {"工具", "tools"}:
            body = body[1:]
        subcommand = body[0] if body else ""
        reply: str
        try:
            if subcommand == "状态":
                context = self._command_context(event, "tool.catalog")
                reply = self._diagnostic_text()
            elif subcommand == "调用":
                if len(body) != 2 or not body[1]:
                    raise SafeToolsServiceError("invalid_args", "缺少 OP_ID")
                operation = body[1]
                contracts.operation_spec(operation)
                context = self._command_context(event, "tool.dispatch")
                result = await self.tool_dispatch(
                    context,
                    operation,
                    {},
                    idempotency_key=f"command:{context.turn_id}:{operation.replace('.', '_')}",
                    round_id=0,
                )
                reply = self._format_tool_result(result)
            elif subcommand == "确认":
                if len(body) != 2 or not body[1]:
                    raise SafeToolsServiceError("invalid_args", "缺少 CONFIRM_ID")
                context = self._command_context(event, "confirmation.confirm")
                result = await self.confirmation_confirm(context, body[1])
                reply = self._format_tool_result(result)
            elif subcommand == "取消":
                if len(body) != 2 or not body[1]:
                    raise SafeToolsServiceError("invalid_args", "缺少 CONFIRM_ID")
                context = self._command_context(event, "confirmation.cancel")
                decision = self.confirmation_cancel(context, body[1])
                reply = self._format_confirmation_decision(decision)
            else:
                reply = (
                    "用法：/工具 状态 | 调用 OP_ID | 确认 CONFIRM_ID | "
                    "取消 CONFIRM_ID；别名 /tools"
                )
        except contracts.ContractError:
            reply = "操作ID必须是固定方法表中的安全业务操作。"
        except SafeToolsServiceError as exc:
            reply = self._fallback_reason(
                _ERROR_TO_REASON.get(exc.code, "invalid")
            )[0]
        except confirmation.ConfirmationError as exc:
            reason = _ERROR_TO_REASON.get(exc.code, "invalid")
            reply = self._fallback_reason(reason)[0]
        except auth.AuthorizationError:
            reply = self._fallback_reason("permission")[0]
        except Exception:
            reply = self._fallback_reason("failed")[0]
        yield event.plain_result(reply)
        await self._stop_event(event)

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
                for name, key in (
                    (descriptor.name, descriptor.allow_key)
                    for descriptor in self._tool_registry().all()
                )
            ),
            "依赖插件："
            + "；".join(
                f"{plugin_id}={state(ready)}"
                for plugin_id, ready in availability.items()
            ),
            f"ABI：v{contracts.ABI_VERSION}；"
            f"registry={len(self.registry.snapshot())} 个 provider；"
            f"ready_tools={len(self._advertised_tool_names())}；"
            f"control_bridge={'运行中' if self._control_server and self._control_server.running else '未运行'}"
            + (f"（{self._control_error_code}）" if self._control_error_code else ""),
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
