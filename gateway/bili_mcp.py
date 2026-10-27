"""B 站 MCP 客户端：把内置的 ``bilibili-mcp``（Python）当能力后端。

它和抖音那条路的区别（都是真的，别抄错）
    抖音是 Node 脚本、自己一套工具；B 站是 **Python 脚本**，而且被上游 patch 过
    —— 除了原作者的公开接口，还多了 ``bili_recommend`` / ``bili_transcribe*`` /
    ``bili_cover`` / ``bili_frames`` 和四个 ``duty_*`` 本地工具。所以能力侧不重写，
    原样委托给它；我们只在外面套一层流程（候选 → 证据 → 判断 → 报告 → 投递）。

为什么跑家目录那份、不跑 vendor 里那份
    ``mcp_server.py`` 的凭证路径写死成 ``Path(__file__).parent / "bili_credential.json"``
    —— 跟着脚本走。原地跑就把登录态写进 ``vendor/``：升级被覆盖、装成应用后只读。
    ``bili_home`` 负责把脚本铺到家目录，这里只负责起它。

三道闸都靠环境变量（上游自己实现的，我们只负责设对）
    ``BILI_DUTY_HOME``   家目录：报告 / 判重表 / 凭证 / 转写环境全在它下面
    ``BILI_MCP_TOOLS``   工具白名单。**我们设它** —— 名单外的工具连 ``tools/list``
                         都不出现，等于在模型和网关那边根本不存在
    ``BILI_STYLE_OFF``   置 1 关掉语感闸门。**不设**：那道闸是上游最有价值的一层
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .mcp_client import (  # noqa: F401 - 对外契约，雷达与界面都在 import
    CallResult,
    McpClient,
    McpError,
    describe_error,
    resolve_server,
)

log = logging.getLogger(__name__)

# 与 ``radar/provider.py`` 的失败码对齐；多两个 B 站特有的
NOT_LOGGED_IN = "未登录B站"
DEPS_MISSING_HINTS = ("转写环境没装好", "No module named", "modulenotfounderror")
# 私信长度上限：490 字能发，再长被拒并返回 code 21007（上游章程正文里的实测）
DM_TOO_LONG_CODE = 21007


class BiliMCPError(McpError):
    """别名，方便调用方按平台区分。"""


def classify(payload: dict[str, Any], fallback: str = "tool_failed") -> str:
    """把工具返回压成一个失败码。与 ``radar/provider.py`` 的约定一致。

    顺序有讲究：先认「未登录」，因为它在返回里出现时其它字段都不可信；
    再认依赖缺失（转写环境没装），最后才是普通失败。
    """
    if payload.get("blocked_by") == "style":
        return "style_blocked"
    if payload.get("code") == DM_TOO_LONG_CODE:
        return "too_long"
    text = describe_error(payload).lower()
    if NOT_LOGGED_IN.lower() in text or "请先调用 bili_login" in text:
        return "not_logged_in"
    if any(hint.lower() in text for hint in DEPS_MISSING_HINTS):
        return "deps_missing"
    if "限流" in text or "频繁" in text or "rate" in text:
        return "rate_limited"
    if "没有这个 job" in text or "缺 " in text or "参数" in text:
        return "invalid_args"
    return fallback


class BiliMCP(McpClient):
    """一个实例对应一个 Python MCP 子进程；由雷达持有并复用。"""

    def __init__(
        self,
        *,
        command: list[str],
        home: Path | str,
        timeout: float = 120.0,
        max_restarts: int = 1,
        tools: list[str] | None = None,
        style_gate: bool = True,
    ) -> None:
        env = {"BILI_DUTY_HOME": str(home)}
        if tools:
            env["BILI_MCP_TOOLS"] = ",".join(tools)
        if not style_gate:
            env["BILI_STYLE_OFF"] = "1"
        super().__init__(
            command=command,
            env=env,
            timeout=timeout,
            max_restarts=max_restarts,
        )

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None,
                        *, timeout: float | None = None) -> CallResult:
        """``call`` 的别名：雷达那边统一叫 ``call_tool``，别在两层里混用两个名字。"""
        return await self.call(tool, arguments, timeout=timeout)

    async def list_tools(self, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """导出 ``tools/list``。自检与文档核对用，不参与业务。

        必须和 ``call`` 抢同一把锁：控制面会同时打 status 和 doctor，
        两个协程一起 ``readline()`` 同一个管道就是
        「readuntil() called while another coroutine is already waiting for
        incoming data」，界面上表现为「未就绪 + 0 个工具」。
        """
        async with self._lock:
            await self.start()
            result = await self._request("tools/list", {}, timeout=timeout)  # noqa: SLF001
            tools = result.get("tools") if isinstance(result, dict) else None
            return [item for item in tools or [] if isinstance(item, dict)]


def find_server(repo_root: Path | str, resource_root: Path | str | None = None,
                home: Path | str | None = None) -> Path:
    """按「家目录优先、随包次之、仓库最后」找 ``mcp_server.py``。

    家目录那份排最前不是洁癖：凭证跟着脚本走，只有跑它登录态才落在 ``data/bili-home``。
    """
    candidates: list[Path] = []
    if home is not None:
        candidates.append(Path(home) / "mcp_server.py")
    if resource_root:
        candidates.append(Path(resource_root) / "bili-mcp" / "mcp_server.py")
        candidates.append(Path(resource_root) / "vendor" / "dsh-bili-scroll"
                          / "vendor" / "bilibili-mcp" / "mcp_server.py")
    candidates.append(Path(repo_root) / "vendor" / "dsh-bili-scroll"
                      / "vendor" / "bilibili-mcp" / "mcp_server.py")
    return resolve_server(candidates)


def build_client(
    *,
    repo_root: Path | str,
    home: Path | str,
    resource_root: Path | str | None = None,
    python: str = "",
    server: Path | str | None = None,
    timeout: float = 120.0,
    tools: list[str] | None = None,
    style_gate: bool = True,
) -> BiliMCP:
    """装一台客户端。``python`` 留空就用 ``bili_home`` 那套解析顺序。"""
    from .bili_home import resolve_python

    script = Path(server) if server else find_server(repo_root, resource_root, home)
    interpreter, _venv = resolve_python(Path(home), python)
    return BiliMCP(
        command=[interpreter, str(script)],
        home=home,
        timeout=timeout,
        tools=tools,
        style_gate=style_gate,
    )
