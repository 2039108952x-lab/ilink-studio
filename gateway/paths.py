"""应用资源与可写数据路径；永不依赖调用方的当前工作目录。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    resource_root: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.resource_root / "config.toml"

    @property
    def mailbox_db(self) -> Path:
        return self.data_dir / "mailbox.db"

    @property
    def vault_db(self) -> Path:
        return self.data_dir / "vault.db"

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def app_paths() -> AppPaths:
    """开发模式使用项目 data/；frozen 发布模式使用当前用户的 LOCALAPPDATA。"""
    source_root = Path(__file__).resolve().parent.parent
    frozen = bool(getattr(sys, "frozen", False))
    resource_root = Path(
        os.environ.get("ILINK_RESOURCE_DIR")
        or getattr(sys, "_MEIPASS", source_root)
    ).resolve()
    override = os.environ.get("ILINK_DATA_DIR", "").strip()
    if override:
        data_dir = Path(override).expanduser().resolve()
    elif frozen:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError("发布模式缺少 LOCALAPPDATA，无法确定可写数据目录")
        data_dir = Path(local) / "iLink Studio" / "data"
    else:
        data_dir = source_root / "data"
    return AppPaths(resource_root=resource_root, data_dir=data_dir)


def resolve_data_path(data_dir: Path, configured: str, *, default: str) -> Path:
    """相对工作区/浏览器 profile 均落在本应用数据目录，不读旧项目。

    历史默认值写成 ``data/im-workspace``，而基准已经就是数据目录；
    这里剥掉前导 ``data/``，否则会拼成 ``<数据目录>/data/im-workspace``。
    """
    parts = Path(configured or default).parts
    if len(parts) > 1 and parts[0] == "data":
        parts = parts[1:]
    path = Path(*parts).expanduser()
    if not path.is_absolute():
        path = data_dir / path
    resolved = path.resolve()
    if ".." in parts:
        raise ValueError("数据路径不能包含 ..")
    return resolved
