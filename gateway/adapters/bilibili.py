"""B站适配器：私信 + @/评论区 + 看视频。

不做直播间弹幕 —— 那是另一个场景。（历史记录：自拼二进制协议被 B 站拒，
换 `LiveDanmaku` 能连能收，但和本适配器的目标不重叠，相关代码已删除。）

收
    SessionListener   `bilibili_api.session.Session` 轮询新私信。
                      库内部固定 6 秒一轮（原作者注释：3 秒会被风控封号），别调快。
    NotifyPoller      定时拉 `get_at()` / `get_replies()`。
                      B站这两类通知没有推送通道，只能轮询，间隔给到 45 秒。

发
    BiliPoster        私信走 `session.send_msg`，评论回复走 `comment.send_comment`。

看视频
    video_brief()     元信息 + CC 字幕，给 brain 当上下文，也给 MCP 工具当实现。

凭据（全部走环境变量，缺一个都跑不起来，因为所有接口都要登录态）
    BILI_SESSDATA / BILI_JCT / BILI_DEDEUSERID / BILI_BUVID3
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from ..models import (
    Attachment,
    ChatKind,
    InboundMessage,
    OutboundMessage,
    Platform,
    SendResult,
)
from .base import Adapter

log = logging.getLogger(__name__)

# 这四个缺一不可：Session 要 SESSDATA，发消息要 bili_jct，
# 拼 sender_uid 要 DedeUserID，缺 buvid3 时 B 站会把请求当未登录设备。
CRED_ENV = ("BILI_SESSDATA", "BILI_JCT", "BILI_DEDEUSERID", "BILI_BUVID3")

NOTIFY_INTERVAL = 45.0      # @/回复轮询间隔，太密会触发风控
CRED_RETRY_MAX = 300.0      # 凭据失效后最长退避

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SUBTITLE_MAX_CHARS = 6000   # 字幕喂给模型的截断上限，防上下文炸掉

InboundCallback = Callable[[InboundMessage], None]

# 登录助手（scripts/bili_login.py）把凭据写在这儿；环境变量优先，这个只是兜底。
# data/ 在 gitignore 里，所以登录凭据不会进版本库。
COOKIE_FILE = Path(__file__).resolve().parents[2] / "data" / "bili-cookies.json"

# 已处理的通知 id。必须落盘，否则每次重启都会把旧通知再回复一遍。
_SEEN_KEEP = 2000


# ---------------------------------------------------------------- 凭据


def _load_cookie_file() -> dict[str, str]:
    """读登录助手写的 cookie 文件。读不到就返回空，不抛。"""
    try:
        if COOKIE_FILE.is_file():
            data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
    except Exception:  # noqa: BLE001 - 文件损坏/权限问题都不该拖垮网关
        log.debug("读 B站 cookie 文件失败：%s", COOKIE_FILE, exc_info=True)
    return {}


def credential_value(name: str) -> str:
    """按「环境变量 > cookie 文件」取一个凭据值。"""
    return os.environ.get(name) or _load_cookie_file().get(name, "")


def missing_credential_env(vault=None) -> list[str]:
    """返回缺了哪些凭据；空列表 = 齐了。桌面模式优先 DPAPI 保险库。"""
    missing = []
    for name in CRED_ENV:
        value = credential_value(name)
        if not value and vault is not None:
            try:
                value = vault.get("bilibili:cookie", name)
            except Exception:
                value = ""
        if not value:
            missing.append(name)
    return missing


def build_credential(vault=None) -> Any:
    """造一个 `bilibili_api.Credential`。

    库是懒导入的：没装 bilibili_api 时网关其它平台仍能起。

    `BILI_PROXY` 可选：本机 DNS 对 B站 域名时通时不通时，配一个能解析的代理
    （如 `http://127.0.0.1:7897`）把库的请求整体送过去。
    """
    from bilibili_api import Credential

    def secret(name: str) -> str:
        value = credential_value(name)
        if not value and vault is not None:
            try:
                value = vault.get("bilibili:cookie", name)
            except Exception:
                value = ""
        return value

    return Credential(
        sessdata=secret("BILI_SESSDATA") or None,
        bili_jct=secret("BILI_JCT") or None,
        dedeuserid=secret("BILI_DEDEUSERID") or None,
        buvid3=secret("BILI_BUVID3") or None,
        proxy=credential_value("BILI_PROXY") or None,
    )


# ---------------------------------------------------------------- 视频引用


_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.IGNORECASE)


def parse_video_ref(text: str) -> dict[str, Any]:
    """从 BV 号 / av 号 / 完整链接里认出视频。

    返回 `{"bvid": ...}` 或 `{"aid": ...}`；认不出返回空 dict。
    两者都给不出来时调用方该直接告诉用户「没认出视频」。
    """
    if not text:
        return {}
    if m := _BV_RE.search(text):
        return {"bvid": m.group(1)}
    if m := _AV_RE.search(text):
        return {"aid": int(m.group(1))}
    return {}


def _first_page_cid(info: dict) -> int:
    """取第一个分P 的 cid —— 字幕是按 cid 存的，不传就拿不到。"""
    pages = info.get("pages") or []
    if pages and isinstance(pages[0], dict):
        try:
            return int(pages[0].get("cid") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _pick_chinese_subtitle(payload: dict) -> dict | None:
    """字幕列表里挑一条：优先人工中文，其次 AI 中文，最后任意一条。"""
    subs = (payload or {}).get("subtitles") or []
    if not isinstance(subs, list) or not subs:
        return None
    for lan in ("zh-CN", "zh-Hans", "zh"):
        for sub in subs:
            if isinstance(sub, dict) and sub.get("lan") == lan:
                return sub
    for sub in subs:
        if isinstance(sub, dict) and str(sub.get("lan", "")).startswith("ai-zh"):
            return sub
    return subs[0] if isinstance(subs[0], dict) else None


def _subtitle_text(body: dict) -> str:
    """把字幕 JSON（`{"body":[{"content": "..."}]}`）拼成一段纯文本。"""
    lines: list[str] = []
    for row in (body or {}).get("body") or []:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content", "")).strip()
        if content:
            lines.append(content)
        if sum(len(x) for x in lines) > SUBTITLE_MAX_CHARS:
            break
    return " ".join(lines)[:SUBTITLE_MAX_CHARS]


async def _fetch_subtitle_body(url: str, transport: httpx.AsyncBaseTransport | None) -> str:
    """字幕正文是另一个 JSON，`subtitle_url` 常以 `//` 开头，要补协议。"""
    if url.startswith("//"):
        url = "https:" + url
    kwargs: dict[str, Any] = {"timeout": 20, "headers": {"User-Agent": UA}}
    if transport is not None:
        kwargs["transport"] = transport      # 测试注入，和 proxy 互斥
    elif proxy := credential_value("BILI_PROXY"):
        kwargs["proxy"] = proxy              # DNS 挡了 CDN 域名时走代理
    async with httpx.AsyncClient(**kwargs) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _subtitle_text(resp.json())


async def video_brief(
    ref: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """拿视频元信息 + CC 字幕。

    失败不抛异常，一律回 `{"ok": False, "error": ...}`，
    因为调用方是模型工具，报错要能读，不能让整轮对话崩掉。
    """
    target = parse_video_ref(ref)
    if not target:
        return {"ok": False, "error": f"没认出视频：{ref!r}（给 BV 号、av 号或视频链接都行）"}

    # MCP 工具是独立进程调过来的，没有经过 adapter.build()，这里也要装一次（幂等）
    from .. import doh

    doh.install()

    try:
        from bilibili_api import video as bvideo

        # 取字幕要过 get_player_info，那个接口要求登录态；没有凭据就匿名试，
        # 拿不到字幕也不影响元信息，所以这里不把它当致命错误。
        cred = build_credential() if not missing_credential_env() else None
        v = bvideo.Video(**target, credential=cred)
        info = await v.get_info()
    except Exception as exc:  # noqa: BLE001 - 库的异常类型不稳定，且要喂给模型
        return {"ok": False, "error": f"取视频信息失败：{exc}"}

    owner = info.get("owner") or {}
    stat = info.get("stat") or {}
    brief: dict[str, Any] = {
        "ok": True,
        "bvid": info.get("bvid") or target.get("bvid", ""),
        "aid": info.get("aid") or target.get("aid", 0),
        "title": info.get("title", ""),
        "up": owner.get("name", ""),
        "desc": (info.get("desc") or "").strip(),
        "duration_sec": info.get("duration", 0),
        "pubdate": info.get("pubdate", 0),
        "view": stat.get("view", 0),
        "like": stat.get("like", 0),
        "pages": len(info.get("pages") or []),
        "subtitle": "",
    }

    cid = _first_page_cid(info)
    if not cid:
        brief["subtitle_note"] = "拿不到 cid，跳过字幕"
        return brief

    try:
        payload = await v.get_subtitle(cid=cid)
    except Exception as exc:  # noqa: BLE001
        brief["subtitle_note"] = f"取字幕列表失败：{exc}"
        return brief

    picked = _pick_chinese_subtitle(payload or {})
    if not picked:
        brief["subtitle_note"] = "这个视频没有 CC 字幕"
        return brief

    try:
        host = picked.get("subtitle_url", "")
        brief["subtitle"] = await _fetch_subtitle_body(str(host), transport)
        brief["subtitle_lan"] = picked.get("lan", "")
    except Exception as exc:  # noqa: BLE001
        # 字幕正文在 i0.hdslb.com 这类 CDN 上，本机 DNS 偶尔会拒解析这个域名。
        # 报「getaddrinfo failed」对用户没有意义，换成能直接对症的话。
        detail = str(exc)
        if "getaddrinfo" in detail or "Name or service not known" in detail:
            brief["subtitle_note"] = (
                "字幕 CDN 域名解析失败（i0.hdslb.com），本机 DNS 拦了它；"
                "浏览器能看是因为它自己走 DoH。换 DNS 或把这个域名放进代理规则可解"
            )
        else:
            brief["subtitle_note"] = f"字幕下载失败：{detail}"
    return brief


# ---------------------------------------------------------------- 事件解析


def _describe_non_text(content: Any) -> tuple[str, Attachment | None]:
    """把图片/分享视频对象转成「一句人能读懂的话」+ 可选附件。

    图片同时给文本里的链接和 attachment：
    文本兜底（下载失败时模型至少知道有张图），attachment 让 brain 真去看图。
    分享视频不是图片，只出链接 —— 视频下载下来也没用，模型又不能看画面。
    """
    if isinstance(content, (int, float)):
        return str(content), None

    bvid = getattr(content, "bvid", None)
    if callable(bvid):
        bvid = None
    aid = getattr(content, "aid", None)
    if callable(aid):
        aid = None
    if bvid:
        return f"[分享视频] https://www.bilibili.com/video/{bvid}", None
    if aid:
        return f"[分享视频] https://www.bilibili.com/video/av{aid}", None

    url = getattr(content, "url", "")
    if isinstance(url, str) and url:
        return f"[图片] {url}", Attachment(kind="image", url=url)
    return f"[{type(content).__name__}]", None


def inbound_from_event(event: Any) -> InboundMessage | None:
    """私信事件 → 统一消息。自己发的、认不出的一律丢。"""
    try:
        sender = int(getattr(event, "sender_uid", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not sender:
        return None

    content = getattr(event, "content", "")
    attachments: list[Attachment] = []
    if isinstance(content, str):
        text = content
    else:
        text, attachment = _describe_non_text(content)
        if attachment is not None:
            attachments.append(attachment)
    if not text.strip():
        return None

    ts = getattr(event, "timestamp", 0) or 0
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        ts_f = time.time()

    return InboundMessage(
        platform=Platform.BILIBILI,
        kind=ChatKind.PRIVATE,
        chat_id=str(sender),
        sender_id=str(sender),
        sender_name="",
        text=text.strip(),
        msg_id=str(getattr(event, "msg_key", "") or ""),
        is_mention_me=True,          # 私信本来就是找我
        attachments=attachments,
        ts=ts_f or time.time(),
        ts_source="platform" if ts else "received",
        sender_id_kind="stable",     # B站 sender_uid 是数字 uid
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def inbound_from_notice(item: dict, *, source: str) -> InboundMessage | None:
    """@我的 / 收到的回复 → 统一消息。

    真实字段（2026-10 实测）：
        item.subject_id     视频 aid，回评论的 oid
        item.source_id      发通知的那条评论自己的 rpid
        item.root_id        0 = 那条是根评论；非 0 = 它的根评论 rpid
        item.target_id      楼中楼里被回复的 rpid，根评论时为 0
        item.title          视频标题
        item.source_content 评论正文

    `chat_id` 编成 `oid:root:parent`，对齐 `comment.send_comment` 的语义：
        root=要回复的那条评论，parent=楼中楼里被回复的那条（根评论时为 0）

    老写法按 `oid`/`target_reply_id` 猜字段，真机全是 None，会在错误的
    位置发评论，所以一律以 subject_id/source_id 为准。
    """
    if not isinstance(item, dict):
        return None
    inner = item.get("item")
    if not isinstance(inner, dict):
        return None
    user = item.get("user")
    if not isinstance(user, dict):
        user = {}

    oid = _as_int(inner.get("subject_id") or inner.get("oid") or inner.get("business_id"))
    own_rpid = _as_int(inner.get("source_id") or inner.get("target_id"))
    root_id = _as_int(inner.get("root_id"))
    if not oid or not own_rpid:
        return None

    # 根评论 → 直接回它（root=它自己）；楼中楼 → root 是根，parent 是它自己
    if root_id:
        root, parent = root_id, own_rpid
    else:
        root, parent = own_rpid, 0

    sender = _as_int(user.get("mid"))
    if not sender:
        return None

    title = str(inner.get("title") or "").strip()
    body = str(inner.get("source_content") or inner.get("target_reply_content") or "").strip()
    text = body or title
    if not text:
        return None
    if title and body and title != body:
        text = f"{body}\n（在《{title}》下）"

    return InboundMessage(
        platform=Platform.BILIBILI,
        kind=ChatKind.COMMENT,
        chat_id=f"{oid}:{root}:{parent}",
        sender_id=str(sender),
        sender_name=str(user.get("nickname") or ""),
        text=text,
        # 通知自身的 id 有时缺省；用 vid + 评论 rpid + 来源拼出稳定键，
        # 缺 id 时也不会退化成空串把两条不同通知合成一条。
        msg_id=f"{source}:{oid}:{_as_int(item.get('id')) or own_rpid}",
        is_mention_me=True,
        ts=float(_as_int(item.get("at_time") or item.get("reply_time") or item.get("timestamp")) or time.time()),
        ts_source="platform",
        sender_id_kind="stable",     # 评论/通知里都带 sender uid
        raw={
            "source": source,
            "oid": oid,
            "root": root,
            "parent": parent,
            "uri": str(inner.get("uri") or ""),
        },
    )


# ---------------------------------------------------------------- 私信监听


class SessionListener:
    """私信监听。包装 `bilibili_api.session.Session`，断线靠外层重连。"""

    def __init__(self, credential: Any, on_message: InboundCallback) -> None:
        self._credential = credential
        self._on_message = on_message
        self._running = False
        self._session: Any = None

    def _handle(self, event: Any) -> None:
        msg = inbound_from_event(event)
        if msg is not None:
            self._on_message(msg)

    async def _connect_once(self) -> None:
        from bilibili_api import session as bsession

        sess = bsession.Session(self._credential)
        for event_type in (
            bsession.EventType.TEXT,
            bsession.EventType.SHARE_VIDEO,
            bsession.EventType.PUSHED_VIDEO,
            bsession.EventType.PICTURE,
        ):
            # Session.on() 要的是 EventType 枚举，它内部才去做 str(value)；
            # 这里先转字符串会炸成 'str' object has no attribute 'value'。
            sess.on(event_type)(self._handle)
        self._session = sess
        log.info("B站私信监听启动（库内部 6 秒一轮）")
        try:
            await sess.start()      # 阻塞到 close()
        finally:
            self._session = None

    async def run(self) -> None:
        self._running = True
        backoff = 5.0
        while self._running:
            try:
                await self._connect_once()
                if not self._running:
                    break
                log.warning("B站私信监听结束，准备重连")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络/风控异常类型不稳定
                log.warning("B站私信监听异常：%s", exc)
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, CRED_RETRY_MAX)

    def stop(self) -> None:
        self._running = False
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - 关的时候出错没意义
                log.debug("关闭 Session 失败", exc_info=True)


# ---------------------------------------------------------------- 通知轮询


class NotifyPoller:
    """@我的 + 收到的回复。B站没有推送，只能定时拉，只能按 id 去重。

    去重真相在 ``mailbox.db``：同一条通知重启后再拉一遍，落库时会被
    ``ux_message_platform_id`` 拦住，不会重复回复。这里的集合只是省一次落库尝试，
    不再往 JSON 文件里写一份影子状态。
    """

    def __init__(
        self,
        credential: Any,
        on_message: InboundCallback,
        *,
        interval: float = NOTIFY_INTERVAL,
    ) -> None:
        self._credential = credential
        self._on_message = on_message
        self._interval = interval
        self._running = False
        self._seen: set[str] = set()

    def _emit(self, items: list, *, source: str) -> None:
        for item in items:
            msg = inbound_from_notice(item, source=source)
            if msg is None:
                continue
            # 只有真拿到平台 id 才在本地拦；没有 id 的交给消息库按序号去重，
            # 否则空 id 会把所有无 id 通知互相吞掉。
            if msg.msg_id:
                if msg.msg_id in self._seen:
                    continue
                self._seen.add(msg.msg_id)
                if len(self._seen) > _SEEN_KEEP:
                    self._seen = set(list(self._seen)[-_SEEN_KEEP:])
            self._on_message(msg)

    async def _poll_once(self) -> None:
        from bilibili_api import session as bsession

        at_data = await bsession.get_at(self._credential)
        self._emit(list((at_data or {}).get("items") or []), source="at")

        reply_data = await bsession.get_replies(self._credential)
        self._emit(list((reply_data or {}).get("items") or []), source="reply")

    async def run(self) -> None:
        self._running = True
        log.info("B站 @/评论通知轮询启动，间隔 %.0f 秒", self._interval)
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("B站通知轮询失败：%s", exc)
            if not self._running:
                break
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------- 发送


async def _lib_send_private(credential: Any, receiver_id: int, text: str,
                            image: Any = None) -> dict:
    """发私信。抽成模块级函数，测试可以直接换掉它，不联网。

    ``image`` 是 ``bilibili_api`` 的 Picture。B 站私信一次只带一种类型，
    有图就按 PICTURE 发，文本让位 —— 不能既发图又发字。
    """
    from bilibili_api import session as bsession

    if image is not None:
        return await bsession.send_msg(
            credential, receiver_id, bsession.EventType.PICTURE, image
        )
    return await bsession.send_msg(
        credential, receiver_id, bsession.EventType.TEXT, text
    )


async def _lib_send_comment(
    credential: Any, oid: int, text: str, root: int = 0, parent: int = 0
) -> dict:
    """发评论回复。抽成模块级函数，测试可以直接换掉它，不联网。"""
    from bilibili_api import comment as bcomment

    return await bcomment.send_comment(
        text=text,
        oid=oid,
        type_=bcomment.CommentResourceType.VIDEO,
        root=root or None,
        parent=parent or None,
        credential=credential,
    )


def parse_comment_chat_id(chat_id: str) -> tuple[int, int, int] | None:
    """把 `oid:root:parent` 拆开；只有 oid 也认。拆不出来返回 None。"""
    parts = str(chat_id).split(":")
    try:
        oid = int(parts[0])
    except (IndexError, ValueError):
        return None
    if not oid:
        return None
    root = _as_int(parts[1]) if len(parts) > 1 else 0
    parent = _as_int(parts[2]) if len(parts) > 2 else 0
    return oid, root, parent


class BiliPoster:
    """发私信 / 发评论回复。"""

    def __init__(self, credential: Any = None) -> None:
        self._credential = credential

    @property
    def can_send(self) -> bool:
        return self._credential is not None

    async def send_private(self, uid: str, text: str, image: str = "") -> SendResult:
        if self._credential is None:
            return SendResult(ok=False, error="没有 B站登录凭据，发不了私信")
        try:
            receiver = int(uid)
        except (TypeError, ValueError):
            return SendResult(ok=False, error=f"私信对象不是合法 uid：{uid!r}")
        picture = None
        if image:
            try:
                from bilibili_api.utils.picture import Picture
                picture = Picture.from_file(str(image))
            except Exception as exc:  # noqa: BLE001 - 读不了图就用文字发，别整条丢掉
                picture = None
                if not text.strip():
                    return SendResult(ok=False, error=f"表情图读不了：{type(exc).__name__}")
        try:
            data = await _lib_send_private(self._credential, receiver, text, picture)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, error=f"发私信失败：{exc}")
        if isinstance(data, dict) and data.get("code") not in (None, 0):
            return SendResult(ok=False, error=f"code={data.get('code')} {data.get('message')}")
        return SendResult(ok=True)

    async def send_comment(self, chat_id: str, text: str) -> SendResult:
        if self._credential is None:
            return SendResult(ok=False, error="没有 B站登录凭据，发不了评论")
        parsed = parse_comment_chat_id(chat_id)
        if parsed is None:
            return SendResult(ok=False, error=f"chat_id 解析失败：{chat_id!r}（要 oid:root:parent）")
        oid, root, parent = parsed
        try:
            data = await _lib_send_comment(self._credential, oid, text, root, parent)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, error=f"发评论失败：{exc}")
        if isinstance(data, dict) and data.get("code") not in (None, 0):
            return SendResult(ok=False, error=f"code={data.get('code')} {data.get('message')}")
        rpid = ""
        if isinstance(data, dict):
            rpid = str(((data.get("data") or {}).get("rpid_str")) or "")
        return SendResult(ok=True, msg_id=rpid)


# ---------------------------------------------------------------- 适配器


class BilibiliAdapter(Adapter):
    platform = Platform.BILIBILI

    def __init__(
        self,
        options: dict | None = None,
        *,
        poster: BiliPoster | None = None,
        credential: Any = None,
    ) -> None:
        super().__init__()
        opts = options or {}
        self._credential = credential
        self._poster = poster or BiliPoster(credential)
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._listener: SessionListener | None = None
        self._poller: NotifyPoller | None = None
        self._watch_private = bool(opts.get("watch_private", True))
        self._watch_notice = bool(opts.get("watch_comment", True))
        self._notify_interval = float(opts.get("notify_interval", NOTIFY_INTERVAL))
        self._options = opts

    def attach_vault(self, vault) -> None:
        super().attach_vault(vault)
        if not missing_credential_env(vault):
            self._credential = build_credential(vault)
        else:
            self._credential = None
        self._poster = BiliPoster(self._credential)

    def _enqueue(self, msg: InboundMessage) -> None:
        log.info(
            "B站收到%s消息 from %s：%s",
            "私信" if msg.kind is ChatKind.PRIVATE else "评论",
            msg.sender_name or msg.sender_id,
            msg.text[:60].replace("\n", " "),
        )
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, msg)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        if self._credential is None:
            missing = missing_credential_env(self.vault)
            log.error(
                "B站适配器没拿到凭据，收不到也发不出。缺环境变量：%s",
                ", ".join(missing) or "(未配置)",
            )
            return

        tasks: list[asyncio.Task] = []
        if self._watch_private:
            self._listener = SessionListener(self._credential, self._enqueue)
            tasks.append(asyncio.create_task(self._listener.run(), name="bili-private"))
        if self._watch_notice:
            self._poller = NotifyPoller(
                self._credential, self._enqueue, interval=self._notify_interval
            )
            tasks.append(asyncio.create_task(self._poller.run(), name="bili-notice"))

        if not tasks:
            log.warning("B站适配器私信和通知都关掉了，什么都不会收")
            return

        try:
            while not self._stopping:
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if all(t.done() for t in tasks):
                        for t in tasks:
                            if t.exception() is not None:
                                raise t.exception()  # type: ignore[misc]
                        return
                    continue
                await self.emit(msg)
        finally:
            for task in tasks:
                task.cancel()

    async def stop(self) -> None:
        self._stopping = True
        if self._listener is not None:
            self._listener.stop()
        if self._poller is not None:
            self._poller.stop()
        self._stopping = False

    def capabilities(self) -> dict[str, Any]:
        return {
            **super().capabilities(),
            "text_receive": "supported",
            "text_send": "supported",
            "image_receive": "supported",      # 入站图片带 CDN url，brain 会下载进 workspace/inbox 给模型看
            "image_send": "supported",         # 2026-10-27 真机验证：私信按 PICTURE 发，回了 msg_key
            "max_image_bytes": None,
            "needs_upload": False,             # 走 bilibili_api 的 Picture，不需要自己传 CDN
            "comment_send": "supported",
            "proactive_private": "supported",
        }

    async def send(self, msg: OutboundMessage) -> SendResult:
        if msg.kind is ChatKind.COMMENT:
            return await self._poster.send_comment(msg.chat_id, msg.text)
        image = next((a.path for a in msg.attachments
                      if a.kind == "image" and a.path), "")
        return await self._poster.send_private(msg.chat_id, msg.text, image)


def build(options: dict) -> BilibiliAdapter:
    """网关按配置实例化时走这里。凭据不齐只打印缺什么，不抛异常。"""
    # B站域名在这台机器上系统 DNS 会拒解析，先把 DoH 兜底装上
    from .. import doh

    doh.install()

    missing = missing_credential_env()
    if missing:
        log.error("B站凭据不全，缺：%s", ", ".join(missing))
        return BilibiliAdapter(options, credential=None)
    return BilibiliAdapter(options, credential=build_credential())
