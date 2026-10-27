"""表情库验收：真实格式校验、去重、改名不改 ID、停用、入库与引用（FR-4.1/4.2/4.6）。"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from gateway.mailbox import Incoming, Mailbox
from gateway.stickers import StickerRejected, StickerStore, sniff_image


def png(w: int = 4, h: int = 4) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress((b"\x00" + b"\xff\x00\x00" * w) * h)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
            + chunk(b"IEND", b""))


def gif(w: int = 3, h: int = 4) -> bytes:
    return (b"GIF89a" + struct.pack("<HH", w, h) + b"\xf0\x00\x00"
            + b"\x00\x00\x00\xff\xff\xff" + b"\x2c" + b"\x00" * 9
            + b"\x02\x02\x44\x01\x00" + b"\x3b")


def jpeg(w: int = 5, h: int = 6) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", h, w) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def webp(w: int = 7, h: int = 8) -> bytes:
    bits = (w - 1) | ((h - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", bits) + b"\x00\x00\x00"
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


class StickerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.mailbox = Mailbox(self.root / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account = self.mailbox.ensure_account("qq", "onebot")
        self.store = StickerStore(self.mailbox, self.root / "stickers")
        self.counter = 0

    def write(self, data: bytes, name: str = "img.png") -> Path:
        self.counter += 1
        path = self.root / f"{self.counter}-{name}"
        path.write_bytes(data)
        return path


class TestSniff(StickerCase):
    def test_known_formats_and_sizes(self) -> None:
        self.assertEqual(sniff_image(png(4, 3)), ("image/png", 4, 3))
        self.assertEqual(sniff_image(gif(3, 4)), ("image/gif", 3, 4))
        self.assertEqual(sniff_image(jpeg(5, 6)), ("image/jpeg", 5, 6))
        self.assertEqual(sniff_image(webp(7, 8)), ("image/webp", 7, 8))

    def test_extension_is_not_trusted(self) -> None:
        with self.assertRaises(StickerRejected):
            sniff_image(b"just some text pretending to be an image")

    def test_truncated_files_are_rejected(self) -> None:
        with self.assertRaises(StickerRejected):
            sniff_image(png()[:-16])
        with self.assertRaises(StickerRejected):
            sniff_image(jpeg()[:-4])


class TestImport(StickerCase):
    def test_import_and_dedupe_by_hash(self) -> None:
        first = self.store.import_file(self.write(png()), name="无语", tags=["无语", "白眼"])
        self.assertTrue(first.created)
        self.assertEqual((first.sticker.mime, first.sticker.w, first.sticker.h),
                         ("image/png", 4, 4))
        self.assertEqual(first.sticker.tags, ["无语", "白眼"])

        again = self.store.import_file(self.write(png()), name="换个名字")
        self.assertFalse(again.created)
        self.assertEqual(again.sticker.id, first.sticker.id)

    def test_size_limit(self) -> None:
        store = StickerStore(self.mailbox, self.root / "small", max_bytes=200)
        with self.assertRaises(StickerRejected) as caught:
            # 纯色 PNG 压缩后很小，补点填充把文件顶过上限
            store.import_file(self.write(png(20, 20) + bytes(300)), name="大图")
        self.assertIn("太大", str(caught.exception))

    def test_name_conflict(self) -> None:
        self.store.import_file(self.write(png(2, 2)), name="笑")
        with self.assertRaises(StickerRejected):
            self.store.import_file(self.write(png(3, 3)), name="笑")

    def test_oversized_pixels_rejected(self) -> None:
        with self.assertRaises(StickerRejected):
            self.store.import_file(self.write(png(9000, 2)), name="超宽")

    def test_tags_are_cleaned_and_capped(self) -> None:
        result = self.store.import_file(
            self.write(png()), name="标签测试",
            tags=[" 白 眼 ", "白眼", "", "a", "b", "c", "d", "e", "f", "g", "h"],
        )
        self.assertLessEqual(len(result.sticker.tags), 8)
        self.assertNotIn(" ", "".join(result.sticker.tags))
        self.assertEqual(len(result.sticker.tags), len(set(result.sticker.tags)))


class TestRenameAndDisable(StickerCase):
    def setUp(self) -> None:
        super().setUp()
        self.sticker = self.store.import_file(self.write(png()), name="无语", tags=["白眼"]).sticker

    def test_rename_keeps_id_and_reference(self) -> None:
        renamed = self.store.rename(self.sticker.id, "白眼狼")
        self.assertEqual(renamed.id, self.sticker.id)
        self.assertEqual(self.store.resolve(str(self.sticker.id)).sticker.id, self.sticker.id)
        self.assertIsNotNone(self.store.resolve("白眼狼").sticker)
        self.assertEqual(self.store.resolve("无语").error, "sticker_unknown")

    def test_disable_hides_from_catalog_but_keeps_row(self) -> None:
        self.store.set_enabled(self.sticker.id, False)
        self.assertEqual(self.store.catalog(), [])
        self.assertEqual(self.store.resolve(str(self.sticker.id)).error, "sticker_disabled")
        self.assertFalse(self.store.get(self.sticker.id).enabled)

    def test_path_like_tokens_are_rejected(self) -> None:
        for token in ("/etc/passwd", r"C:\Windows\win.ini", "..\\secret.png", "~/x.png"):
            self.assertEqual(self.store.resolve(token).error, "sticker_path_rejected", token)

    def test_unknown_tokens(self) -> None:
        self.assertEqual(self.store.resolve("9999").error, "sticker_unknown")
        self.assertEqual(self.store.resolve("没这个").error, "sticker_unknown")


class TestMessageImport(StickerCase):
    def ingest(self, attachments: list[dict]) -> int:
        result = self.mailbox.ingest(Incoming(
            account_id=self.account, platform="qq", kind="private", chat_id="u1",
            sender_id="u1", sender_name="神", id_kind="stable", text="看图",
            platform_msg_id=f"m{self.counter}",
            attachments_json=json.dumps(attachments),
        ))
        return result.message_id

    def test_import_from_message_local_image(self) -> None:
        local = self.write(png(), "chat.png")
        message_id = self.ingest([{"kind": "image", "path": str(local), "name": "chat.png"}])
        result = self.store.import_from_message(message_id, name="群里存的", tags=["群"])
        self.assertTrue(result.created)
        self.assertEqual(result.sticker.source, f"message:{message_id}")
        self.assertEqual(result.sticker.source_message_id, message_id)

    def test_remote_only_image_is_not_downloaded(self) -> None:
        message_id = self.ingest([{"kind": "image", "url": "http://example.invalid/x.png"}])
        with self.assertRaises(StickerRejected) as caught:
            self.store.import_from_message(message_id, name="远程图")
        self.assertIn("本地", str(caught.exception))

    def test_message_without_image(self) -> None:
        message_id = self.ingest([])
        with self.assertRaises(StickerRejected):
            self.store.import_from_message(message_id, name="空")


class TestDirectoryImport(StickerCase):
    def test_reports_bad_files_and_skips_own_output(self) -> None:
        source = self.root / "incoming"
        source.mkdir()
        (source / "笑.png").write_bytes(png())
        (source / "坏.png").write_bytes(b"junk")
        report = self.store.import_directory(source)
        self.assertEqual(report["imported"], 1)
        self.assertEqual([item["file"] for item in report["failed"]], ["坏.png"])

        again = self.store.import_directory(self.store.directory)
        self.assertEqual(again["imported"], 0, "本库写出去的副本不该被当成新素材")


if __name__ == "__main__":
    unittest.main()
