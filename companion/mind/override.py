"""情绪上头：自控失效 → 越界 → 事后闭环（第 12 节）。

双系统（系统 1 冲动 / 系统 2 理智）：强情绪**耗尽自控**，于是说出平时不会说的话。
关键在**事后闭环**（12.4）——没有它，"发疯"就只是随机 bug：

    NORMAL → OVERRIDDEN →（情绪回落）→ REGRET →（后悔/找补/合理化）→ NORMAL

方向性约束（12.3）：高宜人+高尽责的人，越界多以"自伤/憋着"形式发生，而不是伤人。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from loguru import logger

from ..engine.config import OverrideConfig
from ..engine.types import LifeRecord, OverrideState, PsycheState, Relationship, now_dt
from . import psyche as psyche_mod


def enter(state: PsycheState, now: datetime | None = None) -> None:
    state.override_state = OverrideState.OVERRIDDEN
    state.override_since = now or now_dt()
    logger.warning("情绪上头：进入 OVERRIDDEN（本轮绕过人设一致性自检）")


def is_regret_due(state: PsycheState, cfg: OverrideConfig, now: datetime) -> bool:
    """情绪回落 / 自控恢复后，该进入后悔闭环了。"""
    if state.override_state != OverrideState.OVERRIDDEN or state.override_since is None:
        return False
    elapsed = (now - state.override_since).total_seconds() / 60.0
    if elapsed < cfg.regret_delay_minutes:
        return False
    # 情绪确实回落了才算
    return state.emotion_intensity < 0.35 or state.self_control > 0.6


def finish(state: PsycheState) -> None:
    state.override_state = OverrideState.NORMAL
    state.override_since = None


_CLOSURES: dict[str, list[str]] = {
    "cold": [
        "…昨天我说话有点冲，别往心里去",
        "我那会儿心情不好 不是冲你",
    ],
    "sullen": [
        "我没事 就是昨天有点堵得慌",
        "昨天那事…算了 不说了",
    ],
    "clingy": [
        "昨天我是不是说太多了…你别嫌我烦",
        "那个 我昨晚有点上头 你别当真",
    ],
    "angry": [
        "昨天我语气重了 对不起啊",
        "我那会儿是真急了 不是故意凶你",
    ],
}

# 回避型：不认错，走"合理化/嘴硬"路线（12.4）
_RATIONALIZE = [
    "我就是这么想的 没什么好改的",
    "我说的也没错 只是当时没说清楚",
]


def closure_text(life: LifeRecord, flavor: str, rng: random.Random) -> str:
    """事后闭环话术：后悔 / 找补 / 合理化（回避型偏爱合理化）。"""
    if life.attachment.value == "avoidant" and rng.random() < 0.6:
        return rng.choice(_RATIONALIZE)
    return rng.choice(_CLOSURES.get(flavor, _CLOSURES["angry"]))


def plan_regret_at(now: datetime, cfg: OverrideConfig) -> datetime:
    return now + timedelta(minutes=cfg.regret_delay_minutes)


# ---- 情绪冲击评估（供结算使用）----------------------------------------
def shock_from_user(state: PsycheState, life: LifeRecord, user_valence: float) -> float:
    """用户带来的情绪冲击强度（0..1），用于消耗自控、累积上头风险。"""
    base = abs(user_valence) * (0.5 + 0.6 * life.temperament.intensity)
    return min(1.0, base)


def should_enter(state: PsycheState, life: LifeRecord, cfg: OverrideConfig, now: datetime) -> bool:
    """综合判定：情绪强度 × (1-自控) 越阈值即上头（受最短间隔约束）。"""
    ok = psyche_mod.should_override(state, cfg, now)
    if ok:
        enter(state, now)
    return ok


def flavor_of(life: LifeRecord) -> str:
    return psyche_mod.override_flavor(life)