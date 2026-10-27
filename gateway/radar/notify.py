"""通知：toast / file / webhook / both。

各通道怎么落地（这台机器上的实情）
    file     写 ``<data_dir>/notifications/<时间戳>-<kind>.json``。界面和桌面端都读这里。
    toast    同样进上面那个队列，但 ``deliver`` 标成 ``toast``；由 Electron 侧轮询后
             弹系统通知（Python 进程没有权限直接弹 Windows 通知，硬做要装模块或提权）。
    webhook  httpx POST 一份 JSON。URL 里的 token 一律脱敏后再进日志和返回值。
    both     file + toast 都来一遍。

不变的约定
    通知失败不许把刷一轮本身搞挂 —— 日报已经写盘了，通知只是顺带。
    所有外发内容里的 URL 都要先脱敏（``mask_url``），别把带签名的分享链或 token 抖出去。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

CHANNELS = ("toast", "file", "webhook", "both")
_SENSITIVE_KEYS = ("token", "key", "secret", "sign", "password",
                   "u_code", "did", "iid", "mid")
# 路径里像 token 的段：飞书 hook/<32位>、企业微信 hook?key=、各种 /v2/hook/<hex>
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_-]{16,}")


def _mask_path(path: str) -> str:
    """把路径里的长 token 段打成 ***。

    不少 webhook 把凭据直接放路径（`/open-apis/bot/v2/hook/<token>`），
    只扫 query 会漏掉这一半。
    """
    if not path:
        return path
    return _TOKENISH_RE.sub("***", path)


def mask_url(url: str) -> str:
    """把 URL 里的敏感部分打成 ***，host 保留。

    两处都要处理：查询参数（`?key=` / `?share_sign=`）与路径里的长 token 段。
    分享链还带 ``share_sign`` / ``u_code`` / ``did``，日志和 webhook 里不该出现原值。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "***"
    query = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        query.append((k, "***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v))
    return urlunsplit((parts.scheme, parts.netloc, _mask_path(parts.path),
                       urlencode(query, safe="*"), ""))


@dataclass(slots=True)
class Notice:
    """一条待发通知。``url`` 用来跳转，会被脱敏后外发。"""

    title: str
    body: str = ""
    kind: str = "round"          # round | error | quota | login
    day: str = ""
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "kind": self.kind,
            "day": self.day,
            "url": mask_url(self.url),
            "extra": dict(self.extra),
            "ts": time.time(),
        }


class Notifier:
    """按配置的通道把通知送出去。任何通道失败都只记结果，不抛。"""

    def __init__(self, data_dir: Path, *, channel: str = "file",
                 webhook_url: str = "", http: Any = None) -> None:
        self._dir = Path(data_dir) / "notifications"
        self._channel = channel if channel in CHANNELS else "file"
        self._webhook = (webhook_url or "").strip()
        self._http = http          # 测试注入；真实环境按需懒建 httpx.AsyncClient

    @property
    def channel(self) -> str:
        return self._channel

    def _wants(self, name: str) -> bool:
        if self._channel == "both":
            return name in ("file", "toast")
        return self._channel == name

    def _write_queue(self, notice: Notice, deliver: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        path = self._dir / f"{stamp}-{notice.kind}-{deliver}.json"
        payload = notice.payload()
        payload["deliver"] = deliver
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return str(path)

    async def _post(self, notice: Notice) -> dict[str, Any]:
        if not self._webhook:
            return {"ok": False, "error": "没配 webhook 地址"}
        client = self._http
        try:
            if client is not None:
                resp = await client.post(self._webhook, json=notice.payload())
                code = getattr(resp, "status_code", 0)
                return {"ok": 200 <= int(code) < 300, "status": int(code)}
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as real:
                resp = await real.post(self._webhook, json=notice.payload())
                return {"ok": 200 <= resp.status_code < 300, "status": resp.status_code}
        except Exception as exc:  # noqa: BLE001 - webhook 挂了不能拖垮一轮
            # 异常消息里常带完整 URL（httpx 就是这样），脱敏后再外传，别顺手泄凭据
            detail = mask_url(str(exc))[:200]
            log.warning("webhook 通知失败（%s）：%s", mask_url(self._webhook), detail)
            return {"ok": False, "error": detail}

    async def send(self, notice: Notice) -> dict[str, Any]:
        """按通道投递，返回每个通道发生了什么。"""
        result: dict[str, Any] = {"channel": self._channel, "ok": False, "parts": {}}
        if self._wants("file"):
            try:
                result["parts"]["file"] = {"ok": True, "path": self._write_queue(notice, "file")}
            except OSError as exc:
                result["parts"]["file"] = {"ok": False, "error": str(exc)[:200]}
        if self._wants("toast"):
            try:
                result["parts"]["toast"] = {"ok": True, "path": self._write_queue(notice, "toast")}
            except OSError as exc:
                result["parts"]["toast"] = {"ok": False, "error": str(exc)[:200]}
        if self._wants("webhook"):
            result["parts"]["webhook"] = await self._post(notice)
        result["ok"] = any(bool(p.get("ok")) for p in result["parts"].values())
        return result


def round_notice(result, *, report_path: Path | None = None, day: str = "") -> Notice:
    """把一轮结果压成一句人话。标题里写清推了几条，别只说「已完成」。"""
    n = len(result.picked)
    if n:
        best = result.picked[0].candidate
        title = f"刷抖音：今天挑了 {n} 条"
        body = f"{best.title.strip().splitlines()[0][:60]} —— {result.picked[0].judgment.reason[:80] if result.picked[0].judgment else ''}"
    else:
        title = "刷抖音：这一轮没够格的"
        body = f"看了 {result.seen} 条，硬筛掉 {sum(result.dropped.values())} 条。" + (
            result.note or "")
    return Notice(title=title, body=body.strip(), kind="round", day=day,
                  url=result.picked[0].candidate.url if result.picked else "",
                  extra={"report": str(report_path) if report_path else "",
                         "picked": n, "seen": result.seen})
