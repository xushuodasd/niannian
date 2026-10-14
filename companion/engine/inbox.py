"""消息收件箱（3. 架构图 / 4.6）：**所有消息先进收件箱**，再由生命状态决定怎么处理。

为什么要有它：真人的消息不是"到达即处理"，而是**排队、积压、合并**。
收件箱是"慢层闸门"的落地处，也是"连发合并理解"（4.11）的入口。
"""
from __future__ import annotations

from datetime import datetime

from .types import InboxItem, InboxStatus, IncomingMessage


class Inbox:
    """单个 (life_id, user_id) 的收件箱。"""

    def __init__(self, merge_window_s: float = 30.0) -> None:
        self._items: list[InboxItem] = []
        self._merge_window_s = merge_window_s
        self._last_ts: datetime | None = None
        self._group = ""

    # ---- 写入 ----------------------------------------------------------
    def push(self, incoming: IncomingMessage, *, merge: bool = True) -> InboxItem:
        """入队。``merge=True`` 时，与上一条间隔 < 窗口 的消息归为同一"连发组"。"""
        if merge and self._last_ts is not None:
            gap = (incoming.received_at - self._last_ts).total_seconds()
            if gap <= self._merge_window_s:
                incoming.merge_group = self._group or incoming.merge_group
        self._group = incoming.merge_group
        self._last_ts = incoming.received_at
        item = InboxItem(incoming=incoming, merge_group=incoming.merge_group)
        self._items.append(item)
        return item

    # ---- 读取 ----------------------------------------------------------
    def queued(self) -> list[InboxItem]:
        return [i for i in self._items if i.status == InboxStatus.QUEUED]

    @property
    def backlog_count(self) -> int:
        return len(self.queued())

    def peek(self) -> list[dict]:
        return [
            {
                "id": i.id,
                "content": i.incoming.content,
                "status": i.status.value,
                "received_at": i.received_at.isoformat(),
            }
            for i in self._items
        ]

    def take_batch(self) -> list[IncomingMessage]:
        """取出全部排队消息（作为**一批**交给会话合并理解）并标记已投递。"""
        batch = [i for i in self._items if i.status == InboxStatus.QUEUED]
        for i in batch:
            i.status = InboxStatus.DELIVERED
        self._items = [i for i in self._items if i.status == InboxStatus.QUEUED]
        return [i.incoming for i in batch]

    def drain_backlog(self) -> list[IncomingMessage]:
        """积压补看：把排队消息全部取出（AFK 回来 / 唤醒后统一补看）。"""
        return self.take_batch()

    def drop_all(self) -> int:
        """已读不回：丢弃全部排队消息，返回条数。"""
        n = len(self.queued())
        for i in self._items:
            i.status = InboxStatus.DROPPED
        self._items = [i for i in self._items if i.status == InboxStatus.QUEUED]
        return n

    def clear(self) -> None:
        self._items.clear()
        self._last_ts = None
        self._group = ""