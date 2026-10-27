"""桌面控制台应用服务；HTTP 鉴权和传输安全在 server.py。"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .accounts import AccountStore
from . import logs as logs_mod
from .identity import IdentityStore, MergeConflict, MergeError
from .importer import FilterImportError, import_sessions_file
from .login import LoginManager
from .login.providers.base import LoginError
from .mailbox import Mailbox, MailboxError
from .brain import ReasonixBrain
from .channels import ChannelError
from .memory import MemoryError, MemoryStore
from .models import ChatKind, Platform
from .radar.provider import RadarError
from .settings import SettingsError, SettingsStore
from .stickers import StickerError, StickerStore
from .vault import CredentialUnavailable, Vault

log = logging.getLogger(__name__)
APP_VERSION = "0.1.0-rc1"

# 有内容雷达的平台（路径 /api/v1/radar/<平台>/<动作> 里认这些）
RADAR_PLATFORMS = ("douyin", "bilibili")

ONBOARDING_RISKS = (
    "NapCat、B站 cookie、抖音 RPA、wxauto 都是非官方路径，有封号风险",
    "聊天正文会发送给已配置的模型服务，不是「永不离开电脑」",
    "聊天正文以明文保存在本机 SQLite；凭据由 Windows DPAPI 保护",
    "DPAPI 通常只对本机当前 Windows 用户可解密，换用户或换机器需要重新登录",
)


class APIError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 400,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable


class ConsoleAPI:
    """界面业务接口。未知发送能力一律 unknown，不拿探活冒充可发消息。"""

    def __init__(self, accounts: AccountStore, vault: Vault, logins: LoginManager,
                 *, on_channel_switch=None, on_shutdown=None, on_pause=None,
                 static_root: Path | None = None, mailbox: Mailbox | None = None,
                 data_dir: Path | None = None, router=None,
                 stickers: StickerStore | None = None,
                 settings: SettingsStore | None = None,
                 on_settings_changed=None, metrics=None,
                 log_path: Path | None = None,
                 app_version: str = APP_VERSION, brain: ReasonixBrain | None = None,
                 on_account_enabled=None, radar=None) -> None:
        self.accounts, self.vault, self.logins = accounts, vault, logins
        self.on_channel_switch = on_channel_switch
        self.on_account_enabled = on_account_enabled
        self.on_shutdown = on_shutdown
        self.on_pause = on_pause
        self.static_root = static_root
        # M1 消息底座；没有库时消息类接口明确 503，不假装有一个空收件箱。
        self.mailbox = mailbox
        self.data_dir = data_dir
        # M2 身份与记忆；同一张 mailbox.db，不另开库。
        self.identity = IdentityStore(mailbox) if mailbox is not None else None
        self.memory = MemoryStore(mailbox) if mailbox is not None else None
        self.router = router
        self.stickers = stickers
        self.settings = settings
        self.on_settings_changed = on_settings_changed
        self.metrics = metrics
        self.log_path = Path(log_path) if log_path is not None else None
        self.app_version = app_version
        self.brain = brain
        # 内容雷达服务层（刷一轮 / 日报 / 分享）；没启用平台时为空，接口明确 503。
        # 传单个实例 = 老用法（等同抖音）；传 dict = 按平台分。
        self.radars: dict[str, Any] = self._normalize_radars(radar)

    def inbox(self) -> Mailbox:
        if self.mailbox is None:
            raise APIError("mailbox_unavailable", "消息库尚未初始化", status=503)
        return self.mailbox

    def account_list(self) -> list[dict[str, Any]]:
        configured = self.vault.configured_keys()
        result = []
        for account in self.accounts.list():
            account["credentials_configured"] = (
                f"{account['platform']}:{account['channel']}" in configured
            )
            account["capabilities"] = {
                "text_receive": "unknown", "text_send": "unknown",
                "image_receive": "unknown", "image_send": "unknown",
            }
            if account["channel"] == "official":
                account["capabilities"].update(text_receive="supported", text_send="supported",
                                               image_receive="degraded", image_send="unsupported")
            if account["channel"] == "wxauto":
                account["prerequisite"] = {
                    "supported": False,
                    "reason": "本机微信为 4.x；wxauto 仅支持微信 3.9.x，不会自动降级客户端",
                }
            result.append(account)
        return result

    @staticmethod
    def _clean_uid(raw: Any) -> str:
        """白名单 ID 清洗：别人从网页复制过来常带 `UID:` 前缀或全角空格。"""
        value = str(raw or "").replace("\u3000", " ").strip()
        for prefix in ("uid:", "uid：", "id:", "id：", "mid:"):
            if value.lower().startswith(prefix):
                return value[len(prefix):].strip()
        return value

    @staticmethod
    def _body_object(body: Any) -> dict:
        if not isinstance(body, dict):
            raise APIError("invalid_body", "请求体必须是 JSON 对象")
        return body

    @staticmethod
    def _id(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise APIError("invalid_id", "ID 格式错误") from exc
        if number <= 0:
            raise APIError("invalid_id", "ID 格式错误")
        return number

    def handle(self, method: str, path: str, body: Any = None,
               query: dict[str, list[str]] | None = None) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        # 所有调用都以 /api/v1 开头；路由不接受 .. 或反斜杠。
        if parts[:2] != ["api", "v1"] or any(p in (".", "..") for p in parts):
            raise APIError("not_found", "接口不存在", status=404)
        tail = parts[2:]
        try:
            if method == "GET" and tail == ["bootstrap"]:
                cfg = self.settings.current() if self.settings is not None else None
                confirmed = bool(cfg is not None and cfg.ui.onboarding_confirmed)
                return 200, {
                    "app": "iLink Studio", "version": self.app_version,
                    "sections": ["accounts", "inbox", "conversations", "radar",
                                 "memory", "stickers", "logs"],
                    "ai": {
                        "ready": bool(self.brain and self.brain.backend_ready),
                        "backend": cfg.brain.backend if cfg is not None else "cli",
                        "model": (cfg.brain.model if cfg is not None else "") or "默认模型",
                        "command": cfg.brain.command if cfg is not None else "",
                        "workspace": cfg.brain.workspace if cfg is not None else "",
                        "reason": "已配置可执行后端" if self.brain and self.brain.backend_ready else "未配置 Reasonix；本地消息库可用，AI 回复未启用",
                    },
                    "onboarding": {
                        "confirmed": confirmed,
                        "data_dir": str(self.data_dir) if self.data_dir is not None else "",
                        "autostart": bool(cfg is not None and cfg.ui.autostart),
                        "risks": list(ONBOARDING_RISKS),
                    },
                    "privacy": "聊天正文保存于本机明文 SQLite；配置模型后，正文会发送给该模型服务。",
                }
            if method == "POST" and tail == ["onboarding"]:
                if self.settings is None:
                    raise APIError("settings_unavailable", "设置服务未初始化", status=503)
                payload = self._body_object(body)
                missing = [
                    label for field, label in (
                        ("accept_risks", "平台风险"), ("accept_data", "数据位置"),
                        ("accept_model", "模型后端"),
                    ) if not payload.get(field)
                ]
                if missing:
                    raise APIError("onboarding_incomplete",
                                   "还需要确认：" + "、".join(missing), status=400)
                command = str(payload.get("command") or "").strip()
                model = str(payload.get("model") or "").strip()
                if command:
                    candidate = Path(command).expanduser()
                    if not candidate.is_absolute() or not candidate.is_file() or candidate.suffix.lower() not in (".exe", ".cmd", ".bat"):
                        raise APIError("backend_not_found", "请选择本机已有的 Reasonix .exe/.cmd 可执行文件", status=400)
                patch: dict[str, dict] = {"ui": {"onboarding_confirmed": True,
                                                "autostart": bool(payload.get("autostart"))}}
                brain_patch: dict[str, str] = {}
                if command:
                    brain_patch["command"] = command
                if model:
                    brain_patch["model"] = model
                if brain_patch:
                    patch["brain"] = brain_patch
                result = self.settings.update(patch)
                if self.on_settings_changed is not None:
                    self.on_settings_changed(result)
                return 202, {
                    "confirmed": True,
                    "autostart": bool(payload.get("autostart")),
                    "restart_required": True,
                    "message": "确认已保存；登录并启用通道后开始接收，是否回复取决于模型配置与白名单",
                }
            if method == "GET" and tail == ["napcat"]:
                from .napcat import describe as describe_napcat
                return 200, describe_napcat()
            if method == "GET" and tail == ["napcat", "qrcode"]:
                from .napcat import qrcode_state
                return 200, qrcode_state()
            if method == "POST" and tail == ["napcat", "launch"]:
                from .napcat import launch as launch_napcat
                result = launch_napcat()
                if not result.get("started"):
                    raise APIError(str(result.get("code") or "napcat_launch_failed"),
                                   str(result.get("message") or "启动 NapCat 失败"),
                                   status=409)
                return 202, result
            if method == "GET" and tail == ["allowlist"]:
                if self.settings is None:
                    raise APIError("settings_unavailable", "设置服务未初始化", status=503)
                cfg = self.settings.current()
                return 200, {
                    "allow_all": bool(cfg.allow_all),
                    "entries": {name: sorted(values) for name, values in cfg.allowlist.items()},
                    "candidates": (self.inbox().recent_senders(limit=24)
                                   if self.mailbox is not None else []),
                }
            if method in ("POST", "DELETE") and tail == ["allowlist"]:
                if self.settings is None:
                    raise APIError("settings_unavailable", "设置服务未初始化", status=503)
                payload = self._body_object(body)
                platform = str(payload.get("platform") or "")
                uid = self._clean_uid(payload.get("platform_uid"))
                if platform not in ("qq", "wechat", "bilibili", "douyin"):
                    raise APIError("unknown_platform", "未知平台", status=400)
                if not uid or len(uid) > 128:
                    raise APIError("invalid_request", "platform_uid 不合法", status=400)
                current = set(self.settings.current().allowlist.get(platform) or ())
                if method == "POST":
                    current.add(uid)
                else:
                    current.discard(uid)
                result = self.settings.update({"allowlist": {platform: sorted(current)}})
                if self.on_settings_changed is not None:
                    self.on_settings_changed(result)
                return 200, {
                    "platform": platform, "platform_uid": uid,
                    "allowed": method == "POST",
                    "entries": {name: sorted(values)
                                for name, values in result.config.allowlist.items()},
                }
            if method == "GET" and tail == ["accounts"]:
                return 200, {"accounts": self.account_list()}
            if method == "GET" and tail == ["runtime"]:
                return 200, {"paused": self.accounts.get_setting("reply_paused", "false") == "true"}
            if method == "POST" and tail == ["runtime", "pause"]:
                paused = self._body_object(body).get("paused")
                if not isinstance(paused, bool):
                    raise APIError("invalid_request", "paused 必须是布尔值")
                self.accounts.set_setting("reply_paused", str(paused).lower())
                if self.on_pause is not None:
                    self.on_pause(paused)
                return 200, {"paused": paused}
            if len(tail) == 3 and tail[:1] == ["accounts"] and tail[2] == "enabled" and method == "PUT":
                enabled = self._body_object(body).get("enabled")
                if not isinstance(enabled, bool):
                    raise APIError("invalid_request", "enabled 必须是布尔值")
                if self.on_account_enabled is None:
                    raise APIError("channel_unavailable", "通道管理尚未启用", status=503)
                try:
                    return 200, self.on_account_enabled(self._id(tail[1]), enabled)
                except ChannelError as exc:
                    raise APIError(exc.code, exc.safe_message, status=409) from exc
            if len(tail) == 3 and tail[:1] == ["accounts"] and tail[2] == "login" and method == "POST":
                return 202, self.logins.start(self._id(tail[1]))
            if len(tail) == 3 and tail[:1] == ["accounts"] and tail[2] == "probe" and method == "POST":
                return 200, self.probe(self._id(tail[1]))
            if len(tail) == 2 and tail[0] == "login-sessions" and method == "GET":
                return 200, self.logins.get(tail[1])
            if len(tail) == 3 and tail[0] == "login-sessions" and method == "POST":
                payload = self._body_object(body)
                if tail[2] == "cancel":
                    return 200, self.logins.cancel(tail[1])
                if tail[2] == "verify":
                    return 200, self.logins.verify(tail[1], str(payload.get("code") or ""))
                if tail[2] == "confirm":
                    return 200, self.logins.confirm(tail[1], str(payload.get("platform_uid") or ""))
            if tail == ["accounts", "wechat", "active-channel"] and method == "PUT":
                payload = self._body_object(body)
                channel = payload.get("channel")
                if channel == "wxauto":
                    raise APIError("unsupported_channel", "本机微信为 4.x；wxauto 仅支持 3.9.x", status=409)
                if channel != "official":
                    raise APIError("unknown_channel", "未知微信通道", status=400)
                if self.on_channel_switch is not None:
                    self.on_channel_switch(channel)
                self.accounts.switch_wechat(channel)
                return 200, {"channel": channel}
            if tail == ["runtime", "shutdown"] and method == "POST":
                if self.on_shutdown is None:
                    raise APIError("shutdown_unavailable", "当前不是桌面托管模式", status=409)
                self.on_shutdown()
                return 202, {"accepted": True}
            # ---- M1 消息底座：收件箱 / 会话 / 检索 / 发送 / 运维 ----
            if method == "GET" and tail == ["messages"]:
                return 200, self._messages(query or {})
            if method == "GET" and len(tail) == 2 and tail[0] == "messages":
                message = self.inbox().get_message_detail(self._id(tail[1]))
                if message is None:
                    raise APIError("not_found", "消息不存在", status=404)
                return 200, message
            if len(tail) == 3 and tail[0] == "messages" and tail[2] == "confirm" and method == "POST":
                return 200, self.inbox().confirm_message(self._id(tail[1]))
            if method == "GET" and tail == ["conversations"]:
                limit = int(self._q(query or {}, "limit", "50") or 50)
                return 200, {"conversations": self.inbox().list_conversations(limit=limit)}
            if (method == "GET" and len(tail) == 3 and tail[0] == "conversations"
                    and tail[2] == "messages"):
                params = {key: list(value) for key, value in (query or {}).items()}
                params["conversation_id"] = [tail[1]]
                return 200, self._messages(params)
            if method == "GET" and tail == ["stats"]:
                return 200, self.inbox().stats()
            if method == "POST" and tail == ["outbox"]:
                return 202, self._create_outbox(self._body_object(body))
            if method == "GET" and len(tail) == 2 and tail[0] == "outbox":
                task = self.inbox().get_outbox(self._id(tail[1]))
                if task is None:
                    raise APIError("not_found", "发送任务不存在", status=404)
                return 200, task
            if (len(tail) == 3 and tail[0] == "outbox" and tail[2] == "reconcile"
                    and method == "POST"):
                action = str(self._body_object(body).get("action") or "")
                return 200, self.inbox().reconcile_outbox(self._id(tail[1]), action)
            if method == "POST" and tail == ["backup"]:
                return 200, self._backup()
            if method == "POST" and tail == ["imports", "sessions"]:
                return 200, self._import_sessions()
            if method == "GET" and tail == ["audit"]:
                limit = int(self._q(query or {}, "limit", "100") or 100)
                return 200, {"events": self.inbox().list_audit(limit=limit)}
            # ---- M4 日志与诊断 ----
            if method == "GET" and tail == ["logs"]:
                lines = int(self._q(query or {}, "lines", "200") or 200)
                level = self._q(query or {}, "level") or None
                platform_name = self._q(query or {}, "platform") or None
                entries = [] if self.log_path is None else logs_mod.tail(
                    self.log_path, lines=lines, level=level, platform=platform_name,
                )
                return 200, {
                    "entries": entries,
                    "available": self.log_path is not None,
                    "file": self.log_path.name if self.log_path is not None else "",
                }
            if method == "GET" and tail == ["diagnostics"]:
                return 200, self._diagnostics()
            if method == "POST" and tail == ["diagnostics", "export"]:
                payload = self._diagnostics()
                if self.log_path is not None:
                    payload["logs_tail"] = logs_mod.tail(self.log_path, lines=200)
                payload["excluded"] = ["聊天正文", "附件", "凭据", "二维码内容"]
                directory = (self.data_dir.parent / "logs") if self.data_dir is not None else Path("logs")
                target = logs_mod.write_diagnostics(directory, payload)
                return 200, {
                    "path": str(target), "bytes": target.stat().st_size,
                    "excluded": payload["excluded"],
                    "logs_lines": len(payload.get("logs_tail") or []),
                }
            # ---- M3 表情与能力 ----
            if tail[:1] == ["radar"]:
                return self._radar_route(method, tail[1:], body, query or {})
            if method == "GET" and tail == ["stickers"]:
                items = self._sticker_store().list(
                    query=self._q(query or {}, "q"),
                    tag=self._q(query or {}, "tag"),
                    enabled_only=self._q(query or {}, "enabled", "1") != "0",
                )
                return 200, {"stickers": [self._sticker_dict(item) for item in items]}
            if method == "POST" and tail == ["stickers"]:
                return self._import_sticker(self._body_object(body))
            if method == "PATCH" and len(tail) == 2 and tail[0] == "stickers":
                return 200, {"sticker": self._patch_sticker(
                    self._id(tail[1]), self._body_object(body))}
            if method == "GET" and tail == ["capabilities"]:
                if self.router is None:
                    raise APIError("unavailable", "适配器尚未启动，能力未知", status=503)
                return 200, {"platforms": self.router.capabilities()}
            # ---- M2 身份隔离与统一记忆 ----
            if method == "GET" and tail == ["persons"]:
                return 200, {"persons": self._identity().list_persons(
                    query=self._q(query or {}, "q"))}
            if method == "GET" and len(tail) == 2 and tail[0] == "persons":
                return 200, self._person_detail(self._id(tail[1]))
            if (method == "GET" and len(tail) == 3 and tail[0] == "persons"
                    and tail[2] == "candidates"):
                return 200, {"candidates": self._identity().candidates(self._id(tail[1]))}
            if (method == "POST" and len(tail) == 3 and tail[0] == "persons"
                    and tail[2] == "merges"):
                return 200, self._merge(self._id(tail[1]), self._body_object(body))
            if method == "GET" and tail == ["merges"]:
                limit = int(self._q(query or {}, "limit", "50") or 50)
                return 200, {"merges": self._identity().merges(limit=limit)}
            if (method == "POST" and len(tail) == 3 and tail[0] == "merges"
                    and tail[2] == "revert"):
                return 200, self._identity().revert(self._id(tail[1]))
            if method == "GET" and tail == ["persona"]:
                return 200, self._persona_dict(self._memory().persona())
            if method == "PUT" and tail == ["persona"]:
                payload = self._body_object(body)
                persona = self._memory().update_persona(
                    name=payload.get("name"), content=payload.get("content"),
                    note=str(payload.get("note") or ""),
                )
                return 200, self._persona_dict(persona)
            if method == "GET" and tail == ["persona", "versions"]:
                return 200, {"versions": self._memory().persona_versions()}
            if method == "GET" and tail == ["memory-facts"]:
                return 200, {"facts": self._facts(query or {})}
            if method == "PATCH" and len(tail) == 2 and tail[0] == "memory-facts":
                return 200, self._patch_fact(self._id(tail[1]), self._body_object(body))
            if method == "GET" and tail == ["settings"]:
                credentials = {key: {"configured": True} for key in self.vault.configured_keys()}
                if self.settings is None:
                    return 200, {"credentials": credentials}
                config = self.settings.snapshot()
                return 200, {
                    "config": config,
                    "credentials": credentials,
                    "onboarding": {"confirmed": bool(config["ui"]["onboarding_confirmed"])},
                }
            if method == "PATCH" and tail == ["settings"]:
                if self.settings is None:
                    raise APIError("settings_unavailable", "设置服务未初始化", status=503)
                payload = self._body_object(body)
                result = self.settings.update(payload)
                if self.on_settings_changed is not None:
                    self.on_settings_changed(result)
                return 200, {
                    "applied": result.applied,
                    "restart_required": result.restart_required,
                    "config": result.config.as_dict(),
                }
            if method == "POST" and tail == ["settings", "secrets"]:
                if self.settings is None:
                    raise APIError("settings_unavailable", "设置服务未初始化", status=503)
                payload = self._body_object(body)
                account_key = str(payload.get("account_key") or payload.get("account") or "")
                key = str(payload.get("key") or "")
                value = str(payload.get("value") or "")
                self.settings.write_secret(account_key, key, value)
                return 200, {"configured": True, "account_key": account_key, "key": key}
            raise APIError("not_found", "接口不存在", status=404)
        except MailboxError as exc:
            status = 404 if exc.code == "not_found" else (
                409 if exc.code in ("conversation_unknown", "not_reconcilable", "not_reviewable",
                                    "merge_conflict", "merge_blocked", "already_reverted")
                else (503 if exc.code in ("database_write_failed", "mailbox_error") else 400))
            raise APIError(exc.code, str(exc), status=status,
                           retryable=status == 503) from exc
        except SettingsError as exc:
            status = 503 if exc.code in ("settings_write_failed", "vault_unavailable") else 400
            raise APIError(exc.code, str(exc), status=status,
                           retryable=status == 503) from exc
        except FilterImportError as exc:
            raise APIError(exc.code, str(exc), status=400) from exc
        except LoginError as exc:
            status = 409 if exc.code in ("login_in_progress", "identity_mismatch", "unsupported_channel") else 400
            raise APIError(exc.code, exc.safe_message, status=status) from exc
        except KeyError as exc:
            raise APIError("not_found", "登录任务不存在", status=404) from exc
        except ValueError as exc:
            raise APIError("invalid_request", str(exc)) from exc
        except CredentialUnavailable as exc:
            raise APIError("vault_unavailable", str(exc), status=503, retryable=True) from exc

    # ---- M1 辅助 ----

    @staticmethod
    def _q(query: dict[str, list[str]], name: str, default: str = "") -> str:
        values = query.get(name) or []
        return str(values[0]) if values else default

    def _messages(self, query: dict[str, list[str]]) -> dict:
        def int_or_none(name: str) -> int | None:
            raw = self._q(query, name)
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise APIError("invalid_request", f"{name} 必须是数字") from exc

        limit_raw = self._q(query, "limit", "50") or "50"
        try:
            limit = int(limit_raw)
        except ValueError as exc:
            raise APIError("invalid_request", "limit 必须是数字") from exc
        page = self.inbox().list_messages(
            limit=limit,
            cursor=self._q(query, "cursor") or None,
            platform=self._q(query, "platform") or None,
            direction=self._q(query, "direction") or None,
            status=self._q(query, "status") or None,
            kind=self._q(query, "kind") or None,
            conversation_id=int_or_none("conversation_id"),
            thread_id=int_or_none("thread_id"),
            person_id=int_or_none("person_id"),
            query=self._q(query, "q") or None,
        )
        return {"items": page.items, "next_cursor": page.next_cursor, "slow": page.slow}

    def _create_outbox(self, body: dict) -> dict:
        try:
            platform = Platform(str(body.get("platform") or ""))
            kind = ChatKind(str(body.get("kind") or "private"))
        except ValueError as exc:
            raise APIError("invalid_request", f"平台或会话类型不认识：{exc}") from exc
        chat_id = str(body.get("chat_id") or "").strip()
        text = str(body.get("text") or "")
        if not chat_id:
            raise APIError("invalid_request", "缺少 chat_id")
        result = self.inbox().enqueue_outbox(
            platform=platform.value, kind=kind.value, chat_id=chat_id, text=text,
            client_request_id=str(body.get("client_request_id") or ""),
        )
        return {"outbox_id": result.outbox_id, "state": result.state,
                "created": result.created}

    # ---- M2 辅助 ----

    def _identity(self) -> IdentityStore:
        if self.identity is None:
            raise APIError("mailbox_unavailable", "消息库尚未初始化", status=503)
        return self.identity

    def _memory(self) -> MemoryStore:
        if self.memory is None:
            raise APIError("mailbox_unavailable", "消息库尚未初始化", status=503)
        return self.memory

    @staticmethod
    def _persona_dict(persona) -> dict:
        return {"name": persona.name, "content": persona.content,
                "version": persona.version, "updated_at": persona.updated_at}

    def _merge(self, target_id: int, payload: dict) -> dict:
        source_id = payload.get("source_person_id")
        if not source_id:
            raise APIError("invalid_request", "缺少 source_person_id")
        try:
            source_id = int(source_id)
        except (TypeError, ValueError) as exc:
            raise APIError("invalid_request", "source_person_id 必须是数字") from exc
        if payload.get("confirm"):
            return self._identity().merge(source_id, target_id,
                                          note=str(payload.get("note") or ""))
        return self._identity().preview_merge(source_id, target_id).as_dict()

    def _person_detail(self, person_id: int) -> dict:
        persons = {item["id"]: item for item in self._identity().list_persons()}
        entry = persons.get(person_id)
        if entry is None:
            raise APIError("not_found", "人物不存在", status=404)
        facts = self._memory().facts(person_id, status=None, limit=200)
        window: list[dict] = []
        if entry["private_thread_id"]:
            window = [asdict(item) for item in
                      self._memory().window(entry["private_thread_id"], turns=12).items]
        return {"person": entry, "facts": [asdict(fact) for fact in facts],
                "window": window, "candidates": self._identity().candidates(person_id)}

    def _facts(self, query: dict[str, list[str]]) -> list[dict]:
        raw = self._q(query, "person_id")
        if not raw:
            raise APIError("invalid_request", "缺少 person_id")
        try:
            person_id = int(raw)
        except ValueError as exc:
            raise APIError("invalid_request", "person_id 必须是数字") from exc
        status = self._q(query, "status", "active")
        facts = self._memory().facts(person_id, status=status or None, limit=200)
        return [asdict(fact) for fact in facts]

    def _patch_fact(self, fact_id: int, payload: dict) -> dict:
        if not any(key in payload for key in ("content", "status", "frozen")):
            raise APIError("invalid_request", "需要 content、status 或 frozen")
        if "content" in payload:
            self._memory().update_fact(fact_id, str(payload.get("content") or ""))
        if "status" in payload:
            self._memory().set_fact_status(fact_id, str(payload["status"]))
        if "frozen" in payload:
            self._memory().freeze_fact(fact_id, bool(payload["frozen"]))
        return {"ok": True, "fact_id": fact_id}

    # ---- M4 辅助 ----

    def _diagnostics(self) -> dict:
        cfg = self.settings.current() if self.settings is not None else None
        if self.mailbox is not None:
            db_state = {
                "file": "mailbox.db",
                "bytes": self.mailbox.path.stat().st_size if self.mailbox.path.is_file() else 0,
                "schema": self.mailbox.schema_version(),
                "queue": self.mailbox.pending_counts(),
                "health": self.mailbox.health,
            }
        else:
            db_state = {}
        metrics = {}
        if self.metrics is not None:
            metrics = self.metrics.snapshot(
                price_per_1k_input=cfg.budget.price_per_1k_input if cfg is not None else 0.0,
                price_per_1k_output=cfg.budget.price_per_1k_output if cfg is not None else 0.0,
            )
        adapters: dict[str, dict] = {}
        if self.router is not None:
            accounts = self.accounts.list()
            for platform_name, capabilities in self.router.capabilities().items():
                row = next((item for item in accounts if item["platform"] == platform_name), {})
                adapters[platform_name] = {
                    "capabilities": capabilities,
                    "connection_state": row.get("connection_state"),
                    "send_health": row.get("send_health"),
                }
        return {
            "app": {"name": "iLink Studio", "version": self.app_version},
            "database": db_state,
            "metrics": metrics,
            "adapters": adapters,
            "onboarding": {
                "confirmed": bool(cfg is not None and cfg.ui.onboarding_confirmed),
            },
            "budget": {
                "daily_tokens": cfg.budget.daily_tokens if cfg is not None else 0,
                "daily_cost_usd": cfg.budget.daily_cost_usd if cfg is not None else 0.0,
                "platform_per_minute": cfg.budget.platform_per_minute if cfg is not None else 0,
                "queue_limit": cfg.budget.queue_limit if cfg is not None else 0,
            },
            "privacy": "不含聊天正文、附件、凭据与二维码内容",
        }

    # ---- M3 辅助 ----

    @staticmethod
    def _normalize_radars(raw: Any) -> dict[str, Any]:
        """把构造参数归一成 ``{平台: 服务}``。

        兼容两种传法：单个实例（老用法，等同抖音）、以及按平台的 dict。
        """
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items() if v is not None}
        return {"douyin": raw}

    def _radar(self, platform: str = ""):
        """取某个平台的雷达。不指定时按历史行为优先给抖音。"""
        if not self.radars:
            raise APIError("radar_unavailable",
                           "内容雷达未启用：先启用对应平台通道并完成登录", status=503)
        if not platform:
            if "douyin" in self.radars:
                return self.radars["douyin"]
            if len(self.radars) == 1:
                return next(iter(self.radars.values()))
            raise APIError("invalid_args",
                           f"要指定平台：{', '.join(sorted(self.radars))}", status=400)
        service = self.radars.get(platform)
        if service is None:
            raise APIError("radar_unavailable",
                           f"{platform} 的内容雷达没启用（看配置里的 enabled 与登录态）",
                           status=503)
        return service

    def _radar_route(self, method: str, tail: list[str], body: Any,
                     query: dict[str, list[str]]) -> tuple[int, dict]:
        """内容雷达：状态 / 候选 / 刷一轮 / 日报 / 分享 / 视频列表。

        路径是 ``/api/v1/radar/<平台>/<动作>``；不带平台时按老行为走抖音。

        刷一轮是长任务（推荐流 + 字幕 + 模型判断，可达一两分钟）。这里同步等它跑完 ——
        控制面每个请求一个线程，不会把别的接口卡住。
        """
        platform = ""
        if tail and tail[0] in RADAR_PLATFORMS:
            platform, tail = tail[0], tail[1:]
        radar = self._radar(platform)
        try:
            if method == "GET" and tail == ["status"]:
                return 200, radar.status()
            if method == "GET" and tail == ["doctor"]:
                return 200, radar.doctor()
            if method == "GET" and tail == ["settings"]:
                return 200, radar.settings()
            if method == "POST" and tail == ["settings"]:
                return 200, radar.save_settings(self._body_object(body))
            if method == "GET" and tail == ["candidates"]:
                return 200, radar.snapshot()
            if method == "GET" and tail == ["videos"]:
                # 「它刷过的视频」：B 站从历史日报里解析，抖音退回上一轮快照
                return 200, radar.videos(
                    day=self._q(query, "day"),
                    grade=self._q(query, "grade"),
                    status=self._q(query, "status"),
                    page=self._q(query, "page", "1"),
                    size=self._q(query, "size", "20"),
                )
            if method == "GET" and tail == ["reports"]:
                return 200, {"items": radar.history()}
            if method == "GET" and tail == ["report"]:
                return 200, radar.report(self._q(query, "day"))
            if method == "POST" and tail == ["run"]:
                payload = self._body_object(body)
                try:
                    top = int(payload.get("top") or 8)
                except (TypeError, ValueError):
                    top = 8
                return 200, radar.run_round(top=max(1, min(top, 30)))
            if method == "POST" and tail == ["share"]:
                payload = self._body_object(body)
                item_id = str(payload.get("item_id") or "").strip()
                target = str(payload.get("to") or "").strip()
                if not item_id:
                    raise APIError("invalid_args", "缺 item_id")
                if not target:
                    raise APIError("invalid_args", "要分享给谁？填收件人")
                return 200, radar.share(
                    item_id=item_id, to=target,
                    reason=str(payload.get("reason") or ""),
                    confirm=payload.get("confirm") is True,
                )
            if method == "POST" and tail == ["comment"]:
                # 发评论是公开行为：不带 confirm 只回草稿，一个字都不发
                payload = self._body_object(body)
                item_id = str(payload.get("item_id") or payload.get("bvid") or "").strip()
                text = str(payload.get("text") or "").strip()
                if not item_id:
                    raise APIError("invalid_args", "缺 item_id（B 站传 BV 号）")
                if not text:
                    raise APIError("invalid_args", "评论正文是空的")
                try:
                    reply_to = int(payload.get("reply_to") or 0)
                    root = int(payload.get("root") or 0)
                except (TypeError, ValueError):
                    raise APIError("invalid_args", "reply_to / root 要是数字") from None
                return 200, radar.comment(
                    item_id=item_id, text=text, reply_to=reply_to, root=root,
                    confirm=payload.get("confirm") is True,
                )
        except RadarError as exc:
            raise APIError(exc.code, exc.message, status=409,
                           retryable=exc.retryable) from exc
        raise APIError("not_found", "接口不存在", status=404)

    def _sticker_store(self) -> StickerStore:
        if self.stickers is None:
            raise APIError("stickers_unavailable", "表情库尚未初始化", status=503)
        return self.stickers

    @staticmethod
    def _sticker_dict(sticker) -> dict:
        return {
            "id": sticker.id, "name": sticker.name, "mime": sticker.mime,
            "w": sticker.w, "h": sticker.h, "bytes": sticker.bytes,
            "sha256": sticker.sha256, "tags": list(sticker.tags),
            "enabled": sticker.enabled, "source": sticker.source,
            "source_message_id": sticker.source_message_id,
            "created_at": sticker.created_at, "updated_at": sticker.updated_at,
        }

    def _import_sticker(self, payload: dict) -> tuple[int, dict]:
        store = self._sticker_store()
        if payload.get("scan_directory"):
            return 200, store.import_directory()
        if not payload.get("source_message_id"):
            raise APIError(
                "invalid_request",
                "需要 source_message_id（从聊天图片入库）或 scan_directory（扫描表情目录）",
            )
        try:
            message_id = int(payload["source_message_id"])
        except (TypeError, ValueError) as exc:
            raise APIError("invalid_request", "source_message_id 必须是数字") from exc
        tags = payload.get("tags") or []
        if not isinstance(tags, list):
            raise APIError("invalid_request", "tags 必须是数组")
        result = store.import_from_message(
            message_id, name=str(payload.get("name") or ""), tags=[str(tag) for tag in tags],
        )
        return (201 if result.created else 200), {
            "sticker": self._sticker_dict(result.sticker), "created": result.created,
        }

    def _patch_sticker(self, sticker_id: int, payload: dict) -> dict:
        store = self._sticker_store()
        sticker = store.get(sticker_id)
        if "name" in payload:
            sticker = store.rename(sticker.id, str(payload["name"] or ""))
        if "tags" in payload:
            tags = payload["tags"] or []
            if not isinstance(tags, list):
                raise APIError("invalid_request", "tags 必须是数组")
            sticker = store.set_tags(sticker.id, [str(tag) for tag in tags])
        if "enabled" in payload:
            sticker = store.set_enabled(sticker.id, bool(payload["enabled"]))
        return self._sticker_dict(sticker)

    def _backup(self) -> dict:
        path = self.inbox().backup()
        from . import db as db_mod
        size = path.stat().st_size if path.is_file() else 0
        return {"path": str(path), "bytes": size, "integrity": db_mod.integrity_check(path)}

    def _import_sessions(self) -> dict:
        if self.data_dir is None:
            raise APIError("unavailable", "数据目录未知，无法定位 sessions.json", status=503)
        report = import_sessions_file(self.inbox(), self.data_dir / "sessions.json")
        return report.as_dict()

    def probe(self, account_id: int) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        key = f"{account['platform']}:{account['channel']}"
        if key == "wechat:wxauto":
            self.accounts.set_connection(account_id, "error", "unsupported_channel")
            raise APIError("unsupported_channel", "本机微信 4.x 不支持 wxauto", status=409)
        if not self.vault.configured(key) and key != "qq:onebot":
            self.accounts.set_connection(account_id, "stopped", "credentials_missing")
            return {"ok": False, "code": "credentials_missing", "message": "请先登录并保存凭据"}
        try:
            if key == "qq:onebot":
                self._probe_qq(account)
            elif key == "bilibili:cookie":
                self._probe_bilibili(key, account)
            else:
                # 微信探活可能消费长轮询游标；登录后只做显式状态展示，不伪报在线。
                self.accounts.set_connection(account_id, "stopped")
                return {"ok": False, "code": "probe_not_available",
                        "message": "该通道没有无副作用探活；尚未验证连接与发送能力"}
        except APIError:
            raise
        except (OSError, TimeoutError) as exc:
            self.accounts.set_connection(account_id, "error", "network_error")
            raise APIError("network_error", "本机协议端连接失败；这不代表凭据已过期",
                           status=503, retryable=True) from exc
        return {"ok": True, "account_id": account_id}

    def _probe_qq(self, account: dict) -> None:
        """同时验证 loopback TCP 与 OneBot get_login_info，不把端口开放当登录成功。"""
        from websockets.sync.client import connect
        url = "ws://127.0.0.1:3001"
        with socket.create_connection(("127.0.0.1", 3001), timeout=3):
            pass
        with connect(url, open_timeout=4, close_timeout=1) as ws:
            ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "ilink-probe"}))
            reply = json.loads(ws.recv(timeout=5))
        data = reply.get("data") if isinstance(reply, dict) else None
        uid = str((data or {}).get("user_id") or "")
        if not uid:
            self.accounts.set_connection(account["id"], "error", "onebot_handshake_failed")
            raise APIError("onebot_handshake_failed", "端口已开放，但未取得有效 OneBot 登录身份", status=503)
        expected = str(account.get("expected_uid") or "")
        if expected and uid != expected:
            self.accounts.set_connection(account["id"], "error", "identity_mismatch")
            raise APIError("identity_mismatch", "OneBot 登录 UID 与配置小号不符", status=409)
        self.accounts.set_auth(account["id"], "bound", platform_uid=uid)
        self.accounts.set_connection(account["id"], "online")

    def _probe_bilibili(self, key: str, account: dict) -> None:
        import httpx
        cookies = {name: self.vault.get(key, name) for name in
                   ("SESSDATA", "bili_jct", "DedeUserID", "buvid3")}
        response = httpx.get("https://api.bilibili.com/x/web-interface/nav",
                             cookies=cookies, timeout=8)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not data.get("isLogin"):
            self.accounts.set_auth(account["id"], "expired")
            self.accounts.set_connection(account["id"], "error", "auth_expired")
            raise APIError("auth_expired", "B站凭据已失效，请重新登录", status=409)
        uid = str(data.get("mid") or cookies["DedeUserID"])
        self.accounts.set_auth(account["id"], "bound", platform_uid=uid)
        self.accounts.set_connection(account["id"], "online")
