"""召回接口（FTS5 → 后期可换向量，上层零改动）。

设计（9.2 F）：Demo 用 SQLite FTS5 关键词 + **重要性 × 时间衰减** 打分，
抽象成 ``Retriever`` 协议，后期换 ``sqlite-vec`` 时只改这一处。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol

from ..engine.types import now_dt
from ..store.db import Database


class RecallHit(dict):
    """一条召回结果：``{text, score, source, ts}``。"""


class Retriever(Protocol):
    async def recall(
        self, life_id: str, user_id: str, query: str, k: int = 6
    ) -> list[RecallHit]: ...


class KeywordRetriever:
    """关键词召回：FTS5 命中，再按 重要性 + 时间衰减 重排。

    ``score = 基础命中分 × importance × exp(-Δt / 半衰期)``
    """

    def __init__(self, db: Database, *, half_life_days: float = 7.0) -> None:
        self._db = db
        self._half_life = max(0.5, half_life_days)

    def _decay(self, ts_iso: str, now: datetime) -> float:
        try:
            ts = datetime.fromisoformat(ts_iso)
        except Exception:
            return 0.5
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        days = max(0.0, (now - ts).total_seconds() / 86400.0)
        return math.exp(-days / self._half_life)

    async def recall(self, life_id: str, user_id: str, query: str, k: int = 6) -> list[RecallHit]:
        now = now_dt()
        hits: list[RecallHit] = []

        # 1) 长期事实（重要性敏感，权重更高）
        for row in await self._db.search_text("fact", life_id, user_id, query, limit=k * 2):
            imp = float(row.get("importance", 0.5))
            score = 1.4 * (0.4 + imp) * self._decay(row.get("last_seen", ""), now)
            hits.append(RecallHit(
                text=f"{row['key']}：{row['value']}", score=score,
                source="fact", ts=row.get("last_seen", ""),
            ))

        # 2) 历史消息（往事）
        for row in await self._db.search_text("message", life_id, user_id, query, limit=k * 2):
            role = "对方" if row.get("role") == "user" else "你"
            score = 1.0 * self._decay(row.get("created_at", ""), now)
            hits.append(RecallHit(
                text=f"{role}说过：{row.get('content', '')[:60]}", score=score,
                source="message", ts=row.get("created_at", ""),
            ))

        hits.sort(key=lambda h: float(h["score"]), reverse=True)
        # 去重（同一句别重复出现）
        seen: set[str] = set()
        out: list[RecallHit] = []
        for h in hits:
            key = str(h["text"])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= k:
                break
        return out