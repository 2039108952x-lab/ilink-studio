"""RadarProvider 契约的回归测试。

数据结构以 ``radar/models.py`` 为准，所以这里锁的是：
访问适配函数（stat / interactions / age_hours）、能力默认保守、
confirm=False 只回草稿、不支持的能力明确报错而不是假装成功。
"""

from __future__ import annotations

import unittest

from gateway.radar import Candidate, CommentSnapshot, RadarError, RadarProvider, ShareDraft, SendOutcome
from gateway.radar.models import Candidate as ModelCandidate
from gateway.radar.provider import age_hours, interactions, stat, title_of


def _cand(**kw) -> Candidate:
    base = dict(platform="douyin", item_id="1", title="一条内容")
    base.update(kw)
    return Candidate(**base)


class _Minimal(RadarProvider):
    """只实现抽象方法的最小 provider，用来验证默认值。"""

    name = "minimal"

    async def whoami(self):
        from gateway.radar import Identity
        return Identity(ok=True, logged_in=True, nick="n", uid="u")

    async def candidates(self, *, top: int = 8):
        return []

    async def send_share(self, draft: ShareDraft, *, confirm: bool = False) -> ShareDraft:
        draft.preview = draft.reason
        draft.sent = bool(confirm)
        return draft

    async def send_text(self, to: str, text: str, *, confirm: bool = False) -> dict:
        return {"ok": True, "sent": bool(confirm), "needsConfirm": not confirm, "draft": text}


class TestModelIsShared(unittest.TestCase):
    def test_provider_reexports_the_same_candidate_class(self):
        # 两套 Candidate 是上一版遗留下来的坑，这里锁死只有一个来源
        self.assertIs(Candidate, ModelCandidate)

    def test_comment_sample_is_an_alias_of_comment_snapshot(self):
        from gateway.radar import CommentSample
        self.assertIs(CommentSample, CommentSnapshot)


class TestStatAdapters(unittest.TestCase):
    def test_stat_reads_semantic_keys(self):
        c = _cand(stats={"like": 10, "comment": 2, "collect": 3, "share": 5})
        self.assertEqual(stat(c, "like"), 10)
        self.assertEqual(stat(c, "comment"), 2)
        self.assertEqual(interactions(c), 20)

    def test_stat_accepts_platform_aliases(self):
        # 抖音原样字段叫 digg / digg_count
        self.assertEqual(stat(_cand(stats={"digg": 7}), "like"), 7)
        self.assertEqual(stat(_cand(stats={"digg_count": 9}), "like"), 9)

    def test_missing_stat_is_zero_not_an_exception(self):
        self.assertEqual(stat(_cand(), "like"), 0)
        self.assertEqual(interactions(_cand()), 0)

    def test_age_hours_prefers_extra(self):
        self.assertAlmostEqual(age_hours(_cand(extra={"age_h": 12.5})), 12.5)

    def test_age_hours_falls_back_to_published_at(self):
        import time
        c = _cand(published_at=time.time() - 7200)
        self.assertAlmostEqual(age_hours(c), 2.0, delta=0.2)

    def test_age_hours_unknown_is_zero(self):
        self.assertEqual(age_hours(_cand()), 0.0)

    def test_title_of_trims(self):
        self.assertEqual(title_of(_cand(title="  带空格  ")), "带空格")


class TestCandidate(unittest.TestCase):
    def test_as_dict_keeps_extra_out_of_payload(self):
        d = _cand(extra={"secret": "cookie"}).as_dict()
        self.assertNotIn("extra", d)
        self.assertEqual(d["item_id"], "1")

    def test_comment_snapshot_has_text_and_likes(self):
        cm = CommentSnapshot(text="笑死", likes=30)
        self.assertEqual((cm.text, cm.likes), ("笑死", 30))


class TestShareDraft(unittest.TestCase):
    def test_defaults_to_not_sent(self):
        draft = ShareDraft(item_id="1", to="某人", reason="值得看")
        self.assertFalse(draft.sent)
        self.assertEqual(draft.to_dict()["to"], "某人")

    def test_dict_carries_the_preview(self):
        draft = ShareDraft(item_id="1", to="某人", preview="卡片预览", sent=True)
        self.assertTrue(draft.to_dict()["sent"])
        self.assertEqual(draft.to_dict()["preview"], "卡片预览")


class TestRadarError(unittest.TestCase):
    def test_single_argument_is_the_message(self):
        exc = RadarError("拉评论失败")
        self.assertEqual(str(exc), "拉评论失败")
        self.assertEqual(exc.code, "failed")

    def test_two_arguments_are_code_and_message(self):
        exc = RadarError("rate_limited", "被限流了", retryable=True, payload={"a": 1})
        self.assertEqual(exc.code, "rate_limited")
        self.assertEqual(str(exc), "被限流了")
        self.assertTrue(exc.retryable)
        self.assertEqual(exc.payload["a"], 1)


class TestCapabilityDefaults(unittest.TestCase):
    def test_capabilities_default_to_false(self):
        p = _Minimal()
        self.assertFalse(p.supports_share)
        self.assertFalse(p.supports_text)
        self.assertFalse(p.supports_comment)

    def test_comments_raises_when_unsupported(self):
        import asyncio
        with self.assertRaises(RadarError):
            asyncio.run(_Minimal().comments("1"))

    def test_close_is_a_noop_by_default(self):
        import asyncio
        asyncio.run(_Minimal().close())


class TestDraftSemantics(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_false_never_sends_text(self):
        out = await _Minimal().send_text("某人", "在吗", confirm=False)
        self.assertFalse(out["sent"])
        self.assertTrue(out["needsConfirm"])

    async def test_confirm_false_never_marks_share_sent(self):
        draft = await _Minimal().send_share(ShareDraft(item_id="1", to="某人", reason="r"),
                                            confirm=False)
        self.assertFalse(draft.sent)

    async def test_send_outcome_code_is_empty_on_success(self):
        self.assertEqual(SendOutcome(ok=True, sent=True).code, "")


if __name__ == "__main__":
    unittest.main()
