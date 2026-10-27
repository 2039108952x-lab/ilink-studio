"""抖音 MCP 客户端：把 dsh-douyin-scroll 的 stdio MCP server 当执行后端。

通用部分（进程管理、JSON-RPC、超时杀进程、重启重试、错误压缩）已经抽到
``gateway/mcp_client.py`` —— B 站走的是同一条路，两边不该各写一份。

这里只剩抖音自己的两件事：
- 家目录变量叫 ``DOUYIN_HOME``，Cookie、记忆、报告都落在它下面；
- 服务器脚本在哪儿找（随包优先、仓库次之）。
"""

from __future__ import annotations

from pathlib import Path

from .mcp_client import (  # noqa: F401 - 这些名字是对外契约，适配器与雷达都在 import
    CallResult,
    McpClient,
    McpError,
    describe_error,
    find_node,
    resolve_server,
)

# 老名字：`adapters/douyin.py` 与 `radar/douyin.py` 一直在用 DouyinMCPError
DouyinMCPError = McpError


class DouyinMCP(McpClient):
    """一个实例对应一个 MCP 子进程；由适配器或雷达持有并复用。"""

    def __init__(
        self,
        *,
        command: list[str],
        home: Path | str,
        timeout: float = 90.0,
        max_restarts: int = 2,
    ) -> None:
        super().__init__(
            command=command,
            env={"DOUYIN_HOME": str(home)},
            timeout=timeout,
            max_restarts=max_restarts,
        )


def find_server(repo_root: Path | str, resource_root: Path | str | None = None) -> Path:
    """按「随包优先、仓库次之」找 mcp-server.mjs。"""
    candidates: list[Path] = []
    if resource_root:
        candidates.append(Path(resource_root) / "douyin-mcp" / "tools" / "mcp-server.mjs")
        candidates.append(Path(resource_root) / "vendor" / "dsh-douyin-scroll"
                          / "tools" / "mcp-server.mjs")
    candidates.append(Path(repo_root) / "vendor" / "dsh-douyin-scroll" / "tools" / "mcp-server.mjs")
    return resolve_server(candidates)


def build_client(*, repo_root: Path | str, home: Path | str,
                 resource_root: Path | str | None = None,
                 node: str = "", server: Path | str | None = None,
                 timeout: float = 90.0) -> DouyinMCP:
    script = Path(server) if server else find_server(repo_root, resource_root)
    command = [find_node(node), str(script)]
    return DouyinMCP(command=command, home=home, timeout=timeout)
