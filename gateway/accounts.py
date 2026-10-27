"""账号槽位与三种独立健康状态。

account 是 platform + channel 接入槽位，不是现实人物。此处不猜联系人身份。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_ACCOUNTS = (
    ("qq", "onebot", "QQ · NapCat / OneBot 11", ""),
    ("wechat", "official", "微信 · 官方 iLink", ""),
    ("wechat", "wxauto", "微信 · wxauto（仅 3.9.x）", ""),
    ("bilibili", "cookie", "B站 · 专用浏览器 Cookie", ""),
    ("douyin", "rpa", "抖音 · 专用浏览器 RPA", ""),
)
# 0.1.0-rc1 之前把开发者的 QQ / B站 UID 写死成预期值，别人的号探活必然 409。
# 这些值只在「从未绑定成功」的槽位上清空；已绑定的槽位保持原样。
# 公开版不再内置任何真实账号：如果是从旧版升级上来、库里还留着当时写死的号，
# 临时把那些号填进这个元组再跑一次 init 即可清掉。
LEGACY_DEFAULT_UIDS: tuple[str, ...] = ()
AUTH_STATES = frozenset({
    "unbound", "waiting_scan", "awaiting_confirm", "completing", "bound",
    "needs_verification", "expired", "revoked", "failed", "cancelled",
})
CONNECTION_STATES = frozenset({"stopped", "starting", "online", "reconnecting", "degraded",
                               "error", "needs_human"})
SEND_STATES = frozenset({"unknown", "success", "failed", "unsupported"})


class AccountError(RuntimeError):
    code = "account_error"


class IdentityMismatch(AccountError):
    code = "identity_mismatch"


class AccountStore:
    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._write_lock = threading.RLock()
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS account (
                  id INTEGER PRIMARY KEY,
                  platform TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  platform_uid TEXT NOT NULL DEFAULT '',
                  display_name TEXT,
                  auth_state TEXT NOT NULL DEFAULT 'unbound',
                  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
                  expected_uid TEXT NOT NULL DEFAULT '',
                  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                  connection_state TEXT NOT NULL DEFAULT 'stopped',
                  send_health TEXT NOT NULL DEFAULT 'unknown',
                  last_auth_ok_at REAL,
                  last_probe_at REAL,
                  last_send_at REAL,
                  last_error_code TEXT,
                  last_send_error_safe TEXT,
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL,
                  UNIQUE(platform, channel)
                );
                CREATE TABLE IF NOT EXISTS app_setting (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                """
            )

    def ensure_defaults(self) -> None:
        now = self.clock()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for platform, channel, name, expected_uid in DEFAULT_ACCOUNTS:
                conn.execute(
                    """INSERT INTO account
                       (platform,channel,display_name,expected_uid,active,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(platform,channel) DO NOTHING""",
                    (platform, channel, name, expected_uid,
                     0 if channel == "wxauto" else 1, now, now),
                )
            conn.execute(
                "INSERT OR IGNORE INTO app_setting(key,value) VALUES ('wechat_active_channel','official')"
            )
            for legacy in LEGACY_DEFAULT_UIDS:
                conn.execute(
                    "UPDATE account SET expected_uid='', updated_at=? "
                    "WHERE expected_uid=? AND platform_uid='' AND auth_state<>'bound'",
                    (now, legacy),
                )

    def set_expected_uid(self, account_id: int, expected_uid: str) -> None:
        """设定「预期机器人 UID」；留空表示首次登录/探活以实际 UID 为准。"""
        value = str(expected_uid or "").strip()
        if len(value) > 128:
            raise ValueError("预期 UID 过长")
        with self._write_lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM account WHERE id=?", (account_id,)).fetchone()
            if row is None:
                raise AccountError(f"账号槽位不存在：{account_id}")
            conn.execute("UPDATE account SET expected_uid=?, updated_at=? WHERE id=?",
                         (value, self.clock(), account_id))

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM account ORDER BY id").fetchall()
            active = self._wechat_channel(conn)
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["active"] = bool(item["active"])
            item["is_active_channel"] = (
                item["channel"] == active if item["platform"] == "wechat" else item["active"]
            )
            result.append(item)
        return result

    def get(self, account_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise AccountError(f"账号槽位不存在：{account_id}")
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["active"] = bool(item["active"])
        return item

    def by_key(self, platform: str, channel: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account WHERE platform=? AND channel=?", (platform, channel)
            ).fetchone()
        if row is None:
            raise AccountError(f"账号槽位不存在：{platform}:{channel}")
        return dict(row)

    @staticmethod
    def _wechat_channel(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT value FROM app_setting WHERE key='wechat_active_channel'"
        ).fetchone()
        return str(row[0]) if row else "official"

    @property
    def wechat_channel(self) -> str:
        with self._connect() as conn:
            return self._wechat_channel(conn)

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_setting WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        if key == "wechat_active_channel":
            raise ValueError("微信通道请使用原子切换接口")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO app_setting(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
            )

    def switch_wechat(self, channel: str) -> None:
        """调用方必须先校验前置条件并停止旧通道，再原子切换命名空间。"""
        if channel not in ("official", "wxauto"):
            raise AccountError("未知微信通道")
        now = self.clock()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE app_setting SET value=? WHERE key='wechat_active_channel'", (channel,)
            )
            conn.execute(
                "UPDATE account SET active=?,updated_at=? WHERE platform='wechat' AND channel=?",
                (1, now, channel),
            )
            conn.execute(
                "UPDATE account SET active=0,enabled=0,connection_state='stopped',updated_at=? "
                "WHERE platform='wechat' AND channel<>?", (now, channel),
            )

    def set_auth(self, account_id: int, state: str, *, platform_uid: str = "") -> None:
        if state not in AUTH_STATES:
            raise ValueError(f"未知凭据状态：{state}")
        now = self.clock()
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT expected_uid FROM account WHERE id=?", (account_id,)
            ).fetchone()
            if row is None:
                raise AccountError(f"账号槽位不存在：{account_id}")
            if state == "bound" and row[0] and platform_uid != row[0]:
                raise IdentityMismatch("登录账号与预期 UID 不符，已阻止启用自动回复")
            conn.execute(
                """UPDATE account SET auth_state=?,platform_uid=?,last_auth_ok_at=?,
                   last_error_code=NULL,updated_at=? WHERE id=?""",
                (state, platform_uid, now if state == "bound" else None, now, account_id),
            )

    def set_enabled(self, account_id: int, enabled: bool) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE account SET enabled=?,updated_at=? WHERE id=?",
                         (int(enabled), self.clock(), account_id))

    def set_connection(self, account_id: int, state: str, error_code: str | None = None) -> None:
        if state not in CONNECTION_STATES:
            raise ValueError(f"未知连接状态：{state}")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE account SET connection_state=?,last_probe_at=?,last_error_code=?,updated_at=? WHERE id=?",
                (state, self.clock(), error_code, self.clock(), account_id),
            )

    def set_send_health(self, account_id: int, state: str, safe_error: str = "") -> None:
        if state not in SEND_STATES:
            raise ValueError(f"未知发送健康状态：{state}")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """UPDATE account SET send_health=?,last_send_at=?,last_send_error_safe=?,
                   updated_at=? WHERE id=?""",
                (state, self.clock(), safe_error[:500], self.clock(), account_id),
            )
