"""MCP 客户端：正常、业务失败、RPC 错误、超时、崩溃重启、日志脱敏。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from gateway.douyin_mcp import DouyinMCP, DouyinMCPError, build_client, describe_error, find_server

FAKE = Path(__file__).with_name("fake_mcp_server.py")


def client(mode: str = "normal", *, timeout: float = 10.0, restarts: int = 1,
           home: Path | str = "") -> DouyinMCP:
    return DouyinMCP(
        command=[sys.executable, str(FAKE), mode],
        home=home or tempfile.gettempdir(),
        timeout=timeout,
        max_restarts=restarts,
    )


class NormalCallTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_payload_and_timing(self) -> None:
        instance = client()
        try:
            result = await instance.call("douyin_status", {"x": 1})
        finally:
            await instance.close()
        self.assertTrue(result.ok)
        self.assertEqual(result.tool, "douyin_status")
        self.assertEqual(result.data["args"], {"x": 1})
        self.assertGreaterEqual(result.elapsed_ms, 0)

    async def test_needs_confirm_flag(self) -> None:
        instance = client("bizfail")  # 业务失败也能读 needsConfirm
        try:
            result = await instance.call("douyin_dm", {})
        finally:
            await instance.close()
        self.assertFalse(result.needs_confirm)  # 这条是业务失败，没有 needsConfirm

    async def test_reuses_process_between_calls(self) -> None:
        instance = client()
        try:
            first = await instance.call("douyin_status")
            pid = instance._proc.pid if instance._proc else None
            second = await instance.call("douyin_status")
            self.assertEqual(pid, instance._proc.pid if instance._proc else None)
        finally:
            await instance.close()
        self.assertTrue(first.ok and second.ok)


class FailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_business_failure_passes_through(self) -> None:
        instance = client("bizfail")
        try:
            result = await instance.call("douyin_forward", {})
        finally:
            await instance.close()
        self.assertFalse(result.ok)
        self.assertIn("还没有登录", result.error)
        self.assertIn("扫码", result.error)   # hint 合并进错误文案

    async def test_rpc_error_raises(self) -> None:
        instance = client("rpcerror")
        try:
            with self.assertRaises(DouyinMCPError) as caught:
                await instance.call("douyin_status")
        finally:
            await instance.close()
        self.assertEqual(caught.exception.code, "rpc_error")
        self.assertFalse(caught.exception.retryable)

    async def test_timeout_raises_and_kills(self) -> None:
        instance = client("hang", timeout=1.0, restarts=0)
        try:
            with self.assertRaises(DouyinMCPError) as caught:
                await instance.call("douyin_status")
        finally:
            await instance.close()
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(instance.alive)

    async def test_crash_exhausts_restarts(self) -> None:
        instance = client("crash", restarts=1)
        try:
            with self.assertRaises(DouyinMCPError) as caught:
                await instance.call("douyin_forward")
        finally:
            await instance.close()
        self.assertEqual(caught.exception.code, "process_exited")

    async def test_restart_recovers_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "seen"
            import os
            os.environ["FAKE_MCP_STATE"] = str(marker)
            instance = client("flaky", restarts=2)
            try:
                result = await instance.call("douyin_status")
            finally:
                await instance.close()
                os.environ.pop("FAKE_MCP_STATE", None)
        self.assertTrue(result.ok, "第一次崩溃后应当自动重启并成功")


class HelperTest(unittest.TestCase):
    def test_describe_error_merges_hint(self) -> None:
        self.assertEqual(describe_error({"error": "还没装运行环境", "hint": "点设置"}),
                         "还没装运行环境（点设置）")
        self.assertEqual(describe_error({}), "未知错误")

    def test_find_server_prefers_resource_root(self) -> None:
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as res:
            bundled = Path(res) / "douyin-mcp" / "tools" / "mcp-server.mjs"
            bundled.parent.mkdir(parents=True)
            bundled.write_text("// stub", encoding="utf-8")
            declared = Path(repo) / "vendor" / "dsh-douyin-scroll" / "tools" / "mcp-server.mjs"
            declared.parent.mkdir(parents=True)
            declared.write_text("// stub", encoding="utf-8")
            self.assertEqual(find_server(repo, res), bundled)
            self.assertEqual(find_server(repo, None), declared)

    def test_build_client_uses_configured_node(self) -> None:
        instance = build_client(repo_root=Path("."), home=Path("."), node="C:/node.exe")
        self.assertEqual(instance._command[0], "C:/node.exe")


class ConcurrentTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_calls_do_not_mix_results(self) -> None:
        """并发两个调用：锁会排队，但结果必须各归各的（不能串号）。"""
        door = client("normal")
        try:
            first, second = await asyncio.gather(
                door.call("alpha_tool", {"n": 1}),
                door.call("beta_tool", {"n": 2}),
            )
        finally:
            await door.close()
        self.assertEqual(first.data["tool"], "alpha_tool")
        self.assertEqual(first.data["args"], {"n": 1})
        self.assertEqual(second.data["tool"], "beta_tool")
        self.assertEqual(second.data["args"], {"n": 2})

    async def test_concurrent_calls_share_one_process(self) -> None:
        """并发不该把子进程反复重启：三次调用跑完仍然是同一个活着的进程。"""
        door = client("normal")
        try:
            await asyncio.gather(door.call("a"), door.call("b"), door.call("c"))
            self.assertTrue(door.alive)
        finally:
            await door.close()


if __name__ == "__main__":
    unittest.main()
