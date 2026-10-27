"""抖音联调助手：经 MCP 后端做一次只读冒烟。

以前的版本用 Playwright 直接读 DOM；RPA 通道已退役（见 ``docs/ACCEPTANCE.md``），
现在所有能力都走 ``dsh-douyin-scroll`` 的 MCP server。这个脚本只做只读动作，
不发任何消息 —— 要发东西请走界面第七区的「分享给…」（带预览与确认）。

用法
    python scripts/douyin_smoke.py                  # 登录态 + 互动通知
    python scripts/douyin_smoke.py --to "某人"       # 再读一个人的私信
    python scripts/douyin_smoke.py --round          # 再拉一轮候选（不判断、不发送）

前提：``DOUYIN_HOME`` 下有 Cookie（默认 ``<数据目录>/douyin-home``）。
没登录先扫码：
    node vendor/dsh-douyin-scroll/tools/qr-login.mjs start
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001 - 老控制台不支持就算了
    pass

from gateway.douyin_mcp import build_client  # noqa: E402
from gateway.paths import app_paths  # noqa: E402
from gateway.radar.douyin import DouyinRadar  # noqa: E402


def resolve_home(explicit: str) -> Path:
    """找抖音家目录。

    开发态 ``app_paths().data_dir`` 是项目内 ``data/``，而登录态通常在发布版的
    ``%LOCALAPPDATA%\\iLink Studio\\data`` —— 两边都有可能是对的那一份，
    所以按「谁真有 Cookie 就用谁」来挑，别让人对着一个空目录猜为什么没登录。
    """
    if explicit:
        return Path(explicit)
    candidates = [app_paths().data_dir / "douyin-home"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "iLink Studio" / "data" / "douyin-home")
    for path in candidates:
        if (path / "douyin_cookie.txt").is_file():
            return path
    return candidates[0]


async def run(args: argparse.Namespace) -> int:
    home = resolve_home(args.home)
    print(f"家目录：{home}")
    radar = DouyinRadar(build_client(repo_root=ROOT, home=home), home)
    try:
        who = await radar.whoami()
        print("whoami:", json.dumps(
            {k: who.get(k) for k in ("logged_in", "nick", "uid")}, ensure_ascii=False))
        if not who.get("logged_in"):
            print("没登录：先跑 node vendor/dsh-douyin-scroll/tools/qr-login.mjs start")
            return 1

        notices = await radar.notices()
        print("通知条数:", json.dumps(notices.get("counts") or {}, ensure_ascii=False))
        ats = notices.get("items") or []
        if ats:
            print("最近一条通知:", json.dumps(
                {k: ats[0].get(k) for k in ("type_name", "nickname", "content", "aweme_id")},
                ensure_ascii=False)[:300])

        if args.to:
            dm = await radar.im_read(to=args.to, limit=args.limit)
            messages = dm.get("messages") or []
            print(f"私信「{args.to}」最近 {len(messages)} 条：")
            for msg in messages[-5:]:
                print(f"  [{msg.get('from')}] {str(msg.get('text') or '')[:60]}")

        if args.round:
            items = await radar.candidates(top=args.top)
            print(f"候选 {len(items)} 条：")
            for item in items:
                print(f"  {item.item_id}  {item.author or '?'}  {item.title[:40]}")
        return 0
    except Exception as exc:  # noqa: BLE001 - 联调脚本要把原因打全
        print(f"失败：{type(exc).__name__}: {exc}")
        return 2
    finally:
        await radar.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="douyin_smoke", description="抖音 MCP 只读冒烟")
    parser.add_argument("--to", default="", help="要读的私信会话昵称或 sec_uid")
    parser.add_argument("--limit", type=int, default=10, help="读多少条私信（默认 10）")
    parser.add_argument("--round", action="store_true", help="再拉一轮候选（只拉不判断）")
    parser.add_argument("--top", type=int, default=6, help="候选条数（默认 6）")
    parser.add_argument("--home", default="", help="覆盖 DOUYIN_HOME")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
