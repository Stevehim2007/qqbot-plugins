"""Shared imports and fakes for the qqbot_food test suite."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

from astrbot_stub import Context, FakeEvent, FakeResult, LOGGER, collect_async, install, run

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PLUGIN_DIR.parent
if str(PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGINS_DIR))

install()

from qqbot_food import api as food_api  # noqa: E402
from qqbot_food import food as food_engine  # noqa: E402
from qqbot_food import state as food_state  # noqa: E402
from qqbot_food.config import DEFAULTS, PluginConfig  # noqa: E402
from qqbot_food.main import FoodPlugin  # noqa: E402

__all__ = [
    "Context",
    "DEFAULTS",
    "FakeEvent",
    "FakeFoodAPI",
    "FakeResult",
    "FoodPlugin",
    "LOGGER",
    "PLUGIN_DIR",
    "PluginConfig",
    "collect_async",
    "copy",
    "food_api",
    "food_engine",
    "food_state",
    "install",
    "make_plugin",
    "run",
]


class FakeFoodAPI:
    """Deterministic fake of FoodAPIClient used by all plugin tests."""

    def __init__(
        self,
        *,
        locations: Any = None,
        tags: Any = None,
        restaurants: Any = None,
        post_results: Any = None,
        get_errors: dict[str, list[Exception]] | None = None,
        post_errors: list[Exception] | None = None,
    ):
        self.locations = list(locations or [])
        self.tags = list(tags or [])
        self.restaurants = list(restaurants or [])
        self.post_results = list(post_results or [])
        self.get_errors = {
            key: list(values) for key, values in (get_errors or {}).items()
        }
        self.post_errors = list(post_errors or [])
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_payloads: list[dict[str, Any]] = []
        self.post_calls = 0

    def _maybe_raise_get(self, path: str):
        errors = self.get_errors.get(path)
        if errors:
            raise errors.pop(0)

    def get_json(self, path: str, params: dict[str, Any]):
        self.get_calls.append((path, copy.deepcopy(params)))
        self._maybe_raise_get(path)
        if path == food_engine.FOOD_LOCATIONS_PATH:
            return copy.deepcopy(self.locations)
        if path in (
            food_engine.FOOD_TAGS_SEARCH_PATH,
            food_engine.FOOD_TAGS_PATH,
        ):
            query = str(params.get("q", "") or "")
            if not query:
                return copy.deepcopy(self.tags)
            lowered = query.casefold()
            return [
                copy.deepcopy(item)
                for item in self.tags
                if lowered in str(item.get("name", "")).casefold()
                or str(item.get("name", "")).casefold() in lowered
            ]
        if path == food_engine.FOOD_RESTAURANTS_PATH:
            return copy.deepcopy(self.restaurants)
        return []

    def post_submit(self, payload: dict[str, Any]):
        self.post_calls += 1
        self.post_payloads.append(copy.deepcopy(payload))
        if self.post_errors:
            raise self.post_errors.pop(0)
        if self.post_results:
            result = self.post_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return 200, {
            "success": True,
            "restaurant_id": 1,
            "restaurant_name": str(payload.get("name", "")),
            "review_id": 2,
            "location_id": 3,
            "location_path": "中国/吉林省/长春市",
            "tags": list(payload.get("tags", [])),
        }


def make_plugin(
    data_dir,
    *,
    config: dict[str, Any] | None = None,
    api: FakeFoodAPI | None = None,
    clock=None,
):
    merged = {
        "food_cooldown_seconds": 0,
        "food_search_cooldown_seconds": 0,
        "food_submit_cooldown_seconds": 0,
    }
    merged.update(config or {})
    return FoodPlugin(
        Context(),
        config=merged,
        data_dir=data_dir,
        api_client=api if api is not None else FakeFoodAPI(),
        clock=clock,
    )
