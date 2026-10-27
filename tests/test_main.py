"""入口的健壮性测试：一个平台崩了不能拖垮整个网关。"""

from __future__ import annotations

import asyncio
import unittest

from gateway.main import _supervise
from gateway.models import Platform


class TestSupervise(unittest.TestCase):
    def test_restarts_after_crash(self) -> None:
        calls: list[int] = []

        class FlakyAdapter:
            async def start(self) -> None:
                calls.append(1)
                if len(calls) < 3:
                    raise RuntimeError("DNS 抽风")

        asyncio.run(_supervise(Platform.BILIBILI, FlakyAdapter()))
        self.assertEqual(len(calls), 3)

    def test_returns_when_adapter_finishes_normally(self) -> None:
        calls: list[int] = []

        class QuietAdapter:
            async def start(self) -> None:
                calls.append(1)

        asyncio.run(_supervise(Platform.QQ, QuietAdapter()))
        self.assertEqual(len(calls), 1)

    def test_cancellation_propagates(self) -> None:
        class Stubborn:
            async def start(self) -> None:
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(_supervise(Platform.QQ, Stubborn()))


if __name__ == "__main__":
    unittest.main()
