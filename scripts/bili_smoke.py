"""B站冒烟：确认四个凭据真的能用、私信/评论接口真的通。

不会给你自己发消息，也不会改动账号上任何东西 —— 全是只读请求。
默认 dry 模式只看环境变量在不在，加 --live 才真连 B站。

    python scripts/bili_smoke.py                 # 只检查环境变量
    python scripts/bili_smoke.py --live          # 真连：验登录态 + 拉一次通知
    python scripts/bili_smoke.py --live --video BV1xx411c7mD   # 顺便测看视频

注：Windows 控制台默认 GBK，输出一律用 ASCII 标记，避免 UnicodeEncodeError。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway import doh  # noqa: E402
from gateway.adapters.bilibili import (  # noqa: E402
    CRED_ENV,
    build_credential,
    credential_value,
    missing_credential_env,
    video_brief,
)

HOWTO = """\
凭据有两种放法，任选其一（环境变量优先）：

  A. 推荐：跑一次登录助手，它会自动写好
       python scripts/bili_login.py
     凭据落在 data/bili-cookies.json（data/ 已在 gitignore 里）

  B. 手动塞进用户环境变量（值别贴进任何文件）：
       浏览器打开 https://www.bilibili.com 并登录
       F12 -> Application -> Cookies -> https://www.bilibili.com
       找到 SESSDATA / bili_jct / DedeUserID / buvid3，复制 Value
       setx BILI_SESSDATA    "SESSDATA 的值"
       setx BILI_JCT         "bili_jct 的值"
       setx BILI_DEDEUSERID  "DedeUserID 的数字"
       setx BILI_BUVID3      "buvid3 的值"
     setx 只对之后新开的终端生效，设完重开终端再跑本脚本
"""


NET_HINTS = ("getaddrinfo", "Cannot connect", "Timeout", "timed out", "ConnectError")


async def check_login(cred) -> dict | None:
    """用 get_self_info 验证 SESSDATA 真的有效。返回自己的信息。"""
    from bilibili_api import user as buser

    try:
        info = await buser.get_self_info(cred)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        # 网络不通和凭据失效是两回事，别让人白跑一趟重新扫码
        if any(hint in detail for hint in NET_HINTS):
            print(f"[x] 连不上 B站（网络问题，不是凭据失效）：{detail}")
            print("    api.bilibili.com 解析不了。可选：换 DNS / 开 Clash TUN /")
            print("    配 BILI_PROXY=http://127.0.0.1:7897 再试")
        else:
            print(f"[x] 登录态无效：{detail}")
            print("    SESSDATA 可能过期了，重跑 scripts/bili_login.py")
        return None
    if not info:
        print("[x] 登录态无效：返回空")
        return None
    if info.get("isLogin") is False:
        print("[x] B站说这个 SESSDATA 没登录")
        return None
    name = info.get("uname") or info.get("name") or "?"
    print(f"[ok] 登录态有效：{name} (uid={info.get('mid', '?')})")
    return info


async def check_notices(cred) -> None:
    """拉一次 @ 和评论回复，确认这两个接口没被风控拦。"""
    from bilibili_api import session as bsession

    for name, func in (("at", bsession.get_at), ("replies", bsession.get_replies)):
        try:
            data = await func(cred)
        except Exception as exc:  # noqa: BLE001
            print(f"[x] 拉 {name} 通知失败：{exc}")
            continue
        items = list((data or {}).get("items") or [])
        print(f"[ok] {name} 通知可读，当前 {len(items)} 条")


async def main(live: bool, video: str) -> int:
    # 这台机器系统 DNS 会拒解析 B站 域名，先上 DoH 兜底
    doh.install()

    missing = missing_credential_env()
    if missing:
        print(f"[x] 缺环境变量：{', '.join(missing)}")
        print()
        print(HOWTO)
        return 2
    print(f"[ok] 四个凭据都在：{', '.join(CRED_ENV)}")
    print(f"[ok] 本机 uid（BILI_DEDEUSERID）= {credential_value('BILI_DEDEUSERID')}")

    if not live:
        print()
        print("dry run：只检查了环境变量。加 --live 才会真连 B站验证。")
        return 0

    cred = build_credential()

    print()
    print("--- 1. 验证登录态 ---")
    if await check_login(cred) is None:
        return 1

    print()
    print("--- 2. 拉一次通知接口 ---")
    await check_notices(cred)

    if video:
        print()
        print("--- 3. 看视频 ---")
        brief = await video_brief(video)
        if not brief.get("ok"):
            print(f"[x] {brief.get('error')}")
        else:
            print(f"[ok] {brief.get('title')} / {brief.get('up')}")
            sub = brief.get("subtitle") or ""
            print(f"     字幕 {len(sub)} 字" + (f"：{sub[:60]}..." if sub else f"（{brief.get('subtitle_note')}）"))

    print()
    print("[done] 凭据可用。现在可以起网关：python -m gateway.main")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真连 B站（只读请求，不改账号）")
    ap.add_argument("--video", default="", help="顺便测看视频，传 BV 号 / av 号 / 链接")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.live, args.video)))
