"""B 站值班插件的家目录：铺运行脚本、铺章程、找 Python。

为什么必须「铺」而不是原地跑
    ``mcp_server.py`` 里凭证路径写死成 ``Path(__file__).parent / "bili_credential.json"``
    —— 跟着脚本走。原地跑就把凭证写进 ``vendor/`` 了：升级会被覆盖，装成应用后可能只读。
    所以运行时永远跑家目录里那一份。

两条指纹各管一件事（别合并）
    ``.shipped-<文件名>.sha``  我们上次铺出去的是哪一版。家目录那份跟它对不上 ⇒ 被改过。
                               运行脚本被改过**也照样覆盖**，只先备份 ``.bak-<时间戳>``：
                               否则会出现「宿主新、Python 侧旧」，新工具根本不出现（上游 2.1.0
                               就是这么栽的）。备份是为了「改了什么都能自己捞回来」。
    ``charter.sha256``         值班章程（``duty.md``）被改过就**一个字都不动**，新版另存
                               ``duty.md.new`` 让人自己合 —— 那是使用者写的东西，不是我们的代码。

家目录布局（与上游一致，报告/判重/归档都在这里）
    ``out/<日期>.md`` 日报 · ``seen.md`` 判重表 · ``digest/<月份>.md`` 归档
    ``reports/<BV>-<日期>.md`` 单条笔记 · ``comments.md`` 已发评论台账 · ``tmp/`` 转写与临时
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# 运行时脚本：从 vendor 铺到家目录，家目录那份才是真正跑的那份
SCRIPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mcp_server.py", ("vendor", "bilibili-mcp", "mcp_server.py")),
    ("asr.py", ("lib", "asr.py")),
    ("favops.py", ("lib", "favops.py")),
)

# 家目录下按用途分的子目录；ensure_home 会全部建好
SUBDIRS = ("out", "digest", "reports", "tmp")

# 认插件根用的标记文件：两个都在才认，免得认到只有一半的目录
_PLUGIN_MARKERS: tuple[tuple[str, ...], ...] = (
    ("vendor", "bilibili-mcp", "mcp_server.py"),
    ("lib", "asr.py"),
)


def plugin_root(source_root: Path | str) -> Path:
    """真正的插件根，而不是它的上一层。

    上游把能力层嵌在自己的 ``vendor/`` 里，本项目又整个 vendor 进来，
    于是同一份脚本有两种落点：``<root>/vendor/bilibili-mcp`` 和
    ``<root>/vendor/dsh-bili-scroll/vendor/bilibili-mcp``；发布包里还可能是
    ``resources/gateway/_internal`` 往上两级。按标记文件认，认不出就退回原样。
    """
    root = Path(source_root)
    for base in (root, root.parent, root.parent.parent):
        for candidate in (base, base / "vendor" / "dsh-bili-scroll"):
            if all(candidate.joinpath(*parts).is_file() for parts in _PLUGIN_MARKERS):
                return candidate
    return root

# 铺章程时用的账本文件名
CHARTER_LEDGER = "charter.sha256"


def home_dir(data_dir: Path | str) -> Path:
    """家目录：``<数据目录>/bili-home``。不碰 ``~/.dsh``。"""
    return Path(data_dir) / "bili-home"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    """原样写字节。

    不能用 ``write_text`` 写章程：它在 Windows 上把 LF 翻成 CRLF，
    而指纹是对**源文件字节**算的 ⇒ 铺完立刻就对不上，下一轮被误判成「使用者改过」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@dataclass(slots=True)
class ScriptResult:
    """一个运行时脚本的铺装结果。``action`` 给日志和界面用。"""

    name: str
    action: str            # installed | updated | unchanged | missing_source | failed
    path: str = ""
    backup: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "action": self.action, "path": self.path,
                "backup": self.backup, "detail": self.detail}


@dataclass(slots=True)
class CharterResult:
    """值班章程的铺装结果。``action`` 里 ``kept`` 表示「你改过，我没动」。"""

    action: str            # installed | updated | unchanged | kept | missing_source | no_ledger
    path: str = ""
    pending: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"action": self.action, "path": self.path, "pending": self.pending,
                "detail": self.detail}


@dataclass(slots=True)
class HomeState:
    """一次 ``ensure_home`` 的全部结果。界面直接展示，不抛异常。"""

    home: Path
    python: str = ""
    venv: str = ""
    scripts: list[ScriptResult] = field(default_factory=list)
    charter: CharterResult = field(default_factory=lambda: CharterResult("unchanged"))
    error: str = ""

    @property
    def ready(self) -> bool:
        """脚本齐了、Python 找到了，才算能起 MCP 子进程。"""
        return not self.error and all(
            item.action not in ("missing_source", "failed") for item in self.scripts
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "python": self.python,
            "venv": self.venv,
            "ready": self.ready,
            "scripts": [item.to_dict() for item in self.scripts],
            "charter": self.charter.to_dict(),
            "error": self.error,
        }


# ---------------------------------------------------------------- 铺脚本


def install_scripts(home: Path, source_root: Path | str) -> list[ScriptResult]:
    """把 runtime 脚本从 ``source_root``（vendor 里的插件根）铺到 ``home``。

    三种情况（对着上游 2.1.0 的实证结论来的）：
      * 家里没有 ⇒ 直接铺；
      * 家里那份和源一样 ⇒ 什么都不做，只补指纹；
      * 家里那份不一样 ⇒ **备份再覆盖**。这几个是我们发的运行时代码，
        不拦住升级，否则宿主新、Python 侧旧。
    """
    root = plugin_root(source_root)
    results: list[ScriptResult] = []
    for name, parts in SCRIPTS:
        source = root.joinpath(*parts)
        dest = home / name
        incoming = _read(source)
        if incoming is None:
            results.append(ScriptResult(name, "missing_source", str(source),
                                        detail=f"源文件不在：{source}"))
            log.warning("B 站运行时脚本缺失：%s", source)
            continue
        ledger = home / f".shipped-{name}.sha"
        digest = _sha256(incoming)
        try:
            existing = _read(dest)
            if existing is None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
                _write_text(ledger, digest)
                results.append(ScriptResult(name, "installed", str(dest)))
                continue
            if _sha256(existing) == digest:
                if _read(ledger) is None:
                    _write_text(ledger, digest)
                results.append(ScriptResult(name, "unchanged", str(dest)))
                continue
            backup = dest.with_suffix(dest.suffix + f".bak-{_stamp()}")
            try:
                shutil.copyfile(dest, backup)
            except OSError:  # 备份不了也继续，原件还在 vendor 里
                backup = Path("")
            shutil.copyfile(source, dest)
            _write_text(ledger, digest)
            results.append(ScriptResult(name, "updated", str(dest),
                                        backup=str(backup) if backup else "",
                                        detail="家目录那份被改过，已备份后覆盖"))
        except OSError as exc:
            results.append(ScriptResult(name, "failed", str(dest), detail=str(exc)[:200]))
            log.warning("铺 B 站运行时脚本失败：%s：%s", name, exc)
    return results


def install_charter(home: Path, source_file: Path | str) -> CharterResult:
    """铺值班章程。使用者改过就**一个字都不动**，新版另存 ``duty.md.new``。

    分不清「没改只是版本旧」和「改过」时按改过处理 —— 宁可让人手动合一次，
    也不能偷偷吃掉他的改动。这条是上游 2026-10-17 审查里 M6 的结论。
    """
    source = Path(source_file)
    incoming = _read(source)
    if incoming is None:
        return CharterResult("missing_source", detail=f"源文件不在：{source}")
    new_hash = _sha256(incoming)
    dest = home / "duty.md"
    ledger = home / CHARTER_LEDGER
    fresh = home / "duty.md.new"

    existing = _read(dest)
    if existing is None:
        try:
            _write_bytes(dest, incoming)
            _write_text(ledger, new_hash)
        except OSError as exc:
            return CharterResult("failed", detail=str(exc)[:200])
        return CharterResult("installed", str(dest))

    home_hash = _sha256(existing)
    last_hash = (_read(ledger) or b"").decode("utf-8", "replace").strip()

    if home_hash == new_hash:
        # 家里那份就是最新版（他可能只是没动过）
        if last_hash != new_hash:
            try:
                _write_text(ledger, new_hash)
            except OSError:
                pass
        return CharterResult("unchanged", str(dest))

    if not last_hash or home_hash != last_hash:
        # 他改过（或没账本、分不清）⇒ 只另存新版，不动他那一份
        if new_hash != last_hash:
            try:
                if _read(fresh) != incoming:
                    _write_bytes(fresh, incoming)
            except OSError:
                pass
            return CharterResult("kept", str(dest), pending=str(fresh),
                                 detail="家目录那份被改过，新版另存为 duty.md.new")
        return CharterResult("kept", str(dest))

    try:
        _write_bytes(dest, incoming)
        _write_text(ledger, new_hash)
    except OSError as exc:
        return CharterResult("failed", detail=str(exc)[:200])
    return CharterResult("updated", str(dest))


# ---------------------------------------------------------------- Python


def venv_python(home: Path) -> Path:
    """家目录里的 venv 解释器。Windows 走 ``Scripts/python.exe``，其余走 ``bin/python``。"""
    if os.name == "nt":
        return home / ".venv" / "Scripts" / "python.exe"
    return home / ".venv" / "bin" / "python"


def asr_python(home: Path) -> Path:
    """转写专用 venv（``.venv-asr``）。跟主 venv 分开：Whisper 很重，不该拖累主依赖。"""
    if os.name == "nt":
        return home / ".venv-asr" / "Scripts" / "python.exe"
    return home / ".venv-asr" / "bin" / "python"


def resolve_python(home: Path, configured: str = "") -> tuple[str, str]:
    """挑一个能起 MCP server 的 Python。返回 ``(解释器, venv 根或空串)``。

    顺序：配置指定的 → 家里 venv → PATH 上的 python → 跑网关的这个解释器。
    最后一个兜底是有意的：依赖都已在本机装好时（``mcp<2`` 除外）它就能跑，
    没有 venv 也不该直接罢工。
    """
    if configured.strip():
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate), str(candidate.parent.parent)
    for venv in (venv_python(home), asr_python(home)):
        if venv.is_file():
            return str(venv), str(venv.parent.parent)
    found = shutil.which("python") or shutil.which("python.exe")
    if found:
        return found, ""
    return sys.executable, ""


# ---------------------------------------------------------------- 总装


def ensure_home(
    data_dir: Path | str,
    *,
    source_root: Path | str | None = None,
    python: str = "",
    create: bool = True,
) -> HomeState:
    """建目录、铺脚本、铺章程、定 Python。**不抛异常**：失败写进返回值。

    ``source_root`` 指向 vendor 里的插件根（``vendor/dsh-bili-scroll``）。
    传 ``create=False`` 时只看状态不动磁盘（界面轮询走这个）。
    """
    home = home_dir(data_dir)
    state = HomeState(home=home)
    try:
        if create:
            home.mkdir(parents=True, exist_ok=True)
            for name in SUBDIRS:
                (home / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        state.error = f"家目录建不起来：{exc}"
        log.warning("B 站家目录创建失败：%s", exc)
        return state

    if source_root is not None:
        root = Path(source_root)
        if create:
            state.scripts = install_scripts(home, root)
            state.charter = install_charter(home, root / "prompts" / "duty.md")
        else:
            state.scripts = [
                ScriptResult(name, "unchanged", str(home / name)) for name, _ in SCRIPTS
            ]

    state.python, state.venv = resolve_python(home, python)
    return state
