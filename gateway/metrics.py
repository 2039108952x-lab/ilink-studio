"""运行时指标（FR-5.7 / FR-6.9 / §10）。

只记计数和耗时，不记正文。token 用量在拿不到真实 usage 时用**估算**并明确标记，
不假装精确；费用也只在配置了单价时才算。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

LATENCY_KINDS = ("inbound", "send")


class Metrics:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, list[float]] = {kind: [] for kind in LATENCY_KINDS}
        self._samples_limit = 500
        self._day = time.strftime("%Y-%m-%d", time.localtime(clock()))
        self._tokens_in = 0
        self._tokens_out = 0
        self._estimated = 0
        self._started_at = clock()

    # ---- 记录 ----

    def count(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[str(name)] += int(value)

    def observe(self, kind: str, seconds: float) -> None:
        if kind not in self._latencies:
            return
        with self._lock:
            samples = self._latencies[kind]
            samples.append(max(0.0, float(seconds)))
            del samples[:-self._samples_limit]

    def add_tokens(self, *, input_tokens: int = 0, output_tokens: int = 0,
                   estimated: bool = False) -> None:
        self._roll_day()
        with self._lock:
            self._tokens_in += max(0, int(input_tokens))
            self._tokens_out += max(0, int(output_tokens))
            if estimated:
                self._estimated += 1

    # ---- 读取 ----

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        with self._lock:
            if today != self._day:
                self._day = today
                self._tokens_in = self._tokens_out = self._estimated = 0

    @staticmethod
    def _percentile(samples: list[float], ratio: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = max(0, min(len(ordered) - 1, int(round(len(ordered) * ratio)) - 1))
        return round(ordered[index], 3)

    def snapshot(self, *, price_per_1k_input: float = 0.0,
                 price_per_1k_output: float = 0.0) -> dict[str, Any]:
        self._roll_day()
        with self._lock:
            latencies = {
                kind: {
                    "count": len(samples),
                    "p50": self._percentile(samples, 0.50),
                    "p95": self._percentile(samples, 0.95),
                }
                for kind, samples in self._latencies.items()
            }
            tokens_in, tokens_out = self._tokens_in, self._tokens_out
            estimated = self._estimated
            counters = dict(self._counters)
            uptime = max(0.0, self._clock() - self._started_at)
        cost = (tokens_in / 1000.0) * float(price_per_1k_input) + (
            tokens_out / 1000.0) * float(price_per_1k_output)
        return {
            "counters": dict(sorted(counters.items())),
            "latency_seconds": latencies,
            "tokens": {
                "day": self._day,
                "input": tokens_in,
                "output": tokens_out,
                "total": tokens_in + tokens_out,
                "estimated_calls": estimated,
            },
            "cost_usd": round(cost, 6) if (price_per_1k_input or price_per_1k_output) else None,
            "uptime_seconds": round(uptime, 1),
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            for samples in self._latencies.values():
                samples.clear()
            self._tokens_in = self._tokens_out = self._estimated = 0
