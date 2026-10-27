"""微信 iLink 扫码登录助手。

跑这个脚本，用手机微信扫一下码，凭据就落到 `data/wx-bot.json`
（`data/` 在 gitignore 里，不会进版本库）。之后网关就能收发微信消息了。

    python scripts/wx_login.py

不碰密码：整个流程走官方 `get_bot_qrcode` / `get_qrcode_status`，只认扫码。

状态机的值全部来自官方实现（`@tencent-weixin/openclaw-weixin@2.4.10`）：
    wait                 还没扫
    scaned               扫了，在手机上确认
    need_verifycode      要在终端输手机微信上显示的数字（走两步验证的号才会碰到）
    verify_code_blocked  输错太多次，得换个码重来
    scaned_but_redirect  服务端让你换个机房，跟着换
    expired              二维码过期，自动换一张（最多 3 次）
    binded_redirect      这个 bot 已经绑过了，不会重新发凭据

⚠️ 扫这个码等于「把你这个微信号接成 bot」。想清楚用小号还是主号再扫。
   别人私聊这个微信号，网关才会收到消息。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.adapters import wechat_ilink as ilink  # noqa: E402

TOKEN_FILE = ilink.TOKEN_FILE
QR_FILE = ROOT / "data" / "wx-qrcode.png"

DEFAULT_BOT_TYPE = "3"
QR_LONG_POLL = 40.0        # 服务端也会 hold；客户端超时当 wait 处理
# 服务端给的二维码活得很短（实测一两分钟就 expired），所以默认换很多次，
# 免得人还没来得及扫流程就自己放弃了。
DEFAULT_MAX_REFRESH = 30


def _headers() -> dict[str, str]:
    return ilink._headers("")     # 登录阶段还没有 token


def fetch_qrcode(api_base: str, bot_type: str) -> dict:
    """要一张新二维码。返回 {qrcode, qrcode_img_content}。"""
    with httpx.Client(timeout=ilink.API_TIMEOUT) as client:
        resp = client.post(
            f"{api_base}/ilink/bot/get_bot_qrcode?bot_type={bot_type}",
            json={"local_token_list": []},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()


def poll_status(api_base: str, qrcode: str, verify_code: str = "") -> dict:
    """查一次扫码状态。长轮询，超时按 wait 处理。"""
    endpoint = f"{api_base}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
    if verify_code:
        endpoint += f"&verify_code={verify_code}"
    try:
        with httpx.Client(timeout=QR_LONG_POLL + 5) as client:
            resp = client.get(endpoint, headers=_headers(), timeout=QR_LONG_POLL)
            resp.raise_for_status()
            return resp.json()
    except (httpx.TimeoutException, httpx.HTTPError):
        return {"status": "wait"}


def render_qrcode(url: str) -> Path | None:
    """把二维码链接画成 PNG，手机上不用手打链接。画不出来就只给链接。

    图上顺便打一行生成时间：这个码一两分钟就过期，没时间戳分不清手上那张还新不新。
    """
    try:
        import qrcode
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    try:
        QR_FILE.parent.mkdir(parents=True, exist_ok=True)
        img = qrcode.make(url).convert("RGB")
        width, height = img.size

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        footer = 34
        canvas = Image.new("RGB", (width, height + footer), "white")
        canvas.paste(img, (0, 0))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype("arial.ttf", 18)
        except OSError:
            font = ImageFont.load_default()
        text = f"created {stamp}  expires in ~1 min"
        text_width = draw.textlength(text, font=font)
        draw.text(((width - text_width) / 2, height + 8), text, fill="black", font=font)

        canvas.save(QR_FILE)
        return QR_FILE
    except Exception:  # noqa: BLE001 - 画不出来不影响登录，链接还能用
        return None


def show_qr(api_base: str, bot_type: str) -> dict:
    qr = fetch_qrcode(api_base, bot_type)
    url = qr.get("qrcode_img_content") or ""
    print("\n用手机微信扫这个二维码：")
    path = render_qrcode(url) if url else None
    if path is not None:
        print(f"  图片：{path}")
    if url:
        print(f"  链接：{url}")
    print()
    return qr


def login(
    api_base: str,
    bot_type: str,
    timeout: float,
    max_refresh: int = DEFAULT_MAX_REFRESH,
) -> dict | None:
    qr = show_qr(api_base, bot_type)
    qrcode = qr.get("qrcode")
    if not qrcode:
        print("服务器没给二维码，先别急着重试，看一眼网络。", file=sys.stderr)
        return None

    deadline = time.monotonic() + timeout
    verify_code = ""
    refreshed = 0
    announced_scan = False

    while time.monotonic() < deadline:
        status = poll_status(api_base, qrcode, verify_code)
        state = status.get("status")

        if state == "wait":
            pass
        elif state == "scaned":
            if verify_code:
                verify_code = ""
            if not announced_scan:
                print("扫到了，在手机上点一下确认。")
                announced_scan = True
        elif state == "need_verifycode":
            prompt = "输错了，重新输入：" if verify_code else "输入手机微信上显示的数字："
            verify_code = input(prompt).strip()
            continue
        elif state == "verify_code_blocked":
            print("验证码输错太多次，换一张码。")
            verify_code = ""
            refreshed += 1
            if refreshed > max_refresh:
                print("反复被拒，停。过一会儿再试。", file=sys.stderr)
                return None
            qr = show_qr(api_base, bot_type)
            qrcode = qr.get("qrcode")
            if not qrcode:
                return None
            announced_scan = False
            continue
        elif state == "expired":
            refreshed += 1
            if refreshed > max_refresh:
                print("二维码反复过期，停。检查网络再试。", file=sys.stderr)
                return None
            print("二维码过期了，换一张。")
            qr = show_qr(api_base, bot_type)
            qrcode = qr.get("qrcode")
            if not qrcode:
                return None
            announced_scan = False
            continue
        elif state == "scaned_but_redirect":
            host = status.get("redirect_host")
            if host:
                api_base = f"https://{host}"
                print(f"服务端让换机房，改到 {host}。")
        elif state == "binded_redirect":
            print("这个微信号已经绑过了，没发新凭据。想重来先在微信里解绑。")
            return None
        elif state == "confirmed":
            bot_id = status.get("ilink_bot_id")
            if not bot_id:
                print("确认了但没给 bot id，登录不算成功。", file=sys.stderr)
                return None
            return {
                "bot_token": status.get("bot_token", ""),
                "bot_id": bot_id,
                "base_url": status.get("baseurl") or api_base,
                "user_id": status.get("ilink_user_id", ""),
                "logged_in_at": int(time.time()),
            }

        time.sleep(1.0)

    print("等太久了，超时。", file=sys.stderr)
    return None


def save(cred: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        TOKEN_FILE.chmod(0o600)     # 这个 token 等于「以这个微信号收发消息」的权限
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="微信 iLink 扫码登录")
    parser.add_argument("--api-base", default=ilink.BASE_URL)
    parser.add_argument("--bot-type", default=DEFAULT_BOT_TYPE)
    parser.add_argument("--timeout", type=float, default=480.0)
    parser.add_argument("--max-refresh", type=int, default=DEFAULT_MAX_REFRESH)
    args = parser.parse_args(argv)

    cred = login(args.api_base, args.bot_type, args.timeout, args.max_refresh)
    if cred is None:
        return 1

    save(cred)
    print("\n登录成功，凭据写到：", TOKEN_FILE)
    print(f"  bot_id ：{cred['bot_id']}")
    if cred.get("user_id"):
        print(f"  你的 id：{cred['user_id']}")
        print("\n把上面这个「你的 id」填进 config.toml 的白名单，否则网关不理你：")
        print(f'  [allowlist]')
        print(f'  wechat = ["{cred["user_id"]}"]')
    print("\n下一步：把 config.toml 里 [platforms.wechat] 的 enabled 改成 true，重启网关。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
