"""生命体注册表（第 13 节 · 多生命体平台）。

每个生命体 = 一份存档（``LifeRecord``）+ 一套独立运行时（psyche / 关系 / 生命状态机）。
一切状态以 ``life_id`` 命名空间隔离，互不干扰；新增/删除生命体 = 加/减一份 runtime。

同时承担**心理慢循环**：按剧本时间推进情绪回落、自控恢复、心情日漂移与信念衰减。
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from ..mind import belief as belief_mod
from ..mind import psyche as psyche_mod
from ..store.db import Database
from ..store.life_store import LifeStore, default_life
from ..store.repo import StateRepo
from .bus import EventBus
from .clock import Clock
from .config import AppConfig
from .life import LifeController
from .types import LifeRecord, PresenceState, PsycheState, Quirk, Relationship, new_id, now_dt

MIND_TICK_SECONDS = 120.0

# 允许通过控制台编辑的人格字段（其余字段走系统内部逻辑，不开放手改）
EDITABLE_LIFE_FIELDS = {
    "name", "avatar", "gender", "backstory", "awareness",
    "temperament", "big_five", "attachment",
    "needs_fears", "quirks", "beliefs", "style", "canon", "schedule",
}


@dataclass
class LifeRuntime:
    """单个生命体的运行时集合（状态隔离的载体）。"""

    life: LifeRecord
    psyche: PsycheState
    controller: LifeController
    rels: dict[str, Relationship] = field(default_factory=dict)


class LifeRegistry:
    def __init__(
        self,
        store: LifeStore,
        repo: StateRepo,
        config: AppConfig,
        clock: Clock,
        bus: EventBus,
        rng: random.Random,
        db: Database | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._db = db  # 级联删除 / 控制台统计用（可选，便于单测）
        self._cfg = config
        self._clock = clock
        self._bus = bus
        self._rng = rng
        self._runtimes: dict[str, LifeRuntime] = {}
        self._tasks: list[asyncio.Task[None]] = []
        # 由 SessionManager 注入：生命状态变化时广播到前端
        self.on_presence = None

    # ---- 载入 ----------------------------------------------------------
    async def ensure(self, life_id: str, *, create_default: bool = True) -> LifeRuntime:
        """确保某生命体已加载（不存在且允许时，创建内置默认人格）。"""
        rt = self._runtimes.get(life_id)
        if rt is not None:
            return rt

        life = self._store.load(life_id)
        if life is None:
            if not create_default:
                raise KeyError(f"生命体不存在：{life_id}")
            life = default_life(life_id)
            self._store.save(life)
            logger.info(f"未找到生命体 {life_id}，已生成内置默认人格「{life.name}」")
        else:
            raw = self._store.load_raw(life_id) or {}
            if "bubble_habit" not in (raw.get("style") or {}):
                # 老存档字段级迁移：补一个确定性的说话习惯（同一生命体永远一致）
                from ..mind.genesis import assign_bubble_habit

                life.style.bubble_habit = assign_bubble_habit(life)
                self._store.save(life)
                logger.info(f"生命体「{life.name}」补齐说话习惯：{life.style.bubble_habit}")

        if not life.schedule:
            # 老存档/默认体迁移：按人设派生个人作息（一次迁移，落盘固化）。
            # 此前所有生命体共用全局 config 一张作息表——"白天都忙碌、晚上都犯困"。
            from ..mind.genesis import derive_schedule

            life.schedule = derive_schedule(life)
            self._store.save(life)
            logger.info(
                f"生命体「{life.name}」生成个人作息：{life.schedule['wake_at']} 起 / "
                f"{life.schedule['sleep_at']} 睡（抖动 ±{life.schedule['jitter_minutes']} 分钟）"
            )

        psyche = await self._repo.load_psyche(life, self._clock.now())
        snapshot = await self._repo.load_life_state(life_id)
        initial = PresenceState.ACTIVE
        state_since = None
        if snapshot.get("presence"):
            try:
                initial = PresenceState(snapshot["presence"])
            except ValueError:
                initial = PresenceState.ACTIVE
        if snapshot.get("state_since"):
            try:
                state_since = datetime.fromisoformat(snapshot["state_since"])
            except ValueError:
                state_since = None

        controller = LifeController(
            life_id=life_id,
            cfg=self._cfg.presence,
            clock=self._clock,
            rng=self._rng,
            on_change=self._on_presence_change,
            initial=initial,
            state_since=state_since,
            schedule_override=life.schedule or None,
        )
        rt = LifeRuntime(life=life, psyche=psyche, controller=controller)
        self._runtimes[life_id] = rt

        await controller.start()
        self._tasks.append(asyncio.create_task(self._mind_loop(life_id), name=f"mind:{life_id}"))
        logger.info(f"生命体「{life.name}」({life_id}) 已就绪：{life.big_five.describe()}")
        return rt

    async def ensure_all(self) -> None:
        """载入全部存档；一个都没有时用内置默认。"""
        ids = self._store.list_ids()
        if not ids:
            ids = [self._cfg.identity.life_id]
        for life_id in ids:
            await self.ensure(life_id)

    # ---- 访问 ----------------------------------------------------------
    def get(self, life_id: str) -> LifeRuntime | None:
        return self._runtimes.get(life_id)

    def require(self, life_id: str) -> LifeRuntime:
        rt = self._runtimes.get(life_id)
        if rt is None:
            raise KeyError(f"生命体未加载：{life_id}")
        return rt

    def list_runtimes(self) -> list[LifeRuntime]:
        return list(self._runtimes.values())

    def list_ids(self) -> list[str]:
        return list(self._runtimes.keys())

    def exists(self, life_id: str) -> bool:
        return self._store.exists(life_id)

    async def relationship(self, life_id: str, user_id: str) -> Relationship:
        rt = self.require(life_id)
        rel = rt.rels.get(user_id)
        if rel is None:
            rel = await self._repo.load_relationship(life_id, user_id)
            rt.rels[user_id] = rel
        return rel

    # ---- 持久化 --------------------------------------------------------
    async def save_psyche(self, life_id: str) -> None:
        rt = self._runtimes.get(life_id)
        if rt is not None:
            await self._repo.save_psyche(life_id, rt.psyche)

    async def save_relationship(self, life_id: str, user_id: str) -> None:
        rt = self._runtimes.get(life_id)
        if rt is not None and user_id in rt.rels:
            await self._repo.save_relationship(life_id, user_id, rt.rels[user_id])

    async def save_life_state(self, life_id: str) -> None:
        rt = self._runtimes.get(life_id)
        if rt is not None:
            await self._repo.save_life_state(life_id, rt.controller.snapshot())
            self._store.save(rt.life)     # 信念/风格可能被同化，一并落盘

    # ---- 生命周期 ------------------------------------------------------
    async def create_life(
        self, seed: int, hint: dict | None = None, mode: str = "random"
    ) -> LifeRecord:
        """创世：新生一个生命体。

        ``mode`` ∈ {random（掷骰）, custom（自定义）, blank（纯洁空白体）}，
        由 ``mind.genesis`` 统一构造，这里负责落库 + 载入运行时。
        """
        from ..mind.genesis import genesis  # 延迟导入避免循环

        life = await genesis(int(seed), hint=hint, cfg=self._cfg.genesis, mode=mode)
        self._store.save(life)
        await self.ensure(life.life_id)
        logger.info(f"新生生命体「{life.name}」mode={mode} seed={seed}")
        return life

    def preview_life(self, seed: int, hint: dict | None = None, mode: str = "random") -> LifeRecord:
        """控制台预览：只构造不落库（掷骰重试不会污染存档）。"""
        from ..mind.genesis import preview

        return preview(int(seed), hint, mode)

    async def clone_life(self, life_id: str, new_name: str | None = None) -> LifeRecord:
        """克隆：复制人格档案，新生命体不继承原体的心理/关系运行时状态。"""
        src = self._store.load(life_id)
        if src is None:
            raise KeyError(f"生命体不存在：{life_id}")
        clone = src.model_copy(deep=True)
        clone.life_id = new_id("life")
        clone.name = (new_name or f"{src.name}·副本").strip()
        clone.avatar = clone.name[-1] if clone.gender != "neutral" else "TA"
        clone.born_at = now_dt()
        clone.seed = src.seed + 1  # 与本体区分，便于回溯
        clone.status = "alive"
        self._store.save(clone)
        await self.ensure(clone.life_id)
        logger.info(f"克隆生命体「{src.name}」→「{clone.name}」({clone.life_id})")
        return clone

    @staticmethod
    def _coerce_quirks(value: object, current: list[Quirk]) -> list[Quirk]:
        """把控制台传来的怪癖规整成 ``Quirk`` 对象。

        控制台的「怪癖/偏好」是一个纯文本输入框（每行一条），发过来的只是字符串，
        无法携带 type。这里在**系统边界**做一次归一：字典直通；字符串则按内容与
        现有条目比对以保留原有 type（catchphrase/tics/emoji），匹配不到的新条目
        归为 preference；顺带去空、去重。避免脏数据把 ``LifeRecord`` 校验打挂。
        """
        by_content = {q.content: q.type for q in current}
        seen: set[str] = set()
        out: list[Quirk] = []
        for item in value or []:  # type: ignore[union-attr]
            if isinstance(item, Quirk):
                out.append(item)
                continue
            if isinstance(item, dict):
                text = str(item.get("content") or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    out.append(Quirk(type=item.get("type") or by_content.get(text, "preference"), content=text))
                continue
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(Quirk(type=by_content.get(text, "preference"), content=text))
        return out

    async def update_life(self, life_id: str, patch: dict) -> LifeRecord:
        """编辑人格档案：只接受白名单字段，重校验后落盘并热替换运行时。"""
        rt = self.require(life_id)
        data = rt.life.model_dump()
        for key, value in (patch or {}).items():
            if key in EDITABLE_LIFE_FIELDS and value is not None:
                if key == "quirks":
                    value = self._coerce_quirks(value, rt.life.quirks)
                data[key] = value
        updated = LifeRecord(**data)  # 走一次模型校验，脏数据在这里被挡下
        self._store.save(updated)
        rt.life = updated
        logger.info(f"生命体 {life_id} 档案已更新：{sorted(k for k in (patch or {}) if k in EDITABLE_LIFE_FIELDS)}")
        return updated

    async def delete_life(self, life_id: str) -> None:
        rt = self._runtimes.pop(life_id, None)
        if rt is not None:
            await rt.controller.stop()
        self._store.delete(life_id)
        if self._db is not None:
            await self._db.delete_life_data(life_id)  # 级联清理，避免脏数据
        logger.info(f"生命体 {life_id} 已休眠并删除存档")

    # ---- 控制台详情 ----------------------------------------------------
    def profile(self, life_id: str) -> dict | None:
        """给控制台的完整档案快照（人格 + 实时生命状态）。"""
        rt = self._runtimes.get(life_id)
        if rt is None:
            life = self._store.load(life_id)
            return life.model_dump(mode="json") if life else None
        data = rt.life.model_dump(mode="json")
        data["presence"] = rt.controller.presence.value
        data["mood_label"] = rt.psyche.mood_label
        data["emotional"] = {
            "valence": round(rt.psyche.emotion_valence, 3),
            "arousal": round(rt.psyche.emotion_arousal, 3),
            "self_control": round(rt.psyche.self_control, 3),
            "fatigue": round(rt.psyche.fatigue, 3),
        }
        return data

    async def stop_all(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        for rt in self._runtimes.values():
            await rt.controller.stop()
            await self.save_psyche(rt.life.life_id)
            await self.save_life_state(rt.life.life_id)

    # ---- 内部 ----------------------------------------------------------
    async def _on_presence_change(self, life_id: str, state: PresenceState, meta: dict) -> None:
        await self.save_life_state(life_id)
        if self.on_presence is not None:
            result = self.on_presence(life_id, state, meta)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result  # type: ignore[misc]

    async def _mind_loop(self, life_id: str) -> None:
        """心理慢循环：情绪回落 / 自控恢复 / 心情日漂移 / 信念衰减。"""
        last_day = self._clock.now().strftime("%Y-%m-%d")
        try:
            while True:
                await self._clock.sleep(MIND_TICK_SECONDS)
                rt = self._runtimes.get(life_id)
                if rt is None:
                    return
                now = self._clock.now()
                psyche_mod.tick(
                    rt.psyche, rt.life, now, rt.controller.presence, self._cfg.relation
                )
                day = now.strftime("%Y-%m-%d")
                if day != last_day:
                    last_day = day
                    psyche_mod.drift_mood_baseline(
                        rt.psyche, rt.life, self._cfg.relation, self._rng,
                        day_key=int(day.replace("-", "")),
                    )
                    belief_mod.decay_all(rt.life, self._cfg.belief, now)
                    for rel in rt.rels.values():
                        psyche_mod.decay_by_neglect(rel, self._cfg.relation, now)
                    logger.info(f"[{life_id}] 跨天：心情底色={rt.psyche.mood_baseline:+.2f}")
                await self.save_psyche(life_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{life_id}] 心理慢循环异常")

    # ---- 调试面板数据 --------------------------------------------------
    def debug_snapshot(self, life_id: str, wake_progress: float = 0.0) -> dict:
        rt = self._runtimes.get(life_id)
        if rt is None:
            return {}
        return {
            "life_id": life_id,
            "name": rt.life.name,
            "presence": rt.controller.presence.value,
            "state_since": rt.controller.state_since.isoformat(),
            "wake_progress": round(wake_progress, 3),
            "mood_baseline": round(rt.psyche.mood_baseline, 3),
            "emotion_valence": round(rt.psyche.emotion_valence, 3),
            "emotion_arousal": round(rt.psyche.emotion_arousal, 3),
            "mood_label": rt.psyche.mood_label,
            "self_control": round(rt.psyche.self_control, 3),
            "fatigue": round(rt.psyche.fatigue, 3),
            "override_state": rt.psyche.override_state.value,
            "relationships": {
                uid: {
                    "stage": rel.stage,
                    "intimacy": round(rel.intimacy, 3),
                    "trust": round(rel.trust, 3),
                    "dependence": round(rel.dependence, 3),
                    "familiarity": round(rel.familiarity, 3),
                    "interactions": rel.interactions,
                }
                for uid, rel in rt.rels.items()
            },
            "beliefs": [
                {"topic": b.topic, "statement": b.statement,
                 "confidence": round(b.confidence, 3), "locked": b.identity_locked}
                for b in rt.life.beliefs
            ],
            "temperament": rt.life.temperament.model_dump(),
            "big_five": rt.life.big_five.model_dump(),
            "saved_at": now_dt().isoformat(),
        }