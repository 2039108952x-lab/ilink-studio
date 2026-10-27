"""适配器基类。

每个平台只做三件事：怎么收、怎么发、怎么起停。其他一律交给 router/brain。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from ..checkpoint import Checkpoint
from ..models import InboundMessage, OutboundMessage, Platform, SendResult

# 收到消息后往哪儿推
InboundHandler = Callable[[InboundMessage], Awaitable[None]]
# 检查点工厂：按 stream 名拿到属于本账号的一个检查点
CheckpointFactory = Callable[[str], Checkpoint]


class Adapter(ABC):
    """一个平台的连接。"""

    platform: Platform

    def __init__(self) -> None:
        self._on_inbound: InboundHandler | None = None
        self._checkpoints: CheckpointFactory | None = None
        self.vault = None            # 需要保存凭据的适配器由上层注入 Vault
        self.on_needs_human = None   # router 注入：遇到验证码/滑块时上报

    def mark_needs_human(self, reason: str = "captcha") -> None:
        """需要本人过验证：上报状态，不绕过、不硬闯。"""
        if callable(self.on_needs_human):
            try:
                self.on_needs_human(reason)
            except Exception:  # noqa: BLE001 - 上报失败不能拖垮轮询
                pass

    def bind(self, handler: InboundHandler, *,
             checkpoints: CheckpointFactory | None = None) -> None:
        """由 router 注入入站回调和检查点工厂。"""
        self._on_inbound = handler
        if checkpoints is not None:
            self._checkpoints = checkpoints

    def attach_vault(self, vault) -> None:
        """凭据必须进保险库；没注入时适配器只保留内存态，不写明文文件。"""
        self.vault = vault

    def checkpoint(self, stream: str = "inbound") -> Checkpoint | None:
        if self._checkpoints is None:
            return None
        return self._checkpoints(stream)

    # ---- 能力矩阵（FR-4.3） ----

    def capabilities(self) -> dict[str, Any]:
        """默认全 unknown：不知道不等于支持，上层一律按「不能发」处理。

        ``image_send`` 只有真机验证过才允许写 supported；``unknown``/``degraded``
        都会走文字降级，不会侥幸发一张图出去。
        """
        return {
            "text_receive": "unknown",
            "text_send": "unknown",
            "image_receive": "unknown",
            "image_send": "unknown",
            "max_image_bytes": None,
            "needs_upload": None,
            "comment_send": "unknown",
            "proactive_private": "unknown",
        }

    async def emit(self, msg: InboundMessage) -> None:
        """子类收到消息后调这个交给上层。"""
        if self._on_inbound is not None:
            await self._on_inbound(msg)

    @abstractmethod
    async def start(self) -> None:
        """建立连接并开始接收，阻塞到 stop()。"""

    @abstractmethod
    async def stop(self) -> None:
        """断开并清理。"""

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> SendResult:
        """发一条消息出去。"""
