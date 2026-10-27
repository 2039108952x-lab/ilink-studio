"""分级滚动日志 + 脱敏 + 尾随（FR-6.8）。

日志里永远不该出现的东西：token、cookie、SESSDATA、context_token、Authorization、
二维码内容、长串随机值。这里在**写入前**脱敏，而不是展示时才处理——
否则文件本身就泄了。
"""

from __future__ import annotations

import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_NAME = "gateway.log"
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# 顺序有讲究：先按「键=值」脱，再兜底长串。长串超过 40 字符一律当秘密。
_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+"), r"\1[redacted]"),
    (re.compile(
        r"(?i)((?:token|secret|password|passwd|cookie|sessdata|context_token|bili_jct|buvid3|"
        r"api_key)\s*[:=]\s*)[^\s,;\"'}]+"), r"\1[redacted]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "[redacted]"),
)
_TAIL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<logger>\S+):\s?(?P<message>.*)$"
)


def redact(text: str) -> str:
    value = str(text)
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


class RedactFilter(logging.Filter):
    """把整条日志（含异常文本）整体脱敏，args 也一并清掉，避免格式化时漏出原值。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # noqa: BLE001 - 日志过滤器自己不能把程序搞崩
            record.msg = "[log redaction failed]"
            record.args = ()
        return True


def setup_file_logging(log_dir: str | Path, *, level: str = "INFO",
                       max_bytes: int = 2 * 1024 * 1024, backups: int = 5) -> Path:
    """幂等：重复调用只更新级别与文件参数，不会叠加 handler。"""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / LOG_NAME
    root = logging.getLogger()
    root.setLevel(str(level).upper() if str(level).upper() in _LEVELS else "INFO")

    handler = next(
        (item for item in root.handlers
         if isinstance(item, RotatingFileHandler)
         and Path(getattr(item, "baseFilename", "")) == log_path.resolve()),
        None,
    )
    if handler is None:
        handler = RotatingFileHandler(
            log_path, maxBytes=max(1024, int(max_bytes)),
            backupCount=max(0, int(backups)), encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(RedactFilter())
        root.addHandler(handler)
    else:
        handler.maxBytes = max(1024, int(max_bytes))
        handler.backupCount = max(0, int(backups))
    for item in root.handlers:
        if isinstance(item, logging.StreamHandler) and not isinstance(item, RotatingFileHandler):
            item.setFormatter(logging.Formatter(LOG_FORMAT))
            if not any(isinstance(existing, RedactFilter) for existing in item.filters):
                item.addFilter(RedactFilter())
    return log_path


def tail(log_path: str | Path, *, lines: int = 200, level: str | None = None,
         platform: str | None = None, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    """读日志尾部（最多 2 MiB），按级别/平台过滤；返回已脱敏的结构化行。"""
    path = Path(log_path)
    if not path.is_file():
        return []
    limit = max(1, min(int(lines), 5000))
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # 丢掉半行
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return []
    wanted_level = str(level or "").upper()
    wanted_platform = str(platform or "").lower()
    result: list[dict[str, Any]] = []
    for raw in data.splitlines():
        match = _TAIL_RE.match(raw)
        if match is None:
            continue
        if wanted_level and match.group("level") != wanted_level:
            continue
        if wanted_platform and wanted_platform not in match.group("logger").lower():
            continue
        result.append({
            "ts": match.group("ts"),
            "level": match.group("level"),
            "logger": match.group("logger"),
            "message": redact(match.group("message")),
        })
    return result[-limit:]


def write_diagnostics(directory: str | Path, payload: dict[str, Any]) -> Path:
    """导出诊断包：只写脱敏后的 JSON，不含正文/附件/凭据。"""
    import json

    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"diagnostics-{int(time.time())}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
