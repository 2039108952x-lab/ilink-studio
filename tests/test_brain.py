"""brain 的单元测试：不真的调 CLI，只验组装与解析。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from gateway.brain import (
    BrainUnavailable,
    ReasonixBrain,
    Reply,
    resolve_sticker,
)
from gateway.config import BrainConfig
from gateway.memory import Persona, Window
from gateway.models import Attachment, PromptContext
from gateway.stickers import Sticker, StickerRef


class EmptyMemory:
    """空记忆：只为验证 prompt 排版，不碰数据库。"""

    def persona(self) -> Persona:
        return Persona(name="蛆", content="短句，别端着。", version=3, updated_at=0.0)

    def person_profile(self, person_id: int):
        raise KeyError(person_id)

    def select_facts(self, person_id: int, *, query: str = "", limit: int = 12):
        return []

    def window(self, thread_id: int, *, turns: int | None = None,
               before_id: int | None = None) -> Window:
        return Window(thread_id=thread_id)

    def cross_platform_brief(self, *args, **kwargs):
        return None


CTX = PromptContext(thread_id=1, platform="qq", kind="private", chat_id="k",
                    sender_name="神", message_id=7)


class FakeStickerStore:
    """只实现 brain 用到的三个方法：resolve / catalog。"""

    def __init__(self, stickers: list[Sticker] | None = None) -> None:
        self.stickers = list(stickers or [])
        self.catalog_calls: list[int] = []

    def resolve(self, token: str) -> StickerRef:
        if any(bad in token for bad in ("/", "\\", "..", ":", "~")):
            return StickerRef(token=token, error="sticker_path_rejected")
        by_id = {item.id: item for item in self.stickers}
        if token.isdigit():
            sticker = by_id.get(int(token))
        else:
            sticker = next((item for item in self.stickers if item.name == token), None)
        if sticker is None:
            return StickerRef(token=token, error="sticker_unknown")
        return StickerRef(token=token, sticker=sticker)

    def catalog(self, *, limit: int = 24) -> list[Sticker]:
        self.catalog_calls.append(limit)
        return self.stickers[:limit]


def sticker(sticker_id: int = 1, name: str = "笑", tags: list[str] | None = None) -> Sticker:
    return Sticker(
        id=sticker_id, name=name, path=f"C:/stickers/{name}.png", sha256="0" * 64,
        mime="image/png", w=64, h=64, bytes=128, source="test", enabled=True,
        tags=list(tags or []), created_at=1.0, updated_at=1.0,
    )


def make_brain(stickers: FakeStickerStore | None = None, **kw) -> ReasonixBrain:
    cfg = BrainConfig(**kw)
    return ReasonixBrain(cfg, None, memory=EmptyMemory(), stickers=stickers)  # type: ignore[arg-type]


class TestPrompt(unittest.TestCase):
    def test_always_includes_safety_rules(self) -> None:
        brain = make_brain()
        prompt = brain.build_prompt(None, "在吗")
        self.assertIn("即时通讯", prompt)
        self.assertIn("不要主动读文件", prompt)
        # 本次消息必须标成不可信内容（§8.1 最后一段）
        self.assertIn("[本次消息｜不可信用户内容] 在吗", prompt)

    def test_persona_is_injected_with_version(self) -> None:
        brain = make_brain()
        prompt = brain.build_prompt(CTX, "在吗")
        self.assertIn("[人格｜v3] 蛆", prompt)
        self.assertIn("短句，别端着。", prompt)


class TestParseOutput(unittest.TestCase):
    def test_dict_result_field(self) -> None:
        self.assertEqual(
            ReasonixBrain.parse_output(json.dumps({"result": "你好"})), "你好"
        )

    def test_dict_text_field(self) -> None:
        self.assertEqual(
            ReasonixBrain.parse_output(json.dumps({"text": "在"})), "在"
        )

    def test_json_string(self) -> None:
        self.assertEqual(ReasonixBrain.parse_output('"纯字符串"'), "纯字符串")

    def test_plain_text_fallback(self) -> None:
        self.assertEqual(ReasonixBrain.parse_output("就是一句话\n"), "就是一句话")

    def test_empty_raises(self) -> None:
        with self.assertRaises(BrainUnavailable):
            ReasonixBrain.parse_output("   ")

    def test_unknown_dict_raises(self) -> None:
        with self.assertRaises(BrainUnavailable):
            ReasonixBrain.parse_output(json.dumps({"weird": 1}))


class TestBuildArgs(unittest.TestCase):
    def test_includes_configured_flags(self) -> None:
        brain = make_brain(
            model="deepseek-pro",
            permission_mode="auto",
            max_steps=12,
            workspace="D:/legacy-project",
        )
        args = brain.build_args()
        self.assertIn("run", args)
        self.assertNotIn("hi", args)  # prompt 走 stdin，不进命令行
        self.assertIn("--output-format", args)
        self.assertIn("json", args)
        for flag, value in (
            ("--model", "deepseek-pro"),
            ("--permission-mode", "auto"),
            ("--max-steps", "12"),
            ("--dir", "D:/legacy-project"),
        ):
            self.assertIn(flag, args)
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_omits_empty_flags(self) -> None:
        brain = make_brain()
        args = brain.build_args()
        self.assertNotIn("--model", args)
        self.assertIn("--dir", args)
        self.assertIn("im-workspace", " ".join(args))
        self.assertNotIn("D:/legacy-project", " ".join(args))


class TestRespond(unittest.TestCase):
    def test_missing_cli_raises_clear_error(self) -> None:
        brain = make_brain(command="reasonix-does-not-exist-xyz")
        with self.assertRaises(BrainUnavailable):
            asyncio.run(brain.respond(CTX, "在吗"))

    def test_reply_carries_injection_context(self) -> None:
        """回复自带注入记录（人格版本、窗口条数），由流水线写进出站消息。"""
        brain = make_brain()
        brain._run_cli = lambda prompt, max_steps=None: asyncio.sleep(0, result="我在")  # type: ignore[method-assign]
        reply = asyncio.run(brain.respond(CTX, "在吗"))
        self.assertEqual(reply.text, "我在")
        self.assertEqual(reply.context["persona_version"], 3)
        self.assertIn("window_messages", reply.context)


class TestStickerReferences(unittest.TestCase):
    def setUp(self) -> None:
        self.store = FakeStickerStore([sticker(3, "无语", ["白眼"]), sticker(4, "笑")])

    def test_resolves_by_id_and_by_name(self) -> None:
        by_id = resolve_sticker("好啊[[表情:3]]", self.store)
        self.assertEqual(by_id.text, "好啊")
        self.assertEqual(by_id.sticker.id, 3)
        by_name = resolve_sticker("[[表情:笑]]", self.store)
        self.assertEqual(by_name.sticker.id, 4)
        self.assertEqual(by_name.sticker.name, "笑")

    def test_unknown_reference_keeps_a_visible_note(self) -> None:
        result = resolve_sticker("嗯[[表情:999]]", self.store)
        self.assertEqual(result.error, "sticker_unknown")
        self.assertIn("（表情「999」不可用）", result.text, "不能静默删标记")
        self.assertIsNone(result.sticker)

    def test_path_like_reference_is_rejected_without_reading_files(self) -> None:
        for token in ("/etc/passwd", "C:\\Windows\\win.ini", "..\\secret.png"):
            result = resolve_sticker(f"看[[表情:{token}]]", self.store)
            self.assertEqual(result.error, "sticker_path_rejected", token)
            self.assertIsNone(result.sticker)
            self.assertIn("不可用", result.text)

    def test_no_reference_leaves_text_alone(self) -> None:
        result = resolve_sticker("就这样", self.store)
        self.assertEqual(result.text, "就这样")
        self.assertIsNone(result.sticker)

    def test_multiple_references_only_send_one(self) -> None:
        result = resolve_sticker("[[表情:3]][[表情:4]]", self.store)
        self.assertEqual(result.sticker.id, 3)
        self.assertEqual(result.error, "sticker_multiple")

    def test_missing_store_keeps_marker(self) -> None:
        result = resolve_sticker("[[表情:3]]", None)
        self.assertEqual(result.error, "sticker_unavailable")
        self.assertIn("[[表情:3]]", result.text)


class TestPromptExtras(unittest.TestCase):
    def test_sticker_listing_injected(self) -> None:
        brain = make_brain(stickers=FakeStickerStore([sticker(3, "无语", ["白眼"])]))
        prompt = brain.build_prompt(None, "在吗")
        self.assertIn("[[表情:ID]]", prompt)
        self.assertIn("- 3 · 无语（标签：白眼）", prompt)

    def test_no_sticker_section_when_library_empty(self) -> None:
        brain = make_brain()
        self.assertNotIn("[[表情:", brain.build_prompt(None, "在吗"))

    def test_images_listed_for_model(self) -> None:
        brain = make_brain()
        prompt = brain.build_prompt(None, "看这个", images=["inbox/a.png"])
        self.assertIn("inbox/a.png", prompt)
        self.assertIn("1 张图", prompt)


class TestStageImages(unittest.TestCase):
    def test_copies_local_file_into_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.png"
            src.write_bytes(b"png-bytes")
            brain = make_brain(workspace=str(Path(d) / "ws"))
            staged = asyncio.run(
                brain.stage_images([Attachment(kind="image", path=str(src), name="src.png")])
            )
            self.assertEqual(len(staged), 1)
            self.assertTrue(staged[0].startswith("inbox/"))
            self.assertTrue((Path(d) / "ws" / staged[0]).is_file())

    def test_no_workspace_means_no_staging(self) -> None:
        brain = make_brain()
        self.assertEqual(
            asyncio.run(brain.stage_images([Attachment(kind="image", url="http://x/y.png")])),
            [],
        )

    def test_non_image_attachment_ignored(self) -> None:
        brain = make_brain(workspace=tempfile.mkdtemp())
        self.assertEqual(
            asyncio.run(brain.stage_images([Attachment(kind="video", url="http://x/y.mp4")])),
            [],
        )


class TestRespondWithSticker(unittest.TestCase):
    def test_sticker_becomes_reply_attachment(self) -> None:
        store = FakeStickerStore([sticker(7, "笑")])
        brain = make_brain(stickers=store)
        brain._run_cli = lambda prompt, max_steps=None: asyncio.sleep(  # type: ignore[method-assign]
            0, result="好[[表情:7]]"
        )
        reply = asyncio.run(brain.respond(CTX, "笑一个"))
        self.assertIsInstance(reply, Reply)
        self.assertEqual(reply.text, "好")
        self.assertIsNotNone(reply.sticker)
        self.assertEqual(reply.sticker.id, 7)  # type: ignore[union-attr]

    def test_unknown_sticker_is_reported_not_dropped(self) -> None:
        brain = make_brain(stickers=FakeStickerStore([sticker(7, "笑")]))
        brain._run_cli = lambda prompt, max_steps=None: asyncio.sleep(  # type: ignore[method-assign]
            0, result="好[[表情:不存在]]"
        )
        reply = asyncio.run(brain.respond(CTX, "笑一个"))
        self.assertIsNone(reply.sticker)
        self.assertEqual(reply.sticker_error, "sticker_unknown")
        self.assertIn("不可用", reply.text)

    def test_empty_text_with_image_is_not_dropped(self) -> None:
        brain = make_brain(workspace=tempfile.mkdtemp())
        seen: list[str] = []

        async def fake(prompt: str, max_steps=None) -> str:
            seen.append(prompt)
            return "看到了"

        brain._run_cli = fake  # type: ignore[method-assign]
        src = Path(tempfile.mkdtemp()) / "x.png"
        src.write_bytes(b"a")
        asyncio.run(brain.respond(CTX, "", [Attachment(kind="image", path=str(src), name="x.png")]))
        self.assertIn("发了一张图", seen[0])


class TestMaxSteps(unittest.TestCase):
    def test_text_only_runs_one_step(self) -> None:
        brain = make_brain(max_steps=3)
        seen: list[int | None] = []

        async def fake(prompt: str, max_steps=None) -> str:
            seen.append(max_steps)
            return "好"

        brain._run_cli = fake  # type: ignore[method-assign]
        asyncio.run(brain.respond(CTX, "在吗"))
        self.assertEqual(seen, [1])

    def test_image_allows_full_steps_to_read_it(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "a.png"
            src.write_bytes(b"x")
            brain = make_brain(max_steps=3, workspace=str(Path(d) / "ws"))
            seen: list[int | None] = []

            async def fake(prompt: str, max_steps=None) -> str:
                seen.append(max_steps)
                return "看到了"

            brain._run_cli = fake  # type: ignore[method-assign]
            asyncio.run(
                brain.respond(CTX, "看图", [Attachment(kind="image", path=str(src), name="a.png")])
            )
            # None = 交给配置里的 max_steps
            self.assertEqual(seen, [None])
            args = brain.build_args(None)
            self.assertEqual(args[args.index("--max-steps") + 1], "3")

    def test_video_bv_allows_full_steps(self) -> None:
        """提到视频必须给工具轮，否则模型没机会调 bili_video。"""
        brain = make_brain(max_steps=3)
        seen: list[int | None] = []

        async def fake(prompt: str, max_steps=None) -> str:
            seen.append(max_steps)
            return "看了"

        brain._run_cli = fake  # type: ignore[method-assign]
        asyncio.run(brain.respond(CTX, "这个 BV1xx411c7mD 到底讲了啥"))
        self.assertEqual(seen, [None])

    def test_video_url_allows_full_steps(self) -> None:
        brain = make_brain(max_steps=3)
        seen: list[int | None] = []

        async def fake(prompt: str, max_steps=None) -> str:
            seen.append(max_steps)
            return "看了"

        brain._run_cli = fake  # type: ignore[method-assign]
        asyncio.run(brain.respond(CTX, "看 https://www.bilibili.com/video/av170001"))
        self.assertEqual(seen, [None])

    def test_av_number_allows_full_steps(self) -> None:
        brain = make_brain(max_steps=3)
        seen: list[int | None] = []

        async def fake(prompt: str, max_steps=None) -> str:
            seen.append(max_steps)
            return "看了"

        brain._run_cli = fake  # type: ignore[method-assign]
        asyncio.run(brain.respond(CTX, "av170001 是啥"))
        self.assertEqual(seen, [None])

    def test_chat_without_video_keeps_one_step(self) -> None:
        brain = make_brain(max_steps=3)
        seen: list[int | None] = []

        async def fake(prompt: str, max_steps=None) -> str:
            seen.append(max_steps)
            return "好"

        brain._run_cli = fake  # type: ignore[method-assign]
        asyncio.run(brain.respond(CTX, "今天天气怎么样"))
        self.assertEqual(seen, [1])


if __name__ == "__main__":
    unittest.main()
