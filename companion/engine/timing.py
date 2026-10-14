"""时序模型（第 5 节）：把"回一条消息"拆成四段延迟，每段都受状态与情绪调制。

    总响应时间 ≈ d_avail + d_perceive + d_think + Σ(d_type) + 条间间隔

**关键理念**：生成可以快，但"展示"必须慢 —— 延迟是**表演**出来的（9.2 原则 1）。
所有 sleep 都交给 Clock，倍速下自动压缩，行为一致。
"""
from __future__ import annotations

import random

from .config import TimingConfig
from .types import PresenceState, Segment, TimingBreakdown, TurnContext


class TimingModel:
    def __init__(self, cfg: TimingConfig) -> None:
        self._cfg = cfg

    # ---- ① 可及性延迟 --------------------------------------------------
    def availability(self, presence: PresenceState, rng: random.Random) -> float:
        """慢层闸门：忙/离开造成的大延迟。休息/离线不在此计算（由唤醒/积压处理）。"""
        c = self._cfg
        if presence == PresenceState.BUSY:
            return rng.uniform(c.d_avail_busy_min, c.d_avail_busy_max)
        if presence == PresenceState.AFK:
            return rng.uniform(c.d_avail_afk_min, c.d_avail_afk_max)
        if presence == PresenceState.ACTIVE:
            return rng.uniform(0.0, 0.8)
        return 0.0

    # ---- ② 感知延迟 ----------------------------------------------------
    def perceive(self, ctx: TurnContext, rng: random.Random, speed: float) -> float:
        """看到消息 → 意识到要回。情绪高涨时更短。"""
        c = self._cfg
        base = rng.uniform(c.perceive_min, c.perceive_max)
        extraversion_factor = 1.15 - 0.3 * ctx.life.big_five.extraversion
        return max(0.15, base * extraversion_factor / max(0.4, speed))

    # ---- ③ 思考延迟 ----------------------------------------------------
    def think(self, ctx: TurnContext, out_len: int, rng: random.Random, speed: float) -> float:
        """组织内容的时间 ∝ 输入复杂度 + 输出长度。"""
        c = self._cfg
        in_len = len(ctx.user_text)
        complexity = min(1.0, in_len / 60.0)
        out_factor = min(1.0, out_len / 80.0)
        base = c.think_min + (c.think_max - c.think_min) * (0.45 * complexity + 0.55 * out_factor)
        # 简单输入（如表情、单字）更快
        if in_len <= 3:
            base *= 0.55
        return max(0.3, base / max(0.4, speed))

    # ---- ④ 打字时延 ----------------------------------------------------
    def typing_seconds(self, text: str, cps: float, speed: float = 1.0) -> float:
        """每句 len/cps，封顶 4.5s、保底 0.5s（真人再慢也不会一个字打半天）。

        ``speed`` 为情绪/气质调制系数：兴奋时"手速"更快。
        ``typing_cps`` 已按真人中文手速调低，前端打字机据此逐字渲染以对齐观感。
        """
        eff = max(4.0, cps * max(0.4, speed))
        return min(4.5, max(0.5, len(text) / eff))

    def seg_gap(self, rng: random.Random) -> float:
        return rng.uniform(self._cfg.seg_gap_min, self._cfg.seg_gap_max)

    # ---- 汇总 ----------------------------------------------------------
    def plan(
        self,
        ctx: TurnContext,
        segments: list[Segment],
        rng: random.Random,
        *,
        speed: float = 1.0,
        include_availability: bool = True,
    ) -> TimingBreakdown:
        out_len = sum(len(s.text) for s in segments)
        return TimingBreakdown(
            d_avail=self.availability(ctx.presence, rng) if include_availability else 0.0,
            d_perceive=self.perceive(ctx, rng, speed),
            d_think=self.think(ctx, out_len, rng, speed),
            d_type=sum(s.typing_seconds for s in segments),
            seg_gaps=sum(float(s.meta.get("gap_after", 0.0)) for s in segments),
            segments=len(segments),
        )