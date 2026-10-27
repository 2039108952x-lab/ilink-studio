from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway import accounts as accounts_mod
from gateway.accounts import AccountStore, IdentityMismatch


class AccountStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self.tmp.name) / "mailbox.db")
        self.store.ensure_defaults()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults_have_separate_wechat_slots(self) -> None:
        accounts = self.store.list()
        self.assertEqual(len(accounts), 5)
        wechat = [a for a in accounts if a["platform"] == "wechat"]
        self.assertEqual({a["channel"] for a in wechat}, {"official", "wxauto"})
        self.assertEqual(sum(a["is_active_channel"] for a in wechat), 1)
        self.assertFalse(any(a["enabled"] for a in accounts))

    def test_health_dimensions_are_independent(self) -> None:
        account = self.store.by_key("qq", "onebot")
        self.store.set_auth(account["id"], "bound", platform_uid="10000004")
        self.store.set_connection(account["id"], "online")
        self.store.set_send_health(account["id"], "unsupported")
        current = self.store.get(account["id"])
        self.assertEqual(current["auth_state"], "bound")
        self.assertEqual(current["connection_state"], "online")
        self.assertEqual(current["send_health"], "unsupported")

    def test_unexpected_uid_cannot_become_bound(self) -> None:
        account = self.store.by_key("bilibili", "cookie")
        self.store.set_expected_uid(account["id"], "expected-bili-uid")
        with self.assertRaises(IdentityMismatch):
            self.store.set_auth(account["id"], "bound", platform_uid="wrong-uid")
        self.assertEqual(self.store.get(account["id"])["auth_state"], "unbound")

    def test_defaults_do_not_pin_someone_elses_uid(self) -> None:
        """默认槽位不能写死别人的 UID，否则本人第一次探活就 409。"""
        self.assertEqual({a["expected_uid"] for a in self.store.list()}, {""})

    def test_legacy_pinned_uid_is_cleared_only_when_never_bound(self) -> None:
        qq = self.store.by_key("qq", "onebot")
        bili = self.store.by_key("bilibili", "cookie")
        self.store.set_auth(bili["id"], "bound", platform_uid="10000003")
        with self.store._connect() as conn:  # 还原历史库里的写死值
            conn.execute("UPDATE account SET expected_uid='10000004' WHERE id=?", (qq["id"],))
            conn.execute("UPDATE account SET expected_uid='10000003' WHERE id=?", (bili["id"],))
        # 公开版不再内置真实账号；这里临时把「旧版写死的值」装回去，验证清理逻辑本身
        with patch.object(accounts_mod, "LEGACY_DEFAULT_UIDS", ("10000004", "10000003")):
            self.store.ensure_defaults()
        self.assertEqual(self.store.get(qq["id"])["expected_uid"], "")
        self.assertEqual(self.store.get(bili["id"])["expected_uid"], "10000003")

    def test_switch_preserves_old_credentials_and_auth(self) -> None:
        official = self.store.by_key("wechat", "official")
        self.store.set_auth(official["id"], "bound", platform_uid="bot-official")
        self.store.switch_wechat("wxauto")
        self.assertEqual(self.store.wechat_channel, "wxauto")
        self.assertEqual(self.store.get(official["id"])["auth_state"], "bound")
        self.assertFalse(self.store.get(official["id"])["enabled"])
        self.store.switch_wechat("official")
        self.assertEqual(self.store.wechat_channel, "official")

    def test_invalid_state_is_rejected(self) -> None:
        account = self.store.by_key("qq", "onebot")
        with self.assertRaises(ValueError):
            self.store.set_connection(account["id"], "online-ish")


if __name__ == "__main__":
    unittest.main()
