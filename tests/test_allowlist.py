"""自动回复白名单：接口行为 + 加入后 Router 立即放行（不重启网关）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.accounts import AccountStore
from gateway.api import APIError, ConsoleAPI
from gateway.config import GatewayConfig
from gateway.mailbox import Mailbox
from gateway.models import ChatKind, InboundMessage, Platform
from gateway.router import Router
from gateway.settings import SettingsStore


class StubVault:
    def configured(self, account_key: str, key: str | None = None) -> bool:
        return False

    def configured_keys(self, *, include_system: bool = False) -> set[str]:
        return set()


class StubBrain:
    async def respond(self, ctx, text, attachments=None):  # pragma: no cover - 未调用
        raise AssertionError("白名单测试不应触发模型生成")


def message(platform: Platform, sender: str) -> InboundMessage:
    return InboundMessage(
        platform=platform, kind=ChatKind.PRIVATE, chat_id=sender,
        sender_id=sender, sender_name=sender, text="在吗",
    )


class AllowlistApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = SettingsStore(GatewayConfig(), root / "ui-settings.json")
        self.accounts = AccountStore(root / "mailbox.db")
        self.accounts.ensure_defaults()
        self.api = ConsoleAPI(self.accounts, StubVault(), None, settings=self.settings)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_then_remove(self) -> None:
        status, body = self.api.handle(
            "POST", "/api/v1/allowlist", {"platform": "wechat", "platform_uid": "wxid_a"})
        self.assertEqual(status, 200)
        self.assertEqual(body["entries"]["wechat"], ["wxid_a"])
        self.assertEqual(self.settings.current().allowlist["wechat"], {"wxid_a"})

        status, body = self.api.handle(
            "DELETE", "/api/v1/allowlist", {"platform": "wechat", "platform_uid": "wxid_a"})
        self.assertEqual(status, 200)
        self.assertEqual(body["entries"].get("wechat", []), [])
        self.assertEqual(self.settings.current().allowlist.get("wechat", set()), set())

    def test_entries_are_per_platform(self) -> None:
        self.api.handle("POST", "/api/v1/allowlist",
                        {"platform": "wechat", "platform_uid": "wxid_a"})
        _, body = self.api.handle("POST", "/api/v1/allowlist",
                                  {"platform": "bilibili", "platform_uid": "10000001"})
        self.assertEqual(body["entries"]["wechat"], ["wxid_a"])
        self.assertEqual(body["entries"]["bilibili"], ["10000001"])
        # 只写一个平台不能把其它平台清空
        self.assertEqual(self.settings.current().allowlist["wechat"], {"wxid_a"})

    def test_get_lists_current_entries(self) -> None:
        self.api.handle("POST", "/api/v1/allowlist",
                        {"platform": "douyin", "platform_uid": "demo_friend"})
        _, body = self.api.handle("GET", "/api/v1/allowlist")
        self.assertEqual(body["entries"]["douyin"], ["demo_friend"])
        self.assertFalse(body["allow_all"])

    def test_rejects_unknown_platform_and_empty_uid(self) -> None:
        with self.assertRaises(APIError) as caught:
            self.api.handle("POST", "/api/v1/allowlist",
                            {"platform": "telegram", "platform_uid": "x"})
        self.assertEqual(caught.exception.code, "unknown_platform")

        with self.assertRaises(APIError) as caught:
            self.api.handle("POST", "/api/v1/allowlist",
                            {"platform": "wechat", "platform_uid": "  "})
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_uid_prefix_is_stripped(self) -> None:
        """从 B站主页复制过来的 `UID:123` 也要能直接粘进白名单。"""
        _, body = self.api.handle(
            "POST", "/api/v1/allowlist",
            {"platform": "bilibili", "platform_uid": "UID:10000001"})
        self.assertEqual(body["entries"]["bilibili"], ["10000001"])
        self.assertEqual(body["platform_uid"], "10000001")

    def test_fullwidth_space_and_prefix_variants(self) -> None:
        _, body = self.api.handle(
            "POST", "/api/v1/allowlist",
            {"platform": "bilibili", "platform_uid": "　uid：10000003 　"})
        self.assertEqual(body["entries"]["bilibili"], ["10000003"])

    def test_get_returns_empty_candidates_without_mailbox(self) -> None:
        _, body = self.api.handle("GET", "/api/v1/allowlist")
        self.assertEqual(body["candidates"], [])

    def test_duplicate_add_is_idempotent(self) -> None:
        for _ in range(2):
            _, body = self.api.handle("POST", "/api/v1/allowlist",
                                      {"platform": "wechat", "platform_uid": "wxid_a"})
        self.assertEqual(body["entries"]["wechat"], ["wxid_a"])


class AllowlistRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = SettingsStore(GatewayConfig(), root / "ui-settings.json")
        self.mailbox = Mailbox(root / "mailbox.db")
        self.router = Router(
            StubBrain(), {}, store=self.mailbox, allow_all=False,
            settings=self.settings,
        )

    def tearDown(self) -> None:
        self.mailbox.close()
        self.tmp.cleanup()

    async def test_added_uid_is_allowed_without_restart(self) -> None:
        incoming = message(Platform.WECHAT, "wxid_new")
        self.assertFalse(self.router.allowed(incoming))

        result = self.settings.update({"allowlist": {"wechat": ["wxid_new"]}})
        self.assertIn("allowlist", result.applied)
        self.assertTrue(self.router.allowed(incoming))
        self.assertFalse(self.router.allowed(message(Platform.WECHAT, "wxid_other")))

    async def test_removed_uid_is_blocked_again(self) -> None:
        self.settings.update({"allowlist": {"bilibili": ["10000001"]}})
        incoming = message(Platform.BILIBILI, "10000001")
        self.assertTrue(self.router.allowed(incoming))

        self.settings.update({"allowlist": {"bilibili": []}})
        self.assertFalse(self.router.allowed(incoming))

    async def test_provisional_identity_never_allowed(self) -> None:
        self.settings.update({"allowlist": {"douyin": ["昵称大王"]}})
        nickname_only = message(Platform.DOUYIN, "昵称大王")
        nickname_only.sender_id_kind = "provisional"
        self.assertFalse(self.router.allowed(nickname_only))

    def _router_with_record_all_bodies(self) -> Router:
        self.mailbox.ensure_account(Platform.DOUYIN, "rpa")
        cfg = GatewayConfig()
        cfg.privacy.record_all_bodies = True
        settings = SettingsStore(cfg, Path(self.tmp.name) / "ui-settings-all-bodies.json")
        return Router(StubBrain(), {}, store=self.mailbox, allow_all=False, settings=settings)

    async def test_stranger_body_is_dropped_by_default(self) -> None:
        """开关关着（默认）：白名单外只留元数据，正文一个字都不落库。"""
        self.mailbox.ensure_account(Platform.WECHAT, "official")
        stranger = message(Platform.WECHAT, "wxid_stranger")
        stranger.msg_id = "stranger-1"

        await self.router.on_inbound(stranger)

        stored = self.mailbox.list_messages(limit=5).items[0]
        self.assertIsNone(stored["body"])
        self.assertEqual(stored["storage_class"], "metadata_only")
        self.assertEqual(stored["skip_reason"], "not_allowlisted")
        self.assertEqual(stored["reply_policy"], "never")

    async def test_record_all_bodies_keeps_text_without_replying(self) -> None:
        """开关打开：正文留在本机，但回复策略仍是 never，不会自己开口。"""
        router = self._router_with_record_all_bodies()
        stranger = message(Platform.DOUYIN, "某个陌生人")
        stranger.msg_id = "stranger-2"

        await router.on_inbound(stranger)

        stored = self.mailbox.list_messages(limit=5).items[0]
        self.assertEqual(stored["body"], "在吗")
        self.assertEqual(stored["storage_class"], "full")
        self.assertEqual(stored["processing_status"], "skipped")
        self.assertEqual(stored["reply_policy"], "never", "存正文不等于允许自动回复")
        self.assertEqual(stored["skip_reason"], "not_allowlisted")

    async def test_provisional_identity_keeps_no_body_even_with_record_all(self) -> None:
        """开关只管「有没有稳定 ID 的陌生人」，连稳定 ID 都没有的不能被这条开关带进来。"""
        router = self._router_with_record_all_bodies()
        self.assertTrue(router.record_all_bodies(), "测试前提：开关确实打开了")
        nickname_only = message(Platform.DOUYIN, "昵称大王")
        nickname_only.sender_id_kind = "provisional"
        nickname_only.msg_id = "prov-1"

        await router.on_inbound(nickname_only)

        stored = self.mailbox.list_messages(limit=5).items[0]
        self.assertIsNone(stored["body"], "provisional 的正文一律不落库")
        self.assertEqual(stored["storage_class"], "metadata_only")
        self.assertEqual(stored["skip_reason"], "provisional_identity")


if __name__ == "__main__":
    unittest.main()
