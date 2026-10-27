"""本地转写的队列与记账：一轮最多排一条，job 状态跨轮活下来。

为什么要这一层（上游已经有 ``bili_transcribe_start`` 了）
    上游的 job 活在 **MCP 子进程的内存里**（``_TRANSCRIBE_JOBS``）。子进程一重启
    —— 网关重启、空闲超时被杀、崩溃重试 —— 那些 job_id 就全成了孤儿，
    ``bili_transcribe_result`` 只会回「没有这个 job」。
    所以这一层负责两件上游不管的事：
      * **队列约束**：whisper 吃 CPU，一轮最多排一条（多开只会互相拖死，
        上游 2026-10-25 有群友实测：两条并行 ⇒ 整条通道超时、私信都没发出去）；
      * **跨轮记账**：job 落在 ``transcribe-jobs.json``，下一轮还能接着取。

顺序红线（章程）：报告 → 判重表 → 归档 → 私信，**这些都落完地之后**才碰转写。
所以这里只提供「排一条 / 取结果」，不主动在轮中等待。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 一条 job 超过这么久还没结果就当它死了（whisper small 跑 20 分钟视频约 3~5 分钟）
JOB_TTL = 3600.0

# 脚本里 `tmp/` 下正在转的音频；跑完的文本在 `tmp/<bv>.txt`
AUDIO_SUFFIXES = (".m4a", ".mp3", ".webm", ".wav")


@dataclass(slots=True)
class Job:
    job_id: str
    bvid: str
    started: float = field(default_factory=time.time)
    state: str = "running"       # running | done | failed | timeout
    text_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"job_id": self.job_id, "bvid": self.bvid, "started": self.started,
                "state": self.state, "text_chars": self.text_chars}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Job":
        # ⚠️ 别写成 ``float(raw.get("started") or time.time())``：``0.0`` 是假值，
        # 会被吞成「刚刚开始」⇒ 过期 job 永远不过期 ⇒ 队列被一条死 job 永久堵住。
        started = raw.get("started")
        return cls(
            job_id=str(raw.get("job_id") or ""),
            bvid=str(raw.get("bvid") or ""),
            started=float(started) if isinstance(started, (int, float)) else time.time(),
            state=str(raw.get("state") or "running"),
            text_chars=int(raw.get("text_chars") or 0),
        )

    @property
    def expired(self) -> bool:
        return self.state == "running" and time.time() - self.started > JOB_TTL


def _path(home: Path | str) -> Path:
    return Path(home) / "transcribe-jobs.json"


def load_jobs(home: Path | str) -> dict[str, Job]:
    """读账本。读不出来就当空 —— 但调用方要知道这只意味着「没有记录」。"""
    try:
        raw = json.loads(_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    jobs: dict[str, Job] = {}
    for job_id, item in raw.items():
        if isinstance(item, dict):
            job = Job.from_dict({**item, "job_id": job_id})
            if job.bvid:
                jobs[job_id] = job
    return jobs


def save_jobs(home: Path | str, jobs: dict[str, Job]) -> None:
    try:
        path = _path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({k: v.to_dict() for k, v in jobs.items()},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("转写账本写不进去：%s", exc)


def running(home: Path | str) -> list[Job]:
    """还在跑的 job。过期的不算（否则一个死 job 会永久堵住队列）。"""
    return [job for job in load_jobs(home).values() if job.state == "running" and not job.expired]


def can_enqueue(home: Path | str) -> tuple[bool, str]:
    """这一轮还能不能排一条。``(能排, 不能排的原因)``。"""
    active = running(home)
    if active:
        return False, f"上一条还在转（{active[0].bvid}），一轮最多排一条"
    return True, ""


async def enqueue(radar: Any, home: Path | str, bvid: str) -> dict[str, Any]:
    """排一条转写。被队列挡住时返回 ``ok:False`` 而不是抛异常 —— 这不是错误。"""
    allowed, why = can_enqueue(home)
    if not allowed:
        return {"ok": False, "queued": False, "error": why}
    try:
        payload = await radar.transcribe_start(bvid)
    except Exception as exc:  # noqa: BLE001 - 转写起不来不该拖垮这一轮
        log.warning("转写排不进去：%s：%s", bvid, exc)
        return {"ok": False, "queued": False, "error": str(exc)[:200]}
    job_id = str(payload.get("job_id") or "")
    if not payload.get("ok") or not job_id:
        return {"ok": False, "queued": False,
                "error": str(payload.get("error") or "上游没给 job_id")}
    jobs = load_jobs(home)
    jobs[job_id] = Job(job_id=job_id, bvid=bvid)
    save_jobs(home, jobs)
    return {"ok": True, "queued": True, "job_id": job_id, "bvid": bvid,
            # 章程原话：看到这句就别等，先干别的
            "hint": "已排队。这一轮别等：先把报告 / 判重表 / 私信做完，下一轮再取结果。"}


async def collect(radar: Any, home: Path | str, *, max_wait: float = 0.0) -> list[dict[str, Any]]:
    """取一轮结果：把账本里所有 running 的 job 都问一遍。

    ``max_wait=0`` 表示**只问一次不等**（收尾时的默认行为）。真等要在后台线程里做。
    """
    jobs = load_jobs(home)
    if not jobs:
        return []
    results: list[dict[str, Any]] = []
    changed = False
    deadline = time.time() + max(0.0, float(max_wait))
    for job in list(jobs.values()):
        if job.state != "running":
            continue
        if job.expired:
            job.state = "timeout"
            changed = True
            results.append({"bvid": job.bvid, "job_id": job.job_id,
                            "state": "timeout", "error": "超时没有结果"})
            continue
        try:
            payload = await radar.transcribe_result(job.job_id)
        except Exception as exc:  # noqa: BLE001 - 取不到就当还在跑
            log.info("取转写结果失败（%s）：%s", job.job_id, exc)
            continue
        state = str(payload.get("state") or "")
        text = str(payload.get("text") or "")
        if state == "running" and not text:
            continue
        job.state = "done" if (text or state == "done") else (state or "failed")
        job.text_chars = len(text)
        changed = True
        results.append({
            "bvid": job.bvid, "job_id": job.job_id, "state": job.state,
            "chars": job.text_chars, "speechless": is_speechless(text),
            "text": text,
        })
    if changed:
        save_jobs(home, jobs)
    _ = deadline
    return results


def is_speechless(text: str) -> bool:
    """空 / 太短 / 只有「字幕by某某」署名 ⇒ 这条没有语音。

    章程里这是**正常结果**，不是转写坏了：记「跳过（无语音）」继续，
    别重试、别写 debug 脚本（有用户为这个烧了 27 次工具调用）。
    """
    body = (text or "").strip()
    if len(body) < 8:
        return True
    return body.startswith("字幕by") or "字幕by" in body[:20].lower()


def cleanup(home: Path | str, *, keep: int = 20) -> int:
    """把账本裁到最近 ``keep`` 条，返回删掉几条。历史一直在也没用。"""
    jobs = load_jobs(home)
    if len(jobs) <= keep:
        return 0
    ordered = sorted(jobs.values(), key=lambda j: j.started, reverse=True)
    kept = {job.job_id: job for job in ordered[:keep]}
    removed = len(jobs) - len(kept)
    save_jobs(home, kept)
    return removed
