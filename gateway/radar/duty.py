"""B 站值班层的落盘部分：日报、判重表、月度归档、单条笔记、配额对账。

为什么这些要单独一层
    上游把这些能力做成了 ``duty_report_append`` / ``duty_seen_append`` 这类工具，
    让模型自己调。我们换成「网关写、模型只说判断」，但**产物格式必须一字不差**：
    上游宿主靠数 ``### BV号`` 开头的行来对账「今天挑了几条」，格式一歪配额就失准。

三条格式红线（改动前先读章程）
    1. 每条挑中的视频单独一段，**段首第一行固定 ``### BV号``**（顶格、不带序号）；
    2. 段内必须有 ``**档位**：<值得看|可以看|不值当>``，只能这三个词，不许编、不许写分数；
    3. 没读过字幕或看过画面，最多写「可以看」—— 用标题猜内容写「值得看」是最不该犯的错。

判重表为什么必须落盘、且读不出来时不许当「没记录过」
    把它当成空的，同一批视频会被反复推、同一批人会被重复打扰。
    章程里这条是「宁可少发」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .pipeline import Pick, RoundResult
from .provider import duration_of, stat, title_of

log = logging.getLogger(__name__)

# 档位只能是这三个词（章程钉死的，界面按它筛选）
GRADES = ("值得看", "可以看", "不值当")

# 判重表的一行：状态只有这三种
SEEN_STATUSES = ("挑了", "跳过", "评论")

# 报告里「挑中了几条」就数这行：``### BV1xxxxxxxxx``
_PICK_HEAD_RE = re.compile(r"^###\s+(BV[0-9A-Za-z]{10})\s*$", re.M)
# 判重表的分隔符用中点，跟人对齐好读；解析时容忍空格变体
_SEEN_SPLIT = re.compile(r"\s*[·|]\s*")

_DROP_LABELS = {
    "no_desc": "没文案",
    "too_short": "视频太短",
    "too_quiet": "互动太少",
    "only_likes": "只有赞没人聊",
}


def _num(n: float) -> str:
    if n >= 1e8:
        return f"{n / 1e8:.1f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return str(int(n))


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# ---------------------------------------------------------------- 路径


def paths_of(home: Path | str) -> dict[str, str]:
    """``duty_paths`` 的等价物：把这一亩三分地的位置一次说清。"""
    root = Path(home)
    return {
        "home": str(root),
        "report_dir": str(root / "out"),
        "report_today": str(root / "out" / f"{date.today().isoformat()}.md"),
        "seen": str(root / "seen.md"),
        "digest_dir": str(root / "digest"),
        "digest_this_month": str(root / "digest" / f"{date.today().strftime('%Y-%m')}.md"),
        "notes_dir": str(root / "reports"),
        "comments": str(root / "comments.md"),
        "tmp": str(root / "tmp"),
    }


def _ensure(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _append(path: Path, text: str) -> bool:
    """追加写。失败返回 False，调用方必须把这件事写进报告，不许静默丢。"""
    try:
        _ensure(path)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return True
    except OSError as exc:
        log.warning("写不进去：%s：%s", path, exc)
        return False


# ---------------------------------------------------------------- 档位


def grade_of(pick: Pick) -> str:
    """把一条判断折成档位三词之一。判不出来返回空串（那就不进报告正文）。

    「值得看」硬要求**读过字幕**：
    手里只有标题/时长/简介这类表层信息时，最多「可以看」。这条是章程里
    最不该犯的错，所以写成代码而不是提示词。
    """
    judgment = pick.judgment
    if judgment is None or judgment.worth is None:
        return ""
    if not judgment.worth:
        return "不值当"
    read_subtitle = str(pick.evidence.get("source") or "") == "subtitle"
    saw_frames = bool(pick.evidence.get("frames_seen"))
    if (read_subtitle or saw_frames) and judgment.confidence >= 0.75:
        return "值得看"
    return "可以看"


def one_line_of(pick: Pick) -> str:
    """「它自己的一句话」。模型没给就空着 —— 不拿理由硬凑一句。"""
    return str(getattr(pick.judgment, "one_line", "") or "").strip()


# ---------------------------------------------------------------- 日报


def render_pick(pick: Pick, *, index: int = 0) -> list[str]:
    """一条挑中的视频 → Markdown 段。**第一行必须是 ``### BV号``**。"""
    c = pick.candidate
    grade = grade_of(pick) or "可以看"
    play = stat(c, "play")
    lines = [f"### {c.item_id}", ""]
    lines.append(f"- 标题：{title_of(c) or '（无标题）'}")
    if c.author:
        lines.append(f"- UP：{c.author}")
    if c.url:
        lines.append(f"- 链接：{c.url}")
    meta = []
    if play:
        meta.append(f"播放 {_num(play)}")
    dur = duration_of(c)
    if dur:
        meta.append(f"{dur:.0f} 秒")
    if meta:
        lines.append("- " + " · ".join(meta))
    lines.append(f"- **档位**：{grade}")
    reason = (pick.judgment.reason if pick.judgment else "") or ""
    if reason:
        lines.append(f"- 为什么值得：{reason}")
    sentence = one_line_of(pick)
    if sentence:
        lines.append(f"- 一句话：{sentence}")
    lines.append("")
    return lines


def render_round(result: RoundResult, *, day: date | None = None,
                 quota: dict[str, Any] | None = None,
                 subtitle_skipped: int = 0,
                 dm_note: str = "") -> str:
    """一轮结果 → B 站口径 Markdown。纯拼装，不调模型，可复现。"""
    day = day or date.today()
    dropped_total = sum(result.dropped.values())
    lines = [
        f"# 刷B站 · {day.isoformat()}",
        "",
        f"> {_clock(result.started_at)} 开始，{result.elapsed:.0f} 秒跑完。"
        f"看了 {result.seen} 条，硬筛掉 {dropped_total} 条，"
        f"挑中 {len(result.picked)} 条，另有 {len(result.uncertain)} 条判断不了。",
    ]
    if quota:
        lines.append(f"> 今天额度：已用 {quota.get('used', 0)}/{quota.get('per_day', 0)}，"
                     f"剩 {quota.get('left', 0)}。")
    if result.note:
        lines.append(f"> 备注：{result.note}")
    lines.append("")

    lines += ["## 值得看", ""]
    if result.picked:
        for index, pick in enumerate(result.picked, 1):
            lines += render_pick(pick, index=index)
    else:
        lines += ["（这一轮没有够格的。宁可空着，也不拿编出来的理由凑数。）", ""]

    if result.dropped:
        lines += ["## 这一轮丢掉的", "", "| 原因 | 条数 |", "| --- | --- |"]
        for key, count in result.dropped.items():
            lines.append(f"| {_DROP_LABELS.get(key, key)} | {count} |")
        lines.append("")

    # 章程要求这一行永远写，N=0 也写：让人一眼看出这轮有没有漏
    lines.append(f"因无字幕跳过 {subtitle_skipped} 条")
    if dm_note:
        lines.append(dm_note)
    return "\n".join(lines).rstrip() + "\n"


def report_path(home: Path | str, day: date | None = None) -> Path:
    return Path(home) / "out" / f"{(day or date.today()).isoformat()}.md"


def append_report(home: Path | str, text: str, *, day: date | None = None) -> bool:
    """追加到当天日报。

    **追加**不覆盖：章程写明「一天只有这一份」，覆盖了配额就数不准
    （要么白跑、要么超量）。
    """
    return _append(report_path(home, day), text)


def write_report(home: Path | str, text: str, *, day: date | None = None) -> Path:
    """整份覆盖写（只在「今天第一次」时用）。原子：先 .tmp 再 replace。"""
    path = report_path(home, day)
    _ensure(path)
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("日报写不进去：%s：%s", path, exc)
    return path


def read_report(home: Path | str, day: date | None = None) -> str:
    try:
        return report_path(home, day).read_text(encoding="utf-8")
    except OSError:
        return ""


def count_picked(markdown: str) -> int:
    """数 ``### BV号`` 行。宿主靠它对账「今天挑了几条」，所以别数别的标题。"""
    return len(_PICK_HEAD_RE.findall(markdown or ""))


def read_picked(home: Path | str, day: date | None = None) -> int:
    return count_picked(read_report(home, day))


# 段内字段行：``- 标题：xxx`` / ``- **档位**：值得看``
_FIELD_RE = re.compile(r"^-\s+\*{0,2}([^：:]{1,12})\*{0,2}[：:]\s*(.*)$")


def parse_report(markdown: str) -> list[dict[str, Any]]:
    """把日报里的 ``### BV号`` 段反解成条目，给「它刷过的视频」列表用。

    报告是唯一事实来源 —— 界面上的档位、理由、一句话都从这里读，
    不另存一份数据库（两份必然对不上，上游也吃过这个亏）。
    """
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.rstrip()
        head = _PICK_HEAD_RE.match(line.strip()) if line.startswith("###") else None
        if head:
            if current is not None:
                items.append(current)
            current = {"bvid": head.group(1), "title": "", "up": "", "url": "",
                       "play": "", "duration": "", "grade": "", "reason": "",
                       "one_line": ""}
            continue
        if current is None:
            continue
        if line.startswith("## "):        # 进入下一节，当前条结束
            items.append(current)
            current = None
            continue
        match = _FIELD_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key == "标题":
            current["title"] = value
        elif key == "UP":
            current["up"] = value
        elif key == "链接":
            current["url"] = value
        elif key == "档位":
            current["grade"] = value
        elif key == "为什么值得":
            current["reason"] = value
        elif key == "一句话":
            current["one_line"] = value
        elif key.startswith("播放"):
            current["play"] = key
            current["duration"] = value
    if current is not None:
        items.append(current)
    return items


def list_videos(home: Path | str, *, grade: str = "", status: str = "",
                page: int = 1, size: int = 20, days: int = 14) -> dict[str, Any]:
    """「它刷过的视频」：跨最近若干天的日报汇总，按档位/状态筛，再分页。

    ``status`` 取判重表里的状态（挑了 / 跳过 / 评论）；没记过就留空。
    排序按日期倒序 —— 使用者想看的是「最近刷到了什么」。
    """
    root = Path(home)
    rows: list[dict[str, Any]] = []
    seen_status: dict[str, str] = {}
    for row in seen_recent(home, limit=5000):
        seen_status.setdefault(row.bvid, row.status)

    try:
        files = sorted((root / "out").glob("*.md"), reverse=True)[: max(1, days)]
    except OSError:
        files = []
    for path in files:
        day = _day_of(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for item in parse_report(text):
            item["day"] = day
            item["status"] = seen_status.get(item["bvid"], "")
            rows.append(item)

    if grade:
        rows = [r for r in rows if r.get("grade") == grade]
    if status:
        rows = [r for r in rows if r.get("status") == status]

    total = len(rows)
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 20), 100))
    start = (page - 1) * size
    return {
        "items": rows[start:start + size],
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size,
        "grade": grade,
        "status": status,
        "grades": list(GRADES),
        "statuses": list(SEEN_STATUSES),
    }


def _day_of(path: Path) -> str:
    stem = path.stem
    try:
        return date.fromisoformat(stem).isoformat()
    except ValueError:
        return stem


# ---------------------------------------------------------------- 判重表


def seen_append(home: Path | str, *, bvid: str, title: str = "", up: str = "",
                status: str = "跳过", when: date | None = None) -> bool:
    """往判重表追一行。**任何一档都要写** —— 它是「看过什么」的账。"""
    day = (when or date.today()).isoformat()
    clean = " ".join(str(title or "").split())[:120]
    fields = [day, str(bvid or "").strip(), clean, str(up or "").strip(),
              status if status in SEEN_STATUSES else "跳过"]
    return _append(Path(home) / "seen.md", " · ".join(fields) + "\n")


@dataclass(slots=True)
class SeenRow:
    day: str
    bvid: str
    title: str = ""
    up: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"day": self.day, "bvid": self.bvid, "title": self.title,
                "up": self.up, "status": self.status}


def seen_recent(home: Path | str, *, limit: int = 40, bvid: str = "") -> list[SeenRow]:
    """读判重表最近的若干行。

    ⚠️ **文件不存在时返回空列表，但那不等于「没记录过」** —— 调用方必须自己
    区分「表是空的」和「表读不出来」，后者要按章程**这一轮不发评论**。
    """
    path = Path(home) / "seen.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[SeenRow] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _SEEN_SPLIT.split(line)
        if len(parts) < 2:
            continue
        row = SeenRow(day=parts[0], bvid=parts[1],
                      title=parts[2] if len(parts) > 2 else "",
                      up=parts[3] if len(parts) > 3 else "",
                      status=parts[4] if len(parts) > 4 else "")
        if bvid and row.bvid != bvid:
            continue
        rows.append(row)
    rows.reverse()
    return rows[: max(1, int(limit))]


def seen_exists(home: Path | str) -> bool:
    """判重表到底存不存在。用来区分「空的」与「读不出来」。"""
    return (Path(home) / "seen.md").is_file()


def seen_bvids(home: Path | str) -> set[str]:
    """已经跑过的 BV 号集合，用来做本轮判重。"""
    return {row.bvid for row in seen_recent(home, limit=5000) if row.bvid}


def seen_day(raw: Any, now: date | None = None) -> str | None:
    """判重表里的日期格 → ``YYYY-MM-DD``；认不出来返回 None。

    宽容解析：模型会写 ``10-23`` / ``2026/10/23`` / 带时间的。
    没写年份就按今年补；补出来比今天还晚（一月读到 ``12-31``）⇒ 算去年。
    """
    text = str(raw or "").strip()
    match = re.match(r"^(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?$",
                     text)
    if not match:
        return None
    today = now or date.today()
    month, day = int(match.group(2)), int(match.group(3))
    year = int(match.group(1)) if match.group(1) else today.year

    def valid(y: int) -> bool:
        try:
            date(y, month, day)
        except ValueError:
            return False
        return True

    if not valid(year):
        return None
    if not match.group(1):
        candidate = date(year, month, day)
        if candidate > today:
            year -= 1
            if not valid(year):
                return None
    return date(year, month, day).isoformat()


# ---------------------------------------------------------------- 归档与笔记


def digest_append(home: Path | str, text: str, *, when: date | None = None) -> bool:
    """追加到本月归档 ``digest/<YYYY-MM>.md``。报告给人看，归档给「以后回头翻」。"""
    month = (when or date.today()).strftime("%Y-%m")
    return _append(Path(home) / "digest" / f"{month}.md", text)


def digest_line(candidate: Any, *, conclusion: str = "", status: str = "") -> str:
    """归档的一行：``日期 · BV 号 · 标题 · UP · 一句话结论 · 链接``。"""
    bits = [
        date.today().isoformat(),
        str(getattr(candidate, "item_id", "") or ""),
        " ".join(str(getattr(candidate, "title", "") or "").split())[:100],
        str(getattr(candidate, "author", "") or ""),
        " ".join(str(conclusion or "").split())[:120] or (status or ""),
        str(getattr(candidate, "url", "") or ""),
    ]
    return " · ".join(bits) + "\n"


def note_write(home: Path | str, *, bvid: str, text: str,
               when: date | None = None) -> Path:
    """单条笔记 ``reports/<BV>-<日期>.md``，方便单条收藏 / 丢进 Obsidian。"""
    stamp = (when or date.today()).isoformat()
    path = Path(home) / "reports" / f"{bvid}-{stamp}.md"
    _ensure(path)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.warning("笔记写不进去：%s：%s", path, exc)
    return path


def note_text(pick: Pick) -> str:
    c = pick.candidate
    lines = [f"# {title_of(c) or c.item_id}", ""]
    lines.append(f"- BV：{c.item_id}")
    if c.author:
        lines.append(f"- UP：{c.author}")
    if c.url:
        lines.append(f"- 链接：{c.url}")
    lines.append(f"- 档位：{grade_of(pick) or '未定'}")
    if pick.judgment and pick.judgment.reason:
        lines += ["", "## 为什么值得", "", pick.judgment.reason]
    sentence = one_line_of(pick)
    if sentence:
        lines += ["", "## 一句话", "", sentence]
    subtitle = str(pick.evidence.get("subtitle") or "").strip()
    if subtitle:
        lines += ["", "## 字幕摘录", "", subtitle[:2000]]
    top = sorted(pick.comments, key=lambda x: x.likes, reverse=True)[:3]
    if top:
        lines += ["", "## 热评", ""]
        for cm in top:
            lines.append(f"- 赞 {_num(cm.likes)}：{cm.text[:160]}")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 评论台账


def comment_ledger(home: Path | str, *, bvid: str, rpid: Any, text: str,
                   root: Any = None, when: date | None = None) -> bool:
    """记「我们发过哪条评论」：BV + rpid + 时间 + 正文前 20 字。

    rpid 是从 ``bili_reply`` 的返回里取的，**取不到就写「未取到 rpid」**，
    绝不自己编一个数字 —— 宿主复核认的就是这个号，编了会核到别人身上。
    """
    stamp = (when or date.today()).isoformat()
    rpid_text = str(rpid) if str(rpid or "").strip() not in ("", "None", "0") else "未取到 rpid"
    root_text = ""
    if str(root or "").strip() not in ("", "None", "0"):
        root_text = f" · root {root}"
    body = " ".join(str(text or "").split())[:20]
    line = f"{stamp} · {bvid} · rpid {rpid_text}{root_text} · {body}\n"
    return _append(Path(home) / "comments.md", line)


def recent_comments(home: Path | str, *, days: int = 7, limit: int = 8) -> list[str]:
    """最近 N 天自己发过的评论。回复评论前要先看这个（漏一条那个人就永远收不到回复）。"""
    path = Path(home) / "comments.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    cutoff = date.today().toordinal() - max(1, int(days))
    kept: list[str] = []
    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        stamp = seen_day(text.split(" · ")[0])
        if stamp and date.fromisoformat(stamp).toordinal() < cutoff:
            continue
        kept.append(text)
        if len(kept) >= limit:
            break
    return kept


# ---------------------------------------------------------------- 配额对账


def quota_state(home: Path | str, *, per_day: int, when: date | None = None) -> dict[str, Any]:
    """今天的额度。``used`` 直接数日报里的 ``### BV号`` —— 跟章程同一个口径。

    为什么不用内存计数：网关会重启，重启就忘掉「今天已经推过 3 条」。
    为什么不用 QuotaTracker：日报是唯一事实来源，两边各记一份必然对不上。
    """
    used = read_picked(home, when)
    cap = max(0, int(per_day))
    return {"per_day": cap, "used": used, "left": max(0, cap - used) if cap else 0,
            "unlimited": cap == 0}


def quota_json(home: Path | str, *, per_day: int, when: date | None = None) -> str:
    return json.dumps(quota_state(home, per_day=per_day, when=when), ensure_ascii=False)


def today_stamp(now: float | None = None) -> str:
    return date.fromtimestamp(now if now is not None else time.time()).isoformat()
