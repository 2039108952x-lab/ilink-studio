"""刷B站的设置层：config 给默认值、界面改覆盖层。

项目铁律里 ``[platforms.*]`` 是只读段，界面改不了 config.toml。所以这里的重点
不是「能不能存」而是：**白名单之外一个键都不许进、非法值必须被拒**、
以及「config 改了默认值之后，用户没动过的项要跟着变」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.radar import bili_settings


class DefaultsTest(unittest.TestCase):
    def test_empty_options(self) -> None:
        defaults = bili_settings.defaults_from_config(None)
        self.assertEqual(defaults["per_day"], 5)
        self.assertEqual(defaults["dm_mode"], "only")      # 上游 2.0 的默认：只发私信
        self.assertEqual(defaults["report_style"], "full")
        self.assertFalse(defaults["comment_auto"])         # 拿你的号说话，默认关
        self.assertFalse(defaults["reply_auto"])
        self.assertEqual(defaults["peaks"], bili_settings.DEFAULT_PEAKS)

    def test_config_values_win(self) -> None:
        defaults = bili_settings.defaults_from_config({
            "per_day": 3, "keywords": ["大模型", "拆机"], "dm_mode": "both",
            "comment_auto": True, "persona": "像个话不多的老用户",
            "mcp_timeout": 300,
        })
        self.assertEqual(defaults["per_day"], 3)
        self.assertEqual(defaults["keywords"], ["大模型", "拆机"])
        self.assertEqual(defaults["dm_mode"], "both")
        self.assertTrue(defaults["comment_auto"])
        self.assertEqual(defaults["persona"], "像个话不多的老用户")
        self.assertEqual(defaults["mcp_timeout"], 300)

    def test_bad_enum_falls_back(self) -> None:
        defaults = bili_settings.defaults_from_config({"dm_mode": "乱写的",
                                                       "report_style": "糊的"})
        self.assertEqual(defaults["dm_mode"], "only")
        self.assertEqual(defaults["report_style"], "full")


class OverrideTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_and_merge(self) -> None:
        bili_settings.write_override(self.home, {"per_day": 9, "comment_auto": True})
        merged = bili_settings.effective(self.home, {"per_day": 3})
        self.assertEqual(merged["per_day"], 9)          # 覆盖层优先
        self.assertTrue(merged["comment_auto"])
        self.assertEqual(merged["dm_mode"], "only")     # 没动过的项跟着 config 走

    def test_unknown_keys_are_dropped(self) -> None:
        """白名单外的键静默忽略 —— 别让人从界面往配置里塞任意东西。"""
        bili_settings.write_override(self.home, {"bogus": 1, "per_day": 4})
        raw = json.loads((self.home / "radar-settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("bogus", raw)
        self.assertEqual(raw["per_day"], 4)

    def test_invalid_enum_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            bili_settings.write_override(self.home, {"dm_mode": "wrong"})
        self.assertIn("投递方式", str(ctx.exception))

    def test_int_is_clamped(self) -> None:
        bili_settings.write_override(self.home, {"comment_per_round": 999})
        merged = bili_settings.effective(self.home, None)
        self.assertEqual(merged["comment_per_round"], 20)   # 上限 20

    def test_keywords_accept_lines_or_commas(self) -> None:
        bili_settings.write_override(self.home, {"keywords": "拆机\n装机, 测评"})
        merged = bili_settings.effective(self.home, None)
        self.assertEqual(merged["keywords"], ["拆机", "装机", "测评"])

    def test_corrupt_file_is_treated_as_empty(self) -> None:
        path = self.home / "radar-settings.json"
        path.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(bili_settings.read_override(self.home), {})
        # 但默认值不能被一起吞掉
        self.assertEqual(bili_settings.effective(self.home, {"per_day": 2})["per_day"], 2)


class ViewTest(unittest.TestCase):
    def test_four_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = bili_settings.view(Path(tmp), None)
        self.assertEqual(set(view["tabs"]), {"daily", "preference", "interact", "advanced"})
        self.assertEqual(view["tab_labels"]["interact"], "互动")

    def test_every_tab_has_labelled_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = bili_settings.view(Path(tmp), None)
        for name, fields in view["tabs"].items():
            self.assertTrue(fields, name)
            for field in fields:
                self.assertTrue(field["label"], field["key"])
                self.assertIn(field["kind"], ("bool", "int", "list", "str"))

    def test_all_editable_keys_are_grouped(self) -> None:
        """每个可改的键都要出现在某个页签里，否则界面上永远看不到它。"""
        grouped = {key for keys in bili_settings.TABS.values() for key in keys}
        self.assertEqual(grouped, set(bili_settings.FIELDS))

    def test_override_path_is_in_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            view = bili_settings.view(Path(tmp), None)
            self.assertTrue(view["override_file"].endswith("radar-settings.json"))


if __name__ == "__main__":
    unittest.main()
