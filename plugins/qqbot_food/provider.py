"""ABI v1 Food provider implementation (fixed methods only).

The provider receives a RequestContext and a plain args mapping.  It never
accepts a URL, SQL statement, method name or free function from callers, and it
returns contract-shaped ProviderResult dictionaries.  All writes go through the
local SQLite ledger and the single Food backend adapter.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping

from . import contracts, food
from .api import FoodHTTPError, FoodResponseTooLarge, FoodTimeoutError, FoodTransportError
from .backend import BackendOutcome, FoodBackend
from .ledger import FoodLedger

_QUERY_ITEM_FIELDS = (
    "id",
    "name",
    "location_id",
    "location_name",
    "location_path",
    "tags",
    "taste_avg",
    "value_avg",
    "total_avg",
    "review_count",
    "latest_review_date",
    "is_closed",
)
_MAX_QUERY_LIMIT = 5
_MAX_LOCATION_LIMIT = 20
_MAX_TAG_LIMIT = 50


def _safe(value: Any, limit: int = 200) -> str:
    return food._safe_display(value, limit=limit)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _encode_cursor(offset: int, fingerprint: str) -> str:
    if int(offset) <= 0:
        return ""
    body = json.dumps(
        {"o": int(offset), "f": str(fingerprint)[:32]},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Any, fingerprint: str) -> int:
    text = str(cursor or "").strip()
    if not text:
        return 0
    try:
        padding = "=" * (-len(text) % 4)
        raw = base64.urlsafe_b64decode((text + padding).encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        if str(data.get("f", ""))[:32] != str(fingerprint)[:32]:
            return -1
        offset = int(data.get("o", 0) or 0)
        return max(0, offset)
    except (TypeError, ValueError, KeyError):
        return -1


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(contracts.canonical_json(value).encode("utf-8")).hexdigest()


class FoodContractProvider:
    """Fixed ABI v1 methods; public methods always return a ProviderResult dict."""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.ledger: FoodLedger = plugin._ledger
        self.backend: FoodBackend = plugin._backend
        self.issuer = plugin._confirmation_issuer

    # ---- dispatch/error plumbing ------------------------------------
    async def _dispatch(
        self,
        handler: Callable[[contracts.RequestContext, dict[str, Any]], Awaitable[contracts.ProviderResult]],
        request_context: Any,
        args: Any,
    ) -> dict[str, Any]:
        try:
            if not bool(getattr(self.plugin, "_contract_ready", False)):
                return contracts.ProviderResult.not_ready().to_dict()
            ctx = contracts.coerce_request_context(request_context)
            if ctx.deadline and ctx.deadline > 0 and self.plugin._now() > ctx.deadline:
                return contracts.ProviderResult.error(
                    contracts.ERR_INVALID_ARGS, "请求已过期", retryable=True
                ).to_dict()
            if not isinstance(args, Mapping):
                raise contracts.ContractError(
                    contracts.ERR_INVALID_ARGS, "args 必须为对象"
                )
            result = await handler(ctx, dict(args))
            if isinstance(result, contracts.ProviderResult):
                return result.to_dict()
            if isinstance(result, dict) and "status" in result:
                return result
            return contracts.ProviderResult.error(
                contracts.ERR_INTERNAL, "provider 返回格式错误"
            ).to_dict()
        except contracts.ContractError as exc:
            return contracts.ProviderResult.error(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                side_effect_state=exc.side_effect_state,
                data=exc.data,
            ).to_dict()
        except FoodTimeoutError:
            return contracts.ProviderResult.error(
                contracts.ERR_BACKEND_TIMEOUT,
                "Food 后端超时",
                retryable=True,
                side_effect_state="none",
            ).to_dict()
        except (FoodTransportError, FoodHTTPError, food.FoodAPIError):
            return contracts.ProviderResult.error(
                contracts.ERR_BACKEND_ERROR,
                "Food 后端暂时不可用",
                retryable=True,
                side_effect_state="none",
            ).to_dict()
        except asyncio.TimeoutError:
            return contracts.ProviderResult.error(
                contracts.ERR_BACKEND_TIMEOUT,
                "Food 后端超时",
                retryable=True,
                side_effect_state="none",
            ).to_dict()
        except Exception:
            return contracts.ProviderResult.error(
                contracts.ERR_INTERNAL, "Food provider 内部错误"
            ).to_dict()

    # ---- read helper APIs -------------------------------------------
    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        return await self.backend.get(path, params)

    @staticmethod
    def _scope(ctx: contracts.RequestContext) -> str:
        return contracts.scope_key(ctx)

    @staticmethod
    def _restaurant_item(item: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in _QUERY_ITEM_FIELDS:
            if key not in item:
                continue
            value = item.get(key)
            if key in ("id", "location_id"):
                value = _as_int(value, -1)
                if value < 0:
                    continue
            elif key in ("taste_avg", "value_avg", "total_avg"):
                value = _as_float(value, -1.0)
                if value < 0:
                    continue
            elif key == "review_count":
                value = max(0, _as_int(value, 0))
            elif key == "tags":
                tags = value if isinstance(value, (list, tuple)) else []
                value = [_safe(tag, 30) for tag in list(tags)[:10] if _safe(tag, 30)]
            elif key == "is_closed":
                value = bool(value)
            else:
                value = _safe(value, 200)
            result[key] = value
        return result

    @staticmethod
    def _location_item(item: Mapping[str, Any]) -> dict[str, Any]:
        ident = _as_int(item.get("id"), -1)
        if ident < 0:
            return {}
        return {
            "id": ident,
            "name": _safe(item.get("name"), 100),
            "path": _safe(item.get("path"), 300),
        }

    @staticmethod
    def _tag_item(item: Mapping[str, Any]) -> dict[str, Any]:
        ident = _as_int(item.get("id"), -1)
        if ident < 0:
            return {}
        return {"id": ident, "name": _safe(item.get("name"), 100)}

    @staticmethod
    def _page(
        items: list[dict[str, Any]],
        *,
        args: dict[str, Any],
        limit: int,
        fingerprint_payload: Any,
    ) -> tuple[list[dict[str, Any]], str]:
        offset = _decode_cursor(args.get("cursor"), _fingerprint(fingerprint_payload))
        if offset < 0:
            raise contracts.ContractError(
                contracts.ERR_INVALID_ARGS, "分页游标无效"
            )
        bounded = max(1, int(limit))
        page = items[offset : offset + bounded]
        next_cursor = _encode_cursor(
            offset + len(page), _fingerprint(fingerprint_payload)
        ) if offset + len(page) < len(items) else ""
        return page, next_cursor

    async def _search_locations(self, query: str) -> list[dict[str, Any]]:
        payload = await self._get(food.FOOD_LOCATIONS_PATH, {"q": query})
        return [
            item
            for item in (
                self._location_item(raw)
                for raw in food.extract_food_items(payload)
            )
            if item
        ]

    async def _search_tags(self, query: str) -> list[dict[str, Any]]:
        payload = await self._get(food.FOOD_TAGS_SEARCH_PATH, {"q": query})
        return [
            item
            for item in (
                self._tag_item(raw)
                for raw in food.extract_food_items(payload)
            )
            if item
        ]

    @staticmethod
    def _exact_matches(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        needle = _safe(query, 100).casefold()
        if not needle:
            return []
        return [
            item
            for item in items
            if _safe(item.get("name"), 100).casefold() == needle
        ]

    async def _resolve_location_id(
        self, query: str, *, require_unique: bool = True
    ) -> dict[str, Any]:
        items = await self._search_locations(query)
        exact = self._exact_matches(items, query)
        if len(exact) == 1:
            return {"id": exact[0]["id"], "name": exact[0]["name"], "items": items}
        if len(exact) > 1:
            return {"error": contracts.ERR_AMBIGUOUS_LOCATION, "items": exact}
        if len(items) == 1:
            return {"id": items[0]["id"], "name": items[0]["name"], "items": items}
        if items:
            return {
                "error": contracts.ERR_AMBIGUOUS_LOCATION
                if require_unique
                else "",
                "items": items,
            }
        return {"error": contracts.ERR_NOT_FOUND, "items": []}

    async def _resolve_tag_id(self, query: str) -> dict[str, Any]:
        items = await self._search_tags(query)
        exact = self._exact_matches(items, query)
        if len(exact) == 1:
            return {"id": exact[0]["id"], "name": exact[0]["name"], "items": items}
        if len(exact) > 1:
            return {"error": contracts.ERR_AMBIGUOUS_TAG, "items": exact}
        if len(items) == 1:
            return {"id": items[0]["id"], "name": items[0]["name"], "items": items}
        if items:
            return {"error": contracts.ERR_AMBIGUOUS_TAG, "items": items}
        return {"error": contracts.ERR_NOT_FOUND, "items": []}

    def _preference(self, ctx: contracts.RequestContext) -> dict[str, Any]:
        return self.ledger.get_scope_preference(
            instance_id=ctx.instance_id,
            platform=ctx.platform,
            bot_id=ctx.bot_id,
            scope_key=self._scope(ctx),
        )

    def _canonical_int(self, group: str, name: str, flat: str, legacy: str, default: int) -> int:
        try:
            return int(
                self.plugin._canonical_value(
                    group, name, flat_key=flat, legacy_key=legacy, default=default
                )
            )
        except (TypeError, ValueError):
            return int(default)

    def _canonical_str(self, group: str, name: str, flat: str, legacy: str, default: str) -> str:
        value = self.plugin._canonical_value(
            group, name, flat_key=flat, legacy_key=legacy, default=default
        )
        return _safe(value, 200) if value is not None else default

    def _idem_key(self, ctx: contracts.RequestContext, operation: str, key: str) -> str:
        raw = {
            "abi": contracts.ABI_VERSION,
            "instance_id": ctx.instance_id,
            "platform": ctx.platform,
            "bot_id": ctx.bot_id,
            "actor_id": ctx.actor_id,
            "scope_key": self._scope(ctx),
            "operation": str(operation),
            "business_key": str(key),
        }
        return hashlib.sha256(
            contracts.canonical_json(raw).encode("utf-8")
        ).hexdigest()

    # ---- fixed read methods ----------------------------------------
    async def food_query(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_query, request_context, args)

    async def _food_query(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        arg = dict(args)
        if "payload" in arg and isinstance(arg["payload"], Mapping):
            arg = dict(arg["payload"])
        query_text = food.sanitize_food_submit_text(
            arg.get("query_text") or arg.get("query") or arg.get("name") or "",
            max_chars=max(1, self._canonical_int("query", "name_max_chars", "query_name_max_chars", "food_search_name_max_chars", 50)),
        )
        if str(arg.get("query_text") or arg.get("query") or arg.get("name") or "").strip() and not query_text:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "查询词不合法")

        location_id = arg.get("location_id")
        location_name = ""
        if location_id not in (None, ""):
            location_id = _as_int(location_id, -1)
            if location_id < 0:
                raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "location_id 非法")
            location_name = _safe(arg.get("location_name"), 100)
        else:
            location_raw = arg.get("location_text") or arg.get("location")
            location_query = food.sanitize_food_submit_text(
                location_raw,
                max_chars=max(
                    1,
                    self._canonical_int(
                        "query",
                        "location_max_chars",
                        "query_location_max_chars",
                        "food_search_location_max_chars",
                        30,
                    ),
                ),
            )
            if str(location_raw or "").strip() and not location_query:
                raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "地点查询词不合法")
            if location_query:
                resolved = await self._resolve_location_id(location_query)
                if resolved.get("error"):
                    return contracts.ProviderResult.error(
                        str(resolved.get("error")),
                        "地点需要更明确的写法",
                        data={"items": resolved.get("items", [])[:20]},
                    )
                location_id = resolved["id"]
                location_name = resolved.get("name", "")
            else:
                pref_location = self._preference(ctx).get("location") or {}
                if isinstance(pref_location, Mapping):
                    location_id = pref_location.get("location_id")
                    location_name = _safe(pref_location.get("name"), 100)
                if location_id in (None, ""):
                    default_location = _safe(
                        self.plugin._canonical_value(
                            "query",
                            "default_location",
                            flat_key="query_default_location",
                            legacy_key="",
                            default="",
                        ),
                        100,
                    )
                    if default_location:
                        resolved_default = await self._resolve_location_id(
                            default_location
                        )
                        if resolved_default.get("error"):
                            return contracts.ProviderResult.error(
                                str(resolved_default.get("error")),
                                "默认地点需要更明确的写法",
                                data={"items": resolved_default.get("items", [])[:20]},
                            )
                        location_id = resolved_default["id"]
                        location_name = resolved_default.get("name", "")

        tag_ids: list[int] = []
        raw_tag_ids = arg.get("tag_ids")
        if raw_tag_ids not in (None, "", []):
            values = raw_tag_ids if isinstance(raw_tag_ids, (list, tuple)) else str(raw_tag_ids).split(",")
            for value in values:
                ident = _as_int(value, -1)
                if ident < 0:
                    raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "tag_id 非法")
                if ident not in tag_ids:
                    tag_ids.append(ident)
        else:
            raw_tags = arg.get("tags") or arg.get("tag")
            tag_values = food.split_food_tags(
                raw_tags,
                max_count=max(1, self._canonical_int("tags", "max_count", "tags_max_count", "food_search_max_tags", 3)),
                max_chars=max(1, self._canonical_int("tags", "name_length", "tags_name_length", "food_search_tag_max_chars", 20)),
            )
            if raw_tags not in (None, "", []) and not tag_values:
                raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "标签查询词不合法")
            for tag in tag_values:
                resolved = await self._resolve_tag_id(tag)
                if resolved.get("error"):
                    return contracts.ProviderResult.error(
                        str(resolved.get("error")),
                        "标签需要更明确的写法",
                        data={"items": resolved.get("items", [])[:20]},
                    )
                if resolved["id"] not in tag_ids:
                    tag_ids.append(resolved["id"])
            if not tag_ids:
                pref_tags = self._preference(ctx).get("tags") or []
                if isinstance(pref_tags, (list, tuple)):
                    tag_ids = [_as_int(item, -1) for item in pref_tags if _as_int(item, -1) >= 0][:10]
            if not tag_ids:
                default_tags = food.split_food_tags(
                    self.plugin._canonical_value(
                        "query",
                        "default_tags",
                        flat_key="query_default_tags",
                        legacy_key="",
                        default="",
                    ),
                    max_count=max(1, self._canonical_int("tags", "max_count", "tags_max_count", "food_search_max_tags", 3)),
                    max_chars=max(1, self._canonical_int("tags", "name_length", "tags_name_length", "food_search_tag_max_chars", 20)),
                )
                for tag in default_tags:
                    resolved_default_tag = await self._resolve_tag_id(tag)
                    if resolved_default_tag.get("error"):
                        return contracts.ProviderResult.error(
                            str(resolved_default_tag.get("error")),
                            "默认标签需要更明确的写法",
                            data={"items": resolved_default_tag.get("items", [])[:20]},
                        )
                    if resolved_default_tag["id"] not in tag_ids:
                        tag_ids.append(resolved_default_tag["id"])

        limit = arg.get("limit")
        limit = _as_int(
            limit,
            self._canonical_int("query", "limit", "query_limit", "food_search_default_limit", 5),
        )
        limit = max(1, min(_MAX_QUERY_LIMIT, limit))
        params: dict[str, Any] = {}
        if query_text:
            params["q"] = query_text
            params["search_type"] = "name"
        if location_id not in (None, ""):
            params["location_id"] = str(location_id)
        if tag_ids:
            params["tag_ids"] = ",".join(str(item) for item in tag_ids)
        payload = await self._get(food.FOOD_RESTAURANTS_PATH, params)
        items = [
            item
            for item in (
                self._restaurant_item(raw)
                for raw in food.extract_food_items(payload)
            )
            if item
        ]
        sort = food.normalize_food_search_sort(arg.get("sort")) or food.FOOD_SEARCH_DEFAULT_SORT
        order = food.normalize_food_search_order(arg.get("order"), sort=sort)
        items = food.sort_food_restaurants(items, sort=sort, order=order)
        fingerprint = {
            "query_text": query_text,
            "location_id": location_id,
            "tag_ids": tag_ids,
            "sort": sort,
            "order": order,
        }
        page, cursor = self._page(
            items, args=arg, limit=limit, fingerprint_payload=fingerprint
        )
        return contracts.ProviderResult.ok(
            {
                "items": page,
                "cursor": cursor,
                "source": "food.dlsz.top",
                "freshness": "fresh",
                "total": len(items),
            }
        )

    async def food_locations(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_locations, request_context, args)

    async def _food_locations(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        query = _safe(args.get("query") or args.get("q") or args.get("location"), 100)
        items = await self._search_locations(query) if query else []
        if not query:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少地点查询词")
        exact = self._exact_matches(items, query)
        ambiguous = len(exact) > 1 or (not exact and len(items) > 1)
        if bool(args.get("require_unique")) and ambiguous:
            return contracts.ProviderResult.error(
                contracts.ERR_AMBIGUOUS_LOCATION,
                "地点匹配到多个结果，请选择",
                data={"items": items[:20], "ambiguous": True},
            )
        limit = max(1, min(_MAX_LOCATION_LIMIT, _as_int(args.get("limit"), _MAX_LOCATION_LIMIT)))
        page, cursor = self._page(
            items, args=args, limit=limit, fingerprint_payload={"query": query}
        )
        return contracts.ProviderResult.ok(
            {
                "items": page,
                "cursor": cursor,
                "ambiguous": ambiguous,
                "source": "food.dlsz.top",
                "freshness": "fresh",
            }
        )

    async def food_tags(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_tags, request_context, args)

    async def _food_tags(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        query = _safe(args.get("query") or args.get("q") or args.get("tag"), 100)
        if not query:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少标签查询词")
        items = await self._search_tags(query)
        exact = self._exact_matches(items, query)
        ambiguous = len(exact) > 1 or (not exact and len(items) > 1)
        if bool(args.get("require_unique")) and ambiguous:
            return contracts.ProviderResult.error(
                contracts.ERR_AMBIGUOUS_TAG,
                "标签匹配到多个结果，请选择",
                data={"items": items[:20], "ambiguous": True},
            )
        limit = max(1, min(_MAX_TAG_LIMIT, _as_int(args.get("limit"), _MAX_TAG_LIMIT)))
        page, cursor = self._page(
            items, args=args, limit=limit, fingerprint_payload={"query": query}
        )
        return contracts.ProviderResult.ok(
            {
                "items": page,
                "cursor": cursor,
                "ambiguous": ambiguous,
                "source": "food.dlsz.top",
                "freshness": "fresh",
            }
        )

    # ---- location/tag write methods --------------------------------
    async def food_set_location(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_set_location, request_context, args)

    async def _food_set_location(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        location_id = args.get("location_id")
        location_name = ""
        if location_id not in (None, ""):
            location_id = _as_int(location_id, -1)
            if location_id < 0:
                raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "location_id 非法")
            location_name = _safe(args.get("location_name"), 100)
        else:
            query = _safe(args.get("location") or args.get("query"), 100)
            if not query:
                raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少地点")
            resolved = await self._resolve_location_id(query)
            if resolved.get("error"):
                return contracts.ProviderResult.error(
                    str(resolved.get("error")),
                    "地点需要更明确的写法",
                    data={"items": resolved.get("items", [])[:20]},
                )
            location_id = resolved["id"]
            location_name = resolved.get("name", "")
        visibility = _safe(
            args.get("visibility")
            or self.plugin._canonical_value(
                "location", "visibility", flat_key="location_visibility", legacy_key="", default="private"
            ),
            20,
        ) or "private"
        action_hash = _fingerprint(
            {"kind": "food_set_location", "location_id": location_id, "visibility": visibility}
        )
        confirmation = str(args.get("confirmation_ref") or "")
        if visibility == "scope_shared":
            if bool(args.get("preview")):
                token, expires = self.issuer.issue(
                    ctx,
                    kind="food_set_location",
                    payload_hash_value=action_hash,
                    extra={"location_id": location_id, "visibility": visibility},
                    now=self.plugin._now(),
                )
                return contracts.ProviderResult.ok(
                    {
                        "requires_confirmation": True,
                        "confirmation_ref": token,
                        "confirmation_expires_at": expires,
                    }
                )
            if not confirmation:
                return contracts.ProviderResult.error(
                    contracts.ERR_CONFIRMATION_REQUIRED, "共享位置写入需要确认"
                )
            validation = self.issuer.verify(
                confirmation,
                ctx,
                kind="food_set_location",
                payload_hash_value=action_hash,
                now=self.plugin._now(),
            )
            if not validation.valid:
                return contracts.ProviderResult.error(
                    validation.code, "位置写入确认无效或已过期"
                )
        result = self.ledger.set_scope_preference(
            instance_id=ctx.instance_id,
            platform=ctx.platform,
            bot_id=ctx.bot_id,
            scope_key=self._scope(ctx),
            location={
                "location_id": location_id,
                "name": location_name,
                "visibility": visibility,
            },
        )
        return contracts.ProviderResult.ok(
            {
                "location_id": location_id,
                "name": location_name,
                "visibility": visibility,
                "scope_key": self._scope(ctx),
                "updated_at": result.get("updated_at", 0),
            }
        )

    async def food_change_tags(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_change_tags, request_context, args)

    async def _food_change_tags(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        current = self._preference(ctx).get("tags") or []
        current_ids = [_as_int(item, -1) for item in current if _as_int(item, -1) >= 0]
        raw_ids = args.get("tag_ids")
        raw_names = args.get("tags") or args.get("tag")
        add_ids = args.get("add_tag_ids")
        remove_ids = args.get("remove_tag_ids")
        merge_mode = (
            args.get("replace") is False
            or add_ids not in (None, "", [])
            or remove_ids not in (None, "", [])
        )
        tag_ids: list[int] = []
        tag_names: list[str] = []
        if merge_mode:
            pass
        elif raw_ids not in (None, "", []):
            values = raw_ids if isinstance(raw_ids, (list, tuple)) else str(raw_ids).split(",")
            for value in values:
                ident = _as_int(value, -1)
                if ident < 0:
                    raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "tag_id 非法")
                if ident not in tag_ids:
                    tag_ids.append(ident)
        elif raw_names not in (None, "", []):
            names = food.split_food_tags(
                raw_names,
                max_count=max(1, self._canonical_int("tags", "max_count", "tags_max_count", "food_submit_tags_max_count", 5)),
                max_chars=max(1, self._canonical_int("tags", "name_length", "tags_name_length", "food_submit_tag_max_chars", 20)),
            )
            for name in names:
                resolved = await self._resolve_tag_id(name)
                if resolved.get("error"):
                    return contracts.ProviderResult.error(
                        str(resolved.get("error")),
                        "标签需要更明确的写法",
                        data={"items": resolved.get("items", [])[:20]},
                    )
                if resolved["id"] not in tag_ids:
                    tag_ids.append(resolved["id"])
                tag_names.append(resolved.get("name", name))
        else:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少标签")

        if merge_mode:
            merged = list(current_ids)
            for raw in (add_ids if isinstance(add_ids, (list, tuple)) else []):
                ident = _as_int(raw, -1)
                if ident >= 0 and ident not in merged:
                    merged.append(ident)
            remove_set = {
                _as_int(raw, -1)
                for raw in (remove_ids if isinstance(remove_ids, (list, tuple)) else [])
            }
            tag_ids = [ident for ident in merged if ident not in remove_set]
        max_tags = max(1, self._canonical_int("tags", "max_count", "tags_max_count", "food_submit_tags_max_count", 5))
        tag_ids = tag_ids[:max_tags]
        visibility = _safe(
            args.get("visibility")
            or self.plugin._canonical_value(
                "location", "visibility", flat_key="location_visibility", legacy_key="", default="private"
            ),
            20,
        ) or "private"
        action_hash = _fingerprint(
            {"kind": "food_change_tags", "tag_ids": tag_ids, "visibility": visibility}
        )
        confirmation = str(args.get("confirmation_ref") or "")
        if visibility == "scope_shared":
            if bool(args.get("preview")):
                token, expires = self.issuer.issue(
                    ctx,
                    kind="food_change_tags",
                    payload_hash_value=action_hash,
                    extra={"tag_ids": tag_ids, "visibility": visibility},
                    now=self.plugin._now(),
                )
                return contracts.ProviderResult.ok(
                    {
                        "requires_confirmation": True,
                        "confirmation_ref": token,
                        "confirmation_expires_at": expires,
                    }
                )
            if not confirmation:
                return contracts.ProviderResult.error(
                    contracts.ERR_CONFIRMATION_REQUIRED, "共享标签写入需要确认"
                )
            validation = self.issuer.verify(
                confirmation,
                ctx,
                kind="food_change_tags",
                payload_hash_value=action_hash,
                now=self.plugin._now(),
            )
            if not validation.valid:
                return contracts.ProviderResult.error(
                    validation.code, "标签写入确认无效或已过期"
                )
        self.ledger.set_scope_preference(
            instance_id=ctx.instance_id,
            platform=ctx.platform,
            bot_id=ctx.bot_id,
            scope_key=self._scope(ctx),
            tags=tag_ids,
        )
        return contracts.ProviderResult.ok(
            {
                "tag_ids": tag_ids,
                "tag_names": tag_names,
                "visibility": visibility,
                "scope_key": self._scope(ctx),
            }
        )

    # ---- draft methods ---------------------------------------------
    def _normalize_draft_payload(self, ctx: contracts.RequestContext, args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("payload") if isinstance(args.get("payload"), Mapping) else args
        raw = dict(raw or {})
        name_max = max(1, self._canonical_int("draft", "name_max_chars", "draft_name_max_chars", "food_submit_name_max_chars", 100))
        location_max = max(1, self._canonical_int("draft", "location_max_chars", "draft_location_max_chars", "food_submit_location_max_chars", 60))
        comment_max = max(1, self._canonical_int("draft", "comment_max_chars", "draft_comment_max_chars", "food_submit_comment_max_chars", 500))
        tag_max = max(1, self._canonical_int("tags", "name_length", "tags_name_length", "food_submit_tag_max_chars", 20))
        tag_count = max(1, self._canonical_int("tags", "max_count", "tags_max_count", "food_submit_tags_max_count", 5))

        restaurant_raw = raw.get("restaurant") if raw.get("restaurant") not in (None, "") else raw.get("name")
        restaurant = food.sanitize_food_submit_text(restaurant_raw, max_chars=name_max)
        if str(restaurant_raw or "").strip() and not restaurant:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "店名不合法")
        location_text_raw = raw.get("location_text") if raw.get("location_text") not in (None, "") else raw.get("location")
        location_text = food.sanitize_food_submit_text(location_text_raw, max_chars=location_max)
        if str(location_text_raw or "").strip() and not location_text:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "位置不合法")
        location_id = raw.get("location_id")
        if location_id not in (None, ""):
            location_id = _as_int(location_id, -1)
            if location_id < 0:
                raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "location_id 非法")
        else:
            location_id = None
        taste = food.parse_food_score(raw.get("taste")) if str(raw.get("taste") or "").strip() else None
        value = food.parse_food_score(raw.get("value")) if str(raw.get("value") or "").strip() else None
        if str(raw.get("taste") or "").strip() and taste is None:
            raise contracts.ContractError("invalid_score", "味道分需要在0.1-5之间")
        if str(raw.get("value") or "").strip() and value is None:
            raise contracts.ContractError("invalid_score", "性价比分需要在0.1-5之间")
        comment = food.sanitize_food_submit_text(
            raw.get("comment"), max_chars=comment_max, forbid_url=False
        )
        if str(raw.get("comment") or "").strip() and not comment:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "评价内容不合法")
        tags = food.split_food_tags(
            raw.get("tags") or raw.get("tag"), max_count=tag_count, max_chars=tag_max
        )
        tag_ids: list[int] = []
        raw_tag_ids = raw.get("tag_ids")
        if raw_tag_ids not in (None, "", []):
            for item in (raw_tag_ids if isinstance(raw_tag_ids, (list, tuple)) else str(raw_tag_ids).split(",")):
                ident = _as_int(item, -1)
                if ident < 0:
                    raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "tag_id 非法")
                if ident not in tag_ids:
                    tag_ids.append(ident)
        if not location_id and not location_text:
            pref = self._preference(ctx).get("location") or {}
            if isinstance(pref, Mapping) and _as_int(pref.get("location_id"), -1) >= 0:
                location_id = _as_int(pref.get("location_id"), -1)
                location_text = _safe(pref.get("name"), 100)
        payload = {
            "restaurant": restaurant,
            "location_id": location_id,
            "location_text": location_text,
            "taste": taste,
            "value": value,
            "comment": comment,
            "tags": tags,
            "tag_ids": tag_ids,
        }
        missing: list[str] = []
        if not restaurant:
            missing.append("restaurant")
        if location_id is None and not location_text:
            missing.append("location")
        if taste is None:
            missing.append("taste")
        if value is None:
            missing.append("value")
        if not comment:
            missing.append("comment")
        return {
            "payload": payload,
            "payload_hash": contracts.payload_hash(payload),
            "missing_fields": missing,
            "status": "collecting" if missing else "draft",
        }

    @staticmethod
    def _draft_data(row: Mapping[str, Any], *, include_payload: bool = True) -> dict[str, Any]:
        payload = row.get("payload_json") if isinstance(row.get("payload_json"), Mapping) else {}
        data: dict[str, Any] = {
            "draft_id": str(row.get("draft_id", "")),
            "status": str(row.get("status", "")),
            "revision": int(row.get("revision", 0) or 0),
            "payload_hash": str(row.get("payload_hash", "")),
            "missing_fields": list(row.get("missing_json", []) or []),
            "created_at": float(row.get("created_at", 0) or 0),
            "updated_at": float(row.get("updated_at", 0) or 0),
            "expires_at": float(row.get("expires_at", 0) or 0),
            "confirmation_expires_at": float(row.get("confirmation_expires_at", 0) or 0),
        }
        if include_payload:
            data["normalized_payload"] = {
                key: payload.get(key)
                for key in (
                    "restaurant",
                    "location_id",
                    "location_text",
                    "taste",
                    "value",
                    "comment",
                    "tags",
                    "tag_ids",
                )
            }
        return data

    async def food_draft_create(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_draft_create, request_context, args)

    async def _food_draft_create(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        normalized = self._normalize_draft_payload(ctx, args)
        ttl = max(
            1,
            self._canonical_int("drafts", "ttl_seconds", "drafts_ttl_seconds", "food_submit_draft_ttl_seconds", 600),
        )
        draft_id = _safe(args.get("draft_id"), 100) or uuid.uuid4().hex
        create_key_raw = args.get("idempotency_key") or args.get("create_key")
        create_key = (
            self._idem_key(ctx, "draft_create", str(create_key_raw))
            if str(create_key_raw or "").strip()
            else ""
        )
        result = self.ledger.create_draft(
            draft_id=draft_id,
            actor_id=ctx.actor_id,
            instance_id=ctx.instance_id,
            platform=ctx.platform,
            bot_id=ctx.bot_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
            payload=normalized["payload"],
            payload_hash_value=normalized["payload_hash"],
            revision=1,
            status=normalized["status"],
            missing_fields=normalized["missing_fields"],
            ttl_seconds=ttl,
            create_key=create_key,
            update_draft_id=_safe(args.get("draft_id"), 100) or "",
        )
        kind = str(result.get("kind", ""))
        if kind in ("created", "updated", "replay"):
            data = self._draft_data(result.get("draft") or {})
            data["replayed"] = kind == "replay"
            return contracts.ProviderResult.ok(data)
        if kind == "conflict":
            return contracts.ProviderResult.error(
                contracts.ERR_IDEMPOTENCY_CONFLICT,
                "同一幂等键对应不同草稿内容",
                data={"http_status": 409, **self._draft_data(result.get("draft") or {}, include_payload=False)},
            )
        if kind == "forbidden":
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权修改该草稿")
        if kind == "missing":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_NOT_FOUND, "草稿不存在")
        if kind == "invalid_state":
            return contracts.ProviderResult.error(
                contracts.ERR_INVALID_STATE,
                "当前草稿状态不允许修改",
                data=self._draft_data(result.get("draft") or {}, include_payload=False),
            )
        return contracts.ProviderResult.error(contracts.ERR_INTERNAL, "草稿创建失败")

    async def food_draft_preview(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_draft_preview, request_context, args)

    async def _food_draft_preview(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        draft_id = _safe(args.get("draft_id"), 100)
        if not draft_id:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少 draft_id")
        draft = self.ledger.get_draft(draft_id)
        if draft is None:
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_NOT_FOUND, "草稿不存在")
        if not self.ledger._auth_matches(
            draft,
            actor_id=ctx.actor_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
        ):
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权查看该草稿")
        ttl = max(
            1,
            self._canonical_int(
                "drafts",
                "confirmation_ttl_seconds",
                "drafts_confirmation_ttl_seconds",
                "",
                300,
            ),
        )
        result = self.ledger.preview_draft(
            draft_id,
            actor_id=ctx.actor_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
            confirmation_ttl_seconds=int(ttl),
            now=self.plugin._now(),
        )
        kind = str(result.get("kind", ""))
        row = result.get("draft") or {}
        if kind == "missing":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_NOT_FOUND, "草稿不存在")
        if kind == "forbidden":
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权查看该草稿")
        if kind == "expired":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_EXPIRED, "草稿已过期")
        if kind == "incomplete":
            return contracts.ProviderResult.error(
                contracts.ERR_DRAFT_INCOMPLETE,
                "草稿字段尚未补全",
                data=self._draft_data(row),
            )
        if kind != "ready":
            return contracts.ProviderResult.error(
                contracts.ERR_INVALID_STATE,
                "当前草稿状态不允许预览确认",
                data=self._draft_data(row, include_payload=False),
            )
        token, expires = self.issuer.issue(
            ctx,
            kind="food_draft_confirm",
            draft_id=draft_id,
            revision=int(row.get("revision", 0) or 0),
            payload_hash_value=str(row.get("payload_hash", "")),
            now=self.plugin._now(),
        )
        self.ledger.store_confirmation_token(
            draft_id,
            actor_id=ctx.actor_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
            token=token,
            expires_at=expires,
            now=self.plugin._now(),
        )
        data = self._draft_data(self.ledger.get_draft(draft_id) or row)
        data.update(
            {
                "confirmation_ref": token,
                "confirmation_expires_at": expires,
                "confirmation_required": True,
            }
        )
        return contracts.ProviderResult.ok(data)

    # ---- confirmation / operation status ---------------------------
    def _operation_data(self, operation: Mapping[str, Any], *, replay: bool = False) -> dict[str, Any]:
        data = {
            "op_id": str(operation.get("op_id", "")),
            "draft_id": str(operation.get("draft_id", "")),
            "state": str(operation.get("status", "")),
            "status": str(operation.get("status", "")),
            "side_effect_state": str(operation.get("side_effect_state", "none")),
            "retryable": bool(operation.get("retryable", False)),
            "remote_code": str(operation.get("remote_code", "")),
            "remote_data": operation.get("remote_data_json")
            if isinstance(operation.get("remote_data_json"), Mapping)
            else {},
            "error_code": str(operation.get("error_code", "")),
            "message": str(operation.get("message", "")),
            "payload_hash": str(operation.get("payload_hash", "")),
            "created_at": float(operation.get("created_at", 0) or 0),
            "updated_at": float(operation.get("updated_at", 0) or 0),
            "replayed": bool(replay),
        }
        return data

    def _op_result(
        self, operation: Mapping[str, Any], *, replay: bool = False
    ) -> contracts.ProviderResult:
        state = str(operation.get("status", ""))
        data = self._operation_data(operation, replay=replay)
        op_id = data["op_id"]
        if state == "succeeded":
            return contracts.ProviderResult.ok(
                data,
                provider_operation_id=op_id,
                side_effect_state=str(
                    operation.get("side_effect_state", "committed")
                ),
            )
        if state in ("outcome_unknown", "submitting", "confirmed"):
            return contracts.ProviderResult.error(
                contracts.ERR_OUTCOME_UNKNOWN
                if state == "outcome_unknown"
                else contracts.ERR_INVALID_STATE,
                "提交结果未知或仍在进行，禁止自动重发",
                retryable=False,
                side_effect_state="unknown",
                data=data,
                provider_operation_id=op_id,
            )
        return contracts.ProviderResult.error(
            str(operation.get("error_code", "") or contracts.ERR_INVALID_STATE),
            str(operation.get("message", "") or "提交被拒绝"),
            retryable=bool(operation.get("retryable", False)),
            side_effect_state=str(operation.get("side_effect_state", "none")),
            data=data,
            provider_operation_id=op_id,
        )

    def _build_submit_payload(self, ctx: contracts.RequestContext, payload: Mapping[str, Any]) -> dict[str, Any]:
        backend_id = _safe(
            self.plugin._canonical_value(
                "submit",
                "backend_id",
                flat_key="submit_backend_id",
                legacy_key="",
                default="food.dlsz.top",
            ),
            100,
        )
        if backend_id not in ("", "food.dlsz.top"):
            raise contracts.ContractError(
                contracts.ERR_DEPENDENCY_MISSING, "未登记或不安全的 Food 后端"
            )
        username_limit = max(
            1,
            self._canonical_int("submit", "username_max_chars", "submit_username_max_chars", "food_submit_username_max_chars", 32),
        )
        username = food.sanitize_food_submit_text(ctx.actor_name, max_chars=username_limit)
        if not username:
            username = food.sanitize_food_submit_text(
                self.plugin._cfg_str("food_submit_username_fallback"),
                max_chars=username_limit,
            ) or "群友"
        fields = {
            "restaurant": payload.get("restaurant"),
            "comment": payload.get("comment"),
            "taste": payload.get("taste"),
            "value": payload.get("value"),
            "tags": payload.get("tags") if isinstance(payload.get("tags"), list) else [],
        }
        location_id = payload.get("location_id")
        location_text = _safe(payload.get("location_text"), 100)
        built = food.build_food_submit_payload(
            fields,
            username=username,
            password=ctx.actor_id,
            location_id=location_id if location_id not in (None, "") else None,
            new_location_name="" if location_id not in (None, "") else location_text,
            new_location_parent_id=None,
        )
        return built

    async def _submit_claimed(
        self,
        ctx: contracts.RequestContext,
        draft: Mapping[str, Any],
        operation: Mapping[str, Any],
    ) -> contracts.ProviderResult:
        op_id = str(operation.get("op_id", ""))
        draft_id = str(draft.get("draft_id", ""))
        payload = draft.get("payload_json") if isinstance(draft.get("payload_json"), Mapping) else {}
        try:
            request_payload = self._build_submit_payload(ctx, payload)
        except Exception:
            request_payload = {}
        if not request_payload:
            result = self.ledger.finish_operation(
                op_id,
                status="rejected",
                draft_status="rejected",
                side_effect_state="none",
                error_code="invalid_args",
                message="提交参数不完整",
                retryable=True,
                now=self.plugin._now(),
            )
            return self._op_result(result or {"status": "rejected"})
        try:
            outcome: BackendOutcome = await self.backend.submit(request_payload)
        except Exception:
            self.ledger.finish_operation(
                op_id,
                status="outcome_unknown",
                draft_status="outcome_unknown",
                side_effect_state="unknown",
                error_code=contracts.ERR_OUTCOME_UNKNOWN,
                message="提交结果未知，请勿自动重试",
                retryable=False,
                now=self.plugin._now(),
            )
            return contracts.ProviderResult.error(
                contracts.ERR_OUTCOME_UNKNOWN,
                "提交结果未知，请勿自动重试",
                side_effect_state="unknown",
                data={"op_id": op_id, "draft_id": draft_id, "state": "outcome_unknown"},
                provider_operation_id=op_id,
            )
        remote_data = dict(outcome.remote_data or {})
        if outcome.state == "succeeded":
            if outcome.already_submitted:
                # Evidence must explicitly identify a duplicate username and
                # the operation payload hash must already match (CAS above).
                evidence_ok = (
                    outcome.remote_code == "DUPLICATE_USERNAME"
                    and bool(outcome.evidence.get("duplicate_username"))
                )
                remote_data["already_submitted"] = bool(evidence_ok)
                remote_data["evidence"] = dict(outcome.evidence)
            result = self.ledger.finish_operation(
                op_id,
                status="succeeded",
                draft_status="succeeded",
                side_effect_state="committed",
                remote_code=outcome.remote_code,
                remote_data=remote_data,
                message=outcome.message,
                retryable=False,
                now=self.plugin._now(),
            )
            return self._op_result(result or {"status": "succeeded", "op_id": op_id})
        if outcome.state == "rejected":
            result = self.ledger.finish_operation(
                op_id,
                status="rejected",
                draft_status="rejected",
                side_effect_state="none",
                remote_code=outcome.remote_code,
                remote_data=remote_data,
                error_code=outcome.error_code,
                message=outcome.message,
                retryable=outcome.retryable,
                now=self.plugin._now(),
            )
            return self._op_result(result or {"status": "rejected", "op_id": op_id})
        # Unknown/ambiguous outcomes are terminal locally and never resent.
        result = self.ledger.finish_operation(
            op_id,
            status="outcome_unknown",
            draft_status="outcome_unknown",
            side_effect_state="unknown",
            remote_code=outcome.remote_code,
            remote_data=remote_data,
            error_code=contracts.ERR_OUTCOME_UNKNOWN,
            message=outcome.message or "提交结果未知，请勿自动重试",
            retryable=False,
            now=self.plugin._now(),
        )
        return self._op_result(
            result or {"status": "outcome_unknown", "op_id": op_id}
        )

    async def food_confirm(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_confirm, request_context, args)

    async def _food_confirm(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        draft_id = _safe(args.get("draft_id"), 100)
        if not draft_id:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少 draft_id")
        draft = self.ledger.get_draft(draft_id)
        if draft is None:
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_NOT_FOUND, "草稿不存在")
        if not self.ledger._auth_matches(
            draft,
            actor_id=ctx.actor_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
        ):
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权确认该草稿")
        status = str(draft.get("status", ""))
        if status == "expired":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_EXPIRED, "草稿已过期")
        if status in ("succeeded", "rejected", "outcome_unknown"):
            op_id = str(draft.get("last_operation_id", "") or "")
            operation = self.ledger.get_operation(op_id) if op_id else None
            if operation is not None:
                return self._op_result(operation, replay=True)
            if status == "succeeded":
                return contracts.ProviderResult.ok(
                    {"draft_id": draft_id, "state": "succeeded", "replayed": True}
                )
            return contracts.ProviderResult.error(
                contracts.ERR_OUTCOME_UNKNOWN if status == "outcome_unknown" else contracts.ERR_INVALID_STATE,
                "草稿当前状态不可重新提交",
                side_effect_state="unknown" if status == "outcome_unknown" else "none",
                data={"draft_id": draft_id, "state": status},
            )
        if status in ("confirmed", "submitting"):
            op_id = str(draft.get("last_operation_id", "") or "")
            operation = self.ledger.get_operation(op_id) if op_id else None
            return contracts.ProviderResult.error(
                contracts.ERR_OUTCOME_UNKNOWN if status == "submitting" else contracts.ERR_INVALID_STATE,
                "提交正在进行或结果未知，禁止再次提交",
                retryable=False,
                side_effect_state="unknown" if status == "submitting" else "none",
                data=self._operation_data(operation or {"draft_id": draft_id}, replay=True),
                provider_operation_id=op_id,
            )
        confirmation = str(args.get("confirmation_ref") or args.get("token") or "")
        if not confirmation:
            confirmation = str(draft.get("confirmation_token", "") or "")
        if status in ("collecting", "draft"):
            if confirmation:
                validation = self.issuer.verify(
                    confirmation,
                    ctx,
                    kind="food_draft_confirm",
                    draft_id=draft_id,
                    revision=int(draft.get("revision", 0) or 0),
                    payload_hash_value=str(draft.get("payload_hash", "")),
                    now=self.plugin._now(),
                )
                if not validation.valid:
                    return contracts.ProviderResult.error(
                        validation.code, "确认凭证已失效，请重新预览"
                    )
            return contracts.ProviderResult.error(
                contracts.ERR_CONFIRMATION_REQUIRED,
                "请先预览草稿并取得确认凭证",
                data=self._draft_data(draft, include_payload=False),
            )
        if status != "awaiting_confirmation":
            return contracts.ProviderResult.error(
                contracts.ERR_INVALID_STATE,
                "当前草稿状态不允许确认",
                data=self._draft_data(draft, include_payload=False),
            )
        if not confirmation:
            return contracts.ProviderResult.error(
                contracts.ERR_CONFIRMATION_REQUIRED, "缺少确认凭证"
            )
        stored_token = str(draft.get("confirmation_token", "") or "")
        if stored_token and confirmation != stored_token:
            return contracts.ProviderResult.error(
                contracts.ERR_CONFIRMATION_INVALID, "确认凭证不是当前草稿的最新凭证"
            )
        validation = self.issuer.verify(
            confirmation,
            ctx,
            kind="food_draft_confirm",
            draft_id=draft_id,
            revision=int(draft.get("revision", 0) or 0),
            payload_hash_value=str(draft.get("payload_hash", "")),
            now=self.plugin._now(),
        )
        if not validation.valid:
            return contracts.ProviderResult.error(
                validation.code, "确认凭证无效、已过期或内容已变更"
            )
        idempotency_raw = str(
            args.get("idempotency_key")
            or args.get("op_key")
            or f"{draft_id}:{int(draft.get('revision', 0) or 0)}"
        )
        idem_key = self._idem_key(ctx, "food_confirm", idempotency_raw)
        op_id = _safe(args.get("operation_id"), 100) or uuid.uuid4().hex
        begin = self.ledger.begin_submit(
            draft_id,
            actor_id=ctx.actor_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
            expected_revision=int(draft.get("revision", 0) or 0),
            expected_payload_hash=str(draft.get("payload_hash", "")),
            idempotency_key=idem_key,
            payload_hash_value=str(draft.get("payload_hash", "")),
            op_id=op_id,
            now=self.plugin._now(),
        )
        kind = str(begin.get("kind", ""))
        if kind == "claimed":
            return await self._submit_claimed(
                ctx, begin.get("draft") or draft, begin.get("operation") or {}
            )
        if kind == "replay":
            return self._op_result(begin.get("operation") or {}, replay=True)
        if kind == "conflict":
            return contracts.ProviderResult.error(
                contracts.ERR_IDEMPOTENCY_CONFLICT,
                "同一幂等键对应不同提交内容",
                data={"http_status": 409},
            )
        if kind in ("in_progress", "terminal"):
            operation = begin.get("operation") or {}
            if operation:
                return self._op_result(operation, replay=True)
            return contracts.ProviderResult.error(
                contracts.ERR_INVALID_STATE,
                "草稿正在提交或已终结",
                side_effect_state="unknown",
                data=self._draft_data(begin.get("draft") or draft, include_payload=False),
            )
        if kind == "confirmation_expired":
            return contracts.ProviderResult.error(
                contracts.ERR_CONFIRMATION_EXPIRED, "确认已过期，请重新预览"
            )
        if kind == "revision_conflict":
            return contracts.ProviderResult.error(
                contracts.ERR_REVISION_CONFLICT,
                "草稿内容已变更，旧确认失效",
                data={"http_status": 409},
            )
        if kind == "expired":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_EXPIRED, "草稿已过期")
        if kind == "forbidden":
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权确认该草稿")
        if kind == "missing":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_NOT_FOUND, "草稿不存在")
        return contracts.ProviderResult.error(contracts.ERR_INVALID_STATE, "草稿状态不允许确认")

    async def food_cancel(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_cancel, request_context, args)

    async def _food_cancel(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        draft_id = _safe(args.get("draft_id"), 100)
        if not draft_id:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少 draft_id")
        result = self.ledger.cancel_draft(
            draft_id,
            actor_id=ctx.actor_id,
            scope_key=self._scope(ctx),
            persona_id=ctx.persona_id,
            reason=_safe(args.get("reason"), 100),
            now=self.plugin._now(),
        )
        kind = str(result.get("kind", ""))
        if kind == "cancelled":
            return contracts.ProviderResult.ok(self._draft_data(result.get("draft") or {}))
        if kind == "missing":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_NOT_FOUND, "草稿不存在")
        if kind == "forbidden":
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权取消该草稿")
        if kind == "expired":
            return contracts.ProviderResult.error(contracts.ERR_DRAFT_EXPIRED, "草稿已过期")
        if kind == "in_progress":
            return contracts.ProviderResult.error(
                contracts.ERR_INVALID_STATE, "草稿正在提交，无法取消", side_effect_state="unknown"
            )
        return contracts.ProviderResult.error(
            contracts.ERR_INVALID_STATE,
            "当前草稿状态不允许取消",
            data=self._draft_data(result.get("draft") or {}, include_payload=False),
        )

    async def food_operation_status(self, request_context: Any, args: Any) -> dict[str, Any]:
        return await self._dispatch(self._food_operation_status, request_context, args)

    async def _food_operation_status(
        self, ctx: contracts.RequestContext, args: dict[str, Any]
    ) -> contracts.ProviderResult:
        op_id = _safe(args.get("op_id") or args.get("operation_id"), 100)
        if not op_id:
            raise contracts.ContractError(contracts.ERR_INVALID_ARGS, "缺少 op_id")
        operation = self.ledger.get_operation(op_id)
        if operation is None:
            return contracts.ProviderResult.error(contracts.ERR_NOT_FOUND, "操作不存在")
        if (
            str(operation.get("actor_id", "")) != ctx.actor_id
            or str(operation.get("scope_key", "")) != self._scope(ctx)
            or str(operation.get("persona_id", "")) != ctx.persona_id
        ):
            return contracts.ProviderResult.error(contracts.ERR_PERMISSION_DENIED, "无权查看该操作")
        return contracts.ProviderResult.ok(self._operation_data(operation))
