"""发送闸：本地最小间隔 + 403 冷却。

上游 MCP 自己也有一层闸（api 45 秒 / ui 20 秒 / 跨通道 15 秒，撞 403 进 10 分钟冷却），
但那是**子进程内存**里的：进程一重启（崩溃恢复、跑完一轮关掉）就清零，
于是我们会照着冷却没过的状态再撞一次墙。抖音对反复撞 403 的处罚是累加的。

所以这里补两件事，都不比上游更严，只是把状态放对地方：

1. 最小间隔按通道记在**内存**（进程内够用，重启后上游也在重新计时）；
2. 403 冷却**写盘**（``<home>/send-gate.json``），跨进程、跨重启都生效。

约定
- 只有 ``confirm=True`` 的真发送才过闸；预览、草稿、读操作一律不拦。
- 冷却中直接抛 ``RadarError("rate_limited", ...)`` 并带剩余秒数，**绝不自动重试**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .provider import RadarError

# 通道 → 最小间隔（秒）。口径对齐上游 AI-USAGE.md，不更严。
CHANNEL_INTERVAL: dict[str, float] = {"dm": 45.0, "comment": 45.0}
# 跨通道最小间隔
CROSS_INTERVAL = 15.0
# 撞 403 后的冷却
COOLDOWN_SEC = 600.0


@dataclass
class SendGate:
    """一个通道层级的发送闸。状态跟着 home 走。"""

    home: Path | str
    clock: Callable[[], float] = time.time
    _last: dict[str, float] = field(default_factory=dict, repr=False)

    @property
    def _file(self) -> Path:
        return Path(self.home) / "send-gate.json"

    def _cooldown_until(self) -> float:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0.0
        if not isinstance(data, dict):
            return 0.0
        try:
            return float(data.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def remaining_cooldown(self) -> float:
        """还剩多少秒冷却。0 表示可以发。"""
        return max(0.0, self._cooldown_until() - self.clock())

    def check(self, channel: str) -> None:
        """能发就什么都不做；不能发就抛 RadarError，带剩余秒数。"""
        left = self.remaining_cooldown()
        if left > 0:
            raise RadarError(
                "rate_limited",
                f"还在限流冷却里，还要等 {int(left) + 1} 秒。别重试 —— 越撞封得越久。",
            )
        now = self.clock()
        gap = CHANNEL_INTERVAL.get(channel, 45.0)
        last = self._last.get(channel)
        if last is not None and now - last < gap:
            wait = int(gap - (now - last)) + 1
            raise RadarError(
                "rate_limited",
                f"发送太密：同一通道要隔 {gap:.0f} 秒，还要等 {wait} 秒。",
            )
        other = max((t for k, t in self._last.items() if k != channel), default=None)
        if other is not None and now - other < CROSS_INTERVAL:
            wait = int(CROSS_INTERVAL - (now - other)) + 1
            raise RadarError(
                "rate_limited",
                f"跨通道发送要隔 {CROSS_INTERVAL:.0f} 秒，还要等 {wait} 秒。",
            )

    def record_sent(self, channel: str) -> None:
        self._last[channel] = self.clock()

    def record_limited(self, channel: str = "") -> None:
        """撞了 403：写盘冷却，跨重启生效。"""
        until = self.clock() + COOLDOWN_SEC
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(
                json.dumps({"cooldown_until": until, "channel": channel,
                            "at": self.clock()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        self._last[channel or "dm"] = self.clock()

    def state(self) -> dict[str, Any]:
        """给界面看的状态。"""
        return {"cooldown_sec": round(self.remaining_cooldown(), 1),
                "channels": {k: round(v, 1) for k, v in self._last.items()}}
