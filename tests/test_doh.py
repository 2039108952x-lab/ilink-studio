"""DNS 兜底测试：不联网，DoH 调用一律 mock。"""

from __future__ import annotations

import socket
import unittest
from unittest import mock

from gateway import doh


class TestMatches(unittest.TestCase):
    def test_bilibili_domains(self) -> None:
        for host in (
            "api.bilibili.com",
            "www.bilibili.com",
            "i0.hdslb.com",
            "a.w.bilicdn1.com",
        ):
            self.assertTrue(doh._matches(host), host)

    def test_douyin_domains(self) -> None:
        # 实测这台机器系统 DNS 对这几个域名全 gaierror，必须由 DoH 兜住
        for host in (
            "www.douyin.com",
            "douyin.com",
            "imapi.douyin.com",
            "p3-sign.douyinpic.com",
            "lf-douyin-pc-web.douyinstatic.com",
            "p3.byteimg.com",
        ):
            self.assertTrue(doh._matches(host), host)

    def test_other_domains_untouched(self) -> None:
        for host in ("example.com", "api.qq.com", "bilibili.com.evil.com", "",
                     "douyin.com.evil.com", "notdouyin.com"):
            self.assertFalse(doh._matches(host), host)

    def test_case_insensitive(self) -> None:
        self.assertTrue(doh._matches("API.BILIBILI.COM"))


class TestDohLookup(unittest.TestCase):
    def test_extracts_ipv4(self) -> None:
        payload = {
            "Answer": [
                {"type": 5, "data": "a.w.bilicdn1.com."},
                {"type": 1, "data": "112.13.92.199"},
                {"type": 1, "data": "112.13.92.196"},
            ]
        }
        resp = mock.MagicMock()
        resp.json.return_value = payload
        client = mock.MagicMock()
        client.__enter__ = mock.MagicMock(return_value=client)
        client.__exit__ = mock.MagicMock(return_value=False)
        client.get.return_value = resp

        with mock.patch("httpx.Client", return_value=client):
            ips = doh._doh_lookup("api.bilibili.com", "https://223.5.5.5/resolve")

        self.assertEqual(ips, ["112.13.92.199", "112.13.92.196"])
        # 只挑 A 记录
        self.assertNotIn("a.w.bilicdn1.com.", ips)

    def test_caps_address_count(self) -> None:
        payload = {"Answer": [{"type": 1, "data": f"1.1.1.{i}"} for i in range(20)]}
        resp = mock.MagicMock()
        resp.json.return_value = payload
        client = mock.MagicMock()
        client.__enter__ = mock.MagicMock(return_value=client)
        client.__exit__ = mock.MagicMock(return_value=False)
        client.get.return_value = resp

        with mock.patch("httpx.Client", return_value=client):
            ips = doh._doh_lookup("api.bilibili.com", "https://223.5.5.5/resolve")

        self.assertEqual(len(ips), doh.MAX_ADDRS)

    def test_network_error_returns_empty(self) -> None:
        client = mock.MagicMock()
        client.__enter__ = mock.MagicMock(return_value=client)
        client.__exit__ = mock.MagicMock(return_value=False)
        client.get.side_effect = OSError("连不上")

        with mock.patch("httpx.Client", return_value=client):
            ips = doh._doh_lookup("api.bilibili.com", "https://223.5.5.5/resolve")

        self.assertEqual(ips, [])

    def test_no_answer_key(self) -> None:
        resp = mock.MagicMock()
        resp.json.return_value = {}
        client = mock.MagicMock()
        client.__enter__ = mock.MagicMock(return_value=client)
        client.__exit__ = mock.MagicMock(return_value=False)
        client.get.return_value = resp

        with mock.patch("httpx.Client", return_value=client):
            self.assertEqual(doh._doh_lookup("api.bilibili.com", "u"), [])


class TestPatchBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        doh.clear_cache()
        doh.uninstall()
        self._addrs = [((socket.AF_INET, socket.SOCK_STREAM), ("1.2.3.4", 443))]

    def tearDown(self) -> None:
        doh.uninstall()
        doh.clear_cache()

    def test_install_is_idempotent(self) -> None:
        doh.install()
        first = socket.getaddrinfo
        doh.install()
        self.assertIs(socket.getaddrinfo, first)

    def test_uninstall_restores(self) -> None:
        original = socket.getaddrinfo
        doh.install()
        self.assertIsNot(socket.getaddrinfo, original)
        doh.uninstall()
        self.assertIs(socket.getaddrinfo, original)

    def test_target_domain_uses_doh(self) -> None:
        doh.install()
        with mock.patch.object(doh, "_doh_lookup", return_value=["9.9.9.9"]) as fake, \
             mock.patch.object(doh, "_original", return_value=self._addrs) as native:
            socket.getaddrinfo("api.bilibili.com", 443)
        fake.assert_called_once()
        # 传给原生解析的应该是 DoH 拿到的 IP，而不是域名
        self.assertEqual(native.call_args.args[0], "9.9.9.9")

    def test_other_domain_passes_through(self) -> None:
        doh.install()
        with mock.patch.object(doh, "_doh_lookup", return_value=["9.9.9.9"]) as fake, \
             mock.patch.object(doh, "_original", return_value=self._addrs) as native:
            socket.getaddrinfo("example.com", 443)
        fake.assert_not_called()
        self.assertEqual(native.call_args.args[0], "example.com")

    def test_doh_failure_falls_back_to_native(self) -> None:
        doh.install()
        with mock.patch.object(doh, "_doh_lookup", return_value=[]), \
             mock.patch.object(doh, "_original", return_value=self._addrs) as native:
            socket.getaddrinfo("api.bilibili.com", 443)
        # DoH 没结果时，还是拿域名去走原生解析
        self.assertEqual(native.call_args.args[0], "api.bilibili.com")

    def test_cache_avoids_repeat_lookup(self) -> None:
        doh.install()
        with mock.patch.object(doh, "_doh_lookup", return_value=["9.9.9.9"]) as fake, \
             mock.patch.object(doh, "_original", return_value=self._addrs):
            socket.getaddrinfo("api.bilibili.com", 443)
            socket.getaddrinfo("api.bilibili.com", 443)
            socket.getaddrinfo("api.bilibili.com", 443)
        self.assertEqual(fake.call_count, 1)

    def test_non_string_host_untouched(self) -> None:
        doh.install()
        with mock.patch.object(doh, "_doh_lookup", return_value=["9.9.9.9"]) as fake, \
             mock.patch.object(doh, "_original", return_value=self._addrs):
            socket.getaddrinfo(b"raw", 443)  # type: ignore[arg-type]
        fake.assert_not_called()


class TestConfig(unittest.TestCase):
    def test_env_override(self) -> None:
        with mock.patch.dict("os.environ", {"ILINK_DOH": "https://example.test/resolve"}):
            self.assertEqual(doh._current_doh(), "https://example.test/resolve")

    def test_default_when_unset(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(doh._current_doh(), doh.DEFAULT_DOH)


if __name__ == "__main__":
    unittest.main()
