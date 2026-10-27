"""表情库：稳定 ID、真实格式校验、标签、入库与引用解析（PRD FR-4.1～4.6）。

三条底线
--------
1. **不可信文件**：只认 PNG / JPEG / GIF / WebP 的真实结构（magic bytes + 尺寸头 +
   收尾结构），扩展名只用来提示，不参与判定；大小、像素、哈希都要过一遍。
2. **ID 稳定**：模型引用的是 `sticker.id`；改名称、改标签都不影响引用，
   停用（enabled=0）只是不再出现在清单里，行不删、引用仍可解释。
3. **不接受任意路径**：引用解析只查库；带 `/`、`\\`、`..`、盘符的 token 直接拒绝，
   绝不按模型给的路径去读文件（AC-17）。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .mailbox import Mailbox, MailboxError, _tx

MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 8192
MAX_NAME = 24
MAX_TAGS = 8
MAX_TAG_LEN = 16
SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

STICKER_REF_RE = re.compile(r"\[\[\s*(?:表情|sticker)\s*[:：]\s*([^\]\s]{1,256})\s*\]\]")
_UNSAFE_PATH_TOKENS = ("/", "\\", "..", ":", "~")


class StickerError(MailboxError):
    code = "sticker_error"


class StickerRejected(StickerError):
    code = "sticker_rejected"


@dataclass(slots=True)
class Sticker:
    id: int
    name: str
    path: str
    sha256: str
    mime: str
    w: int | None
    h: int | None
    bytes: int
    source: str
    enabled: bool
    tags: list[str] = field(default_factory=list)
    source_message_id: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def degrade_text(self) -> str:
        """不能发图时的文字替代表达（FR-4.5）。"""
        return f"（{self.name}）"


@dataclass(slots=True)
class ImportResult:
    sticker: Sticker
    created: bool


@dataclass(slots=True)
class StickerRef:
    """一次引用解析的结果：要么拿到库里的表情，要么带着可见错误。"""

    token: str
    sticker: Sticker | None = None
    error: str = ""


# ---------------------------------------------------------------- 图片结构校验


def sniff_image(data: bytes) -> tuple[str, int | None, int | None]:
    """按真实结构识别类型并取尺寸。不认识的直接拒绝，不看扩展名。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[12:16] != b"IHDR":
            raise StickerRejected("PNG 头不完整")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if b"IEND" not in data[-32:]:
            raise StickerRejected("PNG 没有 IEND 收尾，文件可能被截断")
        return "image/png", width, height
    if data.startswith((b"GIF87a", b"GIF89a")):
        if len(data) < 13 or b"\x3b" not in data[-4:]:
            raise StickerRejected("GIF 结构不完整")
        return "image/gif", int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise StickerRejected("JPEG 没有 EOI 收尾，文件可能被截断")
        width, height = _jpeg_size(data)
        return "image/jpeg", width, height
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        return "image/webp", width, height
    raise StickerRejected("不是支持的图片格式（只认 PNG / JPEG / GIF / WebP）")


_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    index, size = 2, len(data)
    while index + 9 < size:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in _JPEG_SOF:
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + length
    return None, None


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        if len(data) < 30:
            return None, None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        if len(data) < 25:
            return None, None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        if len(data) < 30:
            return None, None
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None, None


# ---------------------------------------------------------------- 表情库


class StickerStore:
    def __init__(self, mailbox: Mailbox, directory: str | Path, *,
                 max_bytes: int = MAX_BYTES, clock=time.time) -> None:
        self._mailbox = mailbox
        self.directory = Path(directory)
        self.max_bytes = max(1, int(max_bytes))
        self._clock = clock

    # ---- 导入 ----

    def import_file(self, path: str | Path, *, name: str = "", tags: Iterable[str] = (),
                    source: str = "file",
                    source_message_id: int | None = None) -> ImportResult:
        source_path = Path(path)
        if not source_path.is_file():
            raise StickerRejected(f"文件不存在：{source_path.name}")
        try:
            stat = source_path.stat()
        except OSError as exc:
            raise StickerRejected(f"读不了文件：{type(exc).__name__}") from exc
        if stat.st_size <= 0:
            raise StickerRejected("空文件")
        if stat.st_size > self.max_bytes:
            raise StickerRejected(
                f"文件太大（{stat.st_size} 字节，上限 {self.max_bytes} 字节）"
            )
        data = source_path.read_bytes()
        mime, width, height = sniff_image(data)
        if width and width > MAX_SIDE or height and height > MAX_SIDE:
            raise StickerRejected(f"尺寸太大（{width}×{height}，单边上限 {MAX_SIDE}）")
        digest = hashlib.sha256(data).hexdigest()
        clean_name = self._clean_name(name or source_path.stem)
        clean_tags = _clean_tags(tags)

        existing = self._find_by_hash(digest)
        if existing is not None:
            return ImportResult(sticker=existing, created=False)

        with self._mailbox.read() as conn:
            clash = conn.execute(
                "SELECT id FROM sticker WHERE name=?", (clean_name,)
            ).fetchone()
        if clash is not None:
            raise StickerRejected(f"已有同名表情「{clean_name}」（id={clash[0]}），换个名字再导入")

        suffix = SUFFIXES_BY_MIME[mime]
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{digest[:12]}-{_safe_stem(clean_name)}{suffix}"
        if not target.is_file():
            target.write_bytes(data)

        now = self._clock()

        def run(conn) -> int:
            with _tx(conn):
                sticker_id = int(
                    conn.execute(
                        """INSERT INTO sticker(name,path,sha256,mime,w,h,bytes,source,
                                               created_at,updated_at,enabled,source_message_id)
                           VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
                        (clean_name, str(target), digest, mime, width, height, len(data),
                         source[:120], now, now, source_message_id),
                    ).lastrowid or 0
                )
                for tag in clean_tags:
                    conn.execute(
                        "INSERT OR IGNORE INTO sticker_tag(sticker_id,tag) VALUES(?,?)",
                        (sticker_id, tag),
                    )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('sticker_imported','sticker',?,?,?)",
                    (str(sticker_id),
                     _json({"name": clean_name, "mime": mime, "bytes": len(data),
                            "source": source, "message_id": source_message_id}), now),
                )
            return sticker_id

        sticker_id = self._mailbox.transaction(run)
        return ImportResult(sticker=self.get(sticker_id), created=True)

    def import_directory(self, directory: str | Path | None = None) -> dict[str, Any]:
        root = Path(directory) if directory else self.directory
        report: dict[str, Any] = {"imported": 0, "skipped": 0, "failed": [],
                                  "directory": str(root)}
        if not root.is_dir():
            report["failed"].append({"file": str(root), "error": "目录不存在"})
            return report
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                continue
            # 已经导入过的文件（本库自己生成的副本）不再当新素材
            if path.parent == self.directory and _looks_generated(path.name):
                continue
            try:
                result = self.import_file(path, name=path.stem, source="directory")
            except StickerError as exc:
                report["failed"].append({"file": path.name, "error": str(exc)})
                continue
            if result.created:
                report["imported"] += 1
            else:
                report["skipped"] += 1
        return report

    def import_from_message(self, message_id: int, *, name: str = "",
                            tags: Iterable[str] = ()) -> ImportResult:
        """聊天图片入库：只接受该消息自己的附件，且必须是本地文件（FR-4.6）。"""
        detail = self._mailbox.get_message_detail(int(message_id))
        if detail is None:
            raise StickerRejected("消息不存在", code="not_found")
        for attachment in detail.get("attachments") or []:
            if str(attachment.get("kind")) != "image":
                continue
            local = str(attachment.get("path") or "")
            if not local or not Path(local).is_file():
                continue
            return self.import_file(
                local, name=name or Path(local).stem, tags=tags,
                source=f"message:{message_id}", source_message_id=int(message_id),
            )
        raise StickerRejected(
            "这条消息没有可用的本地图片（只有远程链接的图需要先落盘），未入库"
        )

    # ---- 查询 ----

    def list(self, *, query: str = "", tag: str = "",
             enabled_only: bool = True) -> list[Sticker]:
        clauses = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("s.enabled=1")
        if query.strip():
            clauses.append("(s.name LIKE ? OR EXISTS (SELECT 1 FROM sticker_tag t "
                           "WHERE t.sticker_id=s.id AND t.tag LIKE ?))")
            like = f"%{query.strip()}%"
            params.extend([like, like])
        if tag.strip():
            clauses.append("EXISTS (SELECT 1 FROM sticker_tag t2 WHERE t2.sticker_id=s.id AND t2.tag=?)")
            params.append(tag.strip())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._mailbox.read() as conn:
            rows = conn.execute(
                "SELECT s.* FROM sticker s" + where + " ORDER BY s.id", params
            ).fetchall()
            tags = self._tags_by_sticker(conn, [int(row["id"]) for row in rows])
        return [self._sticker(row, tags.get(int(row["id"]), [])) for row in rows]

    def get(self, sticker_id: int) -> Sticker:
        with self._mailbox.read() as conn:
            row = conn.execute("SELECT * FROM sticker WHERE id=?", (int(sticker_id),)).fetchone()
            if row is None:
                raise StickerError("表情不存在", code="not_found")
            tags = self._tags_by_sticker(conn, [int(sticker_id)])
        return self._sticker(row, tags.get(int(sticker_id), []))

    def catalog(self, *, limit: int = 24) -> list[Sticker]:
        return self.list(enabled_only=True)[: max(0, int(limit))]

    # ---- 修改 ----

    def rename(self, sticker_id: int, name: str) -> Sticker:
        clean = self._clean_name(name)

        def run(conn) -> None:
            with _tx(conn):
                clash = conn.execute(
                    "SELECT id FROM sticker WHERE name=? AND id<>?", (clean, int(sticker_id))
                ).fetchone()
                if clash is not None:
                    raise StickerRejected(f"已有同名表情「{clean}」")
                changed = conn.execute(
                    "UPDATE sticker SET name=?,updated_at=? WHERE id=?",
                    (clean, self._clock(), int(sticker_id)),
                ).rowcount
                if not changed:
                    raise StickerError("表情不存在", code="not_found")
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('sticker_renamed','sticker',?,?,?)",
                    (str(int(sticker_id)), _json({"name": clean}), self._clock()),
                )

        self._mailbox.transaction(run)
        return self.get(sticker_id)

    def set_tags(self, sticker_id: int, tags: Iterable[str]) -> Sticker:
        clean = _clean_tags(tags)
        now = self._clock()

        def run(conn) -> None:
            with _tx(conn):
                row = conn.execute("SELECT id FROM sticker WHERE id=?", (int(sticker_id),)).fetchone()
                if row is None:
                    raise StickerError("表情不存在", code="not_found")
                conn.execute("DELETE FROM sticker_tag WHERE sticker_id=?", (int(sticker_id),))
                for tag in clean:
                    conn.execute(
                        "INSERT OR IGNORE INTO sticker_tag(sticker_id,tag) VALUES(?,?)",
                        (int(sticker_id), tag),
                    )
                conn.execute("UPDATE sticker SET updated_at=? WHERE id=?", (now, int(sticker_id)))

        self._mailbox.transaction(run)
        return self.get(sticker_id)

    def set_enabled(self, sticker_id: int, enabled: bool) -> Sticker:
        now = self._clock()

        def run(conn) -> None:
            with _tx(conn):
                changed = conn.execute(
                    "UPDATE sticker SET enabled=?,updated_at=? WHERE id=?",
                    (1 if enabled else 0, now, int(sticker_id)),
                ).rowcount
                if not changed:
                    raise StickerError("表情不存在", code="not_found")
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('sticker_state','sticker',?,?,?)",
                    (str(int(sticker_id)), _json({"enabled": bool(enabled)}), now),
                )

        self._mailbox.transaction(run)
        return self.get(sticker_id)

    # ---- 引用 ----

    def resolve(self, token: str) -> StickerRef:
        """只认 ID 或库里的名称；路径形态直接拒，绝不落到文件系统。"""
        raw = str(token or "").strip()
        if not raw:
            return StickerRef(token=raw, error="sticker_invalid")
        if any(bad in raw for bad in _UNSAFE_PATH_TOKENS):
            return StickerRef(token=raw, error="sticker_path_rejected")
        if raw.isdigit():
            with self._mailbox.read() as conn:
                row = conn.execute(
                    "SELECT id,enabled FROM sticker WHERE id=?", (int(raw),)
                ).fetchone()
            if row is None:
                return StickerRef(token=raw, error="sticker_unknown")
            if not row["enabled"]:
                return StickerRef(token=raw, error="sticker_disabled")
            return StickerRef(token=raw, sticker=self.get(int(raw)))
        with self._mailbox.read() as conn:
            row = conn.execute(
                "SELECT id,enabled FROM sticker WHERE name=?", (raw,)
            ).fetchone()
        if row is None:
            return StickerRef(token=raw, error="sticker_unknown")
        if not row["enabled"]:
            return StickerRef(token=raw, error="sticker_disabled")
        return StickerRef(token=raw, sticker=self.get(int(row["id"])))

    # ---- 内部 ----

    def _find_by_hash(self, digest: str) -> Sticker | None:
        with self._mailbox.read() as conn:
            row = conn.execute("SELECT id FROM sticker WHERE sha256=?", (digest,)).fetchone()
        return self.get(int(row[0])) if row else None

    @staticmethod
    def _tags_by_sticker(conn, ids: list[int]) -> dict[int, list[str]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT sticker_id,tag FROM sticker_tag WHERE sticker_id IN ({marks}) ORDER BY tag",
            ids,
        ).fetchall()
        result: dict[int, list[str]] = {}
        for row in rows:
            result.setdefault(int(row["sticker_id"]), []).append(str(row["tag"]))
        return result

    @staticmethod
    def _sticker(row, tags: list[str]) -> Sticker:
        return Sticker(
            id=int(row["id"]), name=str(row["name"]), path=str(row["path"]),
            sha256=str(row["sha256"]), mime=str(row["mime"]),
            w=row["w"], h=row["h"], bytes=int(row["bytes"] or 0),
            source=str(row["source"]), enabled=bool(row["enabled"]),
            tags=list(tags),
            source_message_id=int(row["source_message_id"]) if row["source_message_id"] else None,
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"] or 0),
        )

    @staticmethod
    def _clean_name(name: str) -> str:
        clean = re.sub(r"[\\/:*?\"<>|\r\n\t]", "", str(name or "")).strip()
        clean = clean[:MAX_NAME]
        if not clean:
            raise StickerRejected("表情名称不能为空")
        return clean


SUFFIXES_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
}


def _clean_tags(tags: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in tags or ():
        tag = re.sub(r"\s+", "", str(raw))[:MAX_TAG_LEN]
        if tag and tag not in result:
            result.append(tag)
        if len(result) >= MAX_TAGS:
            break
    return result


def _safe_stem(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "", name)[:24] or "sticker"


def _looks_generated(filename: str) -> bool:
    """本库写出去的文件名形如 <sha256前12位>-<名字>.<ext>。"""
    head = filename.split("-", 1)[0]
    return len(head) == 12 and all(ch in "0123456789abcdef" for ch in head)


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)
