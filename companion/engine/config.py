"""强类型配置（TOML → pydantic）。

所有"可调参数"集中在此，代码各处只依赖这些模型，**不直接读 TOML**。
调参 = 改 ``config.toml``，无需动代码。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from .types import StyleProfile  # noqa: F401  (供外部 import 便利)


# ---- 基础 -------------------------------------------------------------
class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8788


class EngineConfig(BaseModel):
    time_scale: float = 1.0
    rng_seed: int | None = 20261013


class IdentityConfig(BaseModel):
    life_id: str = "life_demo"
    user_id: str = "user_demo"


class StoreConfig(BaseModel):
    path: str = "data/companion.db"


# ---- M5 生命状态 ------------------------------------------------------
class PresenceConfig(BaseModel):
    enabled: bool = True
    wake_at: str = "07:30"
    sleep_at: str = "23:30"
    busy_periods: list[str] = Field(default_factory=lambda: ["09:00-12:00", "14:00-18:00"])
    rest_periods: list[str] = Field(default_factory=lambda: ["13:00-14:00"])
    jitter_minutes: int = 25
    afk_chance_per_hour: float = 0.35


# ---- M6 唤醒 ----------------------------------------------------------
class WakeConfig(BaseModel):
    base: float = 0.15
    call_bonus: float = 0.25
    chain_bonus: float = 0.10
    chain_window_s: float = 30.0
    decay_per_min: float = 0.02
    resting_factor: float = 0.8
    asleep_factor: float = 0.4
    sleep_back_chance: float = 0.25
    # 呼唤词（命中即加 call_bonus）
    call_words: list[str] = Field(
        default_factory=lambda: ["在吗", "在么", "醒醒", "喂", "起床", "醒一醒", "@", "叫", "起来"]
    )


# ---- M3 时序 ----------------------------------------------------------
class TimingConfig(BaseModel):
    perceive_min: float = 0.35
    perceive_max: float = 1.8
    think_min: float = 1.0
    think_max: float = 5.5
    typing_cps: float = 7.0   # 真人中文手速≈4~8 字/秒；调低后不再"唰"地出完
    seg_gap_min: float = 0.4
    seg_gap_max: float = 1.4
    d_avail_busy_min: float = 20.0
    d_avail_busy_max: float = 150.0
    # 个人"忙时回信"档位（schedule.busy_reply）对应的可及性延迟区间：
    #   quick = 工作性质允许随时看手机（运营/客服/自由职业）——忙也基本秒回
    #   slow  = 深度工作不能分心（医生/授课/开刀/开车）——忙起来真顾不上
    d_avail_quick_min: float = 2.0
    d_avail_quick_max: float = 12.0
    d_avail_slow_min: float = 240.0
    d_avail_slow_max: float = 1800.0
    d_avail_afk_min: float = 300.0
    d_avail_afk_max: float = 2400.0


# ---- M7 不完美行为 ----------------------------------------------------
class ImperfectionConfig(BaseModel):
    already_read_ignore: float = 0.05   # 已保留字段（"已读不回"改由回复意愿闸门决定）
    absent_minded: float = 0.04
    rhetorical_question: float = 0.15
    silence: float = 0.03
    interrupt_stammer: float = 0.30
    enabled: bool = True


# ---- 回复意愿闸门（"她可以不回"）--------------------------------------
class ReplyIntentConfig(BaseModel):
    """她**自主决定回不回**——不是"每条必回"。

    在「感知 + 连发合并之后、思考之前」插入意愿闸门：先用**确定性分数**锚定意愿
    （情绪 / 疲惫 / 自控 / 关系 / 对方当下言行），只在灰区才惊动一次轻量 mind 模型，
    输出 reply / terse / delay / skip 四档行为。

    反陷阱：**绝不直接问大模型"要不要回"**（RLHF 有问必答 → 恒回）。
    必须是"确定性分数定档 + 灰区才问 + 有限档位"，且任何异常都兜底为"正常回"。
    """

    enabled: bool = True
    # 三档路由：>= direct_reply 直接回（不调模型）；<= direct_skip 直接跳过；中间为灰区
    direct_reply: float = 0.62
    direct_skip: float = 0.28
    use_mind_model: bool = True          # 灰区是否调用 mind 模型裁决（关掉则灰区走本地启发式）
    # 意愿分权重（确定性锚定）
    base: float = 0.45
    w_emotion: float = 0.30              # 情绪极性 +
    w_fatigue: float = 0.35             # 疲惫 −
    w_self_control: float = 0.20        # 自控 +
    w_trust: float = 0.15               # 信任（0..1，关系越好越想回）+
    w_intimacy: float = 0.10            # 亲密（0..1，关系越好越想回）+
    w_negativity: float = 0.30          # 对方负面/辱骂 −
    w_burst: float = 0.20               # 刷屏压力 −
    w_late_night: float = 0.15          # 深夜硬聊 −
    jitter: float = 0.05                # 抖动（防机械）
    # 行为参数
    max_delay_s: float = 6.0            # delay 档额外最长等待（秒）
    skip_cooldown_s: float = 120.0      # 刚 skip 过，短期内不再 skip（避免像挂机）
    proactive_floor: float = 0.40       # 意愿分低于此值时不主动发起（与主动性意愿闸门共用）


# ---- M8 记忆 ----------------------------------------------------------
class MemoryConfig(BaseModel):
    short_window_rounds: int = 12
    summarize_every_rounds: int = 8
    fact_extract_every_rounds: int = 6
    recall_top_k: int = 6
    recall_half_life_days: float = 7.0


# ---- M9 关系 / 情绪 ---------------------------------------------------
class RelationConfig(BaseModel):
    intimacy_up_per_round: float = 0.006
    trust_up_per_round: float = 0.004
    dependence_up_per_round: float = 0.005
    familiarity_up_per_round: float = 0.008
    neglect_hours_decay: float = 24.0
    mood_drift_sigma: float = 0.06
    emotion_inertia: float = 0.35


# ---- M11 主动性 -------------------------------------------------------
class ProactiveConfig(BaseModel):
    enabled: bool = True
    min_gap_minutes: float = 5.0
    threshold: float = 0.55
    check_interval_s: float = 45.0
    weights: dict[str, float] = Field(
        default_factory=lambda: {"topic": 0.35, "emotion": 0.25, "time": 0.2, "intimacy": 0.2}
    )


# ---- M12 创世 ---------------------------------------------------------
class GenesisConfig(BaseModel):
    # 生成失败重试次数（LLM 细节生成 + 本地校验）
    max_retries: int = 2


# ---- M13 信念漂移 -----------------------------------------------------
class BeliefConfig(BaseModel):
    k_repeat: float = 0.06
    identity_resist: float = 0.65
    reactance_threshold: float = 0.55
    decay_half_life_days: float = 30.0
    assimilation_rate: float = 0.05


# ---- M14 情绪上头 -----------------------------------------------------
class OverrideConfig(BaseModel):
    threshold: float = 0.62
    min_interval_minutes: float = 20.0
    regret_delay_minutes: float = 25.0


# ---- 主动教导（14.1）--------------------------------------------------
class TeachingConfig(BaseModel):
    """用户显式"记住……"式教导：命中即写成上锁认知。"""

    enabled: bool = True
    max_per_round: int = 2    # 一轮最多接纳几条教导（防止刷屏式投喂）


# ---- 认知习得约束（14. 认知习得）-------------------------------------
class CognitionConfig(BaseModel):
    """给"概念表"上三重约束，防止无限膨胀 + 垃圾概念灌入。

    背景：旧实现每轮无条件把用户话里任意 2~6 字中文片段当"新概念"写库，
    且**无总量上限**，结果问候语 / 语气词 / 脏话 / 整句话被永久记成"认知"，
    概念条数无限增长。这里加：

      * 硬上限 ``max_rows``：到顶后**只更新已有条目、不再新增**（严防无限增长）；
      * 置信门槛 ``min_confidence``：低于此确信度不采纳（旧垃圾 0.32 全被挡下）；
      * 节奏 ``learn_every_rounds`` / ``max_new_per_round``：降频 + 限量。

    用户主动教导 / 锁定条目走独立链路（``teaching``），不受本上限影响。
    """

    enabled: bool = True
    max_rows: int = 60               # 单个生命体认知条目总量上限
    min_confidence: float = 0.45     # 低于此确信度的"概念"不予采纳
    max_new_per_round: int = 1       # 每轮最多新增几条新概念
    learn_every_rounds: int = 2      # 每 N 轮才尝试学一次


# ---- 连发合并（4.11）--------------------------------------------------
class CoalesceConfig(BaseModel):
    """把用户"一段话拆成几条"的消息收拢成**一轮**再理解。

    她还没开口（感知 / 等待 / 思考中）时，先把对方"这段话还没说完"的内容等齐：
    静默 ``settle_s`` 秒没新消息就定稿；若对方持续打字，最多再多等 ``max_wait_s``。
    生成期间又冒出新消息时，最多补充重生成 ``regen_max`` 次（她尚未开口，不算插话）。
    """

    enabled: bool = True
    settle_s: float = 1.6        # 对方安静这么久 = "这句话说完了"
    max_wait_s: float = 8.0      # 持续打字时最多再多等这么久（防饿死）
    regen_max: int = 1           # 生成后又有新消息，最多重生成几次


# ---- 打断（4.6 / 6.3）------------------------------------------------
class InterruptConfig(BaseModel):
    """她**正在说话**时用户又发消息 → 何时才允许打断。

    默认策略：单条消息**不打断**（入队、等她说完整轮再统一回），
    只有"窗口内累计 ≥ burst_threshold 条"或命中喊停词时才打断重开。
    这样既尊重"她说了半句话还没说完"，也避免逐字追问把整轮反复打断。
    """

    enabled: bool = True
    burst_threshold: int = 2          # 窗口内累计这么多条才打断
    window_s: float = 15.0            # 连发计数窗口（秒）
    stop_words: list[str] = [         # 命中即立刻打断（明显是"喊停/纠正"意图）
        "等一下", "等下", "停一下", "打住", "别说了", "你先停", "打断一下",
    ]


# ---- 汇总 -------------------------------------------------------------
class LLMConfigModel(BaseModel):
    """与 cognition.llm.LLMConfig 字段一致；此处避免反向依赖。"""

    base_url: str = ""
    api_key: str = ""
    chat_model: str = "gpt-4o-mini"
    mind_model: str = "gpt-4o-mini"
    temperature: float = 0.9
    max_tokens: int = 512
    timeout_s: float = 60.0


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    llm: LLMConfigModel = Field(default_factory=LLMConfigModel)
    presence: PresenceConfig = Field(default_factory=PresenceConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    imperfection: ImperfectionConfig = Field(default_factory=ImperfectionConfig)
    reply_intent: ReplyIntentConfig = Field(default_factory=ReplyIntentConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    relation: RelationConfig = Field(default_factory=RelationConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    genesis: GenesisConfig = Field(default_factory=GenesisConfig)
    belief: BeliefConfig = Field(default_factory=BeliefConfig)
    override: OverrideConfig = Field(default_factory=OverrideConfig)
    teaching: TeachingConfig = Field(default_factory=TeachingConfig)
    cognition: CognitionConfig = Field(default_factory=CognitionConfig)
    coalesce: CoalesceConfig = Field(default_factory=CoalesceConfig)
    interrupt: InterruptConfig = Field(default_factory=InterruptConfig)


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return AppConfig(**raw)