"""AC-16 / AC-17：表情发送的四种结果，以及未知 ID / 本地路径的拒绝。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gateway.brain import Reply, resolve_sticker
from gateway.mailbox import Incoming, Mailbox
from gateway.models import ChatKind, InboundMessage, Platform, SendResult
from gateway.router import Router
from gateway.stickers import StickerStore
from tests.test_stickers import png


class Adapter:
    """可按能力/结果定制的假适配器。"""

    platform = Platform.QQ

    def __init__(self, capability: str = "supported", outcome: str = "ok") -> None:
        self.capability = capability
        self.outcome = outcome
        self.sent = []

    def bind(self, handler, *, checkpoints=None) -> None:
        self.handler = handler

    def capabilities(self) -> dict:
        return {"image_send": self.capability}

    async def send(self, out):
        if self.outcome == "raise":
            raise TimeoutError("平台没有回执")
        self.sent.append(out)
        if self.outcome == "reject":
            return SendResult(ok=False, error="平台拒绝：不支持该消息类型")
        return SendResult(ok=True, msg_id="p1")


class Brain:
    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.calls = 0

    async def respond(self, ctx, text, attachments=None) -> Reply:
        self.calls += 1
        return self.reply


class StickerSendCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.mailbox = Mailbox(root / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account = self.mailbox.ensure_account("qq", "onebot")
        self.store = StickerStore(self.mailbox, root / "stickers")
        source = root / "无语.png"
        source.write_bytes(png())
        self.sticker = self.store.import_file(source, name="无语", tags=["白眼"]).sticker
        self.counter = 0

    def message(self, text: str = "笑一个") -> InboundMessage:
        self.counter += 1
        return InboundMessage(
            platform=Platform.QQ, kind=ChatKind.PRIVATE, chat_id="u1",
            sender_id="u1", sender_name="神", text=text,
            msg_id=f"m{self.counter}", sender_id_kind="stable",
        )

    def run_router(self, adapter: Adapter, brain: Brain,
                   message: InboundMessage | None = None) -> None:
        router = Router(brain, {Platform.QQ: adapter}, store=self.mailbox, allow_all=True)

        async def scenario() -> None:
            await router.on_inbound(message or self.message())
            await router.drain()

        asyncio.run(scenario())

    def outbox(self, outbox_id: int = 1) -> dict:
        return self.mailbox.get_outbox(outbox_id)


class TestAck16(StickerSendCase):
    def test_supported_sends_image(self) -> None:
        adapter = Adapter("supported")
        self.run_router(adapter, Brain(Reply(text="好啊", sticker=self.sticker)))
        task = self.outbox()
        self.assertEqual((task["state"], task["requested_kind"], task["effective_kind"]),
                         ("sent", "image", "image"))
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(len(adapter.sent[0].attachments), 1)
        self.assertEqual(adapter.sent[0].text, "好啊")

    def test_unsupported_degrades_to_text(self) -> None:
        adapter = Adapter("unsupported")
        self.run_router(adapter, Brain(Reply(text="好啊", sticker=self.sticker)))
        task = self.outbox()
        self.assertEqual((task["state"], task["requested_kind"], task["effective_kind"]),
                         ("degraded", "image", "text"))
        self.assertEqual(task["error_code"], "image_unsupported")
        self.assertIn("降级", task["error_detail_safe"])
        self.assertEqual(adapter.sent[0].text, "好啊（无语）")
        self.assertEqual(adapter.sent[0].attachments, [])

    def test_unknown_capability_is_not_support(self) -> None:
        adapter = Adapter("unknown")
        self.run_router(adapter, Brain(Reply(text="好", sticker=self.sticker)))
        task = self.outbox()
        self.assertEqual(task["state"], "degraded")
        self.assertEqual(task["error_code"], "image_unknown")
        self.assertEqual(task["effective_kind"], "text")

    def test_explicit_rejection_is_failed(self) -> None:
        adapter = Adapter("supported", "reject")
        self.run_router(adapter, Brain(Reply(text="好", sticker=self.sticker)))
        task = self.outbox()
        self.assertEqual((task["state"], task["error_code"]), ("failed", "send_rejected"))
        self.assertIn("不支持该消息类型", task["error_detail_safe"])

    def test_timeout_is_unknown_and_not_retried(self) -> None:
        adapter = Adapter("supported", "raise")
        brain = Brain(Reply(text="好", sticker=self.sticker))
        self.run_router(adapter, brain)
        task = self.outbox()
        self.assertEqual((task["state"], task["error_code"]), ("unknown", "send_exception"))
        self.assertEqual(task["attempt_count"], 1)

        # 再跑一轮也不该重发
        self.run_router(adapter, brain)
        self.assertEqual(self.outbox()["attempt_count"], 1)
        self.assertEqual(len(adapter.sent), 0)

    def test_text_only_reply_is_plain_sent(self) -> None:
        adapter = Adapter("supported")
        self.run_router(adapter, Brain(Reply(text="就一句话")))
        task = self.outbox()
        self.assertEqual((task["state"], task["requested_kind"], task["effective_kind"]),
                         ("sent", "text", "text"))


class TestAck17(StickerSendCase):
    def test_unknown_id_is_rejected_with_visible_note_and_audit(self) -> None:
        adapter = Adapter("supported")
        reply = resolve_sticker("嗯[[表情:999]]", self.store)
        self.assertEqual(reply.error, "sticker_unknown")
        self.run_router(adapter, Brain(Reply(
            text=reply.text, sticker=reply.sticker,
            sticker_error=reply.error, sticker_raw=reply.raw,
        )))

        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(adapter.sent[0].attachments, [], "不能发送任何文件")
        self.assertIn("（表情「999」不可用）", adapter.sent[0].text, "不能静默删标记")
        events = [row["event_type"] for row in self.mailbox.list_audit()]
        self.assertIn("sticker_rejected", events)
        detail = [row["payload_json"] for row in self.mailbox.list_audit()
                  if row["event_type"] == "sticker_rejected"][0]
        self.assertIn("sticker_unknown", detail)

    def test_path_like_reference_never_touches_files(self) -> None:
        missing = str(Path(self._tmp.name) / "definitely-not-here.png")
        reply = resolve_sticker(f"看这个[[表情:{missing}]]", self.store)
        self.assertEqual(reply.error, "sticker_path_rejected")
        self.assertIsNone(reply.sticker)
        self.assertIn("不可用", reply.text)

        adapter = Adapter("supported")
        self.run_router(adapter, Brain(Reply(
            text=reply.text, sticker=reply.sticker,
            sticker_error=reply.error, sticker_raw=reply.raw,
        )))
        self.assertEqual(adapter.sent[0].attachments, [])
        self.assertFalse(Path(missing).exists())

    def test_disabled_sticker_is_not_sent(self) -> None:
        self.store.set_enabled(self.sticker.id, False)
        adapter = Adapter("supported")
        reply = resolve_sticker("[[表情:1]]", self.store)
        self.assertEqual(reply.error, "sticker_disabled")
        self.run_router(adapter, Brain(Reply(
            text=reply.text, sticker=reply.sticker,
            sticker_error=reply.error, sticker_raw=reply.raw,
        )))
        self.assertEqual(adapter.sent[0].attachments, [])
        self.assertIn("不可用", adapter.sent[0].text)

    def test_multiple_references_send_one_image(self) -> None:
        adapter = Adapter("supported")
        reply = resolve_sticker("[[表情:1]][[表情:1]]", self.store)
        self.assertEqual(reply.error, "sticker_multiple")
        self.assertIsNotNone(reply.sticker)
        self.run_router(adapter, Brain(Reply(
            text=reply.text, sticker=reply.sticker,
            sticker_error=reply.error, sticker_raw=reply.raw,
        )))
        self.assertEqual(len(adapter.sent[0].attachments), 1)


if __name__ == "__main__":
    unittest.main()
