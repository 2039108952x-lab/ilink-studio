"""B 站 MCP 客户端：错误归类、脚本查找顺序、环境变量注入。

真实起子进程的用例要 ``mcp<2`` 装好才跑得住（本机默认没有），所以它按环境跳过 ——
不 skip 就会把「没装依赖」报成代码坏了，那是两件事。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from gateway.bili_mcp import BiliMCP, build_client, classify, find_server


class ClassifyTest(unittest.TestCase):
    def test_not_logged_in_first(self) -> None:
        self.assertEqual(
            classify({"error": "未登录B站！请先调用 bili_login 工具获取登录二维码"}), "not_logged_in")

    def test_style_gate_is_not_a_failure(self) -> None:
        """风格闸门返回 ok:false，但那是「换个写法」不是「工具坏了」。"""
        self.assertEqual(classify({"ok": False, "blocked_by": "style",
                                   "reason": "开头『这条我认』是鱼八股"}), "style_blocked")

    def test_dm_too_long(self) -> None:
        self.assertEqual(classify({"ok": False, "code": 21007}), "too_long")

    def test_deps_missing(self) -> None:
        self.assertEqual(classify({"error": "转写环境没装好（设置页里有「装转写依赖」）"}),
                         "deps_missing")
        self.assertEqual(classify({"error": "No module named 'faster_whisper'"}), "deps_missing")

    def test_rate_limited(self) -> None:
        self.assertEqual(classify({"error": "请求过于频繁，请稍后再试"}), "rate_limited")

    def test_invalid_args(self) -> None:
        self.assertEqual(classify({"error": "没有这个 job"}), "invalid_args")

    def test_fallback(self) -> None:
        self.assertEqual(classify({"error": "拉评论失败"}), "tool_failed")
        self.assertEqual(classify({}), "tool_failed")


class FindServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _touch(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("print('mcp')\n", encoding="utf-8")
        return path

    def test_home_wins(self) -> None:
        home = self._touch(self.root / "home" / "mcp_server.py")
        self._touch(self.root / "vendor" / "dsh-bili-scroll" / "vendor"
                    / "bilibili-mcp" / "mcp_server.py")
        found = find_server(self.root, home=self.root / "home")
        self.assertEqual(found, home)

    def test_resource_beats_repo(self) -> None:
        packaged = self._touch(self.root / "res" / "bili-mcp" / "mcp_server.py")
        self._touch(self.root / "vendor" / "dsh-bili-scroll" / "vendor"
                    / "bilibili-mcp" / "mcp_server.py")
        found = find_server(self.root, self.root / "res", home=self.root / "nohome")
        self.assertEqual(found, packaged)

    def test_repo_fallback(self) -> None:
        repo = self._touch(self.root / "vendor" / "dsh-bili-scroll" / "vendor"
                           / "bilibili-mcp" / "mcp_server.py")
        self.assertEqual(find_server(self.root, home=self.root / "nohome"), repo)


class BuildClientTest(unittest.TestCase):
    def test_command_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "bili-home"
            home.mkdir()
            script = home / "mcp_server.py"
            script.write_text("print('mcp')\n", encoding="utf-8")
            client = build_client(repo_root=root, home=home, tools=["bili_search"],
                                  style_gate=False)
            command = getattr(client, "_command")
            self.assertEqual(command[-1], str(script))
            self.assertEqual(len(command), 2)
            env = getattr(client, "_env")
            self.assertEqual(env["BILI_DUTY_HOME"], str(home))
            self.assertEqual(env["BILI_MCP_TOOLS"], "bili_search")
            self.assertEqual(env["BILI_STYLE_OFF"], "1")

    def test_style_gate_on_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bili-home"
            home.mkdir()
            client = BiliMCP(command=["python", "x.py"], home=home)
            self.assertNotIn("BILI_STYLE_OFF", getattr(client, "_env"))
            self.assertNotIn("BILI_MCP_TOOLS", getattr(client, "_env"))


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_mcp_available(), "本机没装 mcp<2，起不了 Python MCP 子进程")
class LiveToolsTest(unittest.TestCase):
    """真起一次子进程拿 tools/list。只在依赖齐了时跑。"""

    def test_tools_list(self) -> None:
        from gateway.bili_home import ensure_home

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = ensure_home(root / "data", source_root=Path("vendor/dsh-bili-scroll"))
            self.assertTrue(state.ready, state.to_dict())
            client = build_client(repo_root=Path.cwd(), home=state.home)

            async def run() -> list[dict]:
                try:
                    return await client.list_tools()
                finally:
                    await client.close()

            tools = asyncio.run(run())
            names = {item.get("name") for item in tools}
            self.assertIn("bili_search", names)
            self.assertIn("bili_subtitle", names)


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    """控制面会同时打 status 和 doctor，两个请求共用同一个 MCP 管道。

    真机事故：``list_tools()`` 当时没拿锁，与 ``call()`` 撞在一起就抛
    「readuntil() called while another coroutine is already waiting for
    incoming data」，界面上显示「未就绪 + 0 个工具」。
    """

    async def test_list_tools_and_call_share_one_pipe_safely(self) -> None:
        fake = Path(__file__).with_name("fake_mcp_server.py")
        instance = BiliMCP(
            command=[sys.executable, str(fake), "normal"],
            home=tempfile.gettempdir(), timeout=15.0, max_restarts=0,
        )
        try:
            results = await asyncio.gather(
                instance.list_tools(),
                instance.call("bili_check_credential", {}),
                instance.list_tools(),
                return_exceptions=True,
            )
        finally:
            await instance.close()
        for item in results:
            self.assertNotIsInstance(item, Exception, f"并发调 MCP 炸了：{item}")


if __name__ == "__main__":
    unittest.main()
