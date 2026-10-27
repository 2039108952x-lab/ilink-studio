"""UI 重接管：只更新保险库内会话凭据并写入受控 stdout，不启动第二个 HTTP 服务。"""

from __future__ import annotations

import argparse
import json
import sys

from .paths import app_paths
from .runtime import rotate_ui_token
from .vault import Vault


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="向已运行网关申请新的桌面会话")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    paths = app_paths()
    runtime = json.loads((paths.data_dir / "runtime.json").read_text(encoding="utf-8"))
    if int(runtime.get("port", 0)) != args.port:
        raise RuntimeError("运行时端口不匹配，已拒绝重接管")
    token = rotate_ui_token(Vault(paths.vault_db), port=args.port)
    sys.stdout.write(token + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
