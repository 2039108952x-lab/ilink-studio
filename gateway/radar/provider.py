"""内容雷达的契约层：只约定「怎么拿候选、怎么发」，不认任何平台细节。

数据结构以 ``radar/models.py`` 为唯一来源（``Candidate`` / ``CommentSnapshot`` /
``PickDecision`` / ``PickReport``），这里只做两件事：

1. 定义 ``RadarProvider`` 抽象、``RadarError``、``ShareDraft`` 这几个接口件；
2. 提供一组**访问适配函数**（``stat`` / ``interactions`` / ``age_hours``），
   让判断层不必知道 ``stats`` 字典里到底有哪些键。

不变的约定（实现方必须守）
    1. ``confirm=False`` 永远不许真发 —— 只回草稿，绝不因为「反正内容对」就放行。
    2. 失败一律 ``RadarError(code, message)``，不把底层异常直接抛给上层：
       ``not_logged_in`` / ``deps_missing`` / ``rate_limited`` / ``invalid_args`` /
       ``tool_failed`` / ``timeout`` / ``crashed``。上层按 code 决定冷却还是等本人点头。
    3. ``Candidate.url`` 是能直接打开的链接；带签名的分享链放 ``extra``，别混用。
    4. 实现方自己解决鉴权与限速，不把 cookie / token 塞进返回结构。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .models import Candidate, CommentSnapshot

# 老名字：判断层内部一直叫 CommentSample，语义相同
CommentSample = CommentSnapshot

__all__ = [
    "Candidate", "CommentSnapshot", "CommentSample", "Identity", "RadarError",
    "RadarProvider", "SendOutcome", "ShareDraft",
    "stat", "interactions", "age_hours", "title_of",
]


class RadarError(RuntimeError):
    """雷达通道的业务失败。``code`` 给界面和冷却逻辑用。

    兼容两种叫法：
        ``RadarError("拉评论失败")``            —— 只有一个 message（老用法）
        ``RadarError("tool_failed", "拉评论失败")`` —— code + message
    """

    def __init__(self, code: str = "failed", message: str = "", *,
                 retryable: bool = False, payload: dict[str, Any] | None = None) -> None:
        if not message:
            message, code = code, "failed"
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.payload = payload or {}

    @property
    def safe_message(self) -> str:
        """给界面用的版本。与 adapters 那边的老叫法保持一致。"""
        return self.message


@dataclass(slots=True)
class ShareDraft:
    """一条分享草稿。``sent`` 才是「真发出去了」，``preview`` 是给人看的那句话。"""

    item_id: str
    to: str = ""
    reason: str = ""
    preview: str = ""
    sent: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "to": self.to, "reason": self.reason,
                "preview": self.preview, "sent": self.sent, "detail": self.detail}


@dataclass(slots=True)
class Identity:
    """当前登录的是谁。判断「能不能干活」的第一步。"""

    ok: bool
    logged_in: bool = False
    nick: str = ""
    uid: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "logged_in": self.logged_in, "nick": self.nick,
                "uid": self.uid, "detail": self.detail}


@dataclass(slots=True)
class SendOutcome:
    """发送结果。``sent`` 才是「真发出去了」，``ok`` 只表示调用没炸。"""

    ok: bool
    sent: bool = False
    needs_confirm: bool = False
    draft: str = ""
    code: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "sent": self.sent, "needs_confirm": self.needs_confirm,
                "draft": self.draft, "code": self.code, "error": self.error}


# ---------------------------------------------------------------- 数据访问适配

# 各平台 stats 的键名不一样，判断层只用这几个语义键
_STAT_ALIASES = {
    "like": ("like", "digg", "digg_count", "likes"),
    "comment": ("comment", "comment_count", "reply"),
    "collect": ("collect", "favorite", "fav"),
    "share": ("share", "share_count", "forward"),
}


def stat(c: Candidate, key: str) -> int:
    """按语义取互动数，认 ``like/comment/collect/share`` 四种 key 的常见别名。"""
    stats = getattr(c, "stats", None) or {}
    for alias in _STAT_ALIASES.get(key, (key,)):
        if alias in stats:
            try:
                return int(stats[alias] or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def interactions(c: Candidate) -> int:
    """互动四件套之和。硬筛与比值打分都基于它。"""
    return sum(stat(c, k) for k in ("like", "comment", "collect", "share"))


def age_hours(c: Candidate) -> float:
    """发布距今多少小时。优先用平台直接给的 ``extra.age_h``，否则由时间戳推。"""
    extra = getattr(c, "extra", None) or {}
    raw = extra.get("age_h")
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    published = getattr(c, "published_at", None)
    if isinstance(published, (int, float)) and published > 0:
        import time
        return max(0.0, (time.time() - float(published)) / 3600.0)
    return 0.0


def title_of(c: Candidate) -> str:
    return str(getattr(c, "title", "") or "").strip()


def duration_of(c: Candidate) -> float:
    raw = getattr(c, "duration_sec", None)
    return float(raw) if isinstance(raw, (int, float)) else 0.0


# ---------------------------------------------------------------- 抽象

class RadarProvider(ABC):
    """一个平台的内容通道。

    抖音那边的 ``radar/douyin.py`` 是鸭子类型实现（不强制继承本类），
    只要方法签名对得上就能用；继承本类只是为了在测试里拿到默认行为。
    """

    name: str = ""
    supports_share: bool = False
    supports_text: bool = False
    supports_comment: bool = False

    @abstractmethod
    async def whoami(self) -> Identity:
        """登录态还在不在、当前是哪个号。只读。"""

    @abstractmethod
    async def candidates(self, *, top: int = 8) -> list[Candidate]:
        """拉一批候选。只读，不改平台侧任何状态。"""

    async def comments(self, item_id: str, *, limit: int = 10) -> list[CommentSnapshot]:
        """取一条内容的高赞评论。默认不支持，支持就声明 ``supports_comment=True``。"""
        raise RadarError("unsupported", f"{self.name} 不支持读评论")

    @abstractmethod
    async def send_share(self, draft: ShareDraft, *, confirm: bool = False) -> ShareDraft:
        """把一条内容以卡片形式分享给某人。``confirm=False`` 只回草稿。"""

    @abstractmethod
    async def send_text(self, to: str, text: str, *, confirm: bool = False) -> dict:
        """发一条纯文本。``confirm=False`` 只回草稿。"""

    async def close(self) -> None:
        """释放子进程/连接。默认无事可做。"""
        return None
