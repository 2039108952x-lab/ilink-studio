"""内容雷达流水线的测试：硬筛、比值打分、严格 JSON、证据不足不推。

用假 provider + 假 brain，不联网、不碰任何平台。
"""

from __future__ import annotations

import unittest

from gateway.radar import Candidate, CommentSample, RadarProvider, SendOutcome
from gateway.radar.bilibili import DEFAULT_THRESHOLDS
from gateway.radar.pipeline import (
    Judgment,
    RadarPipeline,
    SemanticJudge,
    Thresholds,
    build_prompt,
    hard_filter,
    interest_score,
    parse_judgment,
    rank,
    talk_ratio,
)


def _c(item_id="1", *, title="一条有文案的视频", duration=20.0, age=10.0,
       digg=10000, comment=2000, collect=1000, share=500) -> Candidate:
    return Candidate(platform="douyin", item_id=item_id, title=title, author="某人",
                     duration_sec=int(duration),
                     stats={"like": digg, "comment": comment, "collect": collect, "share": share},
                     extra={"age_h": age})


class _FakeProvider(RadarProvider):
    name = "fake"
    supports_comment = True

    def __init__(self, items, comments=None):
        self._items = items
        self._comments = comments or []

    async def whoami(self):
        from gateway.radar import Identity
        return Identity(ok=True, logged_in=True, nick="n", uid="u")

    async def candidates(self, *, top: int = 8):
        return list(self._items)

    async def comments(self, item_id: str, *, limit: int = 10):
        return list(self._comments)

    async def send_share(self, *, item_id: str, to: str = "", to_id: str = "",
                         reason: str = "", confirm: bool = False) -> SendOutcome:
        return SendOutcome(ok=True, needs_confirm=not confirm, draft=reason)

    async def send_text(self, *, to: str = "", to_id: str = "",
                        text: str, confirm: bool = False) -> SendOutcome:
        return SendOutcome(ok=True, needs_confirm=not confirm, draft=text)


class _FakeBrain:
    """只实现 extract_json —— 判断层依赖的就是这一个方法。"""

    backend_ready = True

    def __init__(self, reply: str):
        self._reply = reply

    async def extract_json(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self._reply


# ---------------------------------------------------------------- 硬筛


class TestHardFilter(unittest.TestCase):
    def test_keeps_a_normal_candidate(self):
        kept, dropped = hard_filter([_c()], Thresholds())
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, {})

    def test_no_desc_is_dropped(self):
        kept, dropped = hard_filter([_c(title="  ")], Thresholds())
        self.assertEqual(kept, [])
        self.assertEqual(dropped, {"no_desc": 1})

    def test_too_short_is_dropped(self):
        kept, dropped = hard_filter([_c(duration=3.0)], Thresholds())
        self.assertEqual(dropped, {"too_short": 1})

    def test_too_quiet_is_dropped(self):
        kept, dropped = hard_filter([_c(digg=10, comment=1, collect=1, share=1)], Thresholds())
        self.assertEqual(dropped, {"too_quiet": 1})

    def test_only_likes_is_dropped(self):
        # 互动总量很大，但评论/收藏/转发加起来压不过赞 → 纯流量
        c = _c(digg=100000, comment=10, collect=5, share=5)
        kept, dropped = hard_filter([c], Thresholds())
        self.assertEqual(dropped, {"only_likes": 1})

    def test_dropped_counts_accumulate_per_reason(self):
        items = [_c("a", title=""), _c("b", duration=2.0), _c("c", title=""), _c("d")]
        kept, dropped = hard_filter(items, Thresholds())
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, {"no_desc": 2, "too_short": 1})


# ---------------------------------------------------------------- 比值与排序


class TestScoring(unittest.TestCase):
    def test_talk_ratio_ignores_likes_only(self):
        self.assertAlmostEqual(talk_ratio(_c(digg=1000, comment=0, collect=0, share=0)), 0.0)
        self.assertAlmostEqual(talk_ratio(_c(digg=100, comment=10, collect=10, share=0)), 0.2)

    def test_talk_ratio_never_divides_by_zero(self):
        self.assertGreaterEqual(talk_ratio(_c(digg=0, comment=5, collect=0, share=0)), 0.0)

    def test_bilibili_candidate_survives_on_danmaku_alone(self):
        """B 站推荐流只给 播放/点赞/弹幕，没有评论收藏转发。

        真机事故：不算弹幕时 B 站候选的讨论比恒为 0，8 条全被判「只有赞」，
        界面显示「硬筛之后一条都没剩」。
        """
        bili = Candidate(
            platform="bilibili", item_id="BV1xx", title="一条 B 站视频", author="某 UP",
            duration_sec=219,
            stats={"like": 137281, "comment": 0, "play": 2549204, "danmaku": 1161},
            extra={"age_h": 5.0},
        )
        self.assertGreater(talk_ratio(bili), 0.002)
        kept, dropped = hard_filter([bili], DEFAULT_THRESHOLDS)
        self.assertEqual([c.item_id for c in kept], ["BV1xx"], dropped)

    def test_more_talk_scores_higher_at_equal_heat(self):
        quiet = _c("a", digg=10000, comment=100, collect=100, share=100)
        chatty = _c("b", digg=1000, comment=3000, collect=3000, share=3000)
        self.assertGreater(interest_score(chatty), interest_score(quiet))

    def test_old_items_are_discounted(self):
        fresh = _c("a", age=10.0)
        stale = _c("b", age=2000.0)
        self.assertGreater(interest_score(fresh), interest_score(stale))

    def test_rank_puts_best_first(self):
        low = _c("low", digg=5000, comment=100, collect=50, share=50)
        high = _c("high", digg=100000, comment=20000, collect=10000, share=10000)
        self.assertEqual([c.item_id for c in rank([low, high])], ["high", "low"])


# ---------------------------------------------------------------- 严格 JSON


class TestParseJudgment(unittest.TestCase):
    def test_plain_json(self):
        j = parse_judgment('{"worth": true, "confidence": 0.9, "reason": "评论在接梗", "tags": ["梗"]}',
                           item_id="x")
        self.assertTrue(j.worth)
        self.assertEqual(j.reason, "评论在接梗")
        self.assertEqual(j.tags, ["梗"])

    def test_markdown_fence_is_tolerated(self):
        j = parse_judgment('```json\n{"worth": false, "confidence": 0.8, "reason": "广告"}\n```',
                           item_id="x")
        self.assertFalse(j.worth)

    def test_prose_around_json_is_tolerated(self):
        j = parse_judgment('好的，结论如下：{"worth": true, "confidence": 0.7, "reason": "有具体内容"}',
                           item_id="x")
        self.assertTrue(j.worth)

    def test_low_confidence_becomes_unknown(self):
        j = parse_judgment('{"worth": true, "confidence": 0.3, "reason": "看着还行"}', item_id="x")
        self.assertIsNone(j.worth)
        self.assertIn("低于阈值", j.error)

    def test_missing_reason_becomes_unknown(self):
        j = parse_judgment('{"worth": true, "confidence": 0.9, "reason": ""}', item_id="x")
        self.assertIsNone(j.worth)
        self.assertIn("没给出理由", j.error)

    def test_garbage_is_an_error_not_a_guess(self):
        j = parse_judgment("我觉得这条挺好的", item_id="x")
        self.assertIsNone(j.worth)
        self.assertIn("可解析", j.error)

    def test_non_boolean_worth_is_rejected(self):
        j = parse_judgment('{"worth": "yes", "confidence": 0.9, "reason": "r"}', item_id="x")
        self.assertIsNone(j.worth)
        self.assertIn("布尔", j.error)

    def test_confidence_is_clamped(self):
        j = parse_judgment('{"worth": true, "confidence": 5, "reason": "r"}', item_id="x")
        self.assertEqual(j.confidence, 1.0)


# ---------------------------------------------------------------- prompt


class TestPrompt(unittest.TestCase):
    def test_prompt_contains_evidence_and_strict_json_rule(self):
        p = build_prompt(_c(), [CommentSample(text="笑死", likes=30000)])
        self.assertIn("笑死", p)
        self.assertIn("只输出一个 JSON 对象", p)
        self.assertIn("confidence", p)

    def test_prompt_works_without_comments(self):
        p = build_prompt(_c(), [])
        self.assertIn("没有拿到评论", p)


# ---------------------------------------------------------------- 端到端分流


class TestPipelineRun(unittest.IsolatedAsyncioTestCase):
    async def test_picked_and_uncertain_are_split(self):
        items = [_c("a"), _c("b")]
        provider = _FakeProvider(items, [CommentSample(text="好笑", likes=100)])
        judge = SemanticJudge(_FakeBrain('{"worth": true, "confidence": 0.9, "reason": "评论在接梗"}'))
        result = await RadarPipeline(Thresholds(max_judge=2), judge).run(provider, top=2)
        self.assertEqual(len(result.picked), 2)
        self.assertEqual(result.uncertain, [])
        self.assertEqual(result.seen, 2)

    async def test_low_confidence_lands_in_uncertain(self):
        provider = _FakeProvider([_c("a")], [])
        judge = SemanticJudge(_FakeBrain('{"worth": true, "confidence": 0.2, "reason": "说不清"}'))
        result = await RadarPipeline(Thresholds(), judge).run(provider, top=1)
        self.assertEqual(result.picked, [])
        self.assertEqual(len(result.uncertain), 1)
        self.assertIsNone(result.uncertain[0].judgment.worth)

    async def test_max_judge_caps_model_calls(self):
        items = [_c(str(i)) for i in range(10)]
        provider = _FakeProvider(items, [])
        brain = _FakeBrain('{"worth": true, "confidence": 0.9, "reason": "r"}')
        result = await RadarPipeline(Thresholds(max_judge=3), SemanticJudge(brain)).run(provider, top=10)
        self.assertEqual(len(result.picked) + len(result.uncertain), 3)

    async def test_no_candidates_gives_a_reason(self):
        result = await RadarPipeline(Thresholds(), SemanticJudge(_FakeBrain("{}"))).run(
            _FakeProvider([], []), top=5)
        self.assertEqual(result.picked, [])
        self.assertTrue(result.note)

    async def test_model_failure_degrades_to_uncertain(self):
        class _Broken:
            backend_ready = True

            async def extract_json(self, prompt):
                raise RuntimeError("后端没响应")

        provider = _FakeProvider([_c("a")], [])
        result = await RadarPipeline(Thresholds(), SemanticJudge(_Broken())).run(provider, top=1)
        self.assertEqual(result.picked, [])
        self.assertIn("模型调用失败", result.uncertain[0].judgment.error)

    async def test_comment_failure_does_not_drop_the_item(self):
        class _NoComments(_FakeProvider):
            async def comments(self, item_id: str, *, limit: int = 10):
                from gateway.radar import RadarError
                raise RadarError("拉评论失败")

        provider = _NoComments([_c("a")], [])
        judge = SemanticJudge(_FakeBrain('{"worth": true, "confidence": 0.9, "reason": "看文案就够"}'))
        result = await RadarPipeline(Thresholds(), judge).run(provider, top=1)
        self.assertEqual(len(result.picked), 1)
        self.assertEqual(result.picked[0].comments, [])

    async def test_no_judge_means_everything_is_uncertain(self):
        provider = _FakeProvider([_c("a")], [])
        result = await RadarPipeline(Thresholds(), None).run(provider, top=1)
        self.assertEqual(result.picked, [])
        self.assertIn("没有判断器", result.uncertain[0].judgment.error)


class TestJudgmentDict(unittest.TestCase):
    def test_unknown_worth_serialises_as_null(self):
        d = Judgment(item_id="x", worth=None, error="e").to_dict()
        self.assertIsNone(d["worth"])
        self.assertEqual(d["error"], "e")


if __name__ == "__main__":
    unittest.main()
