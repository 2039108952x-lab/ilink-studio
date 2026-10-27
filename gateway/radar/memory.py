"""把上游 ``douyin_im_read`` 里的 ``context`` 映射进现有 person / memory 体系。

不另造一套记忆的原因
    网关已经有 ``MemoryStore``：人格、长期事实、窗口、跨平台摘要都在里面。
    抖音那边自带一份「我记得这个人」的 facts / recent，如果原样再存一份，
    同一个人的两套记忆迟早对不上。

映射规则
    上游 ``context.facts``（这个人的长期事实）→ ``memory_fact``，kind 用 ``profile``。
    上游 ``context.recent``（最近几轮原话）不落库，它只是窗口的镜像；要读历史
    一律走 ``MemoryStore.window``，避免同一句话存两遍。
    重复的 fact 用 ``has_fact`` 挡住，所以同一轮 sync 多次也不会长出一堆副本。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FACT_CHARS = 80
_CONTROL_RE = re.compile(r"[\r\n\t]+")


@dataclass(slots=True)
class RadarContext:
    """一个人的抖音上下文快照。"""

    facts: list[str] = field(default_factory=list)
    recent: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"facts": list(self.facts), "recent": list(self.recent),
                "rounds": self.rounds, "error": self.error}


def clean_fact(text: str) -> str:
    """压成一行、削掉控制字符、超长截断。事实要短，长了就进不了 prompt。"""
    flat = _CONTROL_RE.sub(" ", str(text or "")).strip()
    flat = re.sub(r"\s{2,}", " ", flat)
    if len(flat) > MAX_FACT_CHARS:
        flat = flat[: MAX_FACT_CHARS - 1].rstrip() + "…"
    return flat


def dedupe_facts(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        fact = clean_fact(raw)
        if not fact:
            continue
        key = fact.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def parse_context(payload: Any) -> RadarContext:
    """从 im_read 的返回值里抠出 context。结构不对就当没有，绝不抛。"""
    if not isinstance(payload, dict):
        return RadarContext(error="返回值不是对象")
    node = payload.get("context")
    if node is None:
        # 上游有时把 facts 摊在顶层
        if "facts" in payload:
            node = payload
        else:
            return RadarContext()
    if not isinstance(node, dict):
        return RadarContext(error="context 结构不认识")
    facts = node.get("facts")
    if isinstance(facts, str):
        facts = [facts]
    fact_list = dedupe_facts([str(x) for x in facts]) if isinstance(facts, list) else []
    recent = node.get("recent")
    recent_list = [r for r in recent if isinstance(r, dict)] if isinstance(recent, list) else []
    try:
        rounds = int(node.get("rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return RadarContext(facts=fact_list, recent=recent_list, rounds=max(0, rounds))


class RadarMemory:
    """同一个人：上游 facts ↔ 本地 memory_fact 的双向搬运。"""

    def __init__(self, store: Any, *, kind: str = "profile", confidence: float = 0.6) -> None:
        self._store = store
        self._kind = kind
        self._confidence = confidence

    def sync(self, person_id: int, ctx: RadarContext) -> dict[str, Any]:
        """把上游带来的事实写进本地；已存在的不重复写。

        返回 ``{"added": n, "skipped": m, "error": ""}``，调用方据此决定要不要提示。
        """
        result: dict[str, Any] = {"added": 0, "skipped": 0, "error": ""}
        if self._store is None:
            result["error"] = "没有可用的记忆库"
            return result
        for fact in ctx.facts:
            try:
                if self._store.has_fact(person_id, self._kind, fact):
                    result["skipped"] += 1
                    continue
                self._store.add_fact(person_id, self._kind, fact, confidence=self._confidence)
                result["added"] += 1
            except Exception as exc:  # noqa: BLE001 - 人物不存在之类，记下来别炸
                result["error"] = str(exc)[:200]
                break
        return result

    def snapshot(self, person_id: int, *, limit: int = 12) -> RadarContext:
        """从本地事实还原一份 context，给「回话前先看它」用。"""
        if self._store is None:
            return RadarContext(error="没有可用的记忆库")
        try:
            rows = self._store.facts(person_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return RadarContext(error=str(exc)[:200])
        facts = dedupe_facts([getattr(r, "content", "") for r in rows])
        return RadarContext(facts=facts, rounds=len(facts))


DM_FACT_LIMIT = 20              # 与上游 lib/memory.mjs 的 MAX_FACTS 一致
DM_MESSAGE_LIMIT = 80           # 与上游的 MAX_MESSAGES 一致


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def safe_key(value: str) -> str:
    """把 sec_uid / aweme_id 变成安全文件名。

    规则必须和上游 ``lib/memory.mjs`` 的 ``safeKey`` 逐字一致：先剔非法字符，
    再截断 64 位，空的落 ``unknown``。差一个字符，上游就读不到我们写的记忆。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
    return cleaned[:64] or "unknown"


def read_dm_memory(home: Path | str, key: str) -> dict[str, Any] | None:
    """读一个人的私信记忆（``memory/dm-<safe_key>.json``）。读不到或坏掉返回 None。"""
    if not str(key or "").strip():
        return None
    path = Path(home) / "memory" / f"dm-{safe_key(key)}.json"
    data = _read_json_file(path)
    return data if isinstance(data, dict) else None


def context_of(raw: Any) -> dict[str, Any]:
    """把记忆文件转成上游 ``douyin_im_read`` 的 ``context`` 口径，给界面和 prompt 用。"""
    empty: dict[str, Any] = {"who": {}, "rounds": 0, "facts": [],
                             "recent": [], "total_remembered": 0}
    if not isinstance(raw, dict):
        return empty
    who = raw.get("who") if isinstance(raw.get("who"), dict) else {}
    facts = dedupe_facts([x for x in (raw.get("facts") or []) if isinstance(x, str)])
    messages = [m for m in (raw.get("messages") or [])
                if isinstance(m, dict) and m.get("text")]
    try:
        rounds = int(raw.get("rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0
    return {
        "who": dict(who),
        "rounds": max(0, rounds),
        "facts": facts,
        "recent": messages[-5:],
        "total_remembered": len(messages),
    }


def sync_facts(store: Any, person_id: int, raw: Any, *,
               confidence: float = 0.6, kind: str = "profile") -> dict[str, Any]:
    """把记忆文件里的 facts 搬进本地 ``MemoryStore``。

    函数式入口，等价于 ``RadarMemory.sync``；``raw`` 就是 ``read_dm_memory`` 的返回。
    去重靠 ``store.facts()`` 现有内容（大小写不敏感），不依赖 store 有没有 ``has_fact``。

    返回 ``{added, skipped, planned, contents, error}``：
    ``planned`` 是去重后打算写的条数，``contents`` 是真正写进去的那些。
    """
    result: dict[str, Any] = {"added": 0, "skipped": 0, "planned": 0,
                              "contents": [], "error": ""}
    if store is None:
        result["error"] = "没有可用的记忆库"
        return result
    facts = context_of(raw)["facts"]
    result["planned"] = len(facts)
    if not facts:
        return result
    known: set[str] = set()
    try:
        rows = store.facts(person_id) if hasattr(store, "facts") else []
        known = {str(getattr(r, "content", "") or "").strip().lower() for r in rows}
    except Exception:  # noqa: BLE001 - 读不到就当没有，别让去重把整轮搞挂
        known = set()
    for fact in facts:
        if fact.strip().lower() in known:
            result["skipped"] += 1
            continue
        try:
            store.add_fact(person_id, kind, fact, confidence=confidence)
        except Exception as exc:  # noqa: BLE001 - 人物不存在之类，记下来别炸
            result["error"] = str(exc)[:200]
            break
        result["added"] += 1
        result["contents"].append(fact)
        known.add(fact.strip().lower())
    return result


def remember_from_message(home: Path | str, to: str, text: str) -> dict[str, Any]:
    """把一条消息追加进上游的私信记忆文件，让它下次读会话时自己带上。

    写进 ``messages``（``{at, from, text, kind}``），**不动 facts** ——
    facts 是长期事实，由 ``sync_facts`` 单独搬进本地 MemoryStore，两件事别混。

    三种情况都认：
        ① ``to`` 是 sec_uid ⇒ 文件名直接对得上（``dm-<safe_key>.json``）
        ② ``to`` 是昵称 ⇒ 按 ``who.nick`` 反查
        ③ 都没有 ⇒ **新建一份**：手里只有昵称也得能记，不能因为文件不存在就丢掉

    写的结构严格照上游 ``lib/memory.mjs``：``{who:{sec_uid,nick}, updatedAt,
    rounds, facts[≤20], messages[≤80]}`` —— 形状不对，上游读会话时就带不上。
    """
    folder = Path(home) / "memory"
    target = str(to or "").strip()
    fact = clean_fact(text)
    if not target:
        return {"ok": False, "error": "没给收件人"}
    if not fact:
        return {"ok": False, "error": "事实为空"}
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"建不了记忆目录：{exc}", "file": str(folder)}

    direct = folder / f"dm-{safe_key(target)}.json"
    path: Path | None = direct if direct.is_file() else None
    if path is None:
        for candidate in sorted(folder.glob("dm-*.json")):
            data = _read_json_file(candidate)
            if (isinstance(data, dict)
                    and str((data.get("who") or {}).get("nick") or "").strip() == target):
                path = candidate
                break

    if path is None:
        path = direct
        data: dict[str, Any] = {}
    else:
        loaded = _read_json_file(path)
        data = loaded if isinstance(loaded, dict) else {}

    who = data.get("who")
    if not isinstance(who, dict):
        who = {}
    who.setdefault("sec_uid", target)
    who.setdefault("nick", "")
    data["who"] = who
    data.setdefault("rounds", 0)
    data.setdefault("messages", [])

    messages = data.get("messages")
    messages = [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    if any(str(m.get("text") or "").strip() == fact for m in messages):
        return {"ok": True, "file": str(path), "written": 0}
    messages.append({"at": _now_iso(), "from": "me", "text": fact, "kind": "text"})
    data["messages"] = messages[-DM_MESSAGE_LIMIT:]
    data["updatedAt"] = _now_iso()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return {"ok": False, "error": f"写不进去：{exc}", "file": str(path)}
    return {"ok": True, "file": str(path), "written": 1}
