"""内容雷达的通用数据结构。

平台差异全部收敛在 ``RadarProvider`` 后面；这里的结构对抖音/B站一视同仁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Candidate:
    """一条待判断的内容（抖音的 aweme / B站的视频）。"""

    platform: str
    item_id: str
    title: str                      # 文案 / 简介，判断的主依据
    author: str = ""
    duration_sec: int | None = None
    url: str = ""                   # 官方分享链接，可以直接给人点
    published_at: float | None = None
    stats: dict[str, int] = field(default_factory=dict)   # like/comment/collect/share/play
    extra: dict[str, Any] = field(default_factory=dict)   # 平台原样字段，不参与判断

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform, "item_id": self.item_id, "title": self.title,
            "author": self.author, "duration_sec": self.duration_sec, "url": self.url,
            "published_at": self.published_at, "stats": dict(self.stats),
        }


@dataclass(slots=True)
class CommentSnapshot:
    """高赞评论：判断「值不值得点开」的主要文本证据。"""

    text: str
    likes: int = 0


@dataclass(slots=True)
class PickDecision:
    candidate: Candidate
    verdict: str                    # recommend | mention | unsure
    reason: str = ""                # 为什么值得看（必须引用具体证据）
    one_line: str = ""              # 一句话概括
    evidence: list[str] = field(default_factory=list)
    ratios: dict[str, float] = field(default_factory=dict)
    comments: list[CommentSnapshot] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.as_dict(), "verdict": self.verdict,
            "reason": self.reason, "one_line": self.one_line,
            "evidence": list(self.evidence), "ratios": dict(self.ratios),
            "comments": [{"text": c.text, "likes": c.likes} for c in self.comments],
        }


@dataclass(slots=True)
class PickReport:
    date: str
    considered: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    recommended: list[PickDecision] = field(default_factory=list)
    mentions: list[PickDecision] = field(default_factory=list)
    unsure: list[PickDecision] = field(default_factory=list)
    error: str = ""

    @property
    def picked(self) -> int:
        return len(self.recommended)

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date, "considered": self.considered, "dropped": dict(self.dropped),
            "picked": self.picked,
            "recommended": [d.as_dict() for d in self.recommended],
            "mentions": [d.as_dict() for d in self.mentions],
            "unsure": [d.as_dict() for d in self.unsure],
            "error": self.error,
        }
