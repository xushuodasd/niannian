"""信念系统：三观漂移 / "洗脑" / 逆反 / 同化（第 11 节）。

核心公式（11.2）：

    persuasion = 论点强度 × 来源信任 × 情绪共鸣 × 重复暴露因子 − 抵抗×(1+逆反)
    Δconfidence = persuasion × malleability × 时间衰减

且**短时间强推会触发逆反**：confidence 不升反降，并对该来源 trust 下降（11.3）。
—— 这是"油盐不进 / 越劝越犟"的真人体感，防止变成"洗脑机器"。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from loguru import logger

from ..engine.config import BeliefConfig
from ..engine.types import Belief, LifeRecord, Relationship, clamp, now_dt

# 情绪化说服词（密集说教 → 逆反的判定依据）
_PUSH_WORDS = ("你必须", "你就该", "听我的", "不许", "一定要", "别这样", "你怎么能", "你应该")


def select_candidates(life: LifeRecord, text: str, k: int = 3) -> list[Belief]:
    """按关键词重合度挑出本轮**候选信念**（只对候选做核算，省 token，9.2 E）。"""
    if not text.strip():
        return []
    scored: list[tuple[float, Belief]] = []
    for b in life.beliefs:
        score = 0.0
        for token in _tokens(b.topic) + _tokens(b.statement):
            if token and token in text:
                score += 1.0
        if score > 0:
            scored.append((score, b))
    scored.sort(key=lambda x: (x[0], x[1].confidence), reverse=True)
    if scored:
        return [b for _s, b in scored[:k]]
    # 没有明显命中时，返回最易被影响的几条（让"潜移默化"也能发生）
    return sorted(life.beliefs, key=lambda b: b.malleability, reverse=True)[:k]


def argument_strength(text: str) -> float:
    """粗略估计"论点强度"：长度 + 情绪化措辞密度。"""
    if not text:
        return 0.0
    base = min(1.0, len(text) / 120.0) * 0.5
    push = sum(1 for w in _PUSH_WORDS if w in text)
    emotion = 0.15 * min(3, text.count("！") + text.count("!"))
    return clamp(base + 0.12 * push + emotion, 0.0, 1.0)


def is_hard_push(text: str) -> bool:
    """是否"短时间强推/密集说教"（触发逆反）。"""
    return sum(1 for w in _PUSH_WORDS if w in text) >= 2


def apply(
    belief: Belief,
    *,
    strength: float,
    trust: float,
    intimacy: float,
    empathy: float,
    hard_push: bool,
    rel: Relationship,
    cfg: BeliefConfig,
    now: datetime | None = None,
) -> dict:
    """对单条信念施加一次说服，写回 confidence 与 exposure_count。返回变更记录。"""
    now = now or now_dt()
    before = belief.confidence

    # 重复暴露因子（单纯曝光效应）：听多了就信了
    repeat = 1.0 + belief.exposure_count * cfg.k_repeat
    source = 0.35 + 0.9 * trust + 0.45 * intimacy      # 越亲越有分量（关系绑架）
    resonance = 0.6 + 0.8 * empathy                     # 趁虚而入
    resist = (cfg.identity_resist if belief.identity_locked else 0.25) * repeat * 0.4

    persuasion = strength * source * resonance * repeat - resist

    # 逆反（reactance）：核心身份 + 强推 → 反向
    reactance = hard_push and belief.identity_locked and persuasion > cfg.reactance_threshold
    if reactance:
        persuasion = -abs(persuasion) * 0.6
        rel.trust = clamp(rel.trust - 0.03)
    elif hard_push and abs(persuasion) > 0.4:
        persuasion *= 0.4    # 强推打折

    delta = persuasion * belief.malleability * (0.5 + 0.5 * empathy)
    belief.confidence = clamp(before + delta, 0.02, 0.99)
    belief.exposure_count += 1
    belief.updated_at = now

    change = {
        "topic": belief.topic,
        "statement": belief.statement,
        "before": round(before, 3),
        "after": round(belief.confidence, 3),
        "delta": round(belief.confidence - before, 3),
        "reactance": bool(reactance),
    }
    if abs(change["delta"]) > 0.005:
        logger.info(f"信念漂移：{belief.topic} {before:.2f} → {belief.confidence:.2f}"
                    f"{'（逆反）' if reactance else ''}")
    return change


def decay_all(life: LifeRecord, cfg: BeliefConfig, now: datetime | None = None) -> None:
    """时间衰减：久不提及的信念缓慢回归中性（防止单向漂移到极端）。"""
    now = now or now_dt()
    for b in life.beliefs:
        days = max(0.0, (now - b.updated_at).total_seconds() / 86400.0)
        if days < 1:
            continue
        pull = 1.0 - math.exp(-days / cfg.decay_half_life_days) * 0.15
        b.confidence = clamp(b.confidence * pull + 0.5 * (1 - pull), 0.02, 0.99)


def assimilate(life: LifeRecord, user_style_terms: list[str], cfg: BeliefConfig) -> list[str]:
    """同化（11.4）：语言向对方靠拢——把对方的口头禅吸收进来（有上限）。"""
    added: list[str] = []
    catch = life.style.catchphrases
    for term in user_style_terms:
        term = term.strip()
        if 1 < len(term) <= 8 and term not in catch and len(catch) < 8:
            catch.append(term)
            added.append(term)
    if added:
        logger.info(f"同化：吸收了对方的口头禅 {added}")
    return added


def snapshot(life: LifeRecord) -> str:
    """信念快照（供提示词中期块）。列出漂移最明显的几条。"""
    if not life.beliefs:
        return ""
    ordered = sorted(life.beliefs, key=lambda b: (b.identity_locked, abs(b.confidence - 0.5)), reverse=True)
    return "\n".join(
        f"- {b.topic}：{b.statement}（确信度{b.confidence:.0%}"
        f"{'，核心' if b.identity_locked else ''}）"
        for b in ordered[:5]
    )


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s，。！？、；：,.;:!?]+", text or "") if len(t) >= 2]