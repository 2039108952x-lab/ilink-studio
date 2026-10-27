"""内容雷达内核的契约快照。

这个文件早先测的是 ``Limits`` / ``build_evidence_pack`` / ``decide`` / ``parse_picks`` /
``scan`` 那一套「纯函数 + 三档 verdict（recommend | mention | unsure）」架构。
内核统一到 ``provider.py`` 契约 + ``pipeline.py`` 之后有两处根本变化：

* ``mention`` 这一档不存在了 —— 配额改由 ``report.QuotaTracker`` 单独管，
  一轮出来的只有 ``picked`` / ``uncertain`` 两档；
* 判断改成「一条候选一次模型调用」，模型输出单个 JSON 对象而不是 picks 数组，
  所以 ``parse_picks(raw)`` 变成 ``parse_judgment(raw, item_id=...)``。

这里只保留新架构下仍然承重的边界，细节用例在 ``test_radar_pipeline.py``。
"""

from __future__ import annotations

import json
import unittest

from gateway.radar import (
    Candidate,
    CommentSnapshot,
    Thresholds,
    build_prompt,
    hard_filter,
    parse_judgment,
    talk_ratio,
)


def candidate(item_id: str, *, title: str = "加班到三点回家猫以为我死了", duration: int = 30,
              like: int = 1000, comment: int = 50, collect: int = 90,
              share: int = 0) -> Candidate:
    return Candidate(
        platform="douyin", item_id=item_id, title=title, author="铲屎官",
        duration_sec=duration, url=f"https://www.douyin.com/video/{item_id}",
        stats={"like": like, "comment": comment, "collect": collect, "share": share},
    )


class HardFilterTest(unittest.TestCase):
    def test_thresholds_are_configurable(self) -> None:
        kept, dropped = hard_filter([candidate("1", duration=30)],
                                    Thresholds(min_duration_s=60))
        self.assertEqual(kept, [])
        self.assertEqual(sum(dropped.values()), 1)

    def test_missing_description_is_dropped(self) -> None:
        kept, dropped = hard_filter([candidate("1", title="   ")], Thresholds())
        self.assertEqual(kept, [])
        self.assertEqual(sum(dropped.values()), 1)

    def test_a_normal_candidate_survives(self) -> None:
        hot = candidate("1", like=5000, comment=200, collect=200, share=50)
        kept, dropped = hard_filter([hot], Thresholds())
        self.assertEqual([c.item_id for c in kept], ["1"])
        self.assertEqual(sum(dropped.values()), 0)


class TalkRatioTest(unittest.TestCase):
    def test_likes_only_has_no_ratio(self) -> None:
        only_likes = candidate("1", like=1000, comment=0, collect=0, share=0)
        self.assertEqual(talk_ratio(only_likes), 0.0)

    def test_ratio_uses_likes_as_base(self) -> None:
        self.assertAlmostEqual(
            talk_ratio(candidate("1", like=1000, comment=20, collect=90, share=6)), 0.116)

    def test_zero_likes_falls_back_to_absolute_talk(self) -> None:
        """零赞时 ``max(like, 1)`` 兜底：返回绝对讨论数，而不是 inf / 除零异常。"""
        quiet = candidate("1", like=0, comment=5, collect=0, share=0)
        self.assertEqual(talk_ratio(quiet), 5.0)


class ParseJudgmentTest(unittest.TestCase):
    def test_non_boolean_worth_is_rejected(self) -> None:
        judgment = parse_judgment(json.dumps({"worth": "yes", "confidence": 0.9}), item_id="1")
        self.assertIsNone(judgment.worth)
        self.assertTrue(judgment.error)

    def test_low_confidence_is_not_recommended(self) -> None:
        judgment = parse_judgment(
            json.dumps({"worth": True, "confidence": 0.3, "reason": "还行"}), item_id="1")
        self.assertIsNone(judgment.worth)

    def test_garbage_is_an_error_not_a_guess(self) -> None:
        judgment = parse_judgment("模型今天不想说话", item_id="1")
        self.assertIsNone(judgment.worth)
        self.assertTrue(judgment.error)


class BuildPromptTest(unittest.TestCase):
    def test_does_not_leak_raw_platform_fields(self) -> None:
        """`extra` 里带签名的分享链绝不能进 prompt。"""
        c = candidate("1")
        c.extra = {
            "share_url": "https://www.iesdouyin.com/share/video/1/?share_sign=SECRET",
            "web_url": "https://www.douyin.com/video/1",
        }
        prompt = build_prompt(c, [CommentSnapshot("笑死", 12)])
        self.assertNotIn("SECRET", prompt)
        self.assertNotIn("iesdouyin", prompt)

    def test_includes_comments_and_asks_for_strict_json(self) -> None:
        prompt = build_prompt(candidate("1"), [CommentSnapshot("笑死", 12)])
        self.assertIn("笑死", prompt)
        self.assertIn("只输出一个 JSON 对象", prompt)

    def test_works_without_comments(self) -> None:
        self.assertIn("没有拿到评论", build_prompt(candidate("1"), []))


if __name__ == "__main__":
    unittest.main()
