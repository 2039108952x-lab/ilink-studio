from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol


class LoginError(RuntimeError):
    """登录错误的安全信息；不允许把完整协议报文或 token 放进去。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(slots=True)
class LoginResult:
    platform_uid: str = ""
    credentials: dict[str, str] | None = None
    requires_confirmation: bool = False


class LoginContext(Protocol):
    account_key: str
    expected_uid: str

    def cancelled(self) -> bool: ...

    def update(self, state: str, message: str, *, qr_png: bytes | None = None,
               platform_uid: str = "") -> None: ...

    def verification_code(self) -> str: ...

    def wait_confirmation(self, timeout: float = 1.0) -> bool: ...


class LoginProvider(Protocol):
    def run(self, context: LoginContext) -> LoginResult: ...
