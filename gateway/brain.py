"""brain：把一句话交给 Reasonix，拿回一句回答。

设计取舍
--------
Reasonix 的 `-p` 是一次性无状态调用，网关这边必须自己扛上下文。
做法是每个 session_key 维护一份最近 N 轮历史，拼进 prompt 再发。
这样不依赖 CLI 的 session 语义，CLI 换版本也不会坏。

真机上的延迟来源就是这里：每条消息都要起一次 agent 循环。

图像/表情包
-----------
CLI 只吃文本，所以图片走两条路：
- 收：图片先落到工作区 `inbox/`，prompt 里给出相对路径，让 agent 自己读；
- 发：模型在回复末尾写 `[[表情:文件名]]`，网关剥掉标记后单独发一张图。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import BrainConfig
from .mailbox import Mailbox
from .memory import MemoryStore
from .models import Attachment, PromptContext
from .prompt import DEFAULT_STICKER_LIMIT, PromptAssembler
from .stickers import STICKER_REF_RE, Sticker, StickerStore

log = logging.getLogger(__name__)

# 消息里出现这些就当「用户在聊某个视频」，给它工具轮，好让 bili_video 有机会被调。
# 纯闲聊仍然只给 1 步：快 3 倍，也不容易跑偏。
_VIDEO_REF_RE = re.compile(
    r"BV[0-9A-Za-z]{10}|(?:^|[^0-9A-Za-z])av\d+|bilibili\.com/video",
    re.IGNORECASE,
)

# prompt 的排版、常量与预算裁剪都在 gateway/prompt.py；这里只管调用 CLI。

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@dataclass(slots=True)
class Reply:
    """一次回答：正文 + 可选的表情 + 这次注入了什么。"""

    text: str = ""
    sticker: Sticker | None = None
    sticker_error: str = ""
    sticker_raw: str = ""
    context: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.text or self.sticker)


@dataclass(slots=True)
class StickerResolution:
    text: str
    sticker: Sticker | None = None
    error: str = ""
    raw: str = ""


def resolve_sticker(text: str, store: StickerStore | None) -> StickerResolution:
    """把回复里的表情引用解析成库里的表情（PRD FR-4.4 / AC-17）。

    - 只认库内 ID 或名称；带路径特征的 token 直接拒绝，绝不拿它去读文件。
    - 认不出时**不静默删标记**：换成人能看懂的「（表情「x」不可用）」，并带回错误码，
      由流水线写审计。
    - 一条回复最多发一个表情；多余的引用保留原文并记 ``sticker_multiple``。
    """
    matches = list(STICKER_REF_RE.finditer(text))
    if not matches:
        return StickerResolution(text=text)
    first = matches[0]
    token = first.group(1)
    if store is None:
        return StickerResolution(text=text, error="sticker_unavailable", raw=first.group(0))
    resolved = store.resolve(token)
    if resolved.sticker is None:
        note = f"（表情「{token}」不可用）"
        return StickerResolution(
            text=(text[:first.start()] + note + text[first.end():]).strip(),
            error=resolved.error or "sticker_unknown", raw=first.group(0),
        )
    cleaned = (text[:first.start()] + text[first.end():]).strip()
    return StickerResolution(
        text=cleaned, sticker=resolved.sticker,
        error="sticker_multiple" if len(matches) > 1 else "", raw=first.group(0),
    )


class BrainUnavailable(RuntimeError):
    """CLI 不在、超时、或输出没法解析。"""


# ---------- 表情包 ----------


# ---------- 图片落盘 ----------


def _safe_name(att: Attachment) -> str:
    stem = Path(att.name).stem if att.name else "img"
    stem = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]", "", stem)[:24] or "img"
    digest = hashlib.sha1((att.url or att.path or att.name).encode("utf-8")).hexdigest()[:8]
    suffix = Path(att.name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    return f"{stem}-{digest}{suffix}"


class ReasonixBrain:
    """装 prompt、调 CLI。记忆不再放 JSON：历史从 ``mailbox.db`` 按 thread 读。"""

    def __init__(self, cfg: BrainConfig, store: Mailbox, *,
                 memory: MemoryStore | None = None,
                 stickers: StickerStore | None = None) -> None:
        self._cfg = cfg
        self._store = store
        self._memory = memory or MemoryStore(store)
        self._assembler = PromptAssembler(self._memory)
        self._exe = shutil.which(cfg.command) or cfg.command
        self._sticker_store = stickers

    def reconfigure(self, cfg: BrainConfig) -> None:
        """设置热重载：未找到后端时保持未就绪，不用假模型替代。"""
        self._cfg = cfg
        self._exe = shutil.which(cfg.command) or cfg.command

    @property
    def backend_ready(self) -> bool:
        if not self._cfg.command.strip() or self._cfg.backend != "cli":
            return False
        candidate = Path(self._cfg.command).expanduser()
        return bool(shutil.which(self._cfg.command) or candidate.is_file())

    # ---- prompt 组装 ----

    def build_prompt(self, ctx: PromptContext | None, text: str, *,
                     images: list[str] | None = None) -> str:
        """按 PRD §8.1 装配；排版与裁剪都在 gateway/prompt.py。"""
        return self.assemble(ctx, text, images=images).text

    def assemble(self, ctx: PromptContext | None, text: str, *,
                 images: list[str] | None = None):
        """返回 PromptBundle：正文 + 本次注入了什么（落到出站消息 context_json）。"""
        catalog = (
            self._sticker_store.catalog(limit=DEFAULT_STICKER_LIMIT)
            if self._sticker_store is not None else []
        )
        return self._assembler.build(
            ctx, text, images=images, stickers=catalog,
            include_history=self._cfg.keep_session,
        )

    # ---- 图片 ----

    async def stage_images(self, attachments: list[Attachment]) -> list[str]:
        """把入站图片放到工作区的 inbox/，返回给模型看的相对路径。"""
        if not attachments or not self._cfg.workspace:
            return []
        inbox = Path(self._cfg.workspace) / "inbox"
        staged: list[str] = []
        for att in attachments:
            if att.kind != "image":
                continue
            dest = inbox / _safe_name(att)
            try:
                await self._materialize(att, dest)
            except (OSError, ValueError) as exc:
                log.warning("图片落盘失败：%s", exc)
                continue
            staged.append(f"inbox/{dest.name}")
        return staged

    async def _materialize(self, att: Attachment, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if att.path and Path(att.path).is_file():
            await asyncio.to_thread(shutil.copyfile, att.path, dest)
            return
        if att.url:
            await asyncio.to_thread(self._download, att.url, dest)
            return
        raise ValueError("既没有本地路径也没有 url")

    @staticmethod
    def _download(url: str, dest: Path) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "ilink-gateway/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - 来源是本地协议端
            data = resp.read()
        if not data:
            raise ValueError("下载到空内容")
        dest.write_bytes(data)

    # ---- CLI ----

    def build_args(self, max_steps: int | None = None) -> list[str]:
        """注意：prompt 不在这里，走 stdin。

        Windows 上 `.cmd` shim 必须经 cmd.exe 启动，而 cmd.exe 会把带换行的
        参数截断在第一个换行处——实测模型只收到了 preamble 第一句，答非所问。
        改用 stdin 之后多行 prompt 完整送达。
        """
        args = [self._exe, "run", "--output-format", "json"]
        if self._cfg.model:
            args += ["--model", self._cfg.model]
        if self._cfg.permission_mode:
            args += ["--permission-mode", self._cfg.permission_mode]
        steps = self._cfg.max_steps if max_steps is None else max_steps
        if steps:
            # 实测：max-steps 给到 3，CLI 灌进来 78k tokens 的系统上下文，
            # 回一句闲聊要 30 秒，还会答非所问；给 1 只要 10k tokens / 10 秒，
            # 模型也能正眼看问题了。所以纯文字聊天一律只给 1 轮。
            args += ["--max-steps", str(steps)]
        if self._cfg.workspace:
            from .paths import resolve_data_path
            data_dir = self._store.path.parent if self._store is not None else Path.cwd()
            workspace = resolve_data_path(
                data_dir, self._cfg.workspace, default="im-workspace"
            )
            # CLI 会 chdir 到 --dir；目录不存在会直接退出码 2。
            try:
                workspace.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.warning("工作区目录创建失败，交给 CLI 报错：%s", workspace)
            args += ["--dir", workspace.as_posix()]
        return args

    @staticmethod
    def parse_output(stdout: str) -> str:
        """CLI 的 json 输出可能把答案放在不同字段，逐个试。"""
        raw = stdout.strip()
        if not raw:
            raise BrainUnavailable("CLI 没有输出")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw  # 不是 JSON 就当成纯文本答案
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for field in ("result", "text", "answer", "content", "output", "message"):
                value = data.get(field)
                if isinstance(value, str) and value.strip():
                    return value
        raise BrainUnavailable(f"认不出 CLI 输出结构：{raw[:200]}")

    async def _run_cli(self, prompt: str, max_steps: int | None = None) -> str:
        """CLI 偶发退出码 1（stderr 还是空的），重试一次基本就过了。"""
        if not self.backend_ready:
            raise BrainUnavailable("模型后端未配置或不可执行；请在首启向导中选择 Reasonix")
        last: BrainUnavailable | None = None
        for attempt in range(2):
            try:
                return await self._run_cli_once(prompt, max_steps)
            except BrainUnavailable as exc:
                last = exc
                # 超时、找不到命令这类重试也没用
                if "退出码" not in str(exc) or attempt == 1:
                    raise
                log.warning("CLI 调用失败，重试一次：%s", exc)
                await asyncio.sleep(1.0)
        raise last if last else BrainUnavailable("CLI 没有输出")

    async def _run_cli_once(self, prompt: str, max_steps: int | None = None) -> str:
        if not self._exe:
            raise BrainUnavailable("找不到 reasonix 命令，先 npm i -g reasonix")
        argv = self.build_args(max_steps)
        if os.name == "nt" and self._exe.lower().endswith((".cmd", ".bat")):
            # npm 全局包装出来的是 .cmd shim，Windows 上必须经 cmd.exe 才能跑
            argv = ["cmd", "/c", *argv]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise BrainUnavailable(f"启动 {self._exe} 失败：{exc}") from exc

        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), self._cfg.timeout
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise BrainUnavailable(f"超过 {self._cfg.timeout}s 没有回答") from exc

        if proc.returncode != 0:
            detail = (err.decode("utf-8", "replace") or out.decode("utf-8", "replace")).strip()
            raise BrainUnavailable(f"CLI 退出码 {proc.returncode}：{detail[:400]}")
        return self.parse_output(out.decode("utf-8", "replace"))

    async def extract_json(self, prompt: str) -> str:
        """事实抽取专用：只给 1 轮、不读文件、不带会话历史。"""
        return await self._run_cli(prompt, 1)

    # ---- 对外 ----

    async def respond(
        self, ctx: PromptContext, text: str, attachments: list[Attachment] | None = None
    ) -> Reply:
        images = await self.stage_images(attachments or [])
        if not text.strip():
            text = "（我发了一张图，没有配文字）" if images else "（空消息）"
        bundle = self.assemble(ctx, text, images=images)
        # 有图要工具轮（读文件看图）；提到视频也要（模型得调 bili_video 才知道内容）。
        # 其余纯文字仍给 1 轮：快 3 倍，也不容易跑偏。
        needs_tools = bool(images) or bool(_VIDEO_REF_RE.search(text))
        raw = await self._run_cli(bundle.text, None if needs_tools else 1)
        resolution = resolve_sticker(raw, self._sticker_store)
        return Reply(
            text=resolution.text, sticker=resolution.sticker,
            sticker_error=resolution.error, sticker_raw=resolution.raw,
            context=bundle.context,
        )
