"""配置加载。

优先级：CLI 参数 > 图形界面覆盖层（data/ui-settings.json）> config.toml > 默认值。
密钥一律走环境变量或保险库，配置文件里只出现变量名。

M4 起多了五段（[ui] [memory] [budget] [stickers] [logging] [paths]），
全部有默认值；``validate()`` 把范围和危险值集中校验，设置 API 和启动共用同一套规则。
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
THEMES = ("system", "light", "dark")
MAX_STICKER_BYTES = 50 * 1024 * 1024


@dataclass(slots=True)
class BrainConfig:
    """回答是怎么产出来的。"""

    backend: str = "cli"                    # cli | http | acp
    command: str = ""                       # 首启配置；空 = AI 未就绪，不用假模型
    model: str = ""                         # 空 = 用 Reasonix 默认模型
    workspace: str = "im-workspace"         # 相对应用数据目录的最小工作区；不是整个项目或凭据目录
    timeout: float = 240.0
    permission_mode: str = "auto"
    max_steps: int = 25
    # 每条消息是否接着同一会话聊（靠 --resume），关掉就是每次都新会话
    keep_session: bool = True
    # 表情包目录（旧字段，保留兼容；新配置用 [stickers] directory）
    sticker_dir: str = ""


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"                 # 红线：只许 loopback
    port: int = 8787
    token_env: str = "ILINK_GATEWAY_TOKEN"  # 控制 API 的 Bearer token 变量名


@dataclass(slots=True)
class PlatformConfig:
    enabled: bool = False
    options: dict = field(default_factory=dict)


@dataclass(slots=True)
class UiConfig:
    theme: str = "system"
    start_minimized: bool = False
    autostart: bool = False
    # 首启向导是否已由本人确认（未确认前不启用自动收发）
    onboarding_confirmed: bool = False


@dataclass(slots=True)
class MemoryConfig:
    recent_rounds: int = 12
    summary_max_chars: int = 800
    facts_enabled: bool = True


@dataclass(slots=True)
class BudgetConfig:
    """每日预算与速率上限；0 = 不限制（仅限开发配置）。"""

    daily_tokens: int = 0
    daily_cost_usd: float = 0.0
    platform_per_minute: int = 30
    queue_limit: int = 50
    price_per_1k_input: float = 0.0
    price_per_1k_output: float = 0.0


@dataclass(slots=True)
class StickerConfig:
    directory: str = ""
    max_bytes: int = 5 * 1024 * 1024
    auto_scan: bool = False


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    max_bytes: int = 2 * 1024 * 1024
    backups: int = 5
    tail_lines: int = 200


@dataclass(slots=True)
class PathsConfig:
    data_dir: str = ""                      # 仅开发模式可覆盖


@dataclass(slots=True)
class PrivacyConfig:
    """正文落库策略。

    ``record_all_bodies`` 默认关：白名单外的人发来的消息只留审计元数据，正文不落库。
    打开后仍然不会自动回复（白名单才是回复的开关），只是把正文保存在本机，
    让收件箱能看见对方到底说了什么。只有 config.toml 能改，界面覆盖层不接管。
    """

    record_all_bodies: bool = False


@dataclass(slots=True)
class GatewayConfig:
    brain: BrainConfig = field(default_factory=BrainConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    stickers: StickerConfig = field(default_factory=StickerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    allow_all: bool = False
    allowlist: dict[str, set[str]] = field(default_factory=dict)

    def token(self) -> str:
        return os.environ.get(self.server.token_env, "")

    def as_dict(self) -> dict:
        """给界面看的配置快照；这里面没有任何秘密。"""
        payload = asdict(self)
        payload["allowlist"] = {name: sorted(ids) for name, ids in self.allowlist.items()}
        return payload


def validate(cfg: GatewayConfig, *, production: bool | None = None) -> list[str]:
    """返回配置问题列表；空列表表示可以启动/保存。"""
    problems: list[str] = []
    try:
        address = ipaddress.ip_address(cfg.server.host)
    except ValueError:
        problems.append("监听地址只能使用数值 loopback，例如 127.0.0.1")
        address = None
    if address is not None and (not address.is_loopback or address.is_unspecified):
        problems.append("监听地址只能使用数值 loopback，例如 127.0.0.1")
    if not 1 <= int(cfg.server.port) <= 65535:
        problems.append("控制面端口必须在 1～65535 之间")

    if cfg.ui.theme not in THEMES:
        problems.append(f"主题只能是 {'/'.join(THEMES)}")
    if str(cfg.logging.level).upper() not in LOG_LEVELS:
        problems.append(f"日志级别只能是 {'/'.join(LOG_LEVELS)}")
    if not 1 <= int(cfg.logging.max_bytes) <= 100 * 1024 * 1024:
        problems.append("日志单文件大小必须在 1 字节～100 MiB 之间")
    if not 0 <= int(cfg.logging.backups) <= 20:
        problems.append("日志备份份数必须在 0～20 之间")
    if not 20 <= int(cfg.logging.tail_lines) <= 5000:
        problems.append("日志尾随行数必须在 20～5000 之间")

    if not 1 <= int(cfg.memory.recent_rounds) <= 50:
        problems.append("记忆窗口轮数必须在 1～50 之间")
    if not 0 <= int(cfg.memory.summary_max_chars) <= 4000:
        problems.append("跨平台摘要长度必须在 0～4000 字之间")

    for name in ("daily_tokens", "daily_cost_usd", "platform_per_minute", "queue_limit",
                 "price_per_1k_input", "price_per_1k_output"):
        if float(getattr(cfg.budget, name)) < 0:
            problems.append(f"[budget].{name} 不能为负数")
    if not 1 <= int(cfg.budget.platform_per_minute) <= 600:
        problems.append("平台每分钟上限必须在 1～600 之间")
    if not 1 <= int(cfg.budget.queue_limit) <= 1000:
        problems.append("队列上限必须在 1～1000 之间")

    if not 1024 <= int(cfg.stickers.max_bytes) <= MAX_STICKER_BYTES:
        problems.append("表情文件大小上限必须在 1 KiB～50 MiB 之间")
    for value in (cfg.stickers.directory, cfg.paths.data_dir):
        if value and (not Path(value).is_absolute() or ".." in Path(value).parts):
            problems.append("自定义路径必须是绝对路径且不能包含 ..")

    is_production = os.environ.get("ILINK_PRODUCTION") == "1" if production is None else production
    if is_production and cfg.allow_all:
        problems.append("生产配置禁止 allow_all，请配置明确的稳定平台 ID 白名单")
    return problems


def _parse(raw: dict) -> GatewayConfig:
    cfg = GatewayConfig()

    if b := raw.get("brain"):
        for k in ("backend", "command", "model", "workspace", "permission_mode", "sticker_dir"):
            if k in b:
                setattr(cfg.brain, k, b[k])
        for k in ("timeout", "max_steps"):
            if k in b:
                setattr(cfg.brain, k, float(b[k]) if k == "timeout" else int(b[k]))
        if "keep_session" in b:
            cfg.brain.keep_session = bool(b["keep_session"])

    if s := raw.get("server"):
        for k in ("host", "port", "token_env"):
            if k in s:
                setattr(cfg.server, k, s[k])

    if u := raw.get("ui"):
        for k in ("theme", "start_minimized", "autostart", "onboarding_confirmed"):
            if k in u:
                setattr(cfg.ui, k, u[k] if k in ("theme",) else bool(u[k]))

    if m := raw.get("memory"):
        for k in ("recent_rounds", "summary_max_chars"):
            if k in m:
                setattr(cfg.memory, k, int(m[k]))
        if "facts_enabled" in m:
            cfg.memory.facts_enabled = bool(m["facts_enabled"])

    if b := raw.get("budget"):
        for k in ("daily_tokens", "platform_per_minute", "queue_limit"):
            if k in b:
                setattr(cfg.budget, k, int(b[k]))
        for k in ("daily_cost_usd", "price_per_1k_input", "price_per_1k_output"):
            if k in b:
                setattr(cfg.budget, k, float(b[k]))

    if s := raw.get("stickers"):
        for k in ("directory",):
            if k in s:
                setattr(cfg.stickers, k, str(s[k]))
        if "max_bytes" in s:
            cfg.stickers.max_bytes = int(s["max_bytes"])
        if "auto_scan" in s:
            cfg.stickers.auto_scan = bool(s["auto_scan"])

    if g := raw.get("logging"):
        for k in ("level",):
            if k in g:
                setattr(cfg.logging, k, str(g[k]).upper())
        for k in ("max_bytes", "backups", "tail_lines"):
            if k in g:
                setattr(cfg.logging, k, int(g[k]))

    if p := raw.get("paths"):
        if "data_dir" in p:
            cfg.paths.data_dir = str(p["data_dir"])

    if p := raw.get("privacy"):
        if "record_all_bodies" in p:
            cfg.privacy.record_all_bodies = bool(p["record_all_bodies"])

    for name, opts in (raw.get("platforms") or {}).items():
        cfg.platforms[name] = PlatformConfig(
            enabled=bool(opts.get("enabled", False)),
            options={k: v for k, v in opts.items() if k != "enabled"},
        )

    if a := raw.get("allowlist"):
        cfg.allow_all = bool(a.get("allow_all", False))
        for name, ids in a.items():
            if name != "allow_all":
                cfg.allowlist[name] = {str(x) for x in ids}

    return cfg


def load(path: str | Path | None = None) -> GatewayConfig:
    """读 config.toml；文件不存在就返回全默认。校验失败直接抛，不带着坏配置启动。"""
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.toml"
    p = Path(path)
    if not p.is_file():
        cfg = GatewayConfig()
    else:
        with p.open("rb") as fh:
            cfg = _parse(tomllib.load(fh))
    problems = validate(cfg)
    if problems:
        raise ValueError("；".join(problems))
    return cfg
