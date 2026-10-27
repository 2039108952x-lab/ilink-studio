"""B站登录助手：开一个专用浏览器 profile，你手动登录，脚本自动抠出四个 cookie。

为什么要这么做
    Edge 正在运行时会独占锁住 Cookies 数据库，别的进程读不到；
    而且 SESSDATA 是 HttpOnly，F12 控制台里的 document.cookie 也拿不到。
    所以开一个自己的 profile，登录后由 Playwright 直接从浏览器上下文里取。

为什么不用你的密码
    密码一旦过自动化流程（输入、日志、截图、网络请求）就有泄露面。
    这里只等你自己在窗口里登录，脚本全程不碰账号密码。

用法
    python scripts/bili_login.py
    python scripts/bili_login.py --expect-uid 10000003   # 顺带核对登录的是不是这个号

产物
    data/bili-cookies.json   四个 cookie 值（data/ 已在 gitignore 里）
    data/bili-browser/       专用浏览器 profile，下次不用重新登录

注：Windows 控制台默认 GBK，输出一律用 ASCII 标记，避免 UnicodeEncodeError。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "bili-browser"
OUT = ROOT / "data" / "bili-cookies.json"

# 环境变量名 -> B站 cookie 名
WANT = {
    "BILI_SESSDATA": "SESSDATA",
    "BILI_JCT": "bili_jct",
    "BILI_DEDEUSERID": "DedeUserID",
    "BILI_BUVID3": "buvid3",
}

LOGIN_URL = "https://www.bilibili.com"
WAIT_SECONDS = 600      # 最多等 10 分钟
POLL_MS = 2000


def grab_cookies(context) -> dict[str, str]:
    """从浏览器上下文里取 bilibili 域下的 cookie 名字->值。"""
    found: dict[str, str] = {}
    for cookie in context.cookies():
        domain = str(cookie.get("domain", ""))
        if "bilibili.com" not in domain:
            continue
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if name and value:
            found[name] = value
    return found


def main(expect_uid: str) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[x] 没装 playwright：python -m pip install playwright && playwright install chromium")
        return 2

    PROFILE.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 820},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 首页没打开（{exc}），窗口里手动访问 bilibili.com 也行")

        print("[i] 浏览器窗口已打开。请在里面登录 B站小号。")
        print("[i] 登录成功后脚本会自动继续，最多等 %d 秒。" % WAIT_SECONDS)

        deadline = time.time() + WAIT_SECONDS
        cookies: dict[str, str] = {}
        while time.time() < deadline:
            try:
                cookies = grab_cookies(context)
            except Exception:  # noqa: BLE001 - 窗口被关掉之类
                cookies = {}
            if all(bili_name in cookies for bili_name in WANT.values()):
                # 等一下让可能的后置 cookie 落全
                page.wait_for_timeout(1500)
                cookies = grab_cookies(context)
                break
            page.wait_for_timeout(POLL_MS)
        else:
            print("[x] 等超时了，没拿到完整的四个 cookie。重跑本脚本再试。")
            context.close()
            return 1

        missing = [b for b in WANT.values() if b not in cookies]
        if missing:
            print(f"[x] 还缺：{', '.join(missing)}")
            context.close()
            return 1

        uid = cookies.get("DedeUserID", "")
        print(f"[ok] 四个 cookie 都拿到了，DedeUserID = {uid}")
        if expect_uid and uid != expect_uid:
            print(f"[!] 注意：登录的号是 {uid}，你期望的是 {expect_uid}")

        OUT.parent.mkdir(parents=True, exist_ok=True)
        payload = {env: cookies[bili] for env, bili in WANT.items()}
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] 已写入 {OUT}")
        print("     文件里是登录凭据，别提交、别外发。")
        for env, bili in WANT.items():
            print(f"     {env:18} <- {bili} ({len(cookies[bili])} 字符)")

        context.close()
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-uid", default="", help="期望登录的 uid，用来核对")
    args = ap.parse_args()
    sys.exit(main(args.expect_uid))
