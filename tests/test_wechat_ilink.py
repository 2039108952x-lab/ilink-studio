"""微信 iLink 适配器的测试：全部打桩，不碰真实网络、不写真实 data/。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway.adapters import wechat, wechat_ilink
from gateway.checkpoint import Checkpoint
from gateway.mailbox import Mailbox
from gateway.models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult


def text_msg(**over) -> dict:
    """一条最小的「用户发来的文本消息」。"""
    raw = {
        "message_id": "7301",
        "from_user_id": "o9cq800kum_abc@im.wechat",
        "to_user_id": "e06c1ceea05e@im.bot",
        "message_type": wechat_ilink.MSG_FROM_USER,
        "message_state": wechat_ilink.STATE_FINISH,
        "context_token": "AARzJWAFAAAB",
        "item_list": [{"type": wechat_ilink.ITEM_TEXT, "text_item": {"text": "在吗"}}],
    }
    raw.update(over)
    return raw


class FakeApi:
    """假的 IlinkApi：按剧本回 getupdates，记住发出去的东西。"""

    def __init__(self, rounds: list[dict] | None = None, token: str = "tk") -> None:
        self.rounds = list(rounds or [])
        self.token = token
        self.sent: list[tuple[str, str, str]] = []
        self.bufs: list[str] = []
        self.on_idle = None
        self.send_result: dict = {"ret": 0, "message_id": "srv-1"}
        self.send_error: Exception | None = None

    async def get_updates(self, buf: str) -> dict:
        self.bufs.append(buf)
        if self.rounds:
            return self.rounds.pop(0)
        if self.on_idle is not None:
            self.on_idle()
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}

    async def send_text(self, to_user_id: str, text: str, context_token: str = "") -> dict:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((to_user_id, text, context_token))
        return self.send_result


class TempStateMixin(unittest.TestCase):
    """游标进临时 mailbox.db；旧的 wx-ilink-state.json 只用来测一次性迁移。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "wx-ilink-state.json"
        patch = mock.patch.object(wechat_ilink, "STATE_FILE", self.state_file)
        patch.start()
        self.addCleanup(patch.stop)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account_id = self.mailbox.ensure_account("wechat", "official")

    def checkpoint(self, stream: str = "cursor") -> Checkpoint:
        return Checkpoint(self.mailbox, self.account_id, f"wechat:{stream}")

    def make_adapter(self, api: FakeApi, **opts) -> wechat_ilink.IlinkWeChatAdapter:
        adapter = wechat_ilink.IlinkWeChatAdapter(opts, api=api)  # type: ignore[arg-type]
        adapter.bind(lambda m: asyncio.sleep(0, result=None), checkpoints=self.checkpoint)
        return adapter


# ---------------------------------------------------------------- 消息转换


class TestRenderItems(unittest.TestCase):
    def test_text_passthrough(self) -> None:
        items = [{"type": 1, "text_item": {"text": "吃了吗"}}]
        self.assertEqual(wechat_ilink._render_items(items), "吃了吗")

    def test_media_becomes_placeholder(self) -> None:
        # 我们没做解密，所以这里必须如实说「有个图」，不能假装看见了
        items = [
            {"type": 2, "image_item": {"aeskey": "00"}},
            {"type": 5, "video_item": {}},
        ]
        self.assertEqual(wechat_ilink._render_items(items), "[图片]\n[视频]")

    def test_voice_prefers_transcript(self) -> None:
        items = [
            {"type": 3, "voice_item": {"text": "明天几点"}},
            {"type": 3, "voice_item": {}},
        ]
        self.assertEqual(wechat_ilink._render_items(items), "[语音] 明天几点\n[语音]")

    def test_file_keeps_name(self) -> None:
        items = [{"type": 4, "file_item": {"file_name": "报表.xlsx"}}]
        self.assertEqual(wechat_ilink._render_items(items), "[文件] 报表.xlsx")

    def test_empty(self) -> None:
        self.assertEqual(wechat_ilink._render_items(None), "")
        self.assertEqual(wechat_ilink._render_items([]), "")


class TestToInbound(unittest.TestCase):
    def test_normal_message(self) -> None:
        msg = wechat_ilink.to_inbound(text_msg())
        assert msg is not None
        self.assertEqual(msg.platform, Platform.WECHAT)
        self.assertEqual(msg.kind, ChatKind.PRIVATE)
        self.assertEqual(msg.text, "在吗")
        self.assertEqual(msg.chat_id, "o9cq800kum_abc@im.wechat")
        self.assertEqual(msg.sender_id, "o9cq800kum_abc@im.wechat")
        self.assertEqual(msg.msg_id, "7301")

    def test_bot_message_ignored(self) -> None:
        # message_type=2 是自己发的。不拦掉就会自问自答。
        self.assertIsNone(wechat_ilink.to_inbound(text_msg(message_type=2)))

    def test_empty_text_ignored(self) -> None:
        self.assertIsNone(wechat_ilink.to_inbound(text_msg(item_list=[])))

    def test_missing_sender_ignored(self) -> None:
        self.assertIsNone(wechat_ilink.to_inbound(text_msg(from_user_id="")))

    def test_group_message_still_private(self) -> None:
        # 官方实现回复时固定发给 from_user_id，所以群里来的也按一对一处理
        msg = wechat_ilink.to_inbound(text_msg(group_id="g-1"))
        assert msg is not None
        self.assertEqual(msg.kind, ChatKind.PRIVATE)
        self.assertEqual(msg.chat_id, "o9cq800kum_abc@im.wechat")


# ---------------------------------------------------------------- 协议头


class TestProtocolHeaders(unittest.TestCase):
    def test_uin_is_base64_of_decimal(self) -> None:
        import base64

        decoded = base64.b64decode(wechat_ilink._wechat_uin()).decode()
        self.assertTrue(decoded.isdigit())
        self.assertLessEqual(int(decoded), 2**32 - 1)

    def test_uin_changes_every_request(self) -> None:
        # 每次请求都要变，这是防重放的设计
        self.assertNotEqual(
            wechat_ilink._headers("t")["X-WECHAT-UIN"],
            wechat_ilink._headers("t")["X-WECHAT-UIN"],
        )

    def test_headers_carry_token_and_app_id(self) -> None:
        h = wechat_ilink._headers("abc")
        self.assertEqual(h["Authorization"], "Bearer abc")
        self.assertEqual(h["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(h["iLink-App-Id"], "bot")
        self.assertNotIn("Authorization", wechat_ilink._headers(""))

    def test_client_version_packing(self) -> None:
        # 官方包把 x.y.z 压成 major<<16 | minor<<8 | patch
        self.assertEqual(wechat_ilink._app_client_version("2.4.10"), 132106)
        self.assertEqual(wechat_ilink._app_client_version("1.0.2"), 65538)


# ---------------------------------------------------------------- 收发


class TestSend(TempStateMixin):
    def test_send_uses_remembered_context_token(self) -> None:
        api = FakeApi()
        adapter = self.make_adapter(api)
        adapter.remember_context("u1", "ctx-1")

        out = OutboundMessage(
            platform=Platform.WECHAT, kind=ChatKind.PRIVATE, chat_id="u1", text="收到"
        )
        result = asyncio.run(adapter.send(out))
        self.assertTrue(result.ok)
        self.assertEqual(result.msg_id, "srv-1")
        # 没带上 context_token 的话，微信会把消息甩到别的窗口或者直接不发
        self.assertEqual(api.sent, [("u1", "收到", "ctx-1")])

    def test_send_without_context_token_still_tries(self) -> None:
        api = FakeApi()
        adapter = self.make_adapter(api)
        out = OutboundMessage(
            platform=Platform.WECHAT, kind=ChatKind.PRIVATE, chat_id="u2", text="主动找你"
        )
        result = asyncio.run(adapter.send(out))
        self.assertTrue(result.ok)
        self.assertEqual(api.sent, [("u2", "主动找你", "")])

    def test_send_reports_server_error(self) -> None:
        api = FakeApi()
        api.send_result = {"ret": -3, "errmsg": "rate limited"}
        adapter = self.make_adapter(api)
        out = OutboundMessage(
            platform=Platform.WECHAT, kind=ChatKind.PRIVATE, chat_id="u1", text="x"
        )
        result = asyncio.run(adapter.send(out))
        self.assertFalse(result.ok)
        self.assertIn("rate limited", result.error)

    def test_send_swallows_network_error(self) -> None:
        api = FakeApi()
        api.send_error = OSError("网线掉了")
        adapter = self.make_adapter(api)
        out = OutboundMessage(
            platform=Platform.WECHAT, kind=ChatKind.PRIVATE, chat_id="u1", text="x"
        )
        result = asyncio.run(adapter.send(out))
        self.assertFalse(result.ok)
        self.assertIn("网线掉了", result.error)

    def test_send_without_token_refuses(self) -> None:
        api = FakeApi(token="")
        adapter = self.make_adapter(api)
        out = OutboundMessage(
            platform=Platform.WECHAT, kind=ChatKind.PRIVATE, chat_id="u1", text="x"
        )
        result = asyncio.run(adapter.send(out))
        self.assertFalse(result.ok)
        self.assertIn("wx_login", result.error)

    def test_empty_text_not_sent(self) -> None:
        api = FakeApi()
        adapter = self.make_adapter(api)
        out = OutboundMessage(
            platform=Platform.WECHAT, kind=ChatKind.PRIVATE, chat_id="u1", text=""
        )
        result = asyncio.run(adapter.send(out))
        self.assertFalse(result.ok)
        self.assertEqual(api.sent, [])


class TestStartLoop(TempStateMixin):
    def test_emits_and_persists_cursor(self) -> None:
        api = FakeApi([{"ret": 0, "msgs": [text_msg()], "get_updates_buf": "buf-2"}])
        adapter = self.make_adapter(api, skip_backlog=False)
        api.on_idle = lambda: setattr(adapter, "_stopping", True)
        got: list[InboundMessage] = []
        adapter.bind(lambda m: asyncio.sleep(0, result=got.append(m)))

        asyncio.run(adapter.start())

        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "在吗")
        # 游标必须落库，否则重启会把历史重放一遍
        self.assertEqual(self.checkpoint().cursor(), "buf-2")
        # context_token 只在内存/保险库里，绝不写明文文件
        self.assertEqual(adapter.context_token("o9cq800kum_abc@im.wechat"), "AARzJWAFAAAB")
        self.assertFalse(self.state_file.exists(), "旧明文状态文件不该再被创建")

    def test_first_round_skips_backlog(self) -> None:
        api = FakeApi(
            [{"ret": 0, "msgs": [text_msg(message_id="old-1"), text_msg(message_id="old-2")],
              "get_updates_buf": "buf-1"}]
        )
        adapter = self.make_adapter(api, skip_backlog=True)
        api.on_idle = lambda: setattr(adapter, "_stopping", True)
        got: list[InboundMessage] = []
        adapter.bind(lambda m: asyncio.sleep(0, result=got.append(m)))

        asyncio.run(adapter.start())
        self.assertEqual(got, [], "首轮不该把登录前的历史喂给模型")

    def test_bot_messages_never_emitted(self) -> None:
        api = FakeApi(
            [{"ret": 0, "msgs": [text_msg(message_type=2)], "get_updates_buf": "b2"}]
        )
        adapter = self.make_adapter(api, skip_backlog=False)
        api.on_idle = lambda: setattr(adapter, "_stopping", True)
        got: list[InboundMessage] = []
        adapter.bind(lambda m: asyncio.sleep(0, result=got.append(m)))

        asyncio.run(adapter.start())
        self.assertEqual(got, [])

    def test_emit_failure_does_not_kill_loop(self) -> None:
        api = FakeApi(
            [{"ret": 0, "msgs": [text_msg(message_id="a"), text_msg(message_id="b")],
              "get_updates_buf": "b2"}]
        )
        adapter = self.make_adapter(api, skip_backlog=False)
        api.on_idle = lambda: setattr(adapter, "_stopping", True)
        seen: list[str] = []

        async def handler(msg: InboundMessage) -> None:
            seen.append(msg.msg_id)
            if msg.msg_id == "a":
                raise RuntimeError("brain 炸了")

        adapter.bind(handler)
        asyncio.run(adapter.start())
        self.assertEqual(seen, ["a", "b"], "单条消息出错不能打断整条流")

    def test_session_expired_stops_cleanly(self) -> None:
        api = FakeApi([{"ret": 0, "errcode": -14, "errmsg": "session timeout"}])
        adapter = self.make_adapter(api)
        asyncio.run(adapter.start())
        # 不该无限重连：token 过期只能重新扫码
        self.assertEqual(len(api.bufs), 1)

    def test_migrates_legacy_state_file_once(self) -> None:
        """旧 JSON 里的游标要搬进数据库，搬迁完把明文文件改名存证。"""
        self.state_file.write_text(
            json.dumps({"get_updates_buf": "saved-buf", "contexts": {}}), encoding="utf-8"
        )
        api = FakeApi()
        adapter = self.make_adapter(api)
        api.on_idle = lambda: setattr(adapter, "_stopping", True)
        asyncio.run(adapter.start())
        self.assertEqual(api.bufs[0], "saved-buf")
        self.assertEqual(self.checkpoint().cursor(), "saved-buf")
        self.assertFalse(self.state_file.exists())
        self.assertTrue(self.state_file.with_suffix(".migrated.json").exists())

    def test_resumes_from_checkpoint(self) -> None:
        self.checkpoint().save(cursor="db-buf")
        api = FakeApi()
        adapter = self.make_adapter(api)
        api.on_idle = lambda: setattr(adapter, "_stopping", True)
        asyncio.run(adapter.start())
        self.assertEqual(api.bufs[0], "db-buf")

    def test_no_token_does_not_even_poll(self) -> None:
        api = FakeApi(token="")
        adapter = self.make_adapter(api)
        asyncio.run(adapter.start())
        self.assertEqual(api.bufs, [], "没登录就别去骚扰服务器")


# ---------------------------------------------------------------- 配置分派


class TestBuildDispatch(unittest.TestCase):
    def test_default_goes_official(self) -> None:
        adapter = wechat.build({"enabled": True})
        self.assertIsInstance(adapter, wechat_ilink.IlinkWeChatAdapter)

    def test_explicit_official(self) -> None:
        adapter = wechat.build({"mode": "official"})
        self.assertIsInstance(adapter, wechat_ilink.IlinkWeChatAdapter)

    def test_bogus_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            wechat.build({"mode": "magic"})

    def test_wxauto_mode_still_selectable(self) -> None:
        if not wechat.WxAutoDriver.available():
            self.skipTest("本机没装 wxauto")
        adapter = wechat.build({"mode": "wxauto"})
        self.assertIsInstance(adapter, wechat.WeChatAdapter)


if __name__ == "__main__":
    unittest.main()
