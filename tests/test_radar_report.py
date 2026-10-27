"""日报 / 通知 / 记忆映射的测试。

不碰真实数据目录、不发网络请求：全部落在临时目录和假 store 上。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from gateway.radar import Candidate, CommentSample, SendOutcome
from gateway.radar.memory import RadarMemory, clean_fact, dedupe_facts, parse_context
from gateway.radar.notify import Notice, Notifier, mask_url, round_notice
from gateway.radar.pipeline import Judgment, Pick, RoundResult
from gateway.radar.report import QuotaState, list_reports, read, render, report_path, write


def _pick(item_id="1", *, worth=True, reason="评论在接梗", title="一条视频", url=None) -> Pick:
    c = Candidate(platform="douyin", item_id=item_id, title=title, author="某人",
                  duration_sec=15,
                  stats={"like": 12000, "comment": 3000, "collect": 2000, "share": 1000},
                  url=url or f"https://www.douyin.com/video/{item_id}")
    return Pick(candidate=c, score=9.5,
                comments=[CommentSample(text="笑死我了", likes=30000),
                          CommentSample(text="第二条", likes=100)],
                judgment=Judgment(item_id=item_id, worth=True if worth else None,
                                  confidence=0.9, reason=reason, tags=["梗"]))


def _round(picked=None, uncertain=None, dropped=None, note="") -> RoundResult:
    return RoundResult(platform="douyin", started_at=1_700_000_000.0,
                       finished_at=1_700_000_012.0, seen=10,
                       dropped=dropped or {"too_short": 2},
                       picked=picked or [], uncertain=uncertain or [], note=note)


class TestReportRender(unittest.TestCase):
    def test_header_counts_and_quota(self):
        text = render(_round([_pick()]), day=date(2026, 10, 26),
                      quota=QuotaState(per_day=5, used=2))
        self.assertIn("刷抖音 · 2026-10-26", text)
        self.assertIn("看了 10 条", text)
        self.assertIn("推 1 条", text)
        self.assertIn("已用 2/5", text)

    def test_picked_section_has_reason_and_comments(self):
        text = render(_round([_pick()]), day=date(2026, 10, 26))
        self.assertIn("## 值得看", text)
        self.assertIn("为什么：评论在接梗", text)
        self.assertIn("笑死我了", text)
        self.assertIn("https://www.douyin.com/video/1", text)

    def test_empty_round_says_so_instead_of_padding(self):
        text = render(_round(), day=date(2026, 10, 26))
        self.assertIn("宁可空着", text)

    def test_uncertain_section_reports_why(self):
        p = _pick(worth=False)
        p.judgment.error = "把握 0.20 低于阈值 0.6"
        text = render(_round(uncertain=[p]), day=date(2026, 10, 26))
        self.assertIn("## 判断不了", text)
        self.assertIn("把握 0.20", text)

    def test_dropped_table_uses_human_labels(self):
        text = render(_round(dropped={"too_short": 2, "only_likes": 1}), day=date(2026, 10, 26))
        self.assertIn("视频太短", text)
        self.assertIn("只有赞没人聊", text)


class TestReportIo(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(_round([_pick()]), Path(tmp), day=date(2026, 10, 26))
            self.assertEqual(path, report_path(Path(tmp), date(2026, 10, 26)))
            self.assertIn("值得看", path.read_text(encoding="utf-8"))
            self.assertEqual(read(Path(tmp), date(2026, 10, 26)), path.read_text(encoding="utf-8"))

    def test_missing_report_reads_as_empty_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read(Path(tmp), date(2026, 1, 1)), "")

    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(_round(), Path(tmp), day=date(2026, 10, 26))
            leftovers = list((Path(tmp) / "reports").glob("*.tmp"))
            self.assertEqual(leftovers, [])

    def test_list_reports_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(_round(), Path(tmp), day=date(2026, 10, 25))
            write(_round(), Path(tmp), day=date(2026, 10, 26))
            items = list_reports(Path(tmp))
            self.assertEqual([i["day"] for i in items], ["2026-10-26", "2026-10-25"])


class TestMaskUrl(unittest.TestCase):
    def test_sensitive_query_values_are_masked(self):
        masked = mask_url("https://www.iesdouyin.com/share/video/1/?share_sign=abc123&u_code=xyz")
        self.assertNotIn("abc123", masked)
        self.assertNotIn("xyz", masked)
        self.assertIn("iesdouyin.com", masked)

    def test_plain_url_keeps_its_query(self):
        self.assertIn("a=1", mask_url("https://example.com/x?a=1"))

    def test_empty_url_is_empty(self):
        self.assertEqual(mask_url(""), "")


class _FakeHttp:
    def __init__(self, code=200):
        self.code = code
        self.posted = []

    async def post(self, url, json=None):
        self.posted.append((url, json))
        return type("R", (), {"status_code": self.code})()


class TestNotifier(unittest.IsolatedAsyncioTestCase):
    def _tmp(self):
        return tempfile.mkdtemp()

    async def test_file_channel_writes_queue_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Notice(title="刷抖音：挑了 2 条", body="b", day="2026-10-26")
            out = await Notifier(Path(tmp), channel="file").send(note)
            self.assertTrue(out["ok"])
            files = list((Path(tmp) / "notifications").glob("*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["title"], "刷抖音：挑了 2 条")
            self.assertEqual(payload["deliver"], "file")

    async def test_toast_goes_to_the_same_queue_but_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = await Notifier(Path(tmp), channel="toast").send(Notice(title="t"))
            self.assertTrue(out["ok"])
            files = list((Path(tmp) / "notifications").glob("*toast*.json"))
            self.assertEqual(len(files), 1)

    async def test_both_writes_two_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = await Notifier(Path(tmp), channel="both").send(Notice(title="t"))
            self.assertTrue(out["ok"])
            self.assertEqual(len(list((Path(tmp) / "notifications").glob("*.json"))), 2)

    async def test_webhook_posts_masked_payload(self):
        fake = _FakeHttp()
        with tempfile.TemporaryDirectory() as tmp:
            note = Notice(title="t", url="https://x.com/v?share_sign=secret")
            out = await Notifier(Path(tmp), channel="webhook", webhook_url="https://hook.example/x",
                                 http=fake).send(note)
            self.assertTrue(out["parts"]["webhook"]["ok"])
            url, body = fake.posted[0]
            self.assertEqual(url, "https://hook.example/x")
            self.assertNotIn("secret", body["url"])

    async def test_webhook_without_url_fails_softly(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = await Notifier(Path(tmp), channel="webhook").send(Notice(title="t"))
            self.assertFalse(out["ok"])
            self.assertIn("没配", out["parts"]["webhook"]["error"])

    async def test_unknown_channel_falls_back_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = Notifier(Path(tmp), channel="carrier-pigeon")
            self.assertEqual(n.channel, "file")

    async def test_round_notice_names_the_best_pick(self):
        note = round_notice(_round([_pick()]), day="2026-10-26")
        self.assertIn("挑了 1 条", note.title)
        self.assertIn("一条视频", note.body)

    async def test_round_notice_with_nothing_picked(self):
        note = round_notice(_round(), day="2026-10-26")
        self.assertIn("没够格的", note.title)


class _FakeStore:
    def __init__(self):
        self.rows = []

    def has_fact(self, person_id, kind, content):
        return any(r["person_id"] == person_id and r["content"] == content for r in self.rows)

    def add_fact(self, person_id, kind, content, *, confidence=0.5):
        self.rows.append({"person_id": person_id, "kind": kind, "content": content,
                          "confidence": confidence})
        return len(self.rows)

    def facts(self, person_id, *, limit=50):
        return [type("F", (), {"content": r["content"]}) for r in self.rows
                if r["person_id"] == person_id][:limit]


class TestMemoryMapping(unittest.TestCase):
    def test_clean_fact_flattens_and_truncates(self):
        self.assertEqual(clean_fact("搬\n到  成都"), "搬 到 成都")
        self.assertLessEqual(len(clean_fact("啊" * 200)), 80)

    def test_dedupe_is_case_insensitive(self):
        self.assertEqual(dedupe_facts(["猫叫豆豆", "猫叫豆豆", " 猫叫豆豆 "]), ["猫叫豆豆"])

    def test_parse_context_reads_nested_shape(self):
        ctx = parse_context({"context": {"facts": ["搬到成都了"], "recent": [{"text": "在吗"}],
                                         "rounds": "3"}})
        self.assertEqual(ctx.facts, ["搬到成都了"])
        self.assertEqual(ctx.rounds, 3)
        self.assertEqual(len(ctx.recent), 1)

    def test_parse_context_accepts_flat_facts(self):
        self.assertEqual(parse_context({"facts": ["x"]}).facts, ["x"])

    def test_parse_context_survives_garbage(self):
        self.assertEqual(parse_context("nope").facts, [])
        self.assertEqual(parse_context({"context": 42}).facts, [])
        self.assertEqual(parse_context({}).facts, [])

    def test_sync_writes_new_facts_once(self):
        store = _FakeStore()
        mem = RadarMemory(store)
        ctx = parse_context({"context": {"facts": ["搬到成都了", "猫叫豆豆"]}})
        first = mem.sync(7, ctx)
        second = mem.sync(7, ctx)
        self.assertEqual(first["added"], 2)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(len(store.rows), 2)

    def test_sync_without_store_reports_error(self):
        out = RadarMemory(None).sync(1, parse_context({"facts": ["x"]}))
        self.assertIn("没有可用的记忆库", out["error"])

    def test_sync_stops_cleanly_when_person_missing(self):
        class _Missing(_FakeStore):
            def add_fact(self, person_id, kind, content, *, confidence=0.5):
                raise RuntimeError("人物不存在")

        out = RadarMemory(_Missing()).sync(99, parse_context({"facts": ["x"]}))
        self.assertEqual(out["added"], 0)
        self.assertIn("人物不存在", out["error"])

    def test_snapshot_reads_back_from_store(self):
        store = _FakeStore()
        mem = RadarMemory(store)
        mem.sync(3, parse_context({"facts": ["喜欢猫"]}))
        self.assertEqual(mem.snapshot(3).facts, ["喜欢猫"])


if __name__ == "__main__":
    unittest.main()
