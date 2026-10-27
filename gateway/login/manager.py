"""每个 account 至多一个可取消登录任务。

状态只描述登录任务；凭据、连接、发送健康分别由 AccountStore 保存。
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..accounts import AccountStore, IdentityMismatch
from ..vault import CredentialError, Vault
from .providers import ProviderSet
from .providers.base import LoginContext, LoginError, LoginResult

log = logging.getLogger(__name__)
TERMINAL = frozenset({"bound", "expired", "cancelled", "failed"})


@dataclass(slots=True)
class LoginSession:
    id: str
    account_id: int
    account_key: str
    expected_uid: str = ""
    state: str = "idle"
    message: str = ""
    platform_uid: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    qr_png: bytes | None = field(default=None, repr=False)
    verification_required: bool = False
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _confirm: threading.Event = field(default_factory=threading.Event, repr=False)
    _verification_code: str = field(default="", repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "account_id": self.account_id,
                "account_key": self.account_key,
                "state": self.state,
                "message": self.message,
                "platform_uid": self.platform_uid,
                "expected_uid": self.expected_uid,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "verification_required": self.verification_required,
                "qr_image": (
                    "data:image/png;base64," + base64.b64encode(self.qr_png).decode("ascii")
                    if self.qr_png else None
                ),
            }

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def update(self, state: str, message: str, *, qr_png: bytes | None = None,
               platform_uid: str = "") -> None:
        with self._lock:
            if self.state in TERMINAL:
                return
            self.state = state
            self.message = message
            self.qr_png = qr_png
            self.platform_uid = platform_uid
            self.verification_required = state == "needs_verification"
            self.updated_at = time.time()

    def set_verification_code(self, code: str) -> None:
        with self._lock:
            if self.state != "needs_verification" or not code.isdigit() or len(code) > 32:
                raise ValueError("当前任务不接受此数字验证值")
            self._verification_code = code

    def verification_code(self) -> str:
        with self._lock:
            code, self._verification_code = self._verification_code, ""
            return code

    def wait_confirmation(self, timeout: float = 1.0) -> bool:
        return self._confirm.wait(timeout)


class LoginManager:
    def __init__(self, accounts: AccountStore, vault: Vault, providers: ProviderSet,
                 *, on_reload=None) -> None:
        self.accounts = accounts
        self.vault = vault
        self.providers = providers
        self.on_reload = on_reload
        self._sessions: dict[str, LoginSession] = {}
        self._by_account: dict[int, str] = {}
        self._lock = threading.RLock()

    def start(self, account_id: int) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        key = f"{account['platform']}:{account['channel']}"
        provider = self.providers.get(key)
        if provider is None:
            if account["channel"] == "wxauto":
                raise LoginError("unsupported_channel", "本机微信为 4.x；wxauto 仅支持微信 3.9.x")
            if account["channel"] == "onebot":
                # 不再背一段固定话术：先看本机 NapCat 到底装没装、跑没跑。
                from ..napcat import describe as describe_napcat
                state = describe_napcat()
                raise LoginError("external_login_required", state["message"])
            raise LoginError("unsupported_channel", "此通道没有内嵌登录实现")
        with self._lock:
            previous_id = self._by_account.get(account_id)
            if previous_id and self._sessions[previous_id].state not in TERMINAL:
                raise LoginError("login_in_progress", "该账号已有登录任务，请先取消或等待结束")
            session = LoginSession(
                id=secrets.token_urlsafe(24), account_id=account_id, account_key=key,
                expected_uid=str(account.get("expected_uid") or ""),
            )
            self._sessions[session.id] = session
            self._by_account[account_id] = session.id
            session._thread = threading.Thread(
                target=self._run, args=(session, provider),
                name=f"login-{key}", daemon=True,
            )
            session._thread.start()
            return session.snapshot()

    def _run(self, session: LoginSession, provider) -> None:
        try:
            result: LoginResult = provider.run(session)
            if session.cancelled():
                session.update("cancelled", "登录已取消")
                if not self.accounts.get(session.account_id)["platform_uid"]:
                    self.accounts.set_auth(session.account_id, "cancelled")
                return
            expected = str(self.accounts.get(session.account_id).get("expected_uid") or "")
            if expected and result.platform_uid != expected:
                raise IdentityMismatch("登录账号与预期 UID 不符，未保存凭据或启用自动回复")
            # 保留 provider 返回的 UID；后续状态通知不能再把它覆盖为空。
            session.platform_uid = result.platform_uid.strip()
            if result.requires_confirmation:
                session.update("awaiting_confirm", "请核对机器人账号后确认", platform_uid=session.platform_uid)
                while not session.cancelled() and not session.wait_confirmation(1.0):
                    pass
                if session.cancelled():
                    session.update("cancelled", "登录已取消")
                    if not self.accounts.get(session.account_id)["platform_uid"]:
                        self.accounts.set_auth(session.account_id, "cancelled")
                    return
            if expected and session.platform_uid != expected:
                raise IdentityMismatch("人工确认的 UID 与预期不符，未保存凭据或启用自动回复")
            if not session.platform_uid.strip():
                raise LoginError("identity_unverified", "未核对到稳定机器人 UID，已阻止启用")
            # 先核对身份，再写凭据和 bound；身份未确认时不会留下有效登录态。
            if session.cancelled():
                raise LoginError("cancelled", "登录已取消")
            session.update("completing", "正在校验账号并重载此通道", platform_uid=session.platform_uid)
            if result.credentials:
                self.vault.put_many(session.account_key, result.credentials)
            self.accounts.set_auth(
                session.account_id, "bound", platform_uid=session.platform_uid
            )
            if self.on_reload is not None:
                self.on_reload(session.account_id)
            session.update("bound", "登录完成；连接和发送健康请分别查看", platform_uid=session.platform_uid)
        except IdentityMismatch:
            session.update("failed", "登录 UID 与预期不符，未启用自动回复")
            self.accounts.set_connection(session.account_id, "error", "identity_mismatch")
        except LoginError as exc:
            if exc.code in ("cancelled", "qrcode_expired") or session.cancelled():
                state = "cancelled" if exc.code == "cancelled" else "expired"
            else:
                state = "needs_verification" if exc.code == "needs_human" else "failed"
            session.update(state, exc.safe_message)
            if state not in ("cancelled", "needs_verification"):
                self.accounts.set_connection(session.account_id, "error", exc.code)
        except CredentialError:
            # 不把后端异常中的任何秘密参数写入会话或日志。
            session.update("failed", "凭据保险库不可用；未保存明文，请检查 DPAPI 后重新登录")
            self.accounts.set_connection(session.account_id, "error", "vault_unavailable")
        except Exception as exc:  # noqa: BLE001 - 第三方浏览器/网络边界
            log.error("登录任务 %s 失败：%s", session.account_key, type(exc).__name__)
            session.update("failed", "登录未完成；请检查网络或人工验证状态后重试")
            self.accounts.set_connection(session.account_id, "error", "login_failed")
        finally:
            # 二维码只存在于有鉴权接口的进程内存，任务终结立即释放。
            with session._lock:
                session.qr_png = None
                session._verification_code = ""

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session.snapshot()

    def _find(self, session_id: str) -> LoginSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._find(session_id)
        session._cancel.set()
        session._confirm.set()
        if session.state not in TERMINAL:
            session.update("cancelled", "正在停止登录轮询")
            if not self.accounts.get(session.account_id)["platform_uid"]:
                self.accounts.set_auth(session.account_id, "cancelled")
        return session.snapshot()

    def verify(self, session_id: str, code: str) -> dict[str, Any]:
        session = self._find(session_id)
        session.set_verification_code(code.strip())
        return session.snapshot()

    def confirm(self, session_id: str, platform_uid: str) -> dict[str, Any]:
        session = self._find(session_id)
        if session.state not in ("awaiting_confirm", "needs_verification"):
            raise ValueError("当前登录任务不能确认此 UID")
        # QQ 走 OneBot：UID 是 NapCat 自己报的，界面没有输入框，传空就沿用平台自报的那个。
        # 抖音/B站是人工核对后输入的，仍然是传什么用什么。
        supplied = platform_uid.strip() or session.platform_uid.strip()
        if not supplied:
            raise ValueError("当前登录任务不能确认此 UID")
        session.platform_uid = supplied
        session._confirm.set()
        return session.snapshot()

    def shutdown(self) -> None:
        """受控退出时先取消全部任务；不杀其他账号的消息 worker。"""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.state not in TERMINAL:
                session._cancel.set()
                session._confirm.set()
        for session in sessions:
            if session._thread is not None and session._thread.is_alive():
                session._thread.join(timeout=50)
