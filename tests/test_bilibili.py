"""B站适配器测试：全部 mock，不联网、不需要真 cookie。

真机验证靠 scripts/bili_smoke.py，这里只保证解析和分派逻辑对。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway.adapters import bilibili
from gateway.models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult

_ORIG_COOKIE_FILE = bilibili.COOKIE_FILE


def setUpModule() -> None:
    """别让本机真实的 data/bili-cookies.json 影响测试结果。"""
    scratch = Path(tempfile.gettempdir())
    bilibili.COOKIE_FILE = scratch / "ilink-no-such-bili-cookies.json"


def tearDownModule() -> None:
    bilibili.COOKIE_FILE = _ORIG_COOKIE_FILE


def notice_item(
    *,
    mid: int = 10086,
    nickname: str = "路人乙",
    subject_id: int = 170001,
    source_id: int = 555,
    root_id: int = 0,
    target_id: int = 0,
    content: str = "这期讲得不错",
    title: str = "某个视频",
    item_id: int = 987,
) -> dict:
    """按 2026-10 实测的真实字段结构造一条 @/回复 通知。"""
    return {
        "id": item_id,
        "user": {"mid": mid, "nickname": nickname},
        "item": {
            "type": "reply",
            "business": "评论",
            "subject_id": subject_id,
            "source_id": source_id,
            "root_id": root_id,
            "target_id": target_id,
            "source_content": content,
            "title": title,
            "uri": "https://www.bilibili.com/video/BV1xx411c7mD",
        },
    }


class FakeEvent:
    """够 inbound_from_event 用的最小事件对象。"""

    def __init__(self, sender_uid: int = 10086, content: object = "在吗", ts: int = 1700000000) -> None:
        self.sender_uid = sender_uid
        self.content = content
        self.timestamp = ts
        self.msg_key = 987654321


class TestCredentialEnv(unittest.TestCase):
    def test_missing_lists_all_when_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                sorted(bilibili.missing_credential_env()), sorted(bilibili.CRED_ENV)
            )

    def test_missing_empty_when_all_set(self) -> None:
        env = {k: "x" for k in bilibili.CRED_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(bilibili.missing_credential_env(), [])

    def test_reports_only_the_missing_one(self) -> None:
        env = {k: "x" for k in bilibili.CRED_ENV}
        env.pop("BILI_JCT")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(bilibili.missing_credential_env(), ["BILI_JCT"])


class TestCookieFileFallback(unittest.TestCase):
    """登录助手写的 json 要能兜底，这样不用手动 setx 也能跑。"""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump({k: f"from-file-{k}" for k in bilibili.CRED_ENV}, self._tmp)
        self._tmp.close()
        self._orig = bilibili.COOKIE_FILE
        bilibili.COOKIE_FILE = Path(self._tmp.name)

    def tearDown(self) -> None:
        bilibili.COOKIE_FILE = self._orig
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_file_fills_in_when_env_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bilibili.missing_credential_env(), [])
            self.assertEqual(
                bilibili.credential_value("BILI_SESSDATA"), "from-file-BILI_SESSDATA"
            )

    def test_env_wins_over_file(self) -> None:
        with mock.patch.dict(os.environ, {"BILI_SESSDATA": "from-env"}, clear=True):
            self.assertEqual(bilibili.credential_value("BILI_SESSDATA"), "from-env")

    def test_corrupt_file_is_ignored(self) -> None:
        Path(self._tmp.name).write_text("{ 这不是 json", encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bilibili._load_cookie_file(), {})
            self.assertEqual(
                sorted(bilibili.missing_credential_env()), sorted(bilibili.CRED_ENV)
            )

    def test_missing_file_is_ignored(self) -> None:
        bilibili.COOKIE_FILE = Path(tempfile.gettempdir()) / "definitely-not-here.json"
        self.assertEqual(bilibili._load_cookie_file(), {})


class TestParseVideoRef(unittest.TestCase):
    def test_bv_number(self) -> None:
        self.assertEqual(bilibili.parse_video_ref("BV1xx411c7mD"), {"bvid": "BV1xx411c7mD"})

    def test_bv_in_url(self) -> None:
        got = bilibili.parse_video_ref("看这个 https://www.bilibili.com/video/BV1xx411c7mD?p=2 好玩")
        self.assertEqual(got, {"bvid": "BV1xx411c7mD"})

    def test_av_number(self) -> None:
        self.assertEqual(bilibili.parse_video_ref("av12345"), {"aid": 12345})

    def test_av_in_url(self) -> None:
        self.assertEqual(
            bilibili.parse_video_ref("https://www.bilibili.com/video/av170001"), {"aid": 170001}
        )

    def test_bv_wins_over_av(self) -> None:
        got = bilibili.parse_video_ref("BV1xx411c7mD 和 av999")
        self.assertEqual(got, {"bvid": "BV1xx411c7mD"})

    def test_unrecognized(self) -> None:
        self.assertEqual(bilibili.parse_video_ref("随便聊聊"), {})
        self.assertEqual(bilibili.parse_video_ref(""), {})


class TestSubtitleHelpers(unittest.TestCase):
    def test_first_page_cid(self) -> None:
        self.assertEqual(bilibili._first_page_cid({"pages": [{"cid": 42}]}), 42)
        self.assertEqual(bilibili._first_page_cid({"pages": []}), 0)
        self.assertEqual(bilibili._first_page_cid({}), 0)
        self.assertEqual(bilibili._first_page_cid({"pages": [{"cid": "bad"}]}), 0)

    def test_prefers_human_chinese(self) -> None:
        payload = {
            "subtitles": [
                {"lan": "en", "subtitle_url": "//x/en.json"},
                {"lan": "ai-zh", "subtitle_url": "//x/aizh.json"},
                {"lan": "zh-CN", "subtitle_url": "//x/zh.json"},
            ]
        }
        self.assertEqual(bilibili._pick_chinese_subtitle(payload)["lan"], "zh-CN")

    def test_falls_back_to_ai_chinese(self) -> None:
        payload = {"subtitles": [{"lan": "en", "subtitle_url": "//x/en.json"},
                                 {"lan": "ai-zh", "subtitle_url": "//x/aizh.json"}]}
        self.assertEqual(bilibili._pick_chinese_subtitle(payload)["lan"], "ai-zh")

    def test_falls_back_to_any(self) -> None:
        payload = {"subtitles": [{"lan": "en", "subtitle_url": "//x/en.json"}]}
        self.assertEqual(bilibili._pick_chinese_subtitle(payload)["lan"], "en")

    def test_no_subtitle(self) -> None:
        self.assertIsNone(bilibili._pick_chinese_subtitle({"subtitles": []}))
        self.assertIsNone(bilibili._pick_chinese_subtitle({}))
        self.assertIsNone(bilibili._pick_chinese_subtitle(None))  # type: ignore[arg-type]

    def test_subtitle_text_joins_and_truncates(self) -> None:
        body = {"body": [{"content": "第一句"}, {"content": "  "}, {"content": "第二句"}]}
        self.assertEqual(bilibili._subtitle_text(body), "第一句 第二句")

    def test_subtitle_text_empty(self) -> None:
        self.assertEqual(bilibili._subtitle_text({}), "")

    def test_subtitle_text_caps_length(self) -> None:
        body = {"body": [{"content": "字" * 100} for _ in range(200)]}
        self.assertLessEqual(len(bilibili._subtitle_text(body)), bilibili.SUBTITLE_MAX_CHARS)


class TestVideoBrief(unittest.TestCase):
    def test_unrecognized_ref_returns_error(self) -> None:
        got = asyncio.run(bilibili.video_brief("今天天气不错"))
        self.assertFalse(got["ok"])
        self.assertIn("没认出视频", got["error"])

    def test_library_failure_is_reported_not_raised(self) -> None:
        fake_video = mock.MagicMock()
        fake_video.get_info = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with mock.patch("bilibili_api.video.Video", return_value=fake_video):
            got = asyncio.run(bilibili.video_brief("BV1xx411c7mD"))
        self.assertFalse(got["ok"])
        self.assertIn("boom", got["error"])

    def test_full_path_with_subtitle(self) -> None:
        fake_video = mock.MagicMock()
        fake_video.get_info = mock.AsyncMock(
            return_value={
                "bvid": "BV1xx411c7mD",
                "aid": 170001,
                "title": "标题",
                "owner": {"name": "UP主"},
                "desc": "简介",
                "duration": 300,
                "pubdate": 1700000000,
                "stat": {"view": 1000, "like": 50},
                "pages": [{"cid": 42}],
            }
        )
        fake_video.get_subtitle = mock.AsyncMock(
            return_value={"subtitles": [{"lan": "zh-CN", "subtitle_url": "//s/zh.json"}]}
        )

        async def fake_body(url: str, transport: object) -> str:
            # 补协议那步在 _fetch_subtitle_body 内部，这一层拿到的还是 // 开头
            assert url == "//s/zh.json", url
            return "字幕正文"

        with mock.patch("bilibili_api.video.Video", return_value=fake_video), mock.patch.object(
            bilibili, "_fetch_subtitle_body", side_effect=fake_body
        ):
            got = asyncio.run(bilibili.video_brief("BV1xx411c7mD"))

        self.assertTrue(got["ok"])
        self.assertEqual(got["title"], "标题")
        self.assertEqual(got["up"], "UP主")
        self.assertEqual(got["subtitle"], "字幕正文")
        fake_video.get_subtitle.assert_awaited_once_with(cid=42)

    def test_no_cid_skips_subtitle(self) -> None:
        fake_video = mock.MagicMock()
        fake_video.get_info = mock.AsyncMock(
            return_value={"bvid": "BV1xx411c7mD", "title": "t", "pages": []}
        )
        with mock.patch("bilibili_api.video.Video", return_value=fake_video):
            got = asyncio.run(bilibili.video_brief("BV1xx411c7mD"))
        self.assertTrue(got["ok"])
        self.assertIn("cid", got["subtitle_note"])
        fake_video.get_subtitle.assert_not_called()


class TestInboundFromEvent(unittest.TestCase):
    def test_text_message(self) -> None:
        msg = bilibili.inbound_from_event(FakeEvent(content="你好"))
        assert msg is not None
        self.assertEqual(msg.platform, Platform.BILIBILI)
        self.assertEqual(msg.kind, ChatKind.PRIVATE)
        self.assertEqual(msg.chat_id, "10086")
        self.assertEqual(msg.sender_id, "10086")
        self.assertEqual(msg.text, "你好")
        self.assertEqual(msg.msg_id, "987654321")
        self.assertTrue(msg.is_mention_me)

    def test_zero_sender_dropped(self) -> None:
        self.assertIsNone(bilibili.inbound_from_event(FakeEvent(sender_uid=0)))

    def test_empty_text_dropped(self) -> None:
        self.assertIsNone(bilibili.inbound_from_event(FakeEvent(content="   ")))

    def test_shared_video_becomes_link(self) -> None:
        video = mock.MagicMock()
        video.bvid = "BV1xx411c7mD"
        video.aid = 170001
        msg = bilibili.inbound_from_event(FakeEvent(content=video))
        assert msg is not None
        self.assertIn("BV1xx411c7mD", msg.text)
        self.assertIn("https://www.bilibili.com/video/", msg.text)

    def test_picture_becomes_url(self) -> None:
        pic = mock.MagicMock()
        pic.url = "https://i0.hdslb.com/x.jpg"
        pic.bvid = None
        pic.aid = None
        msg = bilibili.inbound_from_event(FakeEvent(content=pic))
        assert msg is not None
        self.assertIn("i0.hdslb.com", msg.text)

    def test_picture_becomes_attachment(self) -> None:
        """图片要同时进 attachments，否则 brain 的看图能力用不上。"""
        pic = mock.MagicMock()
        pic.url = "https://i0.hdslb.com/x.jpg"
        pic.bvid = None
        pic.aid = None
        msg = bilibili.inbound_from_event(FakeEvent(content=pic))
        assert msg is not None
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.attachments[0].kind, "image")
        self.assertEqual(msg.attachments[0].url, "https://i0.hdslb.com/x.jpg")

    def test_text_message_has_no_attachment(self) -> None:
        msg = bilibili.inbound_from_event(FakeEvent(content="纯文字"))
        assert msg is not None
        self.assertEqual(msg.attachments, [])

    def test_shared_video_has_no_attachment(self) -> None:
        """分享视频只出链接：视频不是图片，stage_images 会跳过，没必要塞附件。"""
        video = mock.MagicMock()
        video.bvid = "BV1xx411c7mD"
        video.aid = 170001
        msg = bilibili.inbound_from_event(FakeEvent(content=video))
        assert msg is not None
        self.assertEqual(msg.attachments, [])

    def test_bad_timestamp_does_not_crash(self) -> None:
        msg = bilibili.inbound_from_event(FakeEvent(ts="not-a-number"))
        assert msg is not None
        self.assertGreater(msg.ts, 0)


class TestInboundFromNotice(unittest.TestCase):
    def test_root_comment_replies_to_itself(self) -> None:
        """根评论（root_id=0）：回复它时 root 就是它自己，parent 留空。"""
        msg = bilibili.inbound_from_notice(notice_item(root_id=0), source="at")
        assert msg is not None
        self.assertEqual(msg.kind, ChatKind.COMMENT)
        self.assertEqual(msg.chat_id, "170001:555:0")
        self.assertEqual(msg.sender_id, "10086")
        self.assertEqual(msg.sender_name, "路人乙")
        self.assertIn("这期讲得不错", msg.text)
        self.assertEqual(msg.msg_id, "at:170001:987")   # 来源:vid:通知/评论 id

    def test_thread_reply_keeps_root_and_parent(self) -> None:
        """楼中楼：root 是根评论，parent 是发通知那条自己。"""
        msg = bilibili.inbound_from_notice(
            notice_item(root_id=111, source_id=222, content="回你一句"), source="reply"
        )
        assert msg is not None
        self.assertEqual(msg.chat_id, "170001:111:222")
        self.assertEqual(msg.msg_id, "reply:170001:987")

    def test_uri_kept_in_raw(self) -> None:
        msg = bilibili.inbound_from_notice(notice_item(), source="at")
        assert msg is not None
        self.assertIn("BV1xx411c7mD", msg.raw["uri"])

    def test_title_appended_when_different(self) -> None:
        msg = bilibili.inbound_from_notice(
            notice_item(content="正文", title="视频名"), source="at"
        )
        assert msg is not None
        self.assertIn("正文", msg.text)
        self.assertIn("视频名", msg.text)

    def test_missing_subject_dropped(self) -> None:
        item = notice_item(subject_id=0)
        self.assertIsNone(bilibili.inbound_from_notice(item, source="at"))

    def test_missing_source_id_dropped(self) -> None:
        """没有 source_id 就定位不到是哪条评论，回不出去，只能丢。"""
        item = notice_item(source_id=0)
        self.assertIsNone(bilibili.inbound_from_notice(item, source="at"))

    def test_missing_mid_dropped(self) -> None:
        item = notice_item()
        item["user"]["mid"] = 0
        self.assertIsNone(bilibili.inbound_from_notice(item, source="at"))

    def test_empty_content_dropped(self) -> None:
        item = notice_item(content="", title="")
        self.assertIsNone(bilibili.inbound_from_notice(item, source="at"))

    def test_malformed_input(self) -> None:
        self.assertIsNone(bilibili.inbound_from_notice({}, source="at"))  # type: ignore[arg-type]
        self.assertIsNone(bilibili.inbound_from_notice("nope", source="at"))  # type: ignore[arg-type]
        self.assertIsNone(bilibili.inbound_from_notice({"item": None}, source="at"))


class TestParseCommentChatId(unittest.TestCase):
    def test_three_parts(self) -> None:
        self.assertEqual(bilibili.parse_comment_chat_id("170001:111:222"), (170001, 111, 222))

    def test_oid_only(self) -> None:
        self.assertEqual(bilibili.parse_comment_chat_id("170001"), (170001, 0, 0))

    def test_two_parts(self) -> None:
        self.assertEqual(bilibili.parse_comment_chat_id("170001:111"), (170001, 111, 0))

    def test_garbage(self) -> None:
        self.assertIsNone(bilibili.parse_comment_chat_id("abc"))
        self.assertIsNone(bilibili.parse_comment_chat_id("0"))
        self.assertIsNone(bilibili.parse_comment_chat_id(""))


class TestBiliPoster(unittest.TestCase):
    def test_cannot_send_without_credential(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            poster = bilibili.BiliPoster(None)
            self.assertFalse(poster.can_send)
            res = asyncio.run(poster.send_private("10086", "hi"))
        self.assertFalse(res.ok)
        self.assertIn("凭据", res.error)

    def test_send_private_ok(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        with mock.patch.object(
            bilibili, "_lib_send_private", mock.AsyncMock(return_value={"code": 0})
        ) as fake:
            res = asyncio.run(poster.send_private("10086", "你好"))
        self.assertTrue(res.ok)
        fake.assert_awaited_once()
        self.assertEqual(fake.await_args.args[1], 10086)

    def test_send_private_api_error(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        with mock.patch.object(
            bilibili,
            "_lib_send_private",
            mock.AsyncMock(return_value={"code": -404, "message": "被拉黑"}),
        ):
            res = asyncio.run(poster.send_private("10086", "hi"))
        self.assertFalse(res.ok)
        self.assertIn("-404", res.error)

    def test_send_private_bad_uid(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        res = asyncio.run(poster.send_private("不是uid", "hi"))
        self.assertFalse(res.ok)

    def test_send_private_exception_is_caught(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        with mock.patch.object(
            bilibili, "_lib_send_private", mock.AsyncMock(side_effect=RuntimeError("网络炸了"))
        ):
            res = asyncio.run(poster.send_private("10086", "hi"))
        self.assertFalse(res.ok)
        self.assertIn("网络炸了", res.error)

    def test_send_comment_ok(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        with mock.patch.object(
            bilibili,
            "_lib_send_comment",
            mock.AsyncMock(return_value={"code": 0, "data": {"rpid_str": "999"}}),
        ) as fake:
            res = asyncio.run(poster.send_comment("170001:111:222", "回复你"))
        self.assertTrue(res.ok)
        self.assertEqual(res.msg_id, "999")
        self.assertEqual(fake.await_args.args[1:], (170001, "回复你", 111, 222))

    def test_send_comment_bad_chat_id(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        res = asyncio.run(poster.send_comment("garbage", "hi"))
        self.assertFalse(res.ok)
        self.assertIn("解析失败", res.error)

    def test_send_comment_api_error(self) -> None:
        poster = bilibili.BiliPoster(mock.MagicMock())
        with mock.patch.object(
            bilibili,
            "_lib_send_comment",
            mock.AsyncMock(return_value={"code": 12002, "message": "评论已关闭"}),
        ):
            res = asyncio.run(poster.send_comment("170001:0:0", "hi"))
        self.assertFalse(res.ok)
        self.assertIn("12002", res.error)


class TestSessionListener(unittest.TestCase):
    def test_dispatch_converts_event(self) -> None:
        got: list[InboundMessage] = []
        listener = bilibili.SessionListener(mock.MagicMock(), got.append)
        listener._handle(FakeEvent(content="hi"))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].text, "hi")

    def test_dispatch_drops_unusable(self) -> None:
        got: list[InboundMessage] = []
        listener = bilibili.SessionListener(mock.MagicMock(), got.append)
        listener._handle(FakeEvent(sender_uid=0))
        self.assertEqual(got, [])

    def test_connect_registers_enum_not_string(self) -> None:
        """真机踩过：Session.on() 要 EventType 枚举，传 str 会炸。"""
        from bilibili_api import session as bsession

        recorded: list = []
        fake_session = mock.MagicMock()

        def fake_on(event_type):
            recorded.append(event_type)

            def deco(fn):
                return fn

            return deco

        fake_session.on.side_effect = fake_on
        fake_session.start = mock.AsyncMock()

        listener = bilibili.SessionListener(mock.MagicMock(), lambda m: None)
        with mock.patch.object(bsession, "Session", return_value=fake_session):
            asyncio.run(listener._connect_once())

        self.assertTrue(recorded, "一个事件都没注册上")
        for item in recorded:
            self.assertIsInstance(item, bsession.EventType, f"注册的是 {item!r}，不是枚举")

    def test_stop_without_session_is_safe(self) -> None:
        listener = bilibili.SessionListener(mock.MagicMock(), lambda m: None)
        listener._running = True
        listener.stop()
        self.assertFalse(listener._running)

    def test_stop_closes_session(self) -> None:
        listener = bilibili.SessionListener(mock.MagicMock(), lambda m: None)
        fake_session = mock.MagicMock()
        listener._session = fake_session
        listener.stop()
        fake_session.close.assert_called_once()

    def test_run_can_be_cancelled(self) -> None:
        listener = bilibili.SessionListener(mock.MagicMock(), lambda m: None)

        async def fake_connect() -> None:
            await asyncio.sleep(10)

        listener._connect_once = fake_connect  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(listener.run())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())


class TestNotifyPoller(unittest.TestCase):
    def _poller(self, got: list[InboundMessage], **kw) -> bilibili.NotifyPoller:
        return bilibili.NotifyPoller(mock.MagicMock(), got.append, **kw)

    def test_no_shadow_state_file(self) -> None:
        """去重真相在 mailbox.db：适配器不再往 JSON 写一份影子状态。"""
        poller = self._poller([])
        poller._emit([notice_item()], source="at")
        self.assertEqual(len(poller._seen), 1)
        self.assertFalse(hasattr(bilibili, "SEEN_FILE"))

    def test_seen_set_is_bounded(self) -> None:
        got: list[InboundMessage] = []
        poller = self._poller(got)
        poller._seen = {f"id-{i}" for i in range(bilibili._SEEN_KEEP)}
        poller._emit([notice_item()], source="at")   # 触发一次裁剪
        self.assertLessEqual(len(poller._seen), bilibili._SEEN_KEEP)

    def test_missing_notice_id_uses_stable_fallback(self) -> None:
        """通知自身 id 缺省时用 vid+rpid 兜底：既不为空，也不会把两条合成一条。"""
        got: list[InboundMessage] = []
        poller = self._poller(got)
        first = notice_item()
        first["id"] = None
        second = notice_item()
        second["id"] = None
        second["item"]["source_id"] = 999999
        poller._emit([first, second], source="at")
        self.assertEqual(len(got), 2)
        self.assertTrue(all(msg.msg_id for msg in got))
        self.assertNotEqual(got[0].msg_id, got[1].msg_id)

    def test_emit_converts_and_dedups(self) -> None:
        got: list[InboundMessage] = []
        poller = self._poller(got)
        item = notice_item()
        poller._emit([item], source="at")
        poller._emit([item], source="at")      # 同一 id 第二次不该再发
        self.assertEqual(len(got), 1)

    def test_emit_skips_unusable_items(self) -> None:
        got: list[InboundMessage] = []
        poller = self._poller(got)
        poller._emit([{"id": 1}, "garbage", None], source="at")  # type: ignore[list-item]
        self.assertEqual(got, [])

    def test_stop_flips_running(self) -> None:
        poller = self._poller([])
        poller._running = True
        poller.stop()
        self.assertFalse(poller._running)

    def test_run_can_be_cancelled(self) -> None:
        poller = self._poller([], interval=0.01)

        async def scenario() -> None:
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_poll_failure_does_not_kill_loop(self) -> None:
        poller = self._poller([], interval=0.01)

        async def boom() -> None:
            raise RuntimeError("接口挂了")

        poller._poll_once = boom  # type: ignore[method-assign]

        async def scenario() -> None:
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())      # 还在跑 = 没被异常带走
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_seen_set_cleared_when_huge(self) -> None:
        got: list[InboundMessage] = []
        poller = self._poller(got)
        poller._seen = {f"id-{i}" for i in range(5001)}
        poller._emit([notice_item(item_id=1)], source="at")
        self.assertLessEqual(len(poller._seen), 5001)


class TestAdapter(unittest.TestCase):
    def test_send_private_routes_to_private(self) -> None:
        poster = mock.MagicMock()
        poster.send_private = mock.AsyncMock(return_value=SendResult(ok=True))
        poster.send_comment = mock.AsyncMock(return_value=SendResult(ok=True))
        adapter = bilibili.BilibiliAdapter({}, poster=poster)

        out = OutboundMessage(
            platform=Platform.BILIBILI, kind=ChatKind.PRIVATE, chat_id="10086", text="hi"
        )
        res = asyncio.run(adapter.send(out))
        self.assertTrue(res.ok)
        poster.send_private.assert_awaited_once()

    def test_send_comment_routes_to_comment(self) -> None:
        poster = mock.MagicMock()
        poster.send_private = mock.AsyncMock(return_value=SendResult(ok=True))
        poster.send_comment = mock.AsyncMock(return_value=SendResult(ok=True))
        adapter = bilibili.BilibiliAdapter({}, poster=poster)

        out = OutboundMessage(
            platform=Platform.BILIBILI, kind=ChatKind.COMMENT, chat_id="170001:0:0", text="hi"
        )
        res = asyncio.run(adapter.send(out))
        self.assertTrue(res.ok)
        poster.send_comment.assert_awaited_once()

    def test_enqueue_without_loop_is_safe(self) -> None:
        adapter = bilibili.BilibiliAdapter({}, poster=bilibili.BiliPoster(None))
        adapter._enqueue(
            InboundMessage(
                platform=Platform.BILIBILI,
                kind=ChatKind.PRIVATE,
                chat_id="1",
                sender_id="1",
                sender_name="",
                text="早",
            )
        )
        self.assertTrue(adapter._queue.empty())

    def test_start_without_credential_returns_quietly(self) -> None:
        adapter = bilibili.BilibiliAdapter({}, poster=bilibili.BiliPoster(None))
        with mock.patch.dict(os.environ, {}, clear=True):
            asyncio.run(adapter.start())   # 不该抛，只打日志

    def test_build_without_credential_gives_sendless_adapter(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            adapter = bilibili.build({})
        self.assertFalse(adapter._poster.can_send)

    def test_build_with_credential(self) -> None:
        env = {k: "x" for k in bilibili.CRED_ENV}
        fake_cred = mock.MagicMock()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            bilibili, "build_credential", return_value=fake_cred
        ):
            adapter = bilibili.build({})
        self.assertIs(adapter._credential, fake_cred)


if __name__ == "__main__":
    unittest.main()
