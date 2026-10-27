"""测试用的假 MCP server：走真实 stdin/stdout，行为由第一条调用决定。

用法：python tests/fake_mcp_server.py [mode]
mode: normal（默认）| bizfail | rpcerror | hang | crash
"""

from __future__ import annotations

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(request_id: int, payload: dict, *, is_error: bool = False) -> None:
    send({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error,
        },
    })


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake", "version": "0.0.1"},
        }})
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/call":
        name = (message.get("params") or {}).get("name")
        if MODE == "crash" and name != "douyin_status":
            sys.exit(3)
        if MODE == "flaky":
            import os
            marker = os.environ.get("FAKE_MCP_STATE", "")
            if marker:
                from pathlib import Path
                path = Path(marker)
                seen = path.is_file()
                path.write_text("1", encoding="utf-8")
                if not seen:
                    sys.exit(4)
        if MODE == "hang":
            time.sleep(30)
            result(request_id, {"ok": True})
            continue
        if MODE == "rpcerror":
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32000, "message": "boom"}})
            continue
        if MODE == "bizfail":
            result(request_id, {"ok": False, "error": "还没有登录", "hint": "扫码"})
            continue
        result(request_id, {
            "ok": True, "tool": name, "home": "X:/fake-home",
            "args": (message.get("params") or {}).get("arguments") or {},
        })
        continue
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}})
