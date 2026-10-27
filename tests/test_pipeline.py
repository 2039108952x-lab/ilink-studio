"""入站流水线验收：四个适配器写同一张库，以及暂停/历史/发送不确定的语义。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import Adapter
from gateway.brain import Reply
from gateway.mailbox import Incoming, Mailbox
from gateway.models import (
    Attachment, ChatKind, InboundMessage, OutboundMessage, Platform, SendResult,
)
from gateway.router import Router


class RecordingAdapter(Adapter):
    def __init__(self, platform: Platform, *, outcome: str = "ok") -> None:
        super().__init__()
        self.platform = platform
        self.outcome = outcome
        self.sent: list[OutboundMessage] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> SendResult:
        if self.outcome == "raise":
            raise TimeoutError("平台没有回执")
        self.sent.append(msg)
        if self.outcome == "reject":
            return SendResult(ok=False, error="平台拒绝：对方设置了隐私限制")
        return SendResult(ok=True, msg_id=f"{self.platform.value}-{len(self.sent)}")


class CountingBrain:
    def __init__(self, reply: str = "收到") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def respond(self, ctx, text, attachments=None) -> Reply:
        self.calls.append((ctx.platform, text))
        return Reply(text=self.reply)


class PipelineCase(unittest.TestCase):
    accounts = {
        Platform.QQ: ("qq", "onebot"),
        Platform.WECHAT: ("wechat", "official"),
        Platform.BILIBILI: ("bilibili", "cookie"),
        Platform.DOUYIN: ("douyin", "rpa"),
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        for platform, channel in self.accounts.values():
            self.mailbox.ensure_account(platform, channel)
        self.brain = CountingBrain()
        self.adapters = {p: RecordingAdapter(p) for p in self.accounts}
        self.router = Router(
            self.brain, self.adapters, store=self.mailbox, allow_all=True
        )

    def message(self, platform: Platform, **over) -> InboundMessage:
        base = dict(
            platform=platform, kind=ChatKind.PRIVATE, chat_id="10001",
            sender_id="u1", sender_name="神", text="在吗", msg_id="m1",
            sender_id_kind="stable",
        )
        base.update(over)
        return InboundMessage(**base)

    def run_msgs(self, *msgs: InboundMessage) -> None:
        async def scenario() -> None:
            for msg in msgs:
                await self.router.on_inbound(msg)
            await self.router.drain()

        asyncio.run(scenario())


class TestFourPlatformsOneStore(PipelineCase):
    def test_all_four_adapters_write_the_same_database(self) -> None:
        self.run_msgs(*[self.message(platform, msg_id=f"{platform.value}-1")
                        for platform in self.accounts])
        items = self.mailbox.list_messages(limit=10, direction="in").items
        self.assertEqual(len(items), 4)
        self.assertEqual({item["platform"] for item in items}, {"qq", "wechat", "bilibili", "douyin"})
        self.assertEqual({item["storage_class"] for item in items}, {"full"})
        self.assertEqual({item["time_source"] for item in items}, {"received"})
        for adapter in self.adapters.values():
            self.assertEqual(len(adapter.sent), 1)
            self.assertTrue(adapter.sent[0].text)

    def test_comment_branch_is_isolated_from_private(self) -> None:
        self.run_msgs(
            self.message(Platform.BILIBILI, kind=ChatKind.PRIVATE, chat_id="42", msg_id="p1"),
            self.message(Platform.BILIBILI, kind=ChatKind.COMMENT, chat_id="1:0:2", msg_id="c1"),
        )
        private = self.mailbox.list_messages(limit=10, kind="private", direction="in").items
        comment = self.mailbox.list_messages(limit=10, kind="comment", direction="in").items
        self.assertEqual(len(private), 1)
        self.assertEqual(len(comment), 1)
        self.assertNotEqual(private[0]["thread_id"], comment[0]["thread_id"])


class TestBacklog(PipelineCase):
    """AC-09：首次同步只登记历史，模型调用数为 0。"""

    def test_backlog_never_calls_the_model(self) -> None:
        self.run_msgs(
            self.message(Platform.QQ, msg_id="old-1", text="昨天的消息", backlog=True),
            self.message(Platform.WECHAT, msg_id="old-2", text="上周的消息", backlog=True),
        )
        self.assertEqual(self.brain.calls, [])
        for adapter in self.adapters.values():
            self.assertEqual(adapter.sent, [])
        items = self.mailbox.list_messages(limit=10).items
        self.assertEqual({item["processing_status"] for item in items}, {"skipped"})
        self.assertEqual({item["skip_reason"] for item in items}, {"backlog"})
        self.assertEqual({item["reply_policy"] for item in items}, {"never"})
        self.assertEqual({item["body"] for item in items}, {"昨天的消息", "上周的消息"})

    def test_backlog_does_not_block_new_messages(self) -> None:
        self.run_msgs(self.message(Platform.QQ, msg_id="old", backlog=True))
        self.run_msgs(self.message(Platform.QQ, msg_id="new", text="新消息"))
        self.assertEqual(self.brain.calls, [("qq", "新消息")])
        self.assertEqual(len(self.adapters[Platform.QQ].sent), 1)


class TestPause(PipelineCase):
    """AC-22：暂停回复仍落库，但不再生成；停止连接才停收。"""

    def test_paused_messages_are_stored_not_answered(self) -> None:
        self.router._paused_cb = lambda: True
        self.run_msgs(self.message(Platform.QQ, msg_id="p1", text="暂停期间的消息"))
        self.assertEqual(self.brain.calls, [])
        self.assertEqual(self.adapters[Platform.QQ].sent, [])
        stored = self.mailbox.list_messages(limit=10).items[0]
        self.assertEqual(stored["body"], "暂停期间的消息")
        self.assertEqual(stored["skip_reason"], "reply_paused")

    def test_resume_answers_new_messages_only(self) -> None:
        self.router._paused_cb = lambda: True
        self.run_msgs(self.message(Platform.QQ, msg_id="p1", text="旧消息"))
        self.router._paused_cb = lambda: False
        self.run_msgs(self.message(Platform.QQ, msg_id="p2", text="新消息"))
        self.assertEqual(self.brain.calls, [("qq", "新消息")])


class TestSendOutcomes(PipelineCase):
    """发送结果只有 sent/failed 是确定的，其余一律 unknown 且不自动重发。"""

    def test_exception_becomes_unknown_and_needs_review(self) -> None:
        self.adapters[Platform.QQ].outcome = "raise"
        self.run_msgs(self.message(Platform.QQ))
        inbound = self.mailbox.list_messages(limit=10, direction="in").items
        statuses = {item["processing_status"] for item in inbound}
        self.assertEqual(statuses, {"needs_review"})
        outbox = self.mailbox.get_outbox(1)
        self.assertEqual(outbox["state"], "unknown")
        self.assertEqual(outbox["error_code"], "send_exception")
        # 再 drain 一次也不会自动重发
        self.run_msgs()
        self.assertEqual(self.mailbox.get_outbox(1)["state"], "unknown")
        self.assertEqual(self.mailbox.get_outbox(1)["attempt_count"], 1)

    def test_explicit_rejection_is_failed(self) -> None:
        self.adapters[Platform.QQ].outcome = "reject"
        self.run_msgs(self.message(Platform.QQ))
        outbox = self.mailbox.get_outbox(1)
        self.assertEqual(outbox["state"], "failed")
        self.assertIn("隐私限制", outbox["error_detail_safe"])
        account = self.mailbox.account_id("qq")
        with self.mailbox.read() as conn:
            health = conn.execute(
                "SELECT send_health,last_send_error_safe FROM account WHERE id=?", (account,)
            ).fetchone()
        self.assertEqual(health["send_health"], "failed")
        self.assertIn("隐私限制", health["last_send_error_safe"])

    def test_success_marks_account_health(self) -> None:
        self.run_msgs(self.message(Platform.QQ))
        account = self.mailbox.account_id("qq")
        with self.mailbox.read() as conn:
            row = conn.execute("SELECT send_health FROM account WHERE id=?", (account,)).fetchone()
        self.assertEqual(row["send_health"], "success")


class TestNeedsReview(PipelineCase):
    """§7.6.5：人工确认待核对消息后才允许生成回复。"""

    def test_confirm_then_reply(self) -> None:
        result = self.mailbox.ingest(Incoming(
            account_id=self.mailbox.account_id("douyin"), platform="douyin", kind="private",
            chat_id="demo_friend", sender_id="demo_friend", sender_name="demo_friend",
            id_kind="opaque", text="这条要人看一眼", platform_msg_id="dy-1",
            status="needs_review",
        ))
        self.run_msgs()
        self.assertEqual(self.brain.calls, [])
        self.mailbox.confirm_message(result.message_id)
        self.run_msgs()
        self.assertEqual(len(self.brain.calls), 1)
        self.assertEqual(len(self.adapters[Platform.DOUYIN].sent), 1)
        with self.mailbox.read() as conn:
            events = [row[0] for row in conn.execute(
                "SELECT event_type FROM audit_event WHERE entity_id=?", (str(result.message_id),)
            )]
        self.assertIn("message_confirmed", events)


class TestWorkerMode(PipelineCase):
    def test_on_inbound_returns_before_generation(self) -> None:
        """适配器通知不能被整段模型生成阻塞（PRD §6.3）。"""

        class SlowBrain:
            def __init__(self) -> None:
                self.finished = asyncio.Event()

            async def respond(self, ctx, text, attachments=None) -> Reply:
                await asyncio.sleep(1.0)
                self.finished.set()
                return Reply(text="慢回复")

        async def scenario() -> None:
            self.router._brain = SlowBrain()
            await self.router.start()
            try:
                started = asyncio.get_running_loop().time()
                await self.router.on_inbound(self.message(Platform.QQ, msg_id="w1"))
                elapsed = asyncio.get_running_loop().time() - started
                # 生成要 1 秒；把阈值留在 0.5 秒，既容得下落库抖动，又能抓住真正的阻塞。
                self.assertLess(elapsed, 0.5, "on_inbound 只该落库并唤醒 worker")
                await asyncio.wait_for(self.router._brain.finished.wait(), timeout=5)
                await self.router.drain(timeout=5)
            finally:
                await self.router.stop()

        asyncio.run(scenario())
        self.assertEqual(len(self.adapters[Platform.QQ].sent), 1)

    def test_restart_recovers_claimed_message(self) -> None:
        """进程在处理中死掉：重启后同一条会被重新处理（还没发出去）。"""
        self.mailbox.ingest(Incoming(
            account_id=self.mailbox.account_id("qq"), platform="qq", kind="private",
            chat_id="10001", sender_id="u1", sender_name="神", id_kind="stable",
            text="处理中崩了", platform_msg_id="crash-1",
        ))
        self.mailbox.claim_message("dead-worker")

        async def scenario() -> None:
            await self.router.start()      # start() 里先做崩溃恢复
            try:
                await self.router.drain(timeout=5)
            finally:
                await self.router.stop()

        asyncio.run(scenario())
        self.assertEqual(len(self.brain.calls), 1)
        self.assertEqual(len(self.adapters[Platform.QQ].sent), 1)


class TestAttachmentWithoutText(PipelineCase):
    """真机事故：图片/表情这类纯附件消息没有正文，以前带着 storage_class="full"
    进 ingest，被 mailbox 判成 invalid_body —— 整条消息丢掉，日志还刷一行
    「消息落库失败，已暂停 AI 处理」，后面所有消息跟着停摆。"""

    def test_attachment_only_message_is_stored_as_metadata(self) -> None:
        self.run_msgs(self.message(
            Platform.WECHAT, msg_id="img-1", text="",
            attachments=[Attachment(kind="image", path="C:/tmp/a.png", name="a.png")],
        ))
        items = self.mailbox.list_messages(limit=10, direction="in").items
        self.assertEqual(len(items), 1, "纯附件消息也要入收件箱，不能整条丢掉")
        stored = items[0]
        self.assertEqual(stored["storage_class"], "metadata_only")
        self.assertIsNone(stored["body"])
        self.assertEqual(stored["skip_reason"], "empty_body")
        self.assertEqual(stored["reply_policy"], "never")
        self.assertEqual(len(stored["attachments"]), 1, "附件元数据照旧保存")
        self.assertEqual(stored["attachments"][0]["kind"], "image")
        self.assertFalse(self.router.health["degraded"], "一条脏消息不该把网关标成数据库降级")
        self.assertEqual(self.brain.calls, [], "没正文的消息不交给模型猜")
        self.assertEqual(self.adapters[Platform.WECHAT].sent, [])

    def test_blank_text_with_attachment_does_not_wedge_the_queue(self) -> None:
        self.run_msgs(
            self.message(Platform.QQ, msg_id="blank", text="   ",
                         attachments=[Attachment(kind="image", url="https://example.com/a.png")]),
            self.message(Platform.QQ, msg_id="after", text="后面的消息"),
        )
        self.assertEqual(self.brain.calls, [("qq", "后面的消息")], "后面的消息必须照常处理")
        self.assertEqual(len(self.adapters[Platform.QQ].sent), 1)


if __name__ == "__main__":
    unittest.main()
