"""定时与配额的测试：默认关、每天一次、跨零点、跨天归零。

时间一律注入，不真的等一分钟。
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from gateway.radar.report import QuotaTracker
from gateway.radar.scheduler import (
    RadarScheduler,
    ScheduleConfig,
    Window,
    in_window,
    now_minute,
    parse_hm,
)


def _ts(y=2026, m=10, d=26, hh=9, mm=5) -> float:
    return dt.datetime(y, m, d, hh, mm).timestamp()


class TestParseHm(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(parse_hm("09:30"), 570)
        self.assertEqual(parse_hm("23:59"), 1439)

    def test_bad_input_returns_none(self):
        for bad in ("", "930", "24:00", "09:60", "abc", "09:xx", None):
            self.assertIsNone(parse_hm(bad), bad)


class TestInWindow(unittest.TestCase):
    def test_plain_range_is_half_open(self):
        self.assertTrue(in_window(9 * 60, "09:00", "10:00"))
        self.assertTrue(in_window(9 * 60 + 59, "09:00", "10:00"))
        self.assertFalse(in_window(10 * 60, "09:00", "10:00"))

    def test_before_range(self):
        self.assertFalse(in_window(8 * 60, "09:00", "10:00"))

    def test_crossing_midnight_covers_both_sides(self):
        # 23:00 → 01:30 被零点切成两段
        self.assertTrue(in_window(23 * 60 + 30, "23:00", "01:30"))
        self.assertTrue(in_window(0 * 60 + 30, "23:00", "01:30"))
        self.assertFalse(in_window(1 * 60 + 30, "23:00", "01:30"))
        self.assertFalse(in_window(12 * 60, "23:00", "01:30"))

    def test_equal_endpoints_never_match(self):
        self.assertFalse(in_window(9 * 60, "09:00", "09:00"))

    def test_invalid_window_never_matches(self):
        self.assertFalse(in_window(600, "bad", "10:00"))

    def test_now_minute_from_timestamp(self):
        self.assertEqual(now_minute(_ts(hh=13, mm=45)), 13 * 60 + 45)


def _cfg(enabled=True, windows=None) -> ScheduleConfig:
    return ScheduleConfig(enabled=enabled, windows=windows or [
        Window(key="peak#1", start="09:00", end="10:00"),
    ])


class _Runner:
    def __init__(self, *, boom: bool = False):
        self.calls = 0
        self.boom = boom

    async def __call__(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("刷一轮炸了")
        return "ok"


class TestScheduler(unittest.IsolatedAsyncioTestCase):
    def _mk(self, tmp, runner, cfg=None, clock=None):
        return RadarScheduler(runner, state_path=Path(tmp) / "sched.json",
                              config=cfg or _cfg(), clock=clock or (lambda: _ts()))

    async def test_disabled_by_default_never_runs(self):
        runner = _Runner()
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner, cfg=ScheduleConfig())
            out = await sched.tick(_ts())
            self.assertEqual(out["skipped"], "off")
            self.assertEqual(runner.calls, 0)

    async def test_fires_once_inside_the_window(self):
        runner = _Runner()
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner)
            out = await sched.tick(_ts(hh=9, mm=5))
            self.assertEqual(out["fired"], "peak#1")
            self.assertEqual(runner.calls, 1)
            again = await sched.tick(_ts(hh=9, mm=30))
            self.assertEqual(again["skipped"], "not_due")
            self.assertEqual(runner.calls, 1)

    async def test_outside_window_does_nothing(self):
        runner = _Runner()
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner)
            out = await sched.tick(_ts(hh=14, mm=0))
            self.assertEqual(out["skipped"], "not_due")
            self.assertEqual(runner.calls, 0)

    async def test_next_day_fires_again(self):
        runner = _Runner()
        with tempfile.TemporaryDirectory() as tmp:
            clock = lambda: _ts()  # noqa: E731
            sched = self._mk(tmp, runner, clock=clock)
            await sched.tick(_ts(d=26, hh=9, mm=5))
            await sched.tick(_ts(d=27, hh=9, mm=5))
            self.assertEqual(runner.calls, 2)

    async def test_state_survives_restart(self):
        runner = _Runner()
        with tempfile.TemporaryDirectory() as tmp:
            first = self._mk(tmp, runner)
            await first.tick(_ts(hh=9, mm=5))
            second = self._mk(tmp, runner)   # 重启后的新实例
            out = await second.tick(_ts(hh=9, mm=20))
            self.assertEqual(out["skipped"], "not_due")
            self.assertEqual(runner.calls, 1)

    async def test_first_matching_window_wins(self):
        runner = _Runner()
        cfg = _cfg(windows=[
            Window(key="peak#1", start="09:00", end="10:00"),
            Window(key="peak#2", start="09:00", end="11:00"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner, cfg=cfg)
            out = await sched.tick(_ts(hh=9, mm=10))
            self.assertEqual(out["fired"], "peak#1")
            self.assertEqual(runner.calls, 1)

    async def test_disabled_window_is_skipped(self):
        runner = _Runner()
        cfg = _cfg(windows=[Window(key="peak#1", start="09:00", end="10:00", enabled=False)])
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner, cfg=cfg)
            out = await sched.tick(_ts(hh=9, mm=10))
            self.assertEqual(out["skipped"], "not_due")

    async def test_failure_is_recorded_and_not_retried_same_day(self):
        runner = _Runner(boom=True)
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner)
            out = await sched.tick(_ts(hh=9, mm=5))
            self.assertIn("炸了", out["error"])
            # 当天不重试：宁可今天空一次，也不要每分钟撞一次模型
            again = await sched.tick(_ts(hh=9, mm=30))
            self.assertEqual(again["skipped"], "not_due")
            self.assertEqual(runner.calls, 1)

    async def test_midnight_crossing_window_fires_after_midnight(self):
        runner = _Runner()
        cfg = _cfg(windows=[Window(key="valley#1", start="23:00", end="02:00")])
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner, cfg=cfg)
            out = await sched.tick(_ts(d=26, hh=0, mm=30))
            self.assertEqual(out["fired"], "valley#1")

    async def test_configure_swaps_windows_without_restart(self):
        runner = _Runner()
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, runner, cfg=ScheduleConfig())
            sched.configure(_cfg())
            out = await sched.tick(_ts(hh=9, mm=5))
            self.assertEqual(out["fired"], "peak#1")

    async def test_next_due_reports_minutes_until_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = self._mk(tmp, _Runner())
            due = sched.next_due(_ts(hh=8, mm=0))
            self.assertEqual(due["key"], "peak#1")
            self.assertEqual(due["in_minutes"], 60)

    async def test_stop_is_safe_when_never_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            await self._mk(tmp, _Runner()).stop()


class TestQuotaTracker(unittest.TestCase):
    def test_consume_accumulates(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = QuotaTracker(Path(tmp) / "q.json", per_day=5, clock=lambda: _ts())
            self.assertEqual(q.state().used, 0)
            self.assertEqual(q.consume(2).used, 2)
            self.assertEqual(q.consume(1).used, 3)
            self.assertEqual(q.state().left, 2)

    def test_never_exceeds_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = QuotaTracker(Path(tmp) / "q.json", per_day=3, clock=lambda: _ts())
            q.consume(10)
            state = q.state()
            self.assertEqual(state.used, 3)
            self.assertEqual(state.left, 0)

    def test_new_day_resets_to_zero(self):
        now = {"ts": _ts(d=26)}
        with tempfile.TemporaryDirectory() as tmp:
            q = QuotaTracker(Path(tmp) / "q.json", per_day=5, clock=lambda: now["ts"])
            q.consume(4)
            self.assertEqual(q.state().used, 4)
            now["ts"] = _ts(d=27)
            self.assertEqual(q.state().used, 0)
            self.assertEqual(q.consume(1).used, 1)

    def test_reset_clears_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = QuotaTracker(Path(tmp) / "q.json", per_day=5, clock=lambda: _ts())
            q.consume(3)
            self.assertEqual(q.reset().used, 0)

    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            QuotaTracker(path, per_day=5, clock=lambda: _ts()).consume(2)
            self.assertEqual(QuotaTracker(path, per_day=5, clock=lambda: _ts()).state().used, 2)

    def test_corrupt_state_file_counts_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(QuotaTracker(path, clock=lambda: _ts()).state().used, 0)


if __name__ == "__main__":
    unittest.main()
