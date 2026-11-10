"""qqbot_scheduler: pure-local scheduled group messages for QQ Bot Studio."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from . import scheduler

PLUGIN_NAME = "qqbot_scheduler"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _cfg(config: Any, key: str, default: Any) -> Any:
    if isinstance(config, dict):
        return config.get(key, default)
    try:
        return config.get(key, default)
    except Exception:
        pass
    try:
        return getattr(config, key, default)
    except Exception:
        return default


def _event_message(event: AstrMessageEvent) -> str:
    return _text(getattr(event, "message_str", ""))


def _event_umo(event: AstrMessageEvent) -> str:
    return _text(getattr(event, "unified_msg_origin", ""))


def _strip_command(text: str) -> str:
    raw = _text(text)
    raw = re.sub(r"^(?:/|／)?\s*(?:定时|schedule)\b\s*", "", raw, flags=re.I)
    return raw.strip()


class SchedulerPlugin(Star):
    def __init__(
        self,
        context: Context,
        config: Any = None,
        *,
        data_dir: str | Path | None = None,
    ):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._timezone = _text(_cfg(self.config, "timezone", DEFAULT_TIMEZONE)) or DEFAULT_TIMEZONE
        self._poll_seconds = max(5, int(_cfg(self.config, "poll_seconds", 20) or 20))
        self._max_schedules = max(1, int(_cfg(self.config, "max_schedules_per_group", 20) or 20))
        self._max_chars = max(1, int(_cfg(self.config, "max_message_chars", 300) or 300))
        self._holiday_enabled = bool(_cfg(self.config, "holiday_api_enabled", True))
        self._data_dir = self._resolve_data_dir(data_dir)
        self._store = scheduler.ScheduleStore(self._data_dir / "schedules.json")
        self._holidays = scheduler.HolidayResolver(enabled=self._holiday_enabled)
        self._task: asyncio.Task | None = None

    def _resolve_data_dir(self, override: str | Path | None) -> Path:
        if override is not None:
            return Path(override)
        try:
            path = StarTools.get_data_dir(plugin_name=PLUGIN_NAME)
            if path:
                return Path(path)
        except Exception:
            pass
        return Path.home() / ".qqbot_scheduler"

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._timezone)
        except Exception:
            return ZoneInfo(DEFAULT_TIMEZONE)

    async def initialize(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="qqbot_scheduler")

    async def terminate(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_seconds)
            try:
                now = datetime.now(self._zone())
                due = scheduler.due_schedules(now, self._store.list(), self._holidays)
                for item in due:
                    try:
                        sent = await self.context.send_message(
                            item.umo, MessageChain().message(item.message)
                        )
                    except Exception as exc:
                        sent = False
                        logger.warning("[qqbot_scheduler] send failed id=%s: %s", item.id, exc)
                    if sent is False:
                        logger.warning("[qqbot_scheduler] platform did not route id=%s umo=%s", item.id, item.umo)
                    self._store.mark_fired(item.id, now.date())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[qqbot_scheduler] loop error: %s", exc)

    def _format_rule(self, rule: scheduler.DateRule) -> str:
        if rule.kind == "daily":
            return "每天"
        if rule.kind == "range":
            return f"{rule.start}~{rule.end}"
        if rule.kind == "weekdays":
            names = "一二三四五六日"
            return "周" + "、周".join(names[d - 1] for d in rule.weekdays)
        return "工作日" if rule.kind == "workdays" else "非工作日"

    @filter.command("定时", alias={"schedule"}, priority=110)
    async def schedule_command(self, event: AstrMessageEvent):
        if not bool(_cfg(self.config, "enabled", True)):
            return
        raw = _strip_command(_event_message(event))
        umo = _event_umo(event)
        if not umo:
            yield event.plain_result("无法确定当前会话，不能创建定时消息。")
            event.stop_event()
            return
        if not raw or raw in ("帮助", "help", "用法"):
            yield event.plain_result(
                "用法：\n"
                "/定时 HH:MM 消息\n"
                "/定时 HH:MM 消息 YYYY-MM-DD~YYYY-MM-DD\n"
                "/定时 HH:MM 消息 周一、周三、周五\n"
                "/定时 HH:MM 消息 工作日\n"
                "/定时 列表\n"
                "/定时 删除 <ID>"
            )
            event.stop_event()
            return
        if raw in ("列表", "list"):
            rows = [item for item in self._store.list() if item.umo == umo]
            if not rows:
                text = "当前会话没有定时任务。"
            else:
                text = "\n".join(
                    f"{item.id}  {item.at}  [{self._format_rule(item.rule)}]  {item.message}"
                    for item in rows
                )
            yield event.plain_result(text)
            event.stop_event()
            return
        delete_match = re.fullmatch(r"(?:删除|delete)\s+(\S+)", raw, flags=re.I)
        if delete_match:
            schedule_id = delete_match.group(1)
            target = next((item for item in self._store.list() if item.id == schedule_id), None)
            if target is None or target.umo != umo:
                yield event.plain_result("没有找到这个定时任务。")
            else:
                self._store.delete(schedule_id)
                yield event.plain_result(f"已删除定时任务 {schedule_id}。")
            event.stop_event()
            return
        try:
            at, message, rule = scheduler.parse_command(
                raw, today=datetime.now(self._zone()).date(), max_chars=self._max_chars
            )
        except scheduler.ScheduleError as exc:
            yield event.plain_result(f"定时设置失败：{exc}")
            event.stop_event()
            return
        current = [item for item in self._store.list() if item.umo == umo]
        if len(current) >= self._max_schedules:
            yield event.plain_result(f"当前会话最多只能有 {self._max_schedules} 个定时任务。")
            event.stop_event()
            return
        item = scheduler.Schedule(
            id=uuid.uuid4().hex[:8],
            umo=umo,
            at=at,
            message=message,
            rule=rule,
        )
        self._store.add(item)
        yield event.plain_result(
            f"已创建定时任务 {item.id}：{at} · {self._format_rule(rule)} · {message}"
        )
        event.stop_event()
