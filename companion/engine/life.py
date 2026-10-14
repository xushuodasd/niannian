"""生命状态机（慢层）：可及性 + 作息节律（4.2 / 4.3）。

这是"像真人"的第一根支柱：**伙伴有自己的生物钟**，不是 24 小时待命的问答机。

  * ``Schedule``：把 config 的作息表解析成"某时刻应该处于什么状态"（含每日抖动）；
  * ``LifeController``：单生命体的状态推进循环 —— 到点自动迁移，可被事件打断
    （聊天正嗨推迟入睡、被叫醒后可能睡回去），每次迁移都 emit 出去让前端角标动起来。
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, time as dtime, timedelta

from loguru import logger

from .config import PresenceConfig
from .clock import Clock
from .types import PresenceState, clamp

OnChange = Callable[[str, PresenceState, dict], "Awaitable[None] | None"]

# 每个 tick 推进的"故事时间"（秒）。倍速越高，真实间隔越短。
TICK_SECONDS = 45.0


def _parse_hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _parse_period(s: str) -> tuple[int, int]:
    a, b = s.split("-")
    return _parse_hhmm(a), _parse_hhmm(b)


def _in_period(minute: int, start: int, end: int) -> bool:
    """支持跨零点区间（如 23:30-07:30）。"""
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


class Schedule:
    """作息表：给定时刻 → 预期生命状态。带每日随机抖动，避免机械感。"""

    def __init__(self, cfg: PresenceConfig) -> None:
        self._cfg = cfg
        self._wake = _parse_hhmm(cfg.wake_at)
        self._sleep = _parse_hhmm(cfg.sleep_at)
        self._busy = [_parse_period(p) for p in cfg.busy_periods]
        self._rest = [_parse_period(p) for p in cfg.rest_periods]
        self._day_jitter: dict[str, tuple[int, int]] = {}

    def _jitter_for(self, day: str, rng: random.Random) -> tuple[int, int]:
        """每天一次性生成 wake/sleep 抖动（分钟）。"""
        if day not in self._day_jitter:
            j = self._cfg.jitter_minutes
            self._day_jitter[day] = (
                rng.randint(-j, j) if j else 0,
                rng.randint(-j, j) if j else 0,
            )
            # 只保留最近若干天，避免无界增长
            if len(self._day_jitter) > 8:
                for k in list(self._day_jitter)[:-4]:
                    self._day_jitter.pop(k, None)
        return self._day_jitter[day]

    def intended_state(self, now: datetime, rng: random.Random) -> PresenceState:
        jw, js = self._jitter_for(now.strftime("%Y-%m-%d"), rng)
        minute = now.hour * 60 + now.minute
        wake, sleep = (self._wake + jw) % 1440, (self._sleep + js) % 1440

        # 睡眠区间（跨零点）
        if _in_period(minute, sleep, wake):
            return PresenceState.ASLEEP
        for a, b in self._rest:
            if _in_period(minute, a, b):
                return PresenceState.RESTING
        for a, b in self._busy:
            if _in_period(minute, a, b):
                return PresenceState.BUSY
        return PresenceState.ACTIVE


class LifeController:
    """单生命体的生命状态机（Presence）+ 作息推进循环。"""

    def __init__(
        self,
        life_id: str,
        cfg: PresenceConfig,
        clock: Clock,
        rng: random.Random,
        on_change: OnChange | None = None,
        *,
        initial: PresenceState = PresenceState.ACTIVE,
        state_since: datetime | None = None,
    ) -> None:
        self.life_id = life_id
        self._cfg = cfg
        self._clock = clock
        self._rng = rng
        self._on_change = on_change
        self._schedule = Schedule(cfg)

        self._state = initial
        self._state_since = state_since or clock.now()
        self._task: asyncio.Task[None] | None = None
        self._prev_state: PresenceState | None = None

        # 事件性覆盖
        self._hold_until: datetime | None = None       # 聊天正嗨：推迟入睡
        self._afk_until: datetime | None = None        # 随机离开
        self._just_woke_at: datetime | None = None     # 起床气窗口
        self._sleep_back_deadline: datetime | None = None

    # ---- 只读 ----------------------------------------------------------
    @property
    def presence(self) -> PresenceState:
        return self._state

    @property
    def state_since(self) -> datetime:
        return self._state_since

    @property
    def just_woke(self) -> bool:
        """是否处于"刚醒、带起床气"窗口（用于话术）。"""
        if self._just_woke_at is None:
            return False
        return (self._clock.now() - self._just_woke_at).total_seconds() < 8 * 60

    def snapshot(self) -> dict:
        return {
            "presence": self._state.value,
            "state_since": self._state_since.isoformat(),
            "just_woke": self.just_woke,
        }

    # ---- 生命周期 ------------------------------------------------------
    async def start(self) -> None:
        if not self._cfg.enabled:
            return
        # 若恢复的存档状态与当前作息明显不符，先对齐一次
        await self._advance(emit=False)
        self._task = asyncio.create_task(self._loop(), name=f"life:{self.life_id}")

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
                await self._clock.sleep(TICK_SECONDS)
                await self._advance()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[{self.life_id}] 生命状态循环异常")

    # ---- 状态推进 ------------------------------------------------------
    async def _advance(self, *, emit: bool = True) -> None:
        now = self._clock.now()
        target = self._schedule.intended_state(now, self._rng)

        # 聊天正嗨 → 推迟入睡
        if target == PresenceState.ASLEEP and self._hold_until and now < self._hold_until:
            target = PresenceState.ACTIVE

        # 随机离开（AFK）
        if target == PresenceState.ACTIVE:
            if self._afk_until and now < self._afk_until:
                target = PresenceState.AFK
            elif self._afk_until and now >= self._afk_until:
                self._afk_until = None
            elif self._cfg.afk_chance_per_hour > 0:
                p = self._cfg.afk_chance_per_hour * (TICK_SECONDS / 3600.0)
                if self._state == PresenceState.ACTIVE and self._rng.random() < p:
                    minutes = self._rng.uniform(5, 40)
                    self._afk_until = now + timedelta(minutes=minutes)
                    target = PresenceState.AFK

        if target != self._state:
            await self._transition(target, emit=emit)

    async def _transition(self, state: PresenceState, *, emit: bool = True, reason: str = "") -> None:
        self._prev_state, self._state = self._state, state
        self._state_since = self._clock.now()
        logger.info(f"[{self.life_id}] 生命状态 {self._prev_state.value} → {state.value} {reason}")
        if emit and self._on_change is not None:
            result = self._on_change(
                self.life_id,
                state,
                {"prev": self._prev_state.value if self._prev_state else "", "reason": reason},
            )
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result  # type: ignore[misc]

    # ---- 外部事件 ------------------------------------------------------
    async def note_activity(self, minutes: float = 40.0) -> None:
        """用户聊天正嗨 → 推迟入睡（4.3：状态可被事件打断）。"""
        self._hold_until = self._clock.now() + timedelta(minutes=minutes)

    async def force_state(self, state: PresenceState, reason: str = "手动强切") -> None:
        """调试面板用：一键切到 BUSY/AFK/RESTING/ASLEEP。"""
        self._hold_until = None
        self._afk_until = None
        await self._transition(state, reason=reason)

    async def wake_up(self, reason: str = "被叫醒") -> None:
        """唤醒：进入 ACTIVE 并带"起床气"标记；小概率稍后又睡回去（4.4）。"""
        self._hold_until = self._clock.now() + timedelta(minutes=20)
        self._afk_until = None
        await self._transition(PresenceState.ACTIVE, reason=f"唤醒（{reason}）")
        self._just_woke_at = self._clock.now()
        if self._rng.random() < 0.25:
            self._sleep_back_deadline = self._clock.now() + timedelta(minutes=6)

    def consume_just_woke(self) -> bool:
        """取用一次"起床气"（用完即清，避免每轮都像刚醒）。"""
        was = self.just_woke
        return was

    async def maybe_sleep_back(self) -> bool:
        """刚被吵醒又没下文 → 短时间重回 RESTING/ASLEEP（4.4）。"""
        if self._sleep_back_deadline is None:
            return False
        now = self._clock.now()
        if now < self._sleep_back_deadline:
            return False
        self._sleep_back_deadline = None
        target = self._schedule.intended_state(now, self._rng)
        await self._transition(
            PresenceState.ASLEEP if target == PresenceState.ASLEEP else PresenceState.RESTING,
            reason="又睡回去了",
        )
        return True

    # ---- 可及性判定（供 Inbox 闸门使用）--------------------------------
    def availability_note(self) -> str:
        return {
            PresenceState.ACTIVE: "",
            PresenceState.BUSY: "正在忙，可能回得慢",
            PresenceState.AFK: "暂时离开",
            PresenceState.DND: "请勿打扰",
            PresenceState.RESTING: "在小睡",
            PresenceState.ASLEEP: "睡着了",
            PresenceState.OFFLINE: "不在线",
        }.get(self._state, "")