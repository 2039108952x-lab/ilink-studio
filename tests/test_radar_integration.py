"""内容雷达的端到端测试：假 provider 跑完一整轮，看落到盘上的东西对不对。

这一层的价值在于把四块串起来验证：pipeline 的判断 → report 的日报 → notify 的通知
→ quota 的额度。单独测每块都过、合起来丢掉调用，是这类流水线最典型的翻车方式。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from gateway.radar import Candidate, CommentSample, Identity, RadarProvider, SendOutcome
from gateway.radar.notify import Notifier, round_notice
from gateway.radar.pipeline import RadarPipeline, SemanticJudge, Thresholds
from gateway.radar.report import QuotaTracker, read, render, report_path, write
from gateway.radar.scheduler import RadarScheduler, ScheduleConfig, Window


class _ScriptedProvider(RadarProvider):
    """按脚本返回候选与评论；评论可以指定某个 id 失败。"""

    name = "fake-douyin"
    supports_share = True
    supports_text = True
    supports_comment = True

    def __init__(self, items, *, comments=None, fail_comment_ids=()):
        self._items = items
        self._comments = comments or []
        self._fail = set(fail_comment_ids)

    async def whoami(self):
        return Identity(ok=True, logged_in=True, nick="测试", uid="1")

    async def candidates(self, *, top: int = 8):
        return list(self._items)[:top]

    async def comments(self, item_id: str, *, limit: int = 10):
        if item_id in self._fail:
            from gateway.radar import RadarError
            raise RadarError("拉评论失败")
        return list(self._comments)

    async def send_share(self, *, item_id: str, to: str = "", to_id: str = "",
                         reason: str = "", confirm: bool = False) -> SendOutcome:
        return SendOutcome(ok=True, sent=confirm, needs_confirm=not confirm, draft=reason)

    async def send_text(self, *, to: str = "", to_id: str = "",
                        text: str, confirm: bool = False) -> SendOutcome:
        return SendOutcome(ok=True, sent=confirm, needs_confirm=not confirm, draft=text)


class _Brain:
    backend_ready = True

    def __init__(self, verdicts: dict[str, str]):
        self._verdicts = verdicts

    async def extract_json(self, prompt: str) -> str:
        for key, reply in self._verdicts.items():
            if key in prompt:
                return reply
        return '{"worth": true, "confidence": 0.9, "reason": "默认通过"}'


def _cand(item_id, *, title="有文案的一条", digg=50000, comment=10000,
          collect=8000, share=4000, duration=20.0, age=5.0) -> Candidate:
    return Candidate(platform="douyin", item_id=item_id, title=title, author="作者",
                     duration_sec=int(duration),
                     stats={"like": digg, "comment": comment, "collect": collect, "share": share},
                     url=f"https://www.douyin.com/video/{item_id}",
                     extra={"age_h": age,
                            "share_url": f"https://www.iesdouyin.com/share/video/{item_id}/"
                                         f"?share_sign=secret"})


CANDIDATES = [
    _cand("111", title="猫把水杯推下去了"),
    # 互动总量很大但几乎没人评论/收藏/转发 → 走 only_likes 丢
    _cand("222", title="纯流量号：只有赞", digg=90000, comment=50, collect=10, share=5),
    _cand("333", title=""),
    # 时长不够 → 走 too_short 丢
    _cand("444", title="太短的", duration=4.0),
]
COMMENTS = [CommentSample(text="笑死，我家猫也这样", likes=9999),
            CommentSample(text="第二个", likes=10)]


class TestFullRound(unittest.IsolatedAsyncioTestCase):
    def _pipeline(self):
        brain = _Brain({"猫把水杯": '{"worth": true, "confidence": 0.92, '
                                  '"reason": "评论在接梗，说自家猫也这样", "tags": ["宠物"]}'})
        return RadarPipeline(Thresholds(max_judge=3), SemanticJudge(brain))

    async def test_one_round_produces_a_report_on_disk(self):
        provider = _ScriptedProvider(CANDIDATES, comments=COMMENTS)
        result = await self._pipeline().run(provider, top=4)
        self.assertEqual(result.seen, 4)
        self.assertGreaterEqual(sum(result.dropped.values()), 2)   # 空文案 + 太短
        self.assertEqual(len(result.picked), 1)
        self.assertEqual(result.picked[0].candidate.item_id, "111")

        with tempfile.TemporaryDirectory() as tmp:
            path = write(result, Path(tmp), day=date(2026, 10, 26),
                         quota=QuotaTracker(Path(tmp) / "q.json").state())
            text = path.read_text(encoding="utf-8")
            self.assertIn("猫把水杯推下去了", text)
            self.assertIn("评论在接梗", text)
            self.assertIn("没文案", text)      # 丢弃原因也要写出来
            self.assertEqual(read(Path(tmp), date(2026, 10, 26)), text)

    async def test_report_flags_comments_unavailable_without_dropping_the_item(self):
        provider = _ScriptedProvider(CANDIDATES, comments=COMMENTS, fail_comment_ids={"111"})
        result = await self._pipeline().run(provider, top=4)
        self.assertEqual(len(result.picked), 1)
        self.assertEqual(result.picked[0].comments, [])

    async def test_low_confidence_goes_to_uncertain_not_picked(self):
        brain = _Brain({"猫把水杯": '{"worth": true, "confidence": 0.2, "reason": "说不清"}'})
        provider = _ScriptedProvider(CANDIDATES, comments=COMMENTS)
        result = await RadarPipeline(Thresholds(max_judge=3), SemanticJudge(brain)).run(provider, top=4)
        self.assertEqual(result.picked, [])
        self.assertEqual(len(result.uncertain), 1)

    async def test_notice_carries_the_best_pick_and_masked_url(self):
        provider = _ScriptedProvider(CANDIDATES, comments=COMMENTS)
        result = await self._pipeline().run(provider, top=4)
        note = round_notice(result, day="2026-10-26")
        self.assertIn("挑了 1 条", note.title)
        with tempfile.TemporaryDirectory() as tmp:
            out = await Notifier(Path(tmp), channel="file").send(note)
            self.assertTrue(out["ok"])
            payload = json.loads(next((Path(tmp) / "notifications").glob("*.json"))
                                 .read_text(encoding="utf-8"))
            self.assertNotIn("secret", payload["url"])     # 分享链签名必须脱敏
            self.assertIn("douyin.com/video/111", payload["url"])

    async def test_quota_is_consumed_by_the_picks(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = QuotaTracker(Path(tmp) / "q.json", per_day=5, clock=lambda: 1_700_000_000.0)
            provider = _ScriptedProvider(CANDIDATES, comments=COMMENTS)
            result = await self._pipeline().run(provider, top=4)
            after = quota.consume(len(result.picked))
            self.assertEqual(after.used, 1)
            self.assertEqual(after.left, 4)

    async def test_scheduler_runs_the_whole_round_once(self):
        import datetime as dt

        def _local(hh: int, mm: int) -> float:
            # 必须用本地时间构造：裸时间戳会落在别的钟点上，窗口就命不中
            return dt.datetime(2026, 10, 26, hh, mm).timestamp()

        provider = _ScriptedProvider(CANDIDATES, comments=COMMENTS)
        pipeline = self._pipeline()
        calls = {"n": 0}

        async def run_once():
            calls["n"] += 1
            return await pipeline.run(provider, top=4)

        with tempfile.TemporaryDirectory() as tmp:
            sched = RadarScheduler(run_once, state_path=Path(tmp) / "sched.json",
                                   config=ScheduleConfig(enabled=True, windows=[
                                       Window(key="peak#1", start="09:00", end="10:00")]),
                                   clock=lambda: _local(9, 5))
            first = await sched.tick(_local(9, 5))
            second = await sched.tick(_local(9, 15))
            self.assertEqual(first["fired"], "peak#1")
            self.assertEqual(second["skipped"], "not_due")
            self.assertEqual(calls["n"], 1)

    async def test_nothing_picked_still_renders_a_readable_report(self):
        provider = _ScriptedProvider([_cand("999", title="")], comments=[])
        result = await self._pipeline().run(provider, top=1)
        text = render(result, day=date(2026, 10, 26))
        self.assertIn("刷抖音", text)
        self.assertIn("宁可空着", text)
        self.assertTrue(report_path(Path("."), date(2026, 10, 26)).name.startswith("2026-10-26"))


if __name__ == "__main__":
    unittest.main()
