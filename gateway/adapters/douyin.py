"""抖音适配器：走 dsh-douyin-scroll 的 MCP 后端（不再用 Playwright 读 DOM）。

为什么换掉 RPA：DOM 选择器会随改版失效，页面还会被风控换成验证码页 —— 线上真实撞过
「私信浮层打不开」「这轮和上一轮窗口对不上」反复跳过。现在：

- 收：``douyin_im_read`` 读会话私信；``douyin_at_me`` 收 @/回复互动
- 发：``douyin_dm`` 发私信；``douyin_comment_reply`` 回具体评论（楼层由它自动判）
- 断线：MCP 子进程挂了自动重启一次；业务失败按可操作类别报错，不静默跳过

窗口增量对齐（``align_window``）保留着：``im_read`` 返回的是"最近 N 条"，
窗口被裁剪时同样需要把上一轮的尾巴对上，否则会重复回或者漏。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from typing import Any

from ..douyin_mcp import build_client
from ..models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult
from ..paths import app_paths
from ..radar.douyin import DouyinRadar
from ..radar.provider import RadarError, ShareDraft
from .base import Adapter

log = logging.getLogger(__name__)

RECONNECT_MIN = 5.0
RECONNECT_MAX = 120.0
WINDOW_KEEP = 40
NOTICE_KEEP = 500


def align_window(previous: list[str], current: list[str]) -> list[int] | None:
    """算出本轮比上轮多了哪几条（按当前窗口下标）。

    先用「上轮后缀 = 本轮前缀」求最长重叠；对不上时退一步，用上轮最后一条在本轮
    **唯一出现**的位置定位。两样都判不出来就返回 None，由调用方跳过这一轮 ——
    宁可漏，不可串。
    """
    if not previous:
        return list(range(len(current)))
    for size in range(min(len(previous), len(current)), 0, -1):
        if previous[-size:] == current[:size]:
            return list(range(size, len(current)))
    if not current:
        return None
    last = previous[-1]
    positions = [index for index, text in enumerate(current) if text == last]
    if len(positions) == 1:
        return list(range(positions[0] + 1, len(current)))
    return None


def _short(text: str, limit: int = 20) -> str:
    return str(text or "").strip().replace("\n", " ")[:limit]


class DouyinAdapter(Adapter):
    platform = Platform.DOUYIN

    def __init__(self, options: dict | None = None, radar: DouyinRadar | None = None) -> None:
        super().__init__()
        opts = options or {}
        self._options = opts
        self._radar = radar
        self._poll = max(5.0, float(opts.get("poll_interval", 30)))
        self._conversations: list[dict[str, str]] = [
            {"to": str(item.get("to") or ""), "to_id": str(item.get("to_id") or "")}
            for item in (opts.get("conversations") or [])
            if isinstance(item, dict) and (item.get("to") or item.get("to_id"))
        ]
        self._auto_discover = bool(opts.get("discover_conversations", True))
        self._max_conversations = max(1, int(opts.get("max_conversations", 5)))
        self._message_limit = max(5, min(int(opts.get("message_limit", 30)), 60))
        self._state: dict[str, dict[str, Any]] = {}
        # 没有检查点工厂时按「已就绪」处理，行为和以前一致；有检查点时由 cp.primed() 决定
        self._primed = True
        self._seen_notices: deque[str] = deque(maxlen=NOTICE_KEEP)
        self._stopping = False

    # ---- 构造 ----

    def _ensure_radar(self) -> DouyinRadar:
        if self._radar is not None:
            return self._radar
        paths = app_paths()
        home = self._options.get("home_dir") or (paths.data_dir / "douyin-home")
        timeout = float(self._options.get("mcp_timeout", 120.0))
        client = build_client(
            repo_root=paths.resource_root,
            home=home,
            resource_root=paths.resource_root,
            node=str(self._options.get("node_path") or ""),
            timeout=timeout,
        )
        self._radar = DouyinRadar(client, home, timeout=timeout)
        log.info("抖音通道使用 MCP 后端：home=%s", home)
        return self._radar

    # ---- 生命周期 ----

    async def start(self) -> None:
        radar = self._ensure_radar()
        try:
            info = await radar.whoami()
        except RadarError as exc:
            if exc.code == "not_logged_in":
                self.mark_needs_human("douyin_login_required")
            elif exc.code in ("process_exited", "timeout", "spawn_failed"):
                self.mark_needs_human("douyin_backend_down")
            raise
        if not info.get("logged_in"):
            self.mark_needs_human("douyin_login_required")
            raise RadarError("not_logged_in", "抖音未登录：先在「刷抖音」里扫码")
        log.info("抖音通道就绪：账号=%s", info.get("nick") or info.get("uid") or "未知")

        backoff = RECONNECT_MIN
        while not self._stopping:
            started = asyncio.get_running_loop().time()
            try:
                await self._poll_once()
                if asyncio.get_running_loop().time() - started > self._poll * 2:
                    backoff = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except RadarError as exc:
                if exc.code == "not_logged_in":
                    log.warning("抖音登录态失效，停止轮询并交给人工处理")
                    self.mark_needs_human("douyin_login_required")
                    return
                log.warning("抖音轮询失败（%s），%.0f 秒后继续", exc.code, backoff)
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)
                continue
            except Exception as exc:  # noqa: BLE001 - 网络/接口异常类型不稳定
                log.warning("抖音轮询异常：%s", type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)
                continue
            await asyncio.sleep(self._poll)

    async def stop(self) -> None:
        self._stopping = True
        if self._radar is not None:
            await self._radar.close()

    # ---- 收 ----

    async def _poll_once(self) -> None:
        radar = self._ensure_radar()
        await self._poll_conversations(radar)
        await self._poll_notices(radar)

    async def _discover(self, radar: DouyinRadar) -> list[dict[str, str]]:
        if not self._auto_discover:
            return []
        try:
            rows = await radar.list_conversations()
        except RadarError as exc:
            log.debug("列会话失败（%s），本轮只用已配置的联系人", exc.code)
            return []
        targets: list[dict[str, str]] = []
        for row in rows:
            nick = str(row.get("nick") or "").strip()
            sec_uid = str(row.get("sec_uid") or "").strip()
            if not nick and not sec_uid:
                continue
            targets.append({"to": nick, "to_id": sec_uid})
        if targets:
            log.info("抖音发现 %d 个会话：%s", len(targets),
                     "、".join(_short(t.get("to") or t.get("to_id") or "") for t in targets[:6]))
        return targets

    def _known_targets(self, discovered: list[dict[str, str]]) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in [*self._conversations, *discovered]:
            key = item.get("to_id") or item.get("to") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged[: self._max_conversations]

    async def _poll_conversations(self, radar: DouyinRadar) -> None:
        targets = self._known_targets(await self._discover(radar))
        for target in targets:
            nick = target.get("to") or target.get("to_id") or ""
            try:
                payload = await radar.im_read(to=target.get("to", ""),
                                              to_id=target.get("to_id", ""),
                                              limit=self._message_limit)
            except RadarError as exc:
                log.debug("读会话「%s」失败（%s），跳过", nick, exc.code)
                continue
            await self._ingest_conversation(nick, payload)

    async def _ingest_conversation(self, nick: str, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        window = [str(item.get("text") or "").strip() for item in messages
                  if isinstance(item, dict)]
        previous = self._state_for(nick)
        if not self._primed:
            # 首次接上这个会话：只建基线，不把窗口里的历史一条条补发出去。
            # seq 从当前时间起步，保证不会和历史上用过的 1、2、3… 撞键被去重丢掉。
            self._state[nick] = {"tail": window[-WINDOW_KEEP:], "seq": int(time.time())}
            self._persist_state(primed=True)
            log.info("抖音私信「%s」建立基线：%d 条历史不补发，之后的新消息才回",
                     nick, len(window))
            return
        fresh = align_window(list(previous.get("tail") or []), window)
        if fresh is None:
            log.warning("会话「%s」窗口对不上，本轮跳过（宁可漏，不可串）", nick)
            return
        seq = int(previous.get("seq") or 0)
        for offset in fresh:
            item = messages[offset]
            text = str(item.get("text") or "").strip()
            if item.get("from") != "them":
                continue
            if not text:
                # 抖音 im_read 不返回图片/表情/文件字段（实测消息只有 awe/from/item/text），
                # 正文为空就是这类非文本消息。别静默吞掉：让模型知道对方发过东西，
                # 只是这边看不到内容，回复时说清楚比装没看见好。
                text = "[对方发来一条这里看不到内容的消息]"
            seq += 1
            # local_id 里的 seq 必须跨重启延续：以前它只活在内存里，适配器一重启就归零，
            # 生成出「dy-<会话>-1」这种已经用过的 id，整条消息被去重丢掉 ——
            # 表现就是「私信发了没反应」，而评论走 dyc-<真实 cid> 所以不受影响。
            local_id = "dy-{}-{}".format(
                hashlib.sha1(nick.encode("utf-8")).hexdigest()[:10], seq)
            await self.emit(InboundMessage(
                platform=Platform.DOUYIN, kind=ChatKind.PRIVATE, chat_id=nick,
                sender_id=nick, sender_name=nick, text=text, msg_id=local_id,
                # 会话标题是昵称，只能是平台内稳定句柄：进白名单才会自动回复
                sender_id_kind="opaque",
            ))
        self._state[nick] = {"tail": window[-WINDOW_KEEP:], "seq": seq}
        self._persist_state()

    # ---- 私信窗口的持久化游标 ----
    # 只放窗口尾部和自增序号，不含任何凭据；宁可重启后补报一条，也不能把新消息当旧的吞掉。
    _STATE_STREAM = "douyin-conversations"

    def _state_for(self, nick: str) -> dict[str, Any]:
        if not getattr(self, "_state_loaded", False):
            self._state_loaded = True
            cp = self.checkpoint(self._STATE_STREAM)
            if cp is not None:
                self._primed = cp.primed()
                raw = cp.state().get("conv")
                if isinstance(raw, dict):
                    self._state = {str(key): dict(value) for key, value in raw.items()
                                   if isinstance(value, dict)}
        return self._state.get(nick) or {}

    def _persist_state(self, *, primed: bool = False) -> None:
        cp = self.checkpoint(self._STATE_STREAM)
        if cp is None:
            return
        try:
            if primed:
                cp.save(state={"conv": self._state}, primed=True)
                self._primed = True
            else:
                cp.save(state={"conv": self._state})
        except Exception:  # noqa: BLE001 - 游标存不进不该拖垮轮询
            log.warning("抖音私信游标没能落库，重启后可能会重复读取一次")

    async def _poll_notices(self, radar: DouyinRadar) -> None:
        try:
            payload = await radar.at_me(limit=10)
        except RadarError as exc:
            log.debug("读「@ 我」失败（%s）", exc.code)
            return
        rows = payload.get("list") if isinstance(payload.get("list"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            comment_id = str(row.get("comment_id") or "").strip()
            aweme_id = str(row.get("aweme_id") or "").strip()
            nick = str(row.get("nickname") or "").strip()
            text = str(row.get("content") or "").strip()
            if not (comment_id and aweme_id and text):
                continue
            # 楼根：notices 里 root_text / reply_to_nickname 都为空 ⇒ 这条自己就是顶层，
            # 楼根就是它自己。有值说明是楼中楼回复，我们不知道楼根 ⇒ 留空让 MCP 自己判。
            in_thread = bool(str(row.get("reply_to_nickname") or "").strip()
                             or str(row.get("root_text") or "").strip())
            root_cid = "" if in_thread else comment_id
            key = f"{aweme_id}:{comment_id}"
            if key in self._seen_notices:
                continue
            self._seen_notices.append(key)
            await self.emit(InboundMessage(
                platform=Platform.DOUYIN, kind=ChatKind.COMMENT,
                chat_id=f"{aweme_id}:{comment_id}:{root_cid}",
                sender_id=nick or comment_id, sender_name=nick,
                text=text, msg_id=f"dyc-{comment_id}", sender_id_kind="opaque",
                raw={"aweme_id": aweme_id, "comment_id": comment_id,
                     "root_comment_id": root_cid,
                     "video_url": row.get("video_url", "")},
            ))

    # ---- 发 ----

    def capabilities(self) -> dict[str, Any]:
        return {
            **super().capabilities(),
            "text_receive": "supported",
            "text_send": "supported",
            "image_receive": "unknown",
            "image_send": "unsupported",
            "max_image_bytes": None,
            "needs_upload": None,
            "comment_send": "supported",     # MCP 能回具体评论（楼层自动判）
            "proactive_private": "supported",
        }

    async def send(self, msg: OutboundMessage) -> SendResult:
        radar = self._ensure_radar()
        try:
            if msg.kind == ChatKind.COMMENT:
                return await self._send_comment(radar, msg)
            payload = await radar.send_text(msg.chat_id, msg.text, confirm=True)
        except RadarError as exc:
            return SendResult(ok=False, error=exc.safe_message)
        if payload.get("sent"):
            return SendResult(ok=True, msg_id=str(payload.get("msg_id") or ""))
        return SendResult(ok=False, error=str(payload.get("note") or "抖音没有确认发送"))

    async def _send_comment(self, radar: DouyinRadar, msg: OutboundMessage) -> SendResult:
        # chat_id 是 ``aweme_id:comment_id:root_comment_id``；老记录只有两段，
        # 那就没有楼根，交给 MCP 自己判。
        aweme_id, _, rest = msg.chat_id.partition(":")
        comment_id, _, root_cid = rest.partition(":")
        if not aweme_id:
            return SendResult(ok=False, error="评论目标缺少视频 id")
        try:
            payload = await radar.comment_reply(
                aweme_id=aweme_id, text=msg.text, comment_id=comment_id,
                root_comment_id=root_cid, confirm=True)
        except RadarError as exc:
            return SendResult(ok=False, error=exc.safe_message)
        if payload.get("sent") is False:
            return SendResult(ok=False, error=str(payload.get("note") or "评论没有确认发出"))
        return SendResult(ok=True, msg_id=str(payload.get("cid") or ""))

    # ---- 看视频（不读画面）----

    async def video_brief(self, ref: str, *, max_comments: int = 20) -> dict:
        """只给评论与链接，并明确说明读不到画面。"""
        radar = self._ensure_radar()
        item_id = str(ref or "").strip()
        if "/video/" in item_id:
            item_id = item_id.split("/video/", 1)[1]
        if "modal_id=" in item_id:
            item_id = item_id.split("modal_id=", 1)[1]
        item_id = item_id.split("&")[0].split("?")[0].strip("/ ").strip()
        if not item_id.isdigit():
            return {"ok": False,
                    "error": f"没认出视频：{ref!r}（给纯数字 aweme_id 或 /video/ 链接）"}
        try:
            comments = await radar.comments(item_id, limit=max(1, min(max_comments, 20)))
        except RadarError as exc:
            return {"ok": False, "error": exc.safe_message}
        return {
            "ok": True,
            "aweme_id": item_id,
            "url": f"https://www.douyin.com/video/{item_id}",
            "comments": [{"text": c.text, "likes": c.likes} for c in comments],
            "note": "只能读文案与评论，看不到画面，也没有字幕。",
        }

    # ---- 内容雷达（给界面用）----

    async def scan(self, *, top: int = 10) -> list[dict[str, Any]]:
        radar = self._ensure_radar()
        candidates = await radar.candidates(top=top)
        return [item.as_dict() for item in candidates]

    async def share(self, *, item_id: str, to: str, reason: str,
                    confirm: bool = False) -> dict[str, Any]:
        radar = self._ensure_radar()
        draft = ShareDraft(item_id=item_id, to=to, reason=reason, preview="")
        result = await radar.send_share(draft, confirm=confirm)
        return {"sent": result.sent, "to": result.to, "preview": result.preview,
                "detail": result.detail}


def build(options: dict) -> DouyinAdapter:
    """构造适配器；真正的 MCP 连接在第一次 start 时才建立。"""
    adapter = DouyinAdapter(options)
    adapter._ensure_radar()   # 早失败：路径/Node 不对时立刻暴露，不等到第一条消息
    return adapter
