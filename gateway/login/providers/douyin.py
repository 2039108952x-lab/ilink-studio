"""抖音专用 profile 人工登录；不猜 UID，不自动通过滑块或挑战。"""

from __future__ import annotations

import time

from .base import LoginContext, LoginError, LoginResult


class DouyinProvider:
    def __init__(self, profile_dir: str, *, wait_seconds: float = 600) -> None:
        self.profile_dir = profile_dir
        self.wait_seconds = wait_seconds

    def run(self, context: LoginContext) -> LoginResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LoginError("dependency_missing", "缺少已固定版本的 Playwright Chromium") from exc
        context.update("waiting_scan", "请在抖音专用窗口扫码；遇到滑块请本人完成")
        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                self.profile_dir, headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                try:
                    page.goto("https://www.douyin.com/", wait_until="commit", timeout=30000)
                except Exception:  # noqa: BLE001 - 人工窗口保留，不反复自动导航
                    pass
                context.update("awaiting_confirm", "登录后请核对小号；回到控制台输入 UID 并确认")
                deadline = time.monotonic() + self.wait_seconds
                while time.monotonic() < deadline and not context.cancelled():
                    # 先处理可见人工挑战；即使挑战未消失，也不允许带着未核验身份直接结束登录。
                    try:
                        challenge = page.locator("#captcha_container:visible, .captcha_verify_container:visible")
                        if challenge.count():
                            context.update("needs_verification", "检测到人工验证；请在专用窗口完成，自动动作已暂停", platform_uid=context.platform_uid)
                    except Exception:  # noqa: BLE001 - 页面导航期间允许读不到
                        pass
                    if context.wait_confirmation(1.0):
                        # UID 由本人核对提供，不从昵称猜测；manager 会再次检查预期 UID。
                        return LoginResult(platform_uid=context.platform_uid)
            finally:
                browser.close()
        raise LoginError("cancelled", "抖音登录已取消或等待超时")
