"""把 Python 网关打成 onedir（PyInstaller），供 Electron 发布目录使用。

    python scripts/build_gateway.py            # 输出 dist/gateway/gateway.exe
    python scripts/build_gateway.py --clean    # 先清 dist/build

打包后由 desktop/main.cjs 从 resources/gateway/gateway.exe 启动，
不再要求目标机器安装 Python。PyInstaller 本身不是运行时依赖。

注意：`--collect-submodules gateway` 在生成 spec 时执行，那一刻项目根还没进
sys.path，它返回空列表（PyInstaller 6.20 实测）。适配器是按账号动态导入的，
静态分析看不到，所以必须用显式 `--hidden-import` 登记，否则发布版会在启用通道时
报「依赖没装齐」。构建结束会校验 PYZ 清单，缺模块直接失败。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "gateway"
OUT = ROOT / "dist" / "gateway"

# 运行时由 gateway/registry.py 动态导入，静态分析与 collect_submodules 都覆盖不到。
DYNAMIC_MODULES = (
    "gateway.adapters.qq",
    "gateway.adapters.wechat",
    "gateway.adapters.wechat_ilink",
    "gateway.adapters.bilibili",
    "gateway.adapters.douyin",
)


def verify_bundle() -> list[str]:
    """从 PYZ 清单确认动态导入的适配器真的进了包。"""
    toc = BUILD / "gateway" / "PYZ-00.toc"
    if not toc.is_file():
        return [f"缺少打包清单 {toc.name}，无法确认适配器已入包"]
    text = toc.read_text(encoding="utf-8", errors="ignore")
    return [name for name in DYNAMIC_MODULES if name not in text]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)
    if args.clean:
        for path in (BUILD, OUT):
            shutil.rmtree(path, ignore_errors=True)
    if shutil.which("pyinstaller") is None:
        print("[x] 没装 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 2
    command = [
        "pyinstaller", "--noconfirm", "--clean", "--onedir", "--console",
        "--name", "gateway", "--distpath", str(ROOT / "dist"),
        "--workpath", str(BUILD), "--specpath", str(BUILD),
        "--paths", str(ROOT),
        "--collect-all", "playwright",
        "--collect-all", "bilibili_api",
        "--add-data", f"{ROOT / 'web' / 'dist'};web/dist",
        "--version-file", str(ROOT / "scripts" / "gateway_version.txt"),
        str(ROOT / "scripts" / "gateway_entry.py"),
    ]
    for name in DYNAMIC_MODULES:
        command[1:1] = ["--hidden-import", name]
    print(" ".join(command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode
    missing = verify_bundle()
    if missing:
        print("[x] 打包产物缺少适配器模块：" + "、".join(missing), file=sys.stderr)
        print("    它们由 gateway/registry.py 动态导入，必须显式 --hidden-import。",
              file=sys.stderr)
        return 3
    exe = OUT / ("gateway.exe" if sys.platform == "win32" else "gateway")
    print(f"[ok] {exe}" if exe.exists() else "[!] 未找到产物")
    return 0 if exe.exists() else 1


if __name__ == "__main__":
    sys.exit(main())
