"""图形界面设置：覆盖层 + 原子写入 + 校验 + 热重载。

为什么不直接改 `config.toml`
---------------------------
那是用户手写的文件：有注释、有本程序不认识的段。整文件重写会吃掉这些东西。
所以界面的修改写到 `data/ui-settings.json`（覆盖层），启动时按
`config.toml → 覆盖层` 合成；`config.toml` 永远只读。

只允许 schema 明确允许的字段：界面设置、记忆、预算、表情、日志、模型、通道、白名单与端口。
`paths` 与任意秘密字段不可写；危险 listen host / allow_all 会被拒绝。
秘密字段不接受明文写入设置文件，只接受「写进保险库」这一个动作（FR-7.3）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import config as cfg_mod
from .config import GatewayConfig

log = logging.getLogger(__name__)

EDITABLE_SECTIONS = ("ui", "memory", "budget", "stickers", "logging")
RUNTIME_SECTIONS = ("brain", "platforms", "allowlist", "server")
BRAIN_FIELDS = {"command", "model", "workspace", "timeout", "max_steps", "keep_session"}
# 精确名或「_token」这类后缀才算秘密；daily_tokens 是计量字段，不是凭据。
SECRET_NAMES = {"token", "secret", "password", "passwd", "sessdata", "context_token", "cookie"}
SECRET_SUFFIXES = ("_token", "_secret", "_password", "_cookie", "_sessdata", "_passwd")


def _looks_secret(name: str) -> bool:
    lowered = str(name or "").lower()
    return lowered in SECRET_NAMES or lowered.endswith(SECRET_SUFFIXES)


class SettingsError(RuntimeError):
    code = "settings_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class SettingsInvalid(SettingsError):
    code = "settings_invalid"


class SettingsWriteFailed(SettingsError):
    code = "settings_write_failed"


@dataclass(slots=True)
class SettingsResult:
    config: GatewayConfig
    applied: list[str]
    restart_required: list[str]


class SettingsStore:
    def __init__(self, base: GatewayConfig, path: str | Path, *,
                 production: bool = False,
                 secret_writer: Callable[[str, str, str], None] | None = None,
                 clock=time.time) -> None:
        self._base = base
        self._path = Path(path)
        self._production = production
        self._secret_writer = secret_writer
        self._clock = clock
        self._overlay: dict[str, Any] = {}
        self._config = base
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def current(self) -> GatewayConfig:
        return self._config

    def snapshot(self) -> dict:
        return self._config.as_dict()

    def overlay(self) -> dict:
        return copy.deepcopy(self._overlay)

    # ---- 读 ----

    def _load(self) -> None:
        if not self._path.is_file():
            self._config = self._base
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 覆盖层坏了不致命：按 config.toml 启动，留一条日志，等界面重写
            log.warning("界面设置文件损坏，已按 config.toml 启动：%s", self._path.name)
            self._overlay = {}
            self._config = self._base
            return
        self._overlay = payload if isinstance(payload, dict) else {}
        try:
            self._config = self._compose(self._overlay)
        except SettingsInvalid:
            log.warning("界面设置文件内容非法，已忽略：%s", self._path.name)
            self._overlay = {}
            self._config = self._base

    def _compose(self, overlay: Mapping[str, Any]) -> GatewayConfig:
        cfg = copy.deepcopy(self._base)
        for section, values in overlay.items():
            if section not in EDITABLE_SECTIONS + RUNTIME_SECTIONS or not isinstance(values, Mapping):
                continue
            if section == "allowlist":
                if "allow_all" in values and bool(values["allow_all"]):
                    raise SettingsInvalid("界面不允许开启 allow_all，请使用稳定平台 ID 白名单")
                allow = {str(name) for name in cfg.platforms} | {"qq", "wechat", "bilibili", "douyin"}
                for name, ids in values.items():
                    if name == "allow_all":
                        cfg.allow_all = False
                        continue
                    if name not in allow or not isinstance(ids, list) or not all(
                        isinstance(uid, str) and uid.strip() and len(uid) <= 128 for uid in ids
                    ):
                        raise SettingsInvalid("白名单必须是各平台稳定 ID 的非空字符串数组")
                    cfg.allowlist[name] = {uid.strip() for uid in ids if uid.strip()}
                continue
            if section == "platforms":
                for name, options in values.items():
                    if name not in ("qq", "wechat", "bilibili", "douyin") or not isinstance(options, Mapping):
                        raise SettingsInvalid("未知平台配置")
                    allowed = {"enabled"}
                    if name == "qq":
                        allowed |= {"onebot_ws", "require_mention"}
                    elif name == "wechat":
                        allowed |= {"mode", "skip_backlog"}
                    elif name == "bilibili":
                        allowed |= {"watch_private", "watch_comment", "notify_interval"}
                    elif name == "douyin":
                        allowed |= {"poll_interval", "max_conversations"}
                    unknown = set(options) - allowed
                    if unknown:
                        raise SettingsInvalid(f"[platforms.{name}] 不允许界面修改：{', '.join(sorted(unknown))}")
                    current_options = dict(cfg.platforms[name].options) if name in cfg.platforms else {}
                    current_options.update(options)
                    enabled = bool(current_options.pop("enabled", False))
                    if enabled and name == "wechat" and current_options.get("mode", "official") == "wxauto":
                        raise SettingsInvalid("本机微信 4.x 不支持 wxauto，请选择 official")
                    cfg.platforms[name] = cfg_mod.PlatformConfig(enabled=enabled, options=current_options)
                continue
            target = getattr(cfg, section)
            for key, value in values.items():
                if not hasattr(target, key) or section == "brain" and key not in BRAIN_FIELDS:
                    raise SettingsInvalid(f"[{section}] 没有这个字段或不允许修改：{key}")
                if section == "server" and key not in ("port",):
                    raise SettingsInvalid("监听地址只能在发布配置中固定为 127.0.0.1")
                current = getattr(target, key)
                if isinstance(current, str) and not isinstance(value, str):
                    raise SettingsInvalid(f"[{section}].{key} 必须是字符串")
                if isinstance(current, bool) and not isinstance(value, bool):
                    raise SettingsInvalid(f"[{section}].{key} 必须是布尔值")
                if isinstance(current, (int, float)) and not isinstance(value, (int, float)):
                    raise SettingsInvalid(f"[{section}].{key} 必须是数字")
                try:
                    if isinstance(current, bool):
                        setattr(target, key, value)
                    elif isinstance(current, int):
                        setattr(target, key, int(value))
                    elif isinstance(current, float):
                        setattr(target, key, float(value))
                    else:
                        setattr(target, key, value)
                except (TypeError, ValueError) as exc:
                    raise SettingsInvalid(f"[{section}].{key} 的值类型不对") from exc
            problems = cfg_mod.validate(cfg, production=self._production)
            if problems:
                raise SettingsInvalid("；".join(problems))
        return cfg

    # ---- 写 ----

    def update(self, patch: Mapping[str, Any]) -> SettingsResult:
        if not isinstance(patch, Mapping) or not patch:
            raise SettingsInvalid("请求体必须是非空对象")
        blocked = [key for key in patch if key not in EDITABLE_SECTIONS + RUNTIME_SECTIONS]
        if blocked:
            raise SettingsInvalid("这些段不允许从界面修改：" + ", ".join(sorted(blocked)))
        for section, values in patch.items():
            if not isinstance(values, Mapping) or not values:
                raise SettingsInvalid(f"[{section}] 必须是非空对象")
            for key in values:
                if _looks_secret(key):
                    raise SettingsInvalid(
                        f"[{section}].{key} 是秘密字段：只能通过 /settings/secrets 写入保险库"
                    )

        candidate = copy.deepcopy(self._overlay)
        for section, values in patch.items():
            merged = dict(candidate.get(section) or {})
            merged.update(dict(values))
            candidate[section] = merged
        composed = self._compose(candidate)
        problems = cfg_mod.validate(composed, production=self._production)
        if problems:
            raise SettingsInvalid("；".join(problems))

        self._write_atomic(candidate)
        self._overlay, self._config = candidate, composed
        log.info("界面设置已更新：%s", ", ".join(sorted(patch)))
        return SettingsResult(config=composed, applied=sorted(patch),
                              restart_required=sorted(set(patch) & {"server"}))

    def _write_atomic(self, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise SettingsWriteFailed(
                f"保存失败，原配置未改动：{type(exc).__name__}"
            ) from exc

    # ---- 秘密 ----

    def write_secret(self, account_key: str, key: str, value: str) -> None:
        if self._secret_writer is None:
            raise SettingsError("保险库不可用，无法保存秘密", code="vault_unavailable")
        if not str(account_key or "").strip() or not str(key or "").strip():
            raise SettingsInvalid("账号槽位和字段名都不能为空")
        if not str(value or "").strip():
            raise SettingsInvalid("秘密不能为空")
        try:
            self._secret_writer(str(account_key), str(key), str(value))
        except Exception as exc:  # noqa: BLE001 - 保险库错误要转成可展示信息
            raise SettingsError(f"写入保险库失败：{type(exc).__name__}") from exc
