"""唤醒仲裁（4.4）：休息状态下"要多发好几条才能吵醒"。

    每条消息增量 = (基础值 + 呼唤词加成 + 连环加成) × 睡眠深度系数 - 自然衰减

叫醒一个睡着的人本身就是一种体感：普通消息要十几条，带名字的连环夺命 call 五条就行。
"""
from __future__ import annotations

import random
from datetime import datetime

from .config import WakeConfig
from .types import PresenceState, WakeResult, clamp


def has_call_word(text: str, name: str, words: list[str]) -> str | None:
    """是否含呼唤词（名字 / 在吗 / 醒醒 / 喂 / @…）。"""
    for w in words:
        if w and w in text:
            return w
    if name and name in text:
        return name
    return None


class _State:
    __slots__ = ("progress", "last_msg_at", "name")

    def __init__(self) -> None:
        self.progress = 0.0
        self.last_msg_at: datetime | None = None
        self.name = ""


class WakeArbiter:
    def __init__(self, cfg: WakeConfig, rng: random.Random) -> None:
        self._cfg = cfg
        self._rng = rng
        self._states: dict[str, _State] = {}

    # ---- 查询 ----------------------------------------------------------
    def progress(self, life_id: str) -> float:
        return self._get(life_id).progress

    def reset(self, life_id: str) -> None:
        self._states.pop(life_id, None)

    def _get(self, life_id: str) -> _State:
        st = self._states.get(life_id)
        if st is None:
            st = _State()
            self._states[life_id] = st
        return st

    # ---- 累积 ----------------------------------------------------------
    def accumulate(
        self,
        life_id: str,
        text: str,
        presence: PresenceState,
        now: datetime,
        *,
        life_name: str = "",
    ) -> WakeResult:
        """累积一次消息带来的唤醒值，返回是否达到唤醒阈值。"""
        cfg = self._cfg
        st = self._get(life_id)
        st.name = life_name or st.name

        # 自然衰减（按距上一条的分钟数）
        if st.last_msg_at is not None:
            minutes = max(0.0, (now - st.last_msg_at).total_seconds() / 60.0)
            st.progress = max(0.0, st.progress - cfg.decay_per_min * minutes)

        delta = cfg.base
        reasons: list[str] = []
        call = has_call_word(text, st.name, cfg.call_words)
        if call:
            delta += cfg.call_bonus
            reasons.append(f"呼唤「{call}」")
        if st.last_msg_at is not None:
            gap = (now - st.last_msg_at).total_seconds()
            if gap < cfg.chain_window_s:
                delta += cfg.chain_bonus
                reasons.append("连环")

        depth = cfg.resting_factor if presence == PresenceState.RESTING else cfg.asleep_factor
        delta_effective = delta * depth
        st.progress = clamp(st.progress + delta_effective, 0.0, 1.5)
        st.last_msg_at = now

        woke = st.progress >= 1.0
        if woke:
            st.progress = 0.0
            st.last_msg_at = None

        return WakeResult(
            life_id=life_id,
            progress=st.progress,
            delta=delta_effective,
            woke=woke,
            reason=("、".join(reasons) if reasons else "普通消息"),
        )

    def wake_greeting_reason(self, presence: PresenceState, result: WakeResult) -> str:
        """给模型的一句"唤醒情境"说明（让它先回应"被叫醒"再回内容）。"""
        when = "睡着了" if presence == PresenceState.ASLEEP else "在小睡"
        if result.reason and result.reason != "普通消息":
            return f"你刚刚{when}，被对方连着发消息（{result.reason}）吵醒了。"
        return f"你刚刚{when}，被对方的消息吵醒了。"

    def maybe_sleep_back(self) -> bool:
        """刚被叫醒、又没下文时，小概率立刻又困回去（由调用方决定时机）。"""
        return self._rng.random() < self._cfg.sleep_back_chance