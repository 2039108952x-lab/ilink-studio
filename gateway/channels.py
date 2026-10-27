"""账号级通道生命周期：登录/设置只热重载一个账号，不影响其他平台。"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from .accounts import AccountStore
from .models import Platform
from .registry import build_platform_adapter
from .vault import Vault

log = logging.getLogger(__name__)


class ChannelError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.safe_message = code, message


class ChannelManager:
    def __init__(self, accounts: AccountStore, vault: Vault, router,
                 *, config, data_dir) -> None:
        self.accounts, self.vault, self.router = accounts, vault, router
        self._config = config
        self.data_dir = data_dir
        self._tasks: dict[int, asyncio.Task] = {}
        self._adapters: dict[int, Any] = {}
        self._closed = False

    def _cfg(self):
        return self._config() if callable(self._config) else self._config

    def _options(self, account: dict) -> dict:
        pc = self._cfg().platforms.get(account["platform"])
        options = dict(pc.options) if pc else {}
        options["profile_dir"] = str(self.data_dir / "douyin-profile")
        return options

    def _build(self, account: dict):
        adapter = build_platform_adapter(account["platform"], self._options(account))
        if adapter is None:
            raise ChannelError("dependency_missing", "该通道依赖不可用；请查看脱敏日志，不假装已上线")
        adapter.attach_vault(self.vault)
        return adapter

    def _eligible(self, account: dict) -> None:
        if not self._cfg().ui.onboarding_confirmed:
            raise ChannelError("onboarding_required", "请先完成首启风险、数据位置和模型后端确认")
        if account["channel"] == "wxauto":
            raise ChannelError("unsupported_channel", "本机微信 4.x 不支持 wxauto")
        if account["auth_state"] != "bound" or not account["platform_uid"]:
            raise ChannelError("identity_unverified", "请先登录并核对机器人 UID，再启用此通道")
        if account["expected_uid"] and account["platform_uid"] != account["expected_uid"]:
            raise ChannelError("identity_mismatch", "机器人 UID 与预期不符，禁止启用")
        key = f"{account['platform']}:{account['channel']}"
        if account["channel"] in ("official", "cookie") and not self.vault.configured(key):
            raise ChannelError("credentials_missing", "保险库中没有此通道的凭据")

    async def enable(self, account_id: int) -> dict:
        if self._closed:
            raise ChannelError("shutdown", "网关正在退出")
        account = self.accounts.get(account_id)
        self._eligible(account)
        await self.disable(account_id)
        adapter = self._build(account)
        platform = Platform(account["platform"])
        self.router.bind_adapter(platform, adapter)
        self.accounts.set_connection(account_id, "starting")
        self.accounts.set_enabled(account_id, True)
        self._adapters[account_id] = adapter
        self._tasks[account_id] = asyncio.create_task(
            self._run(account_id, adapter), name=f"account-{account_id}-{account['platform']}"
        )
        return {"account_id": account_id, "enabled": True, "connection_state": "starting"}

    async def _run(self, account_id: int, adapter) -> None:
        account = self.accounts.get(account_id)
        backoff = 1.0
        try:
            while not self._closed and self.accounts.get(account_id)["enabled"]:
                started = asyncio.get_running_loop().time()
                try:
                    # starting 不等于已连接；探活/握手成功前不伪造 online。
                    await adapter.start()
                    current = self.accounts.get(account_id)
                    if current["connection_state"] == "needs_human":
                        return
                    self.accounts.set_connection(account_id, "stopped")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._closed or not self.accounts.get(account_id)["enabled"]:
                        return
                    code = "needs_human" if "captcha" in type(exc).__name__.lower() else "adapter_error"
                    if code == "needs_human":
                        self.accounts.set_connection(account_id, "needs_human", "captcha")
                        return
                    self.accounts.set_connection(account_id, "reconnecting", code)
                    log.warning("%s 通道异常，有界退避后重连：%s", account["platform"], type(exc).__name__)
                    if asyncio.get_running_loop().time() - started > 30:
                        backoff = 1.0
                    await asyncio.sleep(min(60, backoff) + random.random())
                    backoff = min(60, backoff * 2)
        finally:
            if self.accounts.get(account_id)["connection_state"] != "needs_human":
                self.accounts.set_connection(account_id, "stopped")

    async def disable(self, account_id: int) -> dict:
        task = self._tasks.pop(account_id, None)
        adapter = self._adapters.pop(account_id, None)
        if task:
            task.cancel()
        if adapter:
            # 先停止旧适配器接收，再从 Router 原子移除，其他账号不动。
            try:
                await adapter.stop()
            except Exception:
                log.warning("停止通道 %s 时发生异常", account_id, exc_info=True)
            self.router.unbind_adapter(Platform(self.accounts.get(account_id)["platform"]))
        if task:
            await asyncio.gather(task, return_exceptions=True)
        self.accounts.set_enabled(account_id, False)
        self.accounts.set_connection(account_id, "stopped")
        return {"account_id": account_id, "enabled": False, "connection_state": "stopped"}

    async def set_enabled(self, account_id: int, enabled: bool) -> dict:
        if enabled:
            return await self.enable(account_id)
        return await self.disable(account_id)

    async def reload(self, account_id: int) -> dict:
        return await self.enable(account_id)

    async def stop_all(self) -> None:
        self._closed = True
        for account_id in list(self._adapters):
            await self.disable(account_id)
