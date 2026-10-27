"""核心骨架的单元测试：不碰网络、不碰真实平台。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gateway import config as cfg_mod
from gateway.adapters.base import Adapter
from gateway.brain import Reply
from gateway.mailbox import Mailbox
from gateway.models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult
from gateway.router import Router


class FakeAdapter(Adapter):
    platform = Platform.QQ

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutboundMessage] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send(self, msg: OutboundMessage) -> SendResult:
        self.sent.append(msg)
        return SendResult(ok=True, msg_id=f"fake-{len(self.sent)}")


class FakeBrain:
    def __init__(self, reply: str = "收到") -> None:
        self.reply = reply
        self.calls: list[tuple[int, str]] = []

    async def respond(self, ctx, text, attachments=None) -> Reply:
        self.calls.append((ctx.thread_id, text))
        return Reply(text=self.reply)


def make_msg(**kw) -> InboundMessage:
    base = dict(
        platform=Platform.QQ,
        kind=ChatKind.PRIVATE,
        chat_id="10001",
        sender_id="u1",
        sender_name="神",
        text="在吗",
        msg_id="m1",
    )
    base.update(kw)
    return InboundMessage(**base)


class TestModels(unittest.TestCase):
    def test_session_key_is_stable_and_excludes_sender(self) -> None:
        a = make_msg(sender_id="u1")
        b = make_msg(sender_id="u2", msg_id="m2")
        self.assertEqual(a.session_key, b.session_key)
        self.assertEqual(a.session_key, "qq:private:10001")

    def test_session_key_differs_by_kind(self) -> None:
        a = make_msg(kind=ChatKind.GROUP)
        b = make_msg(kind=ChatKind.PRIVATE)
        self.assertNotEqual(a.session_key, b.session_key)


class TestRouter(unittest.TestCase):
    """流水线行为：落库 → 租约 → 模型 → outbox → 发送。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.mailbox.ensure_account("qq", "onebot")

    def make_router(self, brain, adapter=None, **kw):
        adapter = adapter or FakeAdapter()
        return Router(brain, {Platform.QQ: adapter}, store=self.mailbox, **kw), adapter

    def run_msgs(self, router, *msgs) -> None:
        async def scenario() -> None:
            for msg in msgs:
                await router.on_inbound(msg)
            await router.drain()

        asyncio.run(scenario())

    def only_message(self) -> dict:
        return self.mailbox.list_messages(limit=1).items[0]

    def test_allowlist_blocks_stranger(self) -> None:
        router, adapter = self.make_router(FakeBrain(), allowlist={"qq": {"u1"}})
        self.run_msgs(router, make_msg(sender_id="intruder", text="我的口令是 42"))
        self.assertEqual(router._brain.calls, [])
        self.assertEqual(adapter.sent, [])
        # 非白名单正文为 0（AC-05）
        stored = self.only_message()
        self.assertIsNone(stored["body"])
        self.assertEqual(stored["skip_reason"], "not_allowlisted")
        self.assertEqual(self.mailbox.list_messages(query="口令").items, [])

    def test_allowlist_lets_known_user_through(self) -> None:
        router, adapter = self.make_router(FakeBrain(), allowlist={"qq": {"u1"}})
        self.run_msgs(router, make_msg())
        self.assertEqual(len(router._brain.calls), 1)
        self.assertEqual(adapter.sent[0].text, "收到")
        self.assertEqual(adapter.sent[0].reply_to, "m1")
        self.assertEqual(self.mailbox.get_message(1).reply_policy, "normal")

    def test_allow_all_shortcut(self) -> None:
        router, _ = self.make_router(FakeBrain(), allow_all=True)
        self.run_msgs(router, make_msg(sender_id="whoever"))
        self.assertEqual(len(router._brain.calls), 1)

    def test_duplicate_msg_id_ignored(self) -> None:
        router, _ = self.make_router(FakeBrain(), allow_all=True)
        self.run_msgs(router, make_msg(msg_id="dup"))
        self.run_msgs(router, make_msg(msg_id="dup"))
        self.assertEqual(len(router._brain.calls), 1, "平台重投同一条只该处理一次")

    def test_empty_reply_not_sent(self) -> None:
        router, adapter = self.make_router(FakeBrain(reply=""), allow_all=True)
        self.run_msgs(router, make_msg())
        self.assertEqual(adapter.sent, [])
        self.assertEqual(self.only_message()["skip_reason"], "empty_reply")

    def test_backlog_is_stored_without_reply(self) -> None:
        router, adapter = self.make_router(FakeBrain(), allow_all=True)
        self.run_msgs(router, make_msg(backlog=True))
        self.assertEqual(adapter.sent, [])
        self.assertEqual(router._brain.calls, [])
        stored = self.only_message()
        self.assertEqual(stored["processing_status"], "skipped")
        self.assertEqual(stored["skip_reason"], "backlog")
        self.assertEqual(stored["body"], "在吗")

    def test_pause_still_stores_but_does_not_generate(self) -> None:
        router, adapter = self.make_router(FakeBrain(), allow_all=True, paused=lambda: True)
        self.run_msgs(router, make_msg())
        self.assertEqual(adapter.sent, [])
        self.assertEqual(router._brain.calls, [])
        stored = self.only_message()
        self.assertEqual(stored["body"], "在吗")
        self.assertEqual(stored["skip_reason"], "reply_paused")

    def test_provisional_sender_keeps_no_body(self) -> None:
        router, _ = self.make_router(FakeBrain(), allow_all=True)
        self.run_msgs(router, make_msg(sender_id_kind="provisional", text="我是你朋友"))
        stored = self.only_message()
        self.assertIsNone(stored["body"])
        self.assertEqual(stored["skip_reason"], "provisional_identity")

    def test_brain_error_lands_as_failed(self) -> None:
        class BoomBrain:
            async def respond(self, ctx, text, attachments=None) -> Reply:
                raise RuntimeError("模型挂了")

        router, adapter = self.make_router(BoomBrain(), allow_all=True)
        self.run_msgs(router, make_msg())
        stored = self.only_message()
        self.assertEqual(stored["processing_status"], "failed")
        self.assertEqual(stored["error_code"], "brain_error")
        self.assertEqual(len(adapter.sent), 1, "挂了也要跟对方说一句，别装死")
        self.assertIn("卡了一下", adapter.sent[0].text)

    def test_failure_notice_is_throttled(self) -> None:
        class BoomBrain:
            async def respond(self, ctx, text, attachments=None) -> Reply:
                raise RuntimeError("模型挂了")

        router, adapter = self.make_router(BoomBrain(), allow_all=True)
        self.run_msgs(router, make_msg(msg_id="f1"), make_msg(msg_id="f2"), make_msg(msg_id="f3"))
        self.assertEqual(len(adapter.sent), 1, "90 秒内只提醒一次")

    def test_send_to_missing_platform_fails_softly(self) -> None:
        router, _ = self.make_router(FakeBrain(), allow_all=True)
        result = asyncio.run(router.send(Platform.DOUYIN, ChatKind.PRIVATE, "x", "hi"))
        self.assertFalse(result.ok)
        self.assertIn("douyin", result.error)

    def test_same_thread_is_serialized(self) -> None:
        order: list[str] = []

        class SlowBrain:
            async def respond(self, ctx, text, attachments=None) -> Reply:
                order.append(f"start:{text}")
                await asyncio.sleep(0.02)
                order.append(f"end:{text}")
                return Reply(text=f"re:{text}")

        router, _ = self.make_router(SlowBrain(), allow_all=True)

        async def burst() -> None:
            await asyncio.gather(
                router.on_inbound(make_msg(text="a", msg_id="1")),
                router.on_inbound(make_msg(text="b", msg_id="2")),
            )
            await router.drain()

        asyncio.run(burst())
        # 谁先落库谁先处理，但同一个 thread 的两条绝不能交错。
        self.assertEqual(len(order), 4)
        pairs = list(zip(order[::2], order[1::2]))
        for start, end in pairs:
            self.assertTrue(start.startswith("start:"))
            self.assertEqual(start.replace("start:", ""), end.replace("end:", ""))
        self.assertEqual({start.split(":")[1] for start, _ in pairs}, {"a", "b"})

    def test_outbox_is_idempotent_per_inbound(self) -> None:
        router, adapter = self.make_router(FakeBrain(), allow_all=True)
        self.run_msgs(router, make_msg())
        self.run_msgs(router, make_msg(msg_id="m1"))     # 同一平台 ID 再投一次
        self.assertEqual(len(adapter.sent), 1)


class TestConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        cfg = cfg_mod.GatewayConfig()
        self.assertEqual(cfg.server.host, "127.0.0.1")
        self.assertEqual(cfg.brain.backend, "cli")
        self.assertFalse(cfg.allow_all)

    def test_parse_full(self) -> None:
        cfg = cfg_mod._parse(
            {
                "brain": {"backend": "cli", "timeout": 90, "max_steps": 10, "model": "x"},
                "server": {"port": 9000},
                "platforms": {"qq": {"enabled": True, "app_id": "123"}},
                "allowlist": {"allow_all": False, "qq": ["u1", "u2"]},
            }
        )
        self.assertEqual(cfg.brain.timeout, 90.0)
        self.assertEqual(cfg.brain.max_steps, 10)
        self.assertEqual(cfg.server.port, 9000)
        self.assertTrue(cfg.platforms["qq"].enabled)
        self.assertEqual(cfg.platforms["qq"].options["app_id"], "123")
        self.assertEqual(cfg.allowlist["qq"], {"u1", "u2"})

    def test_missing_file_returns_defaults(self) -> None:
        cfg = cfg_mod.load("definitely-not-here.toml")
        self.assertEqual(cfg.server.port, 8787)


if __name__ == "__main__":
    unittest.main()
