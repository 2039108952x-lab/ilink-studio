"""NapCat 探测：端口必须来自它自己的配置，不能写死 6099/3001。"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway import napcat

PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def listening_socket() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


class NapcatDescribeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "NapCat"
        self.root.mkdir(parents=True)
        (self.root / "NapCatWinBootMain.exe").write_bytes(b"stub")
        # 默认假设端口都没人监听；不依赖本机是否真的跑着 NapCat
        self.ports: set[int] = set()
        port_patch = patch.object(
            napcat, "_port_open", side_effect=lambda host, port, timeout=0.5: port in self.ports
        )
        port_patch.start()
        self.addCleanup(port_patch.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_install_reports_not_installed(self) -> None:
        state = napcat.describe(Path(self.tmp.name) / "nope")
        self.assertFalse(state["installed"])
        self.assertIn("没有找到 NapCat", state["message"])

    def test_ports_come_from_napcat_config(self) -> None:
        write_json(self.root / "config" / "webui.json",
                   {"host": "127.0.0.1", "port": 7211, "token": "should-not-leak"})
        write_json(self.root / "config" / "onebot11_12345.json", {
            "network": {"websocketServers": [
                {"enable": True, "host": "127.0.0.1", "port": 3399, "token": "secret-token"},
            ]},
        })
        state = napcat.describe(self.root)
        self.assertTrue(state["installed"])
        self.assertEqual(state["webui_url"], "http://127.0.0.1:7211")
        self.assertEqual(state["onebot_ws"], "ws://127.0.0.1:3399")
        self.assertEqual(state["accounts"], ["12345"])
        payload = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("secret-token", payload)
        self.assertNotIn("should-not-leak", payload)

    def test_disabled_server_falls_back_to_default_port(self) -> None:
        write_json(self.root / "config" / "onebot11_777.json", {
            "network": {"websocketServers": [{"enable": False, "port": 5000}]},
        })
        state = napcat.describe(self.root)
        self.assertEqual(state["onebot_ws"], f"ws://127.0.0.1:{napcat.DEFAULT_ONEBOT_PORT}")

    def test_not_running_message_points_at_launcher(self) -> None:
        """没运行时不再让用户去双击 bat，而是告诉他在应用里点「启动 NapCat」。"""
        launcher = self.root / "launcher-user.bat"
        launcher.write_text("@echo off\n", encoding="utf-8")
        state = napcat.describe(self.root)
        self.assertFalse(state["onebot_running"])
        self.assertEqual(state["launcher"], str(launcher))
        self.assertIn("启动 NapCat", state["message"])
        self.assertIn("没在运行", state["message"])

    def test_webui_up_but_onebot_down_asks_for_qq_login(self) -> None:
        """NapCat 起来了但 OneBot 没监听，通常是 QQ 还没扫码。"""
        write_json(self.root / "config" / "webui.json", {"host": "127.0.0.1", "port": 6099})
        self.ports.add(6099)
        state = napcat.describe(self.root)
        self.assertTrue(state["webui_running"])
        self.assertFalse(state["onebot_running"])
        self.assertIn("等 QQ 登录", state["message"])

    def test_running_detected_by_port(self) -> None:
        write_json(self.root / "config" / "onebot11_888.json", {
            "network": {"websocketServers": [{"enable": True, "port": 8888}]},
        })
        self.ports.add(8888)
        state = napcat.describe(self.root)
        self.assertTrue(state["onebot_running"])
        self.assertIn("正在运行", state["message"])


class NapcatQrcodeTest(unittest.TestCase):
    """登录二维码只在 NapCat 控制台里，界面必须能直接拿到。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "NapCat"
        (self.root / "config").mkdir(parents=True)
        (self.root / "cache").mkdir(parents=True)
        (self.root / "NapCatWinBootMain.exe").write_bytes(b"stub")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_qrcode(self) -> None:
        state = napcat.qrcode_state(self.root)
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "no_qrcode")

    def test_fresh_qrcode_is_returned(self) -> None:
        (self.root / "cache" / "qrcode.png").write_bytes(PNG_BYTES)
        state = napcat.qrcode_state(self.root)
        self.assertTrue(state["available"])
        self.assertTrue(state["data_url"].startswith("data:image/png;base64,"))
        self.assertGreaterEqual(state["age_seconds"], 0)

    def test_expired_qrcode_is_hidden(self) -> None:
        png = self.root / "cache" / "qrcode.png"
        png.write_bytes(PNG_BYTES)
        old = 1_600_000_000
        os.utime(png, (old, old))
        state = napcat.qrcode_state(self.root, max_age=60)
        self.assertFalse(state["available"])
        self.assertEqual(state["reason"], "expired")

    def test_decoded_url_comes_from_launch_log(self) -> None:
        (self.root / "cache" / "qrcode.png").write_bytes(PNG_BYTES)
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "ilink-launch.log").write_text(
            "请扫描下面的二维码\n二维码解码URL: https://txz.qq.com/p?k=abc&f=1\n",
            encoding="utf-8",
        )
        state = napcat.qrcode_state(self.root)
        self.assertEqual(state["decoded_url"], "https://txz.qq.com/p?k=abc&f=1")


class NapcatLaunchTest(unittest.TestCase):
    """软件内启动 NapCat：只认探测到的固定路径，缺 QQ / 缺文件都如实拒绝。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "NapCat"
        self.root.mkdir(parents=True)
        (self.root / "NapCatWinBootMain.exe").write_bytes(b"stub")
        (self.root / "NapCatWinBootHook.dll").write_bytes(b"stub")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_install(self) -> None:
        with patch.object(napcat, "find_napcat_dir", return_value=None):
            result = napcat.launch()
        self.assertFalse(result["started"])
        self.assertEqual(result["code"], "not_installed")

    def test_incomplete_directory(self) -> None:
        (self.root / "NapCatWinBootMain.exe").unlink()
        with patch.object(napcat, "find_napcat_dir", return_value=self.root):
            result = napcat.launch()
        self.assertFalse(result["started"])
        self.assertEqual(result["code"], "napcat_incomplete")

    def test_already_running(self) -> None:
        with patch.object(napcat, "find_napcat_dir", return_value=self.root), \
                patch.object(napcat, "_port_open", return_value=True):
            result = napcat.launch()
        self.assertFalse(result["started"])
        self.assertEqual(result["code"], "already_running")

    def test_webui_only_also_counts_as_running(self) -> None:
        """WebUI 在监听就说明 NapCat 活着；再 spawn 会多注入一个 QQ。"""
        with patch.object(napcat, "find_napcat_dir", return_value=self.root), \
                patch.object(napcat, "_port_open",
                             side_effect=lambda host, port, timeout=0.5: port == 6099):
            result = napcat.launch()
        self.assertFalse(result["started"])
        self.assertEqual(result["code"], "already_running")

    def test_qq_missing(self) -> None:
        with patch.object(napcat, "find_napcat_dir", return_value=self.root), \
                patch.object(napcat, "_port_open", return_value=False), \
                patch.object(napcat, "find_qq_exe", return_value=""):
            result = napcat.launch()
        self.assertFalse(result["started"])
        self.assertEqual(result["code"], "qq_missing")

    def test_launch_spawns_fixed_paths_with_env(self) -> None:
        class FakeProcess:
            pid = 4321

        calls: list[tuple[list[str], dict]] = []

        def fake_popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeProcess()

        with patch.object(napcat, "find_napcat_dir", return_value=self.root), \
                patch.object(napcat, "_port_open", return_value=False), \
                patch.object(napcat, "find_qq_exe", return_value="D:/qq/QQ.exe"), \
                patch("subprocess.Popen", side_effect=fake_popen):
            result = napcat.launch()

        self.assertTrue(result["started"])
        self.assertEqual(result["pid"], 4321)
        argv, kwargs = calls[0]
        self.assertEqual(argv[0], str(self.root / "NapCatWinBootMain.exe"))
        self.assertEqual(argv[1], "D:/qq/QQ.exe")
        self.assertEqual(argv[2], str(self.root / "NapCatWinBootHook.dll"))
        self.assertEqual(kwargs["env"]["NAPCAT_INJECT_PATH"],
                         str(self.root / "NapCatWinBootHook.dll"))
        self.assertEqual(str(kwargs["cwd"]), str(self.root))


if __name__ == "__main__":
    unittest.main()
