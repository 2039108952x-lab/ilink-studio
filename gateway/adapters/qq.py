"""QQ 适配器：OneBot 11 over WebSocket。

方向选的是「网关主动连协议端」而不是反向 WS —— 网关不监听任何端口，
出问题只影响自己，也不用在 NapCat 上放开什么。

协议端可以是 NapCatQQ / LLOneBot / Lagrange，只要它讲 OneBot 11 就行。
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import time
from pathlib import Path
from typing import Any

import websockets

from ..models import Attachment, ChatKind, InboundMessage, OutboundMessage, Platform, SendResult
from .base import Adapter

log = logging.getLogger(__name__)

RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
API_TIMEOUT = 30.0


# ---------- 纯函数：OneBot 报文 → 我们的消息模型 ----------


def parse_segments(raw: Any) -> list[dict]:
    """OneBot 的 message 可能是段数组、纯字符串或 CQ 码。"""
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    if isinstance(raw, str):
        return [{"type": "text", "data": {"text": raw}}]
    return []


def text_of(segments: list[dict]) -> str:
    """把段数组拼成纯文本，非文本段用占位符保留位置。"""
    out: list[str] = []
    for seg in segments:
        kind = seg.get("type")
        data = seg.get("data") or {}
        if kind == "text":
            out.append(str(data.get("text", "")))
        elif kind == "at":
            qq = str(data.get("qq", ""))
            out.append("@全体成员" if qq == "all" else f"@{qq}")
        elif kind == "image":
            out.append("[图片]")
        elif kind == "face":
            out.append("[表情]")
        elif kind == "record":
            out.append("[语音]")
        elif kind == "video":
            out.append("[视频]")
        elif kind == "file":
            out.append(f"[文件:{data.get('name', '')}]")
    return "".join(out).strip()


def mentions(segments: list[dict], self_id: str) -> bool:
    """有没有 @ 我这个号。"""
    for seg in segments:
        if seg.get("type") != "at":
            continue
        qq = str((seg.get("data") or {}).get("qq", ""))
        if qq and (qq == self_id or qq == "all"):
            return True
    return False


def images_of(segments: list[dict]) -> list[Attachment]:
    """把 image 段抽成附件。优先本地文件路径，其次网络 url。"""
    out: list[Attachment] = []
    for seg in segments:
        if seg.get("type") != "image":
            continue
        data = seg.get("data") or {}
        raw_file = str(data.get("file") or "")
        local = Path(raw_file) if raw_file else None
        out.append(
            Attachment(
                kind="image",
                url=str(data.get("url") or ""),
                path=str(local) if local is not None and local.is_file() else "",
                name=local.name if local is not None else raw_file,
            )
        )
    return out


def to_inbound(event: dict, self_id: str) -> InboundMessage | None:
    """一条 OneBot 消息事件 → InboundMessage；不是消息事件就返回 None。"""
    if event.get("post_type") != "message":
        return None
    segs = parse_segments(event.get("message"))
    attachments = images_of(segs)
    text = text_of(segs)
    if attachments:
        # 图片已经单独交给 brain 了，占位符留着只会让模型以为正文是「[图片]」
        text = text.replace("[图片]", "").strip()
    if not text and not attachments:
        return None

    mtype = event.get("message_type")
    sender = event.get("sender") or {}
    if mtype == "group":
        kind = ChatKind.GROUP
        chat_id = str(event.get("group_id", ""))
    else:
        kind = ChatKind.PRIVATE
        chat_id = str(event.get("user_id", ""))

    # OneBot 事件自带平台时间；QQ 号是稳定 uid，可以参与白名单匹配。
    try:
        occurred = float(event.get("time") or 0) or time.time()
        ts_source = "platform" if event.get("time") else "received"
    except (TypeError, ValueError):
        occurred, ts_source = time.time(), "received"

    return InboundMessage(
        platform=Platform.QQ,
        kind=kind,
        chat_id=chat_id,
        sender_id=str(event.get("user_id", "")),
        sender_name=str(sender.get("card") or sender.get("nickname") or ""),
        text=text,
        msg_id=str(event.get("message_id", "")),
        is_mention_me=mentions(segs, self_id),
        attachments=attachments,
        raw=event,
        ts=occurred,
        ts_source=ts_source,
        sender_id_kind="stable",
    )


# ---------- 适配器 ----------


class QQAdapter(Adapter):
    platform = Platform.QQ

    def __init__(self, options: dict | None = None) -> None:
        super().__init__()
        opts = options or {}
        self._url: str = opts.get("onebot_ws", "ws://127.0.0.1:3001")
        self._token: str = opts.get("access_token", "")
        self._self_id: str = str(opts.get("self_id", ""))
        # 群聊是否必须 @ 我才理；私聊默认都理
        self._require_mention: bool = bool(opts.get("require_mention", True))

        self._ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = itertools.count(1)
        self._running = False
        self._ready = asyncio.Event()

    # ---- 生命周期 ----

    async def start(self) -> None:
        self._running = True
        backoff = RECONNECT_MIN
        while self._running:
            try:
                await self._session()
                if not self._running:
                    break
                log.warning("QQ 连接关闭，准备重连")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断线原因很多，统一重试
                log.warning("QQ 连接异常：%s", exc)
            self._ready.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _session(self) -> None:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with websockets.connect(
            self._url, additional_headers=headers or None, ping_interval=30
        ) as ws:
            self._ws = ws
            self._ready.set()
            log.info("已连上 OneBot 协议端：%s", self._url)
            async for frame in ws:
                self._handle_frame(frame)
            self._ws = None

    # ---- 收 ----

    def _handle_frame(self, frame: str | bytes) -> None:
        try:
            data = json.loads(frame)
        except (json.JSONDecodeError, TypeError):
            log.debug("收到非 JSON 帧，忽略")
            return
        if not isinstance(data, dict):
            return

        echo = data.get("echo")
        if echo and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(data)
            return

        # 事件：同步处理，真正的 emit 放到事件循环里
        asyncio.create_task(self._dispatch_event(data))

    async def _dispatch_event(self, event: dict) -> None:
        if event.get("post_type") == "meta_event":
            if sid := event.get("self_id"):
                self._self_id = str(sid)
            return

        msg = to_inbound(event, self._self_id)
        if msg is None:
            return
        if msg.kind is ChatKind.GROUP and self._require_mention and not msg.is_mention_me:
            return
        if self._self_id and msg.sender_id == self._self_id:
            return  # 别理自己
        await self._resolve_attachments(msg)
        await self.emit(msg)

    async def _resolve_attachments(self, msg: InboundMessage) -> None:
        """把图片换成可用的本地路径。

        NapCat 通常给一个外网 URL，本机 DNS 一旦抽风就下不动（实测过：
        getaddrinfo failed）。QQ 客户端本地其实已经缓存了这张图，
        所以问协议端要本地路径，别去网上拿。
        """
        for att in msg.attachments:
            if att.kind != "image" or (att.path and Path(att.path).is_file()):
                continue
            try:
                resp = await self._call("get_image", {"file": att.name or att.url})
            except Exception as exc:  # noqa: BLE001 - 拿不到就退回 url 下载
                log.debug("问协议端要本地图片失败：%s", exc)
                continue
            data = resp.get("data") or {}
            local = str(data.get("file") or data.get("path") or "")
            if local and Path(local).is_file():
                att.path = local
                log.debug("图片改用本地缓存：%s", local)

    # ---- 发 ----

    def capabilities(self) -> dict[str, Any]:
        # OneBot 11 的图片用 base64:// 直接塞进消息段，不需要先上传 CDN。
        return {
            **super().capabilities(),
            "text_receive": "supported",
            "text_send": "supported",
            "image_receive": "supported",
            "image_send": "supported",
            "max_image_bytes": 5 * 1024 * 1024,
            "needs_upload": False,
            "comment_send": "unsupported",
            "proactive_private": "supported",
        }

    async def send(self, msg: OutboundMessage) -> SendResult:
        if self._ws is None:
            return SendResult(ok=False, error="QQ 还没连上协议端")
        if msg.kind is ChatKind.GROUP:
            action = "send_group_msg"
            params: dict = {"group_id": _as_id(msg.chat_id)}
        else:
            action = "send_private_msg"
            params = {"user_id": _as_id(msg.chat_id)}

        segments = self._segments_of(msg)
        params["message"] = segments if segments else msg.text

        echo = f"gw{next(self._seq)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            resp = await asyncio.wait_for(fut, API_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            return SendResult(ok=False, error=f"{action} 超时")
        except Exception as exc:  # noqa: BLE001
            self._pending.pop(echo, None)
            return SendResult(ok=False, error=str(exc))

        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            return SendResult(ok=False, error=f"{resp.get('retcode')}: {resp.get('message', resp)}")
        data = resp.get("data") or {}
        return SendResult(ok=True, msg_id=str(data.get("message_id", "")))

    async def _call(self, action: str, params: dict) -> dict:
        """通用 OneBot API 调用：发请求、等 echo 回包。失败就抛。"""
        if self._ws is None:
            raise RuntimeError("还没连上协议端")
        echo = f"gw{next(self._seq)}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            return await asyncio.wait_for(fut, API_TIMEOUT)
        finally:
            self._pending.pop(echo, None)

    @staticmethod
    def _segments_of(msg: OutboundMessage) -> list[dict]:
        """文本 + 引用 + 图片拼成 OneBot 段数组。没有附件时也走数组，行为一致。"""
        segments: list[dict] = []
        if msg.reply_to and msg.reply_to.isdigit():
            segments.append({"type": "reply", "data": {"id": msg.reply_to}})
        if msg.text:
            segments.append({"type": "text", "data": {"text": msg.text}})
        for att in msg.attachments:
            if att.kind != "image":
                continue
            payload = _image_payload(att)
            if payload:
                segments.append(
                    {"type": "image", "data": {"file": payload, "summary": "[动画表情]"}}
                )
        return segments


def _image_payload(att: Attachment) -> str:
    """本地文件转 base64://，让 NapCat 不用管路径；有 url 就直接用。"""
    if att.path:
        local = Path(att.path)
        if local.is_file():
            return "base64://" + base64.b64encode(local.read_bytes()).decode("ascii")
    return att.url


def _as_id(value: str) -> Any:
    """OneBot 要数字 id；转不动就原样传，让它自己报错。"""
    return int(value) if value.isdigit() else value


def build(options: dict) -> QQAdapter:
    """main.py 的工厂入口。"""
    return QQAdapter(options)
