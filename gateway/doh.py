"""DNS 兜底：系统 DNS 解析不了时，改用 DoH 查 IP。

为什么需要
    这台机器的系统 DNS（江西移动 211.141.90.68）对 B站 域名时通时不通，
    实测 `api.bilibili.com` 能连续 5 次 `getaddrinfo failed`。
    抖音更狠：`www.douyin.com` / `douyin.com` / `imapi.douyin.com` 实测全是
    `gaierror`，浏览器能打开纯粹是因为它自己走 DoH。
    而浏览器不受影响是因为它有 DoH；Python 的 `socket.getaddrinfo` 只吃系统 DNS。
    而对外 53 端口是被拦的（公共 DNS 全部 timeout），所以只能走 DoH（443）。

做法
    给进程打一个补丁：只有列在后缀表里的域名才先问 DoH，拿到 IP 再交给原生
    getaddrinfo；其它域名一律原样透传。SoH 端点本身用 IP 直连，不需要 DNS。

为什么改 socket 而不是改 HTTP 客户端
    httpx 用同步 `socket.getaddrinfo`，aiohttp 用
    `loop.getaddrinfo` —— 后者在线程池里调的也是同一个同步函数。
    所以改这一处就同时覆盖两个库。
    证书校验不受影响：TLS 的 SNI 用的是域名，不是解析出来的 IP。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# 默认 DoH：阿里 223.5.5.5 用 IP 直连，不依赖 DNS 本身。
# 实测 1.1.1.1 / 8.8.8.8 在这台机器上不通，所以不用它们。
DEFAULT_DOH = "https://223.5.5.5/resolve"

# 只接管这些后缀。放宽会拖慢所有网络请求，也容易踩到 CDN 分流的坑。
SUFFIXES: tuple[str, ...] = (
    ".bilibili.com",
    ".hdslb.com",
    ".bilicdn1.com",
    ".bilibili.tv",
    # 抖音同病相怜：实测 www.douyin.com / imapi.douyin.com 系统 DNS 全 gaierror，
    # DoH 能查到。图片视频在 douyinpic / douyinstatic / byteimg 上，一并兜住。
    ".douyin.com",
    ".douyinpic.com",
    ".douyinstatic.com",
    ".byteimg.com",
    ".amemv.com",
)

CACHE_TTL = 180.0        # 秒。CDN 会轮换 IP，别缓存太久
LOOKUP_TIMEOUT = 4.0     # 单次 DoH 查询超时
MAX_ADDRS = 4            # 每个域名最多用几个 IP

_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()
_original = socket.getaddrinfo
_installed = False


def _matches(host: str) -> bool:
    host = host.lower()
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in SUFFIXES)


def _doh_lookup(host: str, doh_url: str) -> list[str]:
    """查一次 DoH，返回 IPv4 列表；失败返回空。"""
    try:
        with httpx.Client(timeout=LOOKUP_TIMEOUT) as client:
            resp = client.get(
                doh_url,
                params={"name": host, "type": "A"},
                headers={"accept": "application/dns-json"},
            )
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - DNS 兜底失败不该影响主流程
        log.debug("DoH 查询 %s 失败：%s", host, exc)
        return []

    answers = payload.get("Answer") or []
    ips = [
        str(row.get("data"))
        for row in answers
        if isinstance(row, dict) and row.get("type") == 1
    ]
    return ips[:MAX_ADDRS]


def _resolve(host: str) -> list[str]:
    """带缓存的查询。并发时只让一个线程去查，避免重复请求。"""
    now = time.time()
    with _lock:
        hit = _cache.get(host)
        if hit and hit[0] > now:
            return hit[1]

    ips = _doh_lookup(host, _current_doh())
    if ips:
        with _lock:
            _cache[host] = (now + CACHE_TTL, ips)
    return ips


def _current_doh() -> str:
    import os

    return os.environ.get("ILINK_DOH") or DEFAULT_DOH


def _patched_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    """只对目标域名走 DoH，其它原样透传。失败立刻回退原生逻辑。"""
    if isinstance(host, str) and _matches(host):
        ips = _resolve(host)
        if ips:
            # 解析到具体 IP 后交给原生函数；SNI/证书仍按域名走，不受影响
            try:
                return _original(ips[0], port, *args, **kwargs)
            except OSError:
                pass
    return _original(host, port, *args, **kwargs)


def install() -> bool:
    """幂等安装。返回是否真的装上了。"""
    global _installed
    if _installed:
        return True
    socket.getaddrinfo = _patched_getaddrinfo  # type: ignore[assignment]
    _installed = True
    log.info("DNS 兜底已启用：%s（仅接管 %s）", _current_doh(), ", ".join(SUFFIXES))
    return True


def uninstall() -> None:
    """主要给测试用，恢复原生解析。"""
    global _installed
    socket.getaddrinfo = _original  # type: ignore[assignment]
    _installed = False


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def resolve_via_doh(host: str) -> list[str]:
    """查一次 DoH 拿 IP，不走缓存。

    给「浏览器也要解析」的场景用：Chromium 有自己的解析器，不吃 socket 补丁，
    只能把 IP 通过 `--host-resolver-rules` 喂给它。
    真机上 `douyin.com` 裸域连续多次 gaierror，`www.douyin.com` 时通时不通，
    就是靠这个把页面顶起来的。

    注意：不要把结果当长期缓存 —— CDN 会轮换 IP，每次启动重新查。
    """
    return _doh_lookup(host, _current_doh())
