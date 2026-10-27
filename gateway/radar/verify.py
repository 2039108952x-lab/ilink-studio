"""评论复核：发出去了**不等于公开可见**。

为什么要有这一层（上游 2026-10-16 用户实测踩出来的）
    B 站接口回「发送成功」只说明它收下了。新号 / 风控会让评论进审核 ——
    你自己看不见，插件以为发出去了、还记进判重表 ⇒ 下一轮不再发 ⇒ **一条都没到人**。
    更坏的一层：不公开时连通知都不推，于是「@ 了但没人收到」。
    所以发完必须回头核一次。

复核靠两条只读接口（上游实测可行，这里照抄）
    顶层   ``/x/v2/reply/main?type=1&oid=<aid>&mode=2&next=0&ps=20``  按时间，刚发的在第一页
    楼中楼 ``/x/v2/reply/reply?type=1&oid=<aid>&root=<root>&ps=20``   直接按 root 拉那一串
    ✗ ``/x/v2/reply/detail`` 走不通（回 -400），别无谓地换它。

一条最容易犯的错
    **看不到 ≠ 没发出去**。翻页、热度排序、审核延迟都会让「最新一页里没有」。
    所以结论的措辞一律是「没查到」，绝不写成「没发出去」。

风格闸门不在这里
    那道闸在上游 MCP 里（``_style_reason``：开头八股正则 / 跟自己历史相似 ≥0.55 /
    跟该视频热评撞车 ≥0.5），只要不设 ``BILI_STYLE_OFF=1`` 就自带生效。
    网关这边只负责「被拦之后改写一次、再被拦就不发」这个流程。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_TOP = "https://api.bilibili.com/x/v2/reply/main"
API_SUB = "https://api.bilibili.com/x/v2/reply/reply"

# 已确认可见的记录，7 天内不重复查
VERIFY_TTL = 7 * 86400

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
_LONG_NUM_RE = re.compile(r"\b(\d{9,})\b")

# 报告里这一段的标题：重复触发时要先截断旧的，不能越追加越长
REPORT_MARKER = "## 评论复核"


@dataclass(slots=True)
class CommentRecord:
    """要复核的一条：哪条评论（rpid）、挂在哪个视频（bv）下、楼中楼的话根是谁。"""

    bvid: str
    rpid: int
    root: int = 0
    line: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"bvid": self.bvid, "rpid": self.rpid, "root": self.root}


@dataclass(slots=True)
class VerifyResult:
    record: CommentRecord
    visible: bool | None = None
    likes: int | None = None
    how: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"bvid": self.record.bvid, "rpid": self.record.rpid,
                "visible": self.visible, "likes": self.likes,
                "how": self.how, "note": self.note}

    @property
    def label(self) -> str:
        return "公开可见" if self.visible is True else "没查到"


def load_credential(home: Path | str) -> dict[str, str] | None:
    """读家目录里的凭证。没有就返回 None —— 复核必须用你自己的登录态。"""
    path = Path(home) / "bili_credential.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("sessdata"):
        return None
    return {str(k): str(v or "") for k, v in raw.items()}


def parse_ledger(text: str) -> list[CommentRecord]:
    """从评论台账里抠出「我们发过的评论」。

    容忍随手写的行：一行里出现 BV 号 + 至少一个 9 位以上数字就算一条。
    约定第一个长数字是 rpid，第二个（如果有）是 root。
    """
    rows: list[CommentRecord] = []
    for line in str(text or "").splitlines():
        match = _BV_RE.search(line)
        if not match:
            continue
        numbers = [int(n) for n in _LONG_NUM_RE.findall(line)]
        if not numbers:
            continue
        rows.append(CommentRecord(
            bvid=match.group(0),
            rpid=numbers[0],
            root=numbers[1] if len(numbers) > 1 else 0,
            line=line.strip()[:160],
        ))
    return rows


def ledger_records(home: Path | str) -> list[CommentRecord]:
    try:
        return parse_ledger((Path(home) / "comments.md").read_text(encoding="utf-8"))
    except OSError:
        return []


def _headers(cred: dict[str, str]) -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Cookie": f"SESSDATA={cred.get('sessdata', '')}; "
                  f"bili_jct={cred.get('bili_jct', '')}; buvid3={cred.get('buvid3', '')}",
    }


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any],
               headers: dict[str, str]) -> dict[str, Any]:
    try:
        resp = await client.get(url, params=params, headers=headers)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"code": -998, "message": str(exc)[:160]}
    return data if isinstance(data, dict) else {"code": -999, "message": "返回不是对象"}


async def verify_one(record: CommentRecord, *, client: httpx.AsyncClient,
                     headers: dict[str, str],
                     aid_cache: dict[str, int | None] | None = None) -> VerifyResult:
    """核一条。``visible=True`` 才算确认公开可见，其余一律「没查到」。"""
    cache = aid_cache if aid_cache is not None else {}
    if record.bvid not in cache:
        view = await _get(client, API_VIEW, {"bvid": record.bvid}, headers)
        cache[record.bvid] = int(view["data"]["aid"]) if view.get("code") == 0 else None
    aid = cache[record.bvid]
    if not aid:
        return VerifyResult(record, how="取视频信息失败",
                            note="这条跳过（视频可能已删或不可见）")

    if not record.root:
        data = await _get(client, API_TOP,
                          {"type": 1, "oid": aid, "mode": 2, "next": 0, "ps": 20}, headers)
        replies = ((data.get("data") or {}).get("replies") or [])
        hit = next((r for r in replies if int(r.get("rpid") or 0) == record.rpid), None)
        if hit:
            return VerifyResult(record, visible=True, likes=int(hit.get("like") or 0),
                                how="顶层·最新一页",
                                note="（接口标了 invisible）" if hit.get("invisible") else "")
        return VerifyResult(record, how="顶层·最新一页",
                            note="最新一页里没找到 —— 可能还在审核，也可能只是翻页看不到（**不等于没发出去**）")

    data = await _get(client, API_SUB,
                      {"type": 1, "oid": aid, "root": record.root, "ps": 20, "pn": 1}, headers)
    replies = ((data.get("data") or {}).get("replies") or [])
    hit = next((r for r in replies if int(r.get("rpid") or 0) == record.rpid), None)
    how = f"楼中楼·root={record.root}"
    if hit:
        return VerifyResult(record, visible=True, likes=int(hit.get("like") or 0), how=how,
                            note="（接口标了 invisible）" if hit.get("invisible") else "")
    note = (f"那一串里有 {len(replies)} 条，但没有我们这条 —— 可能还在审核（**不等于没发出去**）"
            if replies else "那一串暂时拉不到内容 —— 可能还在审核（**不等于没发出去**）")
    return VerifyResult(record, how=how, note=note)


def _state_path(home: Path | str) -> Path:
    return Path(home) / "verify-state.json"


def _load_state(home: Path | str) -> dict[str, Any]:
    try:
        raw = json.loads(_state_path(home).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(home: Path | str, state: dict[str, Any]) -> None:
    try:
        path = _state_path(home)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # 写不了就算了，复核不该因此失败


@dataclass(slots=True)
class VerifyReport:
    checked: list[VerifyResult] = field(default_factory=list)
    skipped: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"checked": [item.to_dict() for item in self.checked],
                "skipped": self.skipped, "note": self.note}

    def render(self) -> str:
        if not self.checked and not self.note:
            return ""
        lines = ["", REPORT_MARKER, ""]
        for item in self.checked:
            tail = f" · 依据：{item.how}" if item.how else ""
            more = f" · {item.note}" if item.note else ""
            likes = f"（赞 {item.likes or 0}）" if item.visible else ""
            lines.append(f"- `{item.record.bvid}` rpid **{item.record.rpid}** —— "
                         f"{item.label}{likes}{tail}{more}")
        if self.skipped:
            lines.append(f"- （另外 {self.skipped} 条最近已复核过，没重复查）")
        if self.note:
            lines.append(f"- {self.note}")
        return "\n".join(lines)


async def verify_recent(home: Path | str, *, max_items: int = 6,
                        client: httpx.AsyncClient | None = None,
                        pause: float = 0.7) -> VerifyReport:
    """复核最近的一批（默认最多 6 条，7 天内已确认可见的不重复查）。

    没有凭证、没有台账、没发过评论 —— 三种情况都**如实说清楚**，不当成错误。
    """
    records = ledger_records(home)
    if not records:
        return VerifyReport(note="还没发过评论，暂时不用核")
    cred = load_credential(home)
    if cred is None:
        return VerifyReport(note="还没扫码登录，没法复核（复核要用你自己的登录态）")

    state = _load_state(home)
    now = time.time()
    fresh: list[CommentRecord] = []
    skipped = 0
    for record in reversed(records):        # 从最新的往回看
        previous = state.get(str(record.rpid)) or {}
        if previous.get("visible") is True and now - float(previous.get("at") or 0) < VERIFY_TTL:
            skipped += 1
            continue
        fresh.append(record)
        if len(fresh) >= max(1, int(max_items)):
            break
    if not fresh:
        return VerifyReport(skipped=skipped, note=f"都复核过了（跳过旧记录 {skipped} 条）")

    headers = _headers(cred)
    aid_cache: dict[str, int | None] = {}
    checked: list[VerifyResult] = []
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    try:
        for index, record in enumerate(fresh):
            result = await verify_one(record, client=http, headers=headers, aid_cache=aid_cache)
            checked.append(result)
            state[str(record.rpid)] = {
                "visible": result.visible, "at": now, "how": result.how,
                "bvid": record.bvid, "likes": result.likes,
            }
            log.info("复核 %s rpid=%s → %s（%s）", record.bvid, record.rpid,
                     result.label, result.how)
            if index + 1 < len(fresh) and pause:
                await _sleep(pause)
    finally:
        if own_client:
            await http.aclose()
    _save_state(home, state)
    return VerifyReport(checked=checked, skipped=skipped)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def append_to_report(home: Path | str, text: str) -> Path | None:
    """把复核结果追加到当天报告末尾。

    两个讲究：
      * 报告不存在就**不硬造一份**（那会凭空多出一份空的日报）；
      * 一轮里可能被触发两次（收尾自动核 + 手动点），所以要先把旧的 ``## 评论复核``
        段截掉再写，否则越叠越长（上游 #135）。
    """
    if not str(text or "").strip():
        return None
    from . import duty

    path = duty.report_path(home)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return None
    at = current.find(REPORT_MARKER)
    if at >= 0:
        current = current[:at].rstrip() + "\n"
    merged = current.rstrip() + "\n" + text.strip() + "\n"
    try:
        path.write_text(merged, encoding="utf-8")
    except OSError:
        return None
    return path
