"""B 站的 RadarProvider 实现：把内置 ``bilibili-mcp`` 的工具包成平台无关接口。

和抖音那份最大的三点不同（都是 B 站自己的性质，不是实现偷懒）
    1. **候选有两个来源**：填了关键词走 ``bili_search``；没填走 ``bili_recommend``
       （首页推荐流）。留空**故意**不用内置词表 —— 否则装了同一个插件的所有人
       刷的是同一批视频，评论区会堆成「同款互相报到」。
    2. **判断证据是字幕原文**：B 站大量视频带 CC 字幕，这是比抖音强的地方。
       拿不到字幕才退回弹幕 + 评论；演示类再抽故事板帧看画面。抖音没有字幕轨，只能读观众反应。
    3. **发送只有私信和评论两条路**：没有「分享卡片」这种东西，``send_share``
       就把「标题 + 链接 + 理由」当一条私信发出去。

三个必须记住的坑（源码里读出来的，不是猜的）
    * ``bili_reply`` / ``bili_send_message`` 用 ``success`` 而不是 ``ok`` 表示成败 ——
      ``McpClient`` 按 ``ok`` 判，会把它当成功。所以这里统一再判一道 ``success is False``。
    * ``bili_comments`` 抓取失败时**照样返回 comments**，只在旁边多一个 ``error`` ——
      不能把「没抓到」当成「这条没人评论」。
    * ``bili_search`` / ``bili_recommend`` 的 ``duration`` 是 ``"m:ss"`` 字符串，不是秒。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Any

from ..bili_mcp import BiliMCP, McpError, classify, describe_error
from .gate import SendGate
from .models import Candidate, CommentSnapshot
from .pipeline import Thresholds
from .provider import RadarError, ShareDraft

log = logging.getLogger(__name__)

# B 站能直接点开的链接；分享链、带签名的另算，不混用（provider 约定第 3 条）
VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
SPACE_URL = "https://space.bilibili.com/{uid}"

# 私信实测上限 500 字符：490 能发，再长被拒并返回 21007。留一点余量。
DM_MAX_CHARS = 490

# B 站视频编号：BV 后面固定 10 位
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# ------------------------------------------------------------------ 阈值
# 抖音那套（3000 互动 / 1% 讨论比）是照推荐流量级定的，搬到 B 站会把搜索结果全筛掉：
# 搜出来的视频播放高、点赞少、评论稀疏。这里按 B 站量级重定，宁可多送几条给模型判。
DEFAULT_THRESHOLDS = Thresholds(
    min_duration_s=30.0,      # 30 秒以下基本是切片，读不出内容
    min_interactions=200,     # 赞 + 评论进「有点人看」的门槛
    min_talk_ratio=0.002,     # B 站评论本来就稀疏，比值主要用来压纯流量号
    max_judge=5,
    min_confidence=0.6,
    comments_per_item=8,
    max_age_h=24.0 * 7,       # 章程写的是「近 7 天优先」
)


def parse_duration(raw: Any) -> int | None:
    """``"12:34"`` / ``"1:02:03"`` / ``754`` → 秒。认不出来返回 ``None``。"""
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total or None


def error_text(payload: dict[str, Any]) -> str:
    """把 B 站返回里的失败信息压成一句人话。

    ``message`` 在 B 站工具里是双关的：成功时是「凭证有效」、失败时才是原因。
    所以只在没有任何成功标记时才看它，免得把成功当成失败。
    """
    for key in ("error", "hint"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    if payload.get("success") is False or payload.get("ok") is False:
        return str(payload.get("message") or "").strip() or "工具返回失败"
    return ""


def bvid_of(text: str) -> str:
    """从 BV 号 / 链接里抠出 BV 号。抠不到就原样返回（交给上游报错）。"""
    raw = str(text or "").strip()
    match = _BVID_RE.search(raw)
    return match.group(0) if match else raw


def _failed(payload: dict[str, Any]) -> bool:
    """失败判定：``ok`` 与 ``success`` 任一为假就算失败。"""
    return payload.get("ok") is False or payload.get("success") is False


class BilibiliRadar:
    """B 站内容通道。一个实例对应一个 MCP 子进程，由适配器或服务层持有复用。"""

    platform = "bilibili"
    name = "bilibili"
    supports_share = True
    supports_text = True
    supports_comment = True

    def __init__(
        self,
        client: BiliMCP,
        home: Path | str,
        *,
        keywords: list[str] | None = None,
        dm_uid: str = "",
        timeout: float = 120.0,
        gate: SendGate | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._home = Path(home)
        self._keywords = [k.strip() for k in (keywords or []) if k and k.strip()]
        self._dm_uid = str(dm_uid or "").strip()
        self._timeout = float(timeout)
        self._gate = gate if gate is not None else SendGate(self._home)
        # 随机源可注入：章程要求「从推荐流里随机挑」，测试里得能复现
        self._rng = rng or random.Random()

    # ---------------------------------------------------------------- 底层

    async def _call(self, tool: str, args: dict[str, Any] | None = None,
                    *, timeout: float | None = None) -> dict[str, Any]:
        """调一个工具；失败转成 ``RadarError``（带 provider 统一失败码）。"""
        try:
            result = await self._client.call(tool, args or {},
                                             timeout=timeout or self._timeout)
        except McpError as exc:
            raise RadarError(exc.code, exc.safe_message, retryable=exc.retryable) from exc
        payload = result.data if isinstance(result.data, dict) else {}
        if not result.ok or _failed(payload):
            text = error_text(payload) or result.error or "B站工具返回失败"
            raise RadarError(classify(payload), text, payload=payload)
        return payload

    async def _send(self, tool: str, args: dict[str, Any], *, channel: str,
                    confirm: bool, timeout: float | None = None) -> dict[str, Any]:
        """发送类工具的统一入口：真发送先过本地冷却闸。

        上游的冷却活在子进程内存里，重启就清零 —— 撞过 403 之后靠它记住是不行的。
        """
        if confirm:
            self._gate.check(channel)
        try:
            payload = await self._call(tool, args, timeout=timeout)
        except RadarError as exc:
            if exc.code == "rate_limited":
                self._gate.record_limited(channel)
            raise
        if confirm and payload.get("sent"):
            self._gate.record_sent(channel)
        return payload

    async def close(self) -> None:
        await self._client.close()

    @property
    def alive(self) -> bool:
        return self._client.alive

    # ---------------------------------------------------------------- 只读

    async def whoami(self) -> dict:
        """登录态。未登录时**明确说未登录**，不返回假账号。"""
        payload = await self._call("bili_check_credential", timeout=30.0)
        logged = bool(payload.get("logged_in"))
        return {
            "ok": True,
            "logged_in": logged,
            "nick": str(payload.get("username") or ""),
            "uid": str(payload.get("uid") or ""),
            "detail": str(payload.get("message") or ""),
            "raw": payload,
        }

    async def candidates(self, *, top: int = 8) -> list[Candidate]:
        """拉候选。有命中关键词就搜，一个词一轮；没有就刷首页推荐流。"""
        want = max(1, min(int(top), 40))
        if self._keywords:
            keyword = self._keywords[0]
            payload = await self._call(
                "bili_search",
                {"keyword": keyword, "num": min(want * 2, 50), "order": "pubdate"},
                timeout=max(self._timeout, 180.0),
            )
        else:
            payload = await self._call(
                "bili_recommend", {"ps": want}, timeout=max(self._timeout, 180.0))
        rows = payload.get("videos")
        if not isinstance(rows, list):
            return []
        items: list[Candidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or "").strip()
            if not bvid:
                continue
            stats = {
                "like": int(row.get("like") or 0),
                "comment": int(row.get("review") or 0),
                "play": int(row.get("play") or 0),
                "danmaku": int(row.get("danmaku") or 0),
            }
            items.append(Candidate(
                platform="bilibili",
                item_id=bvid,
                title=str(row.get("title") or "").strip(),
                author=str(row.get("author") or "").strip(),
                duration_sec=parse_duration(row.get("duration")),
                url=VIDEO_URL.format(bvid=bvid),
                published_at=row.get("pubdate") or None,
                stats=stats,
                extra={"description": str(row.get("description") or "")[:400],
                       "reason": str(row.get("reason") or "")},
            ))
        # 章程要求随机挑，不固定取前 N：画像相近的人否则还是会撞车
        self._rng.shuffle(items)
        return items[:want]

    async def comments(self, item_id: str, *, limit: int = 8) -> list[CommentSnapshot]:
        """高赞评论。抓取失败**不能**当成「没人评论」—— 返回空列表但记日志。"""
        bvid = bvid_of(item_id)
        try:
            payload = await self._call("bili_comments", {"bvid": bvid, "num": max(1, int(limit))})
        except RadarError as exc:
            log.warning("B 站拉评论失败（%s）：%s", bvid, exc.code)
            return []
        if payload.get("error"):
            # 上游在「抓到一半失败」时也走这条：有评论就用，用不到的部分不假装
            log.warning("B 站评论抓取不完整（%s）：%s", bvid, payload.get("error"))
        rows = payload.get("comments") or []
        result: list[CommentSnapshot] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            text = str(row.get("content") or "").strip()
            if not text:
                continue
            result.append(CommentSnapshot(text=text[:200], likes=int(row.get("like") or 0)))
        result.sort(key=lambda item: item.likes, reverse=True)
        return result[:max(1, limit)]

    async def evidence(self, candidate: Candidate) -> dict[str, Any]:
        """判断这一条要用的平台证据：字幕优先，弹幕兜底。

        返回结构给 ``pipeline.build_prompt`` 直接用：
            ``{"subtitle": str, "segments": int, "danmaku": [str], "source": "subtitle|danmaku|none"}``
        拿不到就是拿不到，**不编** —— 章程里「没字幕就跳过」和「空是结果不是坏了」
        都依赖这个字段说实话。
        """
        bvid = bvid_of(candidate.item_id)
        subtitle = ""
        segments = 0
        try:
            payload = await self._call("bili_subtitle", {"bvid": bvid},
                                       timeout=max(self._timeout, 180.0))
            subtitle = str(payload.get("text") or "").strip()
            segments = int(payload.get("segments") or 0)
        except RadarError as exc:
            if exc.code != "not_logged_in":
                log.info("B 站取字幕失败（%s）：%s", bvid, exc.code)
        if subtitle and _is_speechless(subtitle):
            # 只有「字幕by某某」这类署名 = 这条视频没有语音，不是工具坏了
            subtitle, segments = "", 0

        danmaku: list[str] = []
        if not subtitle:
            try:
                payload = await self._call("bili_danmaku", {"bvid": bvid, "num": 30},
                                           timeout=max(self._timeout, 120.0))
                for row in payload.get("danmakus") or []:
                    if isinstance(row, dict) and row.get("text"):
                        danmaku.append(str(row["text"])[:60])
            except RadarError as exc:
                log.info("B 站取弹幕失败（%s）：%s", bvid, exc.code)

        return {
            "subtitle": subtitle,
            "segments": segments,
            "danmaku": danmaku[:30],
            "source": "subtitle" if subtitle else ("danmaku" if danmaku else "none"),
        }

    def share_url(self, candidate: Candidate) -> str:
        return candidate.url or VIDEO_URL.format(bvid=bvid_of(candidate.item_id))

    # ---------------------------------------------------------------- 发送

    async def send_share(self, draft: ShareDraft, *, confirm: bool = False) -> ShareDraft:
        """B 站没有分享卡片：把「标题 + 链接 + 理由」当一条私信发出去。

        ``confirm=False`` 只回草稿，一个字都不发。
        """
        uid = str(draft.to or self._dm_uid).strip()
        if not uid:
            return ShareDraft(item_id=draft.item_id, to="", reason=draft.reason,
                              preview=draft.preview, sent=False,
                              detail="没有收件人 UID：在设置里填「私信发给谁」，或指定 to")
        link = draft.preview or VIDEO_URL.format(bvid=bvid_of(draft.item_id))
        text = f"{link}\n{draft.reason}".strip() if draft.reason else link
        if not confirm:
            return ShareDraft(item_id=draft.item_id, to=uid, reason=draft.reason,
                              preview=text[:200], sent=False, detail="未发送（缺 confirm）")
        payload = await self._send("bili_send_message",
                                   {"receiver_uid": int(uid), "text": text[:DM_MAX_CHARS]},
                                   channel="dm", confirm=True)
        return ShareDraft(item_id=draft.item_id, to=uid, reason=draft.reason,
                          preview=text[:200], sent=bool(payload.get("success")),
                          detail=str(payload.get("message") or ""))

    async def send_text(self, to: str, text: str, *, confirm: bool = False) -> dict:
        """发一条私信。B 站实测上限 500 字符，超了先在本地说清楚，不发给它去撞 21007。"""
        uid = str(to or self._dm_uid).strip()
        body = str(text or "").strip()
        if not uid:
            raise RadarError("invalid_args", "没有收件人 UID：先填「私信发给谁」")
        if not body:
            raise RadarError("invalid_args", "私信正文是空的")
        if len(body) > DM_MAX_CHARS:
            raise RadarError(
                "too_long",
                f"私信 {len(body)} 字，超过 {DM_MAX_CHARS} 字上限（B 站超过 500 字会整条被拒）",
            )
        if not confirm:
            return {"ok": True, "sent": False, "draft": body, "needs_confirm": True}
        payload = await self._send("bili_send_message",
                                   {"receiver_uid": int(uid), "text": body},
                                   channel="dm", confirm=True)
        sent = bool(payload.get("success"))
        # 上游 2.0.4 起要求「有 msg_key 才算真投递」，它已经把这条判断做在 success 里了
        self._gate.record_sent("dm") if sent else None
        return {"ok": sent, "sent": sent, "detail": str(payload.get("message") or "")}

    async def comment_reply(self, *, bvid: str, text: str, rpid: int = 0, root: int = 0,
                            confirm: bool = False) -> dict:
        """发评论 / 回复评论。风格闸门拦下来的会抛 ``style_blocked``，上层可以改写一次。"""
        body = str(text or "").strip()
        if not body:
            raise RadarError("invalid_args", "评论正文是空的")
        args: dict[str, Any] = {"bvid": bvid_of(bvid), "text": body}
        if rpid:
            args["rpid"] = int(rpid)
        if root:
            args["root"] = int(root)
        if not confirm:
            return {"ok": True, "sent": False, "needs_confirm": True, "draft": body}
        payload = await self._send("bili_reply", args, channel="comment", confirm=True,
                                   timeout=max(self._timeout, 120.0))
        sent = bool(payload.get("success"))
        return {"ok": sent, "sent": sent, "rpid": payload.get("rpid"),
                "root": payload.get("root"), "detail": str(payload.get("message") or "")}

    async def post_comment(self, *, item_id: str, text: str, reply_to: int = 0,
                           root: int = 0, confirm: bool = False) -> dict:
        """统一签名的发评论入口（网关和 MCP 工具只认这个）。

        ``reply_to`` 是要回复的那条评论 id，``root`` 是楼中楼的楼层根。
        """
        return await self.comment_reply(bvid=item_id, text=text, rpid=reply_to,
                                        root=root, confirm=confirm)

    # ---------------------------------------------------------------- 平台特有

    async def doctor(self) -> dict[str, Any]:
        """体检：Python / 脚本 / 凭证 / 工具数。缺什么就明说缺什么。"""
        items: list[dict[str, Any]] = []
        warns: list[str] = []
        must_fix: list[str] = []

        items.append({"key": "home", "ok": self._home.is_dir(),
                      "detail": str(self._home)})
        script = self._home / "mcp_server.py"
        items.append({"key": "mcp_server", "ok": script.is_file(),
                      "detail": str(script)})
        if not script.is_file():
            must_fix.append("家目录里没有 mcp_server.py：点「装主依赖」铺一次")

        try:
            identity = await self.whoami()
            ok = bool(identity.get("logged_in"))
            items.append({"key": "credential", "ok": ok,
                          "detail": identity.get("nick") or identity.get("detail") or ""})
            if not ok:
                must_fix.append("还没登录：点「扫码登录」")
        except RadarError as exc:
            items.append({"key": "credential", "ok": False, "detail": exc.message})
            must_fix.append(f"登录态检查失败：{exc.message}")

        try:
            tools = await self._client.list_tools(timeout=30.0)
            names = [str(t.get("name") or "") for t in tools]
            items.append({"key": "tools", "ok": bool(names),
                          "detail": f"{len(names)} 个工具"})
            if not names:
                warns.append("MCP 起来了但没报出任何工具")
        except (McpError, OSError) as exc:
            items.append({"key": "tools", "ok": False, "detail": str(exc)[:200]})
            must_fix.append("MCP 子进程起不来：检查 Python 与 mcp<2 是否装好")

        asr = self._home / ".venv-asr"
        if asr.is_dir():
            items.append({"key": "transcribe", "ok": True, "detail": "转写环境已就绪"})
        else:
            items.append({"key": "transcribe", "ok": False, "detail": "没装（可选）"})
            warns.append("本地转写没装：没字幕的视频会跳过，或先看画面")

        return {"ok": not must_fix, "items": items, "must_fix": must_fix, "warns": warns}

    async def video_info(self, bvid: str) -> dict[str, Any]:
        return await self._call("bili_video_info", {"bvid": bvid_of(bvid)})

    async def danmaku(self, bvid: str, *, num: int = 30) -> dict[str, Any]:
        return await self._call("bili_danmaku", {"bvid": bvid_of(bvid), "num": int(num)})

    async def recommend(self, *, ps: int = 20) -> dict[str, Any]:
        return await self._call("bili_recommend", {"ps": int(ps)},
                                timeout=max(self._timeout, 180.0))

    async def search(self, keyword: str, *, num: int = 10,
                     order: str = "totalrank") -> dict[str, Any]:
        return await self._call("bili_search",
                                {"keyword": keyword, "num": int(num), "order": order},
                                timeout=max(self._timeout, 180.0))

    # ---- 画面 ----

    async def cover(self, bvid: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """封面。第二项是图片块（``{"data","mimeType"}``），没有就是空列表。"""
        return await self._client.call_raw("bili_cover", {"bvid": bvid_of(bvid)},
                                           timeout=max(self._timeout, 120.0))

    async def frames(self, bvid: str, *, num: int = 3,
                     at: list[int] | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """故事板抽帧，最多 6 帧。``at`` 只能取最近的切片，引用时间按返回的 ``got_sec`` 说。"""
        args: dict[str, Any] = {"bvid": bvid_of(bvid), "num": max(1, min(int(num), 6))}
        if at:
            args["at"] = list(at)
        return await self._client.call_raw("bili_frames", args,
                                           timeout=max(self._timeout, 180.0))

    # ---- 转写 ----

    async def transcribe_start(self, bvid: str) -> dict[str, Any]:
        """起后台 whisper job，立刻返回 ``job_id``。慢活，别在轮中等它。"""
        return await self._call("bili_transcribe_start", {"bvid": bvid_of(bvid)}, timeout=60.0)

    async def transcribe_result(self, job_id: str) -> dict[str, Any]:
        return await self._call("bili_transcribe_result", {"job_id": job_id}, timeout=60.0)

    async def transcribe(self, bvid: str) -> dict[str, Any]:
        """同步版：上游最多等 25 秒，跑不完会把 job_id 交回来。"""
        return await self._call("bili_transcribe", {"bvid": bvid_of(bvid)}, timeout=60.0)

    async def wait_transcribe(self, job_id: str, *, timeout: float = 600.0,
                              interval: float = 5.0) -> dict[str, Any]:
        """在**另一个进程/线程**里等转写，别占着刷一轮的通道。默认最多等 10 分钟。"""
        waited = 0.0
        while waited < timeout:
            payload = await self.transcribe_result(job_id)
            state = str(payload.get("state") or "")
            if state and state != "running":
                return payload
            if payload.get("ok") and payload.get("text"):
                return payload
            await asyncio.sleep(interval)
            waited += interval
        return {"ok": False, "state": "timeout", "error": f"等了 {timeout:.0f}s 还没转完"}


def _is_speechless(text: str) -> bool:
    """「空的结果」判据：空 / 0 字节 / 只有一行「字幕by某某」这类署名。

    章程里这条单独拎出来强调过：这不是转写坏了，是这条视频没有语音。
    上游有用户为此烧了 27 次工具调用，所以这里必须当成正常结果。
    """
    body = (text or "").strip()
    if len(body) < 8:
        return True
    lowered = body.lower()
    if body.startswith("字幕by") or body.startswith("字幕提供") or "字幕by" in lowered[:20]:
        return True
    return False


def build_radar(*, repo_root: Path | str, data_dir: Path | str,
                resource_root: Path | str | None = None, python: str = "",
                keywords: list[str] | None = None, dm_uid: str = "",
                timeout: float = 120.0) -> BilibiliRadar:
    """按「家目录 + 默认阈值」装一台 B 站雷达。装配点只调这一个函数。"""
    from ..bili_home import ensure_home
    from ..bili_mcp import build_client

    state = ensure_home(data_dir, source_root=repo_root, python=python)
    client = build_client(
        repo_root=repo_root,
        home=state.home,
        resource_root=resource_root,
        python=python or state.python,
        timeout=timeout,
    )
    return BilibiliRadar(client, state.home, keywords=keywords, dm_uid=dm_uid,
                         timeout=timeout)
