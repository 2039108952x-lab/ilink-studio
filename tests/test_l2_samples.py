"""L2 接续与隔离的固定样本集（PRD §4.4，AC-12 / AC-13 / AC-14 / AC-15）。

正样本 20 条：QQ→抖音 10 条、抖音→QQ 10 条，覆盖指代、约定、数字、否定与上下文变更。
每条样本都要求 prompt 里能读到另一平台的那句话，同时读不到「同昵称的另一个 person」
的秘密 —— 后者是隔离负样本的常驻对照。

负样本 10 条：未绑定、同昵称、群聊、评论分支、撤销后、provisional、白名单外等，
要求 0 条跨人泄露。

这里验证的是「记忆装配」这一层：上下文有没有进 prompt、有没有串人。
模型最终答得好不好需要真机人工评分（PRD §4.4 的 18/20 由人工打分，脚本不冒充）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.identity import IdentityStore
from gateway.mailbox import Incoming, Mailbox
from gateway.memory import MemoryStore
from gateway.models import PromptContext
from gateway.prompt import PromptAssembler

NOW = 1_800_000_000.0

# (场景, 另一平台, 那边的原话, 当前平台, 当前这句, 期望在 prompt 里读到的关键词)
POSITIVE: tuple[tuple[str, str, str, str, str, tuple[str, ...]], ...] = (
    ("指代", "qq", "我爸明天来南昌，我去接他", "douyin", "他来几天", ("爸",)),
    ("约定", "qq", "周五晚上一起吃饭？说好了", "douyin", "几点见", ("周五",)),
    ("数字", "qq", "房间号是 302，别走错", "douyin", "哪一间", ("302",)),
    ("否定", "qq", "我不吃辣，别点辣的", "douyin", "点啥好", ("不吃辣",)),
    ("上下文变更", "qq", "改成周六了，周五没空", "douyin", "那周五还去吗", ("周六",)),
    ("人名", "qq", "我妹叫小雨，今年高三", "douyin", "她多大", ("小雨",)),
    ("地点", "qq", "到南昌西站接你", "douyin", "哪个站", ("南昌西站",)),
    ("时间", "qq", "明早八点出发", "douyin", "几点走", ("八点",)),
    ("金额", "qq", "先转你 200，剩下的回头说", "douyin", "多少钱", ("200",)),
    ("偏好", "qq", "我只喝冰美式，不加糖", "douyin", "喝什么", ("冰美式",)),
    ("指代", "douyin", "我妈下周来这边", "qq", "她住哪", ("妈",)),
    ("约定", "douyin", "周六上午一起去取快递", "qq", "几点", ("周六",)),
    ("数字", "douyin", "取件码 8871", "qq", "取件码多少", ("8871",)),
    ("否定", "douyin", "不坐地铁，太挤", "qq", "怎么去", ("不坐地铁",)),
    ("上下文变更", "douyin", "见面地点改到图书馆", "qq", "在哪见", ("图书馆",)),
    ("人名", "douyin", "同事老周帮的忙", "qq", "谁帮的", ("老周",)),
    ("地点", "douyin", "在红谷滩那家店", "qq", "哪家店", ("红谷滩",)),
    ("时间", "douyin", "下午三点开", "qq", "几点", ("三点",)),
    ("金额", "douyin", "票价 480 一张", "qq", "多少钱", ("480",)),
    ("偏好", "douyin", "我不喝奶茶，喝茶", "qq", "喝啥", ("不喝奶茶",)),
)


class _Harness:
    """一个样本一个临时库：干净、可重复。"""

    def __init__(self, name: str) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mailbox = Mailbox(self.root / "mailbox.db", clock=lambda: NOW)
        self.qq = self.mailbox.ensure_account("qq", "onebot")
        self.douyin = self.mailbox.ensure_account("douyin", "rpa")
        self.memory = MemoryStore(self.mailbox, clock=lambda: NOW)
        self.identities = IdentityStore(self.mailbox, clock=lambda: NOW)
        self.name = name

    def close(self) -> None:
        self.mailbox.close()
        self._tmp.cleanup()

    def account(self, platform: str) -> int:
        return self.qq if platform == "qq" else self.douyin

    def ingest(self, platform: str, chat: str, text: str, *, msg_id: str,
               kind: str = "private", account: int | None = None,
               sender: str = "", id_kind: str = "stable"):
        return self.mailbox.ingest(Incoming(
            account_id=account if account is not None else self.account(platform),
            platform=platform, kind=kind, chat_id=chat,
            sender_id=sender or chat, sender_name="神", id_kind=id_kind, text=text,
            platform_msg_id=msg_id, occurred_at=NOW, time_source="platform",
        ))

    def prompt(self, thread_id: int, message_id: int, person_id: int,
               platform: str, text: str, *, kind: str = "private") -> str:
        ctx = PromptContext(
            thread_id=thread_id, message_id=message_id, person_id=person_id,
            platform=platform, kind=kind, chat_id="u1", sender_name="神", person_name="神",
        )
        return PromptAssembler(self.memory).build(ctx, text).text


class TestPositiveSamples(unittest.TestCase):
    def test_twenty_samples_keep_cross_platform_context_without_leaking(self) -> None:
        passed = 0
        failures: list[str] = []
        for index, (scene, side_platform, side_text, current_platform,
                    current_text, expects) in enumerate(POSITIVE):
            secret = f"另一个人的秘密编号{index}"
            harness = _Harness(f"positive-{index}")
            try:
                side = harness.ingest(side_platform, "u1", side_text, msg_id=f"s{index}")
                current = harness.ingest(current_platform, "u1", current_text, msg_id=f"c{index}")
                # 同昵称的第三个人（另一个 uid），不能因为同名就被带进上下文
                harness.ingest("qq", "u9", f"我的{secret}", msg_id=f"x{index}")
                harness.identities.merge(current.person_id, side.person_id)
                text = harness.prompt(side.thread_id, current.message_id, side.person_id,
                                      current_platform, current_text)
            finally:
                harness.close()
            missing = [keyword for keyword in expects if keyword not in text]
            leaked = secret in text
            if missing or leaked:
                failures.append(
                    f"{scene}: 缺 {missing} 泄露={leaked}"
                )
            else:
                passed += 1
        self.assertGreaterEqual(passed, 18, f"PRD 要求至少 18/20，实际 {passed}/20：{failures}")
        self.assertEqual(failures, [], "确定性拼装下应该 20/20")


class TestNegativeSamples(unittest.TestCase):
    def check(self, harness: _Harness, text: str, secret: str, label: str) -> None:
        self.assertNotIn(secret, text, f"{label} 泄露了不该出现的内容")

    def test_unbound_same_nickname(self) -> None:
        harness = _Harness("unbound")
        try:
            ack = harness.ingest("qq", "u1", "普通消息", msg_id="q1")
            secret = "未绑定的秘密口令"
            harness.ingest("qq", "u2", secret, msg_id="q2")      # 同昵称，另一个 uid
            current = harness.ingest("douyin", "u3", "换个平台问", msg_id="d1")
            text = harness.prompt(ack.thread_id, current.message_id, ack.person_id,
                                  "douyin", "换个平台问")
            self.check(harness, text, secret, "同昵称未绑定")
        finally:
            harness.close()

    def test_group_message_never_enters_private_prompt(self) -> None:
        harness = _Harness("group")
        try:
            private = harness.ingest("qq", "u1", "私聊消息", msg_id="q1")
            secret = "群里的秘密"
            harness.ingest("qq", "g1", secret, msg_id="g1", kind="group")
            text = harness.prompt(private.thread_id, private.message_id, private.person_id,
                                  "qq", "私聊消息")
            self.check(harness, text, secret, "群聊")
        finally:
            harness.close()

    def test_comment_branch_never_enters_private_prompt(self) -> None:
        harness = _Harness("comment")
        try:
            private = harness.ingest("qq", "u1", "私聊消息", msg_id="q1")
            secret = "评论区的秘密"
            harness.ingest("bilibili", "12:0:34", secret, msg_id="c1", kind="comment")
            text = harness.prompt(private.thread_id, private.message_id, private.person_id,
                                  "qq", "私聊消息")
            self.check(harness, text, secret, "评论分支")
        finally:
            harness.close()

    def test_other_person_private_never_leaks(self) -> None:
        harness = _Harness("other")
        try:
            mine = harness.ingest("qq", "u1", "我的消息", msg_id="q1")
            secret = "另一个人的私聊秘密"
            harness.ingest("qq", "u2", secret, msg_id="q2")
            text = harness.prompt(mine.thread_id, mine.message_id, mine.person_id,
                                  "qq", "我的消息")
            self.check(harness, text, secret, "他人私聊")
        finally:
            harness.close()

    def test_comment_branches_stay_separate(self) -> None:
        harness = _Harness("branches")
        try:
            first = harness.ingest("bilibili", "1:0:2", "第一条评论", msg_id="c1", kind="comment")
            secret = "另一条分支的秘密"
            harness.ingest("bilibili", "1:0:9", secret, msg_id="c2", kind="comment")
            text = harness.prompt(first.thread_id, first.message_id, 0, "bilibili",
                                  "第一条评论", kind="comment")
            self.check(harness, text, secret, "评论分支间")
        finally:
            harness.close()

    def test_two_groups_stay_separate(self) -> None:
        harness = _Harness("groups")
        try:
            first = harness.ingest("qq", "g1", "一群消息", msg_id="g1", kind="group")
            secret = "二群的秘密"
            harness.ingest("qq", "g2", secret, msg_id="g2", kind="group")
            text = harness.prompt(first.thread_id, first.message_id, 0, "qq",
                                  "一群消息", kind="group")
            self.check(harness, text, secret, "群之间")
        finally:
            harness.close()

    def test_reverted_merge_stops_sharing(self) -> None:
        harness = _Harness("reverted")
        try:
            side = harness.ingest("qq", "u1", "QQ 的私房话", msg_id="q1")
            current = harness.ingest("douyin", "u1", "抖音问一句", msg_id="d1")
            merged = harness.identities.merge(current.person_id, side.person_id)
            harness.identities.revert(merged["merge_id"])
            text = harness.prompt(current.thread_id, current.message_id, current.person_id,
                                  "douyin", "抖音问一句")
            self.assertNotIn("QQ 的私房话", text, "撤销合并后不该再共享上下文")
        finally:
            harness.close()

    def test_provisional_identity_body_is_not_stored(self) -> None:
        harness = _Harness("provisional")
        try:
            secret = "只有昵称的人说的话"
            harness.mailbox.record_rejected(Incoming(
                account_id=harness.qq, platform="qq", kind="private", chat_id="nick",
                sender_id="nick", sender_name="同昵称", id_kind="provisional",
                text=secret, platform_msg_id="n1",
            ), reason="provisional_identity")
            mine = harness.ingest("qq", "u1", "我的消息", msg_id="q1")
            text = harness.prompt(mine.thread_id, mine.message_id, mine.person_id,
                                  "qq", "我的消息")
            self.check(harness, text, secret, "provisional")
        finally:
            harness.close()

    def test_rejected_stranger_body_is_not_stored(self) -> None:
        harness = _Harness("rejected")
        try:
            secret = "陌生人的口令"
            harness.mailbox.record_rejected(Incoming(
                account_id=harness.qq, platform="qq", kind="private", chat_id="stranger",
                sender_id="stranger", sender_name="陌生人", id_kind="stable",
                text=secret, platform_msg_id="s1",
            ))
            mine = harness.ingest("qq", "u1", "我的消息", msg_id="q1")
            text = harness.prompt(mine.thread_id, mine.message_id, mine.person_id,
                                  "qq", "我的消息")
            self.check(harness, text, secret, "白名单外")
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
