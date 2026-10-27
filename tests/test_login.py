from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from gateway.accounts import AccountStore
from gateway.login.manager import LoginManager
from gateway.login.providers.base import LoginError, LoginResult
from gateway.vault import Vault


class FakeProvider:
    def __init__(self, behavior) -> None:
        self.behavior = behavior
        self.calls = 0

    def run(self, context):
        self.calls += 1
        return self.behavior(context)


class LoginManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = AccountStore(root / "mailbox.db")
        self.accounts.ensure_defaults()
        self.vault = Vault(root / "vault.db")
        self.reloaded = []
        self.providers = {}
        self.manager = LoginManager(
            self.accounts, self.vault, self.providers, on_reload=self.reloaded.append
        )

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.tmp.cleanup()

    def install(self, account, provider) -> None:
        self.providers[f"{account['platform']}:{account['channel']}"] = provider

    def wait(self, session_id: str, state: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            snapshot = self.manager.get(session_id)
            if snapshot["state"] == state:
                return snapshot
            time.sleep(0.01)
        self.fail(f"未等到状态 {state}: {snapshot}")

    def test_success_saves_vault_and_reloads_only_account(self) -> None:
        account = self.accounts.by_key("wechat", "official")
        self.install(account, FakeProvider(lambda ctx: LoginResult(
            platform_uid="bot-1", credentials={"bot_token": "test-only-secret"}
        )))
        session = self.manager.start(account["id"])
        self.assertEqual(self.wait(session["id"], "bound")["state"], "bound")
        self.assertEqual(self.vault.get("wechat:official", "bot_token"), "test-only-secret")
        self.assertEqual(self.reloaded, [account["id"]])
        self.assertNotIn("test-only-secret", str(session))

    def test_duplicate_start_does_not_spawn_another_poller(self) -> None:
        account = self.accounts.by_key("wechat", "official")
        release = threading.Event()
        provider = FakeProvider(lambda ctx: (release.wait(2), LoginResult(platform_uid="bot-1"))[1])
        self.install(account, provider)
        session = self.manager.start(account["id"])
        with self.assertRaises(LoginError):
            self.manager.start(account["id"])
        release.set()
        self.wait(session["id"], "bound")
        self.assertEqual(provider.calls, 1)

    def test_cancel_stops_waiting_provider(self) -> None:
        account = self.accounts.by_key("wechat", "official")
        finished = threading.Event()

        def wait(ctx):
            while not ctx.cancelled():
                time.sleep(0.01)
            finished.set()
            raise LoginError("cancelled", "登录已取消")

        self.install(account, FakeProvider(wait))
        session = self.manager.start(account["id"])
        self.manager.cancel(session["id"])
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.manager.get(session["id"])["state"], "cancelled")

    def test_identity_mismatch_never_saves_credentials(self) -> None:
        account = self.accounts.by_key("bilibili", "cookie")
        # 预期 UID 现在由用户显式设定；默认留空表示「以实际 UID 为准」。
        self.accounts.set_expected_uid(account["id"], "expected-uid")
        self.install(account, FakeProvider(lambda ctx: LoginResult(
            platform_uid="wrong", credentials={"SESSDATA": "do-not-save"}
        )))
        session = self.manager.start(account["id"])
        self.wait(session["id"], "failed")
        self.assertFalse(self.vault.configured("bilibili:cookie"))
        self.assertNotEqual(self.accounts.get(account["id"])["auth_state"], "bound")

    def test_manual_uid_confirmation_is_checked(self) -> None:
        account = self.accounts.by_key("douyin", "rpa")

        def ask(ctx):
            ctx.update("awaiting_confirm", "请核对 UID")
            while not ctx.cancelled() and not ctx.wait_confirmation(0.01):
                pass
            return LoginResult(platform_uid=ctx.platform_uid)

        self.install(account, FakeProvider(ask))
        session = self.manager.start(account["id"])
        self.wait(session["id"], "awaiting_confirm")
        self.manager.confirm(session["id"], "douyin-bot")
        self.assertEqual(self.wait(session["id"], "bound")["platform_uid"], "douyin-bot")

    def test_qq_and_wxauto_do_not_fake_login(self) -> None:
        for platform, channel in (("qq", "onebot"), ("wechat", "wxauto")):
            account = self.accounts.by_key(platform, channel)
            with self.assertRaises(LoginError):
                self.manager.start(account["id"])


if __name__ == "__main__":
    unittest.main()
