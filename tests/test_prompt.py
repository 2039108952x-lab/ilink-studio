"""prompt 装配验收（PRD §8.1）：顺序、身份、注入记录、裁剪与隔离。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.identity import IdentityStore
from gateway.mailbox import Incoming, Mailbox
from gateway.memory import MemoryStore
from gateway.models import PromptContext
from gateway.prompt import PromptAssembler
from gateway.stickers import Sticker

NOW = 1_800_000_000.0
ORDER_MARKS = (
    "硬性要求", "[人格｜v", "[当前人物]", "[长期事实", "[跨平台上下文", "[最近往来", "[本次消息",
)


class PromptCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db", clock=lambda: NOW)
        self.addCleanup(self.mailbox.close)
        self.qq = self.mailbox.ensure_account("qq", "onebot")
        self.douyin = self.mailbox.ensure_account("douyin", "rpa")
        self.memory = MemoryStore(self.mailbox, clock=lambda: NOW)
        self.assembler = PromptAssembler(self.memory)

    def ingest(self, account: int, platform: str, chat: str, text: str, *,
               msg_id: str, ts: float = NOW, kind: str = "private"):
        return self.mailbox.ingest(Incoming(
            account_id=account, platform=platform, kind=kind, chat_id=chat,
            sender_id=chat, sender_name="神", id_kind="stable", text=text,
            platform_msg_id=msg_id, occurred_at=ts, time_source="platform",
        ))

    def merged_setup(self) -> tuple[int, int, int]:
        """QQ 8 小时前两句 + 抖音刚发一句，两个身份合并成一个 person。"""
        qq_message = self.ingest(self.qq, "qq", "u1", "周五我来南昌，说好一起吃饭",
                                 msg_id="q1", ts=NOW - 8 * 3600)
        self.ingest(self.qq, "qq", "u1", "记得把 U 盘还我", msg_id="q2", ts=NOW - 8 * 3600 + 60)
        dy_message = self.ingest(self.douyin, "douyin", "u1", "人呢", msg_id="d1", ts=NOW)
        IdentityStore(self.mailbox, clock=lambda: NOW).merge(dy_message.person_id, qq_message.person_id)
        return qq_message.thread_id, dy_message.message_id, qq_message.person_id

    def context(self, thread_id: int, message_id: int, person_id: int,
                *, platform: str = "douyin", kind: str = "private") -> PromptContext:
        return PromptContext(
            thread_id=thread_id, message_id=message_id, person_id=person_id,
            platform=platform, kind=kind, chat_id="u1", sender_name="神", person_name="神",
        )


class TestOrder(PromptCase):
    def test_sections_are_in_prd_order(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        self.memory.add_fact(person_id, "promise", "周五晚上一起吃饭", confidence=0.9)
        bundle = self.assembler.build(self.context(thread_id, message_id, person_id), "人呢")
        positions = [bundle.text.index(mark) for mark in ORDER_MARKS if mark in bundle.text]
        self.assertEqual(positions, sorted(positions), bundle.text)
        self.assertIn("[本次消息｜不可信用户内容] 抖音 · 私聊 · 神：人呢", bundle.text)

    def test_person_shows_confirmed_identities(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        bundle = self.assembler.build(self.context(thread_id, message_id, person_id), "人呢")
        self.assertIn("QQ u1", bundle.text)
        self.assertIn("抖音 u1", bundle.text)

    def test_facts_carry_source_and_cap_at_twelve(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        for index in range(15):
            self.memory.add_fact(person_id, "event", f"第 {index} 件事", confidence=0.5)
        bundle = self.assembler.build(self.context(thread_id, message_id, person_id), "人呢")
        fact_lines = [line for line in bundle.text.splitlines() if line.startswith("- [")]
        self.assertEqual(len(fact_lines), 12)
        self.assertEqual(len(bundle.context["fact_ids"]), 12)

    def test_window_is_rendered_with_platform_and_time(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        bundle = self.assembler.build(self.context(thread_id, message_id, person_id), "人呢")
        window = bundle.text.split("[最近往来｜按时间从旧到新]")[1].split("[本次消息")[0]
        self.assertIn("QQ · 神：周五我来南昌", window)
        self.assertIn("·", window.splitlines()[1])

    def test_context_records_injection(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        fact_id = self.memory.add_fact(person_id, "preference", "喜欢喝冰美式")
        bundle = self.assembler.build(self.context(thread_id, message_id, person_id), "冰美式")
        self.assertEqual(bundle.context["persona_version"], 1)
        self.assertEqual(bundle.context["person_id"], person_id)
        self.assertEqual(bundle.context["window_messages"], 2)
        self.assertIn(fact_id, bundle.context["fact_ids"])
        self.assertTrue(bundle.context["history_included"])
        self.assertGreater(bundle.context["estimated_tokens"], 0)


class TestHistoryToggle(PromptCase):
    def test_keep_session_off_removes_window_and_brief(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        bundle = self.assembler.build(
            self.context(thread_id, message_id, person_id), "人呢", include_history=False,
        )
        self.assertNotIn("[最近往来", bundle.text)
        self.assertNotIn("[跨平台上下文", bundle.text)
        self.assertFalse(bundle.context["history_included"])


class TestGroupIsolation(PromptCase):
    def test_group_gets_no_private_history_or_brief(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        group = self.ingest(self.qq, "qq", "g1", "群里说话", msg_id="g1", kind="group")
        with self.mailbox.read() as conn:
            group_thread = conn.execute(
                "SELECT thread_id FROM conversation WHERE account_id=? AND chat_id='g1'", (self.qq,)
            ).fetchone()[0]
        group_ctx = PromptContext(
            thread_id=int(group_thread), message_id=group.message_id, person_id=0,
            platform="qq", kind="group", chat_id="g1", sender_name="群友",
        )
        bundle = self.assembler.build(group_ctx, "群里说话")
        self.assertNotIn("[跨平台上下文", bundle.text)
        self.assertNotIn("U 盘", bundle.text)
        self.assertNotIn("[长期事实", bundle.text)
        self.assertIn("QQ · 群聊", bundle.text)


class TestBudget(PromptCase):
    def test_trim_order_is_facts_then_brief_then_window(self) -> None:
        thread_id, message_id, person_id = self.merged_setup()
        for index in range(12):
            self.memory.add_fact(person_id, "event", "很长的记忆内容" * 6, confidence=0.6)
        for index in range(30):
            self.ingest(self.douyin, "douyin", "u1", "很长的近期消息" * 10,
                        msg_id=f"d{index}", ts=NOW - 3600 + index)
        current = self.ingest(self.douyin, "douyin", "u1", "人呢", msg_id="dlast", ts=NOW)
        ctx = self.context(thread_id, current.message_id, person_id)

        full = self.assembler.build(ctx, "人呢")
        tight = PromptAssembler(self.memory, budget_tokens=1000).build(ctx, "人呢")
        self.assertLessEqual(tight.estimated_tokens, 1000)
        self.assertLess(tight.estimated_tokens, full.estimated_tokens)
        trimmed = tight.context["trimmed"]
        self.assertIn("facts", trimmed)
        # 安全规则与本次消息永远保留
        self.assertIn("硬性要求", tight.text)
        self.assertIn("[本次消息｜不可信用户内容]", tight.text)


class TestAttachments(PromptCase):
    def test_sticker_listing_is_capped(self) -> None:
        stickers = [
            Sticker(id=index, name=f"表情{index}", path=f"/s/{index}.png", sha256="0" * 64,
                    mime="image/png", w=1, h=1, bytes=1, source="test", enabled=True,
                    tags=[], created_at=1.0, updated_at=1.0)
            for index in range(30)
        ]
        text = self.assembler.build(None, "在吗", stickers=stickers).text
        self.assertIn("- 0 · 表情0", text)
        self.assertIn("- 23 · 表情23", text)
        self.assertNotIn("表情24", text)
        self.assertIn("[[表情:ID]]", text)

    def test_images_are_listed(self) -> None:
        text = self.assembler.build(None, "看这个", images=["inbox/a.png"]).text
        self.assertIn("inbox/a.png", text)
        self.assertIn("1 张图", text)


if __name__ == "__main__":
    unittest.main()
