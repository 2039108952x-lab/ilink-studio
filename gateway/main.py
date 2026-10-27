"""网关入口：把 adapter / brain / router / server 拼起来跑。

用法：
    python -m gateway.main --config config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import config as cfg_mod
from .accounts import AccountStore
from .brain import ReasonixBrain
from .mailbox import Mailbox
from .memory import MemoryStore
from .budget import BudgetGate
from .facts import FactExtractor
from .metrics import Metrics
from .models import Platform
from .paths import app_paths
from .registry import auto_receive_enabled, build_adapters
from .router import Router
from .settings import SettingsStore
from .stickers import StickerStore
from .server import GatewayServer
from .token import ensure_token
from .vault import Vault

log = logging.getLogger("gateway")

__all__ = ["auto_receive_enabled", "build_adapters", "main", "run"]


async def _supervise(platform: Platform, adapter: object) -> None:
    """盯着一个平台跑。它崩了就重启它，别让它把整个网关带走。

    真机上撞过：B站适配器在 DNS 抽风时抛异常，gather 直接冒泡，
    QQ 明明好好的也跟着进程一起死。
    """
    backoff = 1.0
    while True:
        try:
            await adapter.start()  # type: ignore[attr-defined]
            return  # adapter 自己正常结束（比如被 stop）
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 平台各异，统一重启
            log.exception("%s 适配器崩了，%.0f 秒后重启", platform.value, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def run(cfg: cfg_mod.GatewayConfig) -> int:
    paths = app_paths()
    paths.ensure_data_dir()
    # 消息、身份、检查点全在一张 mailbox.db 里；不再有 sessions.json 运行时兜底。
    AccountStore(paths.mailbox_db).ensure_defaults()
    mailbox = Mailbox(paths.mailbox_db, backup_dir=paths.data_dir / "backups")
    _metrics = Metrics()
    settings = SettingsStore(
        cfg, paths.data_dir / "ui-settings.json",
        production=os.environ.get("ILINK_PRODUCTION") == "1",
    )
    cfg = settings.current()
    stickers = StickerStore(mailbox, paths.data_dir / "stickers",
                            max_bytes=cfg.stickers.max_bytes)
    memory = MemoryStore(mailbox, window_turns=cfg.memory.recent_rounds,
                         summary_chars=cfg.memory.summary_max_chars)
    brain = ReasonixBrain(cfg.brain, mailbox, memory=memory, stickers=stickers)

    adapters = build_adapters(cfg) if auto_receive_enabled(cfg) else {}
    if not adapters:
        log.info("首启向导未确认或没有启用平台：只启动控制面，不自动收发")
    vault = Vault(paths.vault_db)
    for adapter in adapters.values():
        adapter.attach_vault(vault)  # type: ignore[attr-defined]
    if not adapters:
        log.warning("没有任何平台被启用，只启动控制面")

    router = Router(
        brain,
        adapters,  # type: ignore[arg-type]
        store=mailbox,
        allow_all=cfg.allow_all,
        allowlist=cfg.allowlist,
        settings=settings,
        metrics=_metrics,
        budget=BudgetGate(_metrics),
        facts=FactExtractor(brain, memory, metrics=_metrics),
    )

    token = ensure_token(cfg.server.token_env)
    server = GatewayServer(
        router,
        host=cfg.server.host,
        port=cfg.server.port,
        token=token,
    )
    server.start()
    await router.start()
    log.info("控制面 token 已就绪（环境变量 / data/gateway-token.txt）")

    tasks: list[asyncio.Task] = []
    for platform, adapter in adapters.items():
        tasks.append(
            asyncio.create_task(_supervise(platform, adapter), name=f"{platform.value}-adapter")
        )
        log.info("平台已启动：%s", platform.value)

    try:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            await asyncio.Event().wait()  # 没平台就挂着等 Ctrl+C
    except asyncio.CancelledError:
        pass
    finally:
        server.stop()
        await router.stop()
        mailbox.close()
        for adapter in adapters.values():
            try:
                await adapter.stop()  # type: ignore[attr-defined]
            except Exception:
                log.exception("停止 adapter 失败")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway", description="iLink 多平台消息网关")
    parser.add_argument("--config", default=None, help="config.toml 路径")
    parser.add_argument("--port", type=int, default=None, help="覆盖控制面端口")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = cfg_mod.load(args.config)
    if args.port is not None:
        cfg.server.port = args.port

    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - 顶层兜底，别让它无声退出
        log.exception("网关异常退出")
        return 1


if __name__ == "__main__":
    sys.exit(main())
