"""发送闸：本地最小间隔、403 冷却写盘、跨实例生效。

上游 MCP 的冷却活在子进程内存里，进程一重启就清零；这里要保证的是
「换一个 SendGate 实例（等价于子进程重启）仍然记得还在冷却」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.radar import RadarError
from gateway.radar.gate import COOLDOWN_SEC, CROSS_INTERVAL, SendGate


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class IntervalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()
        self.gate = SendGate(self.home, clock=self.clock)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_first_send_passes(self) -> None:
        self.gate.check("dm")          # 不抛就算过

    def test_same_channel_too_soon(self) -> None:
        self.gate.record_sent("dm")
        with self.assertRaises(RadarError) as ctx:
            self.gate.check("dm")
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_same_channel_after_interval_passes(self) -> None:
        self.gate.record_sent("dm")
        self.clock.tick(46)
        self.gate.check("dm")

    def test_cross_channel_too_soon(self) -> None:
        self.gate.record_sent("dm")
        self.clock.tick(5)
        with self.assertRaises(RadarError) as ctx:
            self.gate.check("comment")
        self.assertEqual(ctx.exception.code, "rate_limited")

    def test_cross_channel_after_gap_passes(self) -> None:
        self.gate.record_sent("dm")
        self.clock.tick(CROSS_INTERVAL + 1)
        self.gate.check("comment")

    def test_send_after_failure_still_counts(self) -> None:
        """发失败了也算占用间隔 —— 不然失败重试会连环撞墙。"""
        self.gate.record_sent("comment")
        with self.assertRaises(RadarError):
            self.gate.check("comment")


class CooldownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.clock = Clock()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rate_limited_blocks_send(self) -> None:
        gate = SendGate(self.home, clock=self.clock)
        gate.record_limited("dm")
        with self.assertRaises(RadarError) as ctx:
            gate.check("dm")
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertIn("别重试", ctx.exception.message)

    def test_cooldown_survives_a_new_instance(self) -> None:
        """子进程重启后必须还记得冷却 —— 这正是上游做不到的那一半。"""
        SendGate(self.home, clock=self.clock).record_limited("dm")
        fresh = SendGate(self.home, clock=self.clock)
        with self.assertRaises(RadarError):
            fresh.check("dm")

    def test_cooldown_blocks_every_channel(self) -> None:
        gate = SendGate(self.home, clock=self.clock)
        gate.record_limited("dm")
        with self.assertRaises(RadarError):
            gate.check("comment")

    def test_cooldown_expires(self) -> None:
        gate = SendGate(self.home, clock=self.clock)
        gate.record_limited("dm")
        self.clock.tick(COOLDOWN_SEC + 1)
        gate.check("dm")               # 冷却过了就能发

    def test_state_reports_remaining(self) -> None:
        gate = SendGate(self.home, clock=self.clock)
        gate.record_limited("dm")
        self.assertGreater(gate.state()["cooldown_sec"], 0)

    def test_corrupt_state_does_not_lock_forever(self) -> None:
        """状态文件坏了要当成「没有冷却」，不能把账号永久锁死。"""
        (self.home / "send-gate.json").write_text("{oops", encoding="utf-8")
        SendGate(self.home, clock=self.clock).check("dm")


if __name__ == "__main__":
    unittest.main()
