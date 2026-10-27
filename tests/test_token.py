"""共享 token 的测试：环境变量 > 文件 > 自动生成。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway import token as token_mod


class TestEnsureToken(unittest.TestCase):
    def test_env_var_wins(self) -> None:
        with mock.patch.dict(os.environ, {token_mod.DEFAULT_ENV: "from-env"}):
            self.assertEqual(token_mod.ensure_token(), "from-env")

    def test_env_var_ignores_whitespace(self) -> None:
        with mock.patch.dict(os.environ, {token_mod.DEFAULT_ENV: "  spaced  "}):
            self.assertEqual(token_mod.ensure_token(), "spaced")

    def test_generates_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {token_mod.DEFAULT_ENV: ""}
        ):
            root = Path(d)
            first = token_mod.ensure_token(root=root)
            self.assertTrue(first)
            self.assertGreaterEqual(len(first), 32)
            saved = token_mod.token_path(root)
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.read_text(encoding="utf-8"), first)

    def test_reuses_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {token_mod.DEFAULT_ENV: ""}
        ):
            root = Path(d)
            path = token_mod.token_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preexisting-token", encoding="utf-8")

            self.assertEqual(token_mod.ensure_token(root=root), "preexisting-token")
            # 没被覆盖
            self.assertEqual(path.read_text(encoding="utf-8"), "preexisting-token")

    def test_blank_file_is_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {token_mod.DEFAULT_ENV: ""}
        ):
            root = Path(d)
            path = token_mod.token_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("   \n", encoding="utf-8")

            value = token_mod.ensure_token(root=root)
            self.assertTrue(value.strip())
            self.assertNotEqual(value, "   \n")

    def test_two_calls_agree(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {token_mod.DEFAULT_ENV: ""}
        ):
            root = Path(d)
            self.assertEqual(token_mod.ensure_token(root=root), token_mod.ensure_token(root=root))

    def test_custom_env_name(self) -> None:
        with mock.patch.dict(os.environ, {"MY_OWN_TOKEN": "custom"}):
            self.assertEqual(token_mod.ensure_token("MY_OWN_TOKEN"), "custom")

    def test_token_is_urlsafe(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(
            os.environ, {token_mod.DEFAULT_ENV: ""}
        ):
            value = token_mod.ensure_token(root=Path(d))
            allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
            self.assertTrue(set(value) <= allowed, f"出现了意外的字符：{value}")


if __name__ == "__main__":
    unittest.main()
