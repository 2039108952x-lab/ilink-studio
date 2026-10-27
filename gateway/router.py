"""入站流水线与发送路由（PRD §6.3 / §6.4 / §6.5）。

一条平台消息进来之后的顺序是固定的：

    过滤前置 → 落库（消息 + 身份 + 会话 + 检查点，一个事务）
    → 后台 worker 按租约领取 → 组装上下文 → 模型 → 候选回复与 outbox(pending)
    → 单线程发送 worker → sent / degraded / failed / unknown

要点
----
- ``on_inbound`` 只做落库和唤醒 worker，绝不让平台长轮询等模型（PRD §6.3）。
- 白名单外的消息只写 ``metadata_only`` 审计元数据，正文既不落库也不进 prompt（AC-05）。
- 同一 thread 串行：领取语句里排除「本 thread 已有 processing 消息」，
  跨 thread 的并发由 worker 数量封顶。
- 发送结果只有两种是确定的：适配器明确返回失败（``failed``，不自动重发）
  和明确成功（``sent``）。抛异常、崩溃一律 ``unknown``，等人工核对（PRD §6.4）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .adapters.base import Adapter
from .checkpoint import Checkpoint
from .mailbox import (
    BoundaryChanged,
    ClaimedMessage,
    Incoming,
    Mailbox,
    MailboxError,
    OutboxTask,
)
from .models import (
    Attachment,
    ChatKind,
    InboundMessage,
    OutboundMessage,
    Platform,
    PromptContext,
    SendResult,
)

log = logging.getLogger(__name__)

# 同一条对话里 brain 一直挂时，别每条消息都刷「卡了一下」
_FAIL_NOTICE_COOLDOWN = 90.0
_FAIL_NOTICE = "我这边卡了一下，你再说一遍？"
_LEASE_SECONDS = 180.0
_POLL_INTERVAL = 0.25
# 这些错误码说的是「这条消息本身不合规」，不是「数据库挂了」：
# 一条脏消息不该把整个网关标成降级、更不该让界面显示「已暂停 AI 处理」。
_DATA_ERROR_CODES = frozenset({"invalid_body", "invalid_identity_kind", "invalid_scope"})


class Brain(Protocol):
    """把一句（可能带图的）话变成一句回答。ReasonixBrain 实现它。"""

    async def respond(
        self, ctx: PromptContext, text: str, attachments: list[Attachment] | None = None
    ): ...


class ReplyLike(Protocol):
    text: str
    sticker: Path | None


def _attachments_json(attachments: list[Attachment]) -> str:
    return json.dumps(
        [{"kind": a.kind, "url": a.url, "path": a.path, "name": a.name} for a in attachments],
        ensure_ascii=False,
    )


def _decode_attachments(raw: str) -> list[Attachment]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    result: list[Attachment] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        result.append(Attachment(
            kind=str(item.get("kind") or "image"),
            url=str(item.get("url") or ""),
            path=str(item.get("path") or ""),
            name=str(item.get("name") or ""),
        ))
    return result


def classify_brain_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if "BrainUnavailable" in name:
        return "brain_unavailable"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "brain_timeout"
    return "brain_error"


class Router:
    def __init__(
        self,
        brain: Brain,
        adapters: dict[Platform, Adapter],
        *,
        store: Mailbox,
        allow_all: bool = False,
        allowlist: dict[str, set[str]] | None = None,
        paused: Callable[[], bool] | None = None,
        concurrency: int = 2,
        owner: str | None = None,
        settings=None,
        metrics=None,
        budget=None,
        facts=None,
    ) -> None:
        if store is None:
            raise ValueError("Router 必须带持久化消息库；没有库就不允许收发")
        self._brain = brain
        self._adapters = adapters
        self._store = store
        self._allow_all = allow_all
        self._allowlist = allowlist or {}
        self._paused_cb = paused
        self._concurrency = max(1, int(concurrency))
        self._owner = owner or f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._settings = settings
        self._metrics = metrics
        self._budget = budget
        self._facts = facts
        self._wake = asyncio.Event()
        self._outbox_wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self._inflight = 0
        self._db_down = False
        self._fail_notice_at: dict[int, float] = {}
        for platform, adapter in adapters.items():
            account_id = self._store.account_id(platform.value)
            factory = None
            if account_id is not None:
                factory = (lambda stream, account_id=account_id, name=platform.value:
                           Checkpoint(self._store, account_id, f"{name}:{stream}"))
            adapter.bind(self.on_inbound, checkpoints=factory)
            adapter.on_needs_human = (
                lambda reason, name=platform.value: self._mark_needs_human(Platform(name), reason)
            )

    def bind_adapter(self, platform: Platform, adapter: Adapter) -> None:
        """由 ChannelManager 在旧适配器停止后替换；不复制一套业务状态。"""
        account_id = self._store.account_id(platform.value)
        factory = None
        if account_id is not None:
            factory = (lambda stream, account_id=account_id, name=platform.value:
                       Checkpoint(self._store, account_id, f"{name}:{stream}"))
        adapter.bind(self.on_inbound, checkpoints=factory)
        adapter.on_needs_human = (
            lambda reason, name=platform.value: self._mark_needs_human(Platform(name), reason)
        )
        self._adapters[platform] = adapter

    def unbind_adapter(self, platform: Platform) -> None:
        adapter = self._adapters.pop(platform, None)
        if adapter is not None:
            adapter.on_needs_human = None
            adapter.bind(self.on_inbound, checkpoints=None)

    @property
    def platforms(self) -> list[Platform]:
        """当前挂上来的平台，给控制面 /health 用。"""
        return list(self._adapters)

    def _image_capability(self, platform: Platform) -> str:
        """平台能不能发图：只有明确 supported 才发图，其余一律降级（unknown ≠ 支持）。"""
        adapter = self._adapters.get(platform)
        if adapter is None:
            return "unavailable"
        try:
            value = adapter.capabilities().get("image_send")
        except Exception:  # noqa: BLE001 - 适配器坏了按 unknown 处理
            log.exception("读取 %s 能力失败", platform.value)
            return "unknown"
        return str(value or "unknown")

    def current_config(self):
        """界面改过的配置（预算/白名单等）从这里取最新值；没有就返回 None。"""
        return self._settings.current() if self._settings is not None else None

    def capabilities(self) -> dict[str, dict[str, Any]]:
        """四平台各自声明的能力；没挂的适配器不会出现在这里。"""
        return {
            platform.value: adapter.capabilities()
            for platform, adapter in self._adapters.items()
        }

    @property
    def store(self) -> Mailbox:
        return self._store

    @property
    def health(self) -> dict[str, Any]:
        return {
            "database": self._store.health,
            "degraded": self._db_down,
            "paused": self.paused,
            "workers": len(self._tasks),
        }

    # ---- 策略 ----

    def paused(self) -> bool:
        if self._paused_cb is not None:
            try:
                return bool(self._paused_cb())
            except Exception:  # noqa: BLE001 - 设置读不出来时按「没暂停」继续，但要留痕
                log.exception("读取暂停状态失败")
        return self._store.get_setting("reply_paused", "false") == "true"

    def allowed(self, msg: InboundMessage) -> bool:
        """白名单只看稳定标识：只有昵称（provisional）永远不自动回复（FR-5.3）。

        ``stable`` 是平台发的不可变 ID；``opaque`` 是平台内稳定但非官方 uid 的句柄
        （抖音会话标题、wxauto 昵称）——只有本人在配置里逐条列出才生效。
        ``provisional`` 连稳定句柄都没有，因此既不入白名单也不保存正文。
        """
        if msg.sender_id_kind == "provisional":
            return False
        cfg = self.current_config()
        if cfg is not None:
            if cfg.allow_all:
                return True
            return msg.sender_id in cfg.allowlist.get(msg.platform.value, set())
        if self._allow_all:
            return True
        return msg.sender_id in self._allowlist.get(msg.platform.value, set())

    def record_all_bodies(self) -> bool:
        """白名单外的人发来的正文要不要留在本机。

        只看 ``config.toml`` 的 ``[privacy].record_all_bodies``：界面覆盖层不管这一段，
        默认关闭时行为和以前完全一样（只留审计元数据）。
        """
        cfg = self.current_config()
        return bool(cfg.privacy.record_all_bodies) if cfg is not None else False

    # ---- 生命周期 ----

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            recovered = await asyncio.to_thread(self._store.recover)
            if any(recovered.values()):
                log.warning(
                    "崩溃恢复：%d 条入站退回队列，%d 个发送任务转 unknown（需人工核对）",
                    recovered.get("messages_requeued", 0), recovered.get("outbox_unknown", 0),
                )
        except MailboxError:
            self._db_down = True
            log.exception("启动恢复失败：数据库不可用")
        for index in range(self._concurrency):
            self._tasks.append(asyncio.create_task(self._processor(index), name=f"inbound-{index}"))
        self._tasks.append(asyncio.create_task(self._outbox_worker(), name="outbox"))
        self._wake.set()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False

    # ---- 入站 ----

    async def on_inbound(self, msg: InboundMessage) -> None:
        """适配器回调：只落库，不阻塞长轮询。"""
        try:
            await asyncio.to_thread(self._record, msg)
        except MailboxError as exc:
            if exc.code in _DATA_ERROR_CODES:
                # 格式问题：留一条审计 + 一行 warning 就够，别把整个网关标成数据库故障。
                log.warning(
                    "消息 %s 不合规，已跳过落库：%s（%s）",
                    msg.msg_id or "无 id", exc, exc.code,
                )
                try:
                    self._store.audit(
                        "inbound_invalid", "message", "",
                        {"reason": exc.code, "platform": msg.platform.value},
                    )
                except MailboxError:
                    # 写审计都失败了，那就真的是库的问题，退回原判定。
                    self._db_down = True
                return
            self._db_down = True
            if self._metrics is not None:
                self._metrics.count("db_errors")
            log.exception("消息落库失败，已暂停 AI 处理")
            return
        self._db_down = False
        self._wake.set()

    def _record(self, msg: InboundMessage) -> None:
        account_id = self._store.account_id(msg.platform.value)
        if account_id is None:
            log.error("平台 %s 没有启用的账号槽位，消息未落库", msg.platform.value)
            self._store.audit(
                "inbound_without_account", "message", "",
                {"platform": msg.platform.value, "kind": msg.kind.value},
            )
            return
        incoming = self._incoming(msg, account_id)
        if not self.allowed(msg):
            reason = "provisional_identity" if msg.sender_id_kind == "provisional" else "not_allowlisted"
            if reason != "provisional_identity" and self.record_all_bodies():
                # [privacy].record_all_bodies 打开：正文留在本机让收件箱看得见，
                # 但仍然不回复 —— 自动回复的开关始终是白名单。
                if not incoming.text.strip():
                    incoming.storage_class = "metadata_only"
                    incoming.text = ""
                incoming.status = "skipped"
                incoming.reply_policy = "never"
                incoming.skip_reason = reason
                result = self._store.ingest(incoming)
                self._store.audit(
                    "inbound_body_kept", "message", str(result.message_id),
                    {"reason": reason, "platform": msg.platform.value},
                )
                log.info("不在白名单（%s），正文按配置留在本机：%s/%s",
                         reason, msg.platform.value, msg.sender_id)
                return
            self._store.record_rejected(incoming, reason=reason)
            log.info("不在白名单（%s），忽略 %s/%s", reason, msg.platform.value, msg.sender_id)
            return
        status, policy, reason = self._classify(msg)
        if not incoming.text.strip():
            # 纯附件消息（图片/表情/文件）没有正文。以前它带着 storage_class="full" 进
            # ingest，被 mailbox 判成 invalid_body，整条消息丢掉，日志还刷「已暂停 AI 处理」。
            # 现在：附件 JSON 照旧保存、正文留空，标成 skipped/empty_body —— 不入模型，
            # 但收件箱里看得见「对方发过东西」。
            incoming.storage_class = "metadata_only"
            incoming.text = ""
            status, policy, reason = "skipped", "never", "empty_body"
        incoming.status = status
        incoming.reply_policy = policy
        incoming.skip_reason = reason
        self._store.ingest(incoming)

    @staticmethod
    def _incoming(msg: InboundMessage, account_id: int) -> Incoming:
        occurred_at = msg.ts if msg.ts_source == "platform" else None
        return Incoming(
            account_id=account_id,
            platform=msg.platform.value,
            kind=msg.kind.value,
            chat_id=msg.chat_id,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            id_kind=msg.sender_id_kind,
            text=msg.text,
            platform_msg_id=msg.msg_id,
            attachments_json=_attachments_json(msg.attachments),
            occurred_at=occurred_at,
            time_source="platform" if occurred_at is not None else "received",
        )

    def _classify(self, msg: InboundMessage) -> tuple[str, str, str | None]:
        """返回 (状态, reply_policy, skip_reason)；顺序就是优先级。"""
        if msg.backlog:
            return "skipped", "never", "backlog"
        if self.paused():
            return "skipped", "never", "reply_paused"
        if not msg.text.strip() and not msg.attachments:
            return "skipped", "never", "empty_body"
        return "received", "normal", None

    # ---- 处理 worker ----

    async def _processor(self, index: int) -> None:
        while True:
            try:
                claim = await asyncio.to_thread(
                    self._store.claim_message, self._owner, lease_seconds=_LEASE_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except MailboxError:
                self._db_down = True
                log.exception("领取入站消息失败")
                await asyncio.sleep(1.0)
                continue
            if claim is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=_POLL_INTERVAL)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                self._wake.clear()
                continue
            self._inflight += 1
            started = time.monotonic()
            try:
                await self._handle(claim)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单条消息失败不能带走 worker
                log.exception("处理消息失败：%s", claim.id)
                await self._safe_status(claim.id, "failed", error_code="worker_error")
            finally:
                self._inflight -= 1
                if self._metrics is not None:
                    self._metrics.observe("inbound", time.monotonic() - started)
                self._outbox_wake.set()

    async def _safe_status(self, message_id: int, status: str, *,
                           skip_reason: str | None = None,
                           error_code: str | None = None) -> None:
        try:
            await asyncio.to_thread(
                self._store.set_status, message_id, status,
                skip_reason=skip_reason, error_code=error_code,
            )
        except MailboxError:
            self._db_down = True
            log.exception("更新消息状态失败：%s", message_id)

    async def _handle(self, claim: ClaimedMessage) -> None:
        if claim.reply_policy == "never":
            await self._safe_status(claim.id, "skipped", skip_reason="reply_policy_never")
            return
        if self.paused():
            await self._safe_status(claim.id, "skipped", skip_reason="reply_paused")
            return
        if hasattr(self._brain, "backend_ready") and not self._brain.backend_ready:
            await self._safe_status(claim.id, "skipped", skip_reason="backend_not_ready")
            return
        cfg = self.current_config()
        if cfg is not None and self._budget is not None:
            reason = self._budget.check(cfg, claim.platform)
            if reason:
                if self._metrics is not None:
                    self._metrics.count(
                        "budget_blocked" if reason.startswith("budget") else "rate_limited")
                await self._safe_status(claim.id, "skipped", skip_reason=reason)
                try:
                    await asyncio.to_thread(
                        self._store.audit, "reply_blocked", "message", str(claim.id),
                        {"reason": reason},
                    )
                except MailboxError:
                    pass
                return
            self._budget.record(claim.platform)
        ctx = PromptContext(
            thread_id=claim.thread_id, message_id=claim.id, person_id=claim.person_id,
            platform=claim.platform, kind=claim.kind,
            chat_id=claim.chat_id, sender_name=claim.sender_name or claim.person_name,
            person_name=claim.person_name, is_group=claim.kind != "private",
        )
        attachments = _decode_attachments(claim.attachments_json)
        try:
            reply = await self._brain.respond(ctx, claim.body, attachments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - brain 挂了要落成 failed，不能静默
            code = classify_brain_error(exc)
            if self._metrics is not None:
                self._metrics.count("brain_failed")
            log.exception("brain 处理失败：thread=%s code=%s", claim.thread_id, code)
            await self._safe_status(claim.id, "failed", error_code=code)
            await self._notice_failure(claim)
            return

        text = str(getattr(reply, "text", "") or "").strip()
        sticker = getattr(reply, "sticker", None) if reply is not None else None
        sticker_error = str(getattr(reply, "sticker_error", "") or "")
        if sticker_error:
            # 引用没解析成功：正文里已经换成可见说明，这里只留审计（AC-17）
            try:
                await asyncio.to_thread(
                    self._store.audit, "sticker_rejected", "message", str(claim.id),
                    {"error": sticker_error, "raw": str(getattr(reply, "sticker_raw", "") or "")},
                )
            except MailboxError:
                pass
        if not text and sticker is None:
            await self._safe_status(claim.id, "skipped", skip_reason="empty_reply")
            return
        out_attachments = (
            [{"kind": "image", "path": sticker.path, "name": sticker.name,
              "sticker_id": sticker.id}]
            if sticker else []
        )
        requested_kind = "image" if sticker else "text"
        effective_kind = requested_kind
        if sticker is not None:
            capability = self._image_capability(Platform(claim.platform))
            if capability != "supported":
                # 平台不能发图（或不确认支持）：按文字发，并把原因写进注入记录
                effective_kind = "text"
                out_attachments = []
                text = f"{text}{sticker.degrade_text()}".strip()
                context = dict(getattr(reply, "context", None) or {})
                context["sticker_degrade"] = f"image_{capability}"
                reply.context = context
            else:
                context = dict(getattr(reply, "context", None) or {})
        try:
            context = dict(getattr(reply, "context", None) or {})
            if self._metrics is not None:
                self._metrics.add_tokens(
                    input_tokens=int(context.get("estimated_tokens") or 0),
                    output_tokens=len(text), estimated=True,
                )
            await asyncio.to_thread(
                self._store.save_reply, claim, text,
                attachments_json=json.dumps(out_attachments, ensure_ascii=False),
                requested_kind=requested_kind, effective_kind=effective_kind,
                context=context,
                persona_version=int(context.get("persona_version") or 0) or None,
                boundary_version=claim.boundary_version,
            )
        except BoundaryChanged:
            # 生成期间这个人被合并/撤销了：这条回复不能再发给原来那个对象（§7.3.5）
            log.warning("人物归属在生成期间变化，丢弃过期回复：message=%s", claim.id)
            await self._safe_status(claim.id, "skipped", skip_reason="boundary_changed")
            try:
                await asyncio.to_thread(
                    self._store.audit, "reply_dropped", "message", str(claim.id),
                    {"reason": "boundary_changed"},
                )
            except MailboxError:
                pass
            return
        except MailboxError:
            self._db_down = True
            log.exception("保存候选回复失败：%s", claim.id)
            await self._safe_status(claim.id, "failed", error_code="reply_not_saved")
            return
        if (self._facts is not None and cfg is not None and cfg.memory.facts_enabled
                and claim.person_id):
            self._facts.schedule(person_id=claim.person_id, message_id=claim.id,
                                 text=f"{claim.body}\n---\n{text}")
        self._outbox_wake.set()

    def _mark_needs_human(self, platform: Platform, reason: str) -> None:
        """验证码/滑块：账号转 needs_human，暂停该通道自动动作（AC-25）。"""
        account_id = self._store.account_id(platform.value)
        if account_id is None:
            return
        try:
            self._store.set_account_connection(account_id, "needs_human", reason)
            self._store.audit("needs_human", "account", str(account_id),
                              {"platform": platform.value, "reason": reason})
        except MailboxError:
            log.exception("写入 needs_human 状态失败")

    async def _notice_failure(self, claim: ClaimedMessage) -> None:
        """brain 挂了时说一句，别让对方对着空气等。同一对话 90 秒只提一次。"""
        now = time.monotonic()
        if now - self._fail_notice_at.get(claim.thread_id, 0.0) < _FAIL_NOTICE_COOLDOWN:
            return
        self._fail_notice_at[claim.thread_id] = now
        await self.send(
            Platform(claim.platform), ChatKind(claim.kind), claim.chat_id,
            _FAIL_NOTICE, reply_to=claim.platform_msg_id,
        )

    # ---- 发送 worker ----

    async def _outbox_worker(self) -> None:
        while True:
            try:
                task = await asyncio.to_thread(
                    self._store.claim_outbox, self._owner, lease_seconds=_LEASE_SECONDS
                )
            except asyncio.CancelledError:
                raise
            except MailboxError:
                self._db_down = True
                log.exception("领取发送任务失败")
                await asyncio.sleep(1.0)
                continue
            if task is None:
                try:
                    await asyncio.wait_for(self._outbox_wake.wait(), timeout=_POLL_INTERVAL)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                self._outbox_wake.clear()
                continue
            await self._deliver(task)

    async def _deliver(self, task: OutboxTask) -> None:
        started = time.monotonic()
        try:
            await self._deliver_once(task)
        finally:
            if self._metrics is not None:
                self._metrics.observe("send", time.monotonic() - started)

    async def _deliver_once(self, task: OutboxTask) -> None:
        adapter = self._adapters.get(Platform(task.platform))
        account_id = self._store.account_id(task.platform)
        account = self._store.get_account(account_id) if account_id is not None else None
        if adapter is None or account is not None and account["connection_state"] == "needs_human":
            await self._finish(task, "failed", error_code="adapter_unavailable")
            return
        out = OutboundMessage(
            platform=Platform(task.platform), kind=ChatKind(task.kind),
            chat_id=task.chat_id, text=task.text, reply_to=task.reply_to,
            attachments=_decode_attachments(task.attachments_json),
        )
        try:
            result = await adapter.send(out)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 平台侧结果不可知
            log.exception("发送异常，结果不明：outbox=%s", task.outbox_id)
            await self._finish(task, "unknown", error_code="send_exception",
                               error_detail_safe=type(exc).__name__)
            return
        if result.ok:
            if task.requested_kind == "image" and task.effective_kind == "text":
                # 按文字发成功了：记录降级原因，界面上要能看见（FR-4.5）
                capability = self._image_capability(Platform(task.platform))
                await self._finish(task, "degraded", error_code=f"image_{capability}",
                                   error_detail_safe="平台不能发图，已按文字降级发送")
            else:
                await self._finish(task, "sent", platform_msg_id=result.msg_id)
        else:
            await self._finish(task, "failed", error_code="send_rejected",
                               error_detail_safe=result.error)

    async def _finish(self, task: OutboxTask, state: str, *, platform_msg_id: str = "",
                      error_code: str | None = None, error_detail_safe: str | None = None) -> None:
        try:
            if self._metrics is not None:
                self._metrics.count(state)
            await asyncio.to_thread(
                self._store.finish_outbox, task.outbox_id, state,
                platform_msg_id=platform_msg_id, error_code=error_code,
                error_detail_safe=error_detail_safe,
            )
            health = {"sent": "success", "degraded": "success", "failed": "failed",
                      "unknown": "unknown"}.get(state, "unknown")
            await asyncio.to_thread(
                self._store.set_send_health, task.account_id, health,
                self._safe_send_note(state, error_code, error_detail_safe),
            )
        except MailboxError:
            self._db_down = True
            log.exception("写发送结果失败：outbox=%s", task.outbox_id)

    @staticmethod
    def _safe_send_note(state: str, error_code: str | None,
                        detail: str | None) -> str:
        """发送健康里给本人看的可读原因：截断 + 去掉凭据类字样（FR-2.8）。"""
        if state in ("sent", "degraded"):
            return ""
        note = (detail or error_code or "")[:200]
        lowered = note.lower()
        if any(bad in lowered for bad in ("token", "cookie", "sessdata", "bili_jct",
                                          "authorization", "secret")):
            return error_code or "send_failed"
        return note

    # ---- 测试/脚本用的同步驱动 ----

    async def drain(self, *, timeout: float = 30.0) -> None:
        """把当前积压处理完。生产里由 worker 干；这里用同一套 handler 串行驱动。"""
        if self._started:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if self._inflight == 0 and not await asyncio.to_thread(self._store.has_pending_work):
                    return
                await asyncio.sleep(0.02)
            raise TimeoutError("等待消息处理完成超时")
        while True:
            claim = await asyncio.to_thread(
                self._store.claim_message, self._owner, lease_seconds=_LEASE_SECONDS
            )
            if claim is None:
                break
            started = time.monotonic()
            try:
                await self._handle(claim)
            finally:
                if self._metrics is not None:
                    self._metrics.observe("inbound", time.monotonic() - started)
        while True:
            task = await asyncio.to_thread(
                self._store.claim_outbox, self._owner, lease_seconds=_LEASE_SECONDS
            )
            if task is None:
                break
            await self._deliver(task)

    # ---- 主动发送（旧 /send 与运维提示） ----

    async def send(
        self,
        platform: Platform,
        kind: ChatKind,
        chat_id: str,
        text: str,
        *,
        reply_to: str = "",
        attachments: list[Attachment] | None = None,
    ) -> SendResult:
        """直接发送，不经过 outbox。只给已弃用的 /send 和运行期提示用。"""
        adapter = self._adapters.get(platform)
        if adapter is None:
            return SendResult(ok=False, error=f"没有 {platform.value} 的适配器")
        out = OutboundMessage(
            platform=platform, kind=kind, chat_id=chat_id, text=text,
            reply_to=reply_to, attachments=attachments or [],
        )
        try:
            return await adapter.send(out)
        except Exception as exc:  # noqa: BLE001
            log.exception("发送失败: %s/%s", platform.value, chat_id)
            return SendResult(ok=False, error=str(exc))

    async def video_brief(self, platform: Platform, ref: str, *, max_comments: int = 20) -> dict:
        """看视频（抖音走浏览器、B站走公开接口，统一从控制面进来）。"""
        adapter = self._adapters.get(platform)
        if adapter is None:
            return {"ok": False, "error": f"没有 {platform.value} 的适配器"}
        fn = getattr(adapter, "video_brief", None)
        if fn is None:
            return {"ok": False, "error": f"{platform.value} 目前不支持看视频"}
        try:
            return await fn(ref, max_comments=max_comments)
        except Exception as exc:  # noqa: BLE001 - 工具出错要回文本，不能让控制面崩
            log.exception("看视频失败: %s", platform.value)
            return {"ok": False, "error": str(exc)}
