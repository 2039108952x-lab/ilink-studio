"""数据路径解析：历史 `data/xxx` 写法不能拼成 `<数据目录>/data/xxx`。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.paths import resolve_data_path


class ResolveDataPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name).resolve() / "data"
        self.data_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_relative_path_lands_under_data_dir(self) -> None:
        resolved = resolve_data_path(self.data_dir, "im-workspace", default="im-workspace")
        self.assertEqual(resolved, self.data_dir / "im-workspace")

    def test_legacy_data_prefix_is_not_doubled(self) -> None:
        """回归：配置默认值曾是 data/im-workspace，拼出 data/data/im-workspace 让 CLI chdir 失败。"""
        resolved = resolve_data_path(self.data_dir, "data/im-workspace", default="im-workspace")
        self.assertEqual(resolved, self.data_dir / "im-workspace")
        self.assertNotIn("data/data", resolved.as_posix())

    def test_empty_value_falls_back_to_default(self) -> None:
        resolved = resolve_data_path(self.data_dir, "", default="im-workspace")
        self.assertEqual(resolved, self.data_dir / "im-workspace")

    def test_absolute_path_is_kept(self) -> None:
        absolute = Path(self.tmp.name).resolve() / "custom-ws"
        resolved = resolve_data_path(self.data_dir, str(absolute), default="im-workspace")
        self.assertEqual(resolved, absolute)

    def test_parent_traversal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_data_path(self.data_dir, "../outside", default="im-workspace")


class BrainWorkspaceTest(unittest.TestCase):
    """CLI 会 chdir 到 --dir；这个目录必须存在，否则退出码 2。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name).resolve() / "data"
        self.data_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _brain(self, workspace: str):
        from gateway.brain import ReasonixBrain
        from gateway.config import BrainConfig

        class FakeStore:
            path = self.data_dir / "mailbox.db"

        return ReasonixBrain(BrainConfig(workspace=workspace), FakeStore())

    def test_build_args_points_at_existing_workspace(self) -> None:
        brain = self._brain("im-workspace")
        args = brain.build_args(1)
        self.assertIn("--dir", args)
        workspace = Path(args[args.index("--dir") + 1])
        self.assertEqual(workspace, self.data_dir / "im-workspace")
        self.assertTrue(workspace.is_dir(), "build_args 必须先把工作区目录建出来")

    def test_legacy_config_value_does_not_double_data(self) -> None:
        brain = self._brain("data/im-workspace")
        args = brain.build_args(1)
        workspace = Path(args[args.index("--dir") + 1])
        self.assertEqual(workspace, self.data_dir / "im-workspace")
        self.assertNotIn("data", workspace.relative_to(self.data_dir).parts[:1])


if __name__ == "__main__":
    unittest.main()
