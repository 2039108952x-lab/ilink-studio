"""适配器检查点：游标与最小去重状态落在 ``adapter_checkpoint``。

为什么要进库
------------
游标和消息必须在同一个一致性流程里推进，否则会出现「游标已前进但消息没落库」。
检查点因此和消息共用 ``mailbox.db``，而不是各平台自己的 JSON 文件。

凭据绝不进这里：``context_token``、cookie、token 一律由 Vault（DPAPI）保管，
``Mailbox.save_checkpoint`` 会拒收凭据类字段（FR-1.5）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .mailbox import Mailbox

_UNSET: Any = object()


class Checkpoint:
    def __init__(self, store: Mailbox, account_id: int, stream: str) -> None:
        self._store = store
        self._account_id = account_id
        self._stream = stream

    @property
    def stream(self) -> str:
        return self._stream

    def load(self) -> dict[str, Any]:
        return self._store.load_checkpoint(self._account_id, self._stream)

    def cursor(self) -> str:
        return str(self.load().get("cursor") or "")

    def primed(self) -> bool:
        return bool(self.load().get("primed"))

    def state(self) -> dict[str, Any]:
        raw = self.load().get("state")
        return dict(raw) if isinstance(raw, Mapping) else {}

    def save(self, *, cursor: str | None = _UNSET,
             state: Mapping[str, Any] | None = _UNSET,
             primed: bool | None = _UNSET) -> None:
        self._store.save_checkpoint(
            self._account_id, self._stream,
            cursor=None if cursor is _UNSET else cursor,
            state=None if state is _UNSET else dict(state),
            primed=None if primed is _UNSET else bool(primed),
        )
