"""微信 iLink 扫码 provider；二维码仅交给内存中的登录会话。"""

from __future__ import annotations

import io
import time
from typing import Any

import httpx

from ...adapters import wechat_ilink as ilink
from .base import LoginContext, LoginError, LoginResult

BOT_TYPE = "3"
POLL_SECONDS = 40.0


def fetch_qrcode(api_base: str, bot_type: str = BOT_TYPE) -> dict[str, Any]:
    with httpx.Client(timeout=ilink.API_TIMEOUT) as client:
        response = client.post(
            f"{api_base}/ilink/bot/get_bot_qrcode?bot_type={bot_type}",
            json={"local_token_list": []}, headers=ilink._headers(""),
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not payload.get("qrcode"):
        raise LoginError("qrcode_unavailable", "平台未返回可用二维码，请稍后重试")
    return payload


def poll_status(api_base: str, qrcode: str, verify_code: str = "") -> dict[str, Any]:
    endpoint = f"{api_base}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
    if verify_code:
        endpoint += f"&verify_code={verify_code}"
    try:
        with httpx.Client(timeout=POLL_SECONDS + 5) as client:
            response = client.get(endpoint, headers=ilink._headers(""), timeout=POLL_SECONDS)
            response.raise_for_status()
            payload = response.json()
    except (httpx.TimeoutException, httpx.HTTPError):
        return {"status": "wait"}
    return payload if isinstance(payload, dict) else {"status": "wait"}


def qrcode_png(content: str) -> bytes:
    try:
        import qrcode
    except ImportError as exc:
        raise LoginError("qrcode_renderer_missing", "缺少 qrcode 依赖，无法生成软件内二维码") from exc
    image = qrcode.make(content)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class WeChatProvider:
    def __init__(self, api_base: str = ilink.BASE_URL, *, max_seconds: float = 480,
                 max_refresh: int = 30) -> None:
        self.api_base = api_base
        self.max_seconds = max_seconds
        self.max_refresh = max_refresh

    def run(self, context: LoginContext) -> LoginResult:
        api_base = self.api_base
        deadline = time.monotonic() + self.max_seconds
        verify_code = ""
        refreshed = 0
        qr = fetch_qrcode(api_base)
        while time.monotonic() < deadline and not context.cancelled():
            token = str(qr.get("qrcode") or "")
            image = str(qr.get("qrcode_img_content") or "")
            if not token or not image:
                raise LoginError("qrcode_unavailable", "平台未返回完整二维码")
            context.update("waiting_scan", "请使用要作为机器人的微信小号扫码", qr_png=qrcode_png(image))
            while time.monotonic() < deadline and not context.cancelled():
                status = poll_status(api_base, token, verify_code)
                state = str(status.get("status") or "wait")
                if state == "wait":
                    pass
                elif state == "scaned":
                    verify_code = ""
                    context.update("awaiting_confirm", "已扫码，请在手机上确认")
                elif state == "need_verifycode":
                    verify_code = context.verification_code()
                    if not verify_code:
                        context.update("needs_verification", "请输入手机微信显示的数字验证值")
                        time.sleep(0.2)
                elif state == "verify_code_blocked":
                    verify_code = ""
                    break
                elif state == "expired":
                    break
                elif state == "scaned_but_redirect":
                    host = str(status.get("redirect_host") or "")
                    if host and all(c.isalnum() or c in ".-" for c in host):
                        api_base = f"https://{host}"
                elif state == "binded_redirect":
                    raise LoginError("already_bound", "此微信已绑定；请先按平台流程解绑再重试")
                elif state == "confirmed":
                    uid = str(status.get("ilink_bot_id") or "")
                    bot_token = str(status.get("bot_token") or "")
                    if not uid or not bot_token:
                        raise LoginError("incomplete_login", "平台确认登录但未返回完整身份或凭据")
                    if context.expected_uid and uid != context.expected_uid:
                        raise LoginError("identity_mismatch", "登录账号与预期 UID 不符，未保存凭据")
                    return LoginResult(
                        platform_uid=uid,
                        credentials={
                            "bot_token": bot_token,
                            "base_url": str(status.get("baseurl") or api_base),
                            "user_id": str(status.get("ilink_user_id") or ""),
                        },
                    )
                time.sleep(0.2)
            if context.cancelled():
                break
            refreshed += 1
            if refreshed > self.max_refresh:
                raise LoginError("qrcode_expired", "二维码多次过期，请稍后重新发起登录")
            verify_code = ""
            qr = fetch_qrcode(api_base)
        raise LoginError("cancelled", "登录已取消或等待超时")
