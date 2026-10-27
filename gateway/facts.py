"""长期事实抽取（FR-3.7 / §8.2 / AC-18）。

主回复成功后跑一次轻量调用（`--max-steps 1`），只接受严格 JSON；
模型不能指定 person_id，也不能把内容标成高可信。失败只记录，不影响已发出的回复。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

FACT_KINDS = frozenset({"event", "preference", "promise", "meme", "profile"})
MAX_FACTS = 5
MAX_CONTENT = 200
MAX_CONFIDENCE = 0.8

EXTRACT_PROMPT = """从下面这段聊天里，抽取值得长期记住的事实。只输出 JSON，不要任何解释：
{{"facts":[{{"kind":"event|preference|promise|meme|profile","content":"一句话","confidence":0.0}}]}}

规则：
1. 最多 {limit} 条；没有值得记的就输出 {{"facts":[]}}。
2. content 只写事实本身，不写「用户说」；不超过 {chars} 字。
3. 不要编造，不要复述整段对话。
4. confidence 最高 {confidence}。

聊天：
{text}
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_facts(raw: str, *, limit: int = MAX_FACTS) -> list[dict[str, Any]]:
    """严格解析：不是 JSON、schema 不对、内容空一律丢；最多 limit 条。"""
    text = str(raw or "").strip()
    if not text:
        return []
    match = _JSON_RE.search(text)
    if match is None:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        content = str(item.get("content") or "").strip()
        if kind not in FACT_KINDS or not content:
            continue
        if len(content) > MAX_CONTENT:
            content = content[:MAX_CONTENT]
        try:
            confidence = float(item.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = max(0.1, min(MAX_CONFIDENCE, confidence))
        result.append({"kind": kind, "content": content, "confidence": confidence})
        if len(result) >= limit:
            break
    return result


class FactExtractor:
    """后台抽取；返回新写入的事实 id。任何失败都只记日志与指标。"""

    def __init__(self, brain, memory, *, metrics=None, limit: int = MAX_FACTS,
                 max_chars: int = 2000) -> None:
        self._brain = brain
        self._memory = memory
        self._metrics = metrics
        self._limit = limit
        self._max_chars = max_chars

    async def extract(self, *, person_id: int, message_id: int, text: str) -> list[int]:
        if not person_id or not str(text or "").strip():
            return []
        prompt = EXTRACT_PROMPT.format(
            limit=self._limit, chars=MAX_CONTENT, confidence=MAX_CONFIDENCE,
            text=str(text)[: self._max_chars],
        )
        try:
            raw = await self._brain.extract_json(prompt)
        except Exception:  # noqa: BLE001 - 抽取失败不能影响主回复
            log.warning("事实抽取调用失败", exc_info=True)
            if self._metrics is not None:
                self._metrics.count("facts_failed")
            return []
        if self._metrics is not None:
            self._metrics.add_tokens(
                input_tokens=len(prompt), output_tokens=len(str(raw or "")), estimated=True,
            )
        created: list[int] = []
        for item in parse_facts(raw, limit=self._limit):
            try:
                if self._memory.has_fact(person_id, item["kind"], item["content"]):
                    continue
                created.append(self._memory.add_fact(
                    person_id, item["kind"], item["content"],
                    source_message_id=message_id, confidence=item["confidence"],
                ))
            except Exception:  # noqa: BLE001 - 单条失败不影响其他事实
                log.debug("写入事实失败", exc_info=True)
        if created and self._metrics is not None:
            self._metrics.count("facts_extracted", len(created))
        return created

    def schedule(self, *, person_id: int, message_id: int, text: str) -> None:
        """后台跑，不阻塞发送；没有事件循环时直接跳过。"""
        try:
            asyncio.get_running_loop().create_task(
                self.extract(person_id=person_id, message_id=message_id, text=text)
            )
        except RuntimeError:
            return
