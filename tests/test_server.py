"""控制面 HTTP 的集成测试：真起服务、真发请求。

这一层正是之前漏掉的——纯逻辑测试全绿，但 /health 仍带着 AttributeError。
凡是「拼装属性名」的地方，只有真跑一次才靠得住。
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from gateway.adapters.base import Adapter
from gateway.brain import Reply
from gateway.mailbox import Mailbox
from gateway.models import OutboundMessage, Platform, SendResult
from gateway.router import Router
from gateway.server import GatewayServer


class FakeAdapter(Adapter):
    platform = Platform.QQ

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutboundMessage] = []
        self.video_calls: list[tuple[str, int]] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> SendResult:
        self.sent.append(msg)
        return SendResult(ok=True, msg_id=f"id-{len(self.sent)}")

    async def video_brief(self, ref: str, *, max_comments: int = 20) -> dict:
        self.video_calls.append((ref, max_comments))
        return {
            "ok": True,
            "aweme_id": ref,
            "title": "猫猫日常",
            "author": "demo_account",
            "comment_count": 0,
            "comments": [],
        }


class FakeBrain:
    async def respond(self, ctx, text: str, attachments=None) -> Reply:
        return Reply(text="答: " + text)


def make_mailbox(test: unittest.TestCase) -> Mailbox:
    """控制面测试也要一个真库：Router 不接受没有持久化的模式。"""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    mailbox = Mailbox(Path(tmp.name) / "mailbox.db")
    test.addCleanup(mailbox.close)
    mailbox.ensure_account("qq", "onebot")
    return mailbox


class ServerTestBase(unittest.TestCase):
    token = "test-token"

    def setUp(self) -> None:
        # 控制面的异步逻辑要在活着的 loop 里跑，所以单独开线程
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        self.adapter = FakeAdapter()
        self.mailbox = make_mailbox(self)
        self.router = Router(
            FakeBrain(), {Platform.QQ: self.adapter}, store=self.mailbox, allow_all=True
        )
        self.server = GatewayServer(
            self.router, host="127.0.0.1", port=0, token=self.token, loop=self.loop
        )
        self.server.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    # ---- 请求助手 ----

    def get(self, path: str) -> tuple[int, dict]:
        return self._request(path, "GET")

    def post(self, path: str, body: dict | None, token: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self._request(path, "POST", data=data, headers=headers)

    def _request(self, path: str, method: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class TestHealth(ServerTestBase):
    def test_health_reports_platforms(self) -> None:
        status, body = self.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["platforms"], ["qq"])

    def test_unknown_path_is_404(self) -> None:
        status, body = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_health_ignores_query_string(self) -> None:
        status, body = self.get("/health?verbose=1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


class TestSendAuth(ServerTestBase):
    def test_missing_token_is_401(self) -> None:
        status, body = self.post("/send", {"platform": "qq", "chat_id": "1", "text": "hi"})
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])
        self.assertEqual(self.adapter.sent, [])

    def test_wrong_token_is_401(self) -> None:
        status, _ = self.post(
            "/send", {"platform": "qq", "chat_id": "1", "text": "hi"}, token="wrong"
        )
        self.assertEqual(status, 401)

    def test_unknown_post_path_is_404(self) -> None:
        status, _ = self.post("/nope", {"a": 1}, token=self.token)
        self.assertEqual(status, 404)


class TestSend(ServerTestBase):
    def test_send_delivers_to_adapter(self) -> None:
        status, body = self.post(
            "/send",
            {"platform": "qq", "kind": "private", "chat_id": "123", "text": "你好"},
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["msg_id"], "id-1")
        self.assertEqual(self.adapter.sent[0].text, "你好")
        self.assertEqual(self.adapter.sent[0].chat_id, "123")

    def test_send_defaults_to_private(self) -> None:
        self.post("/send", {"platform": "qq", "chat_id": "123", "text": "x"}, token=self.token)
        self.assertEqual(self.adapter.sent[0].kind.value, "private")

    def test_unknown_platform_is_400(self) -> None:
        status, body = self.post(
            "/send", {"platform": "weibo", "chat_id": "1", "text": "x"}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_missing_text_is_400(self) -> None:
        status, body = self.post("/send", {"platform": "qq", "chat_id": "1"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_platform_without_adapter_is_502(self) -> None:
        status, body = self.post(
            "/send", {"platform": "douyin", "chat_id": "1", "text": "x"}, token=self.token
        )
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        self.assertIn("douyin", body["error"])

    def test_empty_body_is_400(self) -> None:
        status, body = self.post("/send", None, token=self.token)
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])


class TestVideo(ServerTestBase):
    """/video：看视频（抖音要登录态，只能借网关的浏览器）。"""

    def test_missing_token_is_401(self) -> None:
        status, _ = self.post(
            "/video", {"platform": "qq", "video": "7652392308650149233"}
        )
        self.assertEqual(status, 401)

    def test_missing_video_is_400(self) -> None:
        status, body = self.post("/video", {"platform": "qq"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("video", body["error"])

    def test_unknown_platform_is_400(self) -> None:
        status, _ = self.post(
            "/video", {"platform": "weibo", "video": "1"}, token=self.token
        )
        self.assertEqual(status, 400)

    def test_platform_without_adapter_is_502(self) -> None:
        status, body = self.post(
            "/video", {"platform": "douyin", "video": "7652392308650149233"}, token=self.token
        )
        self.assertEqual(status, 502)
        self.assertIn("douyin", body["error"])

    def test_delegates_to_adapter(self) -> None:
        status, body = self.post(
            "/video",
            {"platform": "qq", "video": "7652392308650149233", "max_comments": 5},
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["title"], "猫猫日常")
        # 评论条数限制要真的传下去
        self.assertEqual(self.adapter.video_calls, [("7652392308650149233", 5)])

    def test_default_max_comments(self) -> None:
        self.post("/video", {"platform": "qq", "video": "7652392308650149233"}, token=self.token)
        self.assertEqual(self.adapter.video_calls, [("7652392308650149233", 20)])


class TestLoopbackGuard(unittest.TestCase):
    def test_rejects_public_bind(self) -> None:
        router = Router(FakeBrain(), {}, store=make_mailbox(self), allow_all=True)
        for host in ("0.0.0.0", "192.168.1.5", "::"):
            with self.assertRaises(ValueError):
                GatewayServer(router, host=host, port=0, token="t")


class TestPortConflict(unittest.TestCase):
    """Windows 允许两个进程绑同一端口，所以「bind 成功」不能当成「端口是我的」。

    真机上就栽在这上面：旧网关没人认领仍占着 8787，新网关也起来了，
    请求落到旧那个，界面报「桌面会话鉴权失败」，看起来像凭据坏了。
    """

    def test_refuses_when_port_already_listening(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            router = Router(FakeBrain(), {}, store=make_mailbox(self), allow_all=True)
            with self.assertRaises(ValueError) as ctx:
                GatewayServer(router, host="127.0.0.1", port=port, token="t")
            self.assertIn("已有进程在监听", str(ctx.exception))
        finally:
            holder.close()

    def test_free_port_still_binds(self) -> None:
        loop = asyncio.new_event_loop()
        router = Router(FakeBrain(), {}, store=make_mailbox(self), allow_all=True)
        server = GatewayServer(router, host="127.0.0.1", port=0, token="t", loop=loop)
        server.start()
        try:
            host, port = server.address
            self.assertEqual(host, "127.0.0.1")
            self.assertGreater(port, 0)
        finally:
            server.stop()
            loop.close()


if __name__ == "__main__":
    unittest.main()
