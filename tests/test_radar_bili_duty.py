"""B 站值班层的落盘与复核。

这一层的产物**格式即契约**：上游宿主靠数 ``### BV号`` 行来对账「今天挑了几条」，
靠判重表决定「这条要不要再发」。格式一歪，配额和判重就一起失准。所以逐项钉死。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from gateway.radar import duty
from gateway.radar.pipeline import Judgment, Pick
from gateway.radar.provider import Candidate

BVID = "BV1uNk1YxEJQ"


def make_pick(*, worth: bool | None = True, confidence: float = 0.9,
              evidence: dict | None = None, reason: str = "字幕第 3 分钟讲了具体拆机步骤",
              one_line: str = "电源虚焊，不是主板", item_id: str = BVID) -> Pick:
    candidate = Candidate(
        platform="bilibili", item_id=item_id, title="修一台二手机器",
        author="某UP", duration_sec=754, url=f"https://www.bilibili.com/video/{item_id}",
        stats={"like": 1200, "comment": 30, "play": 88000, "danmaku": 40},
    )
    judgment = Judgment(item_id=item_id, worth=worth, confidence=confidence,
                        reason=reason if worth is not None else "", one_line=one_line,
                        error="" if worth is not None else "把握不足")
    return Pick(candidate=candidate, score=1.0, judgment=judgment,
                evidence=evidence or {"source": "subtitle", "subtitle": "字幕原文" * 10})


class GradeTest(unittest.TestCase):
    def test_needs_subtitle_for_top_grade(self) -> None:
        """有字幕 + 高把握 ⇒ 值得看。"""
        self.assertEqual(duty.grade_of(make_pick()), "值得看")

    def test_surface_only_caps_at_can_watch(self) -> None:
        """只有标题这类表层信息 ⇒ 最多「可以看」，这是章程里最不该犯的错。"""
        pick = make_pick(evidence={"source": "none"})
        self.assertEqual(duty.grade_of(pick), "可以看")

    def test_danmaku_is_not_enough_for_top_grade(self) -> None:
        pick = make_pick(evidence={"source": "danmaku", "danmaku": ["笑死"]})
        self.assertEqual(duty.grade_of(pick), "可以看")

    def test_low_confidence_caps_at_can_watch(self) -> None:
        pick = make_pick(confidence=0.62)
        self.assertEqual(duty.grade_of(pick), "可以看")

    def test_frames_count_as_evidence(self) -> None:
        pick = make_pick(evidence={"source": "none", "frames_seen": True})
        self.assertEqual(duty.grade_of(pick), "值得看")

    def test_not_worth(self) -> None:
        self.assertEqual(duty.grade_of(make_pick(worth=False)), "不值当")

    def test_undecided_is_blank(self) -> None:
        self.assertEqual(duty.grade_of(make_pick(worth=None)), "")

    def test_only_three_words(self) -> None:
        for pick in (make_pick(), make_pick(evidence={"source": "none"}),
                     make_pick(worth=False), make_pick(worth=None)):
            grade = duty.grade_of(pick)
            self.assertIn(grade, ("",) + duty.GRADES)


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pick_block_starts_with_bv_heading(self) -> None:
        lines = duty.render_pick(make_pick())
        self.assertEqual(lines[0], f"### {BVID}")
        body = "\n".join(lines)
        self.assertIn("**档位**：值得看", body)
        self.assertIn("为什么值得", body)
        self.assertIn("一句话", body)

    def test_count_picked_matches_quota_convention(self) -> None:
        from gateway.radar.pipeline import RoundResult
        result = RoundResult(platform="bilibili", started_at=0, finished_at=1,
                             seen=5, dropped={"too_short": 2},
                             picked=[make_pick(item_id=f"BV1{i:09d}") for i in range(3)])
        text = duty.render_round(result)
        self.assertEqual(duty.count_picked(text), 3)
        # 「判断不了」与丢掉的表不该被算成挑中
        self.assertIn("因无字幕跳过 0 条", text)

    def test_count_ignores_other_headings(self) -> None:
        text = "## 值得看\n\n### 1. 有条目号的标题\n\n### BV1uNk1YxEJQ\n"
        self.assertEqual(duty.count_picked(text), 1)

    def test_append_and_reread(self) -> None:
        duty.append_report(self.home, "第一轮\n")
        duty.append_report(self.home, "第二轮\n")
        text = duty.read_report(self.home)
        self.assertIn("第一轮", text)
        self.assertIn("第二轮", text)

    def test_quota_reads_the_report(self) -> None:
        from gateway.radar.pipeline import RoundResult
        result = RoundResult(platform="bilibili", started_at=0, finished_at=1,
                             picked=[make_pick(), make_pick(item_id="BV1aaaaaaaaa")])
        duty.write_report(self.home, duty.render_round(result))
        state = duty.quota_state(self.home, per_day=5)
        self.assertEqual(state["used"], 2)
        self.assertEqual(state["left"], 3)

    def test_quota_zero_means_unlimited(self) -> None:
        state = duty.quota_state(self.home, per_day=0)
        self.assertTrue(state["unlimited"])


class SeenTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_and_read_back(self) -> None:
        duty.seen_append(self.home, bvid=BVID, title="标题里 有空格", up="某UP", status="挑了")
        rows = duty.seen_recent(self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].bvid, BVID)
        self.assertEqual(rows[0].status, "挑了")
        self.assertEqual(rows[0].up, "某UP")

    def test_missing_table_is_distinguishable(self) -> None:
        """⚠️ 「表不存在」和「表是空的」要能分开：前者按章程这一轮不发评论。"""
        self.assertFalse(duty.seen_exists(self.home))
        self.assertEqual(duty.seen_recent(self.home), [])
        duty.seen_append(self.home, bvid=BVID)
        self.assertTrue(duty.seen_exists(self.home))

    def test_seen_bvids(self) -> None:
        duty.seen_append(self.home, bvid=BVID)
        duty.seen_append(self.home, bvid="BV1aaaaaaaaa")
        self.assertEqual(duty.seen_bvids(self.home), {BVID, "BV1aaaaaaaaa"})

    def test_filter_by_bvid(self) -> None:
        duty.seen_append(self.home, bvid=BVID, status="评论")
        duty.seen_append(self.home, bvid="BV1aaaaaaaaa", status="跳过")
        rows = duty.seen_recent(self.home, bvid=BVID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "评论")

    def test_bad_status_falls_back(self) -> None:
        duty.seen_append(self.home, bvid=BVID, status="随便写的")
        self.assertEqual(duty.seen_recent(self.home)[0].status, "跳过")


class SeenDayTest(unittest.TestCase):
    def test_variants(self) -> None:
        today = date(2026, 10, 26)
        self.assertEqual(duty.seen_day("2026-10-20", today), "2026-10-20")
        self.assertEqual(duty.seen_day("10-20", today), "2026-10-20")
        self.assertEqual(duty.seen_day("2026/10/20", today), "2026-10-20")
        self.assertEqual(duty.seen_day("2026-10-20 14:30", today), "2026-10-20")

    def test_future_month_rolls_to_last_year(self) -> None:
        today = date(2026, 1, 5)
        self.assertEqual(duty.seen_day("12-31", today), "2025-12-31")

    def test_unparseable(self) -> None:
        self.assertIsNone(duty.seen_day("昨天"))
        self.assertIsNone(duty.seen_day("2026-02-30"))


class ArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_note_per_video(self) -> None:
        path = duty.note_write(self.home, bvid=BVID, text=duty.note_text(make_pick()))
        self.assertTrue(path.is_file())
        self.assertIn(BVID, path.name)
        self.assertIn("为什么值得", path.read_text(encoding="utf-8"))

    def test_digest_line_format(self) -> None:
        pick = make_pick()
        line = duty.digest_line(pick.candidate, conclusion="讲得很实", status="挑了")
        self.assertIn(BVID, line)
        self.assertIn("讲得很实", line)
        self.assertEqual(line.count(" · "), 5)

    def test_comment_ledger_records_rpid(self) -> None:
        duty.comment_ledger(self.home, bvid=BVID, rpid=123456789, text="这期讲得挺细")
        text = (self.home / "comments.md").read_text(encoding="utf-8")
        self.assertIn("123456789", text)
        self.assertIn(BVID, text)

    def test_missing_rpid_is_written_honestly(self) -> None:
        """取不到 rpid 就写「未取到」，绝不编一个数字（复核认的就是这个号）。"""
        duty.comment_ledger(self.home, bvid=BVID, rpid=None, text="话")
        text = (self.home / "comments.md").read_text(encoding="utf-8")
        self.assertIn("未取到 rpid", text)

    def test_recent_comments(self) -> None:
        duty.comment_ledger(self.home, bvid=BVID, rpid=1, text="第一")
        self.assertEqual(len(duty.recent_comments(self.home)), 1)

    def test_paths_payload(self) -> None:
        paths = duty.paths_of(self.home)
        for key in ("home", "report_dir", "seen", "digest_dir", "notes_dir", "comments"):
            self.assertIn(key, paths)


class LedgerParseTest(unittest.TestCase):
    def test_tolerant_parse(self) -> None:
        from gateway.radar.verify import parse_ledger

        text = "\n".join([
            f"2026-10-26 · {BVID} · rpid 123456789 · 这期讲得挺细",
            f"- 2026-10-25 {BVID} rpid 987654321 root 111111111 楼中楼",
            "这行没有 BV 号 123456789",
            f"{BVID} 但没有长数字",
        ])
        rows = parse_ledger(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].rpid, 123456789)
        self.assertEqual(rows[1].root, 111111111)


class TranscribeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_speechless(self) -> None:
        from gateway.radar.transcribe import is_speechless
        self.assertTrue(is_speechless(""))
        self.assertTrue(is_speechless("字幕by某某"))
        self.assertFalse(is_speechless("这是一段真的转写出来的很长很长的正文内容"))

    def test_queue_blocks_second_job(self) -> None:
        from gateway.radar import transcribe

        jobs = {f"j{i}": transcribe.Job(job_id=f"j{i}", bvid=BVID, state="running")
                for i in range(1)}
        transcribe.save_jobs(self.home, jobs)
        allowed, why = transcribe.can_enqueue(self.home)
        self.assertFalse(allowed)
        self.assertIn("一轮最多排一条", why)

    def test_expired_job_does_not_block(self) -> None:
        from gateway.radar import transcribe
        stale = transcribe.Job(job_id="j1", bvid=BVID, state="running",
                               started=0.0)   # 很早以前
        transcribe.save_jobs(self.home, {"j1": stale})
        allowed, _ = transcribe.can_enqueue(self.home)
        self.assertTrue(allowed)

    def test_enqueue_records_job(self) -> None:
        from gateway.radar import transcribe

        class FakeRadar:
            async def transcribe_start(self, bvid):
                return {"ok": True, "job_id": "tj123"}

        import asyncio
        out = asyncio.run(transcribe.enqueue(FakeRadar(), self.home, BVID))
        self.assertTrue(out["queued"])
        self.assertIn("tj123", transcribe.load_jobs(self.home))

    def test_enqueue_reports_upstream_failure(self) -> None:
        from gateway.radar import transcribe

        class FakeRadar:
            async def transcribe_start(self, bvid):
                return {"ok": False, "error": "转写环境没装好"}

        import asyncio
        out = asyncio.run(transcribe.enqueue(FakeRadar(), self.home, BVID))
        self.assertFalse(out["queued"])
        self.assertIn("转写环境没装好", out["error"])


class VerifyReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "out").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_dedupes_previous_block(self) -> None:
        """一轮里复核被触发两次时，不能越叠越长（上游 #135）。"""
        from gateway.radar.verify import append_to_report

        path = self.home / "out" / f"{date.today().isoformat()}.md"
        path.write_text("# 刷B站\n\n正文\n", encoding="utf-8")
        block = "\n## 评论复核\n\n- 第一次\n"
        append_to_report(self.home, block)
        append_to_report(self.home, "\n## 评论复核\n\n- 第二次\n")
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("## 评论复核"), 1)
        self.assertIn("第二次", text)
        self.assertNotIn("第一次", text)

    def test_missing_report_is_not_created(self) -> None:
        """报告不存在就��硬造一份 —— 那会凭空多出一份空日报。"""
        from gateway.radar.verify import append_to_report

        append_to_report(self.home, "\n## 评论复核\n\n- x\n")
        self.assertEqual(list((self.home / "out").glob("*.md")), [])

    def test_verify_without_credential_says_so(self) -> None:
        import asyncio
        from gateway.radar.verify import verify_recent

        (self.home / "comments.md").write_text(f"{BVID} rpid 123456789\n", encoding="utf-8")
        report = asyncio.run(verify_recent(self.home))
        self.assertEqual(report.checked, [])
        self.assertIn("登录", report.note)

    def test_verify_without_ledger_says_so(self) -> None:
        import asyncio
        from gateway.radar.verify import verify_recent

        report = asyncio.run(verify_recent(self.home))
        self.assertIn("还没发过评论", report.note)


class VerifyOneTest(unittest.IsolatedAsyncioTestCase):
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, responses: dict[str, dict]) -> None:
            self.responses = responses

        async def get(self, url, params=None, headers=None, **kwargs):
            for key, payload in self.responses.items():
                if key in url:
                    return VerifyOneTest.FakeResponse(payload)
            return VerifyOneTest.FakeResponse({"code": -404})

    async def test_top_level_visible(self) -> None:
        from gateway.radar.verify import CommentRecord, verify_one

        client = self.FakeClient({
            "web-interface/view": {"code": 0, "data": {"aid": 12345}},
            "reply/main": {"code": 0, "data": {"replies": [
                {"rpid": 123456789, "like": 7},
            ]}},
        })
        result = await verify_one(CommentRecord(bvid=BVID, rpid=123456789),
                                  client=client, headers={})
        self.assertTrue(result.visible)
        self.assertEqual(result.likes, 7)

    async def test_not_found_is_not_claiming_failure(self) -> None:
        """看不到 ≠ 没发出去：措辞必须是「没查到」，不能写成失败。"""
        from gateway.radar.verify import CommentRecord, verify_one

        client = self.FakeClient({
            "web-interface/view": {"code": 0, "data": {"aid": 12345}},
            "reply/main": {"code": 0, "data": {"replies": []}},
        })
        result = await verify_one(CommentRecord(bvid=BVID, rpid=123456789),
                                  client=client, headers={})
        self.assertIsNone(result.visible)
        self.assertIn("不等于没发出去", result.note)
        self.assertNotIn("失败", result.note)


if __name__ == "__main__":
    unittest.main()
