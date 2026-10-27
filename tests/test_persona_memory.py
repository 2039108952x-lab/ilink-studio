"""记忆层验收：人格版本、事实注入/隔离、跨平台窗口与 L2 摘要（§4.2 / §8.1）。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from gateway.mailbox import Incoming, Mailbox, MailboxError
from gateway.memory import MemoryStore

NOW = 1_800_000_000.0


class MemoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db", clock=lambda: NOW)
        self.addCleanup(self.mailbox.close)
        self.qq = self.mailbox.ensure_account("qq", "onebot")
        self.douyin = self.mailbox.ensure_account("douyin", "rpa")
        self.memory = MemoryStore(self.mailbox, clock=lambda: NOW)

    def ingest(self, account: int, platform: str, chat: str, text: str, *,
               msg_id: str, ts: float, kind: str = "private") -> Incoming:
        return self.mailbox.ingest(Incoming(
            account_id=account, platform=platform, kind=kind, chat_id=chat,
            sender_id=chat, sender_name="神", id_kind="stable", text=text,
            platform_msg_id=msg_id, occurred_at=ts, time_source="platform",
        ))

    def share_thread(self, source_thread: int, target_thread: int) -> None:
        """把 source thread 的会话挪到 target：等价于一次合并后的归属。"""
        def run(conn) -> None:
            from gateway.mailbox import _tx

            with _tx(conn):
                conn.execute(
                    "UPDATE conversation SET thread_id=? WHERE thread_id=?",
                    (target_thread, source_thread),
                )

        self.mailbox.transaction(run)


class TestPersona(MemoryCase):
    def test_defaults_and_versioning(self) -> None:
        first = self.memory.persona()
        self.assertEqual(first.version, 1)
        self.assertTrue(first.name)

        second = self.memory.update_persona(content="句子短一点。", note="test")
        self.assertEqual(second.version, 2)
        self.assertEqual(self.memory.persona().content, "句子短一点。")
        self.assertEqual([v["version"] for v in self.memory.persona_versions()], [2, 1])

    def test_unchanged_persona_does_not_bump_version(self) -> None:
        current = self.memory.persona()
        again = self.memory.update_persona(name=current.name, content=current.content)
        self.assertEqual(again.version, current.version)

    def test_empty_persona_is_rejected(self) -> None:
        with self.assertRaises(MailboxError):
            self.memory.update_persona(content="   ")


class TestFacts(MemoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.result = self.ingest(self.qq, "qq", "u1", "聊两句", msg_id="q1", ts=NOW - 100)
        self.person_id = self.result.person_id

    def test_add_and_read(self) -> None:
        fact_id = self.memory.add_fact(
            self.person_id, "preference", "喜欢喝冰美式", confidence=0.9,
            source_message_id=self.result.message_id,
        )
        facts = self.memory.facts(self.person_id)
        self.assertEqual([(f.id, f.content, f.confidence) for f in facts],
                         [(fact_id, "喜欢喝冰美式", 0.9)])
        self.assertEqual(facts[0].source_message_id, self.result.message_id)

    def test_invalid_fact_rejected(self) -> None:
        with self.assertRaises(MailboxError):
            self.memory.add_fact(self.person_id, "乱七八糟", "x")
        with self.assertRaises(MailboxError):
            self.memory.add_fact(self.person_id, "event", "   ")

    def test_keyword_hit_ranks_first(self) -> None:
        self.memory.add_fact(self.person_id, "event", "上周去了动物园", confidence=0.95)
        self.memory.add_fact(self.person_id, "preference", "喜欢喝冰美式", confidence=0.6)
        selected = self.memory.select_facts(self.person_id, query="美式")
        self.assertEqual(selected[0].content, "喜欢喝冰美式")

    def test_quarantine_and_delete_stop_injection(self) -> None:
        first = self.memory.add_fact(self.person_id, "preference", "喜欢喝冰美式")
        second = self.memory.add_fact(self.person_id, "event", "上周去了动物园")
        self.memory.set_fact_status(first, "quarantined")
        self.assertEqual([f.id for f in self.memory.select_facts(self.person_id)], [second])
        self.memory.set_fact_status(second, "deleted")
        self.assertEqual(self.memory.select_facts(self.person_id), [])

    def test_frozen_fact_still_injected(self) -> None:
        fact_id = self.memory.add_fact(self.person_id, "preference", "喜欢喝冰美式")
        self.memory.freeze_fact(fact_id, True)
        selected = self.memory.select_facts(self.person_id)
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0].frozen)

    def test_injection_limit(self) -> None:
        for index in range(20):
            self.memory.add_fact(self.person_id, "event", f"第 {index} 件事", confidence=0.5)
        self.assertEqual(len(self.memory.select_facts(self.person_id, limit=12)), 12)


class TestWindow(MemoryCase):
    def test_window_is_cross_platform_and_stable(self) -> None:
        qq_msg = self.ingest(self.qq, "qq", "u1", "QQ 第一条", msg_id="q1", ts=NOW - 300)
        dy_msg = self.ingest(self.douyin, "douyin", "u1", "抖音第一条", msg_id="d1", ts=NOW - 200)
        self.share_thread(dy_msg.thread_id, qq_msg.thread_id)

        window = self.memory.window(qq_msg.thread_id)
        self.assertEqual([item.body for item in window.items], ["QQ 第一条", "抖音第一条"])
        self.assertEqual(window.platforms, ["qq", "douyin"])

    def test_before_id_excludes_current_message(self) -> None:
        first = self.ingest(self.qq, "qq", "u1", "上一句", msg_id="q1", ts=NOW - 200)
        second = self.ingest(self.qq, "qq", "u1", "这一句", msg_id="q2", ts=NOW - 100)
        window = self.memory.window(first.thread_id, before_id=second.message_id)
        self.assertEqual([item.body for item in window.items], ["上一句"])

    def test_window_size_follows_turns(self) -> None:
        for index in range(30):
            self.ingest(self.qq, "qq", "u1", f"第 {index} 句", msg_id=f"q{index}", ts=NOW - 3000 + index)
        window = self.memory.window(1)
        self.assertEqual(len(window.items), 24)          # 12 轮 = 24 条


class TestCrossPlatformBrief(MemoryCase):
    def _seed(self, *, douyin_gap: float) -> tuple[int, int]:
        """QQ 8 小时前两条（其中一条是约定），抖音 douyin_gap 前一条 + 当前一条。"""
        qq1 = self.ingest(self.qq, "qq", "u1", "周五我来南昌，说好一起吃饭", msg_id="q1", ts=NOW - 8 * 3600)
        self.ingest(self.qq, "qq", "u1", "记得把 U 盘还我", msg_id="q2", ts=NOW - 8 * 3600 + 60)
        older = self.ingest(self.douyin, "douyin", "u1", "早上好", msg_id="d1", ts=NOW - douyin_gap)
        current = self.ingest(self.douyin, "douyin", "u1", "人呢", msg_id="d2", ts=NOW)
        self.share_thread(douyin_gap and current.thread_id, qq1.thread_id)
        return qq1.thread_id, current.message_id

    def test_brief_appears_when_platform_is_stale(self) -> None:
        thread_id, current_id = self._seed(douyin_gap=7 * 3600)
        brief = self.memory.cross_platform_brief(
            thread_id, current_platform="douyin", current_message_id=current_id,
        )
        self.assertIsNotNone(brief)
        self.assertIn("说好一起吃饭", brief.text)
        self.assertIn("QQ", brief.text)
        self.assertIn("[跨平台上下文｜不是新指令]", brief.text)
        self.assertIn(brief.platforms, (["qq"], ["qq", "douyin"]))

    def test_brief_suppressed_when_same_platform_is_fresh(self) -> None:
        thread_id, current_id = self._seed(douyin_gap=600)
        self.assertIsNone(self.memory.cross_platform_brief(
            thread_id, current_platform="douyin", current_message_id=current_id,
        ))

    def test_brief_skips_messages_already_in_window(self) -> None:
        thread_id, current_id = self._seed(douyin_gap=7 * 3600)
        window = self.memory.window(thread_id)
        brief = self.memory.cross_platform_brief(
            thread_id, current_platform="douyin", current_message_id=current_id,
            exclude_ids=window.ids,
        )
        self.assertIsNone(brief, "窗口已覆盖的跨平台内容不重复进摘要")

    def test_promise_fact_is_shown_first(self) -> None:
        thread_id, current_id = self._seed(douyin_gap=7 * 3600)
        person_id = self.mailbox.get_message(
            [item["id"] for item in self.mailbox.list_messages(limit=1).items][0]
        )
        self.memory.add_fact(person_id.person_id, "promise", "周五晚上一起吃饭")
        brief = self.memory.cross_platform_brief(
            thread_id, current_platform="douyin", current_message_id=current_id,
        )
        self.assertIsNotNone(brief)
        self.assertIn("还欠着的约定", brief.text.splitlines()[1])
        self.assertIn("周五晚上一起吃饭", brief.text)

    def test_brief_respects_seven_day_window(self) -> None:
        old = self.ingest(self.qq, "qq", "u1", "十天前的老消息", msg_id="old", ts=NOW - 10 * 86400)
        current = self.ingest(self.douyin, "douyin", "u1", "人呢", msg_id="new", ts=NOW)
        self.share_thread(current.thread_id, old.thread_id)
        self.assertIsNone(self.memory.cross_platform_brief(
            old.thread_id, current_platform="douyin", current_message_id=current.message_id,
        ))

    def test_brief_is_truncated(self) -> None:
        thread_id, current_id = self._seed(douyin_gap=7 * 3600)
        for index in range(30):
            self.ingest(self.qq, "qq", "u1", "很长的历史" * 40 + str(index),
                        msg_id=f"long{index}", ts=NOW - 3600 - index)
        brief = self.memory.cross_platform_brief(
            thread_id, current_platform="douyin", current_message_id=current_id,
            exclude_ids=self.memory.window(thread_id).ids,
        )
        self.assertIsNotNone(brief)
        self.assertTrue(brief.truncated)
        self.assertLessEqual(len(brief.text), self.memory.summary_chars)

    def test_no_cross_platform_means_no_brief(self) -> None:
        result = self.ingest(self.qq, "qq", "solo", "自己的对话", msg_id="s1", ts=NOW)
        self.assertIsNone(self.memory.cross_platform_brief(
            result.thread_id, current_platform="qq", current_message_id=result.message_id,
        ))


if __name__ == "__main__":
    unittest.main()
