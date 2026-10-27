"""PyInstaller 入口：复用 gateway.desktop 的命令行。"""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # 浏览器与 onedir 网关同级发布；首启不联网下载浏览器。
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path(sys.executable).resolve().parent.parent / "playwright"),
    )

from gateway.desktop import main

if __name__ == "__main__":
    sys.exit(main())
