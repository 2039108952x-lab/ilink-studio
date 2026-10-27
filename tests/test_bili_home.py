"""B 站家目录：铺脚本、铺章程、两条指纹各自的行为。

这里最要紧的一条是**幂等**：铺完立刻再铺一次，必须还是 ``unchanged``。
一开始用 ``write_text`` 写章程，Windows 上把 LF 翻成 CRLF ⇒ 指纹永远对不上，
每轮都被误判成「使用者改过」，章程再也升不了级。所以这条单独钉死。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.bili_home import (
    CHARTER_LEDGER,
    asr_python,
    ensure_home,
    home_dir,
    install_charter,
    install_scripts,
    plugin_root,
    resolve_python,
    venv_python,
)

CHARTER = "# 值班章程\n\n第一行\n第二行\n"


def make_source(root: Path, *, charter: str = CHARTER) -> Path:
    """造一个最小插件根，形状跟 vendor/dsh-bili-scroll 一致。"""
    (root / "vendor" / "bilibili-mcp").mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "vendor" / "bilibili-mcp" / "mcp_server.py").write_text("print('mcp')\n", encoding="utf-8")
    (root / "lib" / "asr.py").write_text("print('asr')\n", encoding="utf-8")
    (root / "lib" / "favops.py").write_text("print('fav')\n", encoding="utf-8")
    (root / "prompts" / "duty.md").write_text(charter, encoding="utf-8", newline="")
    return root


class HomeLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.source = make_source(self.root / "plugin")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_home_dir_is_under_data_dir(self) -> None:
        self.assertEqual(home_dir(self.data), self.data / "bili-home")

    def test_creates_subdirs_and_scripts(self) -> None:
        state = ensure_home(self.data, source_root=self.source)
        self.assertTrue(state.ready)
        home = home_dir(self.data)
        for name in ("out", "digest", "reports", "tmp"):
            self.assertTrue((home / name).is_dir(), name)
        self.assertEqual({item.name for item in state.scripts},
                         {"mcp_server.py", "asr.py", "favops.py"})
        self.assertTrue(all(item.action == "installed" for item in state.scripts))
        self.assertEqual(state.charter.action, "installed")
        self.assertTrue((home / "duty.md").is_file())
        self.assertTrue((home / CHARTER_LEDGER).is_file())

    def test_second_run_is_idempotent(self) -> None:
        ensure_home(self.data, source_root=self.source)
        again = ensure_home(self.data, source_root=self.source)
        self.assertTrue(all(item.action == "unchanged" for item in again.scripts))
        # ⭐ 关键：章程不能被误判成「使用者改过」
        self.assertEqual(again.charter.action, "unchanged")

    def test_charter_bytes_survive_roundtrip(self) -> None:
        """铺完的章程要与源文件逐字节相同（CRLF 转换会让指纹漂）。"""
        ensure_home(self.data, source_root=self.source)
        written = (home_dir(self.data) / "duty.md").read_bytes()
        self.assertEqual(written, (self.source / "prompts" / "duty.md").read_bytes())

    def test_missing_source_reports_not_ready(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        state = ensure_home(self.data, source_root=empty)
        self.assertFalse(state.ready)
        self.assertTrue(all(item.action == "missing_source" for item in state.scripts))
        self.assertEqual(state.charter.action, "missing_source")
        # 目录还是要建出来，界面才有东西可看
        self.assertTrue(home_dir(self.data).is_dir())


class CharterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = make_source(self.root / "plugin")
        self.charter = self.source / "prompts" / "duty.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_user_edit_is_never_overwritten(self) -> None:
        """用户改过 + 上游又发了新版 ⇒ 家里那份一个字不动，新版另存 .new。"""
        install_charter(self.home, self.charter)
        mine = "我自己改的章程"
        (self.home / "duty.md").write_text(mine, encoding="utf-8")
        newer = self.source / "prompts" / "duty.md"
        newer.write_text(CHARTER + "\n上游新增的一条\n", encoding="utf-8", newline="")
        result = install_charter(self.home, newer)
        self.assertEqual(result.action, "kept")
        self.assertEqual((self.home / "duty.md").read_text(encoding="utf-8"), mine)
        # 新版另存，让人自己合
        self.assertTrue(Path(result.pending).is_file())
        self.assertEqual(Path(result.pending).read_bytes(), newer.read_bytes())

    def test_user_edit_without_new_version_adds_no_pending(self) -> None:
        """源没变、只有用户改过 ⇒ 什么都不做，也不该凭空造一个 .new。"""
        install_charter(self.home, self.charter)
        mine = "我自己改的章程"
        (self.home / "duty.md").write_text(mine, encoding="utf-8")
        result = install_charter(self.home, self.charter)
        self.assertEqual(result.action, "kept")
        self.assertEqual(result.pending, "")
        self.assertFalse((self.home / "duty.md.new").exists())
        self.assertEqual((self.home / "duty.md").read_text(encoding="utf-8"), mine)

    def test_upgrade_when_untouched(self) -> None:
        install_charter(self.home, self.charter)
        newer = self.source / "prompts" / "duty.md"
        newer.write_text(CHARTER + "\n新加一条\n", encoding="utf-8", newline="")
        result = install_charter(self.home, newer)
        self.assertEqual(result.action, "updated")
        self.assertEqual((self.home / "duty.md").read_bytes(), newer.read_bytes())

    def test_no_ledger_counts_as_edited(self) -> None:
        """没有账本时按「改过」处理：宁可让人手动合一次，也不吞掉他的改动。"""
        (self.home / "duty.md").write_text("旧的、来历不明的一份", encoding="utf-8")
        result = install_charter(self.home, self.charter)
        self.assertEqual(result.action, "kept")
        self.assertEqual((self.home / "duty.md").read_text(encoding="utf-8"),
                         "旧的、来历不明的一份")

    def test_missing_source_is_reported(self) -> None:
        result = install_charter(self.home, self.root / "nope.md")
        self.assertEqual(result.action, "missing_source")


class ScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = make_source(self.root / "plugin")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_edited_script_is_backed_up_then_replaced(self) -> None:
        """运行脚本被改过也照样覆盖（否则宿主新、Python 侧旧），但必须先备份。"""
        install_scripts(self.home, self.source)
        target = self.home / "asr.py"
        target.write_text("# 我瞎改的", encoding="utf-8")
        results = {item.name: item for item in install_scripts(self.home, self.source)}
        self.assertEqual(results["asr.py"].action, "updated")
        self.assertTrue(Path(results["asr.py"].backup).is_file())
        self.assertEqual(Path(results["asr.py"].backup).read_text(encoding="utf-8"), "# 我瞎改的")
        self.assertEqual(target.read_text(encoding="utf-8"), "print('asr')\n")


class PythonTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_venv_layout(self) -> None:
        self.assertIn("python", venv_python(self.home).name.lower())
        self.assertIn(".venv", str(venv_python(self.home)))
        self.assertIn(".venv-asr", str(asr_python(self.home)))

    def test_configured_wins(self) -> None:
        fake = self.home / "my-python.exe"
        fake.write_text("", encoding="utf-8")
        found, venv = resolve_python(self.home, str(fake))
        self.assertEqual(found, str(fake))
        self.assertEqual(venv, str(fake.parent.parent))

    def test_falls_back_to_something_real(self) -> None:
        found, _ = resolve_python(self.home)
        self.assertTrue(found)  # 至少有 sys.executable 兜底


class NoCreateTest(unittest.TestCase):
    def test_probe_mode_does_not_touch_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            state = ensure_home(data, source_root=None, create=False)
            self.assertFalse(home_dir(data).exists())
            self.assertEqual(state.scripts, [])


class PluginRootTest(unittest.TestCase):
    """DEV 下传进来的常是仓库根，真插件在 vendor/dsh-bili-scroll 里面。

    认错一层的后果不是报错，而是三条脚本全「源文件不在」，B 站雷达静默不能用。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_nested_plugin_root(self) -> None:
        wrapper = self.root / "repo"
        plugin = make_source(wrapper / "vendor" / "dsh-bili-scroll")
        self.assertEqual(plugin_root(wrapper), plugin)

    def test_keeps_flat_layout(self) -> None:
        plugin = make_source(self.root / "flat")
        self.assertEqual(plugin_root(plugin), plugin)

    def test_falls_back_when_nothing_matches(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(plugin_root(empty), empty)

    def test_install_scripts_ships_from_wrapper_root(self) -> None:
        wrapper = self.root / "repo2"
        make_source(wrapper / "vendor" / "dsh-bili-scroll")
        home = home_dir(self.root / "data2")
        results = install_scripts(home, wrapper)
        self.assertTrue(all(item.action != "missing_source" for item in results), results)
        for name in ("mcp_server.py", "asr.py", "favops.py"):
            self.assertTrue((home / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
