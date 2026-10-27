"""账号级通道管理：启用前置校验、启停生命周期、API 映射、重启恢复。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.accounts import AccountStore
from gateway.api import APIError, ConsoleAPI
from gateway.channels import ChannelError, ChannelManager
from gateway.config import GatewayConfig
from gateway.desktop import _account_switch, _restore_channels
from gateway.models import Platform


class FakeVault:
    def __init__(self, configured: set[str] | None = None) -> None:
        self.keys = set(configured or ())

    def configured(self, account_key: str, key: str | None = None) -> bool:
        return account_key in self.keys

    def configured_keys(self) -> set[str]:
        return set(self.keys)


class FakeRouter:
    def __init__(self) -> None:
        self.bound: dict[Platform, object] = {}
        self.unbound: list[Platform] = []

    def bind_adapter(self, platform, adapter) -> None:
        self.bound[platform] = adapter

    def unbind_adapter(self, platform) -> None:
        self.unbound.append(platform)
        self.bound.pop(platform, None)


class FakeAdapter:
    def __init__(self) -> None:
        self.vault = None
        self.started = False
        self.stopped = False
        self._release = asyncio.Event()

    def attach_vault(self, vault) -> None:
        self.vault = vault

    async def start(self) -> None:
        self.started = True
        await self._release.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._release.set()


class ChannelTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "mailbox.db"
        self.accounts = AccountStore(self.db)
        self.accounts.ensure_defaults()
        self.config = GatewayConfig()
        self.config.ui.onboarding_confirmed = True
        self.router = FakeRouter()
        self.adapter = FakeAdapter()
        self.vault = FakeVault({"wechat:official", "bilibili:cookie"})
        self.manager = ChannelManager(
            self.accounts, self.vault, self.router,
            config=lambda: self.config, data_dir=self.root,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def bind(self, platform: str, channel: str, uid: str = "10000004") -> dict:
        account = self.accounts.by_key(platform, channel)
        self.accounts.set_auth(account["id"], "bound", platform_uid=uid)
        return self.accounts.get(account["id"])

    async def expect_error(self, code: str, account_id: int) -> None:
        with self.assertRaises(ChannelError) as caught:
            await self.manager.enable(account_id)
        self.assertEqual(caught.exception.code, code)


class TestEnableGuards(ChannelTestCase):
    async def test_requires_onboarding_confirmation(self) -> None:
        account = self.bind("qq", "onebot")
        self.config.ui.onboarding_confirmed = False
        await self.expect_error("onboarding_required", account["id"])

    async def test_rejects_wxauto_channel(self) -> None:
        account = self.accounts.by_key("wechat", "wxauto")
        await self.expect_error("unsupported_channel", account["id"])

    async def test_rejects_unverified_identity(self) -> None:
        account = self.accounts.by_key("qq", "onebot")
        self.assertEqual(account["auth_state"], "unbound")
        await self.expect_error("identity_unverified", account["id"])

    async def test_rejects_uid_mismatch(self) -> None:
        account = self.bind("qq", "onebot")
        # set_auth 拒绝不匹配的 UID；这里直接改库，模拟预期 UID 变更后的存量槽位。
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE account SET expected_uid='999' WHERE id=?", (account["id"],))
            conn.commit()
        finally:
            conn.close()
        await self.expect_error("identity_mismatch", account["id"])

    async def test_rejects_missing_credentials(self) -> None:
        account = self.bind("wechat", "official", uid="")
        self.accounts.set_auth(account["id"], "bound", platform_uid="wxid_bot")
        self.vault.keys.clear()
        await self.expect_error("credentials_missing", account["id"])

    async def test_rejects_missing_dependency(self) -> None:
        account = self.bind("qq", "onebot")
        with patch("gateway.channels.build_platform_adapter", return_value=None):
            await self.expect_error("dependency_missing", account["id"])


class TestEnableLifecycle(ChannelTestCase):
    async def test_enable_then_disable(self) -> None:
        account = self.bind("qq", "onebot")
        with patch("gateway.channels.build_platform_adapter", return_value=self.adapter):
            result = await self.manager.enable(account["id"])
            await asyncio.sleep(0)
            self.assertTrue(result["enabled"])
            # starting 不等于已连接：握手完成前不伪造 online
            self.assertEqual(result["connection_state"], "starting")
            self.assertIs(self.router.bound[Platform.QQ], self.adapter)
            self.assertIs(self.adapter.vault, self.vault)
            self.assertTrue(self.adapter.started)
            self.assertTrue(self.accounts.get(account["id"])["enabled"])

            disabled = await self.manager.disable(account["id"])
        self.assertFalse(disabled["enabled"])
        self.assertFalse(self.accounts.get(account["id"])["enabled"])
        self.assertEqual(self.router.unbound, [Platform.QQ])
        self.assertTrue(self.adapter.stopped)

    async def test_set_enabled_routes_to_enable_and_disable(self) -> None:
        account = self.bind("qq", "onebot")
        with patch("gateway.channels.build_platform_adapter", return_value=self.adapter):
            self.assertTrue((await self.manager.set_enabled(account["id"], True))["enabled"])
            self.assertFalse((await self.manager.set_enabled(account["id"], False))["enabled"])

    async def test_stop_all_releases_adapters(self) -> None:
        account = self.bind("qq", "onebot")
        with patch("gateway.channels.build_platform_adapter", return_value=self.adapter):
            await self.manager.enable(account["id"])
            await self.manager.stop_all()
        self.assertFalse(self.accounts.get(account["id"])["enabled"])
        self.assertEqual(self.router.bound, {})
        with self.assertRaises(ChannelError) as caught:
            await self.manager.enable(account["id"])
        self.assertEqual(caught.exception.code, "shutdown")


class TestConsoleApiMapping(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = AccountStore(Path(self.tmp.name) / "mailbox.db")
        self.accounts.ensure_defaults()
        self.vault = FakeVault()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def api(self, handler=None) -> ConsoleAPI:
        return ConsoleAPI(self.accounts, self.vault, None, on_account_enabled=handler)

    def test_missing_handler_is_503(self) -> None:
        with self.assertRaises(APIError) as caught:
            self.api().handle("PUT", "/api/v1/accounts/1/enabled", {"enabled": True})
        self.assertEqual(caught.exception.code, "channel_unavailable")
        self.assertEqual(caught.exception.status, 503)

    def test_channel_error_becomes_409(self) -> None:
        def handler(account_id: int, enabled: bool):
            raise ChannelError("identity_unverified", "请先登录并核对机器人 UID，再启用此通道")

        with self.assertRaises(APIError) as caught:
            self.api(handler).handle("PUT", "/api/v1/accounts/1/enabled", {"enabled": True})
        self.assertEqual(caught.exception.code, "identity_unverified")
        self.assertEqual(caught.exception.status, 409)

    def test_result_is_passed_through(self) -> None:
        seen: list[tuple[int, bool]] = []

        def handler(account_id: int, enabled: bool):
            seen.append((account_id, enabled))
            return {"account_id": account_id, "enabled": enabled, "connection_state": "starting"}

        status, body = self.api(handler).handle(
            "PUT", "/api/v1/accounts/7/enabled", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["connection_state"], "starting")
        self.assertEqual(seen, [(7, True)])

    def test_non_boolean_body_is_rejected(self) -> None:
        with self.assertRaises(APIError) as caught:
            self.api(lambda *_: {}).handle("PUT", "/api/v1/accounts/1/enabled", {"enabled": "yes"})
        self.assertEqual(caught.exception.code, "invalid_request")


class TestRestoreOnStartup(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = AccountStore(Path(self.tmp.name) / "mailbox.db")
        self.accounts.ensure_defaults()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_only_enabled_active_slots_are_restored(self) -> None:
        qq = self.accounts.by_key("qq", "onebot")
        wxauto = self.accounts.by_key("wechat", "wxauto")
        bili = self.accounts.by_key("bilibili", "cookie")
        self.accounts.set_enabled(qq["id"], True)
        # wxauto 即使被标记启用，也不是当前活动通道，重启不能接管
        self.accounts.set_enabled(wxauto["id"], True)
        calls: list[int] = []

        class Recorder:
            async def enable(self, account_id: int) -> dict:
                calls.append(account_id)
                return {"account_id": account_id, "enabled": True}

        await _restore_channels(Recorder(), self.accounts)
        self.assertEqual(calls, [qq["id"]])
        self.assertNotIn(bili["id"], calls)

    async def test_failure_keeps_intent_but_marks_error(self) -> None:
        qq = self.accounts.by_key("qq", "onebot")
        self.accounts.set_enabled(qq["id"], True)

        class Broken:
            async def enable(self, account_id: int) -> dict:
                raise ChannelError("identity_unverified", "未绑定")

        await _restore_channels(Broken(), self.accounts)
        account = self.accounts.get(qq["id"])
        self.assertTrue(account["enabled"])
        self.assertEqual(account["connection_state"], "error")
        self.assertEqual(account["last_error_code"], "identity_unverified")


class TestAccountSwitch(unittest.TestCase):
    """HTTP 线程调用的回调必须回到网关事件循环线程上执行。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def test_coroutine_runs_on_gateway_loop_thread(self) -> None:
        threads: list[str] = []

        class Manager:
            async def set_enabled(self, account_id: int, enabled: bool) -> dict:
                threads.append(threading.current_thread().name)
                return {"account_id": account_id, "enabled": enabled,
                        "connection_state": "starting"}

        result = _account_switch(Manager(), self.loop)(3, True)
        self.assertEqual(result, {"account_id": 3, "enabled": True,
                                  "connection_state": "starting"})
        self.assertEqual(threads, [self.thread.name])

    def test_channel_error_reaches_caller(self) -> None:
        class Manager:
            async def set_enabled(self, account_id: int, enabled: bool) -> dict:
                raise ChannelError("identity_unverified", "请先登录并核对机器人 UID，再启用此通道")

        with self.assertRaises(ChannelError) as caught:
            _account_switch(Manager(), self.loop)(1, True)
        self.assertEqual(caught.exception.code, "identity_unverified")


if __name__ == "__main__":
    unittest.main()
