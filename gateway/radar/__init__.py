"""内容雷达：把「刷一圈平台内容 → 筛 → 写日报」抽成与平台无关的一层。

契约的唯一来源是 ``radar/provider.py``：``Candidate`` / ``CommentSnapshot`` 由 ``models``
提供，``provider`` 负责抽象、失败码与访问适配；筛选与判断在 ``pipeline.py``。
平台实现：
    douyin.py    走 MCP 子进程（dsh-douyin-scroll）
    bilibili.py  走官方接口（当前是 NotImplemented 骨架）

pipeline / report / notify / scheduler 只认这里的东西，界面也只从 ``gateway.radar`` 取。
"""

from .models import Candidate, CommentSnapshot
from .pipeline import (
    Judgment,
    Pick,
    RadarPipeline,
    RoundResult,
    Thresholds,
    build_prompt,
    hard_filter,
    parse_judgment,
    rank,
    talk_ratio,
)
from .provider import (
    Identity,
    RadarError,
    RadarProvider,
    SendOutcome,
    ShareDraft,
    age_hours,
    duration_of,
    interactions,
    stat,
    title_of,
)

# 判断层内部一直叫 CommentSample，语义相同，保留别名免得两边写法打架
CommentSample = CommentSnapshot

__all__ = [
    # 数据结构
    "Candidate",
    "CommentSnapshot",
    "CommentSample",
    "Judgment",
    "Pick",
    "RoundResult",
    # 判断与筛选
    "Thresholds",
    "RadarPipeline",
    "build_prompt",
    "hard_filter",
    "parse_judgment",
    "rank",
    "talk_ratio",
    # 通道契约
    "Identity",
    "RadarError",
    "RadarProvider",
    "SendOutcome",
    "ShareDraft",
    "age_hours",
    "duration_of",
    "interactions",
    "stat",
    "title_of",
]
