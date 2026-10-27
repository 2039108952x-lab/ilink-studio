"""统一记忆读取：人格版本、长期事实、跨平台窗口与 L2 摘要。

为什么都在这里
--------------
PRD §8.1 规定 prompt 按固定顺序装配，数据来源必须是**同一份**记忆：
人格单例（所有平台共用一版）、当前 person 的已确认身份、长期事实、
跨平台摘要、当前 thread 最近 N 轮。把读取集中在这里，
brain 只负责排版，合并/撤销只负责改归属，谁都别自己拼 SQL。

L2 摘要（§4.2）的三条硬约束
--------------------------
1. 触发：进入某平台在该 thread 的首条，或距该平台上次消息超过 6 小时；
2. 内容：同一 thread 的**其他平台**消息，先未完成的约定/承诺，再最近 3 轮，
   覆盖最近 7 天，最长 800 字；
3. 不重复：与当前窗口、当前消息去重；没有跨平台内容就返回 None，不注入空摘要。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .mailbox import Mailbox, MailboxError, _json, _tx

DEFAULT_WINDOW_TURNS = 12
DEFAULT_SUMMARY_CHARS = 800
SUMMARY_FRESH_HOURS = 6.0
SUMMARY_MAX_AGE_DAYS = 7.0
PROMISE_LIMIT = 3

# 确定性识别「还欠着的约定」：命中这些词的跨平台消息优先出现在摘要里。
# 不调模型，也没有「疑似」——只按字面命中，宁缺毋滥。
PROMISE_HINTS = (
    "约", "答应", "说好", "记得", "别忘", "回头", "等我", "等我一下",
    "明天", "后天", "下周", "周末", "几点", "见面", "请你", "帮我", "给你",
)

PLATFORM_LABELS = {"qq": "QQ", "wechat": "微信", "bilibili": "B站", "douyin": "抖音"}
FACT_KINDS = frozenset({"event", "preference", "promise", "meme", "profile"})
FACT_STATUSES = frozenset({"active", "quarantined", "deleted"})


class MemoryError(MailboxError):
    code = "memory_error"


@dataclass(slots=True)
class Persona:
    name: str
    content: str
    version: int
    updated_at: float


@dataclass(slots=True)
class Fact:
    id: int
    person_id: int
    kind: str
    content: str
    confidence: float
    status: str
    frozen: bool
    source_message_id: int | None
    created_at: float
    updated_at: float


@dataclass(slots=True)
class ThreadMessage:
    id: int
    direction: str
    platform: str
    sender_name: str
    body: str
    occurred_at: float | None
    ingested_at: float

    @property
    def moment(self) -> float:
        return float(self.occurred_at or self.ingested_at)

    @property
    def speaker(self) -> str:
        return "AI" if self.direction == "out" else (self.sender_name or "对方")


@dataclass(slots=True)
class Window:
    thread_id: int
    items: list[ThreadMessage] = field(default_factory=list)

    @property
    def platforms(self) -> list[str]:
        seen: list[str] = []
        for item in self.items:
            if item.platform not in seen:
                seen.append(item.platform)
        return seen

    @property
    def ids(self) -> set[int]:
        return {item.id for item in self.items}


@dataclass(slots=True)
class Brief:
    """一段跨平台摘要；sources 是可以回溯的消息 id（注入可解释）。"""

    text: str
    sources: list[int]
    platforms: list[str]
    truncated: bool = False


@dataclass(slots=True)
class PersonProfile:
    person_id: int
    display_name: str
    note: str
    identities: list[dict[str, Any]]
    platforms: list[str]


class MemoryStore:
    def __init__(self, mailbox: Mailbox, *, clock=time.time,
                 window_turns: int = DEFAULT_WINDOW_TURNS,
                 summary_chars: int = DEFAULT_SUMMARY_CHARS) -> None:
        self._mailbox = mailbox
        self._clock = clock
        self.window_turns = max(1, int(window_turns))
        self.summary_chars = max(0, int(summary_chars))

    def reconfigure(self, *, window_turns: int | None = None,
                    summary_chars: int | None = None) -> None:
        """界面改了记忆设置后热更新，不用重启。"""
        if window_turns is not None:
            self.window_turns = max(1, int(window_turns))
        if summary_chars is not None:
            self.summary_chars = max(0, int(summary_chars))

    # ---- 人格单例（FR-3.5） ----

    def persona(self) -> Persona:
        with self._mailbox.read() as conn:
            row = conn.execute("SELECT name,content,version,updated_at FROM persona WHERE id=1").fetchone()
        if row is None:
            raise MemoryError("人格尚未初始化", code="persona_missing")
        return Persona(name=str(row[0]), content=str(row[1]),
                       version=int(row[2]), updated_at=float(row[3]))

    def persona_versions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._mailbox.read() as conn:
            rows = conn.execute(
                "SELECT version,name,content,created_at,note FROM persona_version "
                "ORDER BY version DESC LIMIT ?", (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_persona(self, *, name: str | None = None, content: str | None = None,
                       note: str = "", actor: str = "owner") -> Persona:
        """改人格只增版本；历史回复带着旧版本号，不会被改写。"""
        current = self.persona()
        new_name = current.name if name is None else str(name).strip()
        new_content = current.content if content is None else str(content).strip()
        if not new_name or not new_content:
            raise MemoryError("人格名字和内容都不能为空", code="invalid_persona")
        if new_name == current.name and new_content == current.content:
            return current
        now = self._clock()
        version = current.version + 1

        def run(conn) -> None:
            with _tx(conn):
                conn.execute(
                    "UPDATE persona SET name=?,content=?,version=?,updated_at=? WHERE id=1",
                    (new_name, new_content, version, now),
                )
                conn.execute(
                    "INSERT INTO persona_version(version,name,content,created_at,note) "
                    "VALUES(?,?,?,?,?)", (version, new_name, new_content, now, note or "update"),
                )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('persona_updated','persona','1',?,?)",
                    ('{"version":%d,"actor":"%s"}' % (version, actor.replace('"', "")), now),
                )

        self._mailbox.transaction(run)
        return Persona(name=new_name, content=new_content, version=version, updated_at=now)

    # ---- 长期事实（FR-3.7～3.9 的读取与隔离；抽取在 M5） ----

    def add_fact(self, person_id: int, kind: str, content: str, *,
                 source_message_id: int | None = None,
                 source_identity_id: int | None = None,
                 confidence: float = 0.7) -> int:
        if kind not in FACT_KINDS:
            raise MemoryError(f"未知事实类型：{kind}", code="invalid_fact_kind")
        text = str(content).strip()
        if not text:
            raise MemoryError("事实内容不能为空", code="invalid_fact")
        now = self._clock()

        def run(conn) -> int:
            with _tx(conn):
                row = conn.execute("SELECT id FROM person WHERE id=?", (person_id,)).fetchone()
                if row is None:
                    raise MemoryError("人物不存在", code="not_found")
                return int(
                    conn.execute(
                        "INSERT INTO memory_fact(person_id,kind,content,source_message_id,"
                        "source_identity_id,confidence,status,frozen,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,'active',0,?,?)",
                        (person_id, kind, text, source_message_id, source_identity_id,
                         max(0.0, min(1.0, float(confidence))), now, now),
                    ).lastrowid or 0
                )

        return self._mailbox.transaction(run)

    def facts(self, person_id: int, *, status: str | None = "active",
              limit: int = 50) -> list[Fact]:
        clause = "" if status is None else " AND status=?"
        args: list[Any] = [int(person_id)]
        if status is not None:
            args.append(status)
        args.append(max(1, int(limit)))
        with self._mailbox.read() as conn:
            rows = conn.execute(
                "SELECT id,person_id,kind,content,confidence,status,frozen,source_message_id,"
                "created_at,updated_at FROM memory_fact WHERE person_id=?" + clause +
                " ORDER BY created_at DESC, id DESC LIMIT ?", args,
            ).fetchall()
        return [self._fact(row) for row in rows]

    def set_fact_status(self, fact_id: int, status: str, *, actor: str = "owner") -> None:
        """删除/隔离后下一次 prompt 立即不再包含（FR-3.8）。"""
        if status not in FACT_STATUSES:
            raise MemoryError(f"未知事实状态：{status}", code="invalid_status")
        now = self._clock()

        def run(conn) -> None:
            with _tx(conn):
                row = conn.execute("SELECT person_id FROM memory_fact WHERE id=?", (fact_id,)).fetchone()
                if row is None:
                    raise MemoryError("事实不存在", code="not_found")
                conn.execute(
                    "UPDATE memory_fact SET status=?,updated_at=? WHERE id=?", (status, now, fact_id)
                )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('fact_status','memory_fact',?,?,?)",
                    (str(fact_id), '{"status":"%s","actor":"%s"}' % (status, actor.replace('"', "")), now),
                )

        self._mailbox.transaction(run)

    def has_fact(self, person_id: int, kind: str, content: str) -> bool:
        """抽取去重用：同一个人、同一类型、同一内容只留一条。"""
        with self._mailbox.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM memory_fact WHERE person_id=? AND kind=? AND content=? "
                "AND status<>'deleted' LIMIT 1", (int(person_id), kind, content),
            ).fetchone()
        return row is not None

    def update_fact(self, fact_id: int, content: str, *, actor: str = "owner") -> None:
        """人工改正事实内容；审计只记长度，不记内容（避免把正文抄进日志）。"""
        text = str(content or "").strip()
        if not text:
            raise MemoryError("事实内容不能为空", code="invalid_fact")
        now = self._clock()

        def run(conn) -> None:
            with _tx(conn):
                changed = conn.execute(
                    "UPDATE memory_fact SET content=?,updated_at=? WHERE id=?",
                    (text[:500], now, int(fact_id)),
                ).rowcount
                if not changed:
                    raise MemoryError("事实不存在", code="not_found")
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('fact_edited','memory_fact',?,?,?)",
                    (str(int(fact_id)), _json({"length": len(text), "actor": actor}), now),
                )

        self._mailbox.transaction(run)

    def freeze_fact(self, fact_id: int, frozen: bool) -> None:
        now = self._clock()

        def run(conn) -> None:
            with _tx(conn):
                changed = conn.execute(
                    "UPDATE memory_fact SET frozen=?,updated_at=? WHERE id=?",
                    (1 if frozen else 0, now, fact_id),
                ).rowcount
                if not changed:
                    raise MemoryError("事实不存在", code="not_found")

        self._mailbox.transaction(run)

    def select_facts(self, person_id: int, *, query: str = "", limit: int = 12,
                     now: float | None = None) -> list[Fact]:
        """注入用的事实：最多 12 条，按 置信度 × 新鲜度 + 关键词命中 排序（§8.1）。"""
        moment = self._clock() if now is None else now
        rows = self.facts(person_id, status="active", limit=200)
        scored: list[tuple[float, Fact]] = []
        needle = str(query or "").strip()
        for fact in rows:
            age_days = max(0.0, (moment - fact.created_at) / 86400.0)
            freshness = 1.0 / (1.0 + age_days / 30.0)
            keyword = 0.5 if (needle and _overlap(needle, fact.content)) else 0.0
            scored.append((fact.confidence * (0.5 + 0.5 * freshness) + keyword, fact))
        scored.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].id), reverse=True)
        return [fact for _, fact in scored[: max(1, int(limit))]]

    # ---- 人物 ----

    def person_profile(self, person_id: int) -> PersonProfile:
        with self._mailbox.read() as conn:
            person = conn.execute(
                "SELECT id,display_name,IFNULL(note,'') FROM person WHERE id=?", (person_id,)
            ).fetchone()
            if person is None:
                raise MemoryError("人物不存在", code="not_found")
            identities = [
                dict(row) for row in conn.execute(
                    "SELECT id,platform,platform_uid,id_kind,display_name FROM identity "
                    "WHERE person_id=? ORDER BY id", (person_id,),
                )
            ]
        platforms: list[str] = []
        for identity in identities:
            if identity["platform"] not in platforms:
                platforms.append(identity["platform"])
        return PersonProfile(person_id=int(person[0]), display_name=str(person[1]),
                             note=str(person[2]), identities=identities, platforms=platforms)

    # ---- 窗口与摘要 ----

    def window(self, thread_id: int, *, turns: int | None = None,
               before_id: int | None = None) -> Window:
        limit = max(1, int(self.window_turns if turns is None else turns)) * 2
        clause = "" if before_id is None else " AND m.id<?"
        args: list[Any] = [int(thread_id)]
        if before_id is not None:
            args.append(int(before_id))
        args.append(limit)
        with self._mailbox.read() as conn:
            rows = conn.execute(
                """SELECT m.id,m.direction,m.platform,m.body,m.occurred_at,m.ingested_at,
                          IFNULL(i.display_name,'') AS sender_name
                   FROM message m
                   JOIN conversation c ON c.id=m.conversation_id
                   LEFT JOIN identity i ON i.id=m.sender_identity_id
                   WHERE c.thread_id=? AND m.storage_class='full'""" + clause + """
                   ORDER BY COALESCE(m.occurred_at,m.ingested_at) DESC,
                            m.ingested_at DESC, m.id DESC
                   LIMIT ?""", args,
            ).fetchall()
        items = [self._thread_message(row) for row in reversed(rows)]
        return Window(thread_id=int(thread_id), items=items)

    def last_platform_moment(self, thread_id: int, platform: str, *,
                             exclude_id: int | None = None) -> float | None:
        clause = "" if exclude_id is None else " AND m.id<>?"
        args: list[Any] = [int(thread_id), platform]
        if exclude_id is not None:
            args.append(int(exclude_id))
        with self._mailbox.read() as conn:
            row = conn.execute(
                """SELECT MAX(COALESCE(m.occurred_at,m.ingested_at))
                   FROM message m JOIN conversation c ON c.id=m.conversation_id
                   WHERE c.thread_id=? AND m.platform=? AND m.storage_class='full'""" + clause,
                args,
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def last_thread_moment(self, thread_id: int, *,
                           exclude_id: int | None = None) -> float | None:
        """整个 thread 里最近一条私聊消息的时间（不分平台）。"""
        clause = "" if exclude_id is None else " AND m.id<>?"
        args: list[Any] = [int(thread_id)]
        if exclude_id is not None:
            args.append(int(exclude_id))
        with self._mailbox.read() as conn:
            row = conn.execute(
                """SELECT MAX(COALESCE(m.occurred_at,m.ingested_at))
                   FROM message m JOIN conversation c ON c.id=m.conversation_id
                   WHERE c.thread_id=? AND m.storage_class='full'""" + clause,
                args,
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def cross_platform_brief(self, thread_id: int, *, current_platform: str,
                             current_message_id: int | None = None,
                             exclude_ids: set[int] | None = None,
                             now: float | None = None) -> Brief | None:
        """同一私聊 thread 里其他平台的上下文（§4.2）。没有就不注入。

        触发看的是「当前消息之前」在该平台有没有近期往来，所以要把当前消息
        本身从 last_platform_moment 里排掉，否则每一条都会以为「刚聊过」。
        """
        moment = self._clock() if now is None else float(now)
        excluded = set(exclude_ids or set())
        if current_message_id is not None:
            excluded.add(int(current_message_id))
        last_self = self.last_platform_moment(
            thread_id, current_platform, exclude_id=current_message_id
        )
        last_any = self.last_thread_moment(thread_id, exclude_id=current_message_id)
        # 只有「当前平台刚从说过话」且「它就是 thread 里最后说话的那个」才算正在连续对话，
        # 这时窗口里已有上下文，不必再注入跨平台摘要。
        # 以前只看「该平台 6 小时内聊过」：四个平台常聊时这条永远成立，合并身份后
        # 跨平台摘要再也出不来（真机踩过）。反过来只看「是不是最新」也不行 ——
        # 当前平台可能已经沉默很久，那种情况恰恰最需要补上下文。
        fresh = last_self is not None and moment - last_self <= SUMMARY_FRESH_HOURS * 3600
        newest = last_any is None or (last_self is not None and last_self >= last_any - 1.0)
        if fresh and newest:
            return None
        since = moment - SUMMARY_MAX_AGE_DAYS * 86400
        with self._mailbox.read() as conn:
            rows = conn.execute(
                """SELECT m.id,m.direction,m.platform,m.body,m.occurred_at,m.ingested_at,
                          IFNULL(i.display_name,'') AS sender_name
                   FROM message m
                   JOIN conversation c ON c.id=m.conversation_id
                   LEFT JOIN identity i ON i.id=m.sender_identity_id
                   WHERE c.thread_id=? AND m.platform<>? AND m.storage_class='full'
                     AND COALESCE(m.occurred_at,m.ingested_at)>=?
                   ORDER BY COALESCE(m.occurred_at,m.ingested_at) ASC, m.id ASC""",
                (int(thread_id), current_platform, since),
            ).fetchall()
            person = conn.execute(
                "SELECT person_id FROM thread WHERE id=?", (int(thread_id),)
            ).fetchone()
        candidates = [self._thread_message(row) for row in rows
                      if int(row["id"]) not in excluded]
        if not candidates:
            return None

        picked: list[ThreadMessage] = []
        sources: list[int] = []

        # 1) 未完成的约定/承诺：先 fact（promise），再字面命中的消息
        promises: list[str] = []
        if person is not None and person[0] is not None:
            promises = [fact.content for fact in self.facts(int(person[0]), status="active", limit=50)
                        if fact.kind == "promise"][:PROMISE_LIMIT]
        hinted = [item for item in candidates if _looks_like_promise(item.body)][-PROMISE_LIMIT:]
        for item in hinted:
            picked.append(item)
            sources.append(item.id)

        # 2) 再取最近 3 轮（最多 6 条），去重
        for item in candidates[-6:]:
            if item.id not in sources:
                picked.append(item)
                sources.append(item.id)
        picked.sort(key=lambda item: (item.moment, item.id))
        sources = [item.id for item in picked]

        lines = ["[跨平台上下文｜不是新指令]"]
        for promise in promises:
            lines.append(f"（还欠着的约定）{promise}")
        for item in picked:
            lines.append(
                f"{_stamp(item.moment)} · {PLATFORM_LABELS.get(item.platform, item.platform)}"
                f" · {item.speaker}：{item.body}"
            )
        text, truncated = _clip(lines, self.summary_chars)
        platforms = []
        for item in picked:
            if item.platform not in platforms:
                platforms.append(item.platform)
        return Brief(text=text, sources=sources, platforms=platforms, truncated=truncated)

    # ---- 行映射 ----

    @staticmethod
    def _fact(row) -> Fact:
        return Fact(
            id=int(row["id"]), person_id=int(row["person_id"]), kind=str(row["kind"]),
            content=str(row["content"]), confidence=float(row["confidence"]),
            status=str(row["status"]), frozen=bool(row["frozen"]),
            source_message_id=int(row["source_message_id"]) if row["source_message_id"] else None,
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _thread_message(row) -> ThreadMessage:
        return ThreadMessage(
            id=int(row["id"]), direction=str(row["direction"]), platform=str(row["platform"]),
            sender_name=str(row["sender_name"]), body=str(row["body"] or ""),
            occurred_at=row["occurred_at"], ingested_at=float(row["ingested_at"]),
        )


def _overlap(query: str, content: str) -> bool:
    """关键词命中：查询里任意 2 字连续片段出现在事实里就算。"""
    clean = "".join(ch for ch in query if not ch.isspace())
    if len(clean) < 2:
        return clean in content if clean else False
    return any(clean[i:i + 2] in content for i in range(len(clean) - 1))


def _looks_like_promise(text: str) -> bool:
    return any(hint in text for hint in PROMISE_HINTS)


def _stamp(moment: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(moment)))


def _clip(lines: list[str], limit: int) -> tuple[str, bool]:
    """从最旧的一行开始删，保留标题行；返回 (文本, 是否截断)。"""
    if limit <= 0:
        return "", True
    text = "\n".join(lines)
    if len(text) <= limit:
        return text, False
    body = list(lines[1:])
    while body and len("\n".join([lines[0], *body])) > limit:
        body.pop(0)
    clipped = "\n".join([lines[0], *body])
    return clipped[:limit], True
