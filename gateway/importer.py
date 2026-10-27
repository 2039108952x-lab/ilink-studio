"""旧 ``data/sessions.json`` 的一次性导入（PRD §7.5）。

- **源文件只读**：读字节算哈希，绝不回写、绝不改名。
- **确定性去重**：``import_batch + source_hash + turn_index + direction`` 构造
  ``dedup_key``，同一个文件导入两次行数不变。
- **不伪造时间**：旧的 turn 没有平台时间，``occurred_at`` 留 NULL、
  ``time_source=import``、``ingested_at`` 是导入时间。
- **不自动回复**：``imported=1`` + ``reply_policy=never`` + ``skipped/imported``。
- **整批一个事务**：失败全回滚，不留半批数据。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .mailbox import Mailbox


class FilterImportError(RuntimeError):
    code = "import_failed"


@dataclass(slots=True)
class ImportReport:
    source: str
    source_hash: str
    imported: int = 0
    skipped: int = 0
    sessions: int = 0
    turns: int = 0
    batch: str = ""

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "source_hash": self.source_hash,
            "imported": self.imported,
            "skipped": self.skipped,
            "sessions": self.sessions,
            "turns": self.turns,
            "batch": self.batch,
        }


def import_sessions_file(mailbox: Mailbox, path: str | Path) -> ImportReport:
    source = Path(path)
    if not source.is_file():
        raise FilterImportError(f"找不到要导入的文件：{source.name}")
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise FilterImportError(f"读不了 {source.name}：{type(exc).__name__}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FilterImportError(f"{source.name} 不是合法 JSON，未导入任何内容") from exc
    if not isinstance(payload, dict):
        raise FilterImportError(f"{source.name} 结构不认识：顶层应该是「会话键 → 轮次列表」")
    result = mailbox.import_legacy(payload, source_hash=digest, source_name=source.name)
    return ImportReport(
        source=str(source), source_hash=digest,
        imported=int(result["imported"]), skipped=int(result["skipped"]),
        sessions=int(result["sessions"]), turns=int(result["turns"]),
        batch=str(result["batch"]),
    )
