"""身份合并与撤销（PRD §7.2 / §7.3）。

规矩只有三条
------------
1. **先预览，再确认**：预览列出会移动的私聊身份、会话、消息数和受影响的事实；
   群聊/评论本来就不挂在 person 名下，所以不会出现在可合并列表里。
2. **一次事务**：``BEGIN IMMEDIATE`` 里写 ``merge_log``，再改 identity / conversation /
   thread / fact 归属；消息正文和平台消息 ID 一律不动，旧 thread 只留空壳。
3. **撤销按日志来**：严格按 ``payload_json`` 恢复，不重新猜归属；如果期间有人工改动
   就先报冲突，不覆盖。归属不明的事实进 ``quarantined``，撤销后也不自动放回。

另外每次归属变化都会让相关 thread 的 ``boundary_version`` +1：
正在生成的任务在保存回复前必须比对，版本变了就不能发给原来那个人（§7.3 第 5 条）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .mailbox import Mailbox, MailboxError, _tx


class MergeError(MailboxError):
    code = "merge_error"


class MergeConflict(MergeError):
    code = "merge_conflict"


@dataclass(slots=True)
class MergePreview:
    source: dict[str, Any]
    target: dict[str, Any]
    identities: list[dict[str, Any]] = field(default_factory=list)
    conversations: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    blocked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "target": self.target,
            "identities": self.identities,
            "conversations": self.conversations,
            "facts": self.facts,
            "message_count": self.message_count,
            "blocked": self.blocked,
        }


class IdentityStore:
    def __init__(self, mailbox: Mailbox, *, clock=time.time) -> None:
        self._mailbox = mailbox
        self._clock = clock

    # ---- 读 ----

    def list_persons(self, *, query: str = "") -> list[dict[str, Any]]:
        """人物列表：平台身份、私聊 thread、消息量与最近活动。"""
        like = f"%{query.strip()}%" if query.strip() else None
        with self._mailbox.read() as conn:
            rows = conn.execute(
                """SELECT p.id,p.display_name,IFNULL(p.note,'') AS note,p.is_self,p.created_at
                   FROM person p ORDER BY p.id"""
            ).fetchall()
            identities = conn.execute(
                """SELECT id,person_id,platform,platform_uid,id_kind,display_name
                   FROM identity ORDER BY person_id,id"""
            ).fetchall()
            threads = conn.execute(
                "SELECT id,person_id FROM thread WHERE scope='private'"
            ).fetchall()
            stats = conn.execute(
                """SELECT t.person_id AS person_id, COUNT(m.id) AS message_count,
                          MAX(m.ingested_at) AS last_at
                   FROM thread t
                   JOIN conversation c ON c.thread_id=t.id
                   LEFT JOIN message m ON m.conversation_id=c.id
                   WHERE t.scope='private'
                   GROUP BY t.person_id"""
            ).fetchall()
        by_person: dict[int, list[dict[str, Any]]] = {}
        for row in identities:
            by_person.setdefault(int(row["person_id"]), []).append(dict(row))
        thread_by_person = {int(row["person_id"]): int(row["id"]) for row in threads}
        stat_by_person = {int(row["person_id"]): row for row in stats}
        result: list[dict[str, Any]] = []
        for row in rows:
            person_id = int(row["id"])
            items = by_person.get(person_id, [])
            stat = stat_by_person.get(person_id)
            entry = {
                "id": person_id,
                "display_name": str(row["display_name"]),
                "note": str(row["note"]),
                "is_self": bool(row["is_self"]),
                "identities": items,
                "platforms": sorted({item["platform"] for item in items}),
                "private_thread_id": thread_by_person.get(person_id),
                "message_count": int(stat["message_count"]) if stat else 0,
                "last_at": float(stat["last_at"]) if stat and stat["last_at"] else None,
                "provisional_only": bool(items) and all(
                    item["id_kind"] == "provisional" for item in items
                ),
            }
            if like and not self._matches(entry, like):
                continue
            result.append(entry)
        return result

    @staticmethod
    def _matches(entry: dict[str, Any], like: str) -> bool:
        needle = like.strip("%")
        if needle in entry["display_name"] or needle in entry["note"]:
            return True
        return any(
            needle in (item.get("display_name") or "") or needle in item["platform_uid"]
            for item in entry["identities"]
        )

    def candidates(self, person_id: int) -> list[dict[str, Any]]:
        """候选只展示、不自动合并；同昵称只能算「看起来像」，不是依据。"""
        persons = self.list_persons()
        me = next((item for item in persons if item["id"] == int(person_id)), None)
        if me is None:
            raise MergeError("人物不存在", code="not_found")
        my_platforms = set(me["platforms"])
        my_name = me["display_name"]
        candidates: list[dict[str, Any]] = []
        for other in persons:
            if other["id"] == me["id"]:
                continue
            same_name = other["display_name"] == my_name
            cross_platform = bool(set(other["platforms"]) - my_platforms)
            candidates.append({
                **other,
                "same_name": same_name,
                "cross_platform": cross_platform,
                "eligible": not (other["provisional_only"] or me["provisional_only"]),
                "reason": "只有昵称的身份不能作为合并依据"
                          if (other["provisional_only"] or me["provisional_only"]) else "",
            })
        candidates.sort(key=lambda item: (
            not item["same_name"], not item["cross_platform"],
            -(item["last_at"] or 0), item["id"],
        ))
        return candidates

    # ---- 预览 ----

    def preview_merge(self, source_id: int, target_id: int) -> MergePreview:
        source_id, target_id = int(source_id), int(target_id)
        persons = {item["id"]: item for item in self.list_persons()}
        if source_id not in persons or target_id not in persons:
            raise MergeError("人物不存在", code="not_found")
        source, target = persons[source_id], persons[target_id]
        preview = MergePreview(source=self._short(source), target=self._short(target))
        if source_id == target_id:
            preview.blocked.append("不能把一个人合并到自己")
            return preview
        if source["provisional_only"] or target["provisional_only"]:
            preview.blocked.append("只有昵称的身份不能参与合并（PRD §7.2）")
        preview.identities = [
            {key: item[key] for key in ("id", "platform", "platform_uid", "id_kind", "display_name")}
            for item in source["identities"]
        ]
        with self._mailbox.read() as conn:
            conversations = conn.execute(
                """SELECT c.id,c.kind,c.chat_id,c.account_id,a.platform,
                          COUNT(m.id) AS message_count, MAX(m.ingested_at) AS last_at
                   FROM conversation c
                   JOIN thread t ON t.id=c.thread_id
                   JOIN account a ON a.id=c.account_id
                   LEFT JOIN message m ON m.conversation_id=c.id
                   WHERE t.scope='private' AND t.person_id=?
                   GROUP BY c.id ORDER BY c.id""",
                (source_id,),
            ).fetchall()
            facts = conn.execute(
                """SELECT id,kind,content,confidence,status,source_identity_id
                   FROM memory_fact WHERE person_id=? ORDER BY id""",
                (source_id,),
            ).fetchall()
        preview.conversations = [dict(row) for row in conversations]
        preview.message_count = sum(int(row["message_count"]) for row in conversations)
        moved_identity_ids = {item["id"] for item in preview.identities}
        for row in facts:
            item = dict(row)
            if item["source_identity_id"] and int(item["source_identity_id"]) in moved_identity_ids:
                item["action"] = "move"
            else:
                item["action"] = "quarantine"
            preview.facts.append(item)
        return preview

    @staticmethod
    def _short(person: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": person["id"], "display_name": person["display_name"],
            "platforms": person["platforms"], "message_count": person["message_count"],
            "provisional_only": person["provisional_only"],
        }

    # ---- 合并 ----

    def merge(self, source_id: int, target_id: int, *, actor: str = "owner",
              note: str = "") -> dict[str, Any]:
        preview = self.preview_merge(source_id, target_id)
        if not preview.ok:
            raise MergeError(preview.blocked[0], code="merge_blocked")
        source_id, target_id = int(source_id), int(target_id)
        now = self._clock()
        report: dict[str, Any] = {}

        def run(conn) -> None:
            with _tx(conn):
                # 事务里重新取一遍，别拿预览时的旧状态做决定
                identities = [
                    dict(row) for row in conn.execute(
                        "SELECT id FROM identity WHERE person_id=? ORDER BY id", (source_id,)
                    )
                ]
                if not identities:
                    raise MergeError("来源人物已经没有身份可合并", code="merge_blocked")
                source_thread = conn.execute(
                    "SELECT id FROM thread WHERE scope='private' AND person_id=?", (source_id,)
                ).fetchone()
                target_thread = conn.execute(
                    "SELECT id FROM thread WHERE scope='private' AND person_id=?", (target_id,)
                ).fetchone()
                source_thread_id = int(source_thread[0]) if source_thread else None
                target_thread_id = int(target_thread[0]) if target_thread else None

                moved_identities = [int(row["id"]) for row in identities]
                for identity_id in moved_identities:
                    conn.execute(
                        "UPDATE identity SET person_id=? WHERE id=?", (target_id, identity_id)
                    )

                conversations: list[dict[str, int]] = []
                threads: list[dict[str, Any]] = []
                if source_thread_id is not None:
                    if target_thread_id is None:
                        # 目标没有私聊 thread：直接把 source 的 thread 过继给 target
                        conn.execute(
                            "UPDATE thread SET person_id=?,updated_at=? WHERE id=?",
                            (target_id, now, source_thread_id),
                        )
                        threads.append({"id": source_thread_id, "action": "reassign",
                                        "from_person": source_id})
                        target_thread_id = source_thread_id
                    else:
                        rows = conn.execute(
                            "SELECT id FROM conversation WHERE thread_id=?", (source_thread_id,)
                        ).fetchall()
                        for row in rows:
                            conversation_id = int(row[0])
                            if conversation_id in {item["id"] for item in conversations}:
                                continue
                            conversations.append({"id": conversation_id,
                                                  "from_thread": source_thread_id,
                                                  "to_thread": target_thread_id})
                            conn.execute(
                                "UPDATE conversation SET thread_id=? WHERE id=?",
                                (target_thread_id, conversation_id),
                            )
                        threads.append({"id": source_thread_id, "action": "retire"})

                moved_set = set(moved_identities)
                facts_payload: list[dict[str, Any]] = []
                quarantined = 0
                for row in conn.execute(
                    "SELECT id,status,source_identity_id FROM memory_fact WHERE person_id=?",
                    (source_id,),
                ).fetchall():
                    fact_id = int(row["id"])
                    source_identity_id = row["source_identity_id"]
                    if (source_identity_id and int(source_identity_id) in moved_set
                            and str(row["status"]) != "deleted"):
                        conn.execute(
                            "UPDATE memory_fact SET person_id=?,updated_at=? WHERE id=?",
                            (target_id, now, fact_id),
                        )
                        facts_payload.append({"id": fact_id, "action": "move",
                                              "from_person": source_id})
                    else:
                        facts_payload.append({"id": fact_id, "action": "quarantine",
                                              "status_before": str(row["status"])})
                        if str(row["status"]) == "active":
                            conn.execute(
                                "UPDATE memory_fact SET status='quarantined',updated_at=? WHERE id=?",
                                (now, fact_id),
                            )
                            quarantined += 1

                boundary_ids = [tid for tid in (target_thread_id, source_thread_id) if tid]
                for thread_id in boundary_ids:
                    conn.execute(
                        "UPDATE thread SET boundary_version=boundary_version+1 WHERE id=?",
                        (thread_id,),
                    )

                payload = {
                    "identities": [{"id": identity_id, "from_person": source_id}
                                   for identity_id in moved_identities],
                    "conversations": conversations,
                    "threads": threads,
                    "facts": facts_payload,
                    "boundary_threads": boundary_ids,
                }
                merge_id = int(
                    conn.execute(
                        "INSERT INTO merge_log(source_person_id,target_person_id,payload_json,"
                        "created_at) VALUES(?,?,?,?)",
                        (source_id, target_id, json.dumps(payload, ensure_ascii=False), now),
                    ).lastrowid or 0
                )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('person_merged','person',?,?,?)",
                    (str(target_id),
                     json.dumps({"merge_id": merge_id, "source": source_id, "target": target_id,
                                 "actor": actor, "note": note, "identities": len(moved_identities),
                                 "conversations": len(conversations),
                                 "quarantined_facts": quarantined}, ensure_ascii=False),
                     now),
                )
                report.update({
                    "merge_id": merge_id,
                    "source_person_id": source_id,
                    "target_person_id": target_id,
                    "moved_identities": len(moved_identities),
                    "moved_conversations": len(conversations),
                    "message_count": preview.message_count,
                    "quarantined_facts": quarantined,
                    "boundary_threads": boundary_ids,
                })

        self._mailbox.transaction(run)
        return report

    # ---- 撤销 ----

    def revert(self, merge_id: int, *, actor: str = "owner") -> dict[str, Any]:
        now = self._clock()
        report: dict[str, Any] = {}

        def run(conn) -> None:
            with _tx(conn):
                row = conn.execute(
                    "SELECT * FROM merge_log WHERE id=?", (int(merge_id),)
                ).fetchone()
                if row is None:
                    raise MergeError("合并记录不存在", code="not_found")
                if row["reverted_at"] is not None:
                    raise MergeConflict("这次合并已经撤销过了", code="already_reverted")
                payload = json.loads(row["payload_json"] or "{}")
                target_id = int(row["target_person_id"])
                source_id = int(row["source_person_id"])

                # 先全部校验，再动手；有任何人工改动就报冲突，不覆盖
                for item in payload.get("identities", []):
                    current = conn.execute(
                        "SELECT person_id FROM identity WHERE id=?", (int(item["id"]),)
                    ).fetchone()
                    if current is None:
                        raise MergeConflict(
                            f"身份 {item['id']} 已不存在，无法按日志恢复", code="merge_conflict"
                        )
                    if int(current[0]) != target_id:
                        raise MergeConflict(
                            f"身份 {item['id']} 的归属已被人工改过，未做任何修改",
                            code="merge_conflict",
                        )
                for item in payload.get("conversations", []):
                    current = conn.execute(
                        "SELECT thread_id FROM conversation WHERE id=?", (int(item["id"]),)
                    ).fetchone()
                    if current is None:
                        raise MergeConflict(
                            f"会话 {item['id']} 已不存在，无法按日志恢复", code="merge_conflict"
                        )
                    if int(current[0]) != int(item["to_thread"]):
                        raise MergeConflict(
                            f"会话 {item['id']} 已被移动到别的 thread，未做任何修改",
                            code="merge_conflict",
                        )
                for item in payload.get("threads", []):
                    if item["action"] != "reassign":
                        continue
                    current = conn.execute(
                        "SELECT person_id FROM thread WHERE id=?", (int(item["id"]),)
                    ).fetchone()
                    if current is None or int(current[0]) != target_id:
                        raise MergeConflict(
                            "私聊 thread 的归属已被人工改过，未做任何修改", code="merge_conflict"
                        )
                for item in payload.get("facts", []):
                    if item["action"] != "move":
                        continue
                    current = conn.execute(
                        "SELECT person_id FROM memory_fact WHERE id=?", (int(item["id"]),)
                    ).fetchone()
                    if current is None:
                        continue
                    if int(current[0]) != target_id:
                        raise MergeConflict(
                            f"事实 {item['id']} 的归属已被人工改过，未做任何修改",
                            code="merge_conflict",
                        )

                for item in payload.get("identities", []):
                    conn.execute(
                        "UPDATE identity SET person_id=? WHERE id=?",
                        (int(item["from_person"]), int(item["id"])),
                    )
                for item in payload.get("conversations", []):
                    conn.execute(
                        "UPDATE conversation SET thread_id=? WHERE id=?",
                        (int(item["from_thread"]), int(item["id"])),
                    )
                for item in payload.get("threads", []):
                    if item["action"] == "reassign":
                        conn.execute(
                            "UPDATE thread SET person_id=?,updated_at=? WHERE id=?",
                            (source_id, now, int(item["id"])),
                        )
                for item in payload.get("facts", []):
                    if item["action"] != "move":
                        continue
                    conn.execute(
                        "UPDATE memory_fact SET person_id=?,updated_at=? WHERE id=?",
                        (int(item["from_person"]), now, int(item["id"])),
                    )
                    # quarantine 的事实保持隔离：归属不明，撤销不等于恢复可信（§7.2）

                boundary_ids = [int(tid) for tid in payload.get("boundary_threads", [])]
                for thread_id in boundary_ids:
                    conn.execute(
                        "UPDATE thread SET boundary_version=boundary_version+1 WHERE id=?",
                        (thread_id,),
                    )
                conn.execute(
                    "UPDATE merge_log SET reverted_at=? WHERE id=?", (now, int(merge_id))
                )
                conn.execute(
                    "INSERT INTO audit_event(event_type,entity_type,entity_id,payload_json,"
                    "created_at) VALUES('person_merge_reverted','person',?,?,?)",
                    (str(target_id),
                     json.dumps({"merge_id": int(merge_id), "actor": actor,
                                 "source": source_id}, ensure_ascii=False), now),
                )
                report.update({
                    "merge_id": int(merge_id),
                    "reverted": True,
                    "source_person_id": source_id,
                    "target_person_id": target_id,
                    "boundary_threads": boundary_ids,
                })

        self._mailbox.transaction(run)
        return report

    # ---- 列表 ----

    def merges(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._mailbox.read() as conn:
            rows = conn.execute(
                """SELECT m.id,m.source_person_id,m.target_person_id,m.created_at,m.reverted_at,
                          s.display_name AS source_name,t.display_name AS target_name
                   FROM merge_log m
                   LEFT JOIN person s ON s.id=m.source_person_id
                   LEFT JOIN person t ON t.id=m.target_person_id
                   ORDER BY m.id DESC LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]
