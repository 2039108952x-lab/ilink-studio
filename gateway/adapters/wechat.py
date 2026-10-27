"""微信适配器：wxauto（Windows 客户端 UI 自动化）—— **过时的备胎，默认不用**。

现在该走的官方通道在 `wechat_ilink.py`（iLink / ClawBot，腾讯自己的 API，
不注入客户端、不封号）。`build()` 默认就分派到那边，本文件只在
`mode = "wxauto"` 时才会被用到。

⚠️ 这条路在本机跑不通（已实测）
    微信 4.x 的进程是 `Weixin.exe`，窗口类名 `Qt51514QWindowIcon`；
    而 wxauto 3.9.x 里写死了 `FindWindow(classname='WeChatMainWndForPC')`，
    那是微信 3.x 的 MFC 类名，4.x 上永远找不到窗口。wxauto 官方 README
    也明写只支持「微信 3.9.X」。要跑就得把客户端降级，仍然违反个人账号规范。

⚠️ 风险声明
    个人微信自动化违反《微信个人账号使用规范》，存在封号可能，且可能连带主号。
    官方合规路径 = `[platforms.wechat] mode = "official"`（iLink）。

⚠️ API 差异（照实写，别按想象写）
    实测 3.9.11.17 的 WeChat 类只有：
        AddListenChat(who, savepic?, savefile?, savevoice?)   ← 没有 callback 参数
        GetListenMessage(who=None)  -> {ChatWnd: [Message, ...]}
        SendMsg(msg, who=None, clear=True, at=None)
        RemoveListenChat(who)
    没有 KeepRunning()，所以主循环由我们自己的轮询 + sleep 承担。

设计
    wxauto 是同步阻塞且操作真实窗口的，全程跑在独立线程里；
    adapter 侧只把回调塞进 asyncio 队列再往上 emit。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from ..models import ChatKind, InboundMessage, OutboundMessage, Platform, SendResult
from .base import Adapter

log = logging.getLogger(__name__)

# 从 wxauto 线程回调进来：(chat_id, 文本)
MessageCallback = Callable[[str, str], None]

MEDIA_PLACEHOLDERS = {
    "image": "[图片]",
    "video": "[视频]",
    "voice": "[语音]",
    "file": "[文件]",
}


class WeChatDriver(Protocol):
    """把 wxauto 的全部脏活关在这里，换实现不影响 adapter。"""

    def start(self, on_message: MessageCallback) -> None:
        """阻塞轮询，直到 stop() 被调用。"""

    def send(self, chat_id: str, text: str) -> SendResult: ...

    def stop(self) -> None: ...


class WxAutoDriver:
    """wxauto 的真实现。没装 wxauto 就别实例化。"""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        opts = options or {}
        self._listen: list[str] = list(opts.get("listen", []))
        self._poll = float(opts.get("poll_interval", 2.0))
        self._wx: Any = None
        self._running = False

    @staticmethod
    def available() -> bool:
        try:
            import wxauto  # noqa: F401
        except ImportError:
            return False
        return True

    # ---- 运行 ----

    def start(self, on_message: MessageCallback) -> None:
        try:
            from wxauto import WeChat
        except ImportError as exc:  # pragma: no cover - 真机才有
            raise ImportError(
                "没装 wxauto。它不在 PyPI 上，得从 wxauto.org 拿。\n"
                "但注意：wxauto 只认微信 3.9.x，现在的微信 4.x 跑不了 —— "
                '改走 mode = "official"（官方 iLink 通道）才是正路。'
            ) from exc

        try:
            self._wx = WeChat()
        except Exception as exc:  # noqa: BLE001 - 微信没开、版本不对都会抛
            raise RuntimeError(
                "wxauto 连不上微信窗口。最可能是客户端版本不对 —— wxauto 只认微信 3.9.x，"
                '本机是 4.x（Weixin.exe），找不到 WeChatMainWndForPC。'
                '改用 mode = "official" 走官方 iLink 通道。'
                f"（{exc}）"
            ) from exc
        self._running = True

        if not self._listen:
            log.warning("没有配置 listen，微信收不到任何消息")
        for who in self._listen:
            # 实测签名是 AddListenChat(who, ...)，没有 callback
            self._wx.AddListenChat(who=who)
        if self._listen:
            log.info("微信已监听 %d 个会话：%s", len(self._listen), self._listen)

        try:
            while self._running:
                try:
                    self._drain(on_message)
                except Exception as exc:  # noqa: BLE001 - UI 自动化什么都可能抛
                    log.warning("微信轮询出错（继续）：%s", exc)
                time.sleep(self._poll)
        finally:
            self._running = False

    def _drain(self, on_message: MessageCallback) -> None:
        if self._wx is None:
            return
        received = self._wx.GetListenMessage() or {}
        if not isinstance(received, dict):
            return
        for chat, messages in received.items():
            who = getattr(chat, "who", None) or str(chat)
            for message in messages or []:
                text = self._render(message)
                if text:
                    on_message(who, text)

    @staticmethod
    def _render(message: Any) -> str:
        """微信消息对象 → 一行文本，媒体用占位符。"""
        content = (getattr(message, "content", "") or "").strip()
        mtype = (getattr(message, "type", "text") or "text").lower()
        if mtype in MEDIA_PLACEHOLDERS:
            placeholder = MEDIA_PLACEHOLDERS[mtype]
            return f"{placeholder} {content}".strip() if content else placeholder
        return content

    # ---- 发送 ----

    def send(self, chat_id: str, text: str) -> SendResult:
        if self._wx is None:
            return SendResult(ok=False, error="微信还没初始化")
        try:
            self._wx.SendMsg(text, who=chat_id)
        except Exception as exc:  # noqa: BLE001 - UI 自动化什么错都可能抛
            return SendResult(ok=False, error=str(exc))
        return SendResult(ok=True)

    def stop(self) -> None:
        self._running = False
        if self._wx is None:
            return
        for who in self._listen:
            try:
                self._wx.RemoveListenChat(who=who)
            except Exception:  # noqa: BLE001
                log.debug("移除监听失败：%s", who, exc_info=True)


class WeChatAdapter(Adapter):
    platform = Platform.WECHAT

    def __init__(self, options: dict | None = None, driver: WeChatDriver | None = None) -> None:
        super().__init__()
        opts = options or {}
        self._driver: WeChatDriver = driver or WxAutoDriver(opts)
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    # ---- 线程 → 事件循环 ----

    def _push(self, chat_id: str, text: str) -> None:
        """由 wxauto 线程调用，必须线程安全。"""
        if self._loop is None:
            return
        msg = InboundMessage(
            platform=Platform.WECHAT,
            kind=ChatKind.PRIVATE if not chat_id.endswith("@chatroom") else ChatKind.GROUP,
            chat_id=chat_id,
            sender_id=chat_id,      # 用昵称兜底，微信拿不到稳定 uid
            sender_name=chat_id,
            text=text,
            # 昵称不是稳定标识：这条通道的消息一律按「平台内稳定句柄」处理，
            # 只有本人在白名单里逐条列出才会自动回复（PRD §7.2）。
            sender_id_kind="opaque",
            ts=time.time(),
            ts_source="received",
        )
        self._loop.call_soon_threadsafe(self._queue.put_nowait, msg)

    # ---- 生命周期 ----

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        runner = self._loop.run_in_executor(None, self._driver.start, self._push)
        try:
            while not self._stopping:
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if runner.done():
                        exc = runner.exception()
                        if exc is not None:
                            raise exc
                        log.warning("微信 driver 已退出")
                        return
                    continue
                await self.emit(msg)
        finally:
            self._stopping = True
            try:
                self._driver.stop()
            except Exception:  # noqa: BLE001
                log.debug("停止微信 driver 失败", exc_info=True)

    async def stop(self) -> None:
        self._stopping = True
        self._driver.stop()

    def capabilities(self) -> dict[str, Any]:
        # 本机微信是 4.x，wxauto 只认 3.9.x：这条通道什么都不声明为支持。
        capabilities = super().capabilities()
        capabilities.update({
            "text_receive": "unsupported",
            "text_send": "unsupported",
            "image_receive": "unsupported",
            "image_send": "unsupported",
            "comment_send": "unsupported",
            "proactive_private": "unsupported",
        })
        return capabilities

    async def send(self, msg: OutboundMessage) -> SendResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._driver.send, msg.chat_id, msg.text)


def build(options: dict):
    """按 `mode` 选路。

    official（默认）= 官方 iLink 通道，见 wechat_ilink.py，那边才是现在该用的。
    wxauto          = 老的 Windows UI 自动化，只对微信 3.9.x 有效。
    """
    mode = str(options.get("mode") or "official").lower()
    if mode in ("official", "ilink", "ilinkai"):
        from . import wechat_ilink

        return wechat_ilink.build(options)
    if mode != "wxauto":
        raise ValueError(f"不认识的微信 mode={mode!r}，只能是 official 或 wxauto")
    if not WxAutoDriver.available():
        raise ImportError(
            "微信 wxauto 模式需要 wxauto，它不在 PyPI 上，得从 wxauto.org 拿：\n"
            "  https://docs.wxauto.org\n"
            "但更简单的做法是改用官方通道：mode = \"official\" + scripts/wx_login.py"
        )
    log.warning(
        "微信走的是 wxauto（UI 自动化），只认客户端 3.9.x。微信 4.x 上必然找不到窗口，"
        "那种情况请改用 mode = \"official\"（官方 iLink 通道，不封号）"
    )
    return WeChatAdapter(options)
