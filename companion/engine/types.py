"""统一数据模型（引擎与前端无关，全部走这里定义的类型）。

分两层职责：

  * **传输层类型**：``IncomingMessage`` / ``OutgoingEvent`` / ``Segment`` —— 引擎与接入层之间的协议；
  * **领域实体**：``LifeRecord`` / ``PsycheState`` / ``Relationship`` / ``Belief`` —— 生命体的数据契约。

行为逻辑分别放在 ``mind/*``（心理内核）与 ``cognition/*``（认知层），本文件只放"数据结构"，
避免各层各自造轮子、也避免循环依赖。
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ======================================================================
# 基础工具
# ======================================================================
def new_id(prefix: str) -> str:
    """生成带前缀的短 ID，便于日志里一眼看出对象类型。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_dt() -> datetime:
    """当前本地时间（带时区）。

    统一从这里取"墙上时间"，业务内部做持续计算一律走 Clock。
    """
    return datetime.now().astimezone()


def monotonic_ms() -> int:
    """单调时钟毫秒，用于耗时统计（与墙上时间解耦）。"""
    return int(time.monotonic() * 1000)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """把数值夹到区间内——心理量纲统一在 [0,1] 或 [-1,1]，到处都要用。"""
    return max(lo, min(hi, x))


# ======================================================================
# 枚举
# ======================================================================
class Channel(str, Enum):
    """接入渠道。Demo 内置 web；qq 走 OneBot v11；其余为后续 IM 预留。"""

    WEB = "web"
    CLI = "cli"
    IM = "im"
    QQ = "qq"


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    PROACTIVE = "proactive"     # 伙伴主动发起
    EVENT = "event"             # 内部事件（如"被叫醒"），用于落盘/展示


class PresenceState(str, Enum):
    """慢层：生命状态（可及性）。"""

    ACTIVE = "active"
    BUSY = "busy"
    AFK = "afk"
    DND = "dnd"
    RESTING = "resting"
    ASLEEP = "asleep"
    OFFLINE = "offline"

    @property
    def is_resting(self) -> bool:
        """是否需要累积唤醒值才能响应。"""
        return self in (PresenceState.RESTING, PresenceState.ASLEEP)

    @property
    def is_unreachable(self) -> bool:
        """是否完全不可达（消息只积压）。"""
        return self in (PresenceState.OFFLINE, PresenceState.DND)


class TurnState(str, Enum):
    """快层：会话状态（正在怎么回）。"""

    IDLE = "idle"
    PERCEIVING = "perceiving"   # 已看到消息，还在"反应"（等待 + 感知延迟）
    THINKING = "thinking"       # 组织语言（思考延迟 + 生成）
    SPEAKING = "speaking"       # 正在说（打字时延）
    STOPPING = "stopping"       # 被强制停止，丢弃剩余输出


class AttachmentStyle(str, Enum):
    """依恋风格（10.3）。"""

    SECURE = "secure"
    ANXIOUS = "anxious"
    AVOIDANT = "avoidant"
    DISORGANIZED = "disorganized"


class OverrideState(str, Enum):
    """情绪上头状态机（12.2）。"""

    NORMAL = "normal"
    OVERRIDDEN = "overridden"
    REGRET = "regret"


class InboxStatus(str, Enum):
    QUEUED = "queued"           # 排队中（等可及/等唤醒）
    DELIVERED = "delivered"     # 已交给会话处理
    DROPPED = "dropped"         # 被丢弃（已读不回等）


# ======================================================================
# 传输层：消息 / 片段 / 出站事件
# ======================================================================
class ChatMessage(BaseModel):
    """一条落盘的聊天消息（用户 / 伙伴 / 系统 / 主动 / 事件）。"""

    id: str = Field(default_factory=lambda: new_id("msg"))
    life_id: str
    user_id: str
    role: Role
    content: str
    created_at: datetime = Field(default_factory=now_dt)
    channel: Channel = Channel.WEB
    meta: dict[str, Any] = Field(default_factory=dict)


class IncomingMessage(BaseModel):
    """从接入层进入引擎的入站消息（尚未被状态机处理）。"""

    id: str = Field(default_factory=lambda: new_id("in"))
    life_id: str
    user_id: str
    content: str
    channel: Channel = Channel.WEB
    received_at: datetime = Field(default_factory=now_dt)
    # 同一批连发消息归为一组，供"合并理解"（4.11）
    merge_group: str = Field(default_factory=lambda: new_id("grp"))
    meta: dict[str, Any] = Field(default_factory=dict)


class Segment(BaseModel):
    """回复被切成的一段（一条气泡）。

    ``typing_seconds`` 是"说这一段要花的时间"，由节奏引擎（M3）算出，
    前端据此表演打字机——**生成可以快，展示必须慢**。
    """

    text: str
    typing_seconds: float = 0.0
    # 越界（上头）时标记 True，前端可特殊呈现；也是事后闭环的锚点
    out_of_character: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)


class OutgoingEvent(BaseModel):
    """引擎 → 前端的出站事件。

    前端按 ``type`` 分派：

      - chunk      流式文本片段（一个气泡的内容）
      - message    一条完整消息（已定稿，落盘后回显）
      - typing     正在输入开关
      - state      会话状态（快层）
      - presence   生命状态（慢层）
      - wake       唤醒值进度（休息中）
      - delivered  已读回执（用户消息被"看到"）
      - notice     系统提示（如"对方可能不在"）
      - error      出错提示
      - sticker    发一张表情包（信道无关：QQ 侧发 image 段，Web 侧渲染 <img>）
      - poke       戳一戳（信道无关：QQ 侧 send_poke，Web 侧渲染文字气泡）
    """

    type: Literal[
        "chunk", "message", "typing", "state", "presence",
        "wake", "delivered", "notice", "error",
        "sticker", "poke",
    ]
    life_id: str
    user_id: str
    ts: datetime = Field(default_factory=now_dt)

    text: str | None = None
    role: Role | None = None
    message_id: str | None = None
    state: TurnState | None = None
    presence: PresenceState | None = None
    typing: bool | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    # 表情包事件专用（type="sticker"）：本地稳定 ID + 出站图片 URL
    sticker_id: str | None = None
    sticker_url: str | None = None


# ======================================================================
# 领域实体：生命体（LIFE / Psyche）—— 见设计文档 10.4
# ======================================================================
class Temperament(BaseModel):
    """气质 9 维（Thomas & Chess）。先天，几乎不变；直接映射为交互参数。"""

    activity_level: float = 0.5    # 活动水平 → 打字速度、主动频率
    regularity: float = 0.5        # 规律性 → 作息固定程度
    approach: float = 0.5          # 趋近/退缩 → 面对新事物主动还是回避
    adaptability: float = 0.5      # 适应性 → 接受新话题/被说服速度
    threshold: float = 0.5         # 反应阈 → 多小刺激就有情绪（越低越敏感）
    intensity: float = 0.5         # 反应强度 → 情绪波动幅度、易上头
    mood_quality: float = 0.5      # 心境质量 → 默认偏乐观还是偏丧
    distractibility: float = 0.5   # 注意分散 → 容易跳话题/走神
    persistence: float = 0.5       # 坚持性 → 唠叨/记仇/不易被转移话题

    def describe(self) -> str:
        """转成给模型的自然语言描述（高/中/低三档）。"""
        def band(v: float, lo: str, mid: str, hi: str) -> str:
            return lo if v < 0.34 else (hi if v > 0.66 else mid)

        return (
            f"活动水平{band(self.activity_level, '偏低', '中等', '偏高')}、"
            f"规律性{band(self.regularity, '随性', '一般', '很强')}、"
            f"对新事物{band(self.approach, '偏回避', '中性', '偏主动')}、"
            f"适应性{band(self.adaptability, '较慢', '一般', '很快')}、"
            f"敏感度{band(self.threshold, '迟钝', '一般', '很敏感')}、"
            f"情绪强度{band(self.intensity, '平稳', '一般', '强烈')}、"
            f"心境{band(self.mood_quality, '偏丧', '中性', '偏乐观')}、"
            f"注意{band(self.distractibility, '很专注', '一般', '容易分心')}、"
            f"坚持性{band(self.persistence, '随和', '一般', '很执着')}"
        )


class BigFive(BaseModel):
    """大五人格（OCEAN）。长期微变。"""

    openness: float = 0.5
    conscientiousness: float = 0.5
    extraversion: float = 0.5
    agreeableness: float = 0.5
    neuroticism: float = 0.5

    def describe(self) -> str:
        def band(v: float, lo: str, mid: str, hi: str) -> str:
            return lo if v < 0.34 else (hi if v > 0.66 else mid)

        return (
            f"开放性{band(self.openness, '偏保守', '中等', '偏好奇')}、"
            f"尽责性{band(self.conscientiousness, '偏随性', '中等', '偏自律')}、"
            f"外向性{band(self.extraversion, '偏内向', '中间', '偏外向')}、"
            f"宜人性{band(self.agreeableness, '偏自我', '中等', '偏温柔')}、"
            f"神经质{band(self.neuroticism, '情绪很稳', '中等', '情绪起伏大')}"
        )


class NeedFear(BaseModel):
    """核心需求 / 核心恐惧 / 逆鳞（底线）。"""

    kind: Literal["need", "fear", "boundary"]
    content: str
    weight: float = 0.5


class Quirk(BaseModel):
    """怪癖 / 偏好 / 口头禅 / 表情习惯。可被同化（11.4）。"""

    type: Literal["catchphrase", "preference", "tics", "emoji"]
    content: str


class Belief(BaseModel):
    """一条三观（信念节点）——见 11.1。"""

    id: str = Field(default_factory=lambda: new_id("bel"))
    topic: str
    statement: str
    confidence: float = 0.7          # 确信度 0..1
    malleability: float = 0.5        # 可塑性 0..1
    identity_locked: bool = False    # 核心身份信念（改变极慢、强推逆反）
    exposure_count: int = 0          # 累计被提及次数（单纯曝光效应）
    updated_at: datetime = Field(default_factory=now_dt)


class Cognition(BaseModel):
    """一条「她对这个世界的认知」（14. 认知习得）。

    与 ``Belief`` 的区别：Belief 是**三观/立场**（对错、该不该），随说服漂移；
    Cognition 是**世界知识**（"这是什么、怎么回事"），由对话中反复接触**新增**而来。
    它不绑定 user_id——学到的东西属于她自己，换个人聊天也还在。
    """

    id: int | None = None
    life_id: str
    topic: str
    understanding: str
    confidence: float = 0.3          # 确信度（学会的程度）
    malleability: float = 0.8        # 可塑性（越新越高，越用越低）
    source: str = "dialogue"         # dialogue / innate / taught / corrected
    exposure_count: int = 0          # 被反复印证次数
    correction_count: int = 0        # 被用户纠正次数
    taught_by: str = ""              # 谁教的（来源为 taught 时才有值）
    locked: bool = False             # 是否已上锁（用户教/纠正过，自学不可覆写）
    learned_at: datetime = Field(default_factory=now_dt)
    updated_at: datetime = Field(default_factory=now_dt)


class StyleProfile(BaseModel):
    """说话风格档（4.9）——最直观的去 AI 味一层。"""

    fillers: list[str] = Field(default_factory=lambda: ["嗯", "诶", "啊"])
    catchphrases: list[str] = Field(default_factory=list)
    punct_style: str = "少用句号，常用空格和省略号，偶尔波浪号"
    emoji_rate: float = 0.25               # 表情使用频率 0..1
    emojis: list[str] = Field(default_factory=lambda: ["🙂", "😂", "🥺", "😳", "😌"])
    sentence_len: int = 18                 # 单条气泡期望字数
    split_prob: float = 0.45               # 长句拆成多条的概率
    max_bubbles: int = 3                   # 最多拆几条
    net_speak: list[str] = Field(default_factory=list)   # 网络用语
    typo_rate: float = 0.0                 # 错字/谐音概率（适度更真实）
    # 分条习惯（说话习惯的核心差异，决定"怎么发"而不是"说什么"）：
    #   fragmented=碎碎念，多段连发（一句一发）  chatty=短句连发（常两三条）
    #   steady=不长不短，一小段（偶尔拆两条）    monologue=一段话说完（能一条就一条）
    bubble_habit: Literal["fragmented", "chatty", "steady", "monologue"] = "steady"


class LifeRecord(BaseModel):
    """生命体一级实体：先天人格 + 后天演化载体（10.1 / 10.4）。"""

    life_id: str
    name: str
    seed: int = 0
    gender: Literal["female", "male", "neutral"] = "female"
    avatar: str = "她"
    born_at: datetime = Field(default_factory=now_dt)
    status: Literal["alive", "dormant"] = "alive"
    # 来源：random=掷骰生成 / custom=用户自定义 / blank=纯洁空白体（需逐步习得认知）
    origin: Literal["random", "custom", "blank"] = "random"
    # 认知成熟度 0..1：blank 体从 0 起步，随"认知习得"增长（驱动提示词分档）
    awareness: float = 1.0
    backstory: str = ""

    temperament: Temperament = Field(default_factory=Temperament)
    big_five: BigFive = Field(default_factory=BigFive)
    attachment: AttachmentStyle = AttachmentStyle.SECURE
    needs_fears: list[NeedFear] = Field(default_factory=list)
    quirks: list[Quirk] = Field(default_factory=list)
    beliefs: list[Belief] = Field(default_factory=list)

    style: StyleProfile = Field(default_factory=StyleProfile)
    # 身份设定事实（key→value），供一致性自检（4.12）
    canon: dict[str, str] = Field(default_factory=dict)
    # 作息表（覆盖 config，可选）
    schedule: dict[str, Any] = Field(default_factory=dict)

    def canon_text(self) -> str:
        if not self.canon:
            return ""
        return "；".join(f"{k}：{v}" for k, v in self.canon.items())


class PsycheState(BaseModel):
    """心理运行时状态（10.4 PSYCHE_STATE）。"""

    mood_baseline: float = 0.0        # 长期心情底色（天级）-1..1，以气质 mood_quality 初始化
    emotion_valence: float = 0.0      # 短期情绪极性 -1..1（分钟级）
    emotion_arousal: float = 0.35     # 唤醒度 0..1
    self_control: float = 1.0         # 自控力 0..1（会被疲惫/情绪/深夜耗尽）
    fatigue: float = 0.0              # 疲惫 0..1
    override_state: OverrideState = OverrideState.NORMAL
    override_since: datetime | None = None
    updated_at: datetime = Field(default_factory=now_dt)

    @property
    def emotion_intensity(self) -> float:
        """情绪强度：唤醒度 × 极性幅度（12.1 的简化合成）。

        实际计算还会乘上气质"反应强度"，见 ``mind.psyche``。
        """
        return clamp(self.emotion_arousal * (0.4 + 0.6 * abs(self.emotion_valence)))

    @property
    def mood_label(self) -> str:
        v, a = self.emotion_valence, self.emotion_arousal
        if v > 0.35 and a > 0.55:
            return "兴奋"
        if v > 0.25:
            return "开心"
        if v < -0.35 and a > 0.55:
            return "烦躁"
        if v < -0.25:
            return "低落"
        return "平静"


class Relationship(BaseModel):
    """对某个用户的四维关系（4.7）。"""

    intimacy: float = 0.05
    trust: float = 0.10
    dependence: float = 0.05
    familiarity: float = 0.05
    interactions: int = 0
    last_active_at: datetime = Field(default_factory=now_dt)
    # 情绪记忆：谁在什么时候惹我/哄我（记好记仇）
    grudges: list[dict[str, Any]] = Field(default_factory=list)
    favors: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def stage(self) -> str:
        score = (self.intimacy + self.trust + self.dependence + self.familiarity) / 4
        if score < 0.28:
            return "陌生"
        if score < 0.58:
            return "熟悉"
        return "亲密"


# ======================================================================
# 跨层上下文（会话一轮所需的全部输入）
# ======================================================================
class TurnContext(BaseModel):
    """一轮 turn 的完整输入快照。

    由会话编排器组装，交给 ``Responder`` 生成；生成器**无副作用**，
    状态更新统一在 turn 结束后由编排器结算（先回复、后结算）。
    """

    model_config = {"arbitrary_types_allowed": True}

    life: LifeRecord
    psyche: PsycheState
    relationship: Relationship
    user_id: str
    batch: list[IncomingMessage] = Field(default_factory=list)
    now: datetime = Field(default_factory=now_dt)
    presence: PresenceState = PresenceState.ACTIVE
    task: Literal["reply", "wake", "proactive", "override"] = "reply"
    wake_reason: str | None = None
    # 记忆召回结果（短期原文 / 中期摘要 / 长期事实 / 相关历史），已格式化好的文本块
    summary_text: str = ""
    facts_text: str = ""
    recall_text: str = ""
    topics_text: str = ""
    # 她已经懂得的事（认知习得产物，blank 体靠它"长大"）
    cognition_text: str = ""
    # 可用表情包素材块（由 session 填充；空 = 本轮不许发表情）
    sticker_block: str = ""
    # 命中的候选信念（供 M13 说服力学）
    candidate_beliefs: list[Belief] = Field(default_factory=list)
    # 不完美行为指令（M7）：silence / absent_minded / rhetorical / stammer
    imperfection: str | None = None
    # 话题/生活线素材
    life_note: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def user_text(self) -> str:
        """本批用户消息合并后的文本。"""
        return "\n".join(m.content for m in self.batch)


class TimingBreakdown(BaseModel):
    """一轮的时序拆解（第 5 节），单位秒。"""

    d_avail: float = 0.0
    d_perceive: float = 0.0
    d_think: float = 0.0
    d_type: float = 0.0          # 各段之和
    seg_gaps: float = 0.0
    segments: int = 0

    @property
    def total(self) -> float:
        return self.d_avail + self.d_perceive + self.d_think + self.d_type + self.seg_gaps


class WakeResult(BaseModel):
    """唤醒累积结果。"""

    life_id: str
    progress: float = 0.0
    delta: float = 0.0
    woke: bool = False
    reason: str = ""


class InboxItem(BaseModel):
    """收件箱条目（7. 数据模型 INBOX_ITEM）。"""

    id: str = Field(default_factory=lambda: new_id("ibx"))
    incoming: IncomingMessage
    status: InboxStatus = InboxStatus.QUEUED
    merge_group: str = ""
    received_at: datetime = Field(default_factory=now_dt)