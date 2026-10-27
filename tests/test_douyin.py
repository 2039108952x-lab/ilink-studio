"""抖音适配器（MCP 后端）：窗口对齐、会话摄取、通知去重、发送映射、未登录。"""

from __future__ import annotations

import asyncio
import unittest

from gateway.adapters import douyin as douyin_mod
from gateway.adapters.douyin import DouyinAdapter, align_window
from gateway.radar.provider import RadarError


class FakeRadar:
    def __init__(self, *, whoami: dict | None = None,
                 conversations: list[dict] | None = None,
                 im: dict[str, dict] | None = None,
                 at: dict | None = None,
                 send: dict | None = None,
                 comment: dict | None = None,
                 comments: list | None = None) -> None:
        self._whoami = whoami if whoami is not None else {
            "ok": True, "logged_in": True, "nick": "demo_account", "uid": "1000000000000001"}
        self._conversations = conversations or []
        self._im = im or {}
        self._at = at or {"ok": True, "list": []}
        self._send = send or {"ok": True, "sent": True, "msg_id": "m1"}
        self._comment = comment or {"ok": True, "sent": True, "cid": "c1"}
        self._comments = comments or []
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def whoami(self):
        return self._whoami

    async def list_conversations(self):
        return list(self._conversations)

    async def im_read(self, *, to: str = "", to_id: str = "", limit: int = 30):
        self.calls.append(("im_read", {"to": to, "to_id": to_id, "limit": limit}))
        key = to or to_id
        payload = self._im.get(key)
        if payload is None:
            raise RadarError("tool_failed", f"会话列表里没有昵称为「{key}」的人。")
        return payload

    async def at_me(self, *, limit: int = 10):
        self.calls.append(("at_me", {"limit": limit}))
        return self._at

    async def send_text(self, to: str, text: str, *, confirm: bool = False):
        self.calls.append(("send_text", {"to": to, "text": text, "confirm": confirm}))
        return self._send

    async def comment_reply(self, *, aweme_id: str, text: str, comment_id: str = "",
                            root_comment_id: str = "", confirm: bool = False):
        self.calls.append(("comment_reply", {
            "aweme_id": aweme_id, "text": text, "comment_id": comment_id,
            "confirm": confirm}))
        return self._comment

    async def comments(self, item_id: str, *, limit: int = 5):
        return self._comments

    async def candidates(self, *, top: int = 10):
        return []

    async def send_share(self, draft, *, confirm: bool = False):
        return draft

    async def close(self):
        self.closed = True


def collect(adapter: DouyinAdapter) -> list:
    seen: list = []

    async def handler(message) -> None:
        seen.append(message)

    adapter.bind(handler)
    return seen


class _FakeCheckpoint:
    """够用的检查点：只实现 state() 和 save(state=)。"""

    def __init__(self, store: dict, stream: str) -> None:
        self._store, self._stream = store, stream

    def state(self) -> dict:
        return dict(self._store.get(self._stream) or {})

    def primed(self) -> bool:
        return bool((self._store.get(self._stream) or {}).get("primed"))

    def save(self, *, state=None, cursor=None, primed=None) -> None:
        slot = dict(self._store.get(self._stream) or {})
        if state is not None:
            slot.update(state)
        if primed:
            slot["primed"] = True
        self._store[self._stream] = slot


def checkpoint_factory(store: dict):
    return lambda stream: _FakeCheckpoint(store, stream)


def collect_with_store(adapter: DouyinAdapter, store: dict) -> list:
    seen: list = []

    async def handler(message) -> None:
        seen.append(message)

    adapter.bind(handler, checkpoints=checkpoint_factory(store))
    return seen


class AlignWindowTest(unittest.TestCase):
    def test_grows_by_suffix(self) -> None:
        self.assertEqual(align_window(["a", "b"], ["a", "b", "c"]), [2])

    def test_trimmed_window_uses_last_line(self) -> None:
        self.assertEqual(align_window(["a", "b"], ["x", "b", "y"]), [2])

    def test_ambiguous_last_line_is_none(self) -> None:
        self.assertIsNone(align_window(["a", "b"], ["x", "b", "y", "b"]))

    def test_first_round_returns_all(self) -> None:
        self.assertEqual(align_window([], ["a", "b"]), [0, 1])

    def test_disjoint_is_none(self) -> None:
        self.assertIsNone(align_window(["a"], ["z"]))


class IngestTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_their_messages_are_emitted(self) -> None:
        adapter = DouyinAdapter({"conversations": [{"to": "demo_friend"}]}, radar=FakeRadar())
        seen = collect(adapter)
        await adapter._ingest_conversation("demo_friend", {"messages": [
            {"from": "them", "text": "在吗"},
            {"from": "me", "text": "在的"},
            {"from": "them", "text": "你好"},
        ]})
        self.assertEqual([m.text for m in seen], ["在吗", "你好"])
        self.assertTrue(all(m.platform.value == "douyin" for m in seen))
        self.assertTrue(all(m.sender_id_kind == "opaque" for m in seen))
        self.assertEqual(len({m.msg_id for m in seen}), 2, "每条要有稳定本地 id")

    async def test_second_poll_only_emits_new_lines(self) -> None:
        adapter = DouyinAdapter({}, radar=FakeRadar())
        seen = collect(adapter)
        await adapter._ingest_conversation("demo_friend", {"messages": [
            {"from": "them", "text": "在吗"}]})
        await adapter._ingest_conversation("demo_friend", {"messages": [
            {"from": "them", "text": "在吗"}, {"from": "them", "text": "回我信息"}]})
        self.assertEqual([m.text for m in seen], ["在吗", "回我信息"])

    async def test_misaligned_window_skips(self) -> None:
        adapter = DouyinAdapter({}, radar=FakeRadar())
        seen = collect(adapter)
        await adapter._ingest_conversation("demo_friend", {"messages": [
            {"from": "them", "text": "a"}]})
        adapter._state["demo_friend"]["tail"] = ["完全不同"]
        await adapter._ingest_conversation("demo_friend", {"messages": [
            {"from": "them", "text": "b"}]})
        self.assertEqual([m.text for m in seen], ["a"])


class PollTest(unittest.IsolatedAsyncioTestCase):
    async def test_seq_survives_restart_so_new_private_message_is_not_deduped(self) -> None:
        """真机事故：seq 只活在内存里，适配器重启后归零 → 生成出用过的 local_id，
        整条私信被去重丢掉。表现就是「私信发了没反应」，评论因为用真实 cid 反而没事。
        """
        store: dict = {}
        window = [{"from": "them", "text": "在吗"}]
        radar = FakeRadar(conversations=[{"nick": "demo_friend", "sec_uid": "sec1", "unread": 1}],
                          im={"demo_friend": {"ok": True, "messages": list(window)}})
        first = DouyinAdapter({"auto_discover": True}, radar=radar)
        seen_first = collect_with_store(first, store)
        await first._poll_once()
        # 首次只建基线：窗口里的历史不补发（否则一接线就刷十几条回复出去）
        self.assertEqual(seen_first, [])
        self.assertTrue(store.get("douyin-conversations", {}).get("conv"))

        # 模拟适配器重启：新实例、同一份检查点，此时只该报出新增那条
        window.append({"from": "them", "text": "回复我"})
        radar2 = FakeRadar(conversations=[{"nick": "demo_friend", "sec_uid": "sec1", "unread": 2}],
                           im={"demo_friend": {"ok": True, "messages": list(window)}})
        second = DouyinAdapter({"auto_discover": True}, radar=radar2)
        seen_second = collect_with_store(second, store)
        await second._poll_once()

        self.assertEqual([m.text for m in seen_second], ["回复我"])
        self.assertTrue(seen_second[0].msg_id.startswith("dy-"))

    async def test_conversations_from_config_and_discovery(self) -> None:
        radar = FakeRadar(
            conversations=[{"nick": "demo_friend", "sec_uid": "sec1", "unread": 0}],
            im={
                "固定联系人": {"messages": [{"from": "them", "text": "hi"}]},
                "demo_friend": {"messages": [{"from": "them", "text": "hello"}]},
            },
        )
        adapter = DouyinAdapter(
            {"conversations": [{"to": "固定联系人", "to_id": ""}]}, radar=radar)
        seen = collect(adapter)
        await adapter._poll_conversations(radar)
        self.assertEqual(sorted(m.text for m in seen), ["hello", "hi"])

    async def test_missing_conversation_is_skipped(self) -> None:
        radar = FakeRadar(conversations=[], im={})
        adapter = DouyinAdapter({"conversations": [{"to": "不存在", "to_id": ""}]}, radar=radar)
        seen = collect(adapter)
        await adapter._poll_conversations(radar)
        self.assertEqual(seen, [])

    async def test_notices_become_comment_messages_and_dedupe(self) -> None:
        row = {"nickname": "demo_friend", "content": "@demo_account", "aweme_id": "760",
               "comment_id": "7605855263023711025",
               "video_url": "https://www.douyin.com/video/760"}
        radar = FakeRadar(at={"ok": True, "list": [row]})
        adapter = DouyinAdapter({}, radar=radar)
        seen = collect(adapter)
        await adapter._poll_notices(radar)
        await adapter._poll_notices(radar)
        self.assertEqual(len(seen), 1)
        message = seen[0]
        self.assertEqual(message.kind.value, "comment")
        # 顶层评论（root_text/reply_to 都空）⇒ 楼根就是它自己，chat_id 带第三段
        self.assertEqual(message.chat_id, "760:7605855263023711025:7605855263023711025")
        self.assertEqual(message.raw["aweme_id"], "760")
        self.assertEqual(message.raw["root_comment_id"], "7605855263023711025")

    async def test_thread_reply_leaves_root_for_mcp_to_judge(self) -> None:
        """楼中楼回复时我们手上没有楼根，宁可留空让 MCP 判，也不瞎填。"""
        row = {"nickname": "demo_friend", "content": "@demo_account", "aweme_id": "760",
               "comment_id": "770", "reply_to_nickname": "demo_user",
               "root_text": "楼下那条", "video_url": "https://www.douyin.com/video/760"}
        radar = FakeRadar(at={"ok": True, "list": [row]})
        adapter = DouyinAdapter({}, radar=radar)
        seen = collect(adapter)
        await adapter._poll_notices(radar)
        self.assertEqual(seen[0].chat_id, "760:770:")
        self.assertEqual(seen[0].raw["root_comment_id"], "")


class LoginTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_logged_in_marks_needs_human(self) -> None:
        radar = FakeRadar(whoami={"ok": True, "logged_in": False})
        adapter = DouyinAdapter({}, radar=radar)
        reasons: list[str] = []
        adapter.on_needs_human = reasons.append
        with self.assertRaises(RadarError) as caught:
            await adapter.start()
        self.assertEqual(caught.exception.code, "not_logged_in")
        self.assertEqual(reasons, ["douyin_login_required"])

    async def test_backend_down_marks_needs_human(self) -> None:
        class Broken(FakeRadar):
            async def whoami(self):
                raise RadarError("process_exited", "MCP 进程提前退出", retryable=True)

        adapter = DouyinAdapter({}, radar=Broken())
        reasons: list[str] = []
        adapter.on_needs_human = reasons.append
        with self.assertRaises(RadarError):
            await adapter.start()
        self.assertEqual(reasons, ["douyin_backend_down"])


class SendTest(unittest.IsolatedAsyncioTestCase):
    async def test_private_goes_through_dm_with_confirm(self) -> None:
        radar = FakeRadar()
        adapter = DouyinAdapter({}, radar=radar)
        from gateway.models import ChatKind, OutboundMessage, Platform
        result = await adapter.send(OutboundMessage(
            platform=Platform.DOUYIN, kind=ChatKind.PRIVATE, chat_id="demo_friend", text="在的"))
        self.assertTrue(result.ok)
        self.assertEqual(radar.calls[0][0], "send_text")
        self.assertTrue(radar.calls[0][1]["confirm"])

    async def test_comment_goes_through_comment_reply(self) -> None:
        radar = FakeRadar()
        adapter = DouyinAdapter({}, radar=radar)
        from gateway.models import ChatKind, OutboundMessage, Platform
        result = await adapter.send(OutboundMessage(
            platform=Platform.DOUYIN, kind=ChatKind.COMMENT,
            chat_id="760:7605855263023711025", text="在的"))
        self.assertTrue(result.ok)
        name, args = radar.calls[0]
        self.assertEqual(name, "comment_reply")
        self.assertEqual(args["aweme_id"], "760")
        self.assertEqual(args["comment_id"], "7605855263023711025")
        self.assertTrue(args["confirm"])

    async def test_unsent_returns_failure(self) -> None:
        radar = FakeRadar(send={"ok": True, "sent": False, "note": "被本地闸拦下了"})
        adapter = DouyinAdapter({}, radar=radar)
        from gateway.models import ChatKind, OutboundMessage, Platform
        result = await adapter.send(OutboundMessage(
            platform=Platform.DOUYIN, kind=ChatKind.PRIVATE, chat_id="x", text="y"))
        self.assertFalse(result.ok)
        self.assertIn("拦", result.error)

    async def test_radar_error_becomes_send_failure(self) -> None:
        class Failing(FakeRadar):
            async def send_text(self, to, text, *, confirm=False):
                raise RadarError("rate_limited", "发送太密")

        adapter = DouyinAdapter({}, radar=Failing())
        from gateway.models import ChatKind, OutboundMessage, Platform
        result = await adapter.send(OutboundMessage(
            platform=Platform.DOUYIN, kind=ChatKind.PRIVATE, chat_id="x", text="y"))
        self.assertFalse(result.ok)
        self.assertIn("太密", result.error)


class CapabilityTest(unittest.TestCase):
    def test_comment_send_is_supported_now(self) -> None:
        caps = DouyinAdapter({}, radar=FakeRadar()).capabilities()
        self.assertEqual(caps["text_send"], "supported")
        self.assertEqual(caps["comment_send"], "supported")
        self.assertEqual(caps["image_send"], "unsupported")


class VideoBriefTest(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_plain_id_and_link(self) -> None:
        from gateway.radar.models import CommentSnapshot
        radar = FakeRadar(comments=[CommentSnapshot("笑死", 9)])
        adapter = DouyinAdapter({}, radar=radar)
        for ref in ("7605469750864635883",
                    "https://www.douyin.com/video/7605469750864635883"):
            result = await adapter.video_brief(ref)
            self.assertTrue(result["ok"], ref)
            self.assertEqual(result["aweme_id"], "7605469750864635883")
            self.assertEqual(result["comments"][0]["text"], "笑死")
            self.assertIn("看不到画面", result["note"])

    async def test_rejects_unrecognised_ref(self) -> None:
        adapter = DouyinAdapter({}, radar=FakeRadar())
        result = await adapter.video_brief("不是链接")
        self.assertFalse(result["ok"])
        self.assertIn("没认出视频", result["error"])


class AlignWindowImportTest(unittest.TestCase):
    def test_module_no_longer_ships_rpa(self) -> None:
        """回归：Playwright RPA 已经删干净，不再有 DOM 选择器那套。"""
        source = douyin_mod.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for token in ("PlaywrightDriver", "_ensure_dialog", "messageMessageBox",
                      "data-e2e", "conversation_item"):
            self.assertNotIn(token, text, f"还想留着 RPA？{token}")


if __name__ == "__main__":
    unittest.main()
