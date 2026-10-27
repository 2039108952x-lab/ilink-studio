"""内容雷达的服务层：把 provider / pipeline / report / notify / 配额 串成界面对外的一层。

为什么要有这一层
    控制面（``api.py``）是**同步**的，跑在 HTTP 工作线程里；而抖音的 MCP 子进程
    绑在网关主 event loop 上 —— 自己 ``asyncio.run()`` 会造一个新 loop，子进程就废了。
    所以这里唯一的职责是把调用桥回主 loop：``run_coroutine_threadsafe(...).result()``。

界面只看这几个方法：``status`` / ``run_round`` / ``report`` / ``snapshot`` / ``share``。

不变的约定
    - 刷一轮是长任务（推荐流 + 评论 + 模型判断，可达一两分钟），同一时刻只许跑一轮。
    - 通知失败不许把这一轮搞挂：日报已经落盘了，通知只是顺带。
    - 分享永远先给预览，``confirm=True`` 才是真发；真发送还受 provider 侧发送闸约束。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from .mcp_client import node_status
from .radar import notify as notify_mod
from .radar import report as report_mod
from .radar.pipeline import Pick, RadarPipeline, SemanticJudge, Thresholds
from .radar.provider import RadarError, ShareDraft

log = logging.getLogger(__name__)


class RadarService:
    """一轮一轮的内容雷达。同步门面 + 内部 async 实现。"""

    def __init__(
        self,
        provider: Any,
        data_dir: Path | str,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        brain: Any | None = None,
        thresholds: Thresholds | None = None,
        min_confidence: float = 0.6,
        per_day: int = 5,
        notify_channel: str = "file",
        webhook_url: str = "",
        platform: str = "douyin",
        top: int = 8,
        report_writer: Any | None = None,
        quota_reader: Any | None = None,
        quota_after_run: Any | None = None,
        node_check: Any | None = None,
        videos_reader: Any | None = None,
        settings_view: Any | None = None,
        settings_save: Any | None = None,
    ) -> None:
        self._provider = provider
        self._data = Path(data_dir)
        self._loop = loop
        self._notify_channel = notify_channel
        self._webhook = webhook_url
        self._platform = platform
        self._top = max(1, min(int(top), 30))
        judge = SemanticJudge(brain, min_confidence=min_confidence) if brain is not None else None
        self._pipeline = RadarPipeline(thresholds, judge)
        self._quota = report_mod.QuotaTracker(self._data / "quota.json", per_day=per_day)
        self._last = None          # 上一轮 RoundResult
        self._running = False

        # ---- 三个注入点：抖音与 B 站在这三件事上口径不同，但没有一个该由平台名分支 ----
        # 报告：抖音按「### 1. 标题」写，B 站必须按「### BV号」写（宿主靠它数配额）。
        self._report_writer = report_writer
        # 配额：抖音用 QuotaTracker 自己记账；B 站直接数当天日报里的 ### BV 行 ——
        # 日报是唯一事实来源，两边各记一份必然对不上。
        self._quota_reader = quota_reader
        self._quota_after_run = quota_after_run
        # 体检：抖音要查本机 Node 版本，B 站跑的是 Python，查 Node 只会误导。
        self._node_check = node_check if node_check is not None else node_status
        # 「它刷过的视频」：B 站从历史日报里解析（带档位/状态/分页），抖音退回上一轮快照。
        self._videos_reader = videos_reader
        # 设置的读写：B 站有自己那套四页签（关键词/私信方式/自动评论…），
        # 抖音沿用 top + 通知方式那三个。有注入就整套交给它，不做平台名判断。
        self._settings_view = settings_view
        self._settings_save = settings_save

    # ---- 注入点的统一出口 ----

    def _report_path(self) -> Path:
        return report_mod.report_path(self._data)

    def _quota_payload(self) -> dict[str, Any]:
        if self._quota_reader is not None:
            return dict(self._quota_reader())
        return self._quota.state().to_dict()

    def _consume_quota(self, picked: int) -> dict[str, Any]:
        if self._quota_after_run is not None:
            return dict(self._quota_after_run(picked))
        return self._quota.consume(picked).to_dict()

    def _node_payload(self) -> dict[str, Any]:
        if self._node_check is None:
            return {}
        try:
            return dict(self._node_check())
        except Exception:  # noqa: BLE001 - 体检失败不许拖垮状态接口
            return {}

    # ---- 主 loop 桥 ----

    def _call(self, coro, timeout: float = 300.0):
        loop = self._loop
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
        return asyncio.run(coro)

    # ---- 状态 ----

    def status(self) -> dict[str, Any]:
        path = self._report_path()
        info: dict[str, Any] = {
            "platform": self._platform,
            "quota": self._quota_payload(),
            "report": str(path),
            "report_day": report_mod.today_stamp(),
            "has_report": path.is_file(),
            "running": self._running,
            "last": self._last_summary(),
            "node": self._node_payload(),
        }
        try:
            who = self._call(self._provider.whoami(), timeout=30.0)
            info["identity"] = who
            info["ready"] = bool(who.get("logged_in"))
        except RadarError as exc:
            info["identity"] = {"ok": False, "error": exc.message, "code": exc.code}
            info["ready"] = False
        except Exception as exc:  # noqa: BLE001 - 状态接口不许炸
            info["identity"] = {"ok": False, "error": str(exc)[:200]}
            info["ready"] = False
        return info

    def _last_summary(self) -> dict[str, Any] | None:
        if self._last is None:
            return None
        return {
            "seen": self._last.seen,
            "picked": len(self._last.picked),
            "uncertain": len(self._last.uncertain),
            "dropped": dict(self._last.dropped),
            "elapsed": round(self._last.elapsed, 1),
            "note": self._last.note,
        }

    def snapshot(self) -> dict[str, Any]:
        """上一轮的候选清单，给界面直接用。没跑过就是空的，不编。"""
        if self._last is None:
            return {"seen": 0, "picked": [], "uncertain": [], "dropped": {}, "note": ""}
        return {
            "seen": self._last.seen,
            "picked": [_row(p) for p in self._last.picked],
            "uncertain": [_row(p) for p in self._last.uncertain],
            "dropped": dict(self._last.dropped),
            "note": self._last.note,
        }

    def videos(self, *, day: str = "", grade: str = "", status: str = "",
               page: str = "1", size: str = "20") -> dict[str, Any]:
        """「它刷过的视频」：带档位/状态筛选与分页。

        B 站从历史日报里解析（注入 reader）；抖音没有这套，退回上一轮快照 ——
        有就有、没有就空着，不拿假数据凑一屏。
        """
        if self._videos_reader is not None:
            try:
                return dict(self._videos_reader(
                    day=day, grade=grade, status=status,
                    page=_as_int(page, 1), size=_as_int(size, 20),
                ))
            except Exception as exc:  # noqa: BLE001 - 列表读不出来要说清楚原因
                log.warning("视频列表读取失败：%s", exc)
                raise RadarError("tool_failed", f"视频列表读不出来：{exc}") from exc
        picked = self.snapshot().get("picked") or []
        return {"items": picked, "total": len(picked), "page": 1, "size": len(picked),
                "grade": "", "status": "", "note": "这个平台没有历史列表，只有上一轮快照"}

    # ---- 跑一轮 ----

    def run_round(self, *, top: int | None = None, notify: bool = True) -> dict[str, Any]:
        if self._running:
            raise RadarError("busy", "上一轮还没跑完，等它结束")
        if top is None:
            top = int(self.settings()["top"])
        self._running = True
        log.info("雷达开始刷一轮：top=%d", top)
        try:
            return self._call(self._run_and_write(top, notify), timeout=900.0)
        finally:
            self._running = False

    async def _run_and_write(self, top: int, notify: bool) -> dict[str, Any]:
        started = time.time()
        result = await self._pipeline.run(self._provider, top=top)
        self._last = result
        # 报告口径按平台注入：抖音走 report_mod，B 站必须是 duty 那套（### BV号 + 档位）
        if self._report_writer is not None:
            try:
                path = Path(self._report_writer(result))
            except Exception as exc:  # noqa: BLE001 - 报告写不出来要让这一轮显式失败
                log.exception("雷达报告写入失败")
                raise RadarError("report_failed", f"日报写不出来：{exc}") from exc
        else:
            path = report_mod.write(result, self._data)
        quota = self._consume_quota(len(result.picked))
        delivered: dict[str, Any] = {}
        if notify:
            try:
                notice = notify_mod.round_notice(result, report_path=Path(path))
                notifier = notify_mod.Notifier(
                    self._data, channel=self._notify_channel, webhook_url=self._webhook)
                delivered = await notifier.send(notice)
            except Exception as exc:  # noqa: BLE001 - 日报已落盘，通知挂了不算失败
                log.warning("通知发送失败：%s", exc)
                delivered = {"ok": False, "error": str(exc)[:200]}
        log.info("雷达一轮完成：看到 %d 条，推 %d 条，用时 %.1fs",
                 result.seen, len(result.picked), time.time() - started)
        return {
            "ok": True,
            "report": str(path),
            "quota": quota,
            "summary": self._last_summary(),
            "notify": delivered,
            "candidates": self.snapshot(),
        }

    # ---- 日报 ----

    def report(self, day: str = "") -> dict[str, Any]:
        target = day or report_mod.today_stamp()
        parsed = _as_day(target)
        path = report_mod.report_path(self._data, parsed)
        return {"day": target, "path": str(path),
                "markdown": report_mod.read(self._data, parsed)}

    def history(self, *, limit: int = 30) -> list[dict[str, object]]:
        return report_mod.list_reports(self._data, limit=limit)

    # ---- 环境体检 ----

    def doctor(self) -> dict[str, Any]:
        """Node / Python / venv / Cookie / 浏览器 / 家目录 逐项体检。"""
        call = getattr(self._provider, "doctor", None)
        if call is None:
            return {"ok": False, "error": "这个平台没有体检接口"}
        try:
            return self._call(call(), timeout=120.0)
        except RadarError as exc:
            return {"ok": False, "error": exc.message, "code": exc.code}
        except Exception as exc:  # noqa: BLE001 - 体检本身不许炸
            return {"ok": False, "error": str(exc)[:200]}

    # ---- 设置 ----

    @property
    def _settings_file(self) -> Path:
        return self._data / "radar-settings.json"

    def _raw_settings(self) -> dict[str, Any]:
        try:
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def settings(self) -> dict[str, Any]:
        if self._settings_view is not None:
            try:
                return dict(self._settings_view())
            except Exception as exc:  # noqa: BLE001 - 读设置失败要说清楚
                raise RadarError("tool_failed", f"设置读不出来：{exc}") from exc
        raw = self._raw_settings()
        try:
            top = max(1, min(int(raw.get("top") or self._top), 30))
        except (TypeError, ValueError):
            top = self._top
        configured = str(raw.get("webhook_url") or self._webhook)
        return {
            "top": top,
            "notify": str(raw.get("notify") or self._notify_channel),
            "webhook_url": notify_mod.mask_url(configured),
            "has_webhook": bool(configured),
            "per_day": self._quota.state().per_day,
            "channels": list(notify_mod.CHANNELS),
        }

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """改设置。webhook 地址不会原样回给界面（回来的是打码版）。"""
        if self._settings_save is not None:
            try:
                self._settings_save(dict(payload or {}))
            except ValueError as exc:
                raise RadarError("invalid_args", str(exc)) from None
            except OSError as exc:
                raise RadarError("tool_failed", f"设置写不进去：{exc}") from None
            return self.settings()
        data = self._raw_settings()
        if "top" in payload:
            try:
                data["top"] = max(1, min(int(payload["top"]), 30))
            except (TypeError, ValueError):
                raise RadarError("invalid_args", "「每次几条」要是数字") from None
        if "notify" in payload:
            channel = str(payload["notify"] or "file")
            if channel not in notify_mod.CHANNELS:
                raise RadarError("invalid_args",
                                 f"通知方式只能是 {' / '.join(notify_mod.CHANNELS)}")
            data["notify"] = channel
        if "webhook_url" in payload:
            url = str(payload["webhook_url"] or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise RadarError("invalid_args", "webhook 只接受 http/https 地址")
            data["webhook_url"] = url
        try:
            self._settings_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._settings_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._settings_file)
        except OSError as exc:
            raise RadarError("tool_failed", f"设置写不进去：{exc}") from exc
        if "notify" in data:
            self._notify_channel = data["notify"]
        if "webhook_url" in data:
            self._webhook = data["webhook_url"]
        return self.settings()

    # ---- 分享 ----

    def share(self, *, item_id: str, to: str, reason: str = "",
              confirm: bool = False) -> dict[str, Any]:
        draft = ShareDraft(item_id=item_id, to=to, reason=reason)
        out = self._call(self._provider.send_share(draft, confirm=confirm), timeout=300.0)
        return {"item_id": out.item_id, "to": out.to, "sent": out.sent,
                "preview": out.preview, "detail": out.detail}

    def comment(self, *, item_id: str, text: str, reply_to: int = 0, root: int = 0,
                confirm: bool = False) -> dict[str, Any]:
        """发评论 / 回复评论。``confirm=False`` 只回草稿，一个字都不发。

        ``reply_to`` 是要回复的那条 id，``root`` 是楼中楼的楼层根。两个平台
        底层参数名不一样（抖音叫 comment_id，B 站叫 rpid），由各自 ``post_comment`` 收敛。
        """
        call = getattr(self._provider, "post_comment", None)
        if call is None:
            raise RadarError("unsupported", f"{self._platform} 这个通道不支持发评论")
        out = self._call(
            call(item_id=item_id, text=text, reply_to=int(reply_to or 0),
                 root=int(root or 0), confirm=confirm),
            timeout=300.0,
        )
        return dict(out) if isinstance(out, dict) else {"ok": True, "raw": out}

    async def close(self) -> None:
        closer = getattr(self._provider, "close", None)
        if closer is not None:
            await closer()


def _as_day(text: str) -> date | None:
    """``report.py`` 的日期参数要 ``date`` 对象；界面传的是 ``YYYY-MM-DD`` 字符串。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        return None


def _as_int(text: Any, default: int) -> int:
    """界面传上来的都是字符串；认不出来就回默认值，不抛异常。"""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _row(pick: Pick) -> dict[str, Any]:
    """一条候选 → 界面口径。评论区只带前三条，别把整页塞进响应。"""
    c = pick.candidate
    j = pick.judgment
    return {
        "item_id": c.item_id,
        "title": c.title,
        "author": c.author,
        "duration_sec": c.duration_sec,
        "url": c.url,
        "stats": dict(c.stats),
        "score": pick.score,
        "worth": j.worth if j else None,
        "reason": j.reason if j else "",
        "tags": list(j.tags) if j else [],
        "error": j.error if j else "",
        "comments": [{"text": x.text, "likes": x.likes} for x in pick.comments[:3]],
    }
