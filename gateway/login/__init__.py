"""统一登录实现；CLI 与桌面控制台共用 provider，不另造协议流程。"""

from .manager import LoginManager, LoginSession
from .providers import ProviderSet, create_providers

__all__ = ["LoginManager", "LoginSession", "ProviderSet", "create_providers"]
