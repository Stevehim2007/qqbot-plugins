"""Pure scheduling core for qqbot_scheduler.

No AstrBot imports here so parsing, persistence and due calculation can be
tested without a running bot or network.  Timezone-aware ``now`` values are
required from the caller.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"
MAX_MESSAGE_CHARS = 300
DATE_RANGE_RE = re.compile(
    r"^(?:(?P<sy>\d{4})-)?(?P<sm>\d{1,2})-(?P<sd>\d{1,2})~"
    r"(?:(?P<ey>\d{4})-)?(?P<em>\d{1,2})-(?P<ed>\d{1,2})$"
)
WEEKDAY_TOKEN_RE = re.compile(r"^(?:周|星期|礼拜|每周|毎周)?([一二三四五六日天1-7])$")
WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
               "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}


class ScheduleError(ValueError):
    """Raised for invalid user schedule syntax."""


def _parse_time(value: str) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", raw)
    if not match:
        match = re.fullmatch(r"(\d{1,2})\s*点\s*(\d{1,2})?", raw)
    if not match:
        raise ScheduleError("时间点必须写成 HH:MM，例如 08:00。")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleError("时间点超出范围。")
    return f"{hour:02d}:{minute:02d}"


def parse_weekday_token(token: str) -> tuple[int, ...] | None:
    token = str(token or "").strip()
    if not token:
        return None
    parts = [part.strip() for part in re.split(r"[、,，]", token) if part.strip()]
    if not parts:
        return None
    days: list[int] = []
    for part in parts:
        if "~" in part:
            bits = [bit.strip() for bit in part.split("~") if bit.strip()]
            if len(bits) != 2:
                return None
            a = parse_weekday_token(bits[0])
            b = parse_weekday_token(bits[1])
            if not a or not b or len(a) != 1 or len(b) != 1:
                return None
            start, end = a[0], b[0]
            if start <= end:
                days.extend(range(start, end + 1))
            else:
                days.extend(list(range(start, 8)) + list(range(1, end + 1)))
            continue
        match = WEEKDAY_TOKEN_RE.match(part)
        if not match:
            return None
        days.append(WEEKDAY_MAP[match.group(1)])
    return tuple(sorted(set(days)))


@dataclass(frozen=True)
class DateRule:
    kind: str = "daily"  # daily | range | weekdays | workdays | nonworkdays
    start: date | None = None
    end: date | None = None
    weekdays: tuple[int, ...] = ()

    def matches(self, day: date, resolver: "HolidayResolver | None" = None) -> bool:
        if self.kind == "daily":
            return True
        if self.kind == "range":
            return bool(self.start and self.end and self.start <= day <= self.end)
        if self.kind == "weekdays":
            return day.isoweekday() in self.weekdays
        if self.kind in ("workdays", "nonworkdays"):
            workday = resolver.is_workday(day) if resolver else day.weekday() < 5
            return workday if self.kind == "workdays" else not workday
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "start": self.start.isoformat() if self.start else "",
            "end": self.end.isoformat() if self.end else "",
            "weekdays": list(self.weekdays),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DateRule":
        if not isinstance(raw, dict):
            return cls()
        kind = str(raw.get("kind") or "daily")
        if kind not in ("daily", "range", "weekdays", "workdays", "nonworkdays"):
            kind = "daily"
        start = _date_or_none(raw.get("start"))
        end = _date_or_none(raw.get("end"))
        weekdays = tuple(int(x) for x in (raw.get("weekdays") or []) if str(x).isdigit())
        return cls(kind=kind, start=start, end=end, weekdays=weekdays)


def _date_or_none(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def parse_date_rule(token: str, *, today: date | None = None) -> DateRule | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    today = today or date.today()
    if raw in ("每天", "每日", "daily"):
        return DateRule()
    if raw in ("工作日", "非法定节假日"):
        return DateRule(kind="workdays")
    if raw in ("非工作日", "节假日", "休息日"):
        return DateRule(kind="nonworkdays")
    match = DATE_RANGE_RE.fullmatch(raw)
    if match:
        sy = int(match.group("sy") or today.year)
        ey = int(match.group("ey") or sy)
        try:
            start = date(sy, int(match.group("sm")), int(match.group("sd")))
            end = date(ey, int(match.group("em")), int(match.group("ed")))
        except ValueError:
            raise ScheduleError("日期段不是有效日期。")
        if end < start:
            raise ScheduleError("日期段结束日期早于开始日期。")
        return DateRule(kind="range", start=start, end=end)
    weekdays = parse_weekday_token(raw)
    if weekdays:
        return DateRule(kind="weekdays", weekdays=weekdays)
    return None


def parse_command(text: str, *, today: date | None = None, max_chars: int = MAX_MESSAGE_CHARS) -> tuple[str, str, DateRule]:
    raw = str(text or "").strip()
    raw = re.sub(r"^(?:/|／)?\s*(?:定时|schedule)\b\s*", "", raw, flags=re.I).strip()
    if not raw:
        raise ScheduleError("用法：/定时 HH:MM 消息 [日期段]")
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        raise ScheduleError("用法：/定时 HH:MM 消息 [日期段]")
    at = _parse_time(parts[0])
    rest = parts[1].strip()
    rule = DateRule()
    message = rest
    if " " in rest:
        head, tail = rest.rsplit(None, 1)
        parsed = parse_date_rule(tail, today=today)
        if parsed is not None:
            rule = parsed
            message = head.strip()
    if not message:
        raise ScheduleError("定时消息不能为空。")
    if len(message) > max_chars:
        raise ScheduleError(f"定时消息不能超过 {max_chars} 个字符。")
    return at, message, rule


@dataclass
class Schedule:
    id: str
    umo: str
    at: str
    message: str
    rule: DateRule = field(default_factory=DateRule)
    last_fired: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "umo": self.umo,
            "at": self.at,
            "message": self.message,
            "rule": self.rule.to_dict(),
            "last_fired": self.last_fired,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Schedule | None":
        if not isinstance(raw, dict):
            return None
        sid = str(raw.get("id") or "").strip()
        umo = str(raw.get("umo") or "").strip()
        at = str(raw.get("at") or "").strip()
        message = str(raw.get("message") or "").strip()
        if not sid or not umo or not at or not message:
            return None
        return cls(sid, umo, at, message, DateRule.from_dict(raw.get("rule")), str(raw.get("last_fired") or ""))


class ScheduleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._schedules: dict[str, Schedule] = {}
        self.load()

    def load(self) -> None:
        self._schedules = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("schedules", []) if isinstance(raw, dict) else []:
            schedule = Schedule.from_dict(item)
            if schedule:
                self._schedules[schedule.id] = schedule

    def list(self) -> list[Schedule]:
        return sorted(self._schedules.values(), key=lambda item: (item.at, item.id))

    def add(self, schedule: Schedule) -> None:
        self._schedules[schedule.id] = schedule
        self.save()

    def delete(self, schedule_id: str) -> bool:
        existed = self._schedules.pop(schedule_id, None) is not None
        if existed:
            self.save()
        return existed

    def mark_fired(self, schedule_id: str, day: date) -> None:
        schedule = self._schedules.get(schedule_id)
        if schedule:
            schedule.last_fired = day.isoformat()
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "schedules": [item.to_dict() for item in self.list()]}
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


class HolidayResolver:
    """Best-effort Chinese workday lookup with weekday fallback."""

    def __init__(
        self,
        enabled: bool = True,
        *,
        fetcher: Callable[[date], dict[str, Any]] | None = None,
        timeout: float = 5,
    ):
        self.enabled = enabled
        self._fetcher = fetcher
        self._timeout = timeout
        self._cache: dict[str, bool] = {}

    def _fetch(self, day: date) -> dict[str, Any]:
        if self._fetcher:
            return self._fetcher(day)
        url = f"https://timor.tech/api/holiday/info/{day.isoformat()}"
        request = urllib.request.Request(url, headers={"User-Agent": "qqbot-scheduler/1.0"})
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            raw = response.read(65536)
        return json.loads(raw.decode("utf-8", "replace"))

    def is_workday(self, day: date) -> bool:
        if not self.enabled:
            return day.weekday() < 5
        key = day.isoformat()
        if key in self._cache:
            return self._cache[key]
        fallback = day.weekday() < 5
        try:
            data = self._fetch(day)
            type_data = data.get("type") if isinstance(data, dict) else None
            code = type_data.get("type") if isinstance(type_data, dict) else None
            if code in (0, 3):
                result = True
            elif code in (1, 2):
                result = False
            else:
                result = fallback
        except Exception:
            result = fallback
        self._cache[key] = result
        return result


def due_schedules(
    now: datetime,
    schedules: Iterable[Schedule],
    resolver: HolidayResolver | None = None,
) -> list[Schedule]:
    today = now.date()
    current = now.strftime("%H:%M")
    due = []
    for schedule in schedules:
        if schedule.at != current or schedule.last_fired == today.isoformat():
            continue
        if schedule.rule.matches(today, resolver):
            due.append(schedule)
    return due
