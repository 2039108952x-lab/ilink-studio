"""消息底座的验收测试：对着 PRD 的 AC-05～11、AC-24 逐条来。"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from gateway import db as db_mod
from gateway.db import SchemaTooNew
from gateway.importer import import_sessions_file
from gateway.mailbox import Incoming, Mailbox, MailboxError


class MailboxCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.mailbox = Mailbox(self.root / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account_id = self.mailbox.ensure_account("qq", "onebot")

    def incoming(self, **over) -> Incoming:
        base = dict(
            account_id=self.account_id, platform="qq", kind="private", chat_id="10001",
            sender_id="u1", sender_name="神", id_kind="stable", text="在吗",
            platform_msg_id="m1",
        )
        base.update(over)
        return Incoming(**base)

    def bodies(self, **kw) -> list[str]:
        return [item["body"] for item in self.mailbox.list_messages(limit=100, **kw).items]


class TestWhitelistPrivacy(MailboxCase):
    """AC-05：非白名单不存正文，库/FTS/导出/审计都查不到。"""

    def test_rejected_message_has_no_body_anywhere(self) -> None:
        secret = "我的口令是 42"
        self.mailbox.record_rejected(self.incoming(text=secret, platform_msg_id="spam"))
        stored = self.mailbox.list_messages(limit=10).items[0]
        self.assertEqual(stored["storage_class"], "metadata_only")
        self.assertIsNone(stored["body"])
        self.assertEqual(stored["skip_reason"], "not_allowlisted")
        self.assertEqual(self.mailbox.list_messages(query=secret).items, [])
        self.assertEqual(self.mailbox.list_messages(query="口令").items, [])
        # 导出只带完整正文的消息；被拒绝的那条连一行都不出现。
        export = self.mailbox.export_thread(stored["thread_id"])
        self.assertEqual(export["messages"], [])
        for event in self.mailbox.list_audit():
            self.assertNotIn(secret, str(event["payload_json"]))

    def test_message_stats_do_not_leak_text(self) -> None:
        self.mailbox.record_rejected(self.incoming(text="机密", platform_msg_id="x"))
        self.assertNotIn("机密", str(self.mailbox.stats()))


class TestDedup(MailboxCase):
    """AC-06：同一稳定 ID 并发投递两次 + 重启后再投，只有一条。"""

    def test_concurrent_delivery_same_id(self) -> None:
        results: list[bool] = []

        def deliver() -> None:
            results.append(self.mailbox.ingest(self.incoming()).created)

        threads = [threading.Thread(target=deliver) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, False, False, True])
        self.assertEqual(len(self.mailbox.list_messages(limit=10).items), 1)

    def test_redelivery_after_restart_is_ignored(self) -> None:
        first = self.mailbox.ingest(self.incoming())
        self.mailbox.close()
        reopened = Mailbox(self.root / "mailbox.db")
        self.addCleanup(reopened.close)
        again = reopened.ingest(self.incoming())
        self.assertFalse(again.created)
        self.assertEqual(again.message_id, first.message_id)

    def test_two_replies_never_for_one_inbound(self) -> None:
        result = self.mailbox.ingest(self.incoming())
        claimed = self.mailbox.claim_message("w1")
        first = self.mailbox.save_reply(claimed, "第一条")
        self.mailbox.claim_message("w2")
        self.mailbox.finish_outbox(first.outbox_id, "sent")
        # 入站已 replied，不会再被领取
        self.assertIsNone(self.mailbox.claim_message("w3"))


class TestNoPlatformId(MailboxCase):
    """AC-07：没有平台 ID 时，同一人连说两句「嗯」不能只留一条。"""

    def test_duplicate_text_is_kept_twice(self) -> None:
        first = self.mailbox.ingest(self.incoming(platform_msg_id="", text="嗯"))
        second = self.mailbox.ingest(self.incoming(platform_msg_id="", text="嗯"))
        self.assertTrue(first.created)
        self.assertTrue(second.created)
        self.assertNotEqual(first.message_id, second.message_id)
        self.assertEqual(self.bodies(), ["嗯", "嗯"])

    def test_dedup_key_includes_ordinal(self) -> None:
        self.mailbox.ingest(self.incoming(platform_msg_id="", text="嗯"))
        self.mailbox.ingest(self.incoming(platform_msg_id="", text="嗯"))
        with self.mailbox.read() as conn:
            keys = [row[0] for row in conn.execute("SELECT dedup_key FROM message ORDER BY id")]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.endswith((":1", ":2")) for key in keys))


class TestCrashRecovery(MailboxCase):
    """AC-08：三个崩溃点的恢复语义。"""

    def test_recover_requeues_processing_message(self) -> None:
        self.mailbox.ingest(self.incoming())
        claimed = self.mailbox.claim_message("dead-worker")
        self.assertIsNotNone(claimed)
        recovered = self.mailbox.recover()
        self.assertEqual(recovered["messages_requeued"], 1)
        again = self.mailbox.claim_message("new-worker")
        self.assertEqual(again.id, claimed.id, "崩在模型调用前的消息可以重新处理")

    def test_recover_marks_sending_outbox_unknown(self) -> None:
        self.mailbox.ingest(self.incoming())
        claimed = self.mailbox.claim_message("w1")
        outbox = self.mailbox.save_reply(claimed, "收到")
        self.mailbox.claim_outbox("w1")
        recovered = self.mailbox.recover()
        self.assertEqual(recovered["outbox_unknown"], 1)
        task = self.mailbox.get_outbox(outbox.outbox_id)
        self.assertEqual(task["state"], "unknown")
        self.assertEqual(task["error_code"], "crashed_during_send")
        # 结果不明的发送禁止自动重发
        self.assertIsNone(self.mailbox.claim_outbox("w2"))

    def test_pending_outbox_survives_restart(self) -> None:
        self.mailbox.ingest(self.incoming())
        claimed = self.mailbox.claim_message("w1")
        outbox = self.mailbox.save_reply(claimed, "收到")
        self.mailbox.close()
        reopened = Mailbox(self.root / "mailbox.db")
        self.addCleanup(reopened.close)
        task = reopened.claim_outbox("w2")
        self.assertEqual(task.outbox_id, outbox.outbox_id, "pending 可以继续发一次")


class TestOutboxStates(MailboxCase):
    def setUp(self) -> None:
        super().setUp()
        self.mailbox.ingest(self.incoming())
        self.claimed = self.mailbox.claim_message("w1")
        self.outbox = self.mailbox.save_reply(self.claimed, "收到")

    def test_failed_send_is_terminal_and_visible(self) -> None:
        self.mailbox.finish_outbox(self.outbox.outbox_id, "failed", error_code="send_rejected",
                                   error_detail_safe="平台拒绝：对方设置了好友验证")
        task = self.mailbox.get_outbox(self.outbox.outbox_id)
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["error_detail_safe"], "平台拒绝：对方设置了好友验证")
        self.assertIsNone(self.mailbox.claim_outbox("w2"), "明确失败的任务不自动重发")

    def test_unknown_can_be_reconciled(self) -> None:
        self.mailbox.finish_outbox(self.outbox.outbox_id, "unknown", error_code="send_exception")
        self.assertEqual(self.mailbox.get_message(self.claimed.id).reply_policy, "normal")
        # 入站转待核对
        detail = self.mailbox.get_message_detail(self.claimed.id)
        self.assertEqual(detail["processing_status"], "needs_review")
        with self.assertRaises(MailboxError):
            self.mailbox.reconcile_outbox(self.outbox.outbox_id, "乱写的动作")
        self.assertEqual(
            self.mailbox.reconcile_outbox(self.outbox.outbox_id, "mark_sent")["state"], "sent"
        )

    def test_resend_creates_new_idempotency_key(self) -> None:
        self.mailbox.finish_outbox(self.outbox.outbox_id, "failed", error_code="send_rejected")
        resent = self.mailbox.reconcile_outbox(self.outbox.outbox_id, "resend")
        self.assertNotEqual(resent["outbox_id"], self.outbox.outbox_id)
        task = self.mailbox.get_outbox(resent["outbox_id"])
        self.assertEqual(task["state"], "pending")
        original = self.mailbox.get_outbox(self.outbox.outbox_id)
        self.assertNotEqual(task["idempotency_key"], original["idempotency_key"])


class TestSearch(MailboxCase):
    """AC-11：长中文命中、短词精确子串、通配符不扩大语义、增删后索引一致。"""

    def setUp(self) -> None:
        super().setUp()
        self.mailbox.ingest(self.incoming(platform_msg_id="a", text="山中猛虎下山了"))
        self.mailbox.ingest(self.incoming(platform_msg_id="b", text="嗯"))
        self.mailbox.ingest(self.incoming(platform_msg_id="c", text="折扣 100%_test 结束"))
        self.mailbox.ingest(self.incoming(platform_msg_id="d", text="普通一句话"))

    def test_long_chinese_hits(self) -> None:
        self.assertEqual(self.bodies(query="山中猛虎"), ["山中猛虎下山了"])
        self.assertEqual(self.bodies(query="猛虎下山"), ["山中猛虎下山了"])

    def test_short_query_is_exact_substring(self) -> None:
        page = self.mailbox.list_messages(query="猛虎", limit=10)
        self.assertTrue(page.slow, "1～2 字检索要标注可能较慢")
        self.assertEqual([item["body"] for item in page.items], ["山中猛虎下山了"])
        self.assertEqual(self.bodies(query="嗯"), ["嗯"])

    def test_wildcards_are_literal(self) -> None:
        self.assertEqual(self.bodies(query="%_"), ["折扣 100%_test 结束"])
        self.assertEqual(self.bodies(query="0%_t"), ["折扣 100%_test 结束"])
        # 单个 % 只会命中真正含 % 的那条，不会像 LIKE 那样匹配全表
        self.assertEqual(self.bodies(query="%"), ["折扣 100%_test 结束"])

    def test_index_follows_delete_and_purge(self) -> None:
        target = self.mailbox.list_messages(query="山中猛虎").items[0]["id"]
        self.mailbox.purge_message_body(target)
        self.assertEqual(self.mailbox.list_messages(query="山中猛虎").items, [])
        self.assertEqual(self.mailbox.list_messages(query="猛虎").items, [])
        with self.mailbox.read() as conn:
            conn.execute("INSERT INTO message_fts(message_fts) VALUES('integrity-check')")

    def test_pagination_is_stable_without_duplicates(self) -> None:
        first = self.mailbox.list_messages(limit=2)
        second = self.mailbox.list_messages(limit=2, cursor=first.next_cursor)
        ids = [item["id"] for item in first.items + second.items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(ids, reverse=True), ids)


class TestCheckpoints(MailboxCase):
    def test_roundtrip_and_primed(self) -> None:
        self.mailbox.save_checkpoint(self.account_id, "qq:inbound", cursor="c1",
                                     state={"observed": 3}, primed=True)
        loaded = self.mailbox.load_checkpoint(self.account_id, "qq:inbound")
        self.assertEqual(loaded["cursor"], "c1")
        self.assertEqual(loaded["state"], {"observed": 3})
        self.assertTrue(loaded["primed"])
        self.mailbox.save_checkpoint(self.account_id, "qq:inbound", cursor="c2")
        self.assertEqual(self.mailbox.load_checkpoint(self.account_id, "qq:inbound")["state"],
                         {"observed": 3})

    def test_secrets_are_rejected(self) -> None:
        for key in ("context_token", "sessdata", "api_key", "cookie_jar", "password"):
            with self.assertRaises(MailboxError):
                self.mailbox.save_checkpoint(self.account_id, "qq:inbound", state={key: "x"})
        with self.assertRaises(MailboxError):
            self.mailbox.audit("x", "message", "1", {"bot_token": "y"})


class TestLegacyImport(MailboxCase):
    """AC-10：同一文件导入两次不重复，原文不变，时间不冒充平台时间。"""

    def setUp(self) -> None:
        super().setUp()
        self.mailbox.ensure_account("bilibili", "cookie")
        self.source = self.root / "sessions.json"
        self.source.write_text(
            '{"qq:private:10001": [{"user": "晚安", "assistant": "晚安好梦"}],'
            ' "bilibili:comment:12:0:34": [{"user": "这视频不错", "assistant": "谢谢"}]}',
            encoding="utf-8",
        )
        self.original = self.source.read_bytes()

    def test_import_twice_is_idempotent(self) -> None:
        first = import_sessions_file(self.mailbox, self.source)
        second = import_sessions_file(self.mailbox, self.source)
        self.assertEqual(first.imported, 4)
        self.assertEqual(second.imported, 0)
        self.assertEqual(self.source.read_bytes(), self.original, "源文件不许改")
        self.assertEqual(len(self.mailbox.list_messages(limit=50).items), 4)

    def test_imported_rows_are_marked_and_never_reply(self) -> None:
        import_sessions_file(self.mailbox, self.source)
        items = self.mailbox.list_messages(limit=50).items
        for item in items:
            self.assertTrue(item["imported"])
            self.assertEqual(item["reply_policy"], "never")
            self.assertEqual(item["time_source"], "import")
            self.assertIsNone(item["occurred_at"], "导入时间不能冒充平台时间")
        with self.mailbox.read() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM message WHERE direction='in' "
                    "AND processing_status<>'skipped'"
                ).fetchone()[0],
                0,
            )

    def test_comment_branch_key_keeps_its_colons(self) -> None:
        import_sessions_file(self.mailbox, self.source)
        comment = self.mailbox.list_messages(limit=50, kind="comment").items
        self.assertEqual(len(comment), 2)
        self.assertEqual(comment[0]["chat_id"], "12:0:34")


class TestBackupRestore(MailboxCase):
    """AC-24：备份 → 恢复到临时目录 → integrity 与行数/抽样一致。"""

    def test_backup_restore_roundtrip(self) -> None:
        self.mailbox.ingest(self.incoming(platform_msg_id="a", text="山中猛虎下山了"))
        self.mailbox.ingest(self.incoming(platform_msg_id="b", text="第二句"))
        backup = self.mailbox.backup(self.root / "backups" / "snapshot.db")
        self.assertTrue(db_mod.integrity_check(backup))

        restored_dir = self.root / "restored"
        restored_dir.mkdir()
        restored = restored_dir / "mailbox.db"
        shutil.copyfile(backup, restored)
        state = db_mod.migrate(restored, backup_dir=restored_dir / "backups")
        self.assertEqual(state.pending, (), "备份里的 schema 已经是最新")
        copy = Mailbox(restored)
        self.addCleanup(copy.close)
        self.assertEqual(
            len(copy.list_messages(limit=50).items), len(self.mailbox.list_messages(limit=50).items)
        )
        self.assertEqual([item["body"] for item in copy.list_messages(query="猛虎").items],
                         ["山中猛虎下山了"])

    def test_newer_schema_is_refused(self) -> None:
        backup = self.mailbox.backup(self.root / "future.db")
        conn = sqlite3.connect(backup)
        conn.execute("INSERT INTO schema_migration(version,checksum,applied_at) "
                     "VALUES(99,'deadbeef',0)")
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaTooNew):
            db_mod.migrate(backup)

    def test_tampered_migration_is_refused(self) -> None:
        backup = self.mailbox.backup(self.root / "tampered.db")
        conn = sqlite3.connect(backup)
        conn.execute("UPDATE schema_migration SET checksum='deadbeef' WHERE version=1")
        conn.commit()
        conn.close()
        with self.assertRaises(db_mod.SchemaChecksumMismatch):
            db_mod.migrate(backup)


class TestStateMachine(MailboxCase):
    def test_skipped_requires_reason(self) -> None:
        result = self.mailbox.ingest(self.incoming())
        with self.assertRaises(MailboxError):
            self.mailbox.set_status(result.message_id, "skipped")

    def test_failed_requires_error_code(self) -> None:
        result = self.mailbox.ingest(self.incoming())
        with self.assertRaises(MailboxError):
            self.mailbox.set_status(result.message_id, "failed")

    def test_confirm_moves_needs_review_to_queued(self) -> None:
        result = self.mailbox.ingest(self.incoming(status="needs_review"))
        self.mailbox.confirm_message(result.message_id)
        self.assertEqual(self.mailbox.get_message(result.message_id).attempt_count, 0)
        claimed = self.mailbox.claim_message("w1")
        self.assertEqual(claimed.id, result.message_id)
        with self.assertRaises(MailboxError):
            self.mailbox.confirm_message(result.message_id)

    def test_manual_send_requires_known_conversation(self) -> None:
        with self.assertRaises(MailboxError):
            self.mailbox.enqueue_outbox(platform="qq", kind="private", chat_id="陌生人", text="hi")

    def test_manual_send_is_idempotent(self) -> None:
        self.mailbox.ingest(self.incoming())
        first = self.mailbox.enqueue_outbox(
            platform="qq", kind="private", chat_id="10001", text="手写一条",
            client_request_id="req-1",
        )
        second = self.mailbox.enqueue_outbox(
            platform="qq", kind="private", chat_id="10001", text="手写一条",
            client_request_id="req-1",
        )
        self.assertFalse(second.created)
        self.assertEqual(first.outbox_id, second.outbox_id)


if __name__ == "__main__":
    unittest.main()
