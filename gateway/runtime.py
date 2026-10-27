"""桌面网关实例登记与受控 UI 重接管。"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from .vault import Vault

SYSTEM_ACCOUNT = "desktop:session"


def issue_ui_token(vault: Vault) -> str:
    token = secrets.token_urlsafe(32)
    vault.put(SYSTEM_ACCOUNT, "ui_token", token)
    return token


def current_ui_token(vault: Vault) -> str:
    return vault.get(SYSTEM_ACCOUNT, "ui_token")


def bootstrap_token(vault: Vault) -> str:
    return vault.get("desktop:bootstrap", "token")


def rotate_ui_token(vault: Vault, *, port: int) -> str:
    """由同机同用户的新 UI 进程调用；不会启动第二个网关或轮询器。"""
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/runtime/session",
        data=json.dumps({"bootstrap_token": bootstrap_token(vault)}).encode("utf-8"),
        headers={
            "Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
            "Content-Type": "application/json",
        }, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("网关拒绝了 UI 重接管")
        token = str(json.loads(response.read()).get("ui_token") or "")
    if len(token.encode()) < 43:
        raise RuntimeError("网关返回了无效的 UI 会话")
    vault.put(SYSTEM_ACCOUNT, "ui_token", token)
    return token


def register_runtime(path: Path, *, port: int, instance_id: str) -> dict[str, Any]:
    payload = {"pid": os.getpid(), "port": port, "instance_id": instance_id, "version": 1}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)
    return payload


def unregister_runtime(path: Path, *, instance_id: str) -> None:
    """只清理本进程创建的临时登记；不处理其他实例的文件。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("instance_id") == instance_id and payload.get("pid") == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
