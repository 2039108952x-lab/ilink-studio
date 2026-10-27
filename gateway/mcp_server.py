"""MCP server：把网关包装成 Reasonix 能直接调用的工具。

传输是 stdio（Reasonix 的 [[plugins]] type="stdio"），协议是逐行 JSON-RPC 2.0。
这个进程自己不含任何平台逻辑，全部转发给网关的 HTTP 控制面，
所以网关没起的时候它会明确报错，而不是假装成功。

    stdout 只写 JSON-RPC，日志一律走 stderr —— 混一行就会破坏协议。

环境变量
    ILINK_GATEWAY_URL    网关控制面地址，默认 http://127.0.0.1:8787
    ILINK_GATEWAY_TOKEN  对应网关的 [server].token_env
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ilink"
SERVER_VERSION = "0.1.0"

GATEWAY_URL = os.environ.get("ILINK_GATEWAY_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("ILINK_GATEWAY_TOKEN", "")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": (
            "往 QQ / 微信 / B站 / 抖音 的指定会话发一条消息。"
            "chat_id：QQ 是 QQ 号或群号，微信是昵称，抖音是会话标题，"
            "B站私信填对方 uid、评论回复填 oid:root:parent。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": ["qq", "wechat", "bilibili", "douyin"],
                    "description": "目标平台",
                },
                "chat_id": {"type": "string", "description": "目标会话 id"},
                "text": {"type": "string", "description": "要发送的文本"},
                "kind": {
                    "type": "string",
                    "enum": ["private", "group", "room", "comment"],
                    "default": "private",
                    "description": "会话类型；B站回复评论区用 comment",
                },
            },
            "required": ["platform", "chat_id", "text"],
        },
    },
    {
        "name": "list_platforms",
        "description": "查看网关当前挂上了哪些平台（也就是哪些渠道真的通了）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bili_video",
        "description": (
            "看一个 B 站视频：拿标题、UP主、简介、时长、播放量，以及 CC 字幕全文。"
            "传 BV 号、av 号或完整链接都行。"
            "对方分享视频、或要聊某个视频内容之前，先用它看一眼，别靠标题瞎猜。"
            "这个工具直接调 B站公开接口，不经过网关，网关没起也能用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video": {"type": "string", "description": "BV 号 / av 号 / 视频链接"},
            },
            "required": ["video"],
        },
    },
    {
        "name": "douyin_video",
        "description": (
            "看一个抖音视频：拿标题、作者、视频描述，以及前若干条评论。"
            "传 aweme_id 或 https://www.douyin.com/video/<id> 链接都行。"
            "要聊某个抖音视频的内容、或想回复它的评论区之前，先用它看一眼，别靠标题瞎猜。"
            "它经网关转发（抖音要登录态，必须借用网关那个浏览器），"
            "所以网关没起时会明确报错，不会假装看到了。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "video": {"type": "string", "description": "aweme_id 或视频链接"},
                "max_comments": {
                    "type": "integer",
                    "description": "最多读几条评论，默认 20",
                },
            },
            "required": ["video"],
        },
    },
    {
        "name": "bili_scan",
        "description": (
            "让 B 站雷达立刻刷一轮：按设置的关键词搜（留空则刷首页推荐流），"
            "读字幕/画面判断值不值得看，写出日报并发到私信。"
            "这是长任务（几十秒到两分钟），同一时刻只许跑一轮。"
            "它经网关转发（要登录态和模型判断），网关没起时会明确报错，不会假装跑过。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "这一轮看几条，默认 8，上限 30"},
            },
        },
    },
    {
        "name": "bili_picks",
        "description": (
            "看「它刷过的视频」：最近日报里挑出来的条目，带档位（值得看/可以看/不值当）"
            "与状态（挑了/跳过/评论）。可按档位、状态筛选与分页。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "grade": {"type": "string", "enum": ["值得看", "可以看", "不值当"]},
                "status": {"type": "string", "enum": ["挑了", "跳过", "评论"]},
                "page": {"type": "integer", "description": "第几页，从 1 开始"},
                "size": {"type": "integer", "description": "每页几条，默认 20"},
            },
        },
    },
    {
        "name": "bili_daily",
        "description": "看 B 站雷达某天的日报原文（Markdown）。不传日期就是今天。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "YYYY-MM-DD，不传=今天"},
            },
        },
    },
    {
        "name": "bili_comment",
        "description": (
            "在 B 站视频下发一条评论，或回复某条评论。"
            "⚠️ 这是拿使用者的号公开说话：不带 confirm 只返回草稿，一个字都不会发。"
            "B 站侧还有一道语感闸门（开头八股 / 跟自己历史太像 / 撞该视频热评），"
            "被拦时返回 blocked_by=style 和改法，按它改写一次再试，别原样重试。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "BV 号（或视频链接）"},
                "text": {"type": "string", "description": "评论正文，≤150 字"},
                "reply_to": {"type": "integer", "description": "要回复的评论 rpid，0=发新评论"},
                "root": {"type": "integer", "description": "楼中楼的楼层根 rpid"},
                "confirm": {"type": "boolean", "description": "true 才真发"},
            },
            "required": ["item_id", "text"],
        },
    },
]


def log(message: str) -> None:
    print(f"[ilink-mcp] {message}", file=sys.stderr, flush=True)


# ---------------- 工具实现 ----------------


def _send_message(arguments: dict[str, Any]) -> str:
    platform = arguments.get("platform")
    chat_id = arguments.get("chat_id")
    text = arguments.get("text")
    kind = arguments.get("kind", "private")
    if not (platform and chat_id and text):
        return "缺少必填参数：platform / chat_id / text"
    if not TOKEN:
        return "没设置 ILINK_GATEWAY_TOKEN，网关会拒绝请求"

    payload = {"platform": platform, "chat_id": str(chat_id), "text": str(text), "kind": kind}
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(
                f"{GATEWAY_URL}/send",
                json=payload,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            data = resp.json()
    except httpx.HTTPError as exc:
        return f"连不上网关（{GATEWAY_URL}）：{exc}"
    except json.JSONDecodeError:
        return f"网关返回的不是 JSON（HTTP {resp.status_code}）"

    if resp.status_code == 200 and data.get("ok"):
        return f"已发送到 {platform}/{chat_id}（msg_id={data.get('msg_id') or 'n/a'}）"
    return f"发送失败（HTTP {resp.status_code}）：{data.get('error') or data}"


def _list_platforms(_arguments: dict[str, Any]) -> str:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{GATEWAY_URL}/health")
            data = resp.json()
    except httpx.HTTPError as exc:
        return f"连不上网关（{GATEWAY_URL}）：{exc}"
    platforms = data.get("platforms") or []
    if not platforms:
        return "网关在跑，但没有启用任何平台"
    return "当前已启用：" + ", ".join(platforms)


def format_video_brief(brief: dict[str, Any]) -> str:
    """把 video_brief 的结果排成一段模型读得懂的话。

    字幕单独放在最后一块，方便模型知道"这个视频讲了什么"，
    又不至于把标题淹没在几千字里。
    """
    lines = [
        f"《{brief.get('title') or '无标题'}》",
        f"UP主：{brief.get('up') or '未知'}　时长：{_human_duration(brief.get('duration_sec'))}"
        f"　播放：{brief.get('view') or 0}　点赞：{brief.get('like') or 0}",
        f"链接：https://www.bilibili.com/video/{brief.get('bvid') or 'av' + str(brief.get('aid'))}",
    ]
    if brief.get("pages", 0) > 1:
        lines.append(f"共 {brief['pages']} 个分P（只看第 1 个）")
    if brief.get("desc"):
        lines.append(f"简介：{brief['desc']}")
    if brief.get("subtitle"):
        lines.append("")
        lines.append("【CC 字幕】")
        lines.append(brief["subtitle"])
    elif brief.get("subtitle_note"):
        lines.append(f"（没有字幕：{brief['subtitle_note']}）")
    return "\n".join(lines)


def _human_duration(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "未知"
    if total <= 0:
        return "未知"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分{secs}秒"


def _bili_video(arguments: dict[str, Any]) -> str:
    """看视频。公开接口，不需要 cookie，所以不经过网关。"""
    ref = str(arguments.get("video") or "").strip()
    if not ref:
        return "缺少必填参数：video"

    import asyncio

    from .adapters.bilibili import video_brief

    try:
        brief = asyncio.run(video_brief(ref))
    except Exception as exc:  # noqa: BLE001 - 工具出错要回文本，不能让 stdio 断掉
        return f"看视频失败：{exc}"
    if not brief.get("ok"):
        return f"看视频失败：{brief.get('error')}"
    return format_video_brief(brief)


def format_douyin_brief(brief: dict[str, Any]) -> str:
    """把 douyin_video 的结果排成一段模型读得懂的话。"""
    lines = [
        f"《{brief.get('title') or '无标题'}》",
        f"作者：{brief.get('author') or '未知'}　评论数：{brief.get('comment_count') or 0}",
        f"链接：{brief.get('url') or ''}",
    ]
    if brief.get("detail"):
        lines.append(f"描述：{brief['detail']}")
    comments = brief.get("comments") or []
    if comments:
        lines.append("")
        lines.append(f"【评论（前 {len(comments)} 条）】")
        for idx, text in enumerate(comments, 1):
            lines.append(f"{idx}. {text}")
    else:
        lines.append("（还没读到评论：可能没展开，或这个视频评论没加载出来）")
    return "\n".join(lines)


def _douyin_video(arguments: dict[str, Any]) -> str:
    """看抖音视频。要登录态，只能经网关转发，不能在本进程起浏览器。"""
    ref = str(arguments.get("video") or "").strip()
    if not ref:
        return "缺少必填参数：video"
    if not TOKEN:
        return "没设置 ILINK_GATEWAY_TOKEN，网关会拒绝请求"

    payload: dict[str, Any] = {"platform": "douyin", "video": ref}
    if arguments.get("max_comments") is not None:
        payload["max_comments"] = arguments["max_comments"]
    try:
        with httpx.Client(timeout=150) as client:
            resp = client.post(
                f"{GATEWAY_URL}/video",
                json=payload,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            data = resp.json()
    except httpx.HTTPError as exc:
        return f"连不上网关（{GATEWAY_URL}）：{exc}"
    except json.JSONDecodeError:
        return f"网关返回的不是 JSON（HTTP {resp.status_code}）"

    if not data.get("ok"):
        return f"看视频失败：{data.get('error') or data}"
    return format_douyin_brief(data)


def _radar_request(path: str, *, method: str = "GET", payload: dict | None = None,
                   timeout: float = 60.0) -> str:
    """经网关调 B 站雷达接口，把结果压成模型读得懂的一段话。

    网关没起、token 没配、业务返回失败 —— 三种情况都要说清是哪一种，
    别糊成一句「失败了」（那是这类工具最没用的地方）。
    """
    if not TOKEN:
        return "没设置 ILINK_GATEWAY_TOKEN，网关会拒绝请求"
    url = f"{GATEWAY_URL}/api/v1/radar/bilibili/{path}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            if method == "GET":
                resp = client.get(url, headers=headers)
            else:
                resp = client.post(url, json=payload or {}, headers=headers)
    except httpx.HTTPError as exc:
        return f"连不上网关（{GATEWAY_URL}）：{exc}"
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return f"网关返回的不是 JSON（HTTP {resp.status_code}）"
    if resp.status_code == 503:
        return (f"B 站雷达没启用：{data.get('error') or data}。"
                "先在配置里把 platforms.bilibili 打开，并扫码登录一次")
    if resp.status_code >= 400 or data.get("ok") is False:
        return f"失败（HTTP {resp.status_code}）：{data.get('error') or data}"
    return json.dumps(data, ensure_ascii=False)


def _bili_scan(arguments: dict[str, Any]) -> str:
    """跑一轮 B 站雷达。长任务，网关那头同步等它跑完。"""
    payload: dict[str, Any] = {}
    if arguments.get("top") is not None:
        payload["top"] = arguments["top"]
    raw = _radar_request("run", method="POST", payload=payload, timeout=900.0)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    summary = data.get("summary") or {}
    lines = [
        f"刷完了：看了 {summary.get('seen', 0)} 条，挑中 {summary.get('picked', 0)} 条，"
        f"另有 {summary.get('uncertain', 0)} 条判断不了，用时 {summary.get('elapsed', 0)} 秒。",
        f"日报：{data.get('report') or '（没写）'}",
    ]
    quota = data.get("quota") or {}
    if quota:
        lines.append(f"今天额度：已用 {quota.get('used', 0)}/{quota.get('per_day', 0)}。")
    notify = data.get("notify") or {}
    if notify.get("ok"):
        lines.append("私信/通知已发出。")
    elif notify.get("error"):
        lines.append(f"通知没发出去：{notify['error']}")
    return "\n".join(lines)


def _bili_picks(arguments: dict[str, Any]) -> str:
    params = []
    for key in ("grade", "status"):
        if arguments.get(key):
            params.append(f"{key}={httpx.QueryParams({key: arguments[key]})[key]}")
    for key in ("page", "size"):
        if arguments.get(key) is not None:
            params.append(f"{key}={int(arguments[key])}")
    query = ("?" + "&".join(params)) if params else ""
    raw = _radar_request(f"videos{query}", timeout=60.0)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    items = data.get("items") or []
    if not items:
        return "还没有刷出东西来。先让 bili_scan 跑一轮。"
    lines = [f"共 {data.get('total', len(items))} 条，第 {data.get('page', 1)}/"
             f"{data.get('pages', 1)} 页："]
    for item in items:
        grade = item.get("grade") or "未定档"
        lines.append(f"- [{grade}] {item.get('bvid')} {item.get('title') or ''}"
                     f" · {item.get('up') or '未知UP'}"
                     + (f" · {item.get('status')}" if item.get("status") else ""))
        if item.get("one_line"):
            lines.append(f"  {item['one_line']}")
    return "\n".join(lines)


def _bili_daily(arguments: dict[str, Any]) -> str:
    day = str(arguments.get("day") or "").strip()
    raw = _radar_request(f"report{'?day=' + day if day else ''}", timeout=60.0)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    text = str(data.get("markdown") or "").strip()
    return text or f"{data.get('day') or '今天'}还没有日报。先让 bili_scan 跑一轮。"


def _bili_comment(arguments: dict[str, Any]) -> str:
    item_id = str(arguments.get("item_id") or "").strip()
    text = str(arguments.get("text") or "").strip()
    if not item_id or not text:
        return "缺少必填参数：item_id / text"
    payload: dict[str, Any] = {"item_id": item_id, "text": text}
    for key in ("reply_to", "root"):
        if arguments.get(key) is not None:
            payload[key] = arguments[key]
    if arguments.get("confirm") is True:
        payload["confirm"] = True
    raw = _radar_request("comment", method="POST", payload=payload, timeout=120.0)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    if not data.get("sent"):
        return f"这是草稿，还没发：\n{data.get('draft') or text}\n\n确认要发就把 confirm 设成 true。"
    rpid = data.get("rpid")
    return f"已发出（rpid={rpid if rpid else '未取到 rpid'}）。"


HANDLERS = {
    "send_message": _send_message,
    "list_platforms": _list_platforms,
    "bili_video": _bili_video,
    "douyin_video": _douyin_video,
    "bili_scan": _bili_scan,
    "bili_picks": _bili_picks,
    "bili_daily": _bili_daily,
    "bili_comment": _bili_comment,
}


# ---------------- JSON-RPC ----------------


def _result(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(request: dict) -> dict | None:
    """处理一条 JSON-RPC 请求；通知（无 id）返回 None。"""
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(req_id, -32602, f"未知工具：{name}")
        try:
            text = handler(arguments)
        except Exception as exc:  # noqa: BLE001 - 工具内部错误不该拖垮 server
            return _result(
                req_id,
                {"content": [{"type": "text", "text": f"工具执行出错：{exc}"}], "isError": True},
            )
        return _result(req_id, {"content": [{"type": "text", "text": text}]})

    if req_id is None:
        return None  # 不认识的通知识趣地忽略
    return _error(req_id, -32601, f"不支持的方法：{method}")


def main() -> int:
    global TOKEN
    if not TOKEN:
        from .token import ensure_token

        TOKEN = ensure_token()
    log(f"启动，网关＝{GATEWAY_URL}，token＝{'已配置' if TOKEN else '未配置'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "JSON 解析失败")
        else:
            if not isinstance(request, dict):
                response = _error(None, -32600, "请求必须是对象")
            else:
                response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
