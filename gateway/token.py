"""网关与 MCP server 共享的本地控制面 token。

为什么需要
    网关的 POST /send 要 Bearer token，MCP server 调它时得带上同一个值。
    让用户手工配两处环境变量很容易漏掉一处然后 401，所以做成：
        环境变量 ILINK_GATEWAY_TOKEN  >  data/gateway-token.txt  >  自动生成
    两边读同一个文件，天然一致。

安全
    文件只落在工作区 data/ 下，权限尽量收紧到 0600；
    token 本身只用于 127.0.0.1 上的调用，不外发。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DEFAULT_ENV = "ILINK_GATEWAY_TOKEN"
_TOKEN_BYTES = 32


def token_path(root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parent.parent
    return base / "data" / "gateway-token.txt"


def ensure_token(env_name: str = DEFAULT_ENV, root: Path | None = None) -> str:
    """拿到 token；没有就生成一个并落盘。"""
    from_env = os.environ.get(env_name, "").strip()
    if from_env:
        return from_env

    path = token_path(root)
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    value = secrets.token_urlsafe(_TOKEN_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上 chmod 语义有限，失败不影响功能
    return value
