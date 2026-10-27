"""雷达服务层：状态、刷一轮、日报、候选快照、分享。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.radar import Candidate, CommentSnapshot, RadarError
from gateway.radar.provider import ShareDraft
from gateway.radar_service import RadarService


def candidate(item_id: str, **kw) -> Candidate:
    base = dict(platform="douyin", item_id=item_id,
                title="加班到三点回家猫以为我死了，它蹲在门口盯着我看",
                author="铲屎官", duration_sec=30,
                url=f"https://www.douyin.com/video/{item_id}",
                stats={"like": 8000, "comment": 300, "collect": 400, "share": 90})
    base.update(kw)
    return Candidate(**base)


class FakeProvider:
    name = "douyin"
    supports_share = True
    supports_text = True
    supports_comment = True

    def __init__(self, items=None, *, comments=None, whoami=None,
                 whoami_error: str = "") -> None:
        self._items = items if items is not None else [candidate("111")]
        self._comments = comments or {}
        self._whoami = whoami or {"ok": True, "logged_in": True, "nick": "demo_account"}
        self._whoami_error = whoami_error
        self.shares: list[tuple[ShareDraft, bool]] = []

    async def whoami(self):
        if self._whoami_error:
            raise RadarError("not_logged_in", self._whoami_error)
        return dict(self._whoami)

    async def candidates(self, *, top: int = 8):
        return list(self._items)[:top]

    async def comments(self, item_id: str, *, limit: int = 10):
        return list(self._comments.get(item_id, []))[:limit]

    async def send_share(self, draft: ShareDraft, *, confirm: bool = False) -> ShareDraft:
        self.shares.append((draft, confirm))
        return ShareDraft(item_id=draft.item_id, to=draft.to, reason=draft.reason,
                          preview=f"预览：{draft.item_id} → {draft.to}",
                          sent=bool(confirm))

    async def send_text(self, to: str, text: str, *, confirm: bool = False) -> dict:
        return {"ok": True, "sent": bool(confirm)}

    async def close(self):
        return None


class FakeBrain:
    backend_ready = True

    def __init__(self, payload: dict | str = "") -> None:
        self.payload = payload
        self.prompts: list[str] = []

    async def extract_json(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)


def service(tmp: str, provider=None, brain=None, **kw) -> RadarService:
    return RadarService(provider or FakeProvider(), Path(tmp),
                        brain=brain if brain is not None else FakeBrain(default_judgment()),
                        **kw)


def default_judgment(**over) -> dict:
    payload = {"worth": True, "confidence": 0.9,
               "reason": "评论里全是同款经历", "tags": ["宠物"]}
    payload.update(over)
    return payload


class StatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ready_when_logged_in(self) -> None:
        info = service(self.home).status()
        self.assertTrue(info["ready"])
        self.assertEqual(info["identity"]["nick"], "demo_account")
        self.assertFalse(info["has_report"])

    def test_not_ready_when_login_raises(self) -> None:
        info = service(self.home, FakeProvider(whoami_error="还没有登录")).status()
        self.assertFalse(info["ready"])
        self.assertEqual(info["identity"]["code"], "not_logged_in")

    def test_status_never_raises(self) -> None:
        """状态接口要是自己炸了，界面就没法显示为什么不能用。"""
        class Broken(FakeProvider):
            async def whoami(self):
                raise ValueError("boom")

        info = service(self.home, Broken()).status()
        self.assertFalse(info["ready"])
        self.assertIn("boom", info["identity"]["error"])


class RunRoundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_writes_report_and_consumes_quota(self) -> None:
        out = service(self.home).run_round(top=3)
        self.assertTrue(out["ok"])
        path = Path(out["report"])
        self.assertTrue(path.is_file())
        self.assertIn("加班到三点", path.read_text(encoding="utf-8"))
        self.assertEqual(out["quota"]["used"], 1)
        self.assertEqual(out["quota"]["left"], 4)

    def test_picks_land_in_candidates(self) -> None:
        out = service(self.home).run_round(top=3)
        rows = out["candidates"]["picked"]
        self.assertEqual([r["item_id"] for r in rows], ["111"])
        self.assertEqual(rows[0]["reason"], "评论里全是同款经历")
        self.assertEqual(rows[0]["url"], "https://www.douyin.com/video/111")

    def test_uncertain_when_model_says_no(self) -> None:
        brain = FakeBrain(default_judgment(worth=False, confidence=0.9))
        out = service(self.home, brain=brain).run_round(top=3)
        self.assertEqual(out["candidates"]["picked"], [])
        self.assertEqual([r["item_id"] for r in out["candidates"]["uncertain"]], ["111"])

    def test_nothing_survives_hard_filter(self) -> None:
        cold = candidate("222", stats={"like": 10, "comment": 0, "collect": 0, "share": 0})
        out = service(self.home, FakeProvider([cold])).run_round(top=3)
        self.assertEqual(out["candidates"]["picked"], [])
        self.assertIn("硬筛", out["summary"]["note"])

    def test_second_round_is_rejected_while_running(self) -> None:
        svc = service(self.home)
        svc._running = True
        with self.assertRaises(RadarError) as ctx:
            svc.run_round()
        self.assertEqual(ctx.exception.code, "busy")


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_back_after_a_round(self) -> None:
        svc = service(self.home)
        svc.run_round(top=3)
        doc = svc.report()
        self.assertIn("加班到三点", doc["markdown"])
        self.assertTrue(doc["path"].endswith(".md"))

    def test_missing_day_reads_empty_not_crash(self) -> None:
        self.assertEqual(service(self.home).report("1999-01-01")["markdown"], "")

    def test_snapshot_is_empty_before_first_round(self) -> None:
        snap = service(self.home).snapshot()
        self.assertEqual(snap["picked"], [])
        self.assertEqual(snap["seen"], 0)


class ShareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_preview_does_not_send(self) -> None:
        provider = FakeProvider()
        out = service(self.home, provider).share(item_id="111", to="demo_account", reason="好笑")
        self.assertFalse(out["sent"])
        self.assertIn("预览", out["preview"])
        self.assertEqual(provider.shares[0][1], False)

    def test_confirm_sends(self) -> None:
        provider = FakeProvider()
        out = service(self.home, provider).share(
            item_id="111", to="demo_account", reason="好笑", confirm=True)
        self.assertTrue(out["sent"])
        self.assertEqual(provider.shares[0][1], True)


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_without_provider_support(self) -> None:
        self.assertFalse(service(self.home).doctor()["ok"])

    def test_reports_items(self) -> None:
        class WithDoctor(FakeProvider):
            async def doctor(self):
                return {"ok": True, "items": [
                    {"item": "Node", "state": "ok", "detail": "24.16.0", "fix": ""},
                    {"item": "Cookie（登录态）", "state": "ok", "detail": "已保存", "fix": ""},
                ]}

        out = service(self.home, WithDoctor()).doctor()
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"][0]["detail"], "24.16.0")


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults(self) -> None:
        out = service(self.home).settings()
        self.assertEqual(out["top"], 8)
        self.assertEqual(out["notify"], "file")
        self.assertFalse(out["has_webhook"])

    def test_save_and_read_back(self) -> None:
        svc = service(self.home)
        out = svc.save_settings({"top": 3, "notify": "toast"})
        self.assertEqual(out["top"], 3)
        self.assertEqual(out["notify"], "toast")
        self.assertEqual(service(self.home).settings()["top"], 3)

    def test_top_is_clamped(self) -> None:
        svc = service(self.home)
        self.assertEqual(svc.save_settings({"top": 999})["top"], 30)
        self.assertEqual(svc.save_settings({"top": 0})["top"], 1)

    def test_bad_notify_channel_is_rejected(self) -> None:
        with self.assertRaises(RadarError) as ctx:
            service(self.home).save_settings({"notify": "sms"})
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_webhook_must_be_http(self) -> None:
        with self.assertRaises(RadarError):
            service(self.home).save_settings({"webhook_url": "file:///etc/passwd"})

    def test_webhook_is_masked_on_the_way_out(self) -> None:
        svc = service(self.home)
        out = svc.save_settings({"webhook_url": "https://example.invalid/hook?key=SECRET"})
        self.assertTrue(out["has_webhook"])
        self.assertNotIn("SECRET", out["webhook_url"])

    def test_run_round_uses_saved_top(self) -> None:
        svc = service(self.home)
        svc.save_settings({"top": 1})
        self.assertEqual(svc.run_round()["candidates"]["seen"], 1)


if __name__ == "__main__":
    unittest.main()
