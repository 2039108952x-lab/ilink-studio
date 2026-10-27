from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .base import LoginContext, LoginError, LoginProvider, LoginResult
from .bilibili import BilibiliProvider
from .douyin import DouyinProvider
from .onebot import OneBotProvider
from .wechat import WeChatProvider

ProviderSet = dict[str, LoginProvider]


def create_providers(profile_dir_factory: Callable[[str], str],
                     onebot_ws: str = "") -> ProviderSet:
    """每个浏览器通道使用自己的 profile，绝不共用日常浏览器。

    ``onebot_ws`` 留空就让 provider 自己按 NapCat 的配置探测；
    QQ 这条路没有二维码可扫，链接的是用户自己那个 NapCat。
    """
    return {
        "wechat:official": WeChatProvider(),
        "bilibili:cookie": BilibiliProvider(profile_dir_factory("bilibili-browser")),
        "douyin:rpa": DouyinProvider(profile_dir_factory("douyin-profile")),
        "qq:onebot": OneBotProvider(onebot_ws),
    }


__all__ = [
    "LoginContext", "LoginError", "LoginProvider", "LoginResult", "ProviderSet",
    "create_providers", "WeChatProvider", "BilibiliProvider", "DouyinProvider",
    "OneBotProvider",
]
