"""把已安装的 Playwright 配套浏览器纳入发布物，不在首启下载。

只接受 playwright install --dry-run 报告的同版本目录；源目录保持不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "ms-playwright"


def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    default_root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(default_root)))
    if not root.is_dir():
        print(f"[x] 找不到已安装的 Playwright 浏览器：{root}", file=sys.stderr)
        return 2
    required = {}
    marker = "Install location:"
    for line in completed.stdout.splitlines():
        if marker not in line:
            continue
        source = Path(line.split(marker, 1)[1].strip())
        relative = source.relative_to(root)
        name = relative.parts[0].split("-", 1)[0]
        if name not in ("chromium", "chromium_headless_shell", "ffmpeg"):
            continue
        if not source.is_dir():
            print(f"[x] 缺少配套浏览器：{source}", file=sys.stderr)
            return 2
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
        required[name] = relative.as_posix()
    if set(required) != {"chromium", "chromium_headless_shell", "ffmpeg"}:
        print("[x] dry-run 未报告匹配的 chromium 与 ffmpeg，未替换发布浏览器", file=sys.stderr)
        return 2
    executable = DEST / required["chromium"] / "chrome-win64" / "chrome.exe"
    if not executable.is_file():
        print(f"[x] 随包浏览器缺少 chrome.exe：{executable}", file=sys.stderr)
        return 2
    report = subprocess.run(
        [sys.executable, "-c", "import importlib.metadata; print(importlib.metadata.version('playwright'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    files = {}
    for path in sorted(DEST.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            files[path.relative_to(DEST).as_posix()] = digest.hexdigest()
    manifest = {"playwright": report, "source": "playwright-managed-installed-browser",
                "required": required, "sha256": files}
    (DEST / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[ok] {len(files)} browser files staged; Playwright {report}: {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
