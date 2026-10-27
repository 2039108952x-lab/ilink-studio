"""MCP server 协议层测试：不联网，HTTP 用假 Client。"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import httpx

from gateway import mcp_server


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """冒充 httpx.Client，记录调用并返回预设响应。"""

    def __init__(self, *, post_response=None, get_response=None, raise_error=None) -> None:
        self._post = post_response
        self._get = get_response
        self._raise = raise_error
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, url: str, json: dict | None = None, headers: dict | None = None):
        self.calls.append(("post", {"url": url, "json": json, "headers": headers}))
        if self._raise is not None:
            raise self._raise
        return self._post

    def get(self, url: str):
        self.calls.append(("get", {"url": url}))
        if self._raise is not None:
            raise self._raise
        return self._get


def patch_client(fake: FakeClient):
    return mock.patch.object(mcp_server.httpx, "Client", lambda *a, **kw: fake)


class TestProtocol(unittest.TestCase):
    def test_initialize(self) -> None:
        resp = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        assert resp is not None
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "ilink")

    def test_initialized_notification_returns_none(self) -> None:
        self.assertIsNone(
            mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_ping(self) -> None:
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        assert resp is not None
        self.assertEqual(resp["result"], {})

    def test_tools_list_exposes_all_tools(self) -> None:
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        assert resp is not None
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(
            names,
            ["send_message", "list_platforms", "bili_video", "douyin_video",
             "bili_scan", "bili_picks", "bili_daily", "bili_comment"],
        )

    def test_bili_comment_needs_confirm_in_schema(self) -> None:
        """发评论是拿使用者的号公开说话：schema 里必须留着 confirm 这道闸。"""
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "bili_comment")
        properties = tool["inputSchema"]["properties"]
        self.assertIn("confirm", properties)
        self.assertFalse(properties["confirm"].get("default", False))
        self.assertEqual(tool["inputSchema"]["required"], ["item_id", "text"])

    def test_douyin_video_schema(self) -> None:
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "douyin_video")
        self.assertEqual(tool["inputSchema"]["required"], ["video"])

    def test_douyin_video_rejects_empty_argument(self) -> None:
        self.assertIn("缺少必填参数", mcp_server._douyin_video({}))

    def test_douyin_video_without_token_explains(self) -> None:
        """没配 token 时要说清楚，而不是让网关返回 401 却看不懂。"""
        original = mcp_server.TOKEN
        mcp_server.TOKEN = ""
        try:
            text = mcp_server._douyin_video({"video": "7652392308650149233"})
        finally:
            mcp_server.TOKEN = original
        self.assertIn("ILINK_GATEWAY_TOKEN", text)

    def test_format_douyin_brief_layout(self) -> None:
        text = mcp_server.format_douyin_brief(
            {
                "ok": True,
                "aweme_id": "7652392308650149233",
                "url": "https://www.douyin.com/video/7652392308650149233",
                "title": "猫猫日常",
                "author": "demo_account",
                "detail": "今天猫又拆家了",
                "comment_count": 2,
                "comments": ["张三：可爱", "李四：哈哈"],
            }
        )
        self.assertIn("猫猫日常", text)
        self.assertIn("demo_account", text)
        self.assertIn("1. 张三：可爱", text)
        self.assertIn("2. 李四：哈哈", text)

    def test_format_douyin_brief_without_comments(self) -> None:
        text = mcp_server.format_douyin_brief(
            {"ok": True, "title": "空评论", "author": "demo_account", "comments": []}
        )
        self.assertIn("还没读到评论", text)

    def test_bili_video_schema_requires_video(self) -> None:
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "bili_video")
        self.assertEqual(tool["inputSchema"]["required"], ["video"])

    def test_bili_video_rejects_empty_argument(self) -> None:
        self.assertIn("缺少必填参数", mcp_server._bili_video({}))

    def test_bili_video_reports_lookup_failure(self) -> None:
        # 认不出的字符串一定走失败分支，不用 mock
        text = mcp_server._bili_video({"video": "这不是一个视频"})
        self.assertIn("看视频失败", text)

    def test_format_video_brief_layout(self) -> None:
        text = mcp_server.format_video_brief(
            {
                "ok": True,
                "bvid": "BV1xx411c7mD",
                "title": "标题",
                "up": "UP主",
                "desc": "简介",
                "duration_sec": 125,
                "view": 1000,
                "like": 50,
                "pages": 1,
                "subtitle": "字幕正文",
            }
        )
        self.assertIn("《标题》", text)
        self.assertIn("UP主", text)
        self.assertIn("2分5秒", text)
        self.assertIn("BV1xx411c7mD", text)
        self.assertIn("字幕正文", text)

    def test_format_video_brief_without_subtitle(self) -> None:
        text = mcp_server.format_video_brief(
            {"ok": True, "bvid": "BV1xx411c7mD", "title": "t", "subtitle_note": "这个视频没有 CC 字幕"}
        )
        self.assertIn("没有字幕", text)

    def test_human_duration_edges(self) -> None:
        self.assertEqual(mcp_server._human_duration(0), "未知")
        self.assertEqual(mcp_server._human_duration(None), "未知")
        self.assertEqual(mcp_server._human_duration(45), "0分45秒")
        self.assertEqual(mcp_server._human_duration(3700), "1小时1分")

    def test_send_message_schema_requires_three_fields(self) -> None:
        tool = next(t for t in mcp_server.TOOLS if t["name"] == "send_message")
        self.assertEqual(
            tool["inputSchema"]["required"], ["platform", "chat_id", "text"]
        )
        self.assertEqual(
            tool["inputSchema"]["properties"]["platform"]["enum"],
            ["qq", "wechat", "bilibili", "douyin"],
        )

    def test_unknown_method(self) -> None:
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        assert resp is not None
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool(self) -> None:
        resp = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            }
        )
        assert resp is not None
        self.assertEqual(resp["error"]["code"], -32602)


class TestSendMessage(unittest.TestCase):
    def test_missing_required_argument(self) -> None:
        resp = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "send_message", "arguments": {"platform": "qq"}},
            }
        )
        assert resp is not None
        text = resp["result"]["content"][0]["text"]
        self.assertIn("缺少必填参数", text)

    def test_no_token_is_reported(self) -> None:
        with mock.patch.object(mcp_server, "TOKEN", ""):
            text = mcp_server._send_message(
                {"platform": "qq", "chat_id": "1", "text": "hi"}
            )
        self.assertIn("ILINK_GATEWAY_TOKEN", text)

    def test_success_hits_send_endpoint(self) -> None:
        fake = FakeClient(post_response=FakeResponse(200, {"ok": True, "msg_id": "m1"}))
        with mock.patch.object(mcp_server, "TOKEN", "tok"), patch_client(fake):
            text = mcp_server._send_message(
                {"platform": "qq", "chat_id": "123", "text": "你好", "kind": "group"}
            )
        self.assertIn("已发送", text)
        self.assertIn("m1", text)
        call = fake.calls[0][1]
        self.assertTrue(call["url"].endswith("/send"))
        self.assertEqual(call["json"]["kind"], "group")
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok")

    def test_gateway_error_is_surfaced(self) -> None:
        fake = FakeClient(post_response=FakeResponse(502, {"ok": False, "error": "没有 douyin 的适配器"}))
        with mock.patch.object(mcp_server, "TOKEN", "tok"), patch_client(fake):
            text = mcp_server._send_message(
                {"platform": "douyin", "chat_id": "1", "text": "x"}
            )
        self.assertIn("发送失败", text)
        self.assertIn("没有 douyin", text)

    def test_network_error_is_surfaced(self) -> None:
        fake = FakeClient(raise_error=httpx.ConnectError("拒绝连接"))
        with mock.patch.object(mcp_server, "TOKEN", "tok"), patch_client(fake):
            text = mcp_server._send_message(
                {"platform": "qq", "chat_id": "1", "text": "x"}
            )
        self.assertIn("连不上网关", text)

    def test_tool_exception_becomes_tool_error(self) -> None:
        def boom(arguments: dict) -> str:
            raise RuntimeError("炸了")

        # HANDLERS 在导入时就绑定了函数引用，改模块属性对它无效，必须改字典本身
        with mock.patch.object(mcp_server, "TOKEN", "tok"), mock.patch.dict(
            mcp_server.HANDLERS, {"send_message": boom}
        ):
            resp = mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "send_message",
                        "arguments": {"platform": "qq", "chat_id": "1", "text": "x"},
                    },
                }
            )
        assert resp is not None
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("炸了", resp["result"]["content"][0]["text"])


class TestListPlatforms(unittest.TestCase):
    def test_lists_platforms(self) -> None:
        fake = FakeClient(get_response=FakeResponse(200, {"ok": True, "platforms": ["qq", "bilibili"]}))
        with patch_client(fake):
            text = mcp_server._list_platforms({})
        self.assertIn("qq", text)
        self.assertIn("bilibili", text)

    def test_empty_platforms(self) -> None:
        fake = FakeClient(get_response=FakeResponse(200, {"ok": True, "platforms": []}))
        with patch_client(fake):
            self.assertIn("没有启用", mcp_server._list_platforms({}))

    def test_unreachable_gateway(self) -> None:
        fake = FakeClient(raise_error=httpx.ConnectError("拒绝连接"))
        with patch_client(fake):
            self.assertIn("连不上网关", mcp_server._list_platforms({}))


class TestStdio(unittest.TestCase):
    def test_response_is_single_line_json(self) -> None:
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        assert resp is not None
        line = json.dumps(resp, ensure_ascii=False)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["id"], 9)


if __name__ == "__main__":
    unittest.main()
