"""心理内核：情绪 / 自控 / 关系 / 行为倾向（10.4 PSYCHE_STATE，4.7~4.8，12.1）。

原则：**心理量的演化全部是本地数值计算**（无 LLM 延迟），LLM 只用于"核算"细节。
所有函数都是纯函数式（传入状态、返回增量/写回），便于用 FakeClock 做确定性测试。
"""
from __future__ import annotations

import random
from datetime import datetime

from ..engine.config import ImperfectionConfig, OverrideConfig, RelationConfig, ReplyIntentConfig
from ..engine.types import (
    LifeRecord,
    OverrideState,
    PresenceState,
    PsycheState,
    Relationship,
    clamp,
    now_dt,
)

IMP_SILENCE = "silence"
IMP_ABSENT = "absent_minded"
IMP_RHETORICAL = "rhetorical"
IMP_STAMMER = "stammer"


# ======================================================================
# 初始化
# ======================================================================
def init_psyche(life: LifeRecord, now: datetime | None = None) -> PsycheState:
    """按人格初始化心理状态：心境底色 ← 气质"心境质量"；自控 ← 尽责/神经质。"""
    mq = life.temperament.mood_quality
    bf = life.big_five
    return PsycheState(
        mood_baseline=clamp((mq - 0.5) * 0.8, -0.8, 0.8),
        emotion_valence=clamp((mq - 0.5) * 0.4, -0.4, 0.4),
        emotion_arousal=0.25 + 0.3 * life.temperament.activity_level,
        self_control=clamp(0.45 + 0.35 * bf.conscientiousness - 0.30 * bf.neuroticism, 0.15, 1.0),
        fatigue=0.1,
        updated_at=now or now_dt(),
    )


def new_relationship(now: datetime | None = None) -> Relationship:
    """一段全新关系的起点（陌生）。"""
    return Relationship(last_active_at=now or now_dt())


# ======================================================================
# 情绪强度与自控（12.1）
# ======================================================================
def emotion_intensity(state: PsycheState, life: LifeRecord) -> float:
    """情绪强度 = 唤醒度 × 极性幅度 × 气质"反应强度"。

    反应强度高的人（intensity 大）同样的事情情绪更炸。
    """
    base = state.emotion_arousal * (0.4 + 0.6 * abs(state.emotion_valence))
    scale = 0.6 + 0.8 * life.temperament.intensity
    return clamp(base * scale)


def nudge_emotion(
    state: PsycheState,
    life: LifeRecord,
    cfg: RelationConfig,
    *,
    d_valence: float,
    d_arousal: float,
) -> None:
    """施加一次情绪冲击（带**情绪惯性**：不会因一句话瞬间反转）。"""
    inertia = clamp(cfg.emotion_inertia, 0.0, 0.95)
    # 反应强度放大冲击
    gain = 0.7 + 0.7 * life.temperament.intensity
    state.emotion_valence = clamp(
        state.emotion_valence + d_valence * gain * (1.0 - inertia), -1.0, 1.0
    )
    state.emotion_arousal = clamp(
        state.emotion_arousal + d_arousal * gain * (1.0 - inertia), 0.0, 1.0
    )


def tick(
    state: PsycheState,
    life: LifeRecord,
    now: datetime,
    presence: PresenceState,
    cfg: RelationConfig,
) -> None:
    """按**真实流逝时间**推进心理量的自然演化（情绪回落 / 自控恢复 / 疲惫）。

    由调用方周期触发（或每轮触发一次），传入 ``now`` 即可，无需逐秒调用。
    """
    elapsed_min = max(0.0, (now - state.updated_at).total_seconds() / 60.0)
    if elapsed_min <= 0:
        return

    sleeping = presence in (PresenceState.ASLEEP, PresenceState.RESTING)
    hour = now.hour

    # --- 疲惫：醒着缓慢累积，休息快速恢复 ---
    if sleeping:
        state.fatigue = clamp(state.fatigue - 0.06 * elapsed_min)
    else:
        load = 0.004 if presence == PresenceState.BUSY else 0.002
        state.fatigue = clamp(state.fatigue + load * elapsed_min)

    # --- 自控力：疲惫与深夜压制恢复；休息/睡眠加速恢复 ---
    recover = 0.05 if sleeping else 0.02
    late_night = 1.0 if (hour >= 23 or hour < 5) else 0.0
    state.self_control = clamp(
        state.self_control
        + (recover * (1.0 - 0.6 * state.fatigue) - 0.02 * late_night) * elapsed_min
    )

    # --- 情绪回落：向长期心境底色回归（半衰期 ~40min） ---
    pull = min(1.0, elapsed_min / 40.0 * (1.0 + 0.5 * (1.0 - cfg.emotion_inertia)))
    state.emotion_valence += (state.mood_baseline - state.emotion_valence) * pull
    state.emotion_arousal += (0.3 - state.emotion_arousal) * pull * 0.6

    state.updated_at = now


# ======================================================================
# 长期心情底色漂移（4.8：天级）
# ======================================================================
def drift_mood_baseline(
    state: PsycheState,
    life: LifeRecord,
    cfg: RelationConfig,
    rng: random.Random,
    day_key: int,
) -> None:
    """每天微调一次"今天整体是挺好还是丧"——由气质心境质量拉向基线 + 随机漂移。"""
    home = clamp((life.temperament.mood_quality - 0.5) * 0.8, -0.8, 0.8)
    noise = rng.gauss(0.0, cfg.mood_drift_sigma)
    state.mood_baseline = clamp(state.mood_baseline + (home - state.mood_baseline) * 0.25 + noise, -1.0, 1.0)


# ======================================================================
# 关系演化（4.7）
# ======================================================================
def grow_relationship(
    rel: Relationship,
    cfg: RelationConfig,
    *,
    rounds: int = 1,
    positive: float = 0.0,
    deep_talk: bool = False,
    conflict: bool = False,
) -> None:
    """一次互动后的关系增量（正面情绪 / 深夜交心→上升；冲突→下降）。"""
    rel.interactions += rounds
    # 高宜人性/高责任心的人更"慢热"还是更快？这里做通用简化
    bonus = 1.0 + 0.5 * positive + (0.6 if deep_talk else 0.0)
    if conflict:
        rel.intimacy = clamp(rel.intimacy - 0.02)
        rel.trust = clamp(rel.trust - 0.03)
        return
    rel.intimacy = clamp(rel.intimacy + cfg.intimacy_up_per_round * rounds * bonus)
    rel.trust = clamp(rel.trust + cfg.trust_up_per_round * rounds * bonus)
    rel.dependence = clamp(rel.dependence + cfg.dependence_up_per_round * rounds * bonus)
    rel.familiarity = clamp(rel.familiarity + cfg.familiarity_up_per_round * rounds)


def decay_by_neglect(rel: Relationship, cfg: RelationConfig, now: datetime) -> None:
    """长期冷落 → 依赖/亲密度缓慢下降（关系越亲，掉得越明显——被冷落更难受）。"""
    gap_h = (now - rel.last_active_at).total_seconds() / 3600.0
    if gap_h <= cfg.neglect_hours_decay:
        return
    days = (gap_h - cfg.neglect_hours_decay) / 24.0
    rel.dependence = clamp(rel.dependence - 0.05 * days * (0.5 + rel.dependence))
    rel.intimacy = clamp(rel.intimacy - 0.02 * days * (0.5 + rel.intimacy))


def record_grudge(rel: Relationship, trigger: str, valence: float, now: datetime | None = None) -> None:
    """记仇：写入情绪记忆，日后相关话题被触发（4.8）。"""
    rel.grudges.append(
        {"trigger": trigger[:40], "valence": valence, "ts": (now or now_dt()).isoformat()}
    )
    rel.grudges = rel.grudges[-20:]


def record_favor(rel: Relationship, trigger: str, valence: float, now: datetime | None = None) -> None:
    """记好。"""
    rel.favors.append(
        {"trigger": trigger[:40], "valence": valence, "ts": (now or now_dt()).isoformat()}
    )
    rel.favors = rel.favors[-20:]


# ======================================================================
# 上头判定（12.2）
# ======================================================================
def should_override(state: PsycheState, cfg: OverrideConfig, now: datetime) -> bool:
    """自控被情绪耗尽 → 越过阈值即上头。"""
    if state.override_state == OverrideState.OVERRIDDEN:
        return True
    if state.override_state == OverrideState.REGRET:
        return False
    load = state.emotion_intensity * (1.0 - state.self_control)
    if load <= cfg.threshold:
        return False
    if state.override_since is not None:
        gap_min = (now - state.override_since).total_seconds() / 60.0
        if gap_min < cfg.min_interval_minutes:
            return False
    return True


def override_flavor(life: LifeRecord) -> str:
    """越界"风格"：方向性约束（12.3）——高宜人+高尽责的人，越界多为自伤式。"""
    bf = life.big_five
    if bf.agreeableness > 0.62 and bf.conscientiousness > 0.55:
        return "sullen"        # 憋着、阴阳怪气、自我消化
    if life.attachment.value == "avoidant":
        return "cold"          # 冷暴力 / 突然不回
    if life.attachment.value == "anxious":
        return "clingy"        # 突然依赖 / 深夜说心里话
    if bf.neuroticism > 0.6:
        return "angry"         # 说狠话
    return "angry"


# ======================================================================
# 不完美行为（6.3 / M7）：概率受气质调制
# ======================================================================
def roll_imperfection(
    state: PsycheState,
    life: LifeRecord,
    cfg: ImperfectionConfig,
    rng: random.Random,
    *,
    is_interrupt: bool = False,
) -> str | None:
    """按低概率掷"人味失误"。返回类型或 None。"""
    if not cfg.enabled:
        return None

    t, bf = life.temperament, life.big_five
    if is_interrupt:
        p = cfg.interrupt_stammer * (0.6 + 0.8 * t.intensity)
        return IMP_STAMMER if rng.random() < p else None

    rolls: list[tuple[float, str]] = [
        # 注意："已读不回"不再靠随机掷骰（那是"架住的"），改由**回复意愿闸门**
        # （reply_willingness + ReplyIntentJudge）按当下情绪/疲惫/关系/对方言行裁定。
        # 注意分散 → 走神
        (cfg.absent_minded * (0.4 + 1.4 * t.distractibility), IMP_ABSENT),
        # 外向/活跃 → 更爱反问
        (cfg.rhetorical_question * (0.5 + 0.9 * bf.extraversion), IMP_RHETORICAL),
        # 低落/低宜人 → 更可能只回"……"
        (cfg.silence * (0.6 + 1.2 * max(0.0, -state.emotion_valence)) * (1.4 - bf.agreeableness), IMP_SILENCE),
    ]
    # 只在少数时候掷一次，避免每轮都触发多个
    for p, kind in rolls:
        if rng.random() < clamp(p, 0.0, 0.6):
            return kind
    return None


# ======================================================================
# 回复意愿（"她可以不回"）：确定性分数锚定
# ======================================================================
# 对方"负面 / 带刺"信号（越负面她越不想回）：普通负面轻、辱骂重
_NEG_WORDS = (
    "难过", "难受", "累", "烦", "生气", "讨厌", "委屈", "崩溃", "哭",
    "孤独", "失望", "无聊", "没意思", "烦死",
)
_ABUSE_WORDS = (
    "sb", "傻逼", "傻子", "智障", "废物", "垃圾", "去死", "恶心", "闭嘴",
    "滚", "滚蛋", "滚啊", "贱", "白痴", "你妈", "神经病",
)


def negativity_of(text: str) -> float:
    """对方当下言行的"负面/攻击性"强度（0..1）——骂人比单纯倒苦水更劝退。"""
    if not text:
        return 0.0
    neg = sum(1 for w in _NEG_WORDS if w in text)
    abuse = sum(1 for w in _ABUSE_WORDS if w in text)
    raw = 0.35 * neg + 0.8 * abuse
    return clamp(raw / 2.0)  # 两个信号即接近 1


def reply_willingness(
    state: PsycheState,
    rel: Relationship,
    cfg: ReplyIntentConfig,
    *,
    user_text: str,
    batch_count: int,
    now: datetime,
    jitter: float = 0.0,
) -> dict[str, float]:
    """确定性"回复意愿分"（0..1）+ 各因子明细（供日志 / 追溯）。

    意愿只由**当下可得的现成状态**驱动：情绪极性、疲惫、自控、关系（信任 / 亲密）、
    对方当下言行（负面辱骂、刷屏）、深夜硬聊。**不含话题兴趣**（用户明确排除）。
    纯函数 → 便于确定性测试（jitter 由调用方传入，测试可传 0）。
    """
    valence = state.emotion_valence                 # -1..1
    fatigue = state.fatigue                         # 0..1
    self_control = state.self_control               # 0..1
    trust = rel.trust                               # 0..1
    intimacy = rel.intimacy                         # 0..1
    negativity = negativity_of(user_text)             # 0..1
    # 刷屏惩罚只针对"密集短消息轰炸"（在吗/喂/???）——连发长句是掏心窝子，不是骚扰。
    # 平均每条字数越多 → earnest 越高 → 惩罚最多打 2 折（保留一点"话太密"的真实疲态）。
    avg_len = len(user_text) / max(1, batch_count)
    earnest = clamp((avg_len - 8.0) / 30.0)           # ≤8 字/条=纯催促；≥38 字/条=认真长谈
    burst = clamp(max(0, batch_count - 1) / 3.0) * (1.0 - 0.8 * earnest)
    late_night = 1.0 if (now.hour >= 23 or now.hour < 6) else (0.4 if now.hour == 22 else 0.0)

    score = (
        cfg.base
        + cfg.w_emotion * valence
        - cfg.w_fatigue * fatigue
        + cfg.w_self_control * self_control
        + cfg.w_trust * trust          # 关系好 = 加意愿；陌生人不倒扣，避免"刚认识就不理"
        + cfg.w_intimacy * intimacy
        - cfg.w_negativity * negativity
        - cfg.w_burst * burst
        - cfg.w_late_night * late_night
        + jitter
    )
    return {
        "score": round(clamp(score, 0.0, 1.0), 4),
        "emotion": round(valence, 4),
        "fatigue": round(fatigue, 4),
        "self_control": round(self_control, 4),
        "trust": round(trust, 4),
        "intimacy": round(intimacy, 4),
        "negativity": round(negativity, 4),
        "burst": round(burst, 4),
        "late_night": round(late_night, 4),
        "jitter": round(jitter, 4),
    }


# ======================================================================
# 表达调制（情绪 → 措辞/节奏，4.8）
# ======================================================================
def expression_modifiers(state: PsycheState) -> dict[str, float]:
    """把情绪映射成"说话方式"的调制系数，供 timing / style 使用。"""
    v, a = state.emotion_valence, state.emotion_arousal
    return {
        # 兴奋 → 更快、句更短更多；低落 → 更慢更长
        "speed": clamp(0.75 + 0.5 * a - 0.15 * max(0.0, -v), 0.5, 1.6),
        "sentence_scale": clamp(1.0 + 0.35 * v - 0.25 * a, 0.6, 1.5),
        "emoji_boost": clamp(0.4 + 0.8 * max(0.0, v) + 0.4 * a, 0.0, 2.0),
        "ellipsis_boost": clamp(0.3 + 1.5 * max(0.0, -v), 0.0, 2.0),
    }