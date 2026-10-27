"""iLink Studio 独立 Python 网关入口。

Electron 仅通过受控 stdin 传递新会话凭据；桌面 API token 不进命令行/URL/日志。
M0 不自动启用任何平台收发，完成首启与账号核对后才启用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import uuid
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from .accounts import AccountStore
from .api import ConsoleAPI
from .brain import ReasonixBrain
from .channels import ChannelError, ChannelManager
from .login import LoginManager, create_providers
from .logs import setup_file_logging
from .mailbox import Mailbox
from .memory import MemoryStore
from .budget import BudgetGate
from .facts import FactExtractor
from .metrics import Metrics
from .paths import app_paths
from .router import Router
from .settings import SettingsStore
from .stickers import StickerStore
from .runtime import bootstrap_token, current_ui_token, issue_ui_token, register_runtime, unregister_runtime
from .server import GatewayServer
from .vault import Vault
from . import config as cfg_mod


def _log_setup(data_dir: Path, verbose: bool, cfg=None) -> Path:
    level = "DEBUG" if verbose else (cfg.logging.level if cfg is not None else "INFO")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    return setup_file_logging(
        data_dir.parent / "logs", level=level,
        max_bytes=cfg.logging.max_bytes if cfg is not None else 2 * 1024 * 1024,
        backups=cfg.logging.backups if cfg is not None else 5,
    )


def _account_switch(channels: ChannelManager, loop: asyncio.AbstractEventLoop):
    """把「HTTP 线程里的启停请求」转交给网关事件循环，并等结果回来。"""

    def on_account_enabled(account_id: int, enabled: bool) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            channels.set_enabled(account_id, enabled), loop)
        try:
            return future.result(timeout=30)
        except FuturesTimeout:
            future.cancel()
            raise ChannelError("timeout", "通道启停超时，请查看本机日志") from None

    return on_account_enabled


async def _restore_channels(channels: ChannelManager, accounts: AccountStore) -> None:
    """重启后只接管界面里仍标记为启用的活动账号；其余一律保持停用。

    恢复失败不改写 enabled：界面继续显示「已启用 + 连接异常」，由人决定重试还是关掉。
    """
    logger = logging.getLogger(__name__)
    for account in accounts.list():
        if not account["enabled"] or not account["is_active_channel"]:
            continue
        try:
            await channels.enable(account["id"])
            logger.info("已恢复通道：%s", account["display_name"] or account["id"])
        except ChannelError as exc:
            accounts.set_connection(account["id"], "error", exc.code)
            logger.warning("通道 %s 未能恢复：%s", account["id"], exc.code)
        except Exception:  # noqa: BLE001 - 单个账号恢复失败不影响其他账号
            accounts.set_connection(account["id"], "error", "restore_failed")
            logger.exception("通道 %s 恢复异常", account["id"])


async def serve(args: argparse.Namespace) -> int:
    paths = app_paths()
    paths.ensure_data_dir()
    base_cfg_for_log = None
    try:
        base_cfg_for_log = cfg_mod.load(paths.config_file)
    except ValueError:
        pass
    log_path = _log_setup(paths.data_dir, args.verbose, base_cfg_for_log)
    accounts = AccountStore(paths.mailbox_db)
    accounts.ensure_defaults()
    # 消息底座：和账号槽位共用 mailbox.db，业务数据只有这一张库。
    mailbox = Mailbox(paths.mailbox_db, backup_dir=paths.data_dir / "backups")
    vault = Vault(paths.vault_db)
    if args.ui_token_stdin:
        ui_token = sys.stdin.readline().strip()
        if len(ui_token.encode()) < 43:
            raise ValueError("Electron 未通过受控通道提供 256 bit 会话凭据")
        vault.put("desktop:session", "ui_token", ui_token)
    elif args.desktop:
        ui_token = issue_ui_token(vault)
    else:
        ui_token = ""
    if ui_token:
        vault.put("desktop:bootstrap", "token", secrets.token_urlsafe(32))
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    # QQ 走 OneBot：没有二维码可扫，把 NapCat 实际监听的地址交给 provider
    qq_ws = ""
    try:
        qq_ws = str((base_cfg_for_log or cfg_mod.load(paths.config_file)).platforms.qq.onebot_ws or "")
    except Exception:  # noqa: BLE001 - 配置读不出来就让 provider 自己探测
        qq_ws = ""
    logins = LoginManager(
        accounts, vault,
        create_providers(lambda name: str(paths.data_dir / name), onebot_ws=qq_ws),
    )
    base_cfg = cfg_mod.load(paths.config_file)
    settings = SettingsStore(
        base_cfg, paths.data_dir / "ui-settings.json",
        production=os.environ.get("ILINK_PRODUCTION") == "1",
        secret_writer=lambda account, key, value: vault.put(account, key, value),
    )
    cfg = settings.current()
    logging.getLogger(__name__).info("桌面网关启动；各账号需登录、核对 UID 后单独启用")
    stickers = StickerStore(mailbox, paths.data_dir / "stickers",
                            max_bytes=cfg.stickers.max_bytes)
    memory = MemoryStore(mailbox, window_turns=cfg.memory.recent_rounds,
                         summary_chars=cfg.memory.summary_max_chars)
    brain = ReasonixBrain(cfg.brain, mailbox, memory=memory, stickers=stickers)
    metrics = Metrics()
    router = Router(
        brain,
        {},  # 通道由 ChannelManager 按账号启停，不在启动时全量创建
        store=mailbox,
        allow_all=cfg.allow_all,
        allowlist=cfg.allowlist,
        settings=settings,
        metrics=metrics,
        budget=BudgetGate(metrics),
        facts=FactExtractor(brain, memory, metrics=metrics),
    )
    channels = ChannelManager(
        accounts, vault, router,
        config=lambda: settings.current(), data_dir=paths.data_dir,
    )

    def on_settings_changed(result) -> None:
        # 单项热重载：记忆窗口、表情大小、日志级别立即生效；其余段启动时读取
        memory.reconfigure(window_turns=result.config.memory.recent_rounds,
                           summary_chars=result.config.memory.summary_max_chars)
        stickers.max_bytes = result.config.stickers.max_bytes
        brain.reconfigure(result.config.brain)
        logging.getLogger().setLevel(result.config.logging.level)
        logging.getLogger(__name__).info("设置已热重载：%s", ", ".join(result.applied))

    def on_account_enabled(account_id: int, enabled: bool) -> dict:
        """HTTP 线程调用；启停协程必须回到网关事件循环里跑。"""
        return _account_switch(channels, loop)(account_id, enabled)

    # 内容雷达：按平台各建一个实例。家目录独立（Cookie / 报告 / 判重表互不干扰）
    radars: dict = {}
    douyin_platform = cfg.platforms.get("douyin")
    if douyin_platform is not None and douyin_platform.enabled:
        try:
            from .douyin_mcp import build_client
            from .radar.douyin import DouyinRadar
            from .radar_service import RadarService

            options = dict(getattr(douyin_platform, "options", {}) or {})
            douyin_home = paths.data_dir / "douyin-home"
            radars["douyin"] = RadarService(
                DouyinRadar(
                    build_client(repo_root=paths.resource_root, home=douyin_home,
                                 resource_root=paths.resource_root),
                    douyin_home,
                ),
                paths.data_dir,
                loop=loop, brain=brain,
                per_day=int(options.get("per_day", 5) or 5),
                notify_channel=str(options.get("notify", "file") or "file"),
                webhook_url=str(options.get("webhook_url", "") or ""),
            )
        except Exception:  # noqa: BLE001 - 雷达起不来不许拖垮整个控制面
            logging.getLogger(__name__).exception("抖音内容雷达初始化失败，第七区会报未启用")

    bili_platform = cfg.platforms.get("bilibili")
    if bili_platform is not None and bili_platform.enabled:
        bili_options = dict(getattr(bili_platform, "options", {}) or {})
        if bili_options.get("radar", True):
            try:
                from .radar import bili_settings
                from .radar import duty as duty_mod
                from .radar.bilibili import DEFAULT_THRESHOLDS, build_radar
                from .radar_service import RadarService

                bili_home = paths.data_dir / "bili-home"
                bili_per_day = int(bili_options.get("per_day", 5) or 5)
                bili_provider = build_radar(
                    repo_root=paths.resource_root,
                    data_dir=paths.data_dir,
                    resource_root=paths.resource_root,
                    python=str(bili_options.get("python", "") or ""),
                    keywords=[str(k) for k in (bili_options.get("keywords") or [])],
                    dm_uid=str(bili_options.get("dm_uid", "") or ""),
                    timeout=float(bili_options.get("mcp_timeout", 120) or 120),
                )

                def _bili_report(result, _home=bili_home, _per_day=bili_per_day):
                    """B 站口径的日报：``### BV号`` + ``**档位**``。

                    一天只有一份 —— 已经存在就**追加**，覆盖了配额就数不准
                    （要么白跑、要么超量）。
                    """
                    quota = duty_mod.quota_state(_home, per_day=_per_day)
                    text = duty_mod.render_round(result, quota=quota)
                    path = duty_mod.report_path(_home)
                    if path.is_file() and path.stat().st_size > 0:
                        duty_mod.append_report(_home, "\n---\n\n" + text)
                        return path
                    return duty_mod.write_report(_home, text)

                radars["bilibili"] = RadarService(
                    bili_provider,
                    bili_home,           # 报告 / 设置 / 配额都落 B 站自己的家目录
                    loop=loop, brain=brain,
                    thresholds=DEFAULT_THRESHOLDS,
                    per_day=bili_per_day,
                    notify_channel=str(bili_options.get("notify", "file") or "file"),
                    webhook_url=str(bili_options.get("webhook_url", "") or ""),
                    platform="bilibili",
                    report_writer=_bili_report,
                    # 配额直接数当天日报里的 ### BV 行 —— 日报是唯一事实来源
                    quota_reader=lambda _home=bili_home, _p=bili_per_day:
                        duty_mod.quota_state(_home, per_day=_p),
                    quota_after_run=lambda _n, _home=bili_home, _p=bili_per_day:
                        duty_mod.quota_state(_home, per_day=_p),
                    # B 站跑的是 Python，查 Node 版本只会误导
                    node_check=lambda: {},
                    videos_reader=lambda **kw: duty_mod.list_videos(bili_home, **kw),
                    # 四页签设置：读 config 默认值 + 覆盖层，改只写覆盖层（config.toml 只读）
                    settings_view=lambda _home=bili_home, _opts=bili_options:
                        bili_settings.view(_home, _opts),
                    settings_save=lambda payload, _home=bili_home, _opts=bili_options:
                        bili_settings.write_override(_home, payload),
                )
            except Exception:  # noqa: BLE001 - 起不来就报未启用，不拖垮控制面
                logging.getLogger(__name__).exception("B 站内容雷达初始化失败，第七区会报未启用")
    api = ConsoleAPI(
        accounts, vault, logins,
        on_shutdown=lambda: loop.call_soon_threadsafe(stop_event.set),
        static_root=paths.resource_root / "web" / "dist",
        mailbox=mailbox, data_dir=paths.data_dir, router=router, stickers=stickers,
        settings=settings, on_settings_changed=on_settings_changed,
        metrics=metrics, log_path=log_path, brain=brain,
        on_account_enabled=on_account_enabled,
        radar=radars,
    )
    server = GatewayServer(
        router, host="127.0.0.1", port=args.port, token="",
        loop=loop, api=api, desktop_mode=bool(ui_token), ui_token=ui_token,
    )
    server._httpd.ui_token_provider = (lambda: current_ui_token(vault)) if ui_token else None
    server._httpd.bootstrap_token_provider = lambda: bootstrap_token(vault)
    server._httpd.rotate_ui_token = lambda token: vault.put("desktop:session", "ui_token", token)
    instance_id = uuid.uuid4().hex
    runtime_file = paths.data_dir / "runtime.json"
    server.start()
    await router.start()
    register_runtime(runtime_file, port=server.address[1], instance_id=instance_id)
    print(json.dumps({
        "type": "ready", "port": server.address[1], "version": "0.1.0-rc1"
    }), flush=True)
    restore_task = asyncio.create_task(
        _restore_channels(channels, accounts), name="restore-channels")
    try:
        await stop_event.wait()
    finally:
        server.stop()
        restore_task.cancel()
        await asyncio.gather(restore_task, return_exceptions=True)
        await channels.stop_all()
        await router.stop()
        logins.shutdown()
        mailbox.close()
        unregister_runtime(runtime_file, instance_id=instance_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="iLink Studio 独立桌面网关")
    parser.add_argument("--desktop", action="store_true", help="启用桌面 API，并生成加密保存的会话凭据")
    parser.add_argument("--ui-token-stdin", action="store_true", help="仅从 stdin 读取 Electron 会话凭据")
    parser.add_argument("--reauth", action="store_true", help="向已有网关申请新 UI 会话，不启动新网关")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.reauth:
        from .session import main as reauth
        return reauth(["--port", str(args.port)])
    if not args.desktop and not args.ui_token_stdin:
        parser.error("桌面入口需要 --desktop，或由 Electron 指定 --ui-token-stdin")
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # 启动失败必须可见；不打印堆栈，凭据也不落日志。
        detail = str(exc).strip().replace("\n", " ")[:400]
        print(json.dumps({"type": "startup_error", "error": type(exc).__name__,
                          "message": detail}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
