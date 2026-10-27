"""内容雷达的判断流水线：硬筛 → 互动比值 → 语义判断 → 证据够的才推。

三段各管一段，谁也别越权
    硬筛   不用模型就能否掉的（没文案、视频太短、互动太少）。跑得便宜、结论确定。
    比值   不看「好不好」，只看「是不是只有赞、没人说话」。压掉纯流量号。
    语义   模型读文案 + 高赞评论，回**严格 JSON**。解析不出来就当「判断不了」，绝不猜。

为什么要有「证据不足」这一档
    推荐流的候选经常是：文案只有几个话题标签、评论区全是「留个印记」。
    这种内容模型也能编出一段理由，但那是编的。所以宁可进 uncertain，
    让人自己扫一眼，也不塞进「值得看」里 —— 推荐位的信任只能坏一次。
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .provider import (
    Candidate,
    CommentSample,
    RadarError,
    RadarProvider,
    age_hours,
    duration_of,
    interactions,
    stat,
    title_of,
)

# 从模型输出里抠 JSON：优先整段，其次代码围栏，最后第一对花括号
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


@dataclass(slots=True)
class Thresholds:
    """全部可配。默认值是照本机实测的推荐流量级定的，不是拍脑袋。"""

    min_duration_s: float = 8.0
    min_interactions: int = 3000
    min_talk_ratio: float = 0.01     # (评+藏+转)/赞 低于此值 = 只有赞没人聊
    max_judge: int = 5               # 最多送几条给模型（省 token 的主要闸门）
    min_confidence: float = 0.6      # 低于此值不进推荐，进「判断不了」
    comments_per_item: int = 8
    max_age_h: float = 720.0         # 超过 30 天的降权到地板


@dataclass(slots=True)
class Judgment:
    """模型对一条候选的结论。``worth is None`` 等于「判断不了」。"""

    item_id: str
    worth: bool | None = None
    confidence: float = 0.0
    reason: str = ""
    tags: list[str] = field(default_factory=list)
    error: str = ""
    # 「它自己的一句话」：报告里单独一行，界面也能直接展示。模型没给就空着，不硬凑。
    one_line: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "worth": self.worth,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "tags": list(self.tags),
            "error": self.error,
            "one_line": self.one_line,
        }


@dataclass(slots=True)
class Pick:
    """一条走到最后的候选：原始数据 + 评论证据 + 模型判断。"""

    candidate: Candidate
    score: float
    comments: list[CommentSample] = field(default_factory=list)
    judgment: Judgment | None = None
    # 平台自带证据（B 站字幕/弹幕）。抖音没有字幕轨，这里一般是空 dict。
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def worth(self) -> bool:
        return bool(self.judgment and self.judgment.worth is True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "score": self.score,
            "comments": [c.to_dict() for c in self.comments],
            "judgment": self.judgment.to_dict() if self.judgment else None,
            "evidence": {
                "source": self.evidence.get("source", ""),
                "segments": self.evidence.get("segments", 0),
                "has_subtitle": bool(self.evidence.get("subtitle")),
            },
        }


@dataclass(slots=True)
class RoundResult:
    """刷一轮的全部产物。report / 界面只认这个结构。"""

    platform: str
    started_at: float
    finished_at: float
    seen: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    picked: list[Pick] = field(default_factory=list)
    uncertain: list[Pick] = field(default_factory=list)
    note: str = ""

    @property
    def elapsed(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(self.elapsed, 1),
            "seen": self.seen,
            "dropped": dict(self.dropped),
            "picked": [p.to_dict() for p in self.picked],
            "uncertain": [p.to_dict() for p in self.uncertain],
            "note": self.note,
        }


# ---------------------------------------------------------------- 硬筛与打分


def hard_filter(items: list[Candidate], th: Thresholds) -> tuple[list[Candidate], dict[str, int]]:
    """不用模型的筛。返回 (留下的, 每种原因丢了几条)。

    丢的理由要记账：日报里得能写清「今天 12 条里有 4 条是视频太短」，
    否则使用者只会觉得「怎么就这么几条」。
    """
    kept: list[Candidate] = []
    dropped = {"no_desc": 0, "too_short": 0, "too_quiet": 0, "only_likes": 0}
    for c in items:
        if not title_of(c):
            dropped["no_desc"] += 1
            continue
        dur = duration_of(c)
        if dur and dur < th.min_duration_s:
            dropped["too_short"] += 1
            continue
        if interactions(c) < th.min_interactions:
            dropped["too_quiet"] += 1
            continue
        if talk_ratio(c) < th.min_talk_ratio:
            dropped["only_likes"] += 1
            continue
        kept.append(c)
    return kept, {k: v for k, v in dropped.items() if v}


def talk_ratio(c: Candidate) -> float:
    """非赞互动占赞的比例。纯流量号的评论/收藏/转发加起来也压不过赞。

    ``danmaku`` 也算：B 站推荐流不返回评论/收藏/转发，只给播放、点赞、弹幕。
    不把弹幕算进来的话，B 站候选的比值恒为 0，会被整批判成「只有赞」——
    真机上就是「刷一轮 → 硬筛之后一条都没剩」。抖音没有弹幕字段，取值就是 0。
    """
    talk = (stat(c, "comment") + stat(c, "collect") + stat(c, "share")
            + stat(c, "danmaku"))
    return talk / max(stat(c, "like"), 1)


def interest_score(c: Candidate, th: Thresholds | None = None) -> float:
    """热度 × 讨论度 × 新鲜度。三个都取对数或比值，避免被单一大数带跑。"""
    th = th or Thresholds()
    heat = math.log10(interactions(c) + 10)
    talk = 1.0 + min(talk_ratio(c), 2.0)
    age = age_hours(c)
    if age <= 0:
        fresh = 1.0
    else:
        fresh = max(0.2, 1.0 - min(age, th.max_age_h) / (th.max_age_h * 1.25))
    return round(heat * talk * fresh, 4)


def rank(items: list[Candidate], th: Thresholds | None = None) -> list[Candidate]:
    th = th or Thresholds()
    return sorted(items, key=lambda c: interest_score(c, th), reverse=True)


# ---------------------------------------------------------------- 语义判断


def _num(n: float) -> str:
    """12982764 → 1298.3万。给模型看的数字别太吓人。"""
    if n >= 1e8:
        return f"{n / 1e8:.2f}亿"
    if n >= 1e4:
        return f"{n / 1e4:.1f}万"
    return str(int(n))


def build_prompt(c: Candidate, comments: list[CommentSample],
                 evidence: dict[str, Any] | None = None) -> str:
    dur = duration_of(c)
    age = age_hours(c)
    lines = [
        "你是内容筛选助手。判断这条内容「值不值得占用使用者几分钟」。",
        "",
        "## 候选",
        f"- 平台：{c.platform}",
        f"- 文案：{title_of(c)[:400]}",
        f"- 作者：{c.author or '未知'}",
        f"- 时长：{dur:.1f} 秒" if dur else "- 时长：未知",
        f"- 发布：{age:.0f} 小时前" if age else "- 发布：未知",
        f"- 互动：赞 {_num(stat(c, 'like'))} / 评 {_num(stat(c, 'comment'))}"
        f" / 藏 {_num(stat(c, 'collect'))} / 转 {_num(stat(c, 'share'))}",
    ]
    extra_desc = str((c.extra or {}).get("description") or "").strip()
    if extra_desc:
        lines.append(f"- 简介：{extra_desc[:300]}")
    # 平台原生的判断证据（B 站是字幕原文；抖音没有字幕轨，传空）
    evidence = evidence or {}
    subtitle = str(evidence.get("subtitle") or "").strip()
    danmaku = [str(item) for item in (evidence.get("danmaku") or []) if str(item).strip()]
    lines += ["", "## 正文证据"]
    if subtitle:
        lines.append(f"【视频字幕原文（{len(subtitle)} 字）】")
        lines.append(subtitle[:4000])
    elif danmaku:
        lines.append("（没有字幕，以下是这条视频的弹幕）")
        for item in danmaku[:20]:
            lines.append(f"- {item[:60]}")
    else:
        lines.append("（没有拿到字幕，也没有弹幕）")

    lines += ["", "## 高赞评论"]
    if comments:
        for i, cm in enumerate(comments[:10], 1):
            likes = getattr(cm, "likes", 0)
            text = (getattr(cm, "text", "") or "").replace("\n", " ")[:160]
            lines.append(f"{i}. （赞 {_num(likes)}）{text}")
    else:
        lines.append("（没有拿到评论）")
    lines += [
        "",
        "## 输出",
        "只输出一个 JSON 对象，不要 markdown 围栏、不要任何解释：",
        '{"worth": true, "confidence": 0.0, "reason": "", "tags": []}',
        "",
        "规则：",
        "1. reason 必须引用具体依据（评论里的原话、文案里的具体点），不许写「很有趣」「挺好玩」这种空话。",
        "2. tags 最多三个词，描述它属于哪类内容（如「科普」「宠物」「生活」）。",
        "3. 评论太少、文案全靠话题标签、看不出内容是什么 —— 这时 confidence 必须低于 0.6。",
        "4. confidence 是你对 worth 这个判断的把握，不是内容质量分。",
    ]
    return "\n".join(lines)


def parse_judgment(raw: str, *, item_id: str, min_confidence: float = 0.6) -> Judgment:
    """把模型输出解析成 Judgment。任何一处不合法都退化成「判断不了」。"""
    text = (raw or "").strip()
    if not text:
        return Judgment(item_id=item_id, error="模型返回空")
    candidates = [text]
    m = _FENCE_RE.search(text)
    if m:
        candidates.insert(0, m.group(1).strip())
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        candidates.append(text[i:j + 1])
    obj: dict[str, Any] | None = None
    for chunk in candidates:
        try:
            parsed = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            obj = parsed
            break
    if obj is None:
        return Judgment(item_id=item_id, error="模型没给出可解析的 JSON")

    worth = obj.get("worth")
    if not isinstance(worth, bool):
        return Judgment(item_id=item_id, error="JSON 里的 worth 不是布尔值")
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    reason = str(obj.get("reason") or "").strip()
    tags = [str(t)[:12] for t in (obj.get("tags") or []) if str(t).strip()][:3]
    # 「它自己的一句话」：报告里单独一行。太长的截断，因为它本该短
    one_line = " ".join(str(obj.get("one_line") or "").split())[:60]

    # 证据不足不推：把握不够、或说不出依据，一律算判断不了
    if confidence < min_confidence:
        return Judgment(item_id=item_id, worth=None, confidence=confidence,
                        reason=reason, tags=tags, one_line=one_line,
                        error=f"把握 {confidence:.2f} 低于阈值 {min_confidence}")
    if not reason:
        return Judgment(item_id=item_id, worth=None, confidence=confidence,
                        tags=tags, one_line=one_line, error="模型没给出理由")
    return Judgment(item_id=item_id, worth=worth, confidence=confidence, reason=reason,
                    tags=tags, one_line=one_line)


class SemanticJudge:
    """把候选丢给 Reasonix 要一份严格 JSON 判断。

    只依赖 ``brain.extract_json(prompt) -> str`` 这一个方法，所以测试里塞个假 brain 就行。
    """

    def __init__(self, brain: Any | None, *, min_confidence: float = 0.6) -> None:
        self._brain = brain
        self._min_confidence = min_confidence

    @property
    def available(self) -> bool:
        return self._brain is not None and bool(getattr(self._brain, "backend_ready", True))

    async def judge(self, c: Candidate, comments: list[CommentSample],
                    evidence: dict[str, Any] | None = None) -> Judgment:
        if self._brain is None:
            return Judgment(item_id=c.item_id, error="没有可用的模型后端")
        try:
            raw = await self._brain.extract_json(build_prompt(c, comments, evidence))
        except Exception as exc:  # noqa: BLE001 - 模型挂了不能把整轮拖垮
            return Judgment(item_id=c.item_id, error=f"模型调用失败：{exc}")
        return parse_judgment(raw, item_id=c.item_id, min_confidence=self._min_confidence)


class RadarPipeline:
    """跑一轮：拉候选 → 硬筛 → 排序 → 逐条要评论和判断。"""

    def __init__(self, thresholds: Thresholds | None = None,
                 judge: SemanticJudge | None = None) -> None:
        self._th = thresholds or Thresholds()
        self._judge = judge

    @property
    def thresholds(self) -> Thresholds:
        return self._th

    async def run(self, provider: RadarProvider, *, top: int = 8,
                  with_comments: bool = True) -> RoundResult:
        started = time.time()
        items = await provider.candidates(top=top)
        kept, dropped = hard_filter(items, self._th)
        ordered = rank(kept, self._th)[: self._th.max_judge]

        picked: list[Pick] = []
        uncertain: list[Pick] = []
        # 平台自带判断证据（B 站字幕）时优先用它：抖音没有字幕轨，provider 上就没这个方法
        evidence_hook = getattr(provider, "evidence", None)
        for c in ordered:
            comments: list[CommentSample] = []
            if with_comments and provider.supports_comment:
                try:
                    comments = await provider.comments(c.item_id, limit=self._th.comments_per_item)
                except RadarError:
                    comments = []
                except Exception:  # noqa: BLE001 - 评论拿不到不该让这条消失
                    comments = []
            evidence: dict[str, Any] = {}
            if evidence_hook is not None:
                try:
                    evidence = await evidence_hook(c) or {}
                except RadarError as exc:
                    log.info("平台证据拿不到（%s）：%s", c.item_id, exc.code)
                except Exception:  # noqa: BLE001 - 证据缺失不该让这条消失
                    evidence = {}
            if self._judge is None:
                judgment = Judgment(item_id=c.item_id, error="没有判断器")
            else:
                judgment = await self._judge.judge(c, comments, evidence)
            pick = Pick(candidate=c, score=interest_score(c, self._th),
                        comments=comments, judgment=judgment, evidence=evidence)
            (picked if pick.worth else uncertain).append(pick)

        note = ""
        if not ordered:
            note = "硬筛之后一条都没剩" if items else "平台没返回候选"
        # 鸭子类型实现可能只写了 platform 没写 name，别因此炸在收尾处
        label = getattr(provider, "name", "") or getattr(provider, "platform", "")
        return RoundResult(
            platform=label, started_at=started, finished_at=time.time(),
            seen=len(items), dropped=dropped, picked=picked, uncertain=uncertain, note=note,
        )
