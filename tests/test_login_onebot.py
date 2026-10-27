"""OneBot provider 的单元测试：不允许再回到「直接抛 external_login_required」。"""
from __future__ import annotations

import unittest
from unittest import mock

from gateway.login.providers.base import LoginError
from gateway.login.providers.onebot import OneBotProvider


class FakeContext:
    def __init__(self, *, confirm: bool = True) -> None:
        self.account_key = "qq:onebot"
        self.expected_uid = ""
        self.updates: list[tuple[str, str, str]] = []
        self.platform_uid = ""
        self._confirm = confirm

    def cancelled(self) -> bool:
        return False

    def update(self, state, message, *, qr_png=None, platform_uid=""):
        if platform_uid:
            self.platform_uid = platform_uid
        self.updates.append((state, message, platform_uid))

    def verification_code(self) -> str:
        return ""

    def wait_confirmation(self, timeout: float = 1.0) -> bool:
        return self._confirm


class OneBotProviderTest(unittest.TestCase):
    """真机事故：QQ 的 login provider 以前只抛 external_login_required，
    于是 auth_state 永远 unbound，而启用通道要求 bound —— 死锁。"""

    def _run(self, *, state, login_info, confirm=True):
        context = FakeContext(confirm=confirm)
        provider = OneBotProvider("ws://127.0.0.1:3001", timeout=1.0)
        with mock.patch("gateway.napcat.describe", return_value=state), \
             mock.patch("gateway.login.providers.onebot.fetch_login_info",
                        return_value=login_info):
            return provider.run(context), context

    def test_returns_uid_from_napcat(self) -> None:
        result, context = self._run(
            state={"onebot_running": True, "onebot_ws": "ws://127.0.0.1:3001",
                   "accounts": [], "message": ""},
            login_info=("10000004", "demo_nick"),
        )
        self.assertEqual(result.platform_uid, "10000004")
        self.assertTrue(any(s == "awaiting_confirm" for s, _, _ in context.updates))

    def test_falls_back_to_configured_account(self) -> None:
        result, _ = self._run(
            state={"onebot_running": True, "accounts": ["10000002"], "message": ""},
            login_info=("", ""),
        )
        self.assertEqual(result.platform_uid, "10000002")

    def test_refuses_when_napcat_not_running(self) -> None:
        with self.assertRaises(LoginError) as caught:
            self._run(state={"onebot_running": False, "message": "NapCat 没在运行"},
                      login_info=("", ""))
        self.assertEqual(caught.exception.code, "external_login_required")

    def test_refuses_ambiguous_accounts(self) -> None:
        with self.assertRaises(LoginError) as caught:
            self._run(state={"onebot_running": True, "accounts": ["111", "222"], "message": ""},
                      login_info=("", ""))
        self.assertEqual(caught.exception.code, "onebot_ambiguous")

    def test_cancelled_confirmation_raises(self) -> None:
        with self.assertRaises(LoginError):
            self._run(state={"onebot_running": True, "accounts": [], "message": ""},
                      login_info=("10000004", "demo_nick"), confirm=False)


if __name__ == "__main__":
    unittest.main()
