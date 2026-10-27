"""通用 MCP 客户端：把一条 stdio JSON-RPC 子进程当执行后端。

原先这段逻辑写死在 ``douyin_mcp.py`` 里。B 站也要走同一条路（起 Python MCP），
所以在这里抽出来，抖音/B 站各自只剩「命令怎么拼、家目录变量叫什么」这点差异。

设计取舍（沿用抖音那版，一条没改）：
- 每次调用走一条真实 stdio 管道，不做长连接池：MCP server 自己很轻（启动 ~40ms）；
- 超时就把子进程杀掉再重起，避免旧响应错配到下一次调用；
- 日志只记工具名、耗时、ok/isError —— 凭据与正文一律不进日志。

    stdout 只读 JSON-RPC：对端往 stdout 打一行日志就会破坏协议，这类行按「认不出就跳过」处理。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
MAX_STDERR_KEEP = 4000
# 单行 JSON-RPC 的上限。B站 bili_login / bili_cover / bili_frames 会把图片
# base64 塞进一行，默认 64 KiB 不够，撞上就是 ValueError 而不是可读错误。
_STREAM_LIMIT = 16 * 1024 * 1024
CLIENT_NAME = "ilink-studio"
CLIENT_VERSION = "0.1.0"


class McpError(RuntimeError):
    """MCP 层失败。``code`` 给界面用。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.safe_message, self.retryable = code, message, retryable


@dataclass(slots=True)
class CallResult:
    tool: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""

    @property
    def needs_confirm(self) -> bool:
        return bool(self.data.get("needsConfirm"))


def describe_error(payload: dict[str, Any]) -> str:
    """把工具返回里的失败信息压成一句人话。"""
    error = str(payload.get("error") or "").strip()
    hint = str(payload.get("hint") or "").strip()
    if error and hint:
        return f"{error}（{hint}）"
    return error or hint or "未知错误"


def find_node(configured: str = "") -> str:
    """优先用配置，其次 PATH 里的 node。"""
    if configured:
        return configured
    found = shutil.which("node") or shutil.which("node.exe")
    return found or "node"


MIN_NODE_MAJOR = 22


def node_status(configured: str = "") -> dict[str, Any]:
    """检查系统 Node 的版本。

    发布版**不带** Node（不把 node.exe 打进包里），所以这里必须明说缺什么：
    静默失败会表现为「刷一轮没反应」，那是最难查的一种。
    """
    binary = find_node(configured)
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "path": binary, "version": "",
                "error": f"找不到可用的 Node（{type(exc).__name__}）。装 Node ≥{MIN_NODE_MAJOR} 后重试"}
    raw = (out.stdout or out.stderr or "").strip().lstrip("v")
    if out.returncode != 0 or not raw:
        return {"ok": False, "path": binary, "version": raw,
                "error": f"Node 调用失败（退出码 {out.returncode}）"}
    try:
        major = int(raw.split(".")[0])
    except (ValueError, IndexError):
        return {"ok": False, "path": binary, "version": raw, "error": "读不出 Node 版本号"}
    return {
        "ok": major >= MIN_NODE_MAJOR,
        "path": binary,
        "version": raw,
        "error": "" if major >= MIN_NODE_MAJOR
        else f"Node 版本过低（{raw}），需要 ≥ {MIN_NODE_MAJOR}",
    }


class McpClient:
    """一次只服务一个调用的小客户端；并发由调用方串行化。"""

    def __init__(
        self,
        *,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout: float = 90.0,
        max_restarts: int = 2,
    ) -> None:
        self._command = list(command)
        self._env = {str(k): str(v) for k, v in (env or {}).items()}
        self._timeout = float(timeout)
        self._max_restarts = max(0, int(max_restarts))
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._ready = False
        self._stderr_tail = ""
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- 进程管理 ----

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._ready

    async def start(self) -> None:
        if self.alive:
            return
        env = dict(os.environ)
        env.update(self._env)
        env.setdefault("PYTHONUTF8", "1")
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # 默认 64 KiB 的 StreamReader 上限会掐断大响应：B站登录二维码是
            # base64 PNG，一帧 JSON-RPC 轻松超过 64 KiB，readline 直接抛
            # 「Separator is found, but chunk is longer than limit」。
            limit=_STREAM_LIMIT,
        )
        self._stderr_tail = ""
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        await self._handshake()

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.readline()
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            self._stderr_tail = (self._stderr_tail + text)[-MAX_STDERR_KEEP:]

    async def _handshake(self) -> None:
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })
        if not isinstance(result, dict):
            raise McpError("handshake_failed", "MCP 初始化返回异常")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    async def close(self) -> None:
        self._ready = False
        proc, self._proc = self._proc, None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - 关管道失败无所谓
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _kill(self) -> None:
        await self.close()

    # ---- JSON-RPC ----

    def _send(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("not_started", "MCP 进程尚未启动")
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))

    async def _read_line(self) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise McpError("not_started", "MCP 进程尚未启动")
        while True:
            line = await proc.stdout.readline()
            if not line:
                detail = self._stderr_tail.strip().replace("\n", " | ")[-200:]
                raise McpError(
                    "process_exited",
                    f"MCP 进程提前退出{('：' + detail) if detail else ''}",
                    retryable=True,
                )
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and ("result" in message or "error" in message):
                return message

    async def _request(self, method: str, params: dict[str, Any],
                       *, timeout: float | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        budget = self._timeout if timeout is None else timeout
        try:
            message = await asyncio.wait_for(self._read_line(), timeout=budget)
        except asyncio.TimeoutError:
            raise McpError("timeout", f"{method} 超过 {budget:.0f}s 没有返回",
                           retryable=True) from None
        error = message.get("error")
        if isinstance(error, dict):
            raise McpError(
                "rpc_error",
                f"{method} 失败：{error.get('message') or error.get('code')}",
            )
        return message.get("result")

    # ---- 对外 ----

    async def call(self, tool: str, arguments: dict[str, Any] | None = None,
                   *, timeout: float | None = None) -> CallResult:
        """调一个工具。失败会重启一次进程；业务失败（ok:false）原样返回给调用方。"""
        args = dict(arguments or {})
        started = time.monotonic()
        last_error: McpError | None = None
        async with self._lock:
            for attempt in range(self._max_restarts + 1):
                try:
                    await self.start()
                    raw = await self._request(
                        "tools/call", {"name": tool, "arguments": args}, timeout=timeout)
                    break
                except (McpError, OSError) as exc:
                    last_error = exc if isinstance(exc, McpError) else McpError(
                        "spawn_failed", f"启动 MCP 失败：{type(exc).__name__}")
                    await self._kill()
                    if attempt >= self._max_restarts or not last_error.retryable:
                        elapsed = int((time.monotonic() - started) * 1000)
                        log.warning("MCP 调用失败：tool=%s ms=%d code=%s", tool, elapsed,
                                    last_error.code)
                        raise last_error
                    await asyncio.sleep(0.5 * (attempt + 1))
            else:  # pragma: no cover - 循环一定会 break 或 raise
                raise last_error or McpError("unknown", "MCP 调用失败")

        elapsed = int((time.monotonic() - started) * 1000)
        content = raw.get("content") if isinstance(raw, dict) else None
        text = ""
        if isinstance(content, list):
            parts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
            text = "\n".join(part for part in parts if part)
        payload: dict[str, Any] = {}
        if text:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = {"ok": False, "error": text[:300]}
            payload = decoded if isinstance(decoded, dict) else {"ok": True, "value": decoded}
        is_error = bool(raw.get("isError")) if isinstance(raw, dict) else False
        ok = bool(payload.get("ok", not is_error)) and not is_error
        log.info("MCP 调用：tool=%s ms=%d ok=%s%s", tool, elapsed, ok,
                 "" if ok else f" reason={describe_error(payload)[:80]}")
        return CallResult(tool=tool, ok=ok, data=payload, elapsed_ms=elapsed,
                          error="" if ok else describe_error(payload))

    async def call_raw(self, tool: str, arguments: dict[str, Any] | None = None,
                       *, timeout: float | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """同 ``call``，但保留图片块：``(payload, images)``，图片项是 ``{"data","mimeType"}``。

        ``bili_cover`` / ``bili_frames`` 返回的是 image 内容块，``call`` 只取文本会把它丢掉。
        """
        args = dict(arguments or {})
        started = time.monotonic()
        async with self._lock:
            await self.start()
            raw = await self._request(
                "tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        elapsed = int((time.monotonic() - started) * 1000)
        content = raw.get("content") if isinstance(raw, dict) else None
        blocks = [part for part in content if isinstance(part, dict)] if isinstance(content, list) else []
        text = "\n".join(
            str(part.get("text") or "") for part in blocks if part.get("text")
        )
        images = [
            {"data": str(part.get("data") or ""),
             "mimeType": str(part.get("mimeType") or part.get("mime_type") or "image/jpeg")}
            for part in blocks if part.get("type") == "image" and part.get("data")
        ]
        payload: dict[str, Any] = {}
        if text:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                decoded = {"ok": False, "error": text[:300]}
            payload = decoded if isinstance(decoded, dict) else {"ok": True, "value": decoded}
        is_error = bool(raw.get("isError")) if isinstance(raw, dict) else False
        ok = bool(payload.get("ok", not is_error)) and not is_error
        log.info("MCP 调用（raw）：tool=%s ms=%d ok=%s images=%d", tool, elapsed, ok, len(images))
        if not ok:
            payload.setdefault("error", describe_error(payload))
        return payload, images


def resolve_server(candidates: list[Path]) -> Path:
    """按「随包优先、仓库次之」挑第一个真实存在的脚本；都不在就返回第一个（让报错落在调用处）。"""
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]
