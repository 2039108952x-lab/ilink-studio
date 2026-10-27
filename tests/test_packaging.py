"""打包清单：动态导入的适配器必须显式登记，否则发布版启用通道会报依赖缺失。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from gateway.registry import PLATFORM_MODULES

BUILD_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_gateway.py"


def declared_dynamic_modules() -> set[str]:
    tree = ast.parse(BUILD_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") == "DYNAMIC_MODULES" for target in node.targets
        ):
            return {element.value for element in node.value.elts}  # type: ignore[union-attr]
    return set()


class BuildManifestTest(unittest.TestCase):
    def test_every_platform_adapter_is_declared(self) -> None:
        declared = declared_dynamic_modules()
        expected = {f"gateway.adapters.{name}" for name in PLATFORM_MODULES}
        self.assertTrue(expected <= declared, f"打包脚本漏登记：{sorted(expected - declared)}")

    def test_declared_modules_match_real_files(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for module in declared_dynamic_modules():
            name = module.rsplit(".", 1)[-1]
            self.assertTrue((root / "gateway" / "adapters" / f"{name}.py").is_file(), module)

    def test_build_script_verifies_the_bundle(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("verify_bundle", source)
        self.assertIn("PYZ-00.toc", source)
        # collect_submodules 在 spec 生成阶段拿不到项目根，不能只靠它
        self.assertNotIn('"--collect-submodules"', source)
        self.assertIn('"--hidden-import"', source)


if __name__ == "__main__":
    unittest.main()
