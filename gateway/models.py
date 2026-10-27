"""统一消息模型。

四个平台的入站/出站消息全部归一到这两个结构，上层路由与 brain 只认这里，
不认任何平台细节。新增平台只需要写一个 adapter，不用动这里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Platform(str, Enum):
    QQ = "qq"
    WECHAT = "wechat"
    BILIBILI = "bilibili"
    DOUYIN = "douyin"


class ChatKind(str, Enum):
    PRIVATE = "private"   # 私聊 / 私信
    GROUP = "group"       # 群聊
    ROOM = "room"         # 直播间（当前没有平台在用，留着给将来的弹幕场景）
    COMMENT = "comment"   # 评论区（B站 @/回复、抖音评论）


@dataclass(slots=True)
class Attachment:
    kind: str                       # image | video | audio | file
    url: str = ""
    path: str = ""                  # 本地路径（下载后）
    name: str = ""


@dataclass(slots=True)
class InboundMessage:
    """从平台进来的消息。

    身份来源（``sender_id_kind``）决定这条消息能不能自动回复：
    ``stable`` 平台发的不可变 ID（QQ 号、wxid、B站 uid）；
    ``opaque`` 平台内稳定但非官方 uid 的句柄；
    ``provisional`` 只有昵称，随时可能被别人改成一样 —— 一律进待核对，不自动回复。
    """

    platform: Platform
    kind: ChatKind
    chat_id: str                    # 群号 / 对方 uid / 评论区定位（oid:root:parent）
    sender_id: str
    sender_name: str
    text: str
    msg_id: str = ""
    is_mention_me: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    sender_id_kind: str = "opaque"   # stable | opaque | provisional
    ts_source: str = "received"      # platform | received
    backlog: bool = False            # 首次同步补的历史，只登记不回

    @property
    def session_key(self) -> str:
        """同一个「对话」映射到同一个 Reasonix 会话。

        只按 平台+类型+chat_id 区分，不按发言人，
        这样群里多人和我说话也共享一份上下文。
        """
        return f"{self.platform.value}:{self.kind.value}:{self.chat_id}"


@dataclass(slots=True)
class OutboundMessage:
    """要发出去的消息。"""

    platform: Platform
    kind: ChatKind
    chat_id: str
    text: str
    reply_to: str = ""              # 平台侧的消息 id，用于引用回复
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(slots=True)
class SendResult:
    ok: bool
    msg_id: str = ""
    error: str = ""


@dataclass(slots=True)
class PromptContext:
    """组装 prompt 时需要的最小上下文；ID 由网关解析，brain 不猜数据库主键。"""

    thread_id: int
    platform: str
    kind: str                       # private | group | comment
    chat_id: str
    sender_name: str = ""
    person_name: str = ""
    is_group: bool = False
    message_id: int = 0
    person_id: int = 0
