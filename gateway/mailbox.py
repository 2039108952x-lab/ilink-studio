"""统一消息库（mailbox.db）：唯一的业务数据入口。

为什么这么写
------------
- **一个专用写线程**（PRD §7.4）：所有写事务排一条队、在同一个连接上串行执行。
  读用临时连接，不持有长事务。这样进程内不会出现两个写者抢锁，
  也不会出现「游标已前进但消息没落库」。
- **落库先于模型**（PRD §6.3）：``ingest`` 在一个事务里写消息 + 身份 + 会话 + 检查点，
  提交之后流水线才允许调用模型。
- **不搞兜底**（FR-2.10）：打不开、写失败一律抛 ``MailboxError``，
  上层据此暂停 AI 处理并报警，绝不退回 JSON 或第二个库继续收发。
- **正文与非正文分开**：白名单外的消息只留 ``metadata_only`` 审计元数据，
  ``body`` 为 NULL，FTS 里也查不到（AC-05）。
"""

from __future__ import annotations

import hashlib
import json
import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from . import db

T = TypeVar("T")

INBOUND_STATUSES = frozenset({
    "received", "queued", "processing", "replied", "skipped", "failed", "needs_review", "outbound",
})
ID_KINDS = frozenset({"stable", "opaque", "provisional"})
SCOPES = frozenset({"private", "group", "comment"})
# CHECK 里写过一遍，这里再写一遍是为了在写库之前就给出可读错误。
_FORBIDDEN_STATE_KEYS = (
    "token", "secret", "cookie", "sessdata", "bili_jct", "buvid3", "context_token",
    "password", "passwd", "authorization", "credential", "api_key",
)
_MAX_AUDIT_PAYLOAD = 8000


class MailboxError(RuntimeError):
    """业务库不可用或请求不合法；``code`` 可安全展示给本机用户。"""

    code = "mailbox_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class BoundaryChanged(MailboxError):
    """生成期间人物归属变了（合并/撤销），这条过期回复不能再发出去（§7.3 第 5 条）。"""

    code = "boundary_changed"


# ---------------------------------------------------------------- 数据结构


@dataclass(slots=True)
class Incoming:
    """一条已经解析过的最小身份/策略信息，等待落库。"""

    account_id: int
    platform: str
    kind: str
    chat_id: str
    sender_id: str
    sender_name: str = ""
    id_kind: str = "stable"
    text: str = ""
    platform_msg_id: str = ""
    dedup_key: str = ""
    attachments_json: str = "[]"
    occurred_at: float | None = None
    time_source: str = "platform"
    reply_policy: str = "normal"
    storage_class: str = "full"
    status: str = "received"
    skip_reason: str | None = None
    error_code: str | None = None
    imported: bool = False
    is_bot_sender: bool = False


@dataclass(slots=True)
class IngestResult:
    message_id: int
    conversation_id: int
    thread_id: int
    person_id: int
    identity_id: int
    created: bool
    status: str
    reply_policy: str


@dataclass(slots=True)
class ClaimedMessage:
    """一条拿到租约、可以交给模型的入站消息。"""

    id: int
    conversation_id: int
    thread_id: int
    account_id: int
    platform: str
    kind: str
    chat_id: str
    direction: str
    body: str
    attachments_json: str
    sender_name: str
    person_name: str
    person_id: int
    identity_id: int | None
    platform_msg_id: str
    occurred_at: float | None
    ingested_at: float
    reply_policy: str
    attempt_count: int
    imported: bool
    boundary_version: int = 0


@dataclass(slots=True)
class OutboxTask:
    outbox_id: int
    message_id: int
    source_message_id: int | None
    account_id: int
    platform: str
    kind: str
    chat_id: str
    text: str
    attachments_json: str
    requested_kind: str
    effective_kind: str
    attempt_count: int
    reply_to: str = ""


@dataclass(slots=True)
class MessagePage:
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    slow: bool = False


@dataclass(slots=True)
class OutboxResult:
    outbox_id: int
    state: str
    created: bool = True


# ---------------------------------------------------------------- 写线程


class _Writer:
    """一个专用线程 + 一个连接，串行执行所有写事务。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: queue.Queue[tuple[Callable[[sqlite3.Connection], Any], Future] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="mailbox-writer", daemon=True)
        self._conn: sqlite3.Connection | None = None
        self._failure: str | None = None
        self._closed = False
        self._thread.start()

    @property
    def failure(self) -> str | None:
        return self._failure

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                # 退出前必须自己关连接：Windows 上没关的句柄会让临时库删不掉。
                self._close_conn()
                return
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if self._conn is None:
                    self._conn = db.connect(self._path)
                future.set_result(fn(self._conn))
            except BaseException as exc:  # noqa: BLE001 - 原样交给等待方
                if isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.IntegrityError):
                    # 连接可能已经坏了；丢掉它，下一次调用重连。
                    self._failure = f"{type(exc).__name__}"
                    self._close_conn()
                future.set_exception(exc)

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def call(self, fn: Callable[[sqlite3.Connection], T], *, timeout: float = 30.0) -> T:
        if self._closed:
            raise MailboxError("数据库已关闭")
        future: Future = Future()
        self._queue.put((fn, future))
        try:
            result = future.result(timeout=timeout)
        except sqlite3.Error as exc:
            raise MailboxError(
                f"写库失败：{type(exc).__name__}", code="database_write_failed"
            ) from exc
        if self._failure is not None and result is None:
            # 记录到 health，由流水线决定暂停 AI 处理。
            return result  # type: ignore[return-value]
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._thread.join(timeout=10)


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reject_secret_keys(payload: Mapping[str, Any], *, where: str) -> None:
    for key in payload:
        lowered = str(key).lower()
        if any(bad in lowered for bad in _FORBIDDEN_STATE_KEYS):
            raise MailboxError(f"{where} 里不允许放凭据类字段：{key}", code="secret_not_allowed")


def _encode_cursor(row: Mapping[str, Any]) -> str:
    # repr 与 SQLite REAL 是同一套 IEEE754，往返无损；
    # 用 "%.6f" 这类格式化会在比较点上舍入，翻页时把同一行再吐一次。
    return f"{float(row['ingested_at'])!r}:{int(row['id'])}"


def _decode_cursor(cursor: str) -> tuple[float, int]:
    raw, _, ident = str(cursor).partition(":")
    try:
        return float(raw), int(ident)
    except ValueError as exc:
        raise MailboxError("分页游标无效", code="invalid_cursor") from exc


# ---------------------------------------------------------------- 仓储


class Mailbox:
    """mailbox.db 的全部读写。"""

    def __init__(self, path: str | Path, *, backup_dir: str | Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.backup_dir = Path(backup_dir) if backup_dir is not None else self.path.parent / "backups"
        try:
            db.migrate(self.path, backup_dir=self.backup_dir, clock=clock)
        except db.DatabaseError as exc:
            raise MailboxError(str(exc), code=exc.code) from exc
        self._writer = _Writer(self.path)

    # ---- 生命周期 ----

    def close(self) -> None:
        self._writer.close()

    @property
    def health(self) -> dict[str, Any]:
        failure = self._writer.failure
        return {"ok": failure is None, "error_code": failure}

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = db.connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    def _write(self, fn: Callable[[sqlite3.Connection], T], *, timeout: float = 30.0) -> T:
        return self._writer.call(fn, timeout=timeout)

    def transaction(self, fn: Callable[[sqlite3.Connection], T], *,
                    timeout: float = 30.0) -> T:
        """给 memory / 身份合并等模块复用同一条写通道，不另开写连接。"""
        return self._write(fn, timeout=timeout)

    # ---- 账号槽位 ----

    def ensure_account(self, platform: str, channel: str, *, display_name: str = "",
                       expected_uid: str = "") -> int:
        now = self.clock()

        def run(conn: sqlite3.Connection) -> int:
            with _tx(conn):
                conn.execute(
                    "INSERT INTO account(platform,channel,display_name,expected_uid,active,"
                    "created_at,updated_at) VALUES(?,?,?,?,1,?,?) "
                    "ON CONFLICT(platform,channel) DO NOTHING",
                    (platform, channel, display_name, expected_uid, now, now),
                )
                row = conn.execute(
                    "SELECT id FROM account WHERE platform=? AND channel=?", (platform, channel)
                ).fetchone()
            return int(row[0])

        return self._write(run)

    def account_id(self, platform: str) -> int | None:
        """取该平台当前启用的槽位（微信只会有 official / wxauto 其一 active）。"""
        with self.read() as conn:
            row = conn.execute(
                "SELECT id FROM account WHERE platform=? AND active=1 ORDER BY id LIMIT 1",
                (platform,),
            ).fetchone()
        return int(row[0]) if row else None

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM account WHERE id=?", (int(account_id),)).fetchone()
        return dict(row) if row else None

    def set_account_connection(self, account_id: int, state: str,
                               error_code: str | None = None) -> None:
        """适配器把 needs_human / error 之类的连接状态写回账号槽位。"""
        now = self.clock()

        def run(conn) -> None:
            with _tx(conn):
                conn.execute(
                    "UPDATE account SET connection_state=?,last_error_code=?,updated_at=? WHERE id=?",
                    (state, error_code, now, int(account_id)),
                )

        self._write(run)

    def set_send_health(self, account_id: int, state: str, safe_error: str = "") -> None:
        """发送健康写这里而不是另开一个写连接，避免和消息写抢锁。"""
        now = self.clock()

        def run(conn: sqlite3.Connection) -> None:
            with _tx(conn):
                conn.execute(
                    "UPDATE account SET send_health=?,last_send_at=?,last_send_error_safe=?,"
                    "updated_at=? WHERE id=?",
                    (state, now, safe_error[:500], now, account_id),
                )

        self._write(run)

    def get_setting(self, key: str, default: str = "") -> str:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    # ---- 入站落库 ----

    def ingest(self, incoming: Incoming) -> IngestResult:
        if incoming.id_kind not in ID_KINDS:
            raise MailboxError(f"未知身份类型：{incoming.id_kind}", code="invalid_identity_kind")
        if incoming.kind not in SCOPES:
            raise MailboxError(f"未知会话类型：{incoming.kind}", code="invalid_scope")
        if incoming.storage_class == "full" and not incoming.text:
            raise MailboxError("full 消息必须有正文", code="invalid_body")
        return self._write(lambda conn: self._ingest(conn, incoming))

    def _ingest(self, conn: sqlite3.Connection, incoming: Incoming) -> IngestResult:
        now = self.clock()
        with _tx(conn):
            identity_id, person_id = self._resolve_identity(conn, incoming, now=now)
            thread_id, conversation_id = self._resolve_thread(
                conn, incoming, person_id=person_id, now=now
            )
            dedup_key = incoming.dedup_key
            if not incoming.platform_msg_id and not dedup_key:
                dedup_key = self._ordinal_key(conn, incoming)
            try:
                cursor = conn.execute(
                    """INSERT INTO message
                       (conversation_id,account_id,platform,platform_msg_id,dedup_key,direction,
                        sender_identity_id,body,attachments_json,storage_class,processing_status,
                        skip_reason,error_code,imported,reply_policy,occurred_at,ingested_at,time_source)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        conversation_id, incoming.account_id, incoming.platform,
                        incoming.platform_msg_id or None, dedup_key or None, "in",
                        identity_id,
                        incoming.text if incoming.storage_class == "full" else None,
                        incoming.attachments_json, incoming.storage_class, incoming.status,
                        incoming.skip_reason, incoming.error_code,
                        1 if incoming.imported else 0, incoming.reply_policy,
                        incoming.occurred_at, now,
                        incoming.time_source,
                    ),
                )
                message_id = int(cursor.lastrowid or 0)
                created = True
            except sqlite3.IntegrityError:
                row = self._find_existing(conn, incoming, dedup_key)
                if row is None:
                    raise
                message_id, status, policy = int(row["id"]), str(row["processing_status"]), str(row["reply_policy"])
                created = False
            else:
                status, policy = incoming.status, incoming.reply_policy
                conn.execute("UPDATE thread SET updated_at=? WHERE id=?", (now, thread_id))
        return IngestResult(
            message_id=message_id, conversation_id=conversation_id, thread_id=thread_id,
            person_id=person_id, identity_id=identity_id, created=created,
            status=status, reply_policy=policy,
        )

    @staticmethod
    def _find_existing(conn: sqlite3.Connection, incoming: Incoming,
                       dedup_key: str) -> sqlite3.Row | None:
        if incoming.platform_msg_id:
            row = conn.execute(
                "SELECT id,processing_status,reply_policy FROM message "
                "WHERE account_id=? AND platform_msg_id=?",
                (incoming.account_id, incoming.platform_msg_id),
            ).fetchone()
            if row is not None:
                return row
        if dedup_key:
            return conn.execute(
                "SELECT id,processing_status,reply_policy FROM message "
                "WHERE account_id=? AND dedup_key=?",
                (incoming.account_id, dedup_key),
            ).fetchone()
        return None

    @staticmethod
    def _ordinal_key(conn: sqlite3.Connection, incoming: Incoming) -> str:
        """没有平台消息 ID 时的受控替代键（PRD §7.6）。

        正文参与哈希，但不单独构成键：同一人连说两句「嗯」会拿到 :1 和 :2，
        两条都落库、都能回。单纯 sha1(昵称+正文) 会把第二句吞掉。
        """
        base = "h" + hashlib.sha1(
            f"{incoming.kind}\x00{incoming.chat_id}\x00{incoming.sender_id}\x00{incoming.text}"
            .encode("utf-8")
        ).hexdigest()
        row = conn.execute(
            "SELECT COUNT(*) FROM message WHERE account_id=? AND dedup_key LIKE ?",
            (incoming.account_id, base + ":%"),
        ).fetchone()
        return f"{base}:{int(row[0]) + 1}"

    def _resolve_identity(self, conn: sqlite3.Connection, incoming: Incoming,
                          *, now: float) -> tuple[int, int]:
        row = conn.execute(
            "SELECT id,person_id FROM identity WHERE platform=? AND id_kind=? AND platform_uid=?",
            (incoming.platform, incoming.id_kind, incoming.sender_id),
        ).fetchone()
        if row is not None:
            # 平台昵称会改，身份键不变。
            if incoming.sender_name:
                conn.execute(
                    "UPDATE identity SET display_name=? WHERE id=? AND IFNULL(display_name,'')<>?",
                    (incoming.sender_name, int(row[0]), incoming.sender_name),
                )
            return int(row[0]), int(row[1])
        name = incoming.sender_name or incoming.sender_id or "未知联系人"
        person_id = int(
            conn.execute(
                "INSERT INTO person(display_name,created_at) VALUES(?,?)", (name, now)
            ).lastrowid or 0
        )
        identity_id = int(
            conn.execute(
                "INSERT INTO identity(person_id,platform,platform_uid,id_kind,display_name,"
                "is_bot,created_at) VALUES(?,?,?,?,?,?,?)",
                (person_id, incoming.platform, incoming.sender_id, incoming.id_kind, name,
                 1 if incoming.is_bot_sender else 0, now),
            ).lastrowid or 0
        )
        return identity_id, person_id

    def _resolve_thread(self, conn: sqlite3.Connection, incoming: Incoming, *,
                        person_id: int, now: float) -> tuple[int, int]:
        if incoming.kind == "private":
            row = conn.execute(
                "SELECT id FROM thread WHERE scope='private' AND person_id=?", (person_id,)
            ).fetchone()
            if row is None:
                thread_id = int(
                    conn.execute(
                        "INSERT INTO thread(scope,person_id,title,created_at,updated_at) "
                        "VALUES('private',?,?,?,?)",
                        (person_id, incoming.sender_name or incoming.chat_id, now, now),
                    ).lastrowid or 0
                )
            else:
                thread_id = int(row[0])
        else:
            # 群/评论：一个会话一个 thread，成员身份不参与 thread 归属。
            row = conn.execute(
                "SELECT id,thread_id FROM conversation WHERE account_id=? AND kind=? AND chat_id=?",
                (incoming.account_id, incoming.kind, incoming.chat_id),
            ).fetchone()
            if row is not None:
                return int(row[1]), int(row[0])
            thread_id = int(
                conn.execute(
                    "INSERT INTO thread(scope,person_id,title,created_at,updated_at) "
                    "VALUES(?,NULL,?,?,?)",
                    (incoming.kind, incoming.chat_id, now, now),
                ).lastrowid or 0
            )

        row = conn.execute(
            "SELECT id FROM conversation WHERE account_id=? AND kind=? AND chat_id=?",
            (incoming.account_id, incoming.kind, incoming.chat_id),
        ).fetchone()
        if row is not None:
            conversation_id = int(row[0])
            conn.execute(
                "UPDATE conversation SET thread_id=? WHERE id=?", (thread_id, conversation_id)
            )
        else:
            conversation_id = int(
                conn.execute(
                    "INSERT INTO conversation(thread_id,account_id,kind,chat_id,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (thread_id, incoming.account_id, incoming.kind, incoming.chat_id, now),
                ).lastrowid or 0
            )
        return thread_id, conversation_id

    def record_rejected(self, incoming: Incoming, *, reason: str = "not_allowlisted") -> int:
        """白名单外的消息：只留最少审计元数据，正文/附件/原始报文一概不落库。"""
        metadata = Incoming(
            account_id=incoming.account_id, platform=incoming.platform, kind=incoming.kind,
            chat_id=incoming.chat_id, sender_id=incoming.sender_id,
            sender_name=incoming.sender_name, id_kind=incoming.id_kind,
            text="", platform_msg_id=incoming.platform_msg_id,
            attachments_json="[]", occurred_at=incoming.occurred_at,
            time_source=incoming.time_source, reply_policy="never",
            storage_class="metadata_only", status="skipped", skip_reason=reason,
        )
        result = self.ingest(metadata)
        self.audit(
            "inbound_rejected", "message", str(result.message_id),
            {"reason": reason, "platform": incoming.platform, "kind": incoming.kind},
        )
        return result.message_id

    # ---- 入站状态机 ----

    def set_status(self, message_id: int, status: str, *, skip_reason: str | None = None,
                   error_code: str | None = None, next_attempt_at: float | None = None) -> None:
        if status not in INBOUND_STATUSES:
            raise MailboxError(f"未知入站状态：{status}", code="invalid_status")
        if status == "skipped" and not skip_reason:
            raise MailboxError("skipped 必须带 skip_reason", code="invalid_status")
        if status == "failed" and not error_code:
            raise MailboxError("failed 必须带 error_code", code="invalid_status")

        def run(conn: sqlite3.Connection) -> None:
            with _tx(conn):
                conn.execute(
                    "UPDATE message SET processing_status=?,skip_reason=?,error_code=?,"
                    "next_attempt_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                    (status, skip_reason, error_code, next_attempt_at, message_id),
                )

        self._write(run)

    def confirm_message(self, message_id: int, *, actor: str = "owner") -> dict[str, Any]:
        """人工核对一条 needs_review 消息后才允许生成回复（PRD §7.6.5）。"""
        now = self.clock()

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            with _tx(conn):
                row = conn.execute(
                    "SELECT processing_status FROM message WHERE id=?", (message_id,)
                ).fetchone()
                if row is None:
                    raise MailboxError("消息不存在", code="not_found")
                if str(row[0]) != "needs_review":
                    raise MailboxError("只有待核对的消息需要人工确认", code="not_reviewable")
                conn.execute(
                    "UPDATE message SET processing_status='queued',skip_reason=NULL,"
                    "next_attempt_at=NULL WHERE id=?", (message_id,)
                )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('message_confirmed','message',?,?,?)",
                    (str(message_id), _json({"actor": actor}), now),
                )
            return {"message_id": int(message_id), "processing_status": "queued"}

        return self._write(run)

    def claim_message(self, owner: str, *, lease_seconds: float = 180.0) -> ClaimedMessage | None:
        """按租约领取一条待处理入站消息；同 thread 一次只放一条出去（PRD §6.3）。"""
        now, expires = self.clock(), self.clock() + lease_seconds

        def run(conn: sqlite3.Connection) -> ClaimedMessage | None:
            with _tx(conn):
                row = conn.execute(
                    """SELECT m.id FROM message m
                       JOIN conversation c ON c.id=m.conversation_id
                       WHERE m.direction='in'
                         AND m.processing_status IN ('received','queued')
                         AND (m.next_attempt_at IS NULL OR m.next_attempt_at<=?)
                         AND NOT EXISTS (
                           SELECT 1 FROM message busy
                           JOIN conversation bc ON bc.id=busy.conversation_id
                           WHERE bc.thread_id=c.thread_id
                             AND busy.processing_status='processing' AND busy.id<>m.id)
                       ORDER BY m.id LIMIT 1""",
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                message_id = int(row[0])
                conn.execute(
                    "UPDATE message SET processing_status='processing',lease_owner=?,"
                    "lease_expires_at=?,attempt_count=attempt_count+1 WHERE id=?",
                    (owner, expires, message_id),
                )
                return self._claimed(conn, message_id)

        return self._write(run)

    @staticmethod
    def _claimed(conn: sqlite3.Connection, message_id: int) -> ClaimedMessage | None:
        row = conn.execute(
            """SELECT m.*, c.thread_id, c.kind AS conversation_kind, c.chat_id,
                      IFNULL(t.boundary_version,0) AS boundary_version,
                      IFNULL(i.display_name,'') AS sender_name,
                      IFNULL(p.display_name,'') AS person_name,
                      IFNULL(p.id,0) AS resolved_person_id
               FROM message m
               JOIN conversation c ON c.id=m.conversation_id
               LEFT JOIN identity i ON i.id=m.sender_identity_id
               LEFT JOIN thread t ON t.id=c.thread_id
               LEFT JOIN person p ON p.id=t.person_id
               WHERE m.id=?""",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        return ClaimedMessage(
            id=int(row["id"]), conversation_id=int(row["conversation_id"]),
            thread_id=int(row["thread_id"]), account_id=int(row["account_id"]),
            platform=str(row["platform"]), kind=str(row["conversation_kind"]),
            chat_id=str(row["chat_id"]), direction=str(row["direction"]),
            body=str(row["body"] or ""), attachments_json=str(row["attachments_json"]),
            sender_name=str(row["sender_name"]), person_name=str(row["person_name"]),
            person_id=int(row["resolved_person_id"]),
            identity_id=int(row["sender_identity_id"]) if row["sender_identity_id"] else None,
            platform_msg_id=str(row["platform_msg_id"] or ""),
            occurred_at=row["occurred_at"], ingested_at=float(row["ingested_at"]),
            reply_policy=str(row["reply_policy"]), attempt_count=int(row["attempt_count"]),
            imported=bool(row["imported"]),
            boundary_version=int(row["boundary_version"] or 0),
        )

    def get_message(self, message_id: int) -> ClaimedMessage | None:
        with self.read() as conn:
            return self._claimed(conn, message_id)

    def recover(self) -> dict[str, int]:
        """启动时收拾上一个进程留下的现场（PRD §6.5）。

        - ``processing`` 的入站消息：本进程还没接手，退回 ``queued`` 重新走一遍；
          模型可能被多调一次，但平台侧还没发出任何东西。
        - ``sending`` 的 outbox：平台可能已经收下了，结果不可知，只能转 ``unknown``，
          **禁止自动重发**，由人工核对。
        """
        now = self.clock()

        def run(conn: sqlite3.Connection) -> dict[str, int]:
            with _tx(conn):
                messages = conn.execute(
                    "UPDATE message SET processing_status='queued',lease_owner=NULL,"
                    "lease_expires_at=NULL,next_attempt_at=NULL "
                    "WHERE processing_status='processing'"
                ).rowcount
                stuck = conn.execute(
                    "UPDATE outbox SET state='unknown',lease_owner=NULL,lease_expires_at=NULL,"
                    "error_code=IFNULL(error_code,'crashed_during_send') "
                    "WHERE state='sending' RETURNING source_message_id"
                ).fetchall()
                for row in stuck:
                    if row[0]:
                        conn.execute(
                            "UPDATE message SET processing_status='needs_review' "
                            "WHERE id=? AND processing_status IN ('processing','queued','replied')",
                            (int(row[0]),),
                        )
            return {"messages_requeued": int(messages or 0), "outbox_unknown": len(stuck)}

        return self._write(run)

    def has_pending_work(self) -> bool:
        with self.read() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM message WHERE direction='in' "
                "  AND processing_status IN ('received','queued')) "
                "OR EXISTS(SELECT 1 FROM outbox WHERE state='pending')"
            ).fetchone()
        return bool(row[0])

    def rethrow_stuck_leases(self, *, lease_seconds: float = 0.0) -> int:
        """运行期租约过期的消息退回队列（进程还活着但处理器挂了）。"""
        now = self.clock()

        def run(conn: sqlite3.Connection) -> int:
            with _tx(conn):
                return conn.execute(
                    "UPDATE message SET processing_status='queued',lease_owner=NULL,"
                    "lease_expires_at=NULL WHERE processing_status='processing' "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
                    (now - lease_seconds,),
                ).rowcount

        return int(self._write(run) or 0)

    # ---- 回复与 outbox ----

    def save_reply(self, claimed: ClaimedMessage, body: str, *, attachments_json: str = "[]",
                   requested_kind: str = "text", effective_kind: str = "text",
                   client_request_id: str = "",
                   context: Mapping[str, Any] | None = None,
                   persona_version: int | None = None,
                   boundary_version: int | None = None) -> OutboxResult:
        """把候选回复和 outbox(pending) 与入站状态放进同一个事务（PRD §6.3）。

        ``context`` 记下这次注入了哪些事实/摘要/窗口（可解释），
        ``persona_version`` 记下这条回复用的是哪一版人格（历史回复不被后续修改改写）。
        """
        now = self.clock()
        request_id = client_request_id or uuid.uuid4().hex
        context_json = _json(dict(context or {}))

        def run(conn: sqlite3.Connection) -> OutboxResult:
            with _tx(conn):
                if boundary_version is not None:
                    current = conn.execute(
                        "SELECT IFNULL(boundary_version,0) FROM thread WHERE id=?",
                        (claimed.thread_id,),
                    ).fetchone()
                    if current is None or int(current[0]) != int(boundary_version):
                        raise BoundaryChanged("人物归属在生成期间变了，这条回复作废")
                existing = conn.execute(
                    "SELECT o.id,o.state,o.message_id FROM outbox o WHERE o.source_message_id=?",
                    (claimed.id,),
                ).fetchone()
                if existing is not None:
                    state = str(existing["state"])
                    if state in ("draft", "pending", "cancelled", "failed"):
                        # 重试只更新已有草稿，不新建任务（PRD §7.1 outbox 注释）。
                        conn.execute(
                            "UPDATE message SET body=?,attachments_json=?,context_json=?,"
                            "persona_version=?,processing_status='outbound' WHERE id=?",
                            (body, attachments_json, context_json, persona_version,
                             int(existing["message_id"])),
                        )
                        conn.execute(
                            "UPDATE outbox SET state='pending',requested_kind=?,effective_kind=?,"
                            "error_code=NULL,error_detail_safe=NULL,next_attempt_at=NULL,"
                            "lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                            (requested_kind, effective_kind, int(existing["id"])),
                        )
                        outbox_id = int(existing["id"])
                    else:
                        # 已发出/结果不明：不碰 outbox，但仍要把入站标成已回复。
                        outbox_id = int(existing["id"])
                    conn.execute(
                        "UPDATE message SET processing_status='replied',lease_owner=NULL,"
                        "lease_expires_at=NULL WHERE id=?",
                        (claimed.id,),
                    )
                    return OutboxResult(outbox_id=outbox_id, state=str(existing["state"]),
                                        created=False)

                message_id = int(
                    conn.execute(
                        """INSERT INTO message
                           (conversation_id,account_id,platform,direction,body,attachments_json,
                            storage_class,processing_status,reply_policy,occurred_at,ingested_at,
                            time_source,context_json,persona_version)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (claimed.conversation_id, claimed.account_id, claimed.platform, "out",
                         body, attachments_json, "full", "outbound", "never", now, now, "received",
                         context_json, persona_version),
                    ).lastrowid or 0
                )
                outbox_id = int(
                    conn.execute(
                        """INSERT INTO outbox
                           (message_id,source_message_id,client_request_id,idempotency_key,
                            requested_kind,effective_kind,state,created_at)
                           VALUES(?,?,?,?,?,?,'pending',?)""",
                        (message_id, claimed.id, request_id, uuid.uuid4().hex,
                         requested_kind, effective_kind, now),
                    ).lastrowid or 0
                )
                conn.execute(
                    "UPDATE message SET processing_status='replied',lease_owner=NULL,"
                    "lease_expires_at=NULL WHERE id=?",
                    (claimed.id,),
                )
            return OutboxResult(outbox_id=outbox_id, state="pending")

        return self._write(run)

    def enqueue_outbox(self, *, platform: str, kind: str, chat_id: str, text: str,
                       attachments_json: str = "[]", requested_kind: str = "text",
                       effective_kind: str = "text", client_request_id: str = "") -> OutboxResult:
        """界面手写发送：只允许发到已经存在的会话（先收到过对方消息）。"""
        if not text.strip() and requested_kind == "text":
            raise MailboxError("空消息不发", code="empty_body")
        request_id = client_request_id or uuid.uuid4().hex
        now = self.clock()

        def run(conn: sqlite3.Connection) -> OutboxResult:
            with _tx(conn):
                existing = conn.execute(
                    "SELECT id,state FROM outbox WHERE client_request_id=?", (request_id,)
                ).fetchone()
                if existing is not None:
                    return OutboxResult(outbox_id=int(existing["id"]),
                                        state=str(existing["state"]), created=False)
                row = conn.execute(
                    """SELECT c.id,c.account_id,c.thread_id FROM conversation c
                       JOIN account a ON a.id=c.account_id
                       WHERE a.platform=? AND c.kind=? AND c.chat_id=? AND a.active=1
                       ORDER BY c.id LIMIT 1""",
                    (platform, kind, chat_id),
                ).fetchone()
                if row is None:
                    raise MailboxError(
                        "该会话还没有收到过消息，无法确认收件人；请等对方先说话",
                        code="conversation_unknown",
                    )
                message_id = int(
                    conn.execute(
                        """INSERT INTO message
                           (conversation_id,account_id,platform,direction,body,attachments_json,
                            storage_class,processing_status,reply_policy,occurred_at,ingested_at,time_source)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (int(row["id"]), int(row["account_id"]), platform, "out", text,
                         attachments_json, "full", "outbound", "never", now, now, "received"),
                    ).lastrowid or 0
                )
                outbox_id = int(
                    conn.execute(
                        """INSERT INTO outbox
                           (message_id,source_message_id,client_request_id,idempotency_key,
                            requested_kind,effective_kind,state,created_at)
                           VALUES(?,NULL,?,?,?,?,'pending',?)""",
                        (message_id, request_id, uuid.uuid4().hex, requested_kind,
                         effective_kind, now),
                    ).lastrowid or 0
                )
            return OutboxResult(outbox_id=outbox_id, state="pending")

        return self._write(run)

    def claim_outbox(self, owner: str, *, lease_seconds: float = 120.0) -> OutboxTask | None:
        now, expires = self.clock(), self.clock() + lease_seconds

        def run(conn: sqlite3.Connection) -> OutboxTask | None:
            with _tx(conn):
                row = conn.execute(
                    "SELECT id FROM outbox WHERE state='pending' "
                    "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                outbox_id = int(row[0])
                conn.execute(
                    "UPDATE outbox SET state='sending',lease_owner=?,lease_expires_at=?,"
                    "attempt_count=attempt_count+1 WHERE id=?",
                    (owner, expires, outbox_id),
                )
                return self._outbox_task(conn, outbox_id)

        return self._write(run)

    def _outbox_task(self, conn: sqlite3.Connection, outbox_id: int) -> OutboxTask | None:
        row = conn.execute(
            """SELECT o.id,o.message_id,o.source_message_id,o.requested_kind,o.effective_kind,
                      o.attempt_count,m.account_id,m.platform,m.body,m.attachments_json,
                      c.kind AS conversation_kind,c.chat_id,
                      IFNULL(src.platform_msg_id,'') AS reply_to
               FROM outbox o
               JOIN message m ON m.id=o.message_id
               JOIN conversation c ON c.id=m.conversation_id
               LEFT JOIN message src ON src.id=o.source_message_id
               WHERE o.id=?""",
            (outbox_id,),
        ).fetchone()
        if row is None:
            return None
        return OutboxTask(
            outbox_id=int(row["id"]), message_id=int(row["message_id"]),
            source_message_id=int(row["source_message_id"]) if row["source_message_id"] else None,
            account_id=int(row["account_id"]), platform=str(row["platform"]),
            kind=str(row["conversation_kind"]), chat_id=str(row["chat_id"]),
            text=str(row["body"] or ""), attachments_json=str(row["attachments_json"]),
            requested_kind=str(row["requested_kind"]),
            effective_kind=str(row["effective_kind"] or row["requested_kind"]),
            attempt_count=int(row["attempt_count"]), reply_to=str(row["reply_to"] or ""),
        )

    def finish_outbox(self, outbox_id: int, state: str, *, platform_msg_id: str = "",
                      error_code: str | None = None, error_detail_safe: str | None = None,
                      effective_kind: str | None = None) -> None:
        if state not in ("sent", "degraded", "failed", "cancelled", "unknown"):
            raise MailboxError(f"未知发送状态：{state}", code="invalid_state")
        now = self.clock()

        def run(conn: sqlite3.Connection) -> None:
            with _tx(conn):
                row = conn.execute(
                    "SELECT source_message_id FROM outbox WHERE id=?", (outbox_id,)
                ).fetchone()
                conn.execute(
                    "UPDATE outbox SET state=?,platform_msg_id=?,error_code=?,"
                    "error_detail_safe=?,effective_kind=COALESCE(?,effective_kind),"
                    "sent_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE id=?",
                    (state, platform_msg_id or None, error_code, (error_detail_safe or "")[:500],
                     effective_kind, now if state in ("sent", "degraded") else None, outbox_id),
                )
                if state == "unknown" and row is not None and row[0]:
                    conn.execute(
                        "UPDATE message SET processing_status='needs_review' "
                        "WHERE id=? AND processing_status IN ('processing','queued','replied')",
                        (int(row[0]),),
                    )

        self._write(run)

    def get_outbox(self, outbox_id: int) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute(
                """SELECT o.*,m.platform,m.body,m.attachments_json,c.kind AS conversation_kind,
                          c.chat_id
                   FROM outbox o JOIN message m ON m.id=o.message_id
                   JOIN conversation c ON c.id=m.conversation_id WHERE o.id=?""",
                (outbox_id,),
            ).fetchone()
        return dict(row) if row else None

    def reconcile_outbox(self, outbox_id: int, action: str) -> dict[str, Any]:
        """结果不明的发送只能人工核对（PRD §6.4）：确认已发/忽略/重发一次。"""
        if action not in ("mark_sent", "ignore", "resend"):
            raise MailboxError(f"未知核对动作：{action}", code="invalid_action")
        now = self.clock()

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            with _tx(conn):
                row = conn.execute(
                    "SELECT * FROM outbox WHERE id=?", (outbox_id,)
                ).fetchone()
                if row is None:
                    raise MailboxError("发送任务不存在", code="not_found")
                state = str(row["state"])
                if state not in ("unknown", "failed"):
                    raise MailboxError("只有结果不明或明确失败的任务才需要人工核对",
                                       code="not_reconcilable")
                if action == "mark_sent":
                    conn.execute(
                        "UPDATE outbox SET state='sent',sent_at=? WHERE id=?", (now, outbox_id)
                    )
                    return {"outbox_id": outbox_id, "state": "sent"}
                if action == "ignore":
                    conn.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (outbox_id,))
                    return {"outbox_id": outbox_id, "state": "cancelled"}
                # resend：新建一条出站消息和任务，绝不复用旧幂等键。
                source = conn.execute(
                    """SELECT m.conversation_id,m.account_id,m.platform,m.body,m.attachments_json,
                              c.kind AS conversation_kind
                       FROM message m JOIN conversation c ON c.id=m.conversation_id
                       WHERE m.id=?""", (int(row["message_id"]),),
                ).fetchone()
                message_id = int(
                    conn.execute(
                        """INSERT INTO message
                           (conversation_id,account_id,platform,direction,body,attachments_json,
                            storage_class,processing_status,reply_policy,occurred_at,ingested_at,time_source)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (int(source["conversation_id"]), int(source["account_id"]),
                         str(source["platform"]), "out", source["body"],
                         source["attachments_json"] or "[]", "full", "outbound", "never",
                         now, now, "received"),
                    ).lastrowid or 0
                )
                # 人工重发是独立任务：source_message_id 留空，
                # 一条入站仍然只有一个自动回复 outbox（ux_outbox_auto_reply）。
                new_id = int(
                    conn.execute(
                        """INSERT INTO outbox
                           (message_id,source_message_id,client_request_id,idempotency_key,
                            requested_kind,effective_kind,state,created_at)
                           VALUES(?,NULL,?,?,?,?, 'pending',?)""",
                        (message_id, f"resend-{uuid.uuid4().hex}", uuid.uuid4().hex,
                         row["requested_kind"], row["effective_kind"], now),
                    ).lastrowid or 0
                )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('outbox_resend','outbox',?,?,?)",
                    (str(new_id), _json({"resent_from": outbox_id,
                                         "source_message_id": row["source_message_id"]}), now),
                )
            return {"outbox_id": new_id, "state": "pending", "resent_from": outbox_id}

        return self._write(run)

    # ---- 读：收件箱 / 会话 / 检索 ----

    _MESSAGE_SELECT = """
        SELECT m.id,m.direction,m.platform,m.platform_msg_id,m.body,m.attachments_json,
               m.storage_class,m.processing_status,m.skip_reason,m.error_code,m.imported,
               m.reply_policy,m.occurred_at,m.ingested_at,m.time_source,
               c.id AS conversation_id,c.thread_id,c.kind AS conversation_kind,c.chat_id,
               t.scope,IFNULL(p.display_name,'') AS person_name,
               IFNULL(i.display_name,'') AS sender_name,
               IFNULL(i.platform_uid,'') AS sender_uid
        FROM message m
        JOIN conversation c ON c.id=m.conversation_id
        JOIN thread t ON t.id=c.thread_id
        LEFT JOIN person p ON p.id=t.person_id
        LEFT JOIN identity i ON i.id=m.sender_identity_id
    """

    def list_messages(self, *, limit: int = 50, cursor: str | None = None,
                      platform: str | None = None, conversation_id: int | None = None,
                      thread_id: int | None = None, direction: str | None = None,
                      status: str | None = None, kind: str | None = None,
                      person_id: int | None = None, query: str | None = None) -> MessagePage:
        limit = max(1, min(int(limit), 200))
        clauses: list[str] = []
        params: list[Any] = []
        slow = False
        if platform:
            clauses.append("m.platform=?")
            params.append(platform)
        if conversation_id:
            clauses.append("m.conversation_id=?")
            params.append(int(conversation_id))
        if thread_id:
            clauses.append("c.thread_id=?")
            params.append(int(thread_id))
        if person_id:
            clauses.append("p.id=?")
            params.append(int(person_id))
        if direction:
            clauses.append("m.direction=?")
            params.append(direction)
        if status:
            clauses.append("m.processing_status=?")
            params.append(status)
        if kind:
            clauses.append("c.kind=?")
            params.append(kind)
        if cursor:
            ingested_at, message_id = _decode_cursor(cursor)
            clauses.append("(m.ingested_at<? OR (m.ingested_at=? AND m.id<?))")
            params.extend([ingested_at, ingested_at, message_id])
        if query is not None and query.strip():
            term = query.strip()
            if len(term) >= 3:
                clauses.append("m.id IN (SELECT rowid FROM message_fts WHERE message_fts MATCH ?)")
                params.append('"' + term.replace('"', '""') + '"')
            else:
                # 1～2 个字符 trigram 查不到，退回参数化子串匹配：
                # instr 不解释 %/_，也不会扩大语义（AC-11）。
                clauses.append("instr(m.body, ?) > 0")
                params.append(term)
                slow = True
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            self._MESSAGE_SELECT
            + where
            + " ORDER BY m.ingested_at DESC, m.id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with self.read() as conn:
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        page = [self._decorate(row) for row in rows[:limit]]
        next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
        return MessagePage(items=page, next_cursor=next_cursor, slow=slow)

    def get_message_detail(self, message_id: int) -> dict[str, Any] | None:
        with self.read() as conn:
            row = conn.execute(
                self._MESSAGE_SELECT + " WHERE m.id=?", (message_id,)
            ).fetchone()
            if row is None:
                return None
            item = self._decorate(dict(row))
            outbox = conn.execute(
                "SELECT id,state,requested_kind,effective_kind,error_code,error_detail_safe "
                "FROM outbox WHERE message_id=? OR source_message_id=? ORDER BY id DESC LIMIT 1",
                (message_id, message_id),
            ).fetchone()
        item["outbox"] = dict(outbox) if outbox else None
        return item

    def recent_senders(self, *, limit: int = 24) -> list[dict[str, Any]]:
        """各平台最近发过消息的人，给白名单界面当候选，省得手抄 ID。"""
        with self.read() as conn:
            rows = conn.execute(
                """SELECT m.platform, i.platform_uid, i.id_kind,
                          IFNULL(i.display_name,'') AS name,
                          COUNT(*) AS hits, MAX(m.ingested_at) AS last_at
                   FROM message m JOIN identity i ON i.id=m.sender_identity_id
                   WHERE m.direction='in'
                   GROUP BY m.platform, i.platform_uid
                   ORDER BY last_at DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _decorate(row: dict[str, Any]) -> dict[str, Any]:
        try:
            row["attachments"] = json.loads(row.get("attachments_json") or "[]")
        except json.JSONDecodeError:
            row["attachments"] = []
        row.pop("attachments_json", None)
        row["imported"] = bool(row.get("imported"))
        return row

    def list_conversations(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                """SELECT c.id,c.kind,c.chat_id,c.account_id,t.scope,t.id AS thread_id,
                          IFNULL(p.display_name,c.chat_id) AS title,
                          COUNT(m.id) AS message_count,
                          MAX(m.ingested_at) AS last_at,
                          SUM(CASE WHEN m.direction='in' AND m.processing_status='needs_review'
                                   THEN 1 ELSE 0 END) AS review_count
                   FROM conversation c
                   JOIN thread t ON t.id=c.thread_id
                   LEFT JOIN person p ON p.id=t.person_id
                   LEFT JOIN message m ON m.conversation_id=c.id
                   GROUP BY c.id ORDER BY last_at DESC NULLS LAST LIMIT ?""",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_thread_messages(self, thread_id: int, *, limit: int = 24,
                               before_id: int | None = None) -> list[dict[str, Any]]:
        """本 thread 最近的完整消息；``before_id`` 用于排除当前正在处理的那条。"""
        clause = "" if before_id is None else " AND m.id<?"
        args: list[Any] = [int(thread_id)]
        if before_id is not None:
            args.append(int(before_id))
        args.append(max(1, int(limit)))
        with self.read() as conn:
            rows = conn.execute(
                """SELECT m.id,m.direction,m.platform,m.body,m.occurred_at,m.ingested_at,
                          IFNULL(i.display_name,'') AS sender_name
                   FROM message m
                   JOIN conversation c ON c.id=m.conversation_id
                   LEFT JOIN identity i ON i.id=m.sender_identity_id
                   WHERE c.thread_id=? AND m.storage_class='full'""" + clause + """
                   ORDER BY m.ingested_at DESC, m.id DESC LIMIT ?""",
                args,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def pending_counts(self) -> dict[str, int]:
        """队列深度：待处理入站 / 处理中 / 待发送。界面和诊断都读这个。"""
        with self.read() as conn:
            inbound = conn.execute(
                "SELECT COUNT(*) FROM message WHERE direction='in' "
                "AND processing_status IN ('received','queued')"
            ).fetchone()[0]
            processing = conn.execute(
                "SELECT COUNT(*) FROM message WHERE processing_status='processing'"
            ).fetchone()[0]
            outbox = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','sending')"
            ).fetchone()[0]
        return {"inbound": int(inbound), "processing": int(processing), "outbox": int(outbox)}

    def schema_version(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migration").fetchone()
        return int(row[0] or 0)

    def stats(self) -> dict[str, Any]:
        with self.read() as conn:
            counts = {
                str(row[0]): int(row[1])
                for row in conn.execute(
                    "SELECT processing_status,COUNT(*) FROM message WHERE direction='in' "
                    "GROUP BY processing_status"
                )
            }
            outbox = {
                str(row[0]): int(row[1])
                for row in conn.execute("SELECT state,COUNT(*) FROM outbox GROUP BY state")
            }
            conversations = int(
                conn.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
            )
        return {"inbound": counts, "outbox": outbox, "conversations": conversations}

    # ---- 正文删除 ----

    def purge_message_body(self, message_id: int) -> None:
        """删正文同时清 FTS（触发器负责），附件一并清掉（FR-2.9）。"""

        def run(conn: sqlite3.Connection) -> None:
            with _tx(conn):
                conn.execute(
                    "UPDATE message SET storage_class='metadata_only',body=NULL,"
                    "attachments_json='[]' WHERE id=?", (message_id,)
                )
                conn.execute("UPDATE memory_fact SET status='deleted' WHERE source_message_id=?",
                             (message_id,))

        self._write(run)

    # ---- 检查点 ----

    def load_checkpoint(self, account_id: int, stream: str) -> dict[str, Any]:
        with self.read() as conn:
            row = conn.execute(
                "SELECT cursor,state_json,primed,updated_at FROM adapter_checkpoint "
                "WHERE account_id=? AND stream=?", (int(account_id), stream),
            ).fetchone()
        if row is None:
            return {"cursor": None, "state": {}, "primed": False, "updated_at": None}
        try:
            state = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            state = {}
        return {"cursor": row["cursor"], "state": state,
                "primed": bool(row["primed"]), "updated_at": row["updated_at"]}

    def save_checkpoint(self, account_id: int, stream: str, *, cursor: str | None = None,
                        state: Mapping[str, Any] | None = None,
                        primed: bool | None = None) -> None:
        if not stream:
            raise MailboxError("检查点必须带 stream 名", code="invalid_stream")
        if state is not None:
            _reject_secret_keys(state, where="检查点")
        now = self.clock()

        def run(conn: sqlite3.Connection) -> None:
            with _tx(conn):
                current = conn.execute(
                    "SELECT cursor,state_json,primed FROM adapter_checkpoint "
                    "WHERE account_id=? AND stream=?", (int(account_id), stream),
                ).fetchone()
                new_cursor = cursor if cursor is not None else (current["cursor"] if current else None)
                if state is not None:
                    new_state = _json(dict(state))
                else:
                    new_state = str(current["state_json"]) if current else "{}"
                new_primed = int(primed) if primed is not None else (
                    int(current["primed"]) if current else 0
                )
                conn.execute(
                    """INSERT INTO adapter_checkpoint
                         (account_id,stream,cursor,state_json,primed,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(account_id,stream) DO UPDATE SET
                         cursor=excluded.cursor,state_json=excluded.state_json,
                         primed=excluded.primed,updated_at=excluded.updated_at""",
                    (int(account_id), stream, new_cursor, new_state, new_primed, now),
                )

        self._write(run)

    # ---- 审计 ----

    def audit(self, event_type: str, entity_type: str, entity_id: str,
              payload: Mapping[str, Any] | None = None) -> None:
        data = dict(payload or {})
        _reject_secret_keys(data, where="审计")
        encoded = _json(data)
        if len(encoded) > _MAX_AUDIT_PAYLOAD:
            raise MailboxError("审计负载过大", code="audit_too_large")
        now = self.clock()

        def run(conn: sqlite3.Connection) -> None:
            with _tx(conn):
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (event_type, entity_type, entity_id, encoded, now),
                )

        self._write(run)

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_event ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- 备份 ----

    def backup(self, dest: str | Path | None = None) -> Path:
        if dest is None:
            dest = self.backup_dir / f"mailbox-{int(self.clock())}.db"
        try:
            return db.backup_database(self.path, dest)
        except db.DatabaseError as exc:
            raise MailboxError(str(exc), code=exc.code) from exc

    # ---- 旧数据导入 ----

    def import_legacy(self, sessions: Mapping[str, Sequence[Mapping[str, Any]]], *,
                      source_hash: str, source_name: str) -> dict[str, Any]:
        """把旧 ``data/sessions.json`` 一次性导入（PRD §7.5）。

        - 每个 turn 拆成入站/出站两条历史消息；
        - ``dedup_key`` = import_batch + source_hash + turn_index + direction，确定性，
          同一个文件导入两次行数不变；
        - ``imported=1`` / ``time_source=import`` / ``reply_policy=never``，
          导入时间绝不冒充平台时间，也不会自动回复；
        - 整批一个事务，失败全回滚；源文件只读。
        """
        batch = f"{source_name}:{source_hash[:16]}"
        now = self.clock()
        report = {"imported": 0, "skipped": 0, "turns": 0, "sessions": 0,
                  "source_hash": source_hash, "batch": batch}

        def run(conn: sqlite3.Connection) -> dict[str, Any]:
            with _tx(conn):
                for session_key, turns in sessions.items():
                    parsed = _parse_session_key(session_key)
                    if parsed is None or not isinstance(turns, list):
                        report["skipped"] += 1
                        continue
                    platform, kind, chat_id = parsed
                    account = conn.execute(
                        "SELECT id FROM account WHERE platform=? AND active=1 ORDER BY id LIMIT 1",
                        (platform,),
                    ).fetchone()
                    if account is None:
                        report["skipped"] += len(turns)
                        continue
                    report["sessions"] += 1
                    for index, turn in enumerate(turns):
                        if not isinstance(turn, Mapping):
                            report["skipped"] += 1
                            continue
                        report["turns"] += 1
                        for direction, field_name in (("in", "user"), ("out", "assistant")):
                            text = str(turn.get(field_name) or "").strip()
                            if not text:
                                report["skipped"] += 1
                                continue
                            key = hashlib.sha1(
                                f"{batch}\x00{index}\x00{direction}".encode("utf-8")
                            ).hexdigest()
                            incoming = Incoming(
                                account_id=int(account["id"]), platform=platform, kind=kind,
                                chat_id=chat_id, sender_id=chat_id,
                                sender_name=chat_id, id_kind="provisional",
                                text=text, dedup_key=f"import:{key}",
                                occurred_at=None, time_source="import",
                                reply_policy="never", status="skipped" if direction == "in" else "outbound",
                                skip_reason="imported" if direction == "in" else None,
                                imported=True,
                            )
                            if self._insert_import(conn, incoming, direction=direction, now=now):
                                report["imported"] += 1
                            else:
                                report["skipped"] += 1
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,created_at)"
                    " VALUES('legacy_import','database',NULL,?,?)",
                    (_json({"batch": batch, "imported": report["imported"],
                            "skipped": report["skipped"]}), now),
                )
            return report

        return self._write(run, timeout=120.0)

    def _insert_import(self, conn: sqlite3.Connection, incoming: Incoming, *,
                       direction: str, now: float) -> bool:
        identity_id, person_id = self._resolve_identity(conn, incoming, now=now)
        thread_id, conversation_id = self._resolve_thread(
            conn, incoming, person_id=person_id, now=now
        )
        try:
            conn.execute(
                """INSERT INTO message
                   (conversation_id,account_id,platform,dedup_key,direction,sender_identity_id,
                    body,attachments_json,storage_class,processing_status,skip_reason,imported,
                    reply_policy,occurred_at,ingested_at,time_source)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (conversation_id, incoming.account_id, incoming.platform, incoming.dedup_key,
                 direction, identity_id if direction == "in" else None, incoming.text, "[]",
                 "full", "outbound" if direction == "out" else "skipped",
                 "imported" if direction == "in" else None, 1, "never", None, now, "import"),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    # ---- 导出（显式、脱敏） ----

    def export_thread(self, thread_id: int, *, limit: int = 500) -> dict[str, Any]:
        items = list(reversed(self.recent_thread_messages(thread_id, limit=limit)))
        return {"thread_id": int(thread_id), "messages": [
            {"id": item["id"], "direction": item["direction"], "platform": item["platform"],
             "body": item["body"], "occurred_at": item["occurred_at"],
             "ingested_at": item["ingested_at"], "sender": item["sender_name"]}
            for item in items
        ]}


def _parse_session_key(key: str) -> tuple[str, str, str] | None:
    """只按前两个冒号拆；评论键内部还有冒号，必须保留（PRD §7.5）。"""
    parts = key.split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
        return None
    platform, kind, chat_id = parts
    if platform not in ("qq", "wechat", "bilibili", "douyin"):
        return None
    if kind not in SCOPES:
        # 旧网关还有 room（直播间）；当前没有平台在用，按评论边界隔离而丢弃更安全。
        return None
    return platform, kind, chat_id
