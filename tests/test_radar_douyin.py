"""抖音 provider：字段映射、评论容错、confirm 透传、记忆双写、错误归类。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.douyin_mcp import CallResult
from gateway.radar import memory as memory_mod
from gateway.radar.douyin import DouyinRadar, _classify
from gateway.radar.provider import RadarError, ShareDraft

FORWARD_PAYLOAD = {
    "ok": True, "picked": 2,
    "sources": [{"src": "feed#1", "new": 2, "status": 0}],
    "dropped": {"no_desc": 1, "too_short_video": 3, "short_and_quiet": 0},
    "list": [
        {
            "aweme_id": "7600000000000000001", "desc": "加班到三点回家猫以为我死了",
            "author": "铲屎官", "duration_s": 15, "digg": 128000, "comment": 8600,
            "collect": 9000, "share": 21000, "age_h": 3,
            "share_url": "https://www.iesdouyin.com/share/video/7600000000000000001/",
            "web_url": "https://www.douyin.com/video/7600000000000000001",
        },
        {
            "aweme_id": "7600000000000000002", "desc": "另一个", "author": "别人",
            "duration_s": 30, "digg": 0, "comment": 0, "collect": 0, "share": 0,
            "share_url": "",
        },
    ],
}

COMMENTS_PAYLOAD = {
    "ok": True,
    "comments": [
        {"text": "笑死", "likes": 1200},
        {"text": "绷不住", "likes": 900},
        {"content": "标题党", "digg": 5},
    ],
}


class FakeClient:
    def __init__(self, table: dict[str, dict], *, ok: bool = True) -> None:
        self.table = table
        self.calls: list[tuple[str, dict]] = []
        self.ok = ok
        self.alive = True

    async def call(self, tool, arguments=None, *, timeout=None):
        self.calls.append((tool, dict(arguments or {})))
        payload = self.table.get(tool, {"ok": False, "error": "unknown tool"})
        ok = bool(payload.get("ok")) if self.ok else False
        from gateway.douyin_mcp import describe_error
        return CallResult(tool=tool, ok=ok, data=payload,
                          error="" if ok else describe_error(payload))

    async def close(self):
        self.alive = False


class CandidatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_maps_fields_to_canonical(self) -> None:
        radar = DouyinRadar(FakeClient({"douyin_forward": FORWARD_PAYLOAD}), tempfile.gettempdir())
        items = await radar.candidates(top=2)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first.item_id, "7600000000000000001")
        self.assertEqual(first.duration_sec, 15)
        self.assertEqual(first.stats["like"], 128000)
        self.assertEqual(first.stats["share"], 21000)
        self.assertTrue(first.url.startswith("https://www.iesdouyin.com/share/video/"))

    async def test_missing_list_is_empty(self) -> None:
        radar = DouyinRadar(FakeClient({"douyin_forward": {"ok": True}}), tempfile.gettempdir())
        self.assertEqual(await radar.candidates(), [])


class CommentsTest(unittest.IsolatedAsyncioTestCase):
    async def test_parses_and_sorts(self) -> None:
        radar = DouyinRadar(FakeClient({"douyin_comments": COMMENTS_PAYLOAD}),
                            tempfile.gettempdir())
        items = await radar.comments("7600000000000000001", limit=2)
        self.assertEqual([c.text for c in items], ["笑死", "绷不住"])

    async def test_failure_returns_empty(self) -> None:
        radar = DouyinRadar(FakeClient({"douyin_comments": {"ok": False, "error": "拉评论失败"}}),
                            tempfile.gettempdir())
        self.assertEqual(await radar.comments("x"), [])


class SendTest(unittest.IsolatedAsyncioTestCase):
    async def test_share_without_confirm_sends_no_confirm_flag(self) -> None:
        client = FakeClient({"douyin_share_card": {"ok": True, "sent": False,
                                                   "needsConfirm": True, "to": "demo_friend",
                                                   "note": "没发送"}})
        radar = DouyinRadar(client, tempfile.gettempdir())
        draft = await radar.send_share(ShareDraft(item_id="111", to="demo_friend", reason="好看",
                                                  preview=""))
        self.assertFalse(draft.sent)
        self.assertNotIn("confirm", client.calls[0][1])

    async def test_share_with_confirm_passes_flag(self) -> None:
        client = FakeClient({"douyin_share_card": {"ok": True, "sent": True}})
        radar = DouyinRadar(client, tempfile.gettempdir())
        await radar.send_share(ShareDraft(item_id="111", to="demo_friend", reason="好看", preview=""),
                               confirm=True)
        self.assertTrue(client.calls[0][1]["confirm"])

    async def test_dm_confirm_flag(self) -> None:
        client = FakeClient({"douyin_dm": {"ok": True, "sent": False, "needsConfirm": True}})
        radar = DouyinRadar(client, tempfile.gettempdir())
        await radar.send_text("demo_friend", "在吗")
        self.assertNotIn("confirm", client.calls[0][1])
        await radar.send_text("demo_friend", "在吗", confirm=True)
        self.assertTrue(client.calls[1][1]["confirm"])


class RememberTest(unittest.IsolatedAsyncioTestCase):
    async def test_writes_plugin_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            radar = DouyinRadar(FakeClient({}), tmp)
            result = await radar.remember("sec-abc", ["搬到成都了", " "])
            self.assertTrue(result["ok"])
            self.assertEqual(result["written"], 1)
            raw = memory_mod.read_dm_memory(tmp, "sec-abc")
            self.assertEqual(raw["messages"][-1]["text"], "搬到成都了")


class ClassifyTest(unittest.TestCase):
    def test_login_and_deps(self) -> None:
        self.assertEqual(_classify({"error": "还没有登录"}), "not_logged_in")
        self.assertEqual(_classify({"error": "还没装运行环境"}), "deps_missing")
        self.assertEqual(_classify({"error": "缺 name（要 @ 的昵称）"}), "invalid_args")

    def test_rate_limited(self) -> None:
        self.assertEqual(_classify({"error": "发送太密", "hint": "rate_limited: true"}),
                         "rate_limited")

    def test_default(self) -> None:
        self.assertEqual(_classify({"error": "别的错"}), "tool_failed")


class ErrorPropagationTest(unittest.IsolatedAsyncioTestCase):
    async def test_business_failure_raises_radar_error(self) -> None:
        radar = DouyinRadar(FakeClient({"douyin_forward": {"ok": False, "error": "还没有登录"}}),
                            tempfile.gettempdir())
        with self.assertRaises(RadarError) as caught:
            await radar.candidates()
        self.assertEqual(caught.exception.code, "not_logged_in")


class DoctorTest(unittest.IsolatedAsyncioTestCase):
    """体检结果要能直接被 json.dumps。

    真机事故：``doctor()`` 把 ``CallResult`` 原样返回，控制面 json.dumps 抛 TypeError，
    连接被掐断，界面只显示 socket hang up，整个雷达页卡在「正在读登录状态…」。
    """

    async def test_doctor_returns_plain_dict(self) -> None:
        radar = DouyinRadar(
            FakeClient({"douyin_doctor": {"ok": True, "checks": [{"key": "node", "ok": True}]}}),
            tempfile.gettempdir())
        out = await radar.doctor()
        self.assertIsInstance(out, dict)
        self.assertTrue(out["ok"])
        json.dumps(out, ensure_ascii=False)

    async def test_doctor_failure_stays_serializable(self) -> None:
        radar = DouyinRadar(
            FakeClient({"douyin_doctor": {"ok": False, "error": "没有装 Node"}}),
            tempfile.gettempdir())
        out = await radar.doctor()
        self.assertIsInstance(out, dict)
        self.assertFalse(out["ok"])
        self.assertIn("Node", out["error"])
        json.dumps(out, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
