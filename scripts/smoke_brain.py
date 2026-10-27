"""端到端冒烟：用真实的 reasonix CLI 跑一轮 brain。

这是唯一会真的花钱的脚本，跑一次几条消息的量级。
默认 dry 模式只打印将要执行的命令，加 --live 才真调模型。

    python scripts/smoke_brain.py            # 只打印命令
    python scripts/smoke_brain.py --live     # 真调

注：Windows 控制台默认 GBK，输出一律用 ASCII 标记，避免 UnicodeEncodeError。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.brain import BrainUnavailable, ReasonixBrain  # noqa: E402
from gateway.config import BrainConfig  # noqa: E402
from gateway.mailbox import Incoming, Mailbox  # noqa: E402
from gateway.models import PromptContext  # noqa: E402


def safe(text: str) -> str:
    """GBK 控制台编不了 emoji，打日志前先压一遍。"""
    enc = sys.stdout.encoding or "utf-8"
    return text.encode(enc, "replace").decode(enc, "replace")


def resolve_exe() -> str:
    """找 npm 全局装的 reasonix。"""
    for cand in ("reasonix", "reasonix.cmd"):
        found = shutil.which(cand)
        if found:
            return found
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        guess = Path(appdata) / "npm" / "reasonix.cmd"
        if guess.is_file():
            return str(guess)
    return ""


async def main(live: bool) -> int:
    exe = resolve_exe()
    if not exe:
        print("[x] 找不到 reasonix 命令", file=sys.stderr)
        return 2
    print(f"[ok] CLI: {exe}")

    root = Path(tempfile.mkdtemp())
    store = Mailbox(root / "mailbox.db")
    account_id = store.ensure_account("qq", "onebot", display_name="冒烟测试")
    cfg = BrainConfig(
        command=exe,
        workspace=str(Path(__file__).resolve().parent.parent),
        timeout=180.0,
        permission_mode="auto",
        max_steps=6,
    )
    brain = ReasonixBrain(cfg, store)

    turns = [
        "只回答两个字：收到。",
        "我刚才让你回答哪两个字？只说出那两个字。",
    ]

    if not live:
        print("\n--- dry run, 将执行 ---")
        print(" $", " ".join(brain.build_args()), "  < prompt 走 stdin")
        for t in turns:
            print("   我：", safe(t))
        print("\n加 --live 才会真的调用。")
        return 0

    key = "smoke:private:test"
    for i, text in enumerate(turns, 1):
        print(f"\n[{i}] 我：{text}")
        try:
            reply = await brain.respond(key, text)
        except BrainUnavailable as exc:
            print(f" [x] {exc}", file=sys.stderr)
            return 1
        print(f"     你：{safe(reply.text)}")
        if reply.sticker:
            print(f"     [表情] {reply.sticker.name}")

    hist = store.history(key)
    print(f"\n[ok] 会话记下 {len(hist)} 轮")
    if len(hist) >= 2:
        preview = brain.build_prompt(hist[:-1], turns[-1])
        print("[ok] 第二轮 prompt 里带上了第一轮：")
        print("   " + preview.replace("\n", "\n   ")[:300])
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="真的调用模型（会产生费用）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.live)))
