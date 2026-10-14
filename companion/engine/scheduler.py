"""主动性调度（M11）+ 情绪上头的事后闭环收口（M14）。

对应设计文档第 8 节（主动性三闸门）与 12.4（越界后必须有闭环，否则"发疯"就只是 bug）。

**为什么单独一个慢循环**：主动发起、后悔找补、又睡回去，都是"分钟~小时级"的事件，
不该塞进会话热路径（那会污染问答延迟）。这里用 Clock 驱动的低频 tick 统一收口，
倍速下同样成立（60x/720x 能快速观察"她会不会想起你"）。

主动性三闸门（第 8 节）：
  ① 状态闸门：只有在可及状态（ACTIVE/BUSY）才可能主动；休息/离开/勿扰一律不打扰。
  ② 时间闸门：距上次互动 > ``min_gap_minutes``，且避开深夜（免得像骚扰）。
  ③ 意愿闸门：``score = w·[未了话题, 情绪强度, 时段匹配, 亲密度] + 抖动 > threshold``。
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from ..mind import override as override_mod
from ..mind import psyche as psyche_mod
from ..store.db import Database
from .clock import Clock
from .config import AppConfig
from .registry import LifeRegistry, LifeRuntime
from .types import OverrideState, PresenceState

if TYPE_CHECKING:  # 仅类型提示，避免运行时循环依赖
    from .session import Session, SessionManager

# 生活线活动池（M10）：给"你眼下的生活"提供素材，让主动搭话有内容可分享
_ACTIVITIES = [
    "在公司改方案改到想砸电脑", "中午和同事去吃了家新开的面馆",
    "下班顺路买了杯奶茶", "晚上窝在家看老电影", "给猫梳毛被挠了一下",
    "出门取快递被雨淋了", "周末去逛了旧货市场", "熬夜追剧到三点",
    "把阳台的花重新换了盆", "去健身房练了个背，明天准酸痛",
    "自己做了顿饭，卖相还行", "听了张很上头的老专辑",
]
_LIFE_EVENT_MIN_GAP = timedelta(minutes=100)

# 适合主动搭话的时段（其余时段只在闸门打分里降权，不硬禁）
_GOOD_HOURS = {8, 9, 10, 12, 13, 19, 20, 21, 22}


class ProactiveScheduler:
    """低频调度器：主动发起 + 后悔闭环 + 睡回去 + 生活线记录。"""

    def __init__(
        self,
        *,
        clock: Clock,
        registry: LifeRegistry,
        sessions: "SessionManager",
        db: Database,
        config: AppConfig,
        rng: random.Random,
    ) -> None:
        self._clock = clock
        self._registry = registry
        self._sessions = sessions
        self._db = db
        self._cfg = config
        self._rng = rng
        self._task: asyncio.Task[None] | None = None

    # ---- 生命周期 ------------------------------------------------------
    async def start(self) -> None:
        if not self._cfg.proactive.enabled:
            logger.info("主动性调度未启用（config.proactive.enabled=false）")
            return
        self._task = asyncio.create_task(self._loop(), name="proactive")
        logger.info(
            f"主动性调度已启动：每 {self._cfg.proactive.check_interval_s:.0f}s 检查一次"
            f"（阈值 {self._cfg.proactive.threshold}，最小间隔 {self._cfg.proactive.min_gap_minutes:.0f}min）"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await self._clock.sleep(self._cfg.proactive.check_interval_s)
                await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("主动性调度循环异常")

    # ---- 单次 tick -----------------------------------------------------
    async def _tick(self) -> None:
        now = self._clock.now()
        for life_id in self._registry.list_ids():
            rt = self._registry.get(life_id)
            if rt is None:
                continue

            # 被吵醒又没下文 → 小概率又困回去（4.4）
            await rt.controller.maybe_sleep_back()

            # 生活线：她自己的日子也在往前走（M10）
            await self._maybe_life_event(rt, now)

            for session in self._sessions.sessions_of(life_id):
                await self._check_override_closure(rt, session, now)
                await self._maybe_proactive(rt, session, now)

    # ==================================================================
    # M14：上头之后的后悔闭环收口
    # ==================================================================
    async def _check_override_closure(self, rt: LifeRuntime, session: "Session", now: datetime) -> None:
        state = rt.psyche
        if not override_mod.is_regret_due(state, self._cfg.override, now):
            return
        life_id, user_id = rt.life.life_id, session.user_id

        # 先切 REGRET：既阻止"上头判定"再次触发，也标记这轮是来找补的
        state.override_state = OverrideState.REGRET
        flavor = override_mod.flavor_of(rt.life)
        text = override_mod.closure_text(rt.life, flavor, self._rng)

        await self._sessions.override_closure_turn(life_id, user_id, text)
        try:
            await self._db.add_emotion_memory(
                life_id, user_id, "override_closure", text, state.emotion_valence
            )
        except Exception:
            logger.debug("情绪记忆写入失败")

        override_mod.finish(state)
        await self._registry.save_psyche(life_id)
        logger.info(f"[{life_id}] 上头闭环：{flavor} → 「{text}」，情绪回到常态")

    # ==================================================================
    # M11：主动发起（三闸门）
    # ==================================================================
    async def _maybe_proactive(self, rt: LifeRuntime, session: "Session", now: datetime) -> None:
        cfg = self._cfg.proactive
        if session.busy:
            return

        presence = rt.controller.presence
        # ① 状态闸门
        if presence not in (PresenceState.ACTIVE, PresenceState.BUSY):
            return

        user_id = session.user_id
        rel = await self._registry.relationship(rt.life.life_id, user_id)

        # ② 时间闸门（避开深夜）
        gap_min = (now - rel.last_active_at).total_seconds() / 60.0
        if gap_min < cfg.min_gap_minutes:
            return
        if now.hour < 7 or now.hour >= 23:
            return

        # ③ 意愿闸门
        #  她当下"不想理人"时，也别主动去打扰 —— 与回复意愿闸门同源（同一意愿分）。
        willingness = psyche_mod.reply_willingness(
            rt.psyche, rel, self._cfg.reply_intent,
            user_text="", batch_count=0, now=now,
        )["score"]
        if willingness < self._cfg.reply_intent.proactive_floor:
            logger.debug(
                f"[{rt.life.life_id}] 主动发起被意愿闸门拦下：willingness={willingness:.2f}"
                f" < {self._cfg.reply_intent.proactive_floor}"
            )
            return

        topics = await self._db.load_topics(rt.life.life_id, user_id, status="open", limit=5)
        w = cfg.weights
        topic_score = min(1.0, len(topics) / 3.0)
        emotion_score = psyche_mod.emotion_intensity(rt.psyche, rt.life)
        time_score = 1.0 if now.hour in _GOOD_HOURS else 0.4
        intimacy_score = rel.intimacy
        jitter = self._rng.uniform(-0.05, 0.05)

        score = (
            w.get("topic", 0.35) * topic_score
            + w.get("emotion", 0.25) * emotion_score
            + w.get("time", 0.2) * time_score
            + w.get("intimacy", 0.2) * intimacy_score
            + jitter
        )
        if score < cfg.threshold:
            return

        kind, seed_text = self._choose_kind(rt, topics, now)
        await self._sessions.proactive_turn(rt.life.life_id, user_id, kind, seed_text)
        # 记一次"最近活动"，避免连续主动刷屏（也参与下次时间闸门）
        rel.last_active_at = now
        await self._registry.save_relationship(rt.life.life_id, user_id)
        logger.info(
            f"[{rt.life.life_id}] 主动发起（{kind}）score={score:.2f} "
            f"(话题{topic_score:.2f} 情绪{emotion_score:.2f} 时段{time_score:.2f} 亲密{intimacy_score:.2f})"
        )

    @staticmethod
    def _choose_kind(rt: LifeRuntime, topics: list[dict], now: datetime) -> tuple[str, str]:
        """按时段与未了话题挑话术档：greeting / topic / care / share。"""
        if 7 <= now.hour < 11:
            return "greeting", ""
        if topics and now.hour < 22:
            return "topic", str(topics[0]["title"])
        if psyche_mod.emotion_intensity(rt.psyche, rt.life) > 0.5:
            return "care", ""
        return "share", ""

    # ==================================================================
    # M10：生活线（她自己的日子）
    # ==================================================================
    async def _maybe_life_event(self, rt: LifeRuntime, now: datetime) -> None:
        # 低频：~每 tick 1% 概率，且距上一条至少 100 分钟
        if self._rng.random() > 0.012:
            return
        life_id = rt.life.life_id
        recent = await self._db.recent_life_events(life_id, limit=1)
        if recent:
            try:
                last = datetime.fromisoformat(recent[0]["ts"])
                if now - last < _LIFE_EVENT_MIN_GAP:
                    return
            except Exception:
                pass
        activity = self._rng.choice(_ACTIVITIES)
        await self._db.add_life_event(
            life_id, now.strftime("%Y-%m-%d"), activity, mood_tag=rt.psyche.mood_label
        )
        logger.debug(f"[{life_id}] 生活线：{activity}")