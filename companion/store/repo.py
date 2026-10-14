"""运行时状态仓库：psyche / relationship / life_state 的读写（JSON 快照）。

人格（``LifeRecord``）走 ``LifeStore`` 文件；**随交互变化的心理与关系状态**走这里，
落 SQLite 的 ``state`` 表 —— 重启不丢"我们相处到什么程度了"。
"""
from __future__ import annotations

from datetime import datetime

from ..cognition.llm import LLMGateway  # noqa: F401  (类型提示便利，不产生循环)
from ..engine.types import LifeRecord, PsycheState, Relationship, now_dt
from ..mind import psyche as psyche_mod
from .db import Database


class StateRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 心理状态 ------------------------------------------------------
    async def load_psyche(self, life: LifeRecord, now: datetime | None = None) -> PsycheState:
        raw = await self._db.load_state("life", life.life_id, "psyche")
        if not raw:
            return psyche_mod.init_psyche(life, now)
        try:
            return PsycheState(**raw)
        except Exception:
            return psyche_mod.init_psyche(life, now)

    async def save_psyche(self, life_id: str, state: PsycheState) -> None:
        await self._db.save_state("life", life_id, "psyche", state.model_dump(mode="json"))

    # ---- 关系 ----------------------------------------------------------
    async def load_relationship(self, life_id: str, user_id: str) -> Relationship:
        raw = await self._db.load_state("rel", life_id, "relationship", user_id=user_id)
        if not raw:
            return psyche_mod.new_relationship()
        try:
            return Relationship(**raw)
        except Exception:
            return psyche_mod.new_relationship()

    async def save_relationship(self, life_id: str, user_id: str, rel: Relationship) -> None:
        await self._db.save_state("rel", life_id, "relationship", rel.model_dump(mode="json"), user_id=user_id)

    # ---- 生命状态 ------------------------------------------------------
    async def load_life_state(self, life_id: str) -> dict:
        return await self._db.load_state("life", life_id, "presence") or {}

    async def save_life_state(self, life_id: str, data: dict) -> None:
        payload = dict(data)
        payload.setdefault("saved_at", now_dt().isoformat())
        await self._db.save_state("life", life_id, "presence", payload)