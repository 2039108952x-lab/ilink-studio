from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from gateway.vault import CredentialUnavailable, DPAPIBackend, Vault


class VaultTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "vault.db"
        try:
            self.vault = Vault(self.path)
        except CredentialUnavailable as exc:
            self.skipTest(str(exc))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip_and_channels_are_isolated(self) -> None:
        self.vault.put("wechat:official", "bot_token", "official-secret")
        self.vault.put("wechat:wxauto", "bot_token", "wxauto-secret")
        self.assertEqual(self.vault.get("wechat:official", "bot_token"), "official-secret")
        self.assertEqual(self.vault.get("wechat:wxauto", "bot_token"), "wxauto-secret")

    def test_database_contains_ciphertext_not_secret(self) -> None:
        secret = "vault-sentinel-do-not-persist"
        self.vault.put("wechat:official", "bot_token", secret)
        raw = self.path.read_bytes() + self.path.with_name("vault.db-wal").read_bytes() \
            if self.path.with_name("vault.db-wal").exists() else self.path.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        with closing(sqlite3.connect(self.path)) as conn:
            backend, blob = conn.execute("SELECT crypto_backend,blob FROM credential").fetchone()
        self.assertEqual(backend, "dpapi-current-user")
        self.assertNotEqual(blob, secret.encode())

    def test_configured_does_not_decrypt_or_return_value(self) -> None:
        self.vault.put("bilibili:cookie", "SESSDATA", "test-only-secret")
        self.assertTrue(self.vault.configured("bilibili:cookie"))
        self.assertEqual(self.vault.configured_keys(), {"bilibili:cookie"})

    def test_put_many_is_atomic(self) -> None:
        self.vault.put("bilibili:cookie", "SESSDATA", "original")
        with self.assertRaises(ValueError):
            self.vault.put_many("bilibili:cookie", {"SESSDATA": "new", "bili_jct": ""})
        self.assertEqual(self.vault.get("bilibili:cookie", "SESSDATA"), "original")

    def test_delete_requires_explicit_operation(self) -> None:
        self.vault.put("wechat:official", "bot_token", "secret")
        self.assertEqual(self.vault.delete("wechat:official"), 1)
        with self.assertRaises(CredentialUnavailable):
            self.vault.get("wechat:official", "bot_token")


class DPAPITest(unittest.TestCase):
    def test_current_user_round_trip(self) -> None:
        try:
            backend = DPAPIBackend()
        except CredentialUnavailable as exc:
            self.skipTest(str(exc))
        value = "本机测试凭据".encode()
        self.assertEqual(backend.unseal(backend.seal(value)), value)

    def test_invalid_ciphertext_is_explicit(self) -> None:
        try:
            backend = DPAPIBackend()
        except CredentialUnavailable as exc:
            self.skipTest(str(exc))
        with self.assertRaises(CredentialUnavailable):
            backend.unseal(b"not-a-dpapi-blob")


if __name__ == "__main__":
    unittest.main()
