"""Windows CurrentUser DPAPI 凭据保险库。

保险库只持久化密文。没有 DPAPI 或解密失败时显式报错，绝不退回明文文件。
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Protocol


class CredentialError(RuntimeError):
    """可安全展示给本机用户的凭据错误；不包含秘密值。"""


class CredentialUnavailable(CredentialError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class CryptoBackend(Protocol):
    name: str

    def seal(self, value: bytes) -> bytes: ...

    def unseal(self, value: bytes) -> bytes: ...


class DPAPIBackend:
    """不携带 LOCAL_MACHINE，不允许系统弹密码 UI。"""

    name = "dpapi-current-user"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialUnavailable("当前系统不支持 Windows DPAPI，请重新配置凭据后端")
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _input(value: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(value, len(value))
        return _Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf

    def _convert(self, value: bytes, *, decrypt: bool) -> bytes:
        source, source_buf = self._input(value)
        target = _Blob()
        optional_entropy = None
        fn = self._crypt32.CryptUnprotectData if decrypt else self._crypt32.CryptProtectData
        args = (ctypes.byref(source), None, optional_entropy, None, None,
                self._CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
        ctypes.set_last_error(0)
        ok = fn(*args)
        if not ok:
            code = ctypes.get_last_error()
            action = "解密" if decrypt else "加密"
            raise CredentialUnavailable(f"Windows DPAPI {action}失败（系统错误 {code}），请重新登录")
        try:
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            if target.pbData:
                self._kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))
            del source_buf

    def seal(self, value: bytes) -> bytes:
        return self._convert(value, decrypt=False)

    def unseal(self, value: bytes) -> bytes:
        if not value:
            raise CredentialUnavailable("凭据密文为空，请重新登录")
        return self._convert(value, decrypt=True)


class Vault:
    """每项凭据按 account_key + key 独立保存，避免双微信通道相互覆盖。"""

    def __init__(self, path: str | Path, backend: CryptoBackend | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backend = backend or DPAPIBackend()
        if self.backend.name not in ("dpapi-current-user", "windows-keyring"):
            raise CredentialError(f"不允许的凭据加密后端：{self.backend.name}")
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS credential (
                  account_key TEXT NOT NULL,
                  key TEXT NOT NULL,
                  crypto_backend TEXT NOT NULL,
                  crypto_version INTEGER NOT NULL,
                  blob BLOB NOT NULL,
                  updated_at REAL NOT NULL,
                  PRIMARY KEY(account_key, key)
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _validate(account_key: str, key: str) -> None:
        if not account_key.strip() or not key.strip():
            raise ValueError("account_key 和 key 不能为空")
        if "\x00" in account_key or "\x00" in key:
            raise ValueError("凭据键含非法字符")

    def put(self, account_key: str, key: str, value: str) -> None:
        self._validate(account_key, key)
        if not value:
            raise ValueError("拒绝保存空凭据")
        blob = self.backend.seal(value.encode("utf-8"))
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO credential
                   (account_key,key,crypto_backend,crypto_version,blob,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(account_key,key) DO UPDATE SET
                     crypto_backend=excluded.crypto_backend,
                     crypto_version=excluded.crypto_version,
                     blob=excluded.blob, updated_at=excluded.updated_at""",
                (account_key, key, self.backend.name, 1, blob, time.time()),
            )

    def put_many(self, account_key: str, values: dict[str, str]) -> None:
        """先全部加密，再在一个事务内保存；不写半成品凭据集合。"""
        if not values:
            raise ValueError("没有可保存的凭据")
        encrypted = []
        for key, value in values.items():
            self._validate(account_key, key)
            if not value:
                raise ValueError(f"凭据 {key} 为空")
            encrypted.append((key, self.backend.seal(value.encode("utf-8"))))
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO credential
                   (account_key,key,crypto_backend,crypto_version,blob,updated_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(account_key,key) DO UPDATE SET
                     crypto_backend=excluded.crypto_backend,
                     crypto_version=excluded.crypto_version,
                     blob=excluded.blob, updated_at=excluded.updated_at""",
                [(account_key, key, self.backend.name, 1, blob, time.time())
                 for key, blob in encrypted],
            )

    def get(self, account_key: str, key: str) -> str:
        self._validate(account_key, key)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT crypto_backend,blob FROM credential WHERE account_key=? AND key=?",
                (account_key, key),
            ).fetchone()
        if row is None:
            raise CredentialUnavailable(f"尚未配置 {account_key} 的 {key}")
        if row[0] != self.backend.name:
            raise CredentialUnavailable("凭据后端与当前配置不一致，请重新登录")
        try:
            return self.backend.unseal(bytes(row[1])).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialUnavailable("凭据内容损坏，请重新登录") from exc

    def configured(self, account_key: str, key: str | None = None) -> bool:
        with self._connect() as conn:
            if key is None:
                row = conn.execute(
                    "SELECT 1 FROM credential WHERE account_key=? LIMIT 1", (account_key,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM credential WHERE account_key=? AND key=?",
                    (account_key, key),
                ).fetchone()
        return row is not None

    def configured_keys(self, *, include_system: bool = False) -> set[str]:
        """只返回逻辑槽位，不返回字段值或密文。桌面会话密钥默认隐藏。"""
        with self._connect() as conn:
            keys = {row[0] for row in conn.execute("SELECT DISTINCT account_key FROM credential")}
            return keys if include_system else {key for key in keys if not key.startswith("desktop:")}

    def delete(self, account_key: str) -> int:
        """仅由用户明确的注销操作调用，不与消息删除混用。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM credential WHERE account_key=?", (account_key,))
            return cursor.rowcount
