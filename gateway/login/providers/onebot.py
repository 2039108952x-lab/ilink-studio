"""NapCat / OneBot 11：登录发生在 NapCat 自己那边，网关只负责核对机器人身份。

原来的实现是直接抛 ``external_login_required`` 就完事，于是 QQ 槽位永远停在
``unbound``；而启用通道要求 ``auth_state == "bound"`` —— 死锁，谁都启用不了。
这里真去问 NapCat 要 QQ 号：连一次 OneBot WS 调 ``get_login_info``，
拿不到再退回它配置文件里登记的那个账号，最终仍由本人核对确认。
"""

from __future__ import annotations

import json
import time

from .base import LoginContext, LoginError, LoginResult

ECHO = "ilink-login-probe"


def fetch_login_info(ws_url: str, timeout: float = 15.0) -> tuple[str, str]:
    """连一次 OneBot WS，问 ``get_login_info``。返回 ``(QQ号, 昵称)``，问不到就空串。"""
    from websockets.sync.client import connect

    with connect(ws_url, open_timeout=timeout, close_timeout=2) as ws:
        ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": ECHO}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.5, deadline - time.monotonic()))
            except TimeoutError:
                break
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict) or frame.get("echo") != ECHO:
                continue
            data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
            return str(data.get("user_id") or ""), str(data.get("nickname") or "")
    return "", ""


class OneBotProvider:
    """不扫码：NapCat 已经登录，这里只做身份核对，然后交给人工确认。"""

    def __init__(self, ws_url: str = "", *, timeout: float = 15.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout

    def run(self, context: LoginContext) -> LoginResult:
        from ...napcat import describe as describe_napcat

        state = describe_napcat()
        ws_url = self.ws_url or str(state.get("onebot_ws") or "")
        if not state.get("onebot_running"):
            raise LoginError(
                "external_login_required",
                str(state.get("message") or "NapCat 没在运行：先启动 NapCat，再在里面扫码登录 QQ"),
            )

        context.update("probing", "正在向 NapCat 核对机器人账号")
        uid, nick, reason = "", "", ""
        try:
            uid, nick = fetch_login_info(ws_url, self.timeout)
        except Exception as exc:  # noqa: BLE001 - 连不上就把原因说清楚，别吞成超时
            reason = type(exc).__name__

        if not uid:
            # WS 问不到就退回 NapCat 配置里登记的账号；仍然要人核对，不自作主张
            candidates = [str(x).strip() for x in (state.get("accounts") or []) if str(x).strip()]
            if len(candidates) == 1:
                uid = candidates[0]
            elif not candidates:
                raise LoginError(
                    "onebot_no_identity",
                    f"NapCat 没返回已登录的 QQ（{reason or '未登录'}）：先在 NapCat 里扫码登录，再回来重试",
                )
            else:
                raise LoginError(
                    "onebot_ambiguous",
                    f"NapCat 里登记了多个账号（{'、'.join(candidates)}）：只留一个登录账号再试",
                )

        context.update(
            "awaiting_confirm",
            f"NapCat 报告当前登录的是 {nick or uid}（QQ {uid}），核对无误后点确认",
            platform_uid=uid,
        )
        if not context.wait_confirmation(self.timeout * 8):
            raise LoginError("cancelled", "QQ 身份核对没有确认，已取消")
        return LoginResult(platform_uid=uid)
