"""平台适配器构建表。

单独成模块，避免 ``channels → main → server → api → channels`` 的循环导入：
入口（main.py）和账号级通道管理（channels.py）都从这里拿同一个构建逻辑。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from . import config as cfg_mod
from .models import Platform

log = logging.getLogger("gateway")

# adapter 模块名 → 平台枚举。没安装依赖或没启用的平台会被跳过。
PLATFORM_MODULES: dict[str, Platform] = {
    "qq": Platform.QQ,
    "wechat": Platform.WECHAT,
    "bilibili": Platform.BILIBILI,
    "douyin": Platform.DOUYIN,
}


def auto_receive_enabled(cfg: cfg_mod.GatewayConfig) -> bool:
    """首启向导确认之后才启用自动收发（FR-6.5）；没确认就只起控制面。"""
    return bool(cfg.ui.onboarding_confirmed)


def build_platform_adapter(name: str, options: dict[str, Any] | None) -> Any | None:
    """构建单个平台适配器；缺依赖只警告，返回 None，不拖垮整个网关。"""
    try:
        module = importlib.import_module(f".adapters.{name}", __package__)
    except ImportError as exc:
        log.warning("平台 %s 的依赖没装齐，跳过：%s", name, exc)
        return None
    try:
        return module.build(dict(options or {}))  # type: ignore[attr-defined]
    except Exception:
        log.exception("平台 %s 初始化失败，跳过", name)
        return None


def build_adapters(cfg: cfg_mod.GatewayConfig) -> dict[Platform, object]:
    """按配置实例化 adapter。"""
    built: dict[Platform, object] = {}
    for name, platform in PLATFORM_MODULES.items():
        pc = cfg.platforms.get(name)
        if pc is None or not pc.enabled:
            continue
        adapter = build_platform_adapter(name, pc.options)
        if adapter is not None:
            built[platform] = adapter
    return built
