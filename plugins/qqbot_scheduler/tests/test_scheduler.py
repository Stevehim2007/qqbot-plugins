import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

try:
    from qqbot_scheduler import scheduler
except ImportError:  # plugin test runner uses the plugin directory as cwd
    import scheduler


class ParseTests(unittest.TestCase):
    def test_time_and_daily(self):
        at, message, rule = scheduler.parse_command("08:00 早安", today=date(2026, 11, 10))
        self.assertEqual(at, "08:00")
        self.assertEqual(message, "早安")
        self.assertEqual(rule.kind, "daily")

    def test_time_chinese_point(self):
        at, _, _ = scheduler.parse_command("8点30 吃早饭", today=date(2026, 11, 10))
        self.assertEqual(at, "08:30")

    def test_weekday_list_and_range(self):
        _, message, rule = scheduler.parse_command(
            "12:00 午饭 周一、周三、周五", today=date(2026, 11, 10)
        )
        self.assertEqual(message, "午饭")
        self.assertEqual(rule.kind, "weekdays")
        self.assertEqual(rule.weekdays, (1, 3, 5))
        _, _, rule = scheduler.parse_command(
            "12:00 午休 周一~周日", today=date(2026, 11, 10)
        )
        self.assertEqual(rule.weekdays, (1, 2, 3, 4, 5, 6, 7))

    def test_arabic_weekday(self):
        _, _, rule = scheduler.parse_command(
            "09:00 打卡 周1、周4", today=date(2026, 11, 10)
        )
        self.assertEqual(rule.weekdays, (1, 4))

    def test_date_range(self):
        _, message, rule = scheduler.parse_command(
            "09:00 活动 2026-11-11~2026-11-20", today=date(2026, 11, 10)
        )
        self.assertEqual(message, "活动")
        self.assertEqual(rule.kind, "range")
        self.assertEqual(rule.start, date(2026, 11, 11))
        self.assertEqual(rule.end, date(2026, 11, 20))

    def test_workday_keywords(self):
        _, _, rule = scheduler.parse_command(
            "09:00 上班 工作日", today=date(2026, 11, 10)
        )
        self.assertEqual(rule.kind, "workdays")
        _, _, rule = scheduler.parse_command(
            "09:00 休息 非工作日", today=date(2026, 11, 10)
        )
        self.assertEqual(rule.kind, "nonworkdays")

    def test_invalid(self):
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.parse_command("25:00 x", today=date(2026, 11, 10))
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.parse_command("08:00", today=date(2026, 11, 10))
        with self.assertRaises(scheduler.ScheduleError):
            scheduler.parse_command("08:00 x 2026-11-20~2026-11-11", today=date(2026, 11, 10))


class DueTests(unittest.TestCase):
    def test_due_daily_and_dedupe(self):
        now = datetime(2026, 11, 10, 8, 0)
        item = scheduler.Schedule("a", "umo", "08:00", "早", scheduler.DateRule())
        self.assertEqual([x.id for x in scheduler.due_schedules(now, [item])], ["a"])
        item.last_fired = "2026-11-10"
        self.assertEqual(scheduler.due_schedules(now, [item]), [])

    def test_range_and_weekday(self):
        now = datetime(2026, 11, 10, 8, 0)  # Tuesday
        weekday = scheduler.Schedule("a", "umo", "08:00", "早", scheduler.DateRule(kind="weekdays", weekdays=(2,)))
        other = scheduler.Schedule("b", "umo", "08:00", "早", scheduler.DateRule(kind="weekdays", weekdays=(1,)))
        self.assertEqual([x.id for x in scheduler.due_schedules(now, [weekday, other])], ["a"])

    def test_workday_resolver(self):
        class Resolver:
            def __init__(self, workday): self.workday = workday
            def is_workday(self, day): return self.workday

        now = datetime(2026, 11, 10, 8, 0)
        item = scheduler.Schedule("a", "umo", "08:00", "早", scheduler.DateRule(kind="workdays"))
        self.assertEqual(len(scheduler.due_schedules(now, [item], Resolver(False))), 0)
        self.assertEqual(len(scheduler.due_schedules(now, [item], Resolver(True))), 1)


class StoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedules.json"
            store = scheduler.ScheduleStore(path)
            store.add(scheduler.Schedule("a", "umo", "08:00", "早", scheduler.DateRule(kind="weekdays", weekdays=(1, 3))))
            self.assertTrue(path.exists())
            loaded = scheduler.ScheduleStore(path)
            self.assertEqual(loaded.list()[0].rule.weekdays, (1, 3))
            loaded.mark_fired("a", date(2026, 11, 10))
            self.assertEqual(loaded.list()[0].last_fired, "2026-11-10")
            self.assertTrue(loaded.delete("a"))
            self.assertFalse(loaded.delete("a"))


class HolidayTests(unittest.TestCase):
    def test_fallback_and_injected_type(self):
        resolver = scheduler.HolidayResolver(enabled=False)
        self.assertTrue(resolver.is_workday(date(2026, 11, 10)))
        self.assertFalse(resolver.is_workday(date(2026, 11, 14)))
        injected = scheduler.HolidayResolver(fetcher=lambda day: {"type": {"type": 2}})
        self.assertFalse(injected.is_workday(date(2026, 11, 10)))
        overtime = scheduler.HolidayResolver(fetcher=lambda day: {"type": {"type": 3}})
        self.assertTrue(overtime.is_workday(date(2026, 11, 14)))


if __name__ == "__main__":
    unittest.main()
