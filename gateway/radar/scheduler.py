"""峰谷时段定时：自己写一个 asyncio tick，不引 APScheduler。

为什么不引现成的
    `requirements.txt` 里没有 APScheduler —— 之前包里的 `apscheduler` 只是
    `bilibili_api` 的传递依赖被 PyInstaller 顺带打进去的，删掉它不影响任何东西。
    我们需要的只有「每分钟看一眼现在该不该跑、今天这个时段跑过没有」，
    十几行 asyncio 就够，没必要为一条 cron 表达式拉一整个调度框架。

三条硬规矩
    1. **默认关**。没人在设置页显式打开，它就永远不动。
    2. 每个时段**每天只跑一次**。同时命中多个时段时只跑最先命中的那个。
    3. 状态落 ``<data_dir>/radar-schedule.json``：跨天自动翻篇，
       重启网关不会在同一天把一个时段再跑一遍。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

RunOnce = Callable[[], Awaitable[Any]]
_HM_RE = None  # 延迟编译，见 parse_hm


@dataclass(slots=True)
class Window:
    """一个时段。``key`` 是稳定标识，状态文件按它记「今天跑过没有」。"""

    key: str
    start: str = "09:00"
    end: str = "10:00"
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "start": self.start, "end": self.end, "enabled": self.enabled}

    @staticmethod
    def from_dict(raw: Any, index: int = 0, prefix: str = "custom") -> "Window":
        if not isinstance(raw, dict):
            return Window(key=f"{prefix}#{index + 1}")
        return Window(
            key=str(raw.get("key") or f"{prefix}#{index + 1}"),
            start=str(raw.get("start") or "09:00"),
            end=str(raw.get("end") or "10:00"),
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(slots=True)
class ScheduleConfig:
    """设置页那一块。默认整块关闭。"""

    enabled: bool = False
    windows: list[Window] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "windows": [w.to_dict() for w in self.windows]}

    @staticmethod
    def from_dict(raw: Any) -> "ScheduleConfig":
        if not isinstance(raw, dict):
            return ScheduleConfig()
        items = raw.get("windows")
        windows: list[Window] = []
        if isinstance(items, list):
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "")
                prefix = key.split("#")[0] if key else "custom"
                windows.append(Window.from_dict(item, i, prefix))
        return ScheduleConfig(enabled=bool(raw.get("enabled", False)), windows=windows)


def parse_hm(text: str) -> int | None:
    """``"09:30"`` → 570（当天的分钟数）。格式不对返回 None，不猜。"""
    raw = str(text or "").strip()
    if not raw or ":" not in raw:
        return None
    hh, _, mm = raw.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def in_window(now_minute: int, start: str, end: str) -> bool:
    """现在在不在这个时段里。支持跨零点（``23:00`` → ``01:30``）。

    跨零点的判断很容易写错：``start < end`` 时是普通区间，
    否则区间被零点切成两段，两段都要算。
    """
    s, e = parse_hm(start), parse_hm(end)
    if s is None or e is None or s == e:
        return False
    if s < e:
        return s <= now_minute < e
    return now_minute >= s or now_minute < e


def now_minute(ts: float | None = None) -> int:
    moment = datetime.fromtimestamp(ts if ts is not None else time.time())
    return moment.hour * 60 + moment.minute


class RadarScheduler:
    """定时器本体。``run_once`` 由上层注入（真正去刷一轮的那个协程）。"""

    def __init__(self, run_once: RunOnce, *, state_path: Path,
                 config: ScheduleConfig | None = None,
                 clock: Callable[[], float] = time.time,
                 interval: float = 60.0) -> None:
        self._run_once = run_once
        self._state_path = Path(state_path)
        self._cfg = config or ScheduleConfig()
        self._clock = clock
        self._interval = max(5.0, float(interval))
        self._task: asyncio.Task | None = None
        self._busy = False
        self._state: dict[str, Any] = self._load()

    # ---- 配置 ----

    @property
    def config(self) -> ScheduleConfig:
        return self._cfg

    def configure(self, cfg: ScheduleConfig) -> None:
        """设置页改了立刻生效，不用重启。"""
        self._cfg = cfg

    # ---- 状态 ----

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:  # 状态写不下去也不该让定时器崩
            log.warning("定时状态写入失败：%s", exc)

    def _today(self, now: float) -> str:
        return date.fromtimestamp(now).isoformat()

    def fired_today(self, now: float | None = None) -> set[str]:
        moment = now if now is not None else self._clock()
        state = self._state
        if state.get("day") != self._today(moment):
            return set()
        fired = state.get("fired")
        return set(fired) if isinstance(fired, list) else set()

    # ---- 核心 ----

    async def tick(self, now: float | None = None) -> dict[str, Any]:
        """看一次现在该不该跑。返回发生了什么，方便界面和日志显示。

        不抛异常：定时器炸掉不该影响网关其他部分。
        """
        moment = now if now is not None else self._clock()
        day = self._today(moment)
        result: dict[str, Any] = {"day": day, "fired": "", "skipped": "", "error": ""}

        if not self._cfg.enabled:
            result["skipped"] = "off"
            return result
        if self._busy:
            result["skipped"] = "busy"
            return result
        if self._state.get("day") != day:
            # 跨天：把「今天跑过」的账清掉，别的字段不动
            self._state = {"day": day, "fired": []}
            self._save(self._state)
        fired = self.fired_today(moment)

        minute = now_minute(moment)
        hit: Window | None = None
        for w in self._cfg.windows:
            if not w.enabled or w.key in fired:
                continue
            if in_window(minute, w.start, w.end):
                hit = w
                break
        if hit is None:
            result["skipped"] = "not_due"
            return result

        self._busy = True
        try:
            await self._run_once()
            result["fired"] = hit.key
        except Exception as exc:  # noqa: BLE001 - 一轮失败不能停表
            log.exception("定时刷一轮失败（%s）", hit.key)
            result["error"] = str(exc)[:200]
        finally:
            self._busy = False
        self._state = {"day": day, "fired": sorted(fired | {hit.key})}
        self._save(self._state)
        return result

    async def start(self) -> None:
        """常驻循环。网关退出时由 ``stop`` 取消。"""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="radar-scheduler")

    async def _loop(self) -> None:
        try:
            while True:
                await self.tick()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def next_due(self, now: float | None = None) -> dict[str, Any] | None:
        """下一个还没跑过的时段（给界面显示「下次 19:00」）。"""
        moment = now if now is not None else self._clock()
        fired = self.fired_today(moment)
        minute = now_minute(moment)
        best: tuple[int, Window] | None = None
        for w in self._cfg.windows:
            if not w.enabled or w.key in fired:
                continue
            s = parse_hm(w.start)
            if s is None:
                continue
            delta = (s - minute) % (24 * 60)
            if best is None or delta < best[0]:
                best = (delta, w)
        if best is None:
            return None
        return {"key": best[1].key, "start": best[1].start, "in_minutes": best[0]}
