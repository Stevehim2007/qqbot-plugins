"""qqbot_food: standalone Food search, review draft and submit plugin.

Standard library + AstrBot >=4.29 only.  This module never imports legacy
plugin packages and never calls an LLM.
"""

from __future__ import annotations

import asyncio
import inspect
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import contracts, food
from .api import (
    FoodAPIClient,
    FoodHTTPError,
    FoodResponseTooLarge,
    FoodTimeoutError,
    FoodTransportError,
)
from .backend import FoodBackend
from .config import DEFAULTS, PluginConfig
from .contracts import (
    ConfirmationTokenIssuer,
    ProviderRegistrationError,
    ProviderResult,
    get_or_create_registry,
)
from .ledger import FoodLedger
from .provider import FoodContractProvider
from .state import FoodStateStore, state_key

PLUGIN_NAME = "qqbot_food"
COMMAND_NAMES = ("food", "美食", "吃什么", "饭店")
CANONICAL_SUBCOMMANDS = {
    "查询": "query",
    "query": "query",
    "位置": "locations",
    "location": "locations",
    "locations": "locations",
    "标签": "tags",
    "tag": "tags",
    "tags": "tags",
    "提交": "submit",
    "submit": "submit",
    "确认": "confirm",
    "confirm": "confirm",
    "取消": "cancel",
    "cancel": "cancel",
    "状态": "status",
    "status": "status",
}
_SUBMIT_ALLOWED_KEYS = {
    "restaurant",
    "name",
    "location",
    "taste",
    "value",
    "comment",
    "tags",
    "tag",
}


def _text(value: Any) -> str:
    try:
        return str(value or "").strip()
    except Exception:
        return ""


def _safe_reply_object(event: AstrMessageEvent, text: Any, max_chars: int = 1800):
    try:
        return event.plain_result(
            food.clamp_food_reply(text, max_chars=max(1, int(max_chars)))
        )
    except Exception:
        return None


def _format_template(template: Any, **kwargs: Any) -> str:
    value = str(template or "")
    safe_kwargs = {
        key: food.clamp_food_reply(str(item), max_chars=200)
        for key, item in kwargs.items()
    }
    try:
        return value.format(**safe_kwargs)
    except (KeyError, IndexError, ValueError):
        return value


def _event_sender_id(event: AstrMessageEvent) -> str:
    # Deliberately not logged or echoed: this value is the site password.
    try:
        return _text(event.get_sender_id())[:64]
    except Exception:
        return ""


def _event_sender_name(event: AstrMessageEvent) -> str:
    try:
        return _text(event.get_sender_name())
    except Exception:
        return ""


def _event_umo(event: AstrMessageEvent) -> str:
    try:
        return _text(getattr(event, "unified_msg_origin", ""))[:200]
    except Exception:
        return ""


def _event_group_id(event: AstrMessageEvent) -> str:
    try:
        return food.sanitize_food_submit_text(
            event.get_group_id(), max_chars=40
        )
    except Exception:
        return ""


def _event_is_private(event: AstrMessageEvent) -> bool:
    try:
        return bool(event.is_private_chat())
    except Exception:
        return False


def _event_group_name(event: AstrMessageEvent) -> str:
    try:
        message_obj = getattr(event, "message_obj", None)
    except Exception:
        return ""
    holders: list[tuple[Any, tuple[str, ...]]] = [
        (message_obj, ("group_name", "name"))
    ]
    if isinstance(message_obj, dict):
        group = message_obj.get("group")
    else:
        group = getattr(message_obj, "group", None) if message_obj is not None else None
    if group is not None:
        holders.append((group, ("group_name", "name")))
    # Some adapters put the group name directly on the event; only trust an
    # explicit group_name there, never a generic sender nickname attribute.
    holders.append((event, ("group_name",)))
    for holder, attrs in holders:
        if holder is None:
            continue
        for attr in attrs:
            try:
                value = (
                    holder.get(attr)
                    if isinstance(holder, dict)
                    else getattr(holder, attr, None)
                )
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _event_message_text(event: AstrMessageEvent) -> str:
    try:
        return _text(getattr(event, "message_str", ""))
    except Exception:
        return ""


def _stop_event(event: AstrMessageEvent) -> None:
    try:
        event.stop_event()
    except Exception:
        pass


def _call_shall_not_llm(event: AstrMessageEvent) -> None:
    try:
        event.should_call_llm(True)
    except Exception:
        pass


class FoodPlugin(Star):
    """AstrBot Star exposing /food, confirm handling and safe-tool methods."""

    def __init__(
        self,
        context: Context,
        config: Any = None,
        *,
        data_dir: str | Path | None = None,
        api_client: Any = None,
        clock: Any = None,
    ):
        super().__init__(context)
        # AstrBot v4.29 passes the per-plugin config as ``config=``; never use
        # context.get_config() here because that is the global bot config.
        self.config = PluginConfig(config)
        self._clock = clock or time.time
        self._last_food_at: dict[str, float] = {}
        self._last_food_search_tool_at: dict[str, float] = {}

        if api_client is not None:
            self._search_api = api_client
            self._submit_api = api_client
        else:
            get_timeout = max(
                1, self.config.int("food_timeout_seconds", default=12)
            )
            get_bytes = max(
                1, self.config.int("food_max_response_bytes", default=500_000)
            )
            post_timeout = max(
                1, self.config.int("food_submit_timeout_seconds", default=12)
            )
            post_bytes = max(
                1, self.config.int("food_submit_max_response_bytes", default=100_000)
            )
            self._search_api = FoodAPIClient(
                timeout=get_timeout, max_bytes=get_bytes
            )
            self._submit_api = FoodAPIClient(
                timeout=post_timeout, max_bytes=post_bytes
            )

        self._data_dir = self._resolve_data_dir(data_dir)
        self._state = FoodStateStore(
            self._data_dir / "food_state.json",
            draft_ttl=max(
                1, self.config.int("food_submit_draft_ttl_seconds", default=600)
            ),
            clock=self._clock,
        )

        # ABI v1 provider.  The SQLite ledger is the canonical draft/operation
        # store and is deliberately independent of the legacy JSON state above.
        self._contract_ready = False
        self._provider_error = ""
        self._ledger: FoodLedger | None = None
        self._backend: FoodBackend | None = None
        self._confirmation_issuer: ConfirmationTokenIssuer | None = None
        self._contract_provider: FoodContractProvider | None = None
        self._registry = None
        try:
            self._ledger = FoodLedger(
                self._data_dir / "food_ledger.sqlite3",
                clock=self._clock,
            )
            self._backend = FoodBackend(
                get_json=self._get_json,
                post_submit=self._post_submit,
                timeout_seconds=max(
                    1, self._cfg_int("food_submit_timeout_seconds", default=12)
                ),
            )
            ttl = self._cfg_int(
                "drafts_confirmation_ttl_seconds", default=300
            )
            self._confirmation_issuer = ConfirmationTokenIssuer(
                self._ledger.token_secret(), ttl_seconds=ttl
            )
            self._contract_provider = FoodContractProvider(self)
            self._registry = get_or_create_registry(context)
            self._registry.register(
                owner=PLUGIN_NAME,
                capability=contracts.PROVIDER_CAPABILITY,
                provider=self._contract_provider,
                abi_version=contracts.ABI_VERSION,
            )
            self._registry.set_ready(PLUGIN_NAME, True)
            self._contract_ready = True
        except ProviderRegistrationError:
            raise
        except Exception:
            self._provider_error = "provider_initialization_failed"
            self._contract_ready = False

    # ------------------------------------------------------------------
    # Config, data and bounded replies
    # ------------------------------------------------------------------
    def _resolve_data_dir(self, override: str | Path | None) -> Path:
        if override is not None:
            return Path(override)
        try:
            base = StarTools.get_data_dir(plugin_name=PLUGIN_NAME)
            if str(base or "").strip():
                return Path(base)
        except Exception:
            pass
        try:
            base = StarTools.get_data_dir()
            if str(base or "").strip():
                return Path(base) / PLUGIN_NAME
        except Exception:
            pass
        return Path.home() / ".qqbot_food"

    def _cfg_bool(self, key: str) -> bool:
        return self.config.bool(key)

    def _capability_params(self, event: AstrMessageEvent) -> dict[str, Any]:
        try:
            getter = getattr(self.context, "get_registered_star", None)
            meta = getter("qqbot_safe_tools") if callable(getter) else None
            if meta is None:
                return {}
            star = getattr(meta, "star_cls", None) or meta
            policy_fn = getattr(star, "policy_effective", None)
            if not callable(policy_fn):
                return {}
            private = _event_is_private(event)
            policy = policy_fn(
                "qqbot_food",
                group_id=None if private else (_event_group_id(event) or None),
                user_id=_event_sender_id(event) or None,
                is_private=private,
            )
            if not isinstance(policy, dict) or not policy.get("enabled", True):
                return {}
            params = policy.get("params")
            return dict(params) if isinstance(params, dict) else {}
        except Exception:
            return {}

    def _capability_enabled(self, event: AstrMessageEvent) -> bool:
        """Return False only when safe_tools explicitly disables Food."""
        try:
            getter = getattr(self.context, "get_registered_star", None)
            meta = getter("qqbot_safe_tools") if callable(getter) else None
            if meta is None:
                return True
            star = getattr(meta, "star_cls", None) or meta
            policy_fn = getattr(star, "policy_effective", None)
            if not callable(policy_fn):
                return True
            private = _event_is_private(event)
            policy = policy_fn(
                "qqbot_food",
                group_id=None if private else (_event_group_id(event) or None),
                user_id=_event_sender_id(event) or None,
                is_private=private,
            )
            return not isinstance(policy, dict) or bool(policy.get("enabled", True))
        except Exception:
            return True

    def _cfg_int(self, key: str, *, minimum: int = 0, maximum: int | None = None, default: int | None = None) -> int:
        return self.config.int(
            key, minimum=minimum, maximum=maximum, default=default
        )

    def _cfg_str(self, key: str) -> str:
        return self.config.string(key)

    def _canonical_value(
        self,
        group: str,
        name: str,
        *,
        flat_key: str = "",
        legacy_key: str = "",
        default: Any = None,
    ) -> Any:
        """Read a canonical nested group where available, then flat/legacy.

        AstrBot's flat ``_conf_schema.json`` cannot describe every nested
        object, so canonical settings are also exposed as flat keys such as
        ``query_limit``.  If a future/struct-aware config passes
        ``{"query": {"limit": 5}}`` it is honored first.
        """

        section = self.config.values.get(str(group))
        if isinstance(section, Mapping):
            for candidate in (str(name), str(flat_key), str(legacy_key)):
                if candidate and candidate in section:
                    return section.get(candidate)
        for candidate in (
            f"{group}.{name}" if group and name else "",
            flat_key,
            legacy_key,
        ):
            if candidate and candidate in self.config.values:
                return self.config.values[candidate]
        if flat_key in self.config.values:
            return self.config.values[flat_key]
        if legacy_key and legacy_key in self.config.values:
            return self.config.values[legacy_key]
        if flat_key in DEFAULTS:
            return DEFAULTS[flat_key]
        if legacy_key and legacy_key in DEFAULTS:
            return DEFAULTS[legacy_key]
        if default is not None:
            return default
        return self.config.raw(f"{group}_{name}")

    def _contract_context(self, event: AstrMessageEvent | None) -> dict[str, Any]:
        sender = _event_sender_id(event) if event is not None else ""
        umo = _event_umo(event) if event is not None else ""
        group_id = "" if event is None or _event_is_private(event) else _event_group_id(event)
        scope = (
            {"kind": "group", "id": group_id}
            if group_id
            else {"kind": "user", "id": sender}
        )
        source_message_id = ""
        turn_id = ""
        if event is not None:
            for attr in ("message_id", "msg_id"):
                value = getattr(event, attr, None)
                if value not in (None, ""):
                    source_message_id = _text(value)[:200]
                    break
            try:
                message_obj = getattr(event, "message_obj", None)
                if source_message_id == "" and message_obj is not None:
                    source_message_id = _text(
                        getattr(message_obj, "message_id", "")
                    )[:200]
            except Exception:
                pass
            turn_id = _text(umo)[:200]
        return contracts.RequestContext(
            protocol_version=contracts.PROTOCOL_VERSION,
            instance_id=_text(
                self._canonical_value(
                    "provider",
                    "instance_id",
                    flat_key="provider_instance_id",
                    legacy_key="",
                    default="astrbot",
                )
            )[:200],
            platform="aiocqhttp",
            bot_id=_text(
                getattr(event, "self_id", "")
                if event is not None
                else ""
            )[:200]
            or "bot",
            actor_id=sender,
            conversation_id=umo,
            persona_id="",
            source_message_id=source_message_id,
            turn_id=turn_id,
            capability=contracts.PROVIDER_CAPABILITY,
            requested_scope=scope,
            owner_epoch=self._cfg_int("provider_owner_epoch", default=1),
            deadline=self._now()
            + max(1, self._cfg_int("food_timeout_seconds", default=12))
            + 5,
            group_id=group_id,
            group_name=_event_group_name(event) if event is not None else "",
            actor_name=_event_sender_name(event) if event is not None else "",
        ).to_dict()

    def _now(self) -> float:
        try:
            value = self._clock() if callable(self._clock) else self._clock
            return float(value)
        except Exception:
            return time.time()

    def _reply(self, key: str, **kwargs: Any) -> str:
        return food.clamp_food_reply(
            _format_template(self.config.string(key), **kwargs),
            max_chars=self._cfg_int(
                "food_reply_max_chars", minimum=200, maximum=5000, default=1800
            ),
        )

    def _plain(self, event: AstrMessageEvent, text: Any):
        return _safe_reply_object(
            event,
            text,
            max_chars=self._cfg_int(
                "food_reply_max_chars", minimum=200, maximum=5000, default=1800
            ),
        )

    def _log_info(self, message: str) -> None:
        try:
            logger.info("qqbot_food %s", message)
        except Exception:
            try:
                logger.info(f"qqbot_food {message}")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Async client adapter (works with real sync client and test fakes)
    # ------------------------------------------------------------------
    async def _invoke_client(self, method: Any, *args: Any, timeout: float) -> Any:
        if inspect.iscoroutinefunction(method):
            return await asyncio.wait_for(method(*args), timeout=timeout)
        return await asyncio.wait_for(
            asyncio.to_thread(method, *args), timeout=timeout
        )

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        timeout = max(
            1, self._cfg_int("food_timeout_seconds", default=12)
        )
        return await self._invoke_client(
            self._search_api.get_json, path, params, timeout=float(timeout + 2)
        )

    async def _post_submit(self, payload: dict[str, Any]) -> tuple[int, Any]:
        timeout = max(
            1, self._cfg_int("food_submit_timeout_seconds", default=12)
        )
        return await self._invoke_client(
            self._submit_api.post_submit, payload, timeout=float(timeout + 2)
        )

    # ------------------------------------------------------------------
    # Command argument parsing and cooldowns
    # ------------------------------------------------------------------
    @staticmethod
    def _command_argument(event: AstrMessageEvent) -> str:
        text = _event_message_text(event)
        if not text:
            return ""
        if text[:1] in ("/", "／"):
            text = text[1:].lstrip()
        lowered = text.casefold()
        for name in COMMAND_NAMES:
            if lowered == name.casefold():
                return ""
            if lowered.startswith(name.casefold()):
                rest = text[len(name):]
                if rest.startswith("@"):
                    _mention, separator, tail = rest.partition(" ")
                    rest = tail if separator else ""
                if not rest or rest[0].isspace():
                    return food._clean_food_text(rest, max_chars=200)
        # Some adapters hand the command handler only the argument text.
        if not text.startswith(("/", "／")):
            return food._clean_food_text(text, max_chars=200)
        return ""

    def _command_cooldown_remaining(self, event: AstrMessageEvent) -> int:
        cooldown = max(0, self._cfg_int("food_cooldown_seconds", default=30))
        if cooldown <= 0:
            return 0
        key = f"{_event_umo(event)}:{_event_sender_id(event)}"
        now = self._now()
        last = self._last_food_at.get(key)
        if last is not None:
            remaining = cooldown - (now - last)
            if remaining > 0:
                return max(1, int(remaining + 0.999))
        self._last_food_at[key] = now
        if len(self._last_food_at) > 256:
            for stale_key, _ in sorted(
                self._last_food_at.items(), key=lambda pair: pair[1]
            )[: len(self._last_food_at) - 256]:
                self._last_food_at.pop(stale_key, None)
        return 0

    def _search_tool_cooldown_remaining(self, event: AstrMessageEvent) -> int:
        cooldown = max(
            0, self._cfg_int("food_search_cooldown_seconds", default=0)
        )
        if cooldown <= 0:
            return 0
        key = f"{_event_umo(event)}:{_event_sender_id(event)}"
        now = self._now()
        last = self._last_food_search_tool_at.get(key)
        if last is not None:
            remaining = cooldown - (now - last)
            if remaining > 0:
                return max(1, int(remaining + 0.999))
        self._last_food_search_tool_at[key] = now
        return 0

    def _parse_search_command(self, raw_args: str) -> dict[str, Any]:
        return food.parse_food_search_command(
            raw_args,
            name_max_chars=max(
                1, self._cfg_int("food_keyword_max_chars", default=50)
            ),
            location_max_chars=max(
                1, self._cfg_int("food_location_max_chars", default=30)
            ),
            tag_max_chars=max(
                1, self._cfg_int("food_search_tag_max_chars", default=20)
            ),
            tag_count=max(1, self._cfg_int("food_search_max_tags", default=3)),
            default_limit=max(
                1,
                min(
                    5,
                    self._cfg_int("food_max_results", default=5),
                ),
            ),
            default_sort=self._cfg_str("food_search_default_sort") or "total",
            default_order=self._cfg_str("food_search_default_order"),
        )

    def _normalize_search_tool_args(self, args: Any) -> dict[str, Any]:
        raw = args if isinstance(args, dict) else {}
        tags = raw.get("tags")
        if tags in (None, "", []):
            tags = raw.get("tag")
        values = {
            "name": raw.get("name") or raw.get("q") or raw.get("keyword"),
            "location": raw.get("location"),
            "tags": tags,
            "sort": raw.get("sort"),
            "order": raw.get("order"),
            "limit": raw.get("limit"),
        }
        return food.normalize_food_search_request(
            values,
            name_max_chars=max(
                1, self._cfg_int("food_search_name_max_chars", default=50)
            ),
            location_max_chars=max(
                1, self._cfg_int("food_search_location_max_chars", default=30)
            ),
            tag_max_chars=max(
                1, self._cfg_int("food_search_tag_max_chars", default=20)
            ),
            tag_count=max(1, self._cfg_int("food_search_max_tags", default=3)),
            default_limit=max(
                1, self._cfg_int("food_search_default_limit", default=5)
            ),
            default_sort=self._cfg_str("food_search_default_sort") or "total",
            default_order=self._cfg_str("food_search_default_order"),
        )

    def _invalid_search_reply(self, code: str, *, tool: bool) -> str:
        if code == "empty":
            return self._reply(
                "food_search_usage_reply" if tool else "food_usage_reply"
            )
        return self._reply(
            "food_search_invalid_reply"
            if tool
            else "food_invalid_query_reply"
        )

    # ------------------------------------------------------------------
    # Group location context
    # ------------------------------------------------------------------
    def _group_location_parts(self, event: AstrMessageEvent | None) -> list[str]:
        if event is None or _event_is_private(event):
            return []
        max_parts = max(
            1, self._cfg_int("food_submit_location_max_parts", default=6)
        )
        max_part_chars = max(
            1,
            self._cfg_int("food_submit_location_part_max_chars", default=30),
        )
        mapping = food.parse_food_group_locations(
            self.config.raw("food_group_locations"),
            max_parts=max_parts,
            max_part_chars=max_part_chars,
        )
        group_id = _event_group_id(event)
        if group_id and group_id in mapping:
            return mapping[group_id]
        if not self._cfg_bool("food_group_location_infer_from_name"):
            return []
        return food.infer_food_group_location_parts(
            _event_group_name(event),
            max_parts=max_parts,
            max_part_chars=max_part_chars,
        )

    async def _resolve_group_location(
        self, context: list[str]
    ) -> dict[str, Any] | None:
        if not context:
            return None
        leaf = _text(context[-1])
        if not leaf:
            return None
        try:
            payload = await self._get_json(
                food.FOOD_LOCATIONS_PATH, {"q": leaf}
            )
            items = food.extract_food_items(payload)
            match = food.match_food_location(context, items)
            if (
                match.get("kind") in {"full", "prefix"}
                and match.get("id") is not None
            ):
                return {
                    "id": match.get("id"),
                    "name": _text(match.get("name")) or leaf,
                }
            item = food.select_food_location(items, leaf)
            if item and item.get("id") is not None:
                return {
                    "id": item.get("id"),
                    "name": _text(item.get("name")) or leaf,
                }
        except Exception:
            # A group default must never turn a read-only search into an error.
            return None
        return None

    def _apply_group_location(
        self,
        event: AstrMessageEvent | None,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        context = self._group_location_parts(event)
        if not context:
            return fields
        max_parts = max(
            1, self._cfg_int("food_submit_location_max_parts", default=6)
        )
        max_part_chars = max(
            1,
            self._cfg_int("food_submit_location_part_max_chars", default=30),
        )
        location_max = max(
            1, self._cfg_int("food_submit_location_max_chars", default=60)
        )
        raw = _text(fields.get("location"))
        if raw:
            parts = food.split_food_location_path(
                raw, max_parts=max_parts, max_part_chars=max_part_chars
            )
            if not parts:
                return fields
            merged = food.merge_food_location_with_context(parts, context)
        else:
            merged = context
        merged_text = food.format_food_location_path(merged)
        if not merged_text:
            return fields
        if len(merged_text) > location_max:
            # Keep the user value untouched; the resolver will reject unsafely
            # long input instead of silently truncating an address.
            return fields
        updated = dict(fields)
        updated["location"] = merged_text
        return updated

    # ------------------------------------------------------------------
    # Search implementation
    # ------------------------------------------------------------------
    async def _resolve_search_location(self, query: str) -> dict[str, Any]:
        try:
            payload = await self._get_json(
                food.FOOD_LOCATIONS_PATH, {"q": query}
            )
        except FoodTimeoutError:
            return {"error": "timeout"}
        except (FoodHTTPError, FoodTransportError, food.FoodAPIError):
            return {"error": "error"}
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        except Exception:
            return {"error": "error"}
        items = food.extract_food_items(payload)
        resolved = food.resolve_food_search_location(items, query)
        if resolved.get("id") is not None:
            return {
                "id": resolved.get("id"),
                "name": _text(resolved.get("name")) or query,
            }
        if resolved.get("ambiguous"):
            return {
                "error": "ambiguous",
                "options": [
                    food._safe_display(item, limit=60)
                    for item in resolved.get("ambiguous", [])[:3]
                ],
            }
        return {"error": "not_found"}

    async def _resolve_tag(self, tag: str) -> dict[str, Any]:
        try:
            payload = await self._get_json(
                food.FOOD_TAGS_SEARCH_PATH, {"q": tag}
            )
        except FoodTimeoutError:
            return {"error": "timeout"}
        except (FoodHTTPError, FoodTransportError, food.FoodAPIError):
            return {"error": "error"}
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        except Exception:
            return {"error": "error"}
        return food.resolve_food_search_tag(
            food.extract_food_items(payload), tag
        )

    async def _run_search(
        self,
        event: AstrMessageEvent,
        request: dict[str, Any],
        *,
        tool: bool = False,
    ):
        timeout_key = "food_search_timeout_reply" if tool else "food_timeout_reply"
        error_key = "food_search_error_reply" if tool else "food_error_reply"
        empty_key = "food_search_empty_reply" if tool else "food_empty_reply"
        location_not_found_key = (
            "food_search_location_not_found_reply"
            if tool
            else "food_location_not_found_reply"
        )
        location_ambiguous_key = (
            "food_search_ambiguous_location_reply"
            if tool
            else "food_ambiguous_location_reply"
        )

        tag_ids: list[int] = []
        tag_names: list[str] = []
        for tag in request.get("tags", []):
            resolved = await self._resolve_tag(tag)
            if resolved.get("error") == "timeout":
                return self._plain(event, self._reply(timeout_key))
            if resolved.get("error"):
                self._log_info("search tag api failure")
                return self._plain(event, self._reply(error_key))
            if resolved.get("id") is not None:
                tag_ids.append(resolved["id"])
                tag_names.append(_text(resolved.get("name")) or tag)
            elif resolved.get("ambiguous"):
                return self._plain(
                    event,
                    self._reply(
                        "food_search_ambiguous_tag_reply",
                        tag=tag,
                        options="、".join(resolved.get("ambiguous", [])[:4]),
                    ),
                )
            else:
                return self._plain(
                    event,
                    self._reply("food_search_unknown_tag_reply", tags=tag),
                )

        location_id: Any = None
        location_name = ""
        search_name = _text(request.get("name"))
        context: list[str] = []
        policy_params = self._capability_params(event)
        if not request.get("location") and policy_params.get("default_city"):
            request["location"] = _text(policy_params.get("default_city"))
        if request.get("location"):
            resolved = await self._resolve_search_location(
                request["location"]
            )
            if resolved.get("error") == "timeout":
                return self._plain(event, self._reply(timeout_key))
            if resolved.get("error") == "ambiguous":
                return self._plain(
                    event,
                    self._reply(
                        location_ambiguous_key,
                        location=request["location"],
                        options="；".join(resolved.get("options", [])[:3]),
                    ),
                )
            if resolved.get("error") == "not_found":
                return self._plain(
                    event,
                    self._reply(
                        location_not_found_key,
                        location=request["location"],
                    ),
                )
            if resolved.get("error"):
                self._log_info("search location api failure")
                return self._plain(event, self._reply(error_key))
            location_id = resolved.get("id")
            location_name = _text(resolved.get("name"))
        else:
            context = self._group_location_parts(event)
            if context:
                group_location = await self._resolve_group_location(context)
                if group_location is not None:
                    location_id = group_location.get("id")
                    location_name = _text(group_location.get("name"))

        if context and search_name:
            stripped, matched = food.strip_food_group_city_prefix(
                search_name, context
            )
            if matched:
                search_name = stripped

        params: dict[str, Any] = {}
        if search_name:
            params["q"] = search_name
            params["search_type"] = "name"
        if tag_ids:
            params["tag_ids"] = ",".join(str(item) for item in tag_ids)
        if location_id is not None:
            params["location_id"] = str(location_id)
        try:
            payload = await self._get_json(
                food.FOOD_RESTAURANTS_PATH, params
            )
        except FoodTimeoutError:
            return self._plain(event, self._reply(timeout_key))
        except asyncio.TimeoutError:
            return self._plain(event, self._reply(timeout_key))
        except (FoodHTTPError, FoodTransportError, food.FoodAPIError):
            self._log_info("search restaurants api failure")
            return self._plain(event, self._reply(error_key))
        except Exception:
            self._log_info("search restaurants internal failure")
            return self._plain(event, self._reply(error_key))
        items = food.extract_food_items(payload)
        items = food.sort_food_restaurants(
            items, sort=request.get("sort"), order=request.get("order")
        )[: int(request.get("limit") or food.FOOD_SEARCH_HARD_LIMIT)]
        if not items:
            return self._plain(event, self._reply(empty_key))
        label = (
            search_name
            or ("、".join(tag_names) if tag_names else "餐厅")
        )
        text = food.format_food_search_results(
            items,
            label=label,
            location_name=location_name,
            max_results=request.get("limit") or food.FOOD_SEARCH_HARD_LIMIT,
            footer=f"更多：{food.FOOD_BASE_URL}",
            max_chars=self._cfg_int(
                "food_reply_max_chars", minimum=200, maximum=5000, default=1800
            ),
        )
        self._log_info(
            "search success count=%s sort=%s order=%s"
            % (
                len(items),
                request.get("sort") or food.FOOD_SEARCH_DEFAULT_SORT,
                request.get("order") or "desc",
            )
        )
        return self._plain(event, text)

    # ------------------------------------------------------------------
    # ABI v1 fixed provider methods (request_context/args -> contract dict)
    # ------------------------------------------------------------------
    def provider_status(self) -> dict[str, Any]:
        registry_status = (
            self._registry.status() if self._registry is not None else {"abi_version": contracts.ABI_VERSION, "providers": []}
        )
        return {
            "abi_version": contracts.ABI_VERSION,
            "ready": bool(self._contract_ready),
            "provider": PLUGIN_NAME,
            "capability": contracts.PROVIDER_CAPABILITY,
            "error": self._provider_error,
            "registry": registry_status,
        }

    def _contract_unavailable(self) -> dict[str, Any] | None:
        if self._contract_ready and self._contract_provider is not None:
            return None
        return ProviderResult.not_ready("Food provider 尚未就绪").to_dict()

    async def _contract_call(self, method: Any, request_context: Any, args: Any) -> dict[str, Any]:
        unavailable = self._contract_unavailable()
        if unavailable is not None:
            return unavailable
        try:
            return await method(request_context, args)
        except Exception:
            return ProviderResult.error(
                contracts.ERR_INTERNAL, "Food provider 内部错误"
            ).to_dict()

    async def food_query(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_query, request_context, args
        )

    async def food_locations(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_locations, request_context, args
        )

    async def food_tags(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_tags, request_context, args
        )

    async def food_set_location(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_set_location, request_context, args
        )

    async def food_change_tags(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_change_tags, request_context, args
        )

    async def food_draft_create(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_draft_create, request_context, args
        )

    async def food_draft_preview(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_draft_preview, request_context, args
        )

    async def food_confirm(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_confirm, request_context, args
        )

    async def food_cancel(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_cancel, request_context, args
        )

    async def food_operation_status(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._contract_call(
            self._contract_provider.food_operation_status, request_context, args
        )

    async def terminate(self) -> None:
        """AstrBot lifecycle cleanup: stop readiness and unregister provider."""

        self._contract_ready = False
        if self._registry is not None:
            try:
                self._registry.set_ready(PLUGIN_NAME, False)
            except Exception:
                pass
            try:
                self._registry.unregister(PLUGIN_NAME)
            except Exception:
                pass
        if self._ledger is not None:
            try:
                self._ledger.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Canonical subcommands and provider result rendering
    # ------------------------------------------------------------------
    @staticmethod
    def _split_subcommand(raw_args: str) -> tuple[str, str]:
        text = str(raw_args or "").strip()
        if not text:
            return "", ""
        parts = text.split(None, 1)
        token = parts[0].casefold()
        rest = parts[1].strip() if len(parts) > 1 else ""
        action = CANONICAL_SUBCOMMANDS.get(token, "")
        return action, rest

    def _contract_error_text(self, result: Mapping[str, Any]) -> str:
        data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        code = _text(result.get("error_code"))
        message = _text(result.get("message"))
        labels = {
            contracts.ERR_PERMISSION_DENIED: "无权执行这个 Food 操作。",
            contracts.ERR_NOT_READY: "Food provider 尚未就绪。",
            contracts.ERR_DEPENDENCY_MISSING: "Food 依赖服务不可用。",
            contracts.ERR_INVALID_ARGS: "Food 参数不合法。",
            contracts.ERR_DRAFT_NOT_FOUND: "草稿不存在或已被清理。",
            contracts.ERR_DRAFT_EXPIRED: "草稿已过期，请重新创建。",
            contracts.ERR_DRAFT_INCOMPLETE: "草稿字段尚未补全。",
            contracts.ERR_CONFIRMATION_REQUIRED: "请先预览草稿并取得确认，再执行确认。",
            contracts.ERR_CONFIRMATION_EXPIRED: "确认已过期，请重新预览草稿。",
            contracts.ERR_CONFIRMATION_INVALID: "确认凭证无效，请重新预览草稿。",
            contracts.ERR_REVISION_CONFLICT: "草稿内容已变更，旧确认已失效（409）。",
            contracts.ERR_IDEMPOTENCY_CONFLICT: "同一幂等键对应不同内容，拒绝执行（409）。",
            contracts.ERR_OUTCOME_UNKNOWN: "提交结果未知，禁止自动重试；可用 /food 状态 <OP_ID> 查询。",
            contracts.ERR_INVALID_STATE: "当前状态不允许该操作。",
            contracts.ERR_AMBIGUOUS_LOCATION: "地点匹配到多个结果，请写得更具体。",
            contracts.ERR_AMBIGUOUS_TAG: "标签匹配到多个结果，请写得更具体。",
            contracts.ERR_NOT_FOUND: "没有找到对应记录。",
            contracts.ERR_BACKEND_TIMEOUT: "Food 后端超时，请稍后再试。",
            contracts.ERR_BACKEND_ERROR: "Food 后端暂时不可用，请稍后再试。",
            "invalid_score": "分数需要在 0.1-5 之间。",
            "site_missing_field": "站点提示缺少必填字段。",
            "site_invalid_score": "站点提示分数不合法。",
            "site_location_exists": "位置已存在，请换更具体的写法。",
            "site_restaurant_not_found": "站点没有找到这家店。",
            "site_rejected": "站点拒绝了这次提交。",
        }
        text = labels.get(code, "")
        missing = data.get("missing_fields") if isinstance(data, Mapping) else None
        if code == contracts.ERR_DRAFT_INCOMPLETE and missing:
            text = "草稿还缺字段：" + "、".join(str(item) for item in list(missing)[:8])
        if code in (contracts.ERR_AMBIGUOUS_LOCATION, contracts.ERR_AMBIGUOUS_TAG):
            options = data.get("items") if isinstance(data, Mapping) else None
            if isinstance(options, list):
                names = [
                    _text(item.get("name") if isinstance(item, Mapping) else item)[:60]
                    for item in options[:5]
                ]
                names = [name for name in names if name]
                if names:
                    text += " 候选：" + "、".join(names)
        if not text:
            text = message or "Food 操作失败，请稍后再试。"
        return food.clamp_food_reply(
            text,
            max_chars=self._cfg_int(
                "food_reply_max_chars", minimum=200, maximum=5000, default=1800
            ),
        )

    @staticmethod
    def _draft_summary(data: Mapping[str, Any]) -> str:
        payload = data.get("normalized_payload")
        if not isinstance(payload, Mapping):
            payload = {}
        parts = []
        if payload.get("restaurant"):
            parts.append("店名：" + _text(payload.get("restaurant"))[:100])
        location = payload.get("location_text") or payload.get("location_id")
        if location not in (None, ""):
            parts.append("位置：" + _text(location)[:100])
        if payload.get("taste") is not None:
            parts.append("味道：" + food._format_number(payload.get("taste")))
        if payload.get("value") is not None:
            parts.append("性价比：" + food._format_number(payload.get("value")))
        if payload.get("tags"):
            parts.append("标签：" + "、".join(str(tag) for tag in list(payload.get("tags"))[:8]))
        if payload.get("comment"):
            parts.append("评价：" + _text(payload.get("comment"))[:100])
        return "；".join(parts)

    def _contract_result_text(
        self,
        result: Mapping[str, Any],
        *,
        action: str,
        request: Mapping[str, Any] | None = None,
    ) -> str:
        if not isinstance(result, Mapping):
            return "Food 服务返回格式异常。"
        if _text(result.get("status")) != "ok":
            return self._contract_error_text(result)
        data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        if action == "query":
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if not items:
                return self._reply("food_empty_reply")
            req = request or {}
            label = _text(req.get("name")) or "餐厅"
            return food.format_food_search_results(
                items,
                label=label,
                location_name=_text(req.get("location")),
                max_results=min(5, len(items)),
                footer=f"更多：{food.FOOD_BASE_URL}",
                max_chars=self._cfg_int(
                    "food_reply_max_chars", minimum=200, maximum=5000, default=1800
                ),
            )
        if action == "locations":
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if not items:
                return self._reply(
                    "food_location_not_found_reply",
                    location=_text((request or {}).get("query")),
                )
            lines = ["地点搜索结果："]
            for index, item in enumerate(items[:20], 1):
                if not isinstance(item, Mapping):
                    continue
                lines.append(
                    f"{index}. {_text(item.get('name'))[:100]}"
                    f"（{_text(item.get('path'))[:200]}） [ID:{item.get('id')}]"
                )
            if data.get("ambiguous"):
                lines.append("匹配到多个地点，请用更具体的路径再查。")
            if data.get("cursor"):
                lines.append("还有更多结果，请继续缩小查询范围。")
            return food.clamp_food_reply(
                "\n".join(lines),
                max_chars=self._cfg_int(
                    "food_reply_max_chars", minimum=200, maximum=5000, default=1800
                ),
            )
        if action == "tags":
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if not items:
                return self._reply(
                    "food_search_unknown_tag_reply",
                    tags=_text((request or {}).get("query")),
                )
            lines = ["标签搜索结果："]
            for index, item in enumerate(items[:50], 1):
                if not isinstance(item, Mapping):
                    continue
                lines.append(
                    f"{index}. {_text(item.get('name'))[:100]} [ID:{item.get('id')}]"
                )
            if data.get("ambiguous"):
                lines.append("匹配到多个标签，请写得更精确。")
            return food.clamp_food_reply(
                "\n".join(lines),
                max_chars=self._cfg_int(
                    "food_reply_max_chars", minimum=200, maximum=5000, default=1800
                ),
            )
        if action == "draft_create":
            draft_id = _text(data.get("draft_id"))
            status = _text(data.get("status"))
            if status == "collecting":
                missing = data.get("missing_fields") or []
                return (
                    f"草稿已创建：{draft_id}\n"
                    + "还缺字段："
                    + "、".join(str(item) for item in list(missing)[:8])
                    + "\n请用 /food 提交 ... 补全，或重新创建。"
                )[:1800]
            return f"草稿已创建：{draft_id}\n{self._draft_summary(data)}\n请先预览后再确认。"
        if action == "draft_preview":
            draft_id = _text(data.get("draft_id"))
            summary = self._draft_summary(data)
            return (
                f"草稿已就绪：{draft_id}\n{summary}\n"
                f"回复 /food 确认 {draft_id} 即可提交；确认内容变更会失效。"
            )[:1800]
        if action == "confirm":
            state = _text(data.get("state"))
            op_id = _text(data.get("op_id"))
            remote = data.get("remote_data") if isinstance(data.get("remote_data"), Mapping) else {}
            if state == "succeeded":
                if remote.get("already_submitted"):
                    return self._reply("food_submit_duplicate_username_reply")
                name = _text(remote.get("restaurant_name"))
                return (
                    (f"已提交：{name}\n" if name else "已提交。\n")
                    + (f"操作ID：{op_id}" if op_id else "")
                ).strip()
            return f"操作已完成：{state}" + (f"\n操作ID：{op_id}" if op_id else "")
        if action == "cancel":
            return f"草稿已取消：{_text(data.get('draft_id'))}"
        if action == "status":
            state = _text(data.get("state") or data.get("status"))
            op_id = _text(data.get("op_id"))
            draft_id = _text(data.get("draft_id"))
            side = _text(data.get("side_effect_state"))
            retryable = "是" if data.get("retryable") else "否"
            lines = ["Food 操作状态：", f"操作ID：{op_id}", f"草稿ID：{draft_id}", f"状态：{state}"]
            if side:
                lines.append(f"副作用：{side}")
            lines.append(f"可重试：{retryable}")
            if state == "outcome_unknown":
                lines.append("结果未知，禁止自动重试。")
            return food.clamp_food_reply(
                "\n".join(lines),
                max_chars=self._cfg_int(
                    "food_reply_max_chars", minimum=200, maximum=5000, default=1800
                ),
            )
        return "Food 操作完成。"

    def _parse_draft_command_args(self, rest: str) -> dict[str, Any]:
        try:
            tokens = shlex.split(str(rest or ""))
        except ValueError:
            raise ValueError("参数引号不匹配")
        aliases = {
            "店名": "restaurant",
            "name": "restaurant",
            "restaurant": "restaurant",
            "位置": "location",
            "location": "location",
            "位置id": "location_id",
            "location_id": "location_id",
            "味道": "taste",
            "味道分": "taste",
            "taste": "taste",
            "性价比": "value",
            "性价比分": "value",
            "value": "value",
            "评价": "comment",
            "评论": "comment",
            "comment": "comment",
            "标签": "tags",
            "tag": "tags",
            "tags": "tags",
            "标签id": "tag_ids",
            "tag_ids": "tag_ids",
            "幂等键": "idempotency_key",
            "idempotency_key": "idempotency_key",
            "draft_id": "draft_id",
        }
        result: dict[str, Any] = {}
        for token in tokens:
            if "=" not in token:
                raise ValueError("参数需要 key=value")
            key, value = token.split("=", 1)
            key = str(key).strip().casefold()
            field = aliases.get(key, "")
            if not field:
                raise ValueError("未知参数")
            if field == "tag_ids":
                result[field] = [
                    part.strip() for part in value.split(",") if part.strip()
                ]
            elif field == "location_id":
                try:
                    result[field] = int(value)
                except (TypeError, ValueError):
                    raise ValueError("位置ID非法")
            else:
                result[field] = value.strip()
        return result

    async def _handle_canonical_command(
        self, event: AstrMessageEvent, action: str, rest: str
    ):
        ctx = self._contract_context(event)
        if action == "query":
            request = self._parse_search_command(rest) if rest else {
                "ok": True,
                "name": "",
                "location": "",
                "tags": [],
                "sort": "total",
                "order": "desc",
                "limit": max(1, self._cfg_int("food_max_results", default=5)),
            }
            if not request.get("ok"):
                return self._plain(
                    event, self._invalid_search_reply(request.get("error", ""), tool=False)
                )
            provider_args = {
                "query_text": request.get("name") or "",
                "location": request.get("location") or "",
                "tags": request.get("tags") or [],
                "sort": request.get("sort") or "total",
                "order": request.get("order") or "desc",
                "limit": request.get("limit") or 5,
            }
            if not provider_args["location"]:
                context_parts = self._group_location_parts(event)
                if context_parts:
                    provider_args["location"] = context_parts[-1]
            result = await self.food_query(ctx, provider_args)
            return self._plain(
                event,
                self._contract_result_text(result, action="query", request=request),
            )
        if action == "locations":
            query = _text(rest)
            if not query:
                return self._plain(event, "用法：/food 位置 <地名>，例如 /food 位置 南关区")
            result = await self.food_locations(ctx, {"query": query, "limit": 20})
            return self._plain(
                event,
                self._contract_result_text(result, action="locations", request={"query": query}),
            )
        if action == "tags":
            query = _text(rest)
            if not query:
                return self._plain(event, "用法：/food 标签 <标签名>，例如 /food 标签 火锅")
            result = await self.food_tags(ctx, {"query": query, "limit": 50})
            return self._plain(
                event,
                self._contract_result_text(result, action="tags", request={"query": query}),
            )
        if action == "submit":
            try:
                draft_args = self._parse_draft_command_args(rest)
            except ValueError as exc:
                return self._plain(event, f"提交参数不合法：{exc}")
            if not draft_args:
                return self._plain(
                    event,
                    "用法：/food 提交 店名=... 位置=... 味道=4.5 性价比=3.5 评价=...",
                )
            created = await self.food_draft_create(ctx, draft_args)
            if _text(created.get("status")) != "ok":
                return self._plain(event, self._contract_error_text(created))
            data = created.get("data") if isinstance(created.get("data"), Mapping) else {}
            if _text(data.get("status")) == "collecting":
                return self._plain(
                    event, self._contract_result_text(created, action="draft_create")
                )
            draft_id = _text(data.get("draft_id"))
            preview = await self.food_draft_preview(ctx, {"draft_id": draft_id})
            if _text(preview.get("status")) != "ok":
                return self._plain(event, self._contract_error_text(preview))
            return self._plain(
                event, self._contract_result_text(preview, action="draft_preview")
            )
        if action == "confirm":
            tokens = rest.split()
            if not tokens:
                return self._plain(event, "用法：/food 确认 <DRAFT_ID> [确认令牌]")
            draft_id = tokens[0]
            confirmation_ref = tokens[1] if len(tokens) > 1 else ""
            result = await self.food_confirm(
                ctx,
                {"draft_id": draft_id, "confirmation_ref": confirmation_ref},
            )
            return self._plain(
                event, self._contract_result_text(result, action="confirm")
            )
        if action == "cancel":
            draft_id = _text(rest.split()[0] if rest.split() else "")
            if not draft_id:
                return self._plain(event, "用法：/food 取消 <DRAFT_ID>")
            result = await self.food_cancel(ctx, {"draft_id": draft_id})
            text = self._contract_result_text(result, action="cancel")
            return self._plain(event, text)
        if action == "status":
            op_id = _text(rest.split()[0] if rest.split() else "")
            if not op_id:
                return self._plain(event, "用法：/food 状态 <OP_ID>")
            result = await self.food_operation_status(ctx, {"op_id": op_id})
            if _text(result.get("status")) != "ok":
                return self._plain(event, self._contract_error_text(result))
            return self._plain(
                event, self._contract_result_text(result, action="status")
            )
        return None

    # ------------------------------------------------------------------
    # AstrBot command handler
    # ------------------------------------------------------------------
    @filter.command("food", alias={"美食", "吃什么", "饭店"}, priority=110)
    async def food_command(self, event: AstrMessageEvent):
        _call_shall_not_llm(event)
        if not self._capability_enabled(event):
            _stop_event(event)
            return
        try:
            if not self._cfg_bool("food_enabled"):
                result = self._plain(event, self._reply("food_disabled_reply"))
                if result is not None:
                    yield result
                return
            raw_args = self._command_argument(event)
            if not raw_args:
                result = self._plain(event, self._reply("food_usage_reply"))
                if result is not None:
                    yield result
                return
            action, rest = self._split_subcommand(raw_args)
            if action:
                # Canonical read/write subcommands are deterministic and do
                # not invoke the chat model.  Query commands pass through the
                # same cooldown as the legacy search form.
                if action == "query":
                    remaining = self._command_cooldown_remaining(event)
                    if remaining > 0:
                        result = self._plain(
                            event,
                            self._reply("food_cooldown_reply", seconds=remaining),
                        )
                        if result is not None:
                            yield result
                        return
                result = await self._handle_canonical_command(
                    event, action, rest
                )
                if result is not None:
                    yield result
                return
            remaining = self._command_cooldown_remaining(event)
            if remaining > 0:
                result = self._plain(
                    event,
                    self._reply("food_cooldown_reply", seconds=remaining),
                )
                if result is not None:
                    yield result
                return
            request = self._parse_search_command(raw_args)
            if not request.get("ok"):
                result = self._plain(
                    event, self._invalid_search_reply(request.get("error", ""), tool=False)
                )
                if result is not None:
                    yield result
                return
            result = await self._run_search(event, request, tool=False)
            if result is not None:
                yield result
        except Exception:
            self._log_info("command internal failure")
            try:
                result = self._plain(event, self._reply("food_error_reply"))
                if result is not None:
                    yield result
            except Exception:
                pass
        finally:
            _stop_event(event)

    # ------------------------------------------------------------------
    # Safe-tool endpoints used by qqbot_safe_tools
    # ------------------------------------------------------------------
    async def tool_search_restaurants(
        self,
        event: AstrMessageEvent,
        args: dict[str, str],
        clean_text: str,
    ):
        """Return an AstrBot result, or None, and never raise."""

        if event is None:
            return None
        if not self._capability_enabled(event):
            return None
        try:
            if not (
                self._cfg_bool("food_search_enabled")
                and self._cfg_bool("safe_tools_allow_search_food_restaurants")
            ):
                return self._plain(event, self._reply("food_disabled_reply"))
            request = self._normalize_search_tool_args(args)
            if not request.get("ok"):
                return self._plain(
                    event,
                    self._invalid_search_reply(request.get("error", ""), tool=True),
                )
            remaining = self._search_tool_cooldown_remaining(event)
            if remaining > 0:
                return self._plain(
                    event,
                    self._reply("food_cooldown_reply", seconds=remaining),
                )
            return await self._run_search(event, request, tool=True)
        except asyncio.TimeoutError:
            return self._plain(event, self._reply("food_search_timeout_reply"))
        except Exception:
            self._log_info("search tool internal failure")
            return self._plain(event, self._reply("food_search_error_reply"))

    def _submit_flag_enabled(self) -> bool:
        return bool(
            self._cfg_bool("food_submit_enabled")
            and self._cfg_bool("safe_tools_allow_submit_food_review")
        )

    @staticmethod
    def _submit_key(event: AstrMessageEvent) -> tuple[str, str]:
        sender = _event_sender_id(event)
        if not sender:
            return "", ""
        return state_key(_event_umo(event), sender), sender

    def _username(self, event: AstrMessageEvent) -> str:
        limit = max(
            1, self._cfg_int("food_submit_username_max_chars", default=32)
        )
        nickname = food.sanitize_food_submit_text(
            _event_sender_name(event), max_chars=limit
        )
        if nickname:
            return nickname
        fallback = food.sanitize_food_submit_text(
            self._cfg_str("food_submit_username_fallback"), max_chars=limit
        )
        return fallback or "群友"

    def _validate_submit_fields(self, args: dict[str, Any]) -> dict[str, Any]:
        return food.validate_food_submission(
            args,
            name_max=max(
                1, self._cfg_int("food_submit_name_max_chars", default=100)
            ),
            location_max=max(
                1, self._cfg_int("food_submit_location_max_chars", default=60)
            ),
            comment_max=max(
                1, self._cfg_int("food_submit_comment_max_chars", default=500)
            ),
            tag_max=max(
                1, self._cfg_int("food_submit_tag_max_chars", default=20)
            ),
            tag_count=max(
                0, self._cfg_int("food_submit_tags_max_count", default=5)
            ),
        )

    def _missing_fields(
        self, fields: dict[str, Any], invalid: list[str]
    ) -> list[str]:
        return [
            key
            for key, _label in food.FOOD_SUBMIT_REQUIRED
            if key not in fields and key not in invalid
        ]

    def _missing_reply(self, missing: list[str]) -> str:
        return self._reply(
            "food_submit_missing_reply",
            missing="、".join(food.food_submit_missing_labels(missing)),
        )

    def _score_reply(self, invalid: list[str]) -> str:
        return self._reply(
            "food_submit_score_reply",
            fields="、".join(food.food_submit_missing_labels(invalid)),
        )

    def _confirmation_reply(self, fields: dict[str, Any]) -> str:
        return self._reply(
            "food_submit_confirm_reply",
            name=food._safe_display(fields.get("restaurant"), limit=100),
            location=food._safe_display(fields.get("location"), limit=100),
            taste=food._format_number(fields.get("taste")),
            value=food._format_number(fields.get("value")),
        )

    def _success_reply(self, fields: dict[str, Any]) -> str:
        return self._reply(
            "food_submit_success_reply",
            name=food._safe_display(fields.get("restaurant"), limit=100),
            location=food._safe_display(fields.get("location"), limit=100),
            taste=food._format_number(fields.get("taste")),
            value=food._format_number(fields.get("value")),
        )

    def _submit_error_text(
        self,
        result: dict[str, Any],
        fields: dict[str, Any],
        *,
        password: str,
    ) -> str:
        kind = _text(result.get("kind"))
        if kind == "duplicate_username":
            return self._reply("food_submit_duplicate_username_reply")
        if kind == "location_exists":
            return self._reply(
                "food_submit_location_exists_reply",
                location=food._safe_display(fields.get("location"), limit=100),
            )
        if kind == "restaurant_not_found":
            return self._reply("food_submit_restaurant_not_found_reply")
        if kind in ("missing_field", "invalid_score", "rejected"):
            reason = food.safe_food_error_reason(
                result, forbidden=password, max_chars=80
            )
            return self._reply("food_submit_invalid_reply", reason=reason)
        return self._reply("food_submit_error_reply")

    @staticmethod
    def _normalize_submit_args(args: Any) -> tuple[dict[str, Any] | None, str]:
        if not isinstance(args, dict):
            return None, "shape"
        unknown = {str(key) for key in args.keys()} - _SUBMIT_ALLOWED_KEYS
        if unknown:
            return None, "unknown"
        values: dict[str, Any] = {
            "restaurant": args.get("restaurant")
            if args.get("restaurant") not in (None, "")
            else args.get("name"),
            "location": args.get("location"),
            "taste": args.get("taste"),
            "value": args.get("value"),
            "comment": args.get("comment"),
        }
        tags = args.get("tags")
        if tags in (None, "", []):
            tags = args.get("tag")
        if tags not in (None, "", []):
            values["tags"] = tags
        return values, ""

    async def tool_submit_food_review(
        self,
        event: AstrMessageEvent,
        args: dict[str, str],
        clean_text: str,
    ):
        """Create/merge a pending draft and ask for confirmation.

        This method never POSTs and never raises.  A later bare confirmation is
        handled deterministically by ``on_food_message``.
        """

        if event is None:
            return None
        try:
            if not self._submit_flag_enabled():
                return self._plain(event, self._reply("food_submit_disabled_reply"))
            key, password = self._submit_key(event)
            if not key or not password:
                return self._plain(event, self._reply("food_submit_no_sender_reply"))
            values, reason = self._normalize_submit_args(args)
            if values is None:
                return self._plain(
                    event, self._reply("food_submit_tool_rejected_reply")
                )
            if not any(
                values.get(field) not in (None, "", [])
                for field in ("restaurant", "location", "taste", "value", "comment", "tags")
            ):
                return self._plain(
                    event, self._reply("food_submit_tool_rejected_reply")
                )

            existing = self._state.get_draft(key)
            existing_pending = bool(
                existing and existing.get("status") == "pending_confirmation"
            )
            if existing and existing.get("status") == "submitting":
                return self._plain(
                    event, self._reply("food_submit_processing_reply")
                )

            validation = self._validate_submit_fields(values)
            fields = dict(validation.get("fields") or {})
            invalid = list(validation.get("invalid") or [])
            if existing_pending:
                draft = self._state.merge_draft(key, fields)
            elif existing and existing.get("status") == "submitted":
                draft = self._state.merge_draft(key, fields)
                if draft and draft.get("status") == "submitted":
                    return self._plain(
                        event,
                        self._reply("food_submit_already_submitted_reply"),
                    )
            else:
                draft = self._state.save_draft(key, fields)
            if not draft:
                return self._plain(
                    event, self._reply("food_submit_tool_rejected_reply")
                )

            merged = self._apply_group_location(
                event, dict(draft.get("fields") or {})
            )
            if merged != draft.get("fields"):
                draft = self._state.save_draft(key, merged)
            fields = dict((draft or {}).get("fields") or {})
            if not fields:
                return self._plain(
                    event, self._reply("food_submit_tool_rejected_reply")
                )

            # Recompute after the group location merged into the draft.
            missing = self._missing_fields(fields, invalid)
            if missing or invalid:
                if missing:
                    self._state.save_draft(key, fields)
                return self._plain(
                    event,
                    "\n".join(
                        part
                        for part in (
                            self._missing_reply(missing) if missing else "",
                            self._score_reply(invalid) if invalid else "",
                        )
                        if part
                    ),
                )

            natural_text = _text(clean_text)
            explicit_intent = food.detect_food_add_intent(
                natural_text
            ) or food.is_food_submit_confirmation(natural_text)
            if (
                natural_text
                and not explicit_intent
                and not existing_pending
            ):
                self._state.save_draft(key, fields)
                return self._plain(
                    event, self._reply("food_submit_ask_add_reply")
                )

            self._state.save_draft(key, fields)
            return self._plain(event, self._confirmation_reply(fields))
        except Exception:
            self._log_info("submit tool internal failure")
            return self._plain(event, self._reply("food_submit_error_reply"))

    # ------------------------------------------------------------------
    # Location resolution for submission
    # ------------------------------------------------------------------
    async def _resolve_submit_location(self, location: str) -> dict[str, Any]:
        parts = food.split_food_location_path(
            location,
            max_parts=max(
                1, self._cfg_int("food_submit_location_max_parts", default=6)
            ),
            max_part_chars=max(
                1,
                self._cfg_int("food_submit_location_part_max_chars", default=30),
            ),
        )
        if not parts:
            return {"error": "unsafe"}
        for depth in range(len(parts), 0, -1):
            prefix = food.format_food_location_path(parts[:depth])
            try:
                payload = await self._get_json(
                    food.FOOD_LOCATIONS_PATH, {"q": prefix}
                )
            except FoodTimeoutError:
                return {"error": "timeout"}
            except asyncio.TimeoutError:
                return {"error": "timeout"}
            except (FoodHTTPError, FoodTransportError, food.FoodAPIError):
                return {"error": "error"}
            except Exception:
                return {"error": "error"}
            items = food.extract_food_items(payload)
            match = food.match_food_location(parts, items)
            if match.get("kind") == "full" and match.get("id") is not None:
                return {
                    "location_id": match.get("id"),
                    "name": _text(match.get("name")),
                }
            if match.get("kind") == "prefix" and match.get("id") is not None:
                tail = list(match.get("tail") or [])
                return {
                    "new_location_name": food.format_food_location_path(tail),
                    "new_location_parent_id": match.get("id"),
                }
            if match.get("kind") == "ambiguous":
                return {
                    "error": "ambiguous",
                    "options": [
                        food._safe_display(item, limit=60)
                        for item in (match.get("options") or [])[:3]
                    ],
                }
            if depth == len(parts) and len(parts) == 1:
                item = food.select_food_location(items, parts[0])
                if item and item.get("id") is not None:
                    return {
                        "location_id": item.get("id"),
                        "name": _text(item.get("name")),
                    }
        return {
            "new_location_name": food.format_food_location_path(parts),
            "new_location_parent_id": None,
        }

    # ------------------------------------------------------------------
    # Actual POST (single write)
    # ------------------------------------------------------------------
    async def _perform_submit(
        self,
        event: AstrMessageEvent,
        key: str,
        fields: dict[str, Any],
        password: str,
    ):
        try:
            if not _text(password):
                self._state.restore_pending(key)
                return self._plain(
                    event, self._reply("food_submit_no_sender_reply")
                )
            fields = self._apply_group_location(event, dict(fields))
            validation = self._validate_submit_fields(fields)
            fields = dict(validation.get("fields") or {})
            invalid = list(validation.get("invalid") or [])
            missing = self._missing_fields(fields, invalid)
            if missing or invalid:
                self._state.restore_pending(key)
                if fields:
                    self._state.save_draft(key, fields)
                return self._plain(
                    event,
                    "\n".join(
                        part
                        for part in (
                            self._missing_reply(missing) if missing else "",
                            self._score_reply(invalid) if invalid else "",
                        )
                        if part
                    ),
                )
            resolved = await self._resolve_submit_location(
                _text(fields.get("location"))
            )
            if resolved.get("error") == "ambiguous":
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                return self._plain(
                    event,
                    self._reply(
                        "food_submit_ambiguous_location_reply",
                        location=food._safe_display(
                            fields.get("location"), limit=100
                        ),
                        options="；".join(resolved.get("options", [])[:3])
                        or "多个同名地点",
                    ),
                )
            if resolved.get("error") == "timeout":
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                return self._plain(
                    event, self._reply("food_submit_timeout_reply")
                )
            if resolved.get("error") == "unsafe":
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                return self._plain(
                    event, self._reply("food_submit_location_invalid_reply")
                )
            if resolved.get("error"):
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                self._log_info("submit location resolution failure")
                return self._plain(
                    event, self._reply("food_submit_error_reply")
                )
            payload = food.build_food_submit_payload(
                fields,
                username=self._username(event),
                # The sender QQ is injected here only.  It never reaches a
                # tool argument, reply, log line or URL.
                password=password,
                location_id=resolved.get("location_id"),
                new_location_name=_text(resolved.get("new_location_name")),
                new_location_parent_id=resolved.get(
                    "new_location_parent_id"
                ),
            )
            if not payload:
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                return self._plain(
                    event, self._reply("food_submit_error_reply")
                )
            self._state.record_submit_attempt(key)
            try:
                status, body = await self._post_submit(payload)
            except FoodTimeoutError:
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                self._log_info("submit failure kind=timeout")
                return self._plain(
                    event, self._reply("food_submit_timeout_reply")
                )
            except asyncio.TimeoutError:
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                self._log_info("submit failure kind=timeout")
                return self._plain(
                    event, self._reply("food_submit_timeout_reply")
                )
            except FoodResponseTooLarge:
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                self._log_info("submit failure kind=oversize")
                return self._plain(
                    event, self._reply("food_submit_error_reply")
                )
            except (FoodTransportError, food.FoodAPIError):
                self._state.restore_pending(key)
                self._state.save_draft(key, fields)
                self._log_info("submit failure kind=transport")
                return self._plain(
                    event, self._reply("food_submit_error_reply")
                )
            result = food.parse_food_submit_response(status, body)
            if result.get("ok"):
                self._state.mark_submitted(key)
                self._log_info("submit success")
                return self._plain(event, self._success_reply(fields))
            if result.get("kind") == "duplicate_username":
                # Same sender / same restaurant already exists: the site has
                # already recorded this review, so never retry the POST.
                self._state.mark_submitted(key, duplicate=True)
                self._log_info("submit duplicate username")
                return self._plain(
                    event, self._reply("food_submit_duplicate_username_reply")
                )
            self._state.restore_pending(key)
            self._state.save_draft(key, fields)
            self._log_info(
                "submit failure kind=%s" % _text(result.get("kind"))[:40]
            )
            return self._plain(
                event,
                self._submit_error_text(result, fields, password=password),
            )
        except Exception:
            try:
                self._state.restore_pending(key)
            except Exception:
                pass
            self._log_info("submit internal failure")
            return self._plain(event, self._reply("food_submit_error_reply"))

    # ------------------------------------------------------------------
    # Bare confirmation handler (no LLM)
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=120)
    async def on_food_message(self, event: AstrMessageEvent):
        if not self._capability_enabled(event):
            return
        handled = False
        claimed_key = ""
        try:
            text = _event_message_text(event)
            if not text or text[:1] in ("/", "／"):
                return
            if not food.is_food_submit_confirmation(text):
                return
            if not self._submit_flag_enabled():
                return
            key, password = self._submit_key(event)
            if not key or not password:
                return
            draft = self._state.get_draft(key)
            if not draft:
                return
            handled = True
            _call_shall_not_llm(event)
            status = _text(draft.get("status"))
            if status == "submitted":
                result = self._plain(
                    event,
                    self._reply("food_submit_already_submitted_reply"),
                )
                if result is not None:
                    yield result
                return
            if status == "submitting":
                result = self._plain(
                    event, self._reply("food_submit_processing_reply")
                )
                if result is not None:
                    yield result
                return
            remaining = self._state.cooldown_remaining(
                key,
                max(
                    0,
                    self._cfg_int(
                        "food_submit_cooldown_seconds", default=60
                    ),
                ),
            )
            if remaining > 0:
                result = self._plain(
                    event,
                    self._reply(
                        "food_submit_cooldown_reply", seconds=remaining
                    ),
                )
                if result is not None:
                    yield result
                return
            claim = self._state.claim_submit(key)
            if claim == "submitted":
                result = self._plain(
                    event,
                    self._reply("food_submit_already_submitted_reply"),
                )
                if result is not None:
                    yield result
                return
            if claim == "submitting":
                result = self._plain(
                    event, self._reply("food_submit_processing_reply")
                )
                if result is not None:
                    yield result
                return
            if claim != "claimed":
                result = self._plain(
                    event, self._reply("food_submit_error_reply")
                )
                if result is not None:
                    yield result
                return
            claimed_key = key
            result = await self._perform_submit(
                event, key, dict(draft.get("fields") or {}), password
            )
            claimed_key = ""
            if result is not None:
                yield result
        except Exception:
            if claimed_key:
                try:
                    self._state.restore_pending(claimed_key)
                except Exception:
                    pass
            if handled:
                self._log_info("confirmation internal failure")
                try:
                    result = self._plain(
                        event, self._reply("food_submit_error_reply")
                    )
                    if result is not None:
                        yield result
                except Exception:
                    pass
        finally:
            if handled:
                _stop_event(event)
