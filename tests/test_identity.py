"""身份合并/撤销验收（PRD §7.2 / §7.3，AC-13 / AC-15）。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from gateway.identity import IdentityStore, MergeConflict, MergeError
from gateway.mailbox import Incoming, Mailbox

NOW = 1_800_000_000.0


class IdentityCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db", clock=lambda: NOW)
        self.addCleanup(self.mailbox.close)
        self.qq = self.mailbox.ensure_account("qq", "onebot")
        self.douyin = self.mailbox.ensure_account("douyin", "rpa")
        self.identities = IdentityStore(self.mailbox, clock=lambda: NOW)

    def ingest(self, account: int, platform: str, chat: str, text: str, *,
               msg_id: str, kind: str = "private", sender: str = "") -> Incoming:
        return self.mailbox.ingest(Incoming(
            account_id=account, platform=platform, kind=kind, chat_id=chat,
            sender_id=sender or chat, sender_name="神", id_kind="stable", text=text,
            platform_msg_id=msg_id, occurred_at=NOW, time_source="platform",
        ))

    def persons(self) -> dict[int, dict]:
        return {item["id"]: item for item in self.identities.list_persons()}


class TestCandidates(IdentityCase):
    def test_same_name_cross_platform_is_only_a_candidate(self) -> None:
        qq_person = self.ingest(self.qq, "qq", "u1", "QQ 消息", msg_id="q1").person_id
        dy_person = self.ingest(self.douyin, "douyin", "u1", "抖音消息", msg_id="d1").person_id
        candidates = self.identities.candidates(qq_person)
        self.assertEqual([item["id"] for item in candidates], [dy_person])
        self.assertTrue(candidates[0]["same_name"])
        self.assertTrue(candidates[0]["cross_platform"])
        self.assertTrue(candidates[0]["eligible"])

    def test_nickname_only_identity_is_not_eligible(self) -> None:
        qq_person = self.ingest(self.qq, "qq", "u1", "QQ 消息", msg_id="q1").person_id
        result = self.mailbox.ingest(Incoming(
            account_id=self.qq, platform="qq", kind="private", chat_id="nick",
            sender_id="nick", sender_name="同昵称的人", id_kind="provisional",
            text="只有昵称", platform_msg_id="n1",
        ))
        candidates = self.identities.candidates(qq_person)
        self.assertFalse(candidates[0]["eligible"])
        self.assertIn("只有昵称", candidates[0]["reason"])
        preview = self.identities.preview_merge(result.person_id, qq_person)
        self.assertFalse(preview.ok)
        with self.assertRaises(MergeError):
            self.identities.merge(result.person_id, qq_person)


class TestPreview(IdentityCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.ingest(self.qq, "qq", "u1", "QQ 消息", msg_id="q1")
        self.b = self.ingest(self.douyin, "douyin", "u1", "抖音消息", msg_id="d1")
        self.mailbox.ingest(Incoming(
            account_id=self.qq, platform="qq", kind="group", chat_id="g1",
            sender_id="u1", sender_name="神", id_kind="stable", text="群里说话",
            platform_msg_id="g1",
        ))

    def test_preview_lists_private_only(self) -> None:
        preview = self.identities.preview_merge(self.b.person_id, self.a.person_id)
        self.assertTrue(preview.ok)
        self.assertEqual(len(preview.identities), 1)
        self.assertEqual(preview.identities[0]["platform"], "douyin")
        self.assertEqual(len(preview.conversations), 1)
        self.assertEqual({item["kind"] for item in preview.conversations}, {"private"})
        self.assertEqual(preview.message_count, 1)

    def test_self_merge_preview_is_blocked(self) -> None:
        preview = self.identities.preview_merge(self.a.person_id, self.a.person_id)
        self.assertFalse(preview.ok)
        self.assertIn("不能把一个人合并到自己", preview.blocked)

    def test_unknown_person_raises(self) -> None:
        with self.assertRaises(MergeError):
            self.identities.preview_merge(999, self.a.person_id)


class TestMerge(IdentityCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.ingest(self.qq, "qq", "u1", "QQ 说一句", msg_id="q1")
        self.b = self.ingest(self.douyin, "douyin", "u1", "抖音说一句", msg_id="d1")

    def fact(self, person_id: int, text: str, *, identity_id: int | None) -> int:
        return int(self.mailbox.transaction(lambda conn: self._insert_fact(
            conn, person_id, text, identity_id)))

    @staticmethod
    def _insert_fact(conn, person_id: int, text: str, identity_id: int | None) -> int:
        from gateway.mailbox import _tx

        with _tx(conn):
            return int(conn.execute(
                "INSERT INTO memory_fact(person_id,kind,content,source_identity_id,"
                "confidence,status,frozen,created_at,updated_at) "
                "VALUES(?,'preference',?,?,0.8,'active',0,?,?)",
                (person_id, text, identity_id, NOW, NOW),
            ).lastrowid)

    def test_merge_moves_identity_conversation_and_facts(self) -> None:
        identity_id = self.persons()[self.b.person_id]["identities"][0]["id"]
        movable = self.fact(self.b.person_id, "喜欢喝冰美式", identity_id=identity_id)
        orphan = self.fact(self.b.person_id, "归属不明的一句话", identity_id=None)

        result = self.identities.merge(self.b.person_id, self.a.person_id, note="test")
        self.assertEqual(result["moved_identities"], 1)
        self.assertEqual(result["moved_conversations"], 1)
        self.assertEqual(result["quarantined_facts"], 1)

        persons = self.persons()
        self.assertEqual(persons[self.a.person_id]["platforms"], ["douyin", "qq"])
        self.assertEqual(persons[self.b.person_id]["identities"], [])
        with self.mailbox.read() as conn:
            movable_row = conn.execute(
                "SELECT person_id,status FROM memory_fact WHERE id=?", (movable,)
            ).fetchone()
            orphan_row = conn.execute(
                "SELECT person_id,status FROM memory_fact WHERE id=?", (orphan,)
            ).fetchone()
        self.assertEqual((movable_row[0], movable_row[1]), (self.a.person_id, "active"))
        self.assertEqual((orphan_row[0], orphan_row[1]), (self.b.person_id, "quarantined"))

    def test_merge_bumps_boundary_versions(self) -> None:
        self.identities.merge(self.b.person_id, self.a.person_id)
        with self.mailbox.read() as conn:
            boundaries = {
                int(row[0]): int(row[1])
                for row in conn.execute("SELECT id,boundary_version FROM thread WHERE scope='private'")
            }
        result = self.mailbox.claim_message("w1")
        self.assertEqual(result.boundary_version, boundaries[result.thread_id])
        self.assertTrue(all(value >= 1 for value in boundaries.values()))

    def test_merge_records_audit_and_log(self) -> None:
        result = self.identities.merge(self.b.person_id, self.a.person_id, actor="test")
        merges = self.identities.merges()
        self.assertEqual(merges[0]["id"], result["merge_id"])
        self.assertIsNone(merges[0]["reverted_at"])
        events = [row["event_type"] for row in self.mailbox.list_audit()]
        self.assertIn("person_merged", events)

    def test_target_without_private_thread_reassigns_source_thread(self) -> None:
        # A 只有群身份（没有私聊 thread），B 有私聊：合并后 thread 过继给 A
        lonely = self.mailbox.ingest(Incoming(
            account_id=self.qq, platform="qq", kind="group", chat_id="g9",
            sender_id="u2", sender_name="群友", id_kind="stable", text="群里",
            platform_msg_id="g9",
        ))
        self.assertIsNone(self.persons()[lonely.person_id]["private_thread_id"])
        result = self.identities.merge(self.b.person_id, lonely.person_id)
        self.assertEqual(result["moved_conversations"], 0)
        persons = self.persons()
        self.assertIsNotNone(persons[lonely.person_id]["private_thread_id"])

    def test_merged_thread_is_shared_for_l2(self) -> None:
        self.identities.merge(self.b.person_id, self.a.person_id)
        with self.mailbox.read() as conn:
            threads = {
                int(row[0])
                for row in conn.execute("SELECT DISTINCT thread_id FROM conversation")
            }
        self.assertEqual(len(threads), 1, "合并后两个平台共用一个私聊 thread")


class TestRevert(IdentityCase):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.ingest(self.qq, "qq", "u1", "QQ 说一句", msg_id="q1")
        self.b = self.ingest(self.douyin, "douyin", "u1", "抖音说一句", msg_id="d1")
        self.merged = self.identities.merge(self.b.person_id, self.a.person_id)

    def test_revert_restores_ownership(self) -> None:
        result = self.identities.revert(self.merged["merge_id"], actor="test")
        self.assertTrue(result["reverted"])
        persons = self.persons()
        self.assertEqual(persons[self.a.person_id]["platforms"], ["qq"])
        self.assertEqual(persons[self.b.person_id]["platforms"], ["douyin"])
        with self.mailbox.read() as conn:
            threads = {
                int(row[0])
                for row in conn.execute("SELECT DISTINCT thread_id FROM conversation")
            }
        self.assertEqual(len(threads), 2)

    def test_revert_refuses_double_revert(self) -> None:
        self.identities.revert(self.merged["merge_id"])
        with self.assertRaises(MergeConflict):
            self.identities.revert(self.merged["merge_id"])

    def test_revert_detects_manual_change(self) -> None:
        def move_again(conn) -> None:
            from gateway.mailbox import _tx

            with _tx(conn):
                conn.execute(
                    "UPDATE conversation SET thread_id=? WHERE thread_id=?",
                    (self.b.thread_id, self.a.thread_id),
                )

        self.mailbox.transaction(move_again)
        with self.assertRaises(MergeConflict):
            self.identities.revert(self.merged["merge_id"])
        # 冲突时什么都不改：合并后的身份仍挂在目标人物上
        self.assertEqual(self.persons()[self.a.person_id]["platforms"], ["douyin", "qq"])

    def test_revert_keeps_quarantined_facts_isolated(self) -> None:
        # setUp 里已合并过一次，先撤回去，再制造「归属不明的事实」走一遍新合并
        self.identities.revert(self.merged["merge_id"])

        def add_orphan(conn) -> int:
            from gateway.mailbox import _tx

            with _tx(conn):
                return int(conn.execute(
                    "INSERT INTO memory_fact(person_id,kind,content,source_identity_id,"
                    "confidence,status,frozen,created_at,updated_at) "
                    "VALUES(?,'event','归属不明',NULL,0.7,'active',0,?,?)",
                    (self.b.person_id, NOW, NOW),
                ).lastrowid)

        orphan = int(self.mailbox.transaction(add_orphan))
        second = self.identities.merge(self.b.person_id, self.a.person_id)
        self.identities.revert(second["merge_id"])
        with self.mailbox.read() as conn:
            row = conn.execute("SELECT person_id,status FROM memory_fact WHERE id=?", (orphan,)).fetchone()
        self.assertEqual((row[0], row[1]), (self.b.person_id, "quarantined"))


if __name__ == "__main__":
    unittest.main()
