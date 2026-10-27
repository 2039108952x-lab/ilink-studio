"""M4/M5 核心验收：预算、事实抽取、事实编辑、needs_human、脱敏与设置。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from gateway.budget import BudgetGate
from gateway.config import GatewayConfig
from gateway.facts import FactExtractor, parse_facts
from gateway.logs import redact
from gateway.mailbox import Incoming, Mailbox
from gateway.memory import MemoryStore
from gateway.models import Platform, PromptContext
from gateway.prompt import PromptAssembler
from gateway.metrics import Metrics
from gateway.settings import SettingsInvalid, SettingsStore


class TestFactsParsing(unittest.TestCase):
    def test_valid_and_invalid(self) -> None:
        raw = '{"facts":[{"kind":"preference","content":"喜欢冰美式","confidence":0.95}, {"kind":"bad","content":"x"}]}'
        facts = parse_facts(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "喜欢冰美式")
        self.assertLessEqual(facts[0]["confidence"], 0.8)
        self.assertEqual(parse_facts("不是 JSON"), [])
        self.assertEqual(parse_facts('{"facts":"x"}'), [])

    def test_limit(self) -> None:
        raw = '{"facts":[' + ",".join(
            '{"kind":"event","content":"第%d件事"}' % i for i in range(9)) + "]}" 
        self.assertEqual(len(parse_facts(raw)), 5)


class TestBudget(unittest.TestCase):
    def setUp(self) -> None:
        self.metrics = Metrics()
        self.gate = BudgetGate(self.metrics)

    def test_daily_tokens_blocked_before_call(self) -> None:
        cfg = GatewayConfig()
        cfg.budget.daily_tokens = 100
        self.metrics.add_tokens(input_tokens=150, estimated=True)
        self.assertEqual(self.gate.check(cfg, "qq"), "budget_tokens_exhausted")

    def test_platform_rate(self) -> None:
        cfg = GatewayConfig()
        cfg.budget.platform_per_minute = 2
        self.assertIsNone(self.gate.check(cfg, "qq"))
        self.gate.record("qq")
        self.gate.record("qq")
        self.assertEqual(self.gate.check(cfg, "qq"), "rate_limited")
        self.assertIsNone(self.gate.check(cfg, "wechat"))

    def test_queue_full(self) -> None:
        cfg = GatewayConfig()
        cfg.budget.queue_limit = 2
        self.assertTrue(BudgetGate.queue_full(cfg, {"inbound": 3}))
        self.assertFalse(BudgetGate.queue_full(cfg, {"inbound": 1}))


class TestMemoryEdits(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account = self.mailbox.ensure_account("qq", "onebot")
        self.memory = MemoryStore(self.mailbox)
        self.result = self.mailbox.ingest(Incoming(
            account_id=self.account, platform="qq", kind="private", chat_id="u1",
            sender_id="u1", sender_name="神", id_kind="stable", text="聊天", platform_msg_id="m1"))
        self.person = self.result.person_id

    def test_has_fact_and_update(self) -> None:
        fact_id = self.memory.add_fact(self.person, "preference", "喜欢冰美式")
        self.assertTrue(self.memory.has_fact(self.person, "preference", "喜欢冰美式"))
        self.memory.update_fact(fact_id, "喜欢热美式")
        self.assertTrue(self.memory.has_fact(self.person, "preference", "喜欢热美式"))
        self.assertFalse(self.memory.has_fact(self.person, "preference", "喜欢冰美式"))
        events = [row["event_type"] for row in self.mailbox.list_audit()]
        self.assertIn("fact_edited", events)

    def test_needs_human_state(self) -> None:
        self.mailbox.set_account_connection(self.account, "needs_human", "captcha")
        with self.mailbox.read() as conn:
            row = conn.execute("SELECT connection_state,last_error_code FROM account WHERE id=?",
                               (self.account,)).fetchone()
        self.assertEqual((row[0], row[1]), ("needs_human", "captcha"))


class TestFactExtractor(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account = self.mailbox.ensure_account("qq", "onebot")
        self.memory = MemoryStore(self.mailbox)
        self.result = self.mailbox.ingest(Incoming(
            account_id=self.account, platform="qq", kind="private", chat_id="u1",
            sender_id="u1", sender_name="神", id_kind="stable", text="聊天", platform_msg_id="m1"))
        self.person = self.result.person_id

    def test_extract_writes_facts_and_dedupes(self) -> None:
        class FakeBrain:
            def __init__(self) -> None:
                self.prompts = []

            async def extract_json(self, prompt: str) -> str:
                self.prompts.append(prompt)
                return ('{"facts":[{"kind":"preference","content":"喜欢冰美式","confidence":0.9},'
                        '{"kind":"promise","content":"周五一起吃饭"}]}')

        brain = FakeBrain()
        extractor = FactExtractor(brain, self.memory)
        created = asyncio.run(extractor.extract(
            person_id=self.person, message_id=self.result.message_id, text="我周五请你吃饭，只喝冰美式"))
        self.assertEqual(len(created), 2)
        self.assertIn("我周五请你吃饭", brain.prompts[0])
        again = asyncio.run(extractor.extract(
            person_id=self.person, message_id=self.result.message_id, text="再说一遍"))
        self.assertEqual(again, [], "同内容事实不重复写")

    def test_frozen_fact_is_not_rewritten(self) -> None:
        frozen = self.memory.add_fact(self.person, "preference", "喜欢热美式")
        self.memory.freeze_fact(frozen, True)

        class FakeBrain:
            async def extract_json(self, prompt: str) -> str:
                return '{"facts":[{"kind":"preference","content":"喜欢冰美式"}]}'

        asyncio.run(FactExtractor(FakeBrain(), self.memory).extract(
            person_id=self.person, message_id=self.result.message_id, text="冰美式"))
        facts = {fact.id: fact for fact in self.memory.facts(self.person, status="active")}
        self.assertEqual(facts[frozen].content, "喜欢热美式")
        self.assertTrue(facts[frozen].frozen)

    def test_bad_json_writes_nothing(self) -> None:
        class FakeBrain:
            async def extract_json(self, prompt: str) -> str:
                return "我觉得他喜欢冰美式"

        self.assertEqual(asyncio.run(FactExtractor(FakeBrain(), self.memory).extract(
            person_id=self.person, message_id=self.result.message_id, text="冰美式")), [])


class TestRedactionAndSettings(unittest.TestCase):
    def test_redact(self) -> None:
        text = redact("token=abc123 cookie=xyz Authorization: Bearer qqq")
        self.assertNotIn("abc123", text)
        self.assertNotIn("xyz", text)
        self.assertNotIn("qqq", text)

    def test_readonly_section_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            store = SettingsStore(GatewayConfig(), Path(d) / "ui-settings.json")
            with self.assertRaises(SettingsInvalid):
                store.update({"server": {"host": "0.0.0.0"}})
            with self.assertRaises(SettingsInvalid):
                store.update({"ui": {"theme": "neon"}})


class TestCorrectionAndNeedsHuman(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mailbox = Mailbox(Path(self._tmp.name) / "mailbox.db")
        self.addCleanup(self.mailbox.close)
        self.account = self.mailbox.ensure_account("qq", "onebot")
        self.memory = MemoryStore(self.mailbox)
        self.result = self.mailbox.ingest(Incoming(
            account_id=self.account, platform="qq", kind="private", chat_id="u1",
            sender_id="u1", sender_name="神", id_kind="stable", text="聊天", platform_msg_id="m1"))

    def test_edit_fact_changes_next_prompt(self) -> None:
        fact_id = self.memory.add_fact(self.result.person_id, "preference", "喜欢冰美式", confidence=0.9)
        ctx = PromptContext(thread_id=self.result.thread_id, message_id=0,
                            person_id=self.result.person_id, platform="qq", kind="private",
                            chat_id="u1", sender_name="神")
        self.assertIn("喜欢冰美式", PromptAssembler(self.memory).build(ctx, "喝什么").text)
        self.memory.update_fact(fact_id, "喜欢热美式")
        after = PromptAssembler(self.memory).build(ctx, "喝什么").text
        self.assertIn("喜欢热美式", after)
        self.assertNotIn("喜欢冰美式", after)

    def test_needs_human_callback(self) -> None:
        from gateway.adapters.base import Adapter
        from gateway.models import SendResult

        class Dummy(Adapter):
            platform = Platform.QQ

            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

            async def send(self, msg):
                return SendResult(ok=True)

        adapter = Dummy()
        seen: list[str] = []
        adapter.on_needs_human = seen.append
        adapter.mark_needs_human("captcha")
        self.assertEqual(seen, ["captcha"])


if __name__ == "__main__":
    unittest.main()
