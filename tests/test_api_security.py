from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from gateway.accounts import AccountStore
from gateway.api import ConsoleAPI
from gateway.login.manager import LoginManager
from gateway.models import Platform
from gateway.router import Router
from gateway.server import GatewayServer
from gateway.vault import Vault
from tests.test_server import FakeAdapter, FakeBrain, make_mailbox


class DesktopServerTest(unittest.TestCase):
    token = "desktop-test-token-at-least-256-bits-long-for-tests"
    legacy_token = "legacy-test-token"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.accounts = AccountStore(root / "mailbox.db")
        self.accounts.ensure_defaults()
        self.vault = Vault(root / "vault.db")
        self.logins = LoginManager(self.accounts, self.vault, {})
        self.mailbox = make_mailbox(self)
        self.api = ConsoleAPI(self.accounts, self.vault, self.logins, mailbox=self.mailbox)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.router = Router(FakeBrain(), {}, store=self.mailbox, allow_all=False)
        self.server = GatewayServer(
            self.router, host="127.0.0.1", port=0, token=self.legacy_token,
            loop=self.loop, api=self.api, desktop_mode=True, ui_token=self.token,
        )
        self.server.start()
        self.host, self.port = self.server.address
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.logins.shutdown()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()
        self.tmp.cleanup()

    def request(self, path, *, method="GET", token=None, headers=None, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req_headers = dict(headers or {})
        if data is not None:
            req_headers["Content-Type"] = "application/json"
        if token:
            req_headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.origin + path, data=data, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read()), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)

    def test_health_is_minimal(self) -> None:
        status, body, _ = self.request("/healthz")
        self.assertEqual((status, body), (200, {"ok": True, "version": "0.1.0-rc1"}))

    def test_accounts_requires_session_not_legacy_token(self) -> None:
        self.assertEqual(self.request("/api/v1/accounts")[0], 401)
        self.assertEqual(self.request("/api/v1/accounts", token=self.legacy_token)[0], 401)
        status, body, _ = self.request("/api/v1/accounts", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["accounts"]), 5)
        self.assertNotIn("bot_token", json.dumps(body))

    def test_host_origin_and_query_secrets_are_rejected(self) -> None:
        self.assertEqual(self.request("/api/v1/accounts", token=self.token,
                                      headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/v1/accounts", token=self.token,
                                      headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/v1/accounts?token=abc", token=self.token)[0], 400)

    def test_csp_and_no_wildcard_cors(self) -> None:
        status, _, headers = self.request("/api/v1/accounts", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/api/v1/accounts", method="OPTIONS", token=self.token)[0], 403)

    def test_wxauto_switch_does_not_destroy_official_state(self) -> None:
        official = self.accounts.by_key("wechat", "official")
        self.vault.put("wechat:official", "bot_token", "test-secret")
        self.accounts.set_auth(official["id"], "bound", platform_uid="bot")
        status, body, _ = self.request("/api/v1/accounts/wechat/active-channel", method="PUT",
                                       token=self.token, body={"channel": "wxauto"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "unsupported_channel")
        self.assertEqual(self.vault.get("wechat:official", "bot_token"), "test-secret")
        self.assertEqual(self.accounts.get(official["id"])["auth_state"], "bound")

    def test_get_login_never_starts_task(self) -> None:
        account = self.accounts.by_key("wechat", "official")
        status, _, _ = self.request(f"/api/v1/accounts/{account['id']}/login", token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(self.logins._sessions, {})


class NapCatProbeTest(unittest.TestCase):
    def test_refuses_non_loopback_probe(self) -> None:
        # 探活 URL 固定在 127.0.0.1，不读取外部配置；这里对 socket 目标做契约测试。
        with patch("gateway.api.socket.create_connection", side_effect=TimeoutError) as probe:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            root = Path(tmp.name)
            accounts = AccountStore(root / "mailbox.db")
            accounts.ensure_defaults()
            vault = Vault(root / "vault.db")
            api = ConsoleAPI(accounts, vault, LoginManager(accounts, vault, {}))
            account = accounts.by_key("qq", "onebot")
            with self.assertRaises(Exception) as caught:
                api.probe(account["id"])
            self.assertEqual(getattr(caught.exception, "code", ""), "network_error")
            probe.assert_called_once_with(("127.0.0.1", 3001), timeout=3)


if __name__ == "__main__":
    unittest.main()
