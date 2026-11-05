"""Standalone read-only Food search command."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import food


def _fetch(keyword: str, limit: int = 3) -> list[dict]:
    url = food.build_food_url(food.FOOD_RESTAURANTS_PATH, {"q": keyword, "limit": max(1, min(limit, 5))})
    req = urllib.request.Request(url, headers={"User-Agent": "QQBotStudio-Food/2.0"})
    with urllib.request.urlopen(req, timeout=12) as response:
        final = response.geturl()
        if not food.validate_food_final_url(final):
            raise food.FoodAPIError("invalid food URL")
        return food.extract_food_items(response.read(512 * 1024).decode("utf-8", "replace"))


class FoodPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("美食搜索", alias={"找吃的"}, priority=110)
    async def search(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        message = str(getattr(event, "message_str", "") or "")
        keyword = message.removeprefix("/美食搜索").removeprefix("/找吃的").strip()
        if not keyword:
            yield event.plain_result("用法：/美食搜索 店名或关键词")
            event.stop_event()
            return
        try:
            items = _fetch(keyword[:50], 3)
            if not items:
                yield event.plain_result("没有找到餐厅。")
            else:
                lines = ["Food 搜索结果："]
                for item in items[:3]:
                    name = food._safe_display(item.get("name") or "未命名", limit=40)
                    total = food._format_number(item.get("total_score") or item.get("score"))
                    lines.append(f"- {name} 评分 {total}")
                yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"Food 搜索失败：{type(exc).__name__}")
        event.stop_event()
