"""控制面里的多平台分发：``/api/v1/radar/<平台>/<动作>``。

这块的风险不在「能不能跑」，而在**会不会把 B 站的请求派给抖音**——
派错了，两个平台的报告、配额、发送闸会互相污染。所以逐条钉死：
谁被选中、不指定平台时回落到谁、没启用时怎么报错。
"""

from __future__ import annotations

import unittest
from typing import Any

from gateway.api import APIError, ConsoleAPI


class FakeService:
    """只记调用，不真跑。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, dict]] = []

    def status(self) -> dict[str, Any]:
        self.calls.append(("status", {}))
        return {"platform": self.name}

    def snapshot(self) -> dict[str, Any]:
        self.calls.append(("snapshot", {}))
        return {"picked": []}

    def videos(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("videos", kw))
        return {"items": [], "platform": self.name}

    def doctor(self) -> dict[str, Any]:
        self.calls.append(("doctor", {}))
        return {"ok": True, "platform": self.name}


def make_api(radars: Any) -> ConsoleAPI:
    """绕开 __init__：这里只测分发，不需要账号库和保险库。"""
    api = object.__new__(ConsoleAPI)
    api.radars = api._normalize_radars(radars)
    return api


class NormalizeTest(unittest.TestCase):
    def test_none_is_empty(self) -> None:
        self.assertEqual(make_api(None).radars, {})

    def test_single_instance_means_douyin(self) -> None:
        """老用法（直接传一个实例）等价于抖音 —— 不能让既有部署失效。"""
        service = FakeService("douyin")
        self.assertEqual(make_api(service).radars, {"douyin": service})

    def test_dict_is_kept_and_none_dropped(self) -> None:
        douyin, bili = FakeService("douyin"), FakeService("bilibili")
        api = make_api({"douyin": douyin, "bilibili": bili, "qq": None})
        self.assertEqual(set(api.radars), {"douyin", "bilibili"})


class SelectTest(unittest.TestCase):
    def test_nothing_enabled_is_503(self) -> None:
        with self.assertRaises(APIError) as ctx:
            make_api(None)._radar()
        self.assertEqual(ctx.exception.status, 503)

    def test_default_prefers_douyin(self) -> None:
        douyin, bili = FakeService("douyin"), FakeService("bilibili")
        api = make_api({"douyin": douyin, "bilibili": bili})
        self.assertIs(api._radar(), douyin)

    def test_single_platform_is_used_as_default(self) -> None:
        bili = FakeService("bilibili")
        self.assertIs(make_api({"bilibili": bili})._radar(), bili)

    def test_ambiguous_without_douyin_is_400(self) -> None:
        api = make_api({"bilibili": FakeService("bilibili"), "qq": FakeService("qq")})
        with self.assertRaises(APIError) as ctx:
            api._radar()
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("bilibili", str(ctx.exception))

    def test_explicit_platform(self) -> None:
        douyin, bili = FakeService("douyin"), FakeService("bilibili")
        api = make_api({"douyin": douyin, "bilibili": bili})
        self.assertIs(api._radar("bilibili"), bili)

    def test_disabled_platform_is_503(self) -> None:
        api = make_api({"douyin": FakeService("douyin")})
        with self.assertRaises(APIError) as ctx:
            api._radar("bilibili")
        self.assertEqual(ctx.exception.status, 503)


class RouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.douyin = FakeService("douyin")
        self.bili = FakeService("bilibili")
        self.api = make_api({"douyin": self.douyin, "bilibili": self.bili})

    def test_platform_prefix_routes_to_bilibili(self) -> None:
        status, payload = self.api._radar_route("GET", ["bilibili", "status"], None, {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["platform"], "bilibili")
        self.assertEqual(self.douyin.calls, [])       # 抖音一次都没被碰
        self.assertEqual(self.bili.calls, [("status", {})])

    def test_bare_path_still_goes_to_douyin(self) -> None:
        _, payload = self.api._radar_route("GET", ["status"], None, {})
        self.assertEqual(payload["platform"], "douyin")
        self.assertEqual(self.bili.calls, [])

    def test_videos_passes_filters_through(self) -> None:
        """档位/状态/分页要原样传给 provider 侧，别在路由层丢掉。"""
        query = {"grade": ["值得看"], "status": ["挑了"], "page": ["2"], "size": ["10"]}
        self.api._radar_route("GET", ["bilibili", "videos"], None, query)
        self.assertEqual(self.bili.calls, [("videos", {
            "day": "", "grade": "值得看", "status": "挑了", "page": "2", "size": "10",
        })])

    def test_videos_defaults_when_no_query(self) -> None:
        self.api._radar_route("GET", ["bilibili", "videos"], None, {})
        _, kwargs = self.bili.calls[0]
        self.assertEqual(kwargs["page"], "1")
        self.assertEqual(kwargs["size"], "20")

    def test_unknown_action_is_404(self) -> None:
        with self.assertRaises(APIError) as ctx:
            self.api._radar_route("GET", ["bilibili", "nope"], None, {})
        self.assertEqual(ctx.exception.status, 404)

    def test_platform_name_alone_is_not_an_action(self) -> None:
        """``/radar/bilibili`` 后面没跟动作 —— 别把它当动作派给抖音。"""
        with self.assertRaises(APIError) as ctx:
            self.api._radar_route("GET", ["bilibili"], None, {})
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
