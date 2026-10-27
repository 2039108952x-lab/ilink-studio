from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from gateway.config import load
from gateway.runtime import current_ui_token, rotate_ui_token
from gateway.vault import Vault
from tests.test_api_security import DesktopServerTest


class ReattachTest(DesktopServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.vault.put("desktop:bootstrap", "token", "bootstrap-test-value-not-a-platform-secret")
        self.vault.put("desktop:session", "ui_token", self.token)
        self.server._httpd.ui_token_provider = lambda: current_ui_token(self.vault)
        from gateway.runtime import bootstrap_token
        self.server._httpd.bootstrap_token_provider = lambda: bootstrap_token(self.vault)
        self.server._httpd.rotate_ui_token = lambda token: self.vault.put("desktop:session", "ui_token", token)

    def test_reattach_rotates_session_without_second_server(self) -> None:
        old_address = self.server.address
        old_token = self.token
        with patch.dict(os.environ, {"ILINK_DATA_DIR": str(Path(self.tmp.name))}):
            token = rotate_ui_token(self.vault, port=self.port)
        self.assertGreaterEqual(len(token.encode()), 43)
        self.assertNotEqual(token, old_token)
        self.assertEqual(self.server.address, old_address)
        self.assertEqual(self.request("/api/v1/accounts", token=old_token)[0], 401)
        self.assertEqual(self.request("/api/v1/accounts", token=token)[0], 200)

    def test_pause_is_persistent_and_does_not_stop_connections(self) -> None:
        status, body, _ = self.request("/api/v1/runtime/pause", method="POST",
                                       token=self.token, body={"paused": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["paused"])
        self.assertEqual(self.accounts.get_setting("reply_paused"), "true")
        _, body, _ = self.request("/api/v1/runtime", token=self.token)
        self.assertTrue(body["paused"])


class ConfigSecurityTest(unittest.TestCase):
    def test_rejects_non_numeric_and_public_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            for host in ("0.0.0.0", "localhost", "192.168.1.2"):
                path.write_text(f'[server]\nhost = "{host}"\n', encoding="utf-8")
                with self.assertRaises(ValueError):
                    load(path)

    def test_production_rejects_allow_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('[allowlist]\nallow_all = true\n', encoding="utf-8")
            with patch.dict(os.environ, {"ILINK_PRODUCTION": "1"}):
                with self.assertRaises(ValueError):
                    load(path)
            with patch.dict(os.environ, {"ILINK_PRODUCTION": "0"}):
                self.assertTrue(load(path).allow_all)


if __name__ == "__main__":
    unittest.main()
