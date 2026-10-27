"""通知：URL 脱敏（query + 路径）、四种通道、失败如实上报。

历史：这个文件早先测的是函数式 ``notify()`` / ``redact_url()`` / ``summarize()``。
``notify.py`` 统一成 ``Notifier`` + ``Notice`` + ``round_notice`` 之后，这里跟着对齐新契约，
并补上两条真实缺口：**路径里的 token** 与 **异常消息里夹带的 URL**。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.radar import Candidate
from gateway.radar import notify as notify_mod
from gateway.radar.pipeline import Judgment, Pick, RoundResult


def make_round(*, with_pick: bool = True, note: str = "") -> RoundResult:
    now = 1_700_000_000.0
    picked: list[Pick] = []
    if with_pick:
        candidate = Candidate(platform="douyin", item_id="111",
                              title="加班到三点回家猫以为我死了",
                              url="https://www.douyin.com/video/111")
        judgment = Judgment(item_id="111", worth=True, confidence=0.9,
                            reason="评论里全是同款经历", tags=["养猫"])
        picked.append(Pick(candidate=candidate, score=1.0, judgment=judgment))
    return RoundResult(platform="douyin", started_at=now, finished_at=now + 3.0,
                       seen=5, dropped={"no_desc": 1}, picked=picked, note=note)


class MaskUrlTest(unittest.TestCase):
    def test_query_secrets_are_masked(self) -> None:
        safe = notify_mod.mask_url("https://qyapi.weixin.qq.com/hook?key=SECRET123&x=1")
        self.assertNotIn("SECRET123", safe)
        self.assertIn("key=***", safe)
        self.assertIn("x=1", safe)

    def test_path_token_is_masked(self) -> None:
        """飞书这类 webhook 把凭据放在路径里，只扫 query 会漏掉一半。"""
        safe = notify_mod.mask_url(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef0123456789abcdef")
        self.assertNotIn("abcdef0123456789abcdef", safe)
        self.assertTrue(safe.endswith("/***"))

    def test_share_sign_and_did_are_masked(self) -> None:
        safe = notify_mod.mask_url(
            "https://www.iesdouyin.com/share/video/1/?share_sign=abc&did=xyz&u_code=uu")
        for secret in ("share_sign=abc", "did=xyz", "u_code=uu"):
            self.assertNotIn(secret, safe)

    def test_empty_url(self) -> None:
        self.assertEqual(notify_mod.mask_url(""), "")


class RoundNoticeTest(unittest.TestCase):
    def test_names_the_best_pick(self) -> None:
        notice = notify_mod.round_notice(make_round())
        self.assertIn("1", notice.title)
        self.assertIn("加班到三点", notice.body)
        self.assertEqual(notice.kind, "round")

    def test_nothing_picked_says_why(self) -> None:
        notice = notify_mod.round_notice(
            make_round(with_pick=False, note="模型判断失败（BrainUnavailable）"))
        self.assertIn("没够格", notice.title)
        self.assertIn("模型判断失败", notice.body)

    def test_payload_url_is_masked(self) -> None:
        notice = notify_mod.round_notice(make_round(), report_path=Path("x.md"))
        notice.url = "https://example.invalid/v?token=SECRET"
        self.assertNotIn("SECRET", json.dumps(notice.payload(), ensure_ascii=False))


class ChannelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _entries(self) -> list[dict]:
        files = sorted((self.dir / "notifications").glob("*.json"))
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]

    async def test_file_channel_writes_one_entry(self) -> None:
        notifier = notify_mod.Notifier(self.dir, channel="file")
        result = await notifier.send(notify_mod.round_notice(make_round()))
        self.assertTrue(result["ok"])
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["deliver"], "file")
        self.assertEqual(entries[0]["kind"], "round")

    async def test_toast_is_marked_for_electron(self) -> None:
        notifier = notify_mod.Notifier(self.dir, channel="toast")
        await notifier.send(notify_mod.round_notice(make_round()))
        self.assertEqual(self._entries()[0]["deliver"], "toast")

    async def test_both_writes_two_entries(self) -> None:
        notifier = notify_mod.Notifier(self.dir, channel="both")
        await notifier.send(notify_mod.round_notice(make_round()))
        self.assertEqual(len(self._entries()), 2)

    async def test_unknown_channel_falls_back_to_file(self) -> None:
        notifier = notify_mod.Notifier(self.dir, channel="sms")
        self.assertEqual(notifier.channel, "file")

    async def test_webhook_posts_masked_payload(self) -> None:
        seen: list[dict] = []

        class FakeResponse:
            status_code = 200

        class FakeClient:
            async def post(self, url, json):
                seen.append(json)
                return FakeResponse()

        notifier = notify_mod.Notifier(self.dir, channel="webhook",
                                       webhook_url="https://example.invalid/hook?key=SECRET",
                                       http=FakeClient())
        result = await notifier.send(notify_mod.round_notice(make_round()))
        self.assertTrue(result["ok"])
        self.assertEqual(len(seen), 1)
        self.assertNotIn("SECRET", json.dumps(seen[0], ensure_ascii=False))

    async def test_webhook_failure_is_honest_and_masked(self) -> None:
        class FakeClient:
            async def post(self, url, json):
                raise TimeoutError("boom https://example.invalid/hook?key=SECRET")

        notifier = notify_mod.Notifier(self.dir, channel="webhook",
                                       webhook_url="https://example.invalid/hook?key=SECRET",
                                       http=FakeClient())
        result = await notifier.send(notify_mod.round_notice(make_round()))
        self.assertFalse(result["ok"])
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))

    async def test_webhook_without_url_fails_softly(self) -> None:
        notifier = notify_mod.Notifier(self.dir, channel="webhook", webhook_url="")
        result = await notifier.send(notify_mod.round_notice(make_round()))
        self.assertFalse(result["ok"])
        self.assertIn("没配", result["parts"]["webhook"]["error"])


if __name__ == "__main__":
    unittest.main()
