"""B 站通道的契约测试。

用假 MCP 客户端跑，不起子进程。重点钉三件事：

* **字段映射**：``duration`` 是 ``"m:ss"`` 字符串不是秒；没有 ``ok`` 字段的工具也要判对成败；
* **不说谎**：没有字幕/弹幕就返回空证据，不编；评论抓一半失败不能报成「没人评论」；
* **发送闸**：``confirm=False`` 一个字都不发，私信超长在本地就拒（不发给 B 站去撞 21007）。
"""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from gateway.mcp_client import CallResult
from gateway.radar.bilibili import (
    DM_MAX_CHARS,
    BilibiliRadar,
    _is_speechless,
    bvid_of,
    error_text,
    parse_duration,
)
from gateway.radar.provider import RadarError, ShareDraft


class FakeBiliMCP:
    """假的 MCP 客户端：按工具名返回预置 JSON，并记下每次调用。"""

    alive = True

    def __init__(self, responses: dict[str, dict], *, images: list | None = None) -> None:
        self.responses = responses
        self.images = images or []
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, arguments: dict | None = None, *, timeout=None) -> CallResult:
        args = dict(arguments or {})
        self.calls.append((tool, args))
        payload = self.responses.get(tool, {})
        return CallResult(tool=tool, ok=True, data=payload, elapsed_ms=1)

    async def call_raw(self, tool: str, arguments: dict | None = None, *, timeout=None):
        result = await self.call(tool, arguments, timeout=timeout)
        return result.data, self.images

    async def list_tools(self, *, timeout: float = 30.0) -> list[dict]:
        return [{"name": name} for name in self.responses]

    async def close(self) -> None:
        return None

    def tools(self) -> list[str]:
        return [name for name, _ in self.calls]


def make_radar(responses: dict[str, dict], *, keywords: list[str] | None = None,
               dm_uid: str = "", seed: int = 0) -> tuple[BilibiliRadar, FakeBiliMCP, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    client = FakeBiliMCP(responses)
    radar = BilibiliRadar(client, Path(tmp.name), keywords=keywords, dm_uid=dm_uid,
                          rng=random.Random(seed))
    return radar, client, tmp


class FieldMappingTest(unittest.TestCase):
    def test_duration_is_mm_ss_string(self) -> None:
        self.assertEqual(parse_duration("12:34"), 754)
        self.assertEqual(parse_duration("1:02:03"), 3723)
        self.assertEqual(parse_duration("0:45"), 45)
        self.assertEqual(parse_duration(754), 754)
        self.assertIsNone(parse_duration(""))
        self.assertIsNone(parse_duration("直播"))

    def test_success_flag_beats_missing_ok(self) -> None:
        """``bili_reply`` 用 success 不用 ok —— 失败必须被认出来。"""
        self.assertEqual(error_text({"success": False, "error": "评论太短"}), "评论太短")
        self.assertEqual(error_text({"success": False}), "工具返回失败")
        self.assertEqual(error_text({"success": True, "message": "评论发送成功"}), "")

    def test_bvid_extraction(self) -> None:
        self.assertEqual(bvid_of("BV1uNk1YxEJQ"), "BV1uNk1YxEJQ")
        self.assertEqual(bvid_of("https://www.bilibili.com/video/BV1uNk1YxEJQ?p=1"),
                         "BV1uNk1YxEJQ")
        self.assertEqual(bvid_of("av123"), "av123")

    def test_speechless_detection(self) -> None:
        self.assertTrue(_is_speechless(""))
        self.assertTrue(_is_speechless("字幕by某某"))
        self.assertFalse(_is_speechless("今天我们来讲一个很长的故事，关于一台旧机器怎么被修好的"))


class ReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_whoami_not_logged_in(self) -> None:
        radar, _, tmp = make_radar({"bili_check_credential": {
            "logged_in": False, "message": "未登录，请调用 bili_login 扫码"}})
        with tmp:
            info = await radar.whoami()
        self.assertTrue(info["ok"])
        self.assertFalse(info["logged_in"])

    async def test_whoami_logged_in(self) -> None:
        radar, _, tmp = make_radar({"bili_check_credential": {
            "logged_in": True, "uid": "12345", "username": "小号", "message": "凭证有效"}})
        with tmp:
            info = await radar.whoami()
        self.assertEqual(info["nick"], "小号")
        self.assertEqual(info["uid"], "12345")

    async def test_keywords_use_search(self) -> None:
        radar, client, tmp = make_radar({"bili_search": {"videos": [
            {"bvid": "BV1aaaaaaaaa", "title": "标题", "author": "UP",
             "play": 1000, "review": 50, "danmaku": 10, "duration": "12:34"},
        ]}}, keywords=["大模型"])
        with tmp:
            items = await radar.candidates(top=3)
        self.assertEqual(client.tools(), ["bili_search"])
        self.assertEqual(client.calls[0][1]["keyword"], "大模型")
        self.assertEqual(items[0].item_id, "BV1aaaaaaaaa")
        self.assertEqual(items[0].duration_sec, 754)
        self.assertEqual(items[0].stats["comment"], 50)
        self.assertTrue(items[0].url.endswith("/BV1aaaaaaaaa"))

    async def test_no_keyword_uses_recommend(self) -> None:
        radar, client, tmp = make_radar({"bili_recommend": {"ok": True, "videos": [
            {"bvid": "BV1bbbbbbbbb", "title": "推荐来的", "author": "UP",
             "play": 2000, "danmaku": 30, "like": 100, "duration": "3:05"},
        ]}})
        with tmp:
            items = await radar.candidates(top=5)
        self.assertEqual(client.tools(), ["bili_recommend"])
        self.assertEqual(items[0].duration_sec, 185)
        self.assertEqual(items[0].stats["like"], 100)

    async def test_candidates_shuffle(self) -> None:
        """章程要求随机挑，不固定取前 N；用固定种子验证顺序真的会变。"""
        rows = [{"bvid": f"BV1{i:09d}", "title": f"t{i}", "duration": "5:00"}
                for i in range(20)]
        radar, _, tmp = make_radar({"bili_recommend": {"ok": True, "videos": rows}},
                                   seed=1234)
        with tmp:
            items = await radar.candidates(top=20)
        self.assertEqual([i.item_id for i in items][:3], [i.item_id for i in items][:3])
        self.assertNotEqual([i.item_id for i in items], [r["bvid"] for r in rows])

    async def test_comments_sorted_and_capped(self) -> None:
        radar, _, tmp = make_radar({"bili_comments": {"count": 3, "comments": [
            {"content": "一般", "like": 5},
            {"content": "很好", "like": 50},
            {"content": "还行", "like": 20},
        ]}})
        with tmp:
            rows = await radar.comments("BV1aaaaaaaaa", limit=2)
        self.assertEqual([r.text for r in rows], ["很好", "还行"])

    async def test_comments_partial_failure_is_not_zero(self) -> None:
        """抓取失败时上游照样返回已抓到的评论 + 一个 error 字段，别当成「没人评论」。"""
        radar, _, tmp = make_radar({"bili_comments": {
            "count": 1, "error": "RequestException: -412 风控",
            "comments": [{"content": "只抓到这条", "like": 1}]}})
        with tmp:
            rows = await radar.comments("BV1aaaaaaaaa")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text, "只抓到这条")


class EvidenceTest(unittest.IsolatedAsyncioTestCase):
    # 真实字幕一定有好几十字；太短的串会被「无语音」判据正确拦掉，所以测试数据得写实
    SUBTITLE = ("今天我们来聊聊这台二手机器怎么修好的，先说结论：不是主板坏，是电源虚焊。"
                "拆开之后你会看到三个电容鼓包，这个很常见。")

    async def test_subtitle_preferred(self) -> None:
        radar, client, tmp = make_radar({
            "bili_subtitle": {"text": self.SUBTITLE, "segments": 12},
            "bili_danmaku": {"danmakus": [{"text": "弹幕", "time": 1}]},
        })
        with tmp:
            info = await radar.evidence(await self._candidate())
        self.assertEqual(info["source"], "subtitle")
        self.assertEqual(info["subtitle"], self.SUBTITLE)
        self.assertNotIn("bili_danmaku", client.tools())

    async def test_danmaku_fallback(self) -> None:
        radar, client, tmp = make_radar({
            "bili_subtitle": {"message": "该视频没有字幕"},
            "bili_danmaku": {"danmakus": [{"text": "笑死", "time": 1},
                                          {"text": "前排放个眼", "time": 2}]},
        })
        with tmp:
            info = await radar.evidence(await self._candidate())
        self.assertEqual(info["source"], "danmaku")
        self.assertEqual(info["danmaku"], ["笑死", "前排放个眼"])

    async def test_speechless_subtitle_treated_as_missing(self) -> None:
        """只有「字幕by某某」= 这条没语音，不是工具坏了。"""
        radar, _, tmp = make_radar({
            "bili_subtitle": {"text": "字幕by某某", "segments": 1},
            "bili_danmaku": {"danmakus": []},
        })
        with tmp:
            info = await radar.evidence(await self._candidate())
        self.assertEqual(info["source"], "none")
        self.assertEqual(info["subtitle"], "")

    async def _candidate(self):
        from gateway.radar import Candidate
        return Candidate(platform="bilibili", item_id="BV1aaaaaaaaa", title="t")


class SendTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_text_needs_confirm(self) -> None:
        radar, client, tmp = make_radar({}, dm_uid="42")
        with tmp:
            out = await radar.send_text("", "在吗")
        self.assertFalse(out["sent"])
        self.assertTrue(out["needs_confirm"])
        self.assertEqual(client.tools(), [])

    async def test_send_text_rejects_too_long_locally(self) -> None:
        radar, client, tmp = make_radar({}, dm_uid="42")
        with tmp, self.assertRaises(RadarError) as ctx:
            await radar.send_text("", "字" * (DM_MAX_CHARS + 1))
        self.assertEqual(ctx.exception.code, "too_long")
        self.assertEqual(client.tools(), [])   # 没发出去，也没碰网络

    async def test_send_text_requires_uid(self) -> None:
        radar, _, tmp = make_radar({})
        with tmp, self.assertRaises(RadarError) as ctx:
            await radar.send_text("", "在吗")
        self.assertEqual(ctx.exception.code, "invalid_args")

    async def test_send_text_confirmed(self) -> None:
        radar, client, tmp = make_radar(
            {"bili_send_message": {"success": True, "message": "私信已投递（回执 abc…）"}},
            dm_uid="42")
        with tmp:
            out = await radar.send_text("", "在吗", confirm=True)
        self.assertTrue(out["sent"])
        self.assertEqual(client.calls[0][0], "bili_send_message")
        self.assertEqual(client.calls[0][1]["receiver_uid"], 42)

    async def test_send_share_without_uid_is_a_draft(self) -> None:
        radar, client, tmp = make_radar({})
        with tmp:
            draft = await radar.send_share(ShareDraft(item_id="BV1aaaaaaaaa",
                                                      reason="值得看"))
        self.assertFalse(draft.sent)
        self.assertIn("没有收件人", draft.detail)
        self.assertEqual(client.tools(), [])

    async def test_comment_reply_style_block_raises(self) -> None:
        """风格闸门是 ok:false + blocked_by:style ⇒ 上层要能认出来改写一次。"""
        radar, _, tmp = make_radar({"bili_reply": {
            "ok": False, "sent": False, "blocked_by": "style",
            "error": "开头『这条我认』是鱼八股"}})
        with tmp, self.assertRaises(RadarError) as ctx:
            await radar.comment_reply(bvid="BV1aaaaaaaaa", text="这条我认", confirm=True)
        self.assertEqual(ctx.exception.code, "style_blocked")

    async def test_comment_reply_success_false_is_failure(self) -> None:
        """``success:false`` 没有 ok 字段 —— McpClient 会判成功，这里必须再判一道。"""
        radar, _, tmp = make_radar({"bili_reply": {
            "success": False, "error": "评论区已关闭"}})
        with tmp, self.assertRaises(RadarError) as ctx:
            await radar.comment_reply(bvid="BV1aaaaaaaaa", text="来看看", confirm=True)
        self.assertIn("评论区已关闭", ctx.exception.message)

    async def test_comment_reply_needs_confirm(self) -> None:
        radar, client, tmp = make_radar({})
        with tmp:
            out = await radar.comment_reply(bvid="BV1aaaaaaaaa", text="来看看")
        self.assertFalse(out["sent"])
        self.assertEqual(client.tools(), [])

    async def test_comment_reply_returns_rpid(self) -> None:
        radar, _, tmp = make_radar({"bili_reply": {
            "success": True, "rpid": 123456, "root": 123456}})
        with tmp:
            out = await radar.comment_reply(bvid="BV1aaaaaaaaa", text="来看看", confirm=True)
        self.assertTrue(out["sent"])
        self.assertEqual(out["rpid"], 123456)


class CapabilityTest(unittest.TestCase):
    def test_declares_capabilities(self) -> None:
        radar = BilibiliRadar(FakeBiliMCP({}), ".")
        self.assertEqual(radar.name, "bilibili")
        self.assertTrue(radar.supports_comment)
        self.assertTrue(radar.supports_share)
        self.assertTrue(radar.supports_text)

    def test_share_url_falls_back_to_bv(self) -> None:
        from gateway.radar import Candidate
        radar = BilibiliRadar(FakeBiliMCP({}), ".")
        c = Candidate(platform="bilibili", item_id="BV1aaaaaaaaa", title="t")
        self.assertEqual(radar.share_url(c), "https://www.bilibili.com/video/BV1aaaaaaaaa")


if __name__ == "__main__":
    unittest.main()
