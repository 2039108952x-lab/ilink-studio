"""抖音的 RadarProvider 实现：把 dsh-douyin-scroll 的 MCP 工具包成平台无关接口。

只做映射与错误归类，不复制判断逻辑 —— 硬筛/比值/语义判断都在 ``radar.pipeline``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..douyin_mcp import DouyinMCP, DouyinMCPError, describe_error
from . import memory as memory_mod
from .gate import SendGate
from .models import Candidate, CommentSnapshot
from .provider import RadarError, ShareDraft

log = logging.getLogger(__name__)

# 平台字段 → 内核统一字段
_STAT_KEYS = {"digg": "like", "comment": "comment", "collect": "collect", "share": "share"}

# 用来触发「列会话」：MCP 没暴露 list，im_read 找不到这个昵称时会把 candidates 吐出来
PROBE_NICK = "__ilink_probe__"


def _classify(payload: dict[str, Any]) -> str:
    text = describe_error(payload)
    if "还没有登录" in text or "未登录" in text:
        return "not_logged_in"
    if "运行环境" in text or "curl_cffi" in text:
        return "deps_missing"
    if "限流" in text or "rate_limited" in jsonish(payload):
        return "rate_limited"
    if "缺 name" in text or "缺 " in text:
        return "invalid_args"
    return "tool_failed"


def jsonish(payload: dict[str, Any]) -> str:
    return str(payload.get("error") or "") + str(payload.get("hint") or "")


class DouyinRadar:
    """一个实例对应一个 MCP 子进程；由适配器持有并复用。"""

    platform = "douyin"
    name = "douyin"
    # 能力声明：pipeline 靠这三个开关决定要不要取评论、允不允许发卡片 / 文本
    supports_share = True
    supports_text = True
    supports_comment = True

    def __init__(self, client: DouyinMCP, home: Path | str, *,
                 timeout: float = 120.0,
                 gate: SendGate | None = None) -> None:
        self._client = client
        self._home = Path(home)
        self._timeout = timeout
        # 发送闸：只有 confirm=True 的真发送才过它，预览/草稿一律不拦
        self._gate = gate if gate is not None else SendGate(self._home)

    # ---- 底层 ----

    async def _call(self, tool: str, args: dict[str, Any] | None = None,
                    *, timeout: float | None = None) -> dict[str, Any]:
        """调用工具；业务失败转成 RadarError（带 code）。"""
        try:
            result = await self._client.call(tool, args or {}, timeout=timeout or self._timeout)
        except DouyinMCPError as exc:
            raise RadarError(exc.code, exc.safe_message, retryable=exc.retryable) from exc
        if not result.ok:
            raise RadarError(_classify(result.data), result.error or "抖音工具返回失败",
                             retryable=False, payload=result.data)
        return result.data

    async def _send(self, tool: str, args: dict[str, Any], *, channel: str,
                    confirm: bool, timeout: float | None = None) -> dict[str, Any]:
        """发送类工具的统一入口。

        真发送（``confirm=True``）先过闸；撞上限流就把冷却写盘 ——
        上游的冷却是子进程内存里的，进程一重启就清零，指望它记住是不行的。
        """
        if confirm:
            self._gate.check(channel)
        try:
            payload = await self._call(tool, args, timeout=timeout)
        except RadarError as exc:
            if exc.code == "rate_limited":
                self._gate.record_limited(channel)
            raise
        if confirm and payload.get("rate_limited"):
            self._gate.record_limited(channel)
            raise RadarError("rate_limited",
                             str(payload.get("error") or "抖音限流，别重试"),
                             payload=payload)
        if confirm and payload.get("sent"):
            self._gate.record_sent(channel)
        return payload

    async def close(self) -> None:
        await self._client.close()

    @property
    def alive(self) -> bool:
        return self._client.alive

    # ---- RadarProvider ----

    async def whoami(self) -> dict:
        payload = await self._call("douyin_whoami")
        return {
            "ok": True,
            "logged_in": bool(payload.get("logged_in")),
            "nick": str(payload.get("nick") or ""),
            "uid": str(payload.get("uid") or ""),
            "raw": payload,
        }

    async def status(self) -> dict:
        return await self._call("douyin_status")

    async def doctor(self) -> dict:
        """体检：解出 payload 再返回。

        这里不能把 ``CallResult`` 直接丢给控制面 —— 它是 dataclass，
        ``json.dumps`` 会抛 TypeError，前端只看到 socket hang up。
        体检失败也照旧返回报告，由调用方看 ``ok``。
        """
        try:
            result = await self._client.call(
                "douyin_doctor", {}, timeout=max(self._timeout, 120.0))
        except DouyinMCPError as exc:
            raise RadarError(exc.code, exc.safe_message) from exc
        payload = dict(result.data or {})
        if not result.ok:
            payload["ok"] = False
            payload.setdefault("error", result.error or "体检没返回结果")
        return payload

    async def guide(self) -> str:
        payload = await self._call("douyin_guide")
        return str(payload.get("doc") or "")

    async def candidates(self, *, top: int = 10) -> list[Candidate]:
        payload = await self._call("douyin_forward", {"top": max(1, int(top))},
                                   timeout=max(self._timeout, 180.0))
        rows = payload.get("list")
        if not isinstance(rows, list):
            return []
        result: list[Candidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("aweme_id") or "").strip()
            if not item_id:
                continue
            stats = {
                target: int(row.get(source) or 0)
                for source, target in _STAT_KEYS.items()
            }
            duration = row.get("duration_s")
            result.append(Candidate(
                platform="douyin",
                item_id=item_id,
                title=str(row.get("desc") or "").strip(),
                author=str(row.get("author") or "").strip(),
                duration_sec=int(duration) if isinstance(duration, (int, float)) else None,
                url=str(row.get("share_url") or row.get("web_url") or ""),
                stats=stats,
                extra={"age_h": row.get("age_h"), "web_url": row.get("web_url")},
            ))
        return result

    async def comments(self, item_id: str, *, limit: int = 5) -> list[CommentSnapshot]:
        try:
            payload = await self._call("douyin_comments", {"aweme_id": item_id})
        except RadarError as exc:
            # 单条拉不到评论不影响整轮判断
            log.warning("拉评论失败（%s）：%s", item_id, exc.code)
            return []
        rows = payload.get("comments") or payload.get("list") or payload.get("items") or []
        result: list[CommentSnapshot] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or row.get("content") or "").strip()
                if not text:
                    continue
                likes = row.get("likes") or row.get("like") or row.get("digg") or 0
                result.append(CommentSnapshot(text=text[:200], likes=int(likes or 0)))
        result.sort(key=lambda item: item.likes, reverse=True)
        return result[:max(1, limit)]

    def share_url(self, candidate: Candidate) -> str:
        return candidate.url or str(candidate.extra.get("web_url") or "")

    async def send_share(self, draft: ShareDraft, *, confirm: bool = False) -> ShareDraft:
        args: dict[str, Any] = {"aweme_id": draft.item_id, "to": draft.to}
        if draft.reason:
            args["reason"] = draft.reason
        if confirm:
            args["confirm"] = True
        payload = await self._send("douyin_share_card", args, channel="dm",
                                   confirm=confirm,
                                   timeout=max(self._timeout, 240.0))
        return ShareDraft(
            item_id=draft.item_id,
            to=str(payload.get("to") or draft.to),
            reason=draft.reason,
            preview=str(payload.get("preview") or payload.get("note") or ""),
            sent=bool(payload.get("sent")),
            detail=describe_error(payload) if not payload.get("sent") else "",
        )

    async def send_text(self, to: str, text: str, *, confirm: bool = False) -> dict:
        args: dict[str, Any] = {"to": to, "text": text}
        if confirm:
            args["confirm"] = True
        return await self._send("douyin_dm", args, channel="dm", confirm=confirm,
                                timeout=max(self._timeout, 240.0))

    async def remember(self, to: str, facts: list[str]) -> dict:
        """MCP 没有 douyin_remember；直接写插件的 dm 记忆文件（它下次读会话会带上）。"""
        written = 0
        for fact in facts:
            text = str(fact).strip()
            if not text:
                continue
            result = memory_mod.remember_from_message(self._home, to, text)
            if result.get("ok"):
                written += 1
        return {"ok": written > 0, "written": written, "file": str(self._home / "memory")}

    # ---- 平台特有（评论 / 私信会话）----

    async def notices(self, *, raw: bool = False) -> dict:
        return await self._call("douyin_notices", {"raw": True} if raw else {})

    async def at_me(self, *, limit: int = 10) -> dict:
        """@ 我 / 回复我的通知。

        用 ``douyin_notices`` 而不是 ``douyin_at_me``：前者多带 ``root_text``、
        ``reply_to_nickname``、``schema_url`` 这几个字段，正是判楼根要用的。
        后者只有 nickname/content/aweme_id/comment_id，回评论时判不出楼根，
        只能整条拒发（真机上就是这样一条都发不出去）。
        """
        payload = await self._call("douyin_notices", {}, timeout=max(self._timeout, 180.0))
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        rows = [row for row in items if isinstance(row, dict) and row.get("comment_id")]
        return {"ok": True, "at_n": len(rows), "list": rows[:max(1, int(limit))]}

    async def im_read(self, *, to: str = "", to_id: str = "", limit: int = 30) -> dict:
        args: dict[str, Any] = {"limit": max(1, min(int(limit), 60))}
        if to:
            args["to"] = to
        if to_id:
            args["to_id"] = to_id
        return await self._call("douyin_im_read", args, timeout=max(self._timeout, 240.0))

    async def comment_reply(self, *, aweme_id: str, text: str, comment_id: str = "",
                            root_comment_id: str = "", confirm: bool = False) -> dict:
        args: dict[str, Any] = {"aweme_id": aweme_id, "text": text}
        if comment_id:
            args["comment_id"] = comment_id
        if root_comment_id:
            args["root_comment_id"] = root_comment_id
        if confirm:
            args["confirm"] = True
        return await self._send("douyin_comment_reply", args,
                                channel="comment", confirm=confirm)

    async def post_comment(self, *, item_id: str, text: str, reply_to: int = 0,
                           root: int = 0, confirm: bool = False) -> dict:
        """统一签名的发评论入口（网关和 MCP 工具只认这个）。

        抖音这边 ``reply_to`` 是 comment_id、``root`` 是 root_comment_id，
        传 0 / 空就当作发表新评论。
        """
        return await self.comment_reply(
            aweme_id=item_id, text=text,
            comment_id=str(reply_to) if reply_to else "",
            root_comment_id=str(root) if root else "",
            confirm=confirm,
        )

    async def comment_mention(self, *, aweme_id: str, name: str, text: str,
                              confirm: bool = False, dry: bool = False) -> dict:
        args: dict[str, Any] = {"aweme_id": aweme_id, "name": name, "text": text}
        if dry:
            args["dry"] = True
        if confirm:
            args["confirm"] = True
        return await self._send("douyin_comment_mention", args,
                                channel="comment", confirm=confirm,
                                timeout=max(self._timeout, 300.0))

    async def list_conversations(self) -> list[dict[str, Any]]:
        """列会话：MCP 没暴露 list，借 im_read 的失败返回体拿 candidates。

        `im_read` 找不到昵称时会把候选会话一起返回，这是唯一不用自己起浏览器的发现路径。
        `to` 传一个绝不会存在的昵称来触发这个分支。
        """
        try:
            await self._call("douyin_im_read", {"to": PROBE_NICK, "limit": 1})
        except RadarError as exc:
            rows = exc.payload.get("candidates") if isinstance(exc.payload, dict) else None
            if not isinstance(rows, list):
                return []
            return [
                {
                    "nick": str(row.get("nick") or "").strip(),
                    "sec_uid": str(row.get("sec_uid") or "").strip(),
                    "unread": int(row.get("unread") or 0),
                }
                for row in rows
                if isinstance(row, dict)
            ]
        return []
