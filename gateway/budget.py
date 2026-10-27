"""每日预算与平台速率闸门（FR-5.6 / FR-5.7 / AC-19）。

所有判断都在**调用模型之前**做：超了就把消息标成 skipped 并写审计，
绝不先花完钱再提示。token 用量拿不到真实 usage 时按估算计入，并在指标里标记。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any


class BudgetGate:
    def __init__(self, metrics, *, clock=time.time, window_seconds: float = 60.0) -> None:
        self._metrics = metrics
        self._clock = clock
        self._window = float(window_seconds)
        self._recent: dict[str, deque[float]] = defaultdict(deque)

    def check(self, cfg, platform: str) -> str | None:
        """返回 None 表示放行；否则返回 skip_reason（budget_* / rate_limited）。"""
        budget = cfg.budget
        if self._metrics is not None and (budget.daily_tokens or budget.daily_cost_usd):
            snapshot = self._metrics.snapshot(
                price_per_1k_input=budget.price_per_1k_input,
                price_per_1k_output=budget.price_per_1k_output,
            )
            used = int(snapshot["tokens"]["total"])
            if budget.daily_tokens and used >= int(budget.daily_tokens):
                return "budget_tokens_exhausted"
            cost = snapshot.get("cost_usd") or 0.0
            if budget.daily_cost_usd and cost >= float(budget.daily_cost_usd):
                return "budget_cost_exhausted"
        now = self._clock()
        queue = self._recent[platform]
        while queue and now - queue[0] > self._window:
            queue.popleft()
        if budget.platform_per_minute and len(queue) >= int(budget.platform_per_minute):
            return "rate_limited"
        return None

    def record(self, platform: str) -> None:
        self._recent[platform].append(self._clock())

    @staticmethod
    def queue_full(cfg, queue: dict[str, Any]) -> bool:
        limit = int(cfg.budget.queue_limit or 0)
        return bool(limit) and int(queue.get("inbound", 0)) >= limit
