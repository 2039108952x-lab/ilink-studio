"""微信适配器（官方 iLink / ClawBot 通道）。

这是微信**官方**开放的个人 Bot API：腾讯自家域名 `ilinkai.weixin.qq.com`，
纯 HTTP/JSON，配《微信ClawBot功能使用条款》。跟 wxauto 那种 UI 自动化不是一回事
——没有注入、不碰客户端进程，正常使用不涉及封号。

为什么不用 wxauto 了
    本机微信已经是 4.x（`Weixin.exe`，窗口类名 `Qt51514QWindowIcon`），而
    wxauto 3.9.x 硬编码 `FindWindow(classname='WeChatMainWndForPC')`（微信 3.x 的
    MFC 窗口类名），永远找不到窗口。官方 README 也明写只支持「微信 3.9.X」。
    想跑通就得把客户端降级到 3.9，那条路仍然违反个人账号规范。所以默认走这条。

协议要点（照 `@tencent-weixin/openclaw-weixin@2.4.10` 的真实实现写，不是猜的）
    鉴权    扫码换 `bot_token`（见 scripts/wx_login.py），之后 Bearer 鉴权
    收      `POST /ilink/bot/getupdates`，服务端 hold 35 秒，返回游标 `get_updates_buf`
    发      `POST /ilink/bot/sendmessage`，**必须带上原消息的 `context_token`**
    头      `AuthorizationType: ilink_bot_token`、`iLink-App-Id`、
            `X-WECHAT-UIN`（随机 uint32 → 十进制字符串 → base64，每次请求都变）
    消息    `message_type` 1=用户 2=bot；`item_list[].type` 1文本 2图片 3语音 4文件 5视频

注意两处容易踩空的地方
    1. `context_token` 是跟「对话窗口」绑定的。缓存丢了就发不进去——而它只从
       收到的消息里来，所以**对方没说过话之前主动去消息可能失败**，这是协议限制。
    2. `get_updates_buf` 必须落盘。不落盘的话每次重启都从零开始，会把历史消息
       当成新消息重放一遍（跟抖音那边的「首轮不回放」是同一个坑）。

已知限制
    - 图片/语音/文件/视频**只收不发**，且只以占位符交给模型（解密要 AES-128-ECB
      + CDN 上传，还没做）。别假装看得到。
    - 群聊：协议上有 `group_id`，但官方实现回复时固定发给 `from_user_id`，
      也就是按一对一处理。这里照抄官方行为，群里发来的消息当私聊回。
    - 没有拉历史消息的接口，只有游标增量。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ..models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult
from ..vault import CredentialError
from .base import Adapter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 协议常量

BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"                          # = 官方包 package.json 里的 ilink_appid
CHANNEL_VERSION = "2.4.10"              # 对齐官方包版本；服务端看这个字段做灰度
BOT_AGENT = "ReasonixGateway/1.0"

LONG_POLL_TIMEOUT = 40.0                # 服务端 hold 35s，客户端留点余量
API_TIMEOUT = 20.0
RETRY_DELAY = 2.0
BACKOFF_DELAY = 30.0
MAX_CONSECUTIVE_FAILURES = 5

# 会话过期。出现就得重新扫码，重启进程没用。
ERRCODE_SESSION_EXPIRED = -14

MSG_FROM_USER = 1
MSG_FROM_BOT = 2
STATE_FINISH = 2

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
# 登录助手（scripts/wx_login.py）写它。data/ 在 gitignore 里，凭据不会进版本库。
TOKEN_FILE = DATA_DIR / "wx-bot.json"
# 游标 + context_token 缓存。丢了不会致命，但会把历史重放一遍。
STATE_FILE = DATA_DIR / "wx-ilink-state.json"


def _app_client_version(version: str = CHANNEL_VERSION) -> int:
    """官方包把 x.y.z 压成一个整数：major<<16 | minor<<8 | patch。"""
    parts = (version.split(".") + ["0", "0", "0"])[:3]
    nums = []
    for part in parts:
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    major, minor, patch = nums
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _wechat_uin() -> str:
    """`X-WECHAT-UIN`：随机 uint32 → 十进制字符串 → base64。每次请求都重新生成。"""
    return base64.b64encode(str(secrets.randbelow(2**32)).encode()).decode()


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _wechat_uin(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(_app_client_version()),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


# ---------------------------------------------------------------- 凭据与状态


def load_token() -> str:
    """环境变量优先，登录助手写的文件兜底。跟 B站适配器一个套路。"""
    import os

    token = os.environ.get("WEIXIN_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        if TOKEN_FILE.is_file():
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return str(data.get("bot_token", "")).strip()
    except (OSError, ValueError):
        log.warning("读 %s 失败，当成没登录", TOKEN_FILE, exc_info=True)
    return ""


def load_state() -> dict[str, Any]:
    try:
        if STATE_FILE.is_file():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        log.warning("读 %s 失败，游标从零开始", STATE_FILE, exc_info=True)
    return {}


def save_state(state: dict[str, Any]) -> None:
    """写回游标和 context_token 缓存。写不进去只警告，不影响收发。"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        log.warning("写 %s 失败", STATE_FILE, exc_info=True)


# ---------------------------------------------------------------- HTTP 客户端


class IlinkApi:
    """iLink 的四个接口，只管收发字节，不管业务。"""

    def __init__(self, token: str = "", base_url: str = BASE_URL) -> None:
        self.token = token
        self.base_url = (base_url or BASE_URL).rstrip("/")

    def _url(self, endpoint: str) -> str:
        return f"{self.base_url}/{endpoint}"

    async def get_updates(self, buf: str) -> dict[str, Any]:
        """长轮询收消息。服务端没有新消息时会 hold 到超时，返回 msgs 为空。"""
        payload = {"get_updates_buf": buf, "base_info": _base_info()}
        try:
            async with httpx.AsyncClient(timeout=LONG_POLL_TIMEOUT + 10) as client:
                resp = await client.post(
                    self._url("ilink/bot/getupdates"),
                    json=payload,
                    headers=_headers(self.token),
                    timeout=LONG_POLL_TIMEOUT,
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException:
            # 长轮询超时是正常控制流，不是错误：返回空，让上层接着轮。
            return {"ret": 0, "msgs": [], "get_updates_buf": buf}

    async def send_text(
        self, to_user_id: str, text: str, context_token: str = ""
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "to_user_id": to_user_id,
            "client_id": f"ilink-gateway-{uuid.uuid4().hex}",
            "message_type": MSG_FROM_BOT,
            "message_state": STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            msg["context_token"] = context_token
        payload = {"msg": msg, "base_info": _base_info()}
        async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
            resp = await client.post(
                self._url("ilink/bot/sendmessage"),
                json=payload,
                headers=_headers(self.token),
            )
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------- 消息转换


def _render_items(items: list[dict[str, Any]] | None) -> str:
    """item_list → 一行文本。媒体一律占位符：我们没做解密，就不假装看见了。"""
    if not items:
        return ""
    parts: list[str] = []
    for item in items:
        kind = item.get("type")
        if kind == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text", "")
            if text:
                parts.append(text)
        elif kind == ITEM_IMAGE:
            parts.append("[图片]")
        elif kind == ITEM_VOICE:
            # 微信会顺带给转写文字，有就用，省一次语音识别
            spoken = (item.get("voice_item") or {}).get("text", "")
            parts.append(f"[语音] {spoken}".strip() if spoken else "[语音]")
        elif kind == ITEM_FILE:
            name = (item.get("file_item") or {}).get("file_name", "")
            parts.append(f"[文件] {name}".strip())
        elif kind == ITEM_VIDEO:
            parts.append("[视频]")
        # 11/12 是 tool_call 相关的类型，人是不会发的，忽略
    return "\n".join(parts).strip()


def to_inbound(raw: dict[str, Any]) -> InboundMessage | None:
    """一条 WeixinMessage → 统一入站消息。自己发的、空消息返回 None。"""
    if raw.get("message_type") != MSG_FROM_USER:
        return None                  # 2 = bot 自己发的，不能回灌给模型，否则自问自答
    sender = str(raw.get("from_user_id") or "")
    if not sender:
        return None
    text = _render_items(raw.get("item_list"))
    if not text:
        return None                  # 纯表情/无内容，没东西可答

    if raw.get("group_id"):
        # 官方实现回复时也是发给 from_user_id，这里照同一个口径当私聊处理
        log.debug("收到带 group_id 的消息（%s），按一对一处理", raw.get("group_id"))

    # 官方通道给的是 wxid，属于平台不可变 ID。
    try:
        created = float(raw.get("create_time") or 0)
    except (TypeError, ValueError):
        created = 0.0
    if created > 1e11:            # 毫秒时间戳
        created /= 1000.0
    return InboundMessage(
        platform=Platform.WECHAT,
        kind=ChatKind.PRIVATE,
        chat_id=sender,
        sender_id=sender,
        sender_name=sender,
        text=text,
        msg_id=str(raw.get("message_id") or raw.get("client_id") or ""),
        raw=raw,
        ts=created or time.time(),
        ts_source="platform" if created else "received",
        sender_id_kind="stable",
    )


# ---------------------------------------------------------------- 适配器


class IlinkWeChatAdapter(Adapter):
    platform = Platform.WECHAT

    def __init__(self, options: dict[str, Any] | None = None, api: IlinkApi | None = None) -> None:
        super().__init__()
        opts = options or {}
        self._skip_backlog = bool(opts.get("skip_backlog", True))
        self._api = api or IlinkApi(
            token=load_token(), base_url=str(opts.get("api_base") or BASE_URL)
        )
        # 游标在 adapter_checkpoint，context_token 在 Vault；
        # 旧的 data/wx-ilink-state.json 只在启动时做一次性迁移（见 _ensure_state）。
        self._api_base = str(opts.get("api_base") or BASE_URL)
        self._buf: str = ""
        self._contexts: dict[str, str] = {}
        self._state_loaded = False
        self._stopping = False
        self._save_at = 0.0

    ACCOUNT_KEY = "wechat:official"

    def attach_vault(self, vault) -> None:
        super().attach_vault(vault)
        try:
            token = vault.get(self.ACCOUNT_KEY, "bot_token")
            base_url = vault.get(self.ACCOUNT_KEY, "base_url")
        except Exception:  # 无凭据或后端失败都按未登录，不回退到旧明文文件。
            token, base_url = "", ""
        if token:
            self._api.token = token
        else:
            self._api.token = ""
        if base_url:
            self._api.base_url = base_url.rstrip("/")
        elif not base_url:
            self._api.base_url = self._api_base

    # ---- 状态 ----

    def _ensure_state(self) -> None:
        """读取当前数据库检查点与保险库；旧项目状态不自动迁移。"""
        if self._state_loaded:
            return
        self._state_loaded = True
        legacy = load_state()
        checkpoint = self.checkpoint("cursor")
        if checkpoint is None:
            # 没有检查点（独立跑或测试）：只保留内存态，绝不另写明文文件。
            self._buf = str(legacy.get("get_updates_buf") or "")
            for chat_id, token in (legacy.get("contexts") or {}).items():
                self._contexts[str(chat_id)] = str(token)
            return
        self._buf = checkpoint.cursor()
        migrated = False
        if not self._buf and legacy.get("get_updates_buf"):
            self._buf = str(legacy["get_updates_buf"])
            checkpoint.save(cursor=self._buf)
            migrated = True
        legacy_contexts = {
            str(chat_id): str(token) for chat_id, token in (legacy.get("contexts") or {}).items()
        }
        if legacy_contexts:
            for chat_id, token in legacy_contexts.items():
                self._remember_context_secret(chat_id, token)
            migrated = True
        if migrated:
            self._retire_legacy_state(has_contexts=bool(legacy_contexts))

    def _retire_legacy_state(self, *, has_contexts: bool) -> None:
        if has_contexts and self.vault is None:
            log.warning("没有保险库可存 context_token，%s 先留着不清理", STATE_FILE)
            return
        try:
            STATE_FILE.replace(STATE_FILE.with_suffix(".migrated.json"))
        except OSError:
            log.debug("旧状态文件清理失败", exc_info=True)

    def _remember_context_secret(self, chat_id: str, token: str) -> None:
        self._contexts[chat_id] = token
        if self.vault is not None:
            try:
                self.vault.put(self.ACCOUNT_KEY, f"context:{chat_id}", token)
            except Exception:  # noqa: BLE001 - 保险库故障不能打断收消息
                log.warning("context_token 存保险库失败，本次只保留内存态", exc_info=True)

    def _persist(self) -> None:
        self._save_at = time.monotonic()
        checkpoint = self.checkpoint("cursor")
        if checkpoint is not None:
            checkpoint.save(cursor=self._buf)

    def remember_context(self, chat_id: str, token: str) -> None:
        """发消息要带原消息的 context_token，而且只从收到的消息里拿得到。"""
        self._ensure_state()
        if token and self._contexts.get(chat_id) != token:
            self._remember_context_secret(chat_id, token)

    def context_token(self, chat_id: str) -> str:
        self._ensure_state()
        if chat_id in self._contexts:
            return self._contexts[chat_id]
        if self.vault is None:
            return ""
        try:
            token = self.vault.get(self.ACCOUNT_KEY, f"context:{chat_id}")
        except CredentialError:
            return ""
        if token:
            self._contexts[chat_id] = token
        return token

    # ---- 生命周期 ----

    async def start(self) -> None:
        self._ensure_state()
        if not self._api.token:
            log.error("微信未登录：请在账号卡扫码绑定；凭据从 DPAPI 保险库读取")
            return

        first_round = not self._buf
        failures = 0
        log.info("微信 iLink 已启动（游标%s）", "接着上次" if self._buf else "为空")

        while not self._stopping:
            try:
                resp = await self._api.get_updates(self._buf)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 网络什么错都可能抛
                failures += 1
                log.warning("getupdates 失败（%d/%d）：%s", failures, MAX_CONSECUTIVE_FAILURES, exc)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    failures = 0
                    await asyncio.sleep(BACKOFF_DELAY)
                else:
                    await asyncio.sleep(RETRY_DELAY)
                continue
            failures = 0

            errcode = resp.get("errcode")
            if errcode == ERRCODE_SESSION_EXPIRED:
                log.error(
                    "微信登录态过期（errcode -14），需要重新扫码：python scripts/wx_login.py"
                )
                return
            if resp.get("ret") not in (0, None):
                log.warning(
                    "getupdates 返回 ret=%s errcode=%s errmsg=%s",
                    resp.get("ret"),
                    errcode,
                    resp.get("errmsg", ""),
                )

            new_buf = resp.get("get_updates_buf")
            if new_buf:
                self._buf = str(new_buf)

            msgs = resp.get("msgs") or []
            if first_round and self._skip_backlog and msgs:
                # 首次启动别把登录前的历史一次性喂给模型，只登记游标
                log.info("首次启动，跳过 %d 条历史消息，从现在开始收", len(msgs))
                msgs = []

            for raw in msgs:
                if self._stopping:
                    break
                if not isinstance(raw, dict):
                    continue
                self.remember_context(
                    str(raw.get("from_user_id") or ""), str(raw.get("context_token") or "")
                )
                msg = to_inbound(raw)
                if msg is None:
                    continue
                try:
                    await self.emit(msg)
                except Exception:  # noqa: BLE001 - 单条消息出错不能打断整条流
                    log.exception("处理微信消息失败：%s", msg.msg_id)

            if first_round:
                first_round = False
            if new_buf:
                self._persist()

    async def stop(self) -> None:
        self._stopping = True
        if self._buf:
            self._persist()

    # ---- 发送 ----

    def capabilities(self) -> dict[str, Any]:
        # 图片要 AES-128-ECB + CDN 上传，未按真实协议验证前不算支持（FR-4.7）；
        # 入站图片只以 [图片] 占位符交给模型，所以接收也只能算降级。
        return {
            **super().capabilities(),
            "text_receive": "supported",
            "text_send": "supported",
            "image_receive": "degraded",
            "image_send": "unsupported",
            "max_image_bytes": None,
            "needs_upload": True,
            "comment_send": "unsupported",
            "proactive_private": "degraded",   # 要先收到对方一条消息才有 context_token
        }

    async def send(self, msg: OutboundMessage) -> SendResult:
        if not self._api.token:
            return SendResult(ok=False, error="微信未登录：桌面版在账号卡扫码；开发模式可跑 scripts/wx_login.py")
        text = msg.text or ""
        if not text:
            return SendResult(ok=False, error="空消息不发")
        context_token = self.context_token(msg.chat_id)
        if not context_token:
            # 协议上是可选的，但没它多半发不进对话窗口。试一次，失败让上层看到原因。
            log.info("会话 %s 还没有 context_token，硬发一次试试", msg.chat_id)
        try:
            resp = await self._api.send_text(msg.chat_id, text, context_token)
        except Exception as exc:  # noqa: BLE001 - 网络/HTTP 什么错都可能
            log.warning("微信发送失败：%s", exc)
            return SendResult(ok=False, error=str(exc))
        ret = resp.get("ret")
        if ret not in (0, None):
            return SendResult(ok=False, error=f"ret={ret} {resp.get('errmsg', '')}".strip())
        return SendResult(ok=True, msg_id=str(resp.get("message_id") or ""))


def build(options: dict) -> IlinkWeChatAdapter:
    return IlinkWeChatAdapter(options)
