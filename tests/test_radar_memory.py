"""记忆映射：插件 memory/*.json → MemoryStore.add_fact，不另造存储。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.radar import memory as memory_mod


class FakeFact:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeSink:
    def __init__(self, existing: list[str] | None = None) -> None:
        self.existing = [FakeFact(text) for text in (existing or [])]
        self.added: list[tuple[int, str, str, float]] = []

    def facts(self, person_id: int, *, status: str | None = "active", limit: int = 50):
        return list(self.existing)

    def add_fact(self, person_id: int, kind: str, content: str, *, confidence: float = 0.7, **kwargs):
        self.added.append((person_id, kind, content, confidence))
        self.existing.append(FakeFact(content))
        return len(self.added)


class SafeKeyTest(unittest.TestCase):
    def test_matches_plugin_rule(self) -> None:
        raw = "MS4wLjABAAAA9u05z66ziy8dhL0O4NLPm3--BFUimHgA5Zv1iBeDNzWRYUdBZG_ldFFMAJjXyUz_"
        self.assertEqual(memory_mod.safe_key(raw), raw[:64])

    def test_strips_illegal_chars(self) -> None:
        self.assertEqual(memory_mod.safe_key("a/b:c*d"), "abcd")

    def test_empty_becomes_unknown(self) -> None:
        self.assertEqual(memory_mod.safe_key(""), "unknown")


class ReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "memory").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, payload: dict) -> None:
        (self.home / "memory" / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(memory_mod.read_dm_memory(self.home, "nobody"))

    def test_read_dm_memory(self) -> None:
        self._write("dm-abc123.json", {
            "who": {"sec_uid": "abc123", "nick": "demo_friend"},
            "rounds": 3,
            "facts": ["在杭州", "养了只橘猫"],
            "messages": [{"from": "them", "text": "在吗", "kind": "text"}],
        })
        raw = memory_mod.read_dm_memory(self.home, "abc123")
        context = memory_mod.context_of(raw)
        self.assertEqual(context["who"]["nick"], "demo_friend")
        self.assertEqual(context["rounds"], 3)
        self.assertEqual(context["facts"], ["在杭州", "养了只橘猫"])
        self.assertEqual(context["recent"][0]["text"], "在吗")
        self.assertEqual(context["total_remembered"], 1)

    def test_corrupt_file_is_ignored(self) -> None:
        (self.home / "memory" / "dm-bad.json").write_text("{oops", encoding="utf-8")
        self.assertIsNone(memory_mod.read_dm_memory(self.home, "bad"))

    def test_context_of_none_is_empty(self) -> None:
        context = memory_mod.context_of(None)
        self.assertEqual(context["facts"], [])
        self.assertEqual(context["recent"], [])


class SyncTest(unittest.TestCase):
    def test_new_facts_are_added(self) -> None:
        sink = FakeSink()
        raw = {"who": {"nick": "x"}, "facts": ["搬到成都了", "猫叫豆豆"]}
        result = memory_mod.sync_facts(sink, 7, raw, confidence=0.6)
        self.assertEqual(result["added"], 2)
        self.assertEqual([item[1] for item in sink.added], ["profile", "profile"])
        self.assertEqual(sink.added[0][3], 0.6)

    def test_duplicates_are_skipped(self) -> None:
        sink = FakeSink(existing=["搬到成都了"])
        raw = {"facts": ["搬到成都了", " 搬到成都了 ", "猫叫豆豆"]}
        result = memory_mod.sync_facts(sink, 7, raw)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["contents"], ["猫叫豆豆"])

    def test_empty_facts_add_nothing(self) -> None:
        sink = FakeSink()
        self.assertEqual(memory_mod.sync_facts(sink, 1, None)["added"], 0)

    def test_add_failure_does_not_break_round(self) -> None:
        class BrokenSink(FakeSink):
            def add_fact(self, person_id, kind, content, **kwargs):
                raise RuntimeError("db locked")

        result = memory_mod.sync_facts(BrokenSink(), 1, {"facts": ["a", "b"]})
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["planned"], 2)


class RememberTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_appends_to_plugin_memory(self) -> None:
        result = memory_mod.remember_from_message(self.home, "sec1", "在吗")
        self.assertTrue(result["ok"])
        raw = memory_mod.read_dm_memory(self.home, "sec1")
        self.assertEqual(raw["messages"][-1]["text"], "在吗")
        self.assertEqual(raw["who"]["sec_uid"], "sec1")

    def test_keeps_existing_facts(self) -> None:
        (self.home / "memory").mkdir(parents=True)
        (self.home / "memory" / "dm-sec2.json").write_text(
            json.dumps({"facts": ["养猫"], "messages": []}), encoding="utf-8")
        memory_mod.remember_from_message(self.home, "sec2", "你好")
        raw = memory_mod.read_dm_memory(self.home, "sec2")
        self.assertEqual(raw["facts"], ["养猫"])


if __name__ == "__main__":
    unittest.main()
