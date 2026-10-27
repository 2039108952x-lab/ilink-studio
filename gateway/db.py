"""SQLite 底座：连接规则、只向前迁移、备份与完整性校验。

设计约束（PRD §7.4）
--------------------
- 每个连接都带上 ``foreign_keys=ON`` / ``busy_timeout=5000``；
- 库使用 WAL，``synchronous=FULL``；
- 迁移只向前；启动时发现库比代码新就拒绝启动，不做「自动降级读数」；
- 迁移前用 SQLite backup API 备份，失败就整条迁移回滚；
- 业务库就是 SQLite。打不开、迁移失败、写失败一律显式报错，
  **不会**悄悄退回 JSON 或另开一个库继续收发（那会掩盖主流程的错误）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .schema import MIGRATIONS, Migration


class DatabaseError(RuntimeError):
    """可安全展示给本机用户的数据库错误；不含正文、token 或堆栈。"""

    code = "database_error"


class SchemaTooNew(DatabaseError):
    code = "schema_too_new"


class SchemaChecksumMismatch(DatabaseError):
    code = "schema_checksum_mismatch"


def connect(path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """按 PRD 规则打开一个连接。调用方负责 close。"""
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def check_checksum(migration: Migration) -> str:
    return hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationState:
    applied: tuple[int, ...]
    pending: tuple[int, ...]
    current: int

    @property
    def target(self) -> int:
        return MIGRATIONS[-1].version


def _known() -> dict[int, Migration]:
    known: dict[int, Migration] = {}
    for migration in MIGRATIONS:
        if migration.version in known:
            raise DatabaseError(f"迁移编号重复：{migration.version}")
        known[migration.version] = migration
    return known


def migration_state(conn: sqlite3.Connection) -> MigrationState:
    known = _known()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        " version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
    )
    rows = conn.execute("SELECT version, checksum FROM schema_migration ORDER BY version").fetchall()
    applied = tuple(int(row[0]) for row in rows)
    for row in rows:
        version, checksum = int(row[0]), str(row[1])
        migration = known.get(version)
        if migration is None:
            raise SchemaTooNew(
                f"数据库 schema 版本 {version} 比当前程序新，请升级程序而不是改库"
            )
        if check_checksum(migration) != checksum:
            raise SchemaChecksumMismatch(
                f"迁移 {version} 的 SQL 与已应用版本不一致；迁移文件不允许事后修改"
            )
    pending = tuple(v for v in sorted(known) if v not in applied)
    current = applied[-1] if applied else 0
    return MigrationState(applied=applied, pending=pending, current=current)


def backup_database(source: str | Path, dest: str | Path) -> Path:
    """用 SQLite backup API 复制一份一致的库（不是文件拷贝，WAL 也安全）。"""
    source, dest = Path(source), Path(dest)
    if not source.is_file():
        raise DatabaseError(f"没有可备份的数据库：{source.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = connect(source)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
        finally:
            out.close()
    finally:
        src.close()
    tmp.replace(dest)
    return dest


def integrity_check(path: str | Path) -> bool:
    conn = connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and str(row[0]) == "ok"
    finally:
        conn.close()


def migrate(path: str | Path, *, backup_dir: str | Path | None = None,
            clock=time.time) -> MigrationState:
    """把库迁移到代码里的最新版本；已在最新则什么都不做。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    had_data = path.is_file() and path.stat().st_size > 0
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        state = migration_state(conn)
        if state.pending and had_data and backup_dir is not None:
            dest = Path(backup_dir) / (
                f"{path.stem}-pre-v{state.pending[0]}-{int(clock() * 1000)}.db"
            )
            backup_database(path, dest)
        known = _known()
        for version in state.pending:
            migration = known[version]
            checksum = check_checksum(migration)
            # executescript 会先提交挂起事务，所以整段迁移自己带 BEGIN/COMMIT，
            # 表结构和版本记录一起落库——中途崩掉不会留下「表建了但没记版本」。
            script = (
                "BEGIN IMMEDIATE;\n"
                f"{migration.sql}\n"
                "INSERT INTO schema_migration(version,checksum,applied_at) "
                f"VALUES({version},'{checksum}',{clock()});\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise DatabaseError(
                    f"迁移 {version}（{migration.name}）失败：{type(exc).__name__}"
                ) from exc
        return migration_state(conn)
    finally:
        conn.close()
