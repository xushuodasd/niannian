"""时间抽象 —— 整个项目的可测试性地基。

血泪教训：真人节奏是分钟~小时级，靠"真的等"无法测试、无法调参。
所以**所有延时、所有"现在几点"都必须走 Clock**：

  - ``clock.now()``       取当前时间（作息推进 / 情绪衰减 / 唤醒衰减都基于它）
  - ``clock.sleep(s)``    所有等待（感知延迟 / 思考延迟 / 打字时延）

``time_scale`` 提供全局倍速：1x 正常聊天、60x 快速验证作息与唤醒、720x 模拟跨天。
行为在 1x 与 720x 下完全一致，只是速率被缩放。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta


class Clock:
    """真实时钟：``now()`` 取墙上时间，``sleep()`` 按倍速缩放真实等待。"""

    def __init__(self, time_scale: float = 1.0) -> None:
        self._time_scale = float(time_scale)

    @property
    def time_scale(self) -> float:
        return self._time_scale

    def set_time_scale(self, scale: float) -> None:
        """切换倍速。倍速越大，故事时间流逝越快（等待越短）。"""
        if scale <= 0:
            raise ValueError("time_scale 必须为正数")
        self._time_scale = float(scale)

    def now(self) -> datetime:
        return datetime.now().astimezone()

    async def sleep(self, seconds: float) -> None:
        """等待 ``seconds`` 秒的**故事时间**。

        真实等待 = seconds / time_scale，所以倍速下会被压缩。
        任何"表演出来的延迟"都必须调用它，而不是直接 asyncio.sleep。
        """
        if seconds <= 0:
            await asyncio.sleep(0)  # 让出控制权，保持协作式调度
            return
        await asyncio.sleep(seconds / self._time_scale)


class FakeClock(Clock):
    """虚拟时钟：时间不真的流逝，而是由 ``sleep`` 直接推进。

    用于测试/回放：把"等 5 分钟"变成瞬时，且结果完全确定。
    虚拟时间即"故事时间"，因此 ``sleep`` 按原值推进、不做倍速缩放。
    """

    def __init__(self, start: datetime | None = None, time_scale: float = 1.0) -> None:
        super().__init__(time_scale=time_scale)
        self._now = start or datetime(2026, 1, 1, 8, 0, 0).astimezone()

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += timedelta(seconds=seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        """手动推进虚拟时间（无需经由 sleep）。"""
        self._now += timedelta(seconds=seconds)