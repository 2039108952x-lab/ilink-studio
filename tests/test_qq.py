"""QQ 适配器的纯逻辑测试：不连真协议端。"""

from __future__ import annotations

import asyncio
import unittest

from gateway.adapters import qq
from gateway.models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult


class TestSegments(unittest.TestCase):
    def test_list_passthrough(self) -> None:
        segs = [{"type": "text", "data": {"text": "hi"}}]
        self.assertEqual(qq.parse_segments(segs), segs)

    def test_string_becomes_one_text_segment(self) -> None:
        self.assertEqual(
            qq.parse_segments("你好"),
            [{"type": "text", "data": {"text": "你好"}}],
        )

    def test_garbage_is_empty(self) -> None:
        self.assertEqual(qq.parse_segments(None), [])
        self.assertEqual(qq.parse_segments(42), [])


class TestTextOf(unittest.TestCase):
    def test_text_only(self) -> None:
        self.assertEqual(
            qq.text_of([{"type": "text", "data": {"text": "在吗"}}]), "在吗"
        )

    def test_at_replaced_by_placeholder(self) -> None:
        segs = [
            {"type": "at", "data": {"qq": "123"}},
            {"type": "text", "data": {"text": " 看看这个"}},
        ]
        self.assertEqual(qq.text_of(segs), "@123 看看这个")

    def test_at_all(self) -> None:
        self.assertEqual(
            qq.text_of([{"type": "at", "data": {"qq": "all"}}]), "@全体成员"
        )

    def test_media_placeholders(self) -> None:
        segs = [
            {"type": "image", "data": {}},
            {"type": "face", "data": {}},
            {"type": "record", "data": {}},
        ]
        self.assertEqual(qq.text_of(segs), "[图片][表情][语音]")

    def test_unknown_segment_ignored(self) -> None:
        self.assertEqual(
            qq.text_of([{"type": "weird", "data": {}}, {"type": "text", "data": {"text": "x"}}]),
            "x",
        )

    def test_strips(self) -> None:
        self.assertEqual(qq.text_of([{"type": "text", "data": {"text": "  a  "}}]), "a")


class TestMentions(unittest.TestCase):
    def test_mentions_self(self) -> None:
        segs = [{"type": "at", "data": {"qq": "999"}}]
        self.assertTrue(qq.mentions(segs, "999"))

    def test_mentions_someone_else(self) -> None:
        segs = [{"type": "at", "data": {"qq": "111"}}]
        self.assertFalse(qq.mentions(segs, "999"))

    def test_mentions_all(self) -> None:
        self.assertTrue(qq.mentions([{"type": "at", "data": {"qq": "all"}}], "999"))

    def test_no_at(self) -> None:
        self.assertFalse(qq.mentions([{"type": "text", "data": {"text": "hi"}}], "999"))


class TestToInbound(unittest.TestCase):
    def _group_event(self, **kw) -> dict:
        base = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 555,
            "user_id": 777,
            "message_id": 1001,
            "message": [{"type": "text", "data": {"text": "在吗"}}],
            "sender": {"nickname": "神", "card": "群名片"},
        }
        base.update(kw)
        return base

    def test_group_message(self) -> None:
        msg = qq.to_inbound(self._group_event(), "999")
        assert msg is not None
        self.assertEqual(msg.platform, Platform.QQ)
        self.assertEqual(msg.kind, ChatKind.GROUP)
        self.assertEqual(msg.chat_id, "555")
        self.assertEqual(msg.sender_id, "777")
        self.assertEqual(msg.sender_name, "群名片")
        self.assertEqual(msg.text, "在吗")
        self.assertEqual(msg.msg_id, "1001")

    def test_private_message(self) -> None:
        ev = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 777,
            "message_id": 1,
            "message": "私聊内容",
            "sender": {"nickname": "神"},
        }
        msg = qq.to_inbound(ev, "999")
        assert msg is not None
        self.assertEqual(msg.kind, ChatKind.PRIVATE)
        self.assertEqual(msg.chat_id, "777")
        self.assertEqual(msg.sender_name, "神")

    def test_non_message_event_returns_none(self) -> None:
        self.assertIsNone(qq.to_inbound({"post_type": "notice"}, "999"))

    def test_empty_text_returns_none(self) -> None:
        ev = self._group_event(message=[{"type": "image", "data": {}}])
        # image 有占位符，所以不算空；换成真正空段
        ev["message"] = [{"type": "unknown", "data": {}}]
        self.assertIsNone(qq.to_inbound(ev, "999"))

    def test_mention_flagged(self) -> None:
        ev = self._group_event(
            message=[
                {"type": "at", "data": {"qq": "999"}},
                {"type": "text", "data": {"text": " 你好"}},
            ]
        )
        msg = qq.to_inbound(ev, "999")
        assert msg is not None
        self.assertTrue(msg.is_mention_me)


class TestAdapterSend(unittest.TestCase):
    def test_send_without_connection_fails_softly(self) -> None:
        adapter = qq.QQAdapter({})
        res = asyncio.run(
            adapter.send(
                OutboundMessage(
                    platform=Platform.QQ, kind=ChatKind.PRIVATE, chat_id="1", text="hi"
                )
            )
        )
        self.assertFalse(res.ok)
        self.assertIn("还没连上", res.error)

    def test_group_mention_gate(self) -> None:
        adapter = qq.QQAdapter({"require_mention": True})
        got: list[InboundMessage] = []

        async def collect(m: InboundMessage) -> None:
            got.append(m)

        adapter.bind(collect)
        ev = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "message_id": 3,
            "message": [{"type": "text", "data": {"text": "没at我"}}],
            "sender": {},
        }
        adapter._self_id = "999"
        asyncio.run(adapter._dispatch_event(ev))
        self.assertEqual(got, [])

        ev["message"] = [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " at了"}},
        ]
        asyncio.run(adapter._dispatch_event(ev))
        self.assertEqual(len(got), 1)

    def test_self_message_ignored(self) -> None:
        adapter = qq.QQAdapter({"require_mention": False})
        got: list[InboundMessage] = []
        adapter.bind(lambda m: asyncio.sleep(0, result=got.append(m)))
        adapter._self_id = "999"
        ev = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 999,
            "message_id": 1,
            "message": "自己说的",
            "sender": {},
        }
        asyncio.run(adapter._dispatch_event(ev))
        self.assertEqual(got, [])

    def test_meta_event_updates_self_id(self) -> None:
        adapter = qq.QQAdapter({})
        asyncio.run(
            adapter._dispatch_event(
                {"post_type": "meta_event", "meta_event_type": "lifecycle", "self_id": 12345}
            )
        )
        self.assertEqual(adapter._self_id, "12345")


class TestBuild(unittest.TestCase):
    def test_build_reads_options(self) -> None:
        adapter = qq.build(
            {"onebot_ws": "ws://127.0.0.1:4000", "access_token": "t", "require_mention": False}
        )
        self.assertIsInstance(adapter, qq.QQAdapter)
        self.assertEqual(adapter._url, "ws://127.0.0.1:4000")
        self.assertEqual(adapter._token, "t")
        self.assertFalse(adapter._require_mention)


class TestImages(unittest.TestCase):
    def test_images_of_picks_url(self) -> None:
        segs = [{"type": "image", "data": {"url": "http://127.0.0.1:3000/a.png", "file": "a.png"}}]
        [att] = qq.images_of(segs)
        self.assertEqual(att.kind, "image")
        self.assertEqual(att.url, "http://127.0.0.1:3000/a.png")
        self.assertEqual(att.name, "a.png")
        self.assertEqual(att.path, "")

    def test_images_of_uses_existing_local_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "真图.png"
            local.write_bytes(b"x")
            [att] = qq.images_of([{"type": "image", "data": {"file": str(local)}}])
            self.assertEqual(att.path, str(local))

    def test_image_only_message_survives(self) -> None:
        ev = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 777,
            "message_id": 2,
            "message": [{"type": "image", "data": {"url": "http://x/y.png"}}],
            "sender": {"nickname": "神"},
        }
        msg = qq.to_inbound(ev, "999")
        assert msg is not None
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.text, "")  # 占位符被清掉，图交给 attachments

    def test_segments_include_base64_image(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "笑.png"
            img.write_bytes(b"\x89PNG")
            out = OutboundMessage(
                platform=Platform.QQ,
                kind=ChatKind.PRIVATE,
                chat_id="1",
                text="好",
                reply_to="77",
                attachments=[qq.Attachment(kind="image", path=str(img))],
            )
            segs = qq.QQAdapter._segments_of(out)
            self.assertEqual(segs[0], {"type": "reply", "data": {"id": "77"}})
            self.assertEqual(segs[1], {"type": "text", "data": {"text": "好"}})
            self.assertEqual(segs[2]["type"], "image")
            self.assertTrue(segs[2]["data"]["file"].startswith("base64://"))

    def test_segments_text_only(self) -> None:
        out = OutboundMessage(
            platform=Platform.QQ, kind=ChatKind.PRIVATE, chat_id="1", text="就一句话"
        )
        self.assertEqual(
            qq.QQAdapter._segments_of(out), [{"type": "text", "data": {"text": "就一句话"}}]
        )


class TestResolveAttachments(unittest.TestCase):
    def test_uses_local_cache_from_protocol_end(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            cached = Path(d) / "cached.jpg"
            cached.write_bytes(b"img")
            adapter = qq.QQAdapter({})
            seen: list[tuple[str, dict]] = []

            async def fake_call(action: str, params: dict) -> dict:
                seen.append((action, params))
                return {"data": {"file": str(cached)}}

            adapter._call = fake_call  # type: ignore[method-assign]
            msg = InboundMessage(
                platform=Platform.QQ,
                kind=ChatKind.PRIVATE,
                chat_id="1",
                sender_id="2",
                sender_name="神",
                text="",
                attachments=[qq.Attachment(kind="image", url="http://cdn/x.jpg", name="x.jpg")],
            )
            asyncio.run(adapter._resolve_attachments(msg))
            self.assertEqual(seen[0][0], "get_image")
            self.assertEqual(msg.attachments[0].path, str(cached))

    def test_unreachable_protocol_leaves_url(self) -> None:
        adapter = qq.QQAdapter({})

        async def boom(action: str, params: dict) -> dict:
            raise RuntimeError("没连上")

        adapter._call = boom  # type: ignore[method-assign]
        msg = InboundMessage(
            platform=Platform.QQ,
            kind=ChatKind.PRIVATE,
            chat_id="1",
            sender_id="2",
            sender_name="神",
            text="",
            attachments=[qq.Attachment(kind="image", url="http://cdn/x.jpg", name="x.jpg")],
        )
        asyncio.run(adapter._resolve_attachments(msg))
        self.assertEqual(msg.attachments[0].path, "")


if __name__ == "__main__":
    unittest.main()
