"""仅监听数值 loopback 的 HTTP 控制面。

旧 /send、/video 保留 Bearer 兼容；桌面 /api/v1 使用独立会话凭据。
桌面 origin、Host、Authorization 三层都校验，loopback 本身不构成授权。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .api import APIError, ConsoleAPI
from .models import ChatKind, Platform, SendResult
from .router import Router

log = logging.getLogger(__name__)
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
MAX_BODY = 1 << 20


def _has_listener(host: str, port: int, timeout: float = 0.35) -> bool:
    """端口上是否真有进程在监听。

    TIME_WAIT 的连接探不通，所以正常重启不会被误拦；真正被占用时才返回 True。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "iLinkStudio/0.3"
    protocol_version = "HTTP/1.1"

    router: Router
    token: str
    loop: asyncio.AbstractEventLoop
    api: ConsoleAPI | None = None
    desktop_mode = False
    ui_token = ""
    token_provider = None

    def log_message(self, fmt: str, *args) -> None:
        # 禁止记录 query/header；认证失败也不把秘密写入日志。
        log.debug("本机 HTTP 请求：%s", self.command)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")

    def _json(self, code: int, payload: dict) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # 真机踩过：provider 把 dataclass（CallResult）原样返回，这里抛 TypeError，
            # 连接被掐断，前端只看到 socket hang up，连哪个接口错了都不知道。
            log.error("接口返回了不可序列化的数据：%s", exc)
            code = 500
            body = json.dumps({
                "error": {"code": "not_serializable", "retryable": False,
                          "message": "服务端返回了不可序列化的数据，已记入日志"},
            }, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: APIError) -> None:
        self._json(error.status, {"error": {
            "code": error.code, "message": error.message, "retryable": error.retryable,
        }})

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "")
        return host in self.server.allowed_hosts  # type: ignore[attr-defined]

    def _valid_origin(self) -> bool:
        origin = self.headers.get("Origin")
        expected = self.server.ui_origin  # type: ignore[attr-defined]
        if origin is not None and origin != expected:
            return False
        return self.headers.get("Sec-Fetch-Site", "") not in ("cross-site", "same-site")

    def _read_json(self) -> dict | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _matches(self, supplied: str, expected: str) -> bool:
        return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())

    def _authorized(self, *, desktop: bool = False) -> bool:
        if not self._valid_host() or not self._valid_origin():
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        supplied = header[7:]
        if desktop and self.desktop_mode:
            expected = self.server.ui_token_provider() if self.server.ui_token_provider else self.ui_token
        else:
            expected = self.token
        return self._matches(supplied, expected)

    def _url(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    @staticmethod
    def _credential_in_query(query: dict[str, list[str]]) -> bool:
        forbidden = {"token", "access_token", "authorization", "auth", "bearer"}
        return any(key.lower() in forbidden for key in query)

    def _reject_untrusted(self) -> bool:
        if not self._valid_host():
            self._error(APIError("invalid_host", "Host 必须是控制台数值 loopback 地址", status=403))
            return True
        if not self._valid_origin():
            self._error(APIError("invalid_origin", "拒绝跨源或跨站控制台请求", status=403))
            return True
        return False

    def _api_request(self, method: str) -> None:
        if self._reject_untrusted():
            return
        path, query = self._url()
        if self._credential_in_query(query):
            self._error(APIError("credential_in_url", "凭据不得放在 URL 中", status=400))
            return
        if path == "/api/v1/runtime/session" and method == "POST":
            body = self._read_json()
            supplied = str((body or {}).get("bootstrap_token") or "")
            provider = self.server.bootstrap_token_provider
            expected = provider() if provider else ""
            if not self._matches(supplied, expected):
                self._error(APIError("unauthorized", "UI 重接管握手无效", status=401))
                return
            if self.api is None or self.server.rotate_ui_token is None:
                self._error(APIError("console_unavailable", "桌面控制台尚未启用", status=503))
                return
            token = secrets.token_urlsafe(32)
            self.server.rotate_ui_token(token)
            self._json(200, {"ui_token": token})
            return
        if not self._authorized(desktop=True):
            self._error(APIError("unauthorized", "需要有效的桌面会话凭据", status=401))
            return
        if self.api is None:
            self._error(APIError("console_unavailable", "桌面控制台尚未启用", status=503))
            return
        body: dict | None = None
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            body = self._read_json()
            if body is None:
                self._error(APIError("invalid_json", "需要不超过 1 MiB 的 JSON 对象", status=400))
                return
        try:
            status, payload = self.api.handle(method, path, body, query=query)
        except APIError as exc:
            self._error(exc)
            return
        except Exception as exc:  # noqa: BLE001 - 不向客户端泄露堆栈或协议报文
            log.error("控制台请求失败：%s", type(exc).__name__)
            self._error(APIError("internal_error", "操作未完成，请查看本机脱敏日志", status=500))
            return
        self._json(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._url()
        if path.startswith("/api/"):
            self._api_request("GET")
            return
        if path in ("/health", "/healthz"):
            if not self._valid_host():
                self._error(APIError("invalid_host", "Host 必须是数值 loopback 地址", status=403))
                return
            if path == "/healthz":
                self._json(200, {"ok": True, "version": "0.1.0-rc1"})
            else:
                self._json(200, {"ok": True, "platforms": [p.value for p in self.router.platforms]})
            return
        if self.api is not None and self.api.static_root is not None:
            self._static(path)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _static(self, path: str) -> None:
        if self._reject_untrusted() or self.api is None or self.api.static_root is None:
            return
        root = self.api.static_root.resolve()
        candidate = (root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if candidate != root and root not in candidate.parents:
            self._json(404, {"ok": False, "error": "not found"})
            return
        if candidate.suffix == "":
            candidate = candidate / "index.html"
        if not candidate.is_file():
            self._json(404, {"ok": False, "error": "not found"})
            return
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path, _ = self._url()
        if path.startswith("/api/"):
            self._api_request("POST")
            return
        if path == "/video":
            self._handle_video()
            return
        if path != "/send":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        body = self._read_json()
        if body is None:
            self._json(400, {"ok": False, "error": "invalid json body"})
            return
        try:
            platform = Platform(body["platform"])
            kind = ChatKind(body.get("kind", "private"))
            chat_id, text = str(body["chat_id"]), str(body["text"])
        except (KeyError, ValueError) as exc:
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return
        future = asyncio.run_coroutine_threadsafe(
            self.router.send(platform, kind, chat_id, text), self.loop
        )
        try:
            result: SendResult = future.result(timeout=60)
        except Exception:  # noqa: BLE001
            self._json(502, {"ok": False, "error": "发送未完成，请检查通道状态"})
            return
        self._json(200 if result.ok else 502, {
            "ok": result.ok, "msg_id": result.msg_id, "error": result.error,
            "deprecated": True,
        })

    def do_PUT(self) -> None:  # noqa: N802
        self._api_request("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._api_request("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._api_request("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._error(APIError("cors_denied", "不开放跨源请求", status=403))

    def _handle_video(self) -> None:
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        body = self._read_json()
        if body is None:
            self._json(400, {"ok": False, "error": "invalid json body"})
            return
        try:
            platform = Platform(body["platform"])
        except (KeyError, ValueError) as exc:
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return
        ref = str(body.get("video") or "").strip()
        if not ref:
            self._json(400, {"ok": False, "error": "缺少 video"})
            return
        try:
            limit = int(body.get("max_comments", 20))
        except (TypeError, ValueError):
            limit = 20
        future = asyncio.run_coroutine_threadsafe(
            self.router.video_brief(platform, ref, max_comments=limit), self.loop
        )
        try:
            result = future.result(timeout=150)
        except Exception:  # noqa: BLE001
            self._json(502, {"ok": False, "error": "视频查询未完成，请检查通道状态"})
            return
        self._json(200 if result.get("ok") else 502, result)


class GatewayServer:
    """一个 loopback 端点；绑定后即确定唯一允许的 Host 与 Origin。"""

    def __init__(self, router: Router, *, host: str, port: int, token: str,
                 loop: asyncio.AbstractEventLoop | None = None,
                 api: ConsoleAPI | None = None, desktop_mode: bool = False,
                 ui_token: str = "") -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"只允许绑定 loopback，收到 {host!r}")
        if desktop_mode and len(ui_token.encode()) < 43:
            raise ValueError("桌面会话凭据必须至少 256 bit")
        # Windows 的 SO_REUSEADDR 允许两个进程绑同一个端口，bind 成功不代表没被占。
        # 不先探一次的话，会出现「两个网关都以为自己起来了」，请求随机落到旧那个，
        # 界面就表现为「桌面会话鉴权失败」——查起来极难。
        if port > 0 and _has_listener(host, port):
            raise ValueError(
                f"{host}:{port} 已有进程在监听，未启动第二个网关；"
                "请先退出旧网关（托盘「退出」或结束那个 gateway 进程）"
            )
        self._router, self._token = router, token
        handler = type("_BoundHandler", (_Handler,), {
            "router": router, "token": token, "loop": loop or asyncio.get_event_loop(),
            "api": api, "desktop_mode": desktop_mode, "ui_token": ui_token,
        })
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        actual_host, actual_port = self.address
        hosts = {f"{actual_host}:{actual_port}"}
        if actual_host == "127.0.0.1":
            hosts.add(f"localhost:{actual_port}")
        elif actual_host == "::1":
            hosts.add(f"[::1]:{actual_port}")
        self._httpd.allowed_hosts = hosts
        origin_host = f"[{actual_host}]" if ":" in actual_host else actual_host
        self._httpd.ui_origin = f"http://{origin_host}:{actual_port}"
        self._httpd.ui_token_provider = None
        self._httpd.bootstrap_token_provider = None
        self._httpd.rotate_ui_token = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="gateway-http", daemon=True)
        self._thread.start()
        log.info("控制面已启动 http://%s:%s", *self.address)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
