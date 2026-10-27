"""B站专用 Playwright profile 扫码登录；从不代填密码或绕过验证。"""

from __future__ import annotations

import time

from .base import LoginContext, LoginError, LoginResult

WANT = {
    "SESSDATA": "SESSDATA",
    "bili_jct": "bili_jct",
    "DedeUserID": "DedeUserID",
    "buvid3": "buvid3",
}


def grab_cookies(context) -> dict[str, str]:
    found: dict[str, str] = {}
    for cookie in context.cookies():
        if "bilibili.com" not in str(cookie.get("domain", "")):
            continue
        name, value = str(cookie.get("name", "")), str(cookie.get("value", ""))
        if name and value:
            found[name] = value
    return found


class BilibiliProvider:
    def __init__(self, profile_dir: str, *, wait_seconds: float = 600) -> None:
        self.profile_dir = profile_dir
        self.wait_seconds = wait_seconds

    def run(self, context: LoginContext) -> LoginResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LoginError("dependency_missing", "缺少已固定版本的 Playwright Chromium") from exc
        context.update("waiting_scan", "正在打开 B站专用登录窗口；请扫码并自行完成验证")
        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                self.profile_dir, headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 820},
            )
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                try:
                    page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=60000)
                except Exception:  # noqa: BLE001 - 用户可在专用窗口手动导航
                    pass
                deadline = time.monotonic() + self.wait_seconds
                while time.monotonic() < deadline and not context.cancelled():
                    cookies = grab_cookies(browser)
                    if all(name in cookies for name in WANT):
                        page.wait_for_timeout(1500)
                        cookies = grab_cookies(browser)
                        uid = cookies.get("DedeUserID", "")
                        if not uid or not all(cookies.get(name) for name in WANT):
                            continue
                        if context.expected_uid and uid != context.expected_uid:
                            raise LoginError("identity_mismatch", "登录账号不是预期 B站小号，未保存 Cookie")
                        return LoginResult(platform_uid=uid, credentials={
                            key: cookies[key] for key in WANT
                        })
                    page.wait_for_timeout(1000)
            finally:
                browser.close()
        raise LoginError("cancelled", "B站登录已取消或等待超时")
