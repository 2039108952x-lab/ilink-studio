"""日报：一轮的结果落成 Markdown，界面、通知、导出都读同一份。

为什么只落一份
    界面上「刷抖音」区展示的是它、通知里摘的是它、出问题时贴给人看的还是它。
    多存一份就多一处会不一致的地方 —— 之前那版适配器就是吃了这个亏。

写得实一点
    丢掉多少、为什么丢、哪几条没把握，全都要写进日报。
    使用者看不到「为什么只剩三条」，只会觉得这功能没用。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .pipeline import Pick, RoundResult
from .provider import duration_of, stat, title_of

DROP_LABELS = {
    "no_desc": "没文案",
    "too_short": "视频太短",
    "too_quiet": "互动太少",
    "only_likes": "只有赞没人聊",
}


@dataclass(slots=True)
class QuotaState:
    """今天的配额。跨天由调用方负责归零，这里只算数。"""

    per_day: int = 5
    used: int = 0

    @property
    def left(self) -> int:
        return max(0, self.per_day - self.used)

    def to_dict(self) -> dict[str, int]:
        return {"per_day": self.per_day, "used": self.used, "left": self.left}


class QuotaTracker:
    """每天额度的账本：跨天自动归零，状态落一个小 json。

    为什么记账要落盘而不是放内存
        网关会重启（更新、崩溃、用户手动关掉），重启就忘掉「今天已经推过 3 条」
        等于没有配额。落盘才拦得住。
    """

    def __init__(self, state_path: Path, *, per_day: int = 5,
                 clock=time.time) -> None:
        self._path = Path(state_path)
        self._per_day = max(1, int(per_day))
        self._clock = clock

    def _load(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, day: str, used: int) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps({"day": day, "used": used}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def state(self) -> QuotaState:
        """今天的额度状态。昨天的记录在这里被自然忽略，等于归零。"""
        day = date.fromtimestamp(self._clock()).isoformat()
        raw = self._load()
        used = int(raw.get("used") or 0) if raw.get("day") == day else 0
        return QuotaState(per_day=self._per_day, used=max(0, used))

    def consume(self, n: int = 1) -> QuotaState:
        """记上 n 条。超过上限就顶在上限，不会记出负数余额。"""
        day = date.fromtimestamp(self._clock()).isoformat()
        used = min(self._per_day, self.state().used + max(0, int(n)))
        self._save(day, used)
        return QuotaState(per_day=self._per_day, used=used)

    def reset(self) -> QuotaState:
        day = date.fromtimestamp(self._clock()).isoformat()
        self._save(day, 0)
        return QuotaState(per_day=self._per_day, used=0)


def _stamp(day: date | None = None) -> str:
    return (day or date.today()).isoformat()


def report_path(data_dir: Path, day: date | None = None) -> Path:
    return Path(data_dir) / "reports" / f"{_stamp(day)}.md"


def _num(n: float) -> str:
    if n >= 1e8:
        return f"{n / 1e8:.1f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return str(int(n))


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _render_pick(index: int, pick: Pick) -> list[str]:
    c = pick.candidate
    j = pick.judgment
    first_line = (title_of(c).splitlines() or [""])[0][:80]
    lines = [f"### {index}. {first_line or '（无文案）'}", ""]
    meta = []
    if c.author:
        meta.append(c.author)
    dur = duration_of(c)
    if dur:
        meta.append(f"{dur:.1f}s")
    meta.append(f"赞 {_num(stat(c, 'like'))} / 评 {_num(stat(c, 'comment'))}"
                f" / 藏 {_num(stat(c, 'collect'))} / 转 {_num(stat(c, 'share'))}")
    lines.append("- " + " · ".join(meta))
    if j and j.reason:
        lines.append(f"- 为什么：{j.reason}")
    if j and j.tags:
        lines.append(f"- 标签：{' / '.join(j.tags)}")
    if c.url:
        lines.append(f"- 打开：{c.url}")
    top = sorted(pick.comments, key=lambda x: getattr(x, "likes", 0), reverse=True)[:2]
    if top:
        lines.append("- 评论里最有用的两条：")
        for cm in top:
            text = (getattr(cm, "text", "") or "").strip().splitlines()
            lines.append(f"  > 赞 {_num(getattr(cm, 'likes', 0))}：{(text[0] if text else '')[:120]}")
    lines.append("")
    return lines


def render(result: RoundResult, *, day: date | None = None,
           quota: QuotaState | None = None, platform_label: str = "抖音") -> str:
    """把一轮结果渲染成 Markdown。没有模型参与，纯字符串拼装，可复现。"""
    day = day or date.today()
    dropped_total = sum(result.dropped.values())
    lines = [
        f"# 刷{platform_label} · {day.isoformat()}",
        "",
        f"> {_clock(result.started_at)} 开始，{result.elapsed:.0f} 秒跑完。"
        f"看了 {result.seen} 条，硬筛掉 {dropped_total} 条，"
        f"推 {len(result.picked)} 条，另有 {len(result.uncertain)} 条判断不了。",
    ]
    if quota is not None:
        lines.append(f"> 今天的额度：已用 {quota.used}/{quota.per_day}，剩 {quota.left}。")
    if result.note:
        lines.append(f"> 备注：{result.note}")
    lines.append("")

    lines += ["## 值得看", ""]
    if result.picked:
        for i, pick in enumerate(result.picked, 1):
            lines += _render_pick(i, pick)
    else:
        lines += ["（这一轮没有够格的。宁可空着，也不拿编出来的理由凑数。）", ""]

    lines += ["## 判断不了（证据不够，给你自己扫一眼）", ""]
    if result.uncertain:
        for i, pick in enumerate(result.uncertain, 1):
            c = pick.candidate
            why = (pick.judgment.error if pick.judgment else "") or "没有判断结果"
            first = (title_of(c).splitlines() or [""])[0][:60]
            lines.append(f"{i}. **{first or '（无文案）'}**"
                         f" · {c.author or '未知'} · 卡在：{why}")
            if c.url:
                lines.append(f"   - {c.url}")
        lines.append("")
    else:
        lines += ["（没有。）", ""]

    if result.dropped:
        lines += ["## 今天丢掉的", "", "| 原因 | 条数 |", "| --- | --- |"]
        for key, count in result.dropped.items():
            lines.append(f"| {DROP_LABELS.get(key, key)} | {count} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write(result: RoundResult, data_dir: Path, *, day: date | None = None,
          quota: QuotaState | None = None, platform_label: str = "抖音") -> Path:
    """原子写：先落 .tmp 再 replace，半截文件不会被界面读到。"""
    path = report_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render(result, day=day, quota=quota, platform_label=platform_label)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(data_dir: Path, day: date | None = None) -> str:
    """读当天日报；没有就返回空串，不抛异常（界面开局就会调它）。"""
    path = report_path(data_dir, day)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def list_reports(data_dir: Path, *, limit: int = 30) -> list[dict[str, object]]:
    """最近的日报清单，给界面列表用。"""
    folder = Path(data_dir) / "reports"
    if not folder.is_dir():
        return []
    items: list[dict[str, object]] = []
    for path in sorted(folder.glob("*.md"), reverse=True)[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({"day": path.stem, "path": str(path),
                      "bytes": stat.st_size, "updated_at": stat.st_mtime})
    return items


def today_stamp() -> str:
    return _stamp()


def now_seconds() -> float:
    return time.time()
