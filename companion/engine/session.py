"""会话状态机（快层）+ 编排器（= 设计文档里的"单会话 Actor"）。

三条铁律（M1 沿用）：
1. **一个 turn = 一个 asyncio.Task**，打断 = ``task.cancel()``；
2. 打断**只取消生成**，已推给前端的内容原样保留；
3. 所有延时走 ``Clock``，状态切换都 emit 出去（前端角标看得见）。

M2+ 在此之上编排整条流水线：闸门（可及性 / 唤醒）→ 感知 → 思考（记忆 + LLM）→
说话（逐条节奏重放）→ **先回复、后结算**（记忆落盘 + 情绪/关系/信念演化）。
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from loguru import logger

from ..cognition.learning import CognitionLearner
from ..cognition.memory import MemoryManager
from ..cognition.responder import CompanionResponder
from ..cognition.teaching import TeachingEngine
from ..mind import belief as belief_mod
from ..mind import override as override_mod
from ..mind import psyche as psyche_mod
from ..mind.mind_llm import MindAccountant, ReplyIntentJudge
from ..store.db import Database
from .bus import TOPIC_CONTROL, TOPIC_INCOMING, TOPIC_OUTBOUND, EventBus
from .clock import Clock
from .config import AppConfig
from .expression import ExpressionController
from .inbox import Inbox
from .registry import LifeRegistry
from .timing import TimingModel
from .types import (
    IncomingMessage,
    OutgoingEvent,
    PresenceState,
    Role,
    Segment,
    TurnContext,
    TurnState,
    new_id,
    now_dt,
)
from .wake import WakeArbiter

IMP_STAMMER = "stammer"

# 「已读未回」标记（存在 state 表里）：记下她"看到了但选择没回"，
# 供下一轮模型理解（否则会误判自己没看见 → 无理由回避、人设崩）
INTENT_SCOPE = "intent"
INTENT_KIND = "last_skip"
SKIP_NOTE_TTL_S = 6 * 3600.0   # 超过这个时长不再回放这条提醒（避免翻出陈年旧账）

# 「清空记录」的水印 scope/kind（存在 state 表里，不删任何 message 行）
HIST_SCOPE = "hist"
HIST_KIND = "cleared_at"


def _parse_ts(raw: str | None) -> datetime | None:
    """把落库的时间串解析成带时区 datetime；非法/缺失返回 None。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_dt().tzinfo)
    return dt


class Responder(Protocol):
    """回复生成器（无副作用）。"""

    async def respond(
        self, ctx: TurnContext, history: list[dict[str, str]], rng: random.Random
    ) -> list[Segment]: ...


@dataclass
class TurnRequest:
    """一次 turn 的请求（由编排器组装）。"""

    batch: list[IncomingMessage]
    task: str = "reply"                 # reply / wake / proactive / override
    wake_reason: str | None = None
    imperfection: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # 是"插话打断"产生的重开（用于断续话术）
    interrupted: bool = False


@dataclass
class SessionDeps:
    """会话共享依赖（避免 Session 到处 import / 层层透传）。"""

    bus: EventBus
    clock: Clock
    registry: LifeRegistry
    memory: MemoryManager
    responder: CompanionResponder
    timing: TimingModel
    expression: ExpressionController
    accountant: MindAccountant
    learner: CognitionLearner
    teaching: TeachingEngine
    reply_judge: ReplyIntentJudge
    config: AppConfig
    rng: random.Random
    db: Database


class Session:
    """单个 (life_id, user_id) 的会话（Actor：串行 + 可取消）。"""

    def __init__(self, life_id: str, user_id: str, deps: SessionDeps) -> None:
        self.life_id = life_id
        self.user_id = user_id
        self._d = deps
        self.inbox = Inbox()

        self._state = TurnState.IDLE
        self._turn: asyncio.Task[None] | None = None
        # 她"正在说话"期间到达的消息时间戳（阈值打断判定用，进入 SPEAKING 时清零）
        self._speak_burst: list[datetime] = []
        # 回复意愿闸门：上次"自主不回"的时刻（冷却用）+ 最近一次裁决（调试面板展示）
        self._last_skip_at: datetime | None = None
        self.last_intent: dict[str, Any] = {}

    # ---- 只读状态 ------------------------------------------------------
    @property
    def state(self) -> TurnState:
        return self._state

    @property
    def busy(self) -> bool:
        return self._turn is not None and not self._turn.done()

    @property
    def speaking(self) -> bool:
        """她是否**正在说话**——只有这个阶段的新消息才算"插话"。"""
        return self.busy and self._state == TurnState.SPEAKING

    def should_interrupt(self, incoming: IncomingMessage, now: datetime) -> bool:
        """她说话期间又收到消息：是否达到"打断"阈值。

        默认单条不打断（入队、等她说完整轮再回）；只有窗口内累计 ≥
        ``burst_threshold`` 条、或命中喊停词（"等一下/别说了"…）才打断。
        """
        cfg = self._d.config.interrupt
        if not cfg.enabled:
            return False
        if any(w and w in incoming.content for w in cfg.stop_words):
            return True
        self._speak_burst = [
            t for t in self._speak_burst if (now - t).total_seconds() <= cfg.window_s
        ]
        self._speak_burst.append(now)
        return len(self._speak_burst) >= max(1, cfg.burst_threshold)

    # ---- 入站 ----------------------------------------------------------
    def enqueue(self, incoming: IncomingMessage) -> None:
        self.inbox.push(incoming)

    async def start_turn(self, request: TurnRequest) -> None:
        """开启一轮。

        * 她**正在说话**时来新消息 → 等价 interject：取消当前输出、立刻重开；
        * 她还没开口（感知 / 思考 / 结算中）→ **不算插话**：把消息退回收件箱，
          由本轮或本轮结束时的续跑逻辑合并理解。
          否则用户每追问一句都会重置等待倒计时，永远等不到回复。
        """
        if self.speaking:
            logger.debug(f"[{self.life_id}/{self.user_id}] 她正在说，新消息打断重开")
            request.interrupted = True
            # 有概率触发"断续"话术（6.3）
            if self._d.rng.random() < self._d.config.imperfection.interrupt_stammer:
                request.imperfection = request.imperfection or IMP_STAMMER
            await self._cancel_turn()
        elif self.busy:
            for inc in request.batch:
                self.inbox.push(inc, merge=False)
            logger.debug(
                f"[{self.life_id}/{self.user_id}] 她还没开口，{len(request.batch)} 条退回收件箱并入本轮"
            )
            return
        self._turn = asyncio.create_task(
            self._run(request), name=f"turn:{self.life_id}:{self.user_id}"
        )

    def resume_if_pending(self) -> None:
        """一轮收尾：若收件箱还有没处理的消息且她当前可及，自动接一轮，避免消息卡死。

        覆盖两类易漏场景：① 思考 / 结算期间到达的消息；② 唤醒补看之后又有新增。
        """
        if self.busy or not self.inbox.backlog_count:
            return
        rt = self._d.registry.get(self.life_id)
        if rt is None:
            return
        presence = rt.controller.presence
        if presence.is_unreachable or presence.is_resting or presence == PresenceState.AFK:
            return  # 交给"回来补看 / 唤醒"链路处理
        batch = self.inbox.take_batch()
        if not batch:
            return
        logger.debug(f"[{self.life_id}/{self.user_id}] 收尾续跑：还有 {len(batch)} 条待回")
        self._turn = asyncio.create_task(
            self._run(TurnRequest(batch=batch, task="reply")),
            name=f"turn:{self.life_id}:{self.user_id}",
        )

    async def force_stop(self) -> None:
        """等价于用户敲 /stop：立即停止当前输出，丢弃剩余内容。"""
        if not self.busy:
            return
        await self._set_state(TurnState.STOPPING)
        await self._cancel_turn()
        await self._set_state(TurnState.IDLE)

    # ---- 一轮流水线 ----------------------------------------------------
    async def _run(self, req: TurnRequest) -> None:
        task = asyncio.current_task()
        cancelled = False
        try:
            await self._execute(req)
        except asyncio.CancelledError:
            cancelled = True
            logger.debug(f"[{self.life_id}/{self.user_id}] 本轮被打断")
            raise
        except Exception:
            logger.exception(f"[{self.life_id}/{self.user_id}] 本轮异常")
            await self._emit(
                OutgoingEvent(
                    type="error", life_id=self.life_id, user_id=self.user_id,
                    text="（我这边好像卡了一下…）",
                )
            )
            await self._set_state(TurnState.IDLE)
        finally:
            if self._turn is task:
                self._turn = None
            # 正常走完的一轮若还攒着新消息 → 自动接上；被取消（打断 / 停止）则不续跑
            if not cancelled:
                self.resume_if_pending()

    async def _absorb_extra(self, ctx: TurnContext, req: TurnRequest) -> int:
        """把收件箱里新到的消息并入本轮（并即时落盘），返回并入条数。

        关键：**不重掷倒计时、也不重开一轮**——否则用户每追问一句就重置等待，永远等不到回复。
        """
        extra = self.inbox.take_batch()
        if not extra:
            return 0
        merged = list(ctx.batch) + list(extra)
        ctx.batch = merged
        req.batch = merged
        logger.debug(f"[{self.life_id}/{self.user_id}] 连发合并：并入 {len(extra)} 条")
        await self._d.memory.persist_user(ctx)  # 并入的消息也即时落盘
        return len(extra)

    async def _coalesce(self, ctx: TurnContext, req: TurnRequest) -> None:
        """连发合并（4.11）：等她"这段话说完"（静默 ``settle_s``）再定稿理解。

        她还没开口时，对方连续发的几条会被逐条并入本轮；若对方持续打字，
        最多再多等 ``max_wait_s``，避免"她嘴上没停、她却已经回起上一句"。
        """
        cfg = self._d.config.coalesce
        if not cfg.enabled:
            return
        waited = 0.0
        while waited < cfg.max_wait_s:
            await self._d.clock.sleep(cfg.settle_s)
            waited += cfg.settle_s
            if not await self._absorb_extra(ctx, req):
                break

    # ---- 回复意愿闸门 --------------------------------------------------
    async def _judge_reply_intent(
        self, rt: Any, rel: Any, ctx: TurnContext, now: datetime
    ) -> dict[str, Any]:
        """算意愿分 → 裁决这一轮怎么回；写可追溯日志与 trace（意愿分＋各因子＋最终档）。"""
        d = self._d
        cfg = d.config.reply_intent
        jitter = d.rng.uniform(-cfg.jitter, cfg.jitter) if cfg.jitter > 0 else 0.0
        willingness = psyche_mod.reply_willingness(
            rt.psyche, rel, cfg,
            user_text=ctx.user_text, batch_count=len(ctx.batch), now=now, jitter=jitter,
        )
        intent = await d.reply_judge.judge(ctx, willingness, cfg)
        action = str(intent.get("action", "reply"))

        # skip 冷却：刚"不回"过 → 降级为"敷衍短回"，避免连续沉默像挂机
        if action == "skip" and not self._can_skip(now):
            action = "terse"
            intent["reason"] = (intent.get("reason") or "") + "（冷却中→敷衍）"

        self.last_intent = {**intent, "willingness": willingness, "final": action}
        logger.info(
            f"[{self.life_id}/{self.user_id}] 意愿闸门 score={willingness['score']:.3f}"
            f" → {action}（{intent.get('reason', '')}）"
            f" [情绪{willingness['emotion']:+.2f}|疲{willingness['fatigue']:.2f}"
            f"|自控{willingness['self_control']:.2f}|信任{willingness['trust']:.2f}"
            f"|亲密{willingness['intimacy']:.2f}|负面{willingness['negativity']:.2f}"
            f"|刷屏{willingness['burst']:.2f}|深夜{willingness['late_night']:.2f}]"
        )
        try:
            await d.db.add_trace(self.life_id, self.user_id, "reply_intent", {
                "willingness": willingness,
                "action": action,
                "reason": intent.get("reason", ""),
                "source": intent.get("source", ""),
            })
        except Exception:
            logger.debug("reply_intent trace 写入失败")
        return {**intent, "action": action}

    def _can_skip(self, now: datetime) -> bool:
        """是否允许这一轮"不回"（冷却期内不再 skip）。"""
        if self._last_skip_at is None:
            return True
        cd = self._d.config.reply_intent.skip_cooldown_s
        return (now - self._last_skip_at).total_seconds() >= cd

    async def _handle_skip(self, ctx: TurnContext, now: datetime) -> None:
        """这一轮不回：静默（不产生 assistant 消息），只发"看到了但没回"提示。

        两条铁律：① 等待期间攒下的用户消息**一条不丢**（全部即时落盘）；
        ② 落一条"我已读未回"状态，供下一轮模型知道这是**她的选择**而非没看见。
        """
        d = self._d
        self._last_skip_at = now

        tail = self.inbox.take_batch()
        if tail:
            ctx.batch = list(ctx.batch) + tail
            await d.memory.persist_user(ctx)  # 一条不丢（消息仍落盘，只是这轮不回）

        try:
            await d.db.save_state(
                INTENT_SCOPE, self.life_id, INTENT_KIND, user_id=self.user_id,
                data={
                    "at": now.isoformat(),
                    "user_text": ctx.user_text[:120],
                    "reason": self.last_intent.get("reason", ""),
                },
            )
        except Exception:
            logger.debug("「已读未回」状态落盘失败")

        await self._emit(
            OutgoingEvent(
                type="notice", life_id=self.life_id, user_id=self.user_id,
                text="（她看到了，但没回）",
            )
        )
        await self._set_state(TurnState.IDLE)

    async def _attach_skip_note(self, ctx: TurnContext) -> None:
        """读上一轮的"已读未回"标记，附到本轮 ctx.meta（随后清掉，避免反复回放）。"""
        d = self._d
        try:
            marker = await d.db.load_state(
                INTENT_SCOPE, self.life_id, INTENT_KIND, user_id=self.user_id
            )
        except Exception:
            return
        if not marker:
            return
        at = _parse_ts(marker.get("at"))
        if at is not None and (ctx.now - at).total_seconds() > SKIP_NOTE_TTL_S:
            marker = None  # 太旧了，不再提醒
        if marker:
            ctx.meta["last_skip_note"] = True
        try:
            await d.db.save_state(
                INTENT_SCOPE, self.life_id, INTENT_KIND, user_id=self.user_id, data={}
            )
        except Exception:
            logger.debug("清理「已读未回」标记失败")

    async def _execute(self, req: TurnRequest) -> None:
        d = self._d
        clock = d.clock
        rng = d.rng
        rt = d.registry.require(self.life_id)
        now = clock.now()

        # 懒推进一次心理状态（情绪回落 / 自控恢复）
        psyche_mod.tick(rt.psyche, rt.life, now, rt.controller.presence, d.config.relation)
        rel = await d.registry.relationship(self.life_id, self.user_id)

        imperfection = req.imperfection
        if imperfection is None:
            imperfection = psyche_mod.roll_imperfection(
                rt.psyche, rt.life, d.config.imperfection, rng,
                is_interrupt=req.interrupted,
            )

        # 上头判定（12.2）：情绪强度 × (1-自控) 越阈值 → 本轮绕过人设一致性
        effective_task = req.task
        override_flavor: str | None = None
        if override_mod.should_enter(rt.psyche, rt.life, d.config.override, now):
            effective_task = "override"
            override_flavor = override_mod.flavor_of(rt.life)
            await self._emit(
                OutgoingEvent(
                    type="notice", life_id=self.life_id, user_id=self.user_id,
                    text="（情绪上头：她有点控制不住了…）",
                )
            )

        ctx = TurnContext(
            life=rt.life,
            psyche=rt.psyche,
            relationship=rel,
            user_id=self.user_id,
            batch=req.batch,
            now=now,
            presence=rt.controller.presence,
            task=effective_task,  # type: ignore[arg-type]
            wake_reason=req.wake_reason,
            imperfection=imperfection,
            meta={"override_flavor": override_flavor, **req.meta} if override_flavor else dict(req.meta),
        )
        # 睡前/静默状态：不主动回复
        if rt.controller.presence in (PresenceState.OFFLINE, PresenceState.DND):
            logger.debug("不可达状态，静默丢弃本轮")
            return

        # 入站消息**立即落盘**：即便本轮随后被她说话中的新消息打断，
        # 你说过的话也不会丢（旧实现把落盘挂在轮末，一打断就整轮丢失）。
        await d.memory.persist_user(ctx)

        # 上一轮她"看到了但选择没回" → 让模型知道那是有意的沉默，而非没看见
        if req.batch:
            await self._attach_skip_note(ctx)

        speed = psyche_mod.expression_modifiers(rt.psyche)["speed"]

        # ---- ① 感知：等待 + 意识到要回 --------------------------------
        await self._set_state(TurnState.PERCEIVING)
        d_avail = d.timing.availability(
            ctx.presence, rng,
            busy_reply=str((rt.life.schedule or {}).get("busy_reply") or "normal"),
        )
        d_perceive = d.timing.perceive(ctx, rng, speed)
        await clock.sleep(d_avail + d_perceive)

        # 连发合并（4.11）：她还没开口，先把对方"这段话还没说完"的内容等齐再理解。
        # 否则后面几条会被拆成独立轮次、一条条滞后回复（表现为"只看到第一句"）。
        await self._coalesce(ctx, req)

        # 候选信念（供说服力学）——必须在消息合并之后算，否则漏掉后到的内容
        ctx.candidate_beliefs = belief_mod.select_candidates(rt.life, ctx.user_text)

        if req.batch:
            await self._emit(
                OutgoingEvent(
                    type="delivered", life_id=self.life_id, user_id=self.user_id,
                    meta={"count": len(req.batch)},
                )
            )

        # ---- 回复意愿闸门：她**自主决定这一轮回不回**（不是每条必回）----
        #  落点在「感知 + 连发合并之后、思考之前」；只对"回复用户消息"的轮次生效，
        #  唤醒 / 主动发起 / 情绪上头不走此闸门（那些本就该说）。
        #  注意：**绝不直接问大模型"要不要回"**（否则恒回）——先算确定性意愿分，
        #  只在灰区才交给轻量 mind 模型在有限四档里裁决；异常一律兜底为"正常回"。
        if d.config.reply_intent.enabled and req.batch and effective_task == "reply":
            intent = await self._judge_reply_intent(rt, rel, ctx, now)
            action = intent["action"]

            if action == "skip":
                await self._handle_skip(ctx, now)
                return
            if action == "delay" and float(intent.get("delay_s") or 0.0) > 0:
                await clock.sleep(float(intent["delay_s"]))
            elif action == "terse":
                # 敷衍短回：复用"沉默"风味 + 限长指令（persona 里 force_terse）
                ctx.imperfection = psyche_mod.IMP_SILENCE
                ctx.meta["force_terse"] = True

        # ---- ② 思考：装配记忆 + 生成 ---------------------------------
        await self._set_state(TurnState.THINKING)
        history = await d.memory.hydrate(ctx)
        segments = await d.responder.respond(ctx, history, rng)
        # 生成期间对方又补了几句 → 并入后重生成（她尚未开口，不算插话；最多 regen_max 次）
        cfg = d.config.coalesce
        regen = 0
        while cfg.enabled and regen < cfg.regen_max:
            if not await self._absorb_extra(ctx, req):
                break
            ctx.candidate_beliefs = belief_mod.select_candidates(rt.life, ctx.user_text)
            history = await d.memory.hydrate(ctx)
            segments = await d.responder.respond(ctx, history, rng)
            regen += 1
        d_think = d.timing.think(ctx, sum(len(s.text) for s in segments), rng, speed)
        await clock.sleep(d_think)
        if not segments:
            await self._set_state(TurnState.IDLE)
            return

        # ---- ③ 说话：逐条节奏重放 ------------------------------------
        #  timing 开口前就能算全（d_type = 各段打字时长之和），先算好，
        #  好让"说完 / 被打断 / 被停止"三种收尾都能带着完整节奏信息落盘。
        timing = {
            "d_avail": round(d_avail, 2), "d_perceive": round(d_perceive, 2),
            "d_think": round(d_think, 2),
            "d_type": round(sum(s.typing_seconds for s in segments), 2),
        }
        await self._set_state(TurnState.SPEAKING)
        spoken: list[Segment] = []          # 已经"说出口"的段落（被打断也照落盘）
        assist_id = new_id("assist")
        try:
            await self._speak(segments, spoken)
        finally:
            # 无论说完还是被打断 / 被停止，都把已说出的部分落盘（幂等，只落一行）
            try:
                await d.memory.persist_assistant(
                    ctx, spoken, persist_id=assist_id, timing=timing
                )
            except Exception:
                logger.exception("助手回复落盘失败")
        await self._set_state(TurnState.IDLE)

        # ---- ④ 结算（先回复、后结算）--------------------------------
        await self._settle(ctx, segments, timing)

    # ---- 说话（表演）---------------------------------------------------
    async def _speak(self, segments: list[Segment], spoken: list[Segment]) -> None:
        d = self._d
        for i, seg in enumerate(segments):
            await self._emit(
                OutgoingEvent(type="typing", life_id=self.life_id, user_id=self.user_id, typing=True)
            )
            await self._emit(
                OutgoingEvent(
                    type="chunk", life_id=self.life_id, user_id=self.user_id,
                    role=Role.ASSISTANT, message_id=new_id("assist"),
                    text=seg.text,
                    meta={
                        "out_of_character": seg.out_of_character,
                        # 这条气泡预计"打"多久（毫秒）：前端打字机据此逐字渲染，
                        # 与后端 holding 的时长对齐，避免前端要么"唰"完、要么中途被截断
                        "type_ms": int(seg.typing_seconds * 1000),
                    },
                )
            )
            spoken.append(seg)  # 已吐给前端 = 已"说出口"；此刻起被打断也算数
            await d.clock.sleep(seg.typing_seconds)
            await self._emit(
                OutgoingEvent(type="typing", life_id=self.life_id, user_id=self.user_id, typing=False)
            )
            gap = float(seg.meta.get("gap_after", 0.0))
            if gap and i < len(segments) - 1:
                await d.clock.sleep(gap)

    # ---- 结算：记忆 + 心理 ---------------------------------------------
    async def _settle(self, ctx: TurnContext, segments: list[Segment], timing: dict) -> None:
        d = self._d
        life_id, user_id = self.life_id, self.user_id
        rt = d.registry.require(life_id)
        rel = await d.registry.relationship(life_id, user_id)
        now = d.clock.now()

        # 1) 轮末后处理（滚动摘要 / 事实抽取 / 跨天日记）。
        #    消息本身已在"发生的那一刻"即时落盘（见 persist_user / persist_assistant），
        #    这里不再写消息，避免"轮末才落盘 → 一打断就丢"的老问题。
        try:
            await d.memory.finalize_turn(ctx, segments, timing=timing)
        except Exception:
            logger.exception("轮末后处理失败")

        # 2) 心理核算（小模型，异步旁路；失败自动启发式兜底）
        round_text = ctx.user_text + "\n" + " ".join(s.text for s in segments)
        account = await d.accountant.account(ctx, round_text)

        # 3) 情绪：用户情绪 + 自己情绪增量（带惯性）
        psyche_mod.nudge_emotion(
            rt.psyche, rt.life, d.config.relation,
            d_valence=float(account.get("d_valence", 0.0)),
            d_arousal=float(account.get("d_arousal", 0.0)),
        )
        positive = max(0.0, float(account.get("user_valence", 0.0)))
        shock = float(account.get("shock", 0.0))
        if shock > 0.5:
            # 情绪冲击消耗自控（12.1）
            rt.psyche.self_control = max(0.0, rt.psyche.self_control - 0.08 * shock)

        # 4) 关系演化（深夜交心加成 / 正面情绪加成）
        deep_talk = now.hour >= 23 or now.hour < 2
        psyche_mod.grow_relationship(
            rel, d.config.relation, rounds=max(1, len(ctx.batch)),
            positive=positive, deep_talk=deep_talk,
        )
        if positive > 0.4:
            psyche_mod.record_favor(rel, ctx.user_text[:30], positive, now)
        elif float(account.get("user_valence", 0.0)) < -0.5:
            psyche_mod.record_grudge(rel, ctx.user_text[:30], -0.5, now)
        if ctx.batch:
            rel.last_active_at = now

        # 5) 信念漂移 / 逆反（第 11 节）
        try:
            persuasion = account.get("persuasion") or {}
            hard_push = belief_mod.is_hard_push(ctx.user_text)
            for b in ctx.candidate_beliefs:
                strength = float(persuasion.get(b.id, belief_mod.argument_strength(ctx.user_text) * 0.5))
                if strength <= 0:
                    continue
                belief_mod.apply(
                    b, strength=strength,
                    trust=rel.trust, intimacy=rel.intimacy,
                    empathy=abs(float(account.get("user_valence", 0.0))),
                    hard_push=hard_push, rel=rel,
                    cfg=d.config.belief, now=now,
                )
        except Exception:
            logger.exception("信念漂移结算失败")

        # 6) 主动教导（14.1）：用户显式"记住……"式的话，直接落成上锁认知。
        #    命中的话题透传给自学步骤排除，避免同一条被低确信度地重复写一遍。
        taught: list[dict] = []
        if d.config.teaching.enabled:
            try:
                taught = await d.teaching.apply(ctx)
            except Exception:
                logger.exception("主动教导失败")
        taught_topics = {t["topic"] for t in taught}

        # 7) 认知习得（14）：从无到有地长出世界认知；反复接触会毕业成三观；
        #    纯洁体据此抬升 awareness（成熟度），提示词随之内在"长大"
        learned: list[dict] = []
        try:
            learned = await d.learner.learn(ctx, round_text, exclude_topics=taught_topics)
        except Exception:
            logger.exception("认知习得失败")

        # 8) 落盘状态 + 决策 trace（9.2 J）
        #    （上头/后悔的状态迁移由 ProactiveScheduler 的慢循环统一收口，见 M14）
        await d.registry.save_psyche(life_id)
        await d.registry.save_relationship(life_id, user_id)
        if learned or rt.life.origin == "blank":
            await d.registry.save_life_state(life_id)  # 认知/信念/成熟度一并落盘
        try:
            await d.db.add_trace(life_id, user_id, "turn", {
                "task": ctx.task,
                "imperfection": ctx.imperfection,
                "timing": timing,
                "presence": ctx.presence.value,
                "emotion": {
                    "valence": round(rt.psyche.emotion_valence, 3),
                    "arousal": round(rt.psyche.emotion_arousal, 3),
                    "self_control": round(rt.psyche.self_control, 3),
                },
                "account": account,
                "taught": taught,
                "learned": learned,
                "awareness": round(rt.life.awareness, 3),
                "beliefs": [
                    {"topic": b.topic, "confidence": round(b.confidence, 3)}
                    for b in rt.life.beliefs
                ],
                "segments": [s.text for s in segments],
            })
        except Exception:
            logger.debug("trace 写入失败")

    # ---- 状态与事件 ----------------------------------------------------
    async def _set_state(self, state: TurnState) -> None:
        if state == self._state:
            return
        self._state = state
        if state == TurnState.SPEAKING:
            self._speak_burst.clear()  # 新一轮"说话期"重新计数
        logger.debug(f"[{self.life_id}/{self.user_id}] 会话状态 → {state.value}")
        await self._emit(
            OutgoingEvent(
                type="state", life_id=self.life_id, user_id=self.user_id, state=state
            )
        )

    async def _emit(self, event: OutgoingEvent) -> None:
        await self._d.bus.emit(TOPIC_OUTBOUND, event)

    async def _cancel_turn(self) -> None:
        task = self._turn
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("取消 turn 时出现异常")

    async def shutdown(self) -> None:
        await self._cancel_turn()


class SessionManager:
    """编排器：按 (life_id, user_id) 管理会话，并在入站处做**慢层闸门**判定。"""

    def __init__(self, deps: SessionDeps) -> None:
        self._d = deps
        self._bus = deps.bus
        self._sessions: dict[tuple[str, str], Session] = {}
        self._wake = WakeArbiter(deps.config.wake, deps.rng)
        deps.registry.on_presence = self._on_presence_change

        self._bus.on(TOPIC_INCOMING, self._on_incoming)
        self._bus.on(TOPIC_CONTROL, self._on_control)

    # ---- 访问 ----------------------------------------------------------
    def get(self, life_id: str, user_id: str) -> Session:
        key = (life_id, user_id)
        session = self._sessions.get(key)
        if session is None:
            session = Session(life_id, user_id, self._d)
            self._sessions[key] = session
        return session

    def sessions_of(self, life_id: str) -> list[Session]:
        return [s for (lid, _u), s in self._sessions.items() if lid == life_id]

    async def history(self, life_id: str, user_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """该会话的历史消息（前端打开/刷新/跳转回来时回显）——像微信一样默认全部可见。

        只读、不涉及任何状态变更——记忆照常留存，刷新页面也看得见"她记得什么"。

        「清空记录」是**软清空**：只在 state 表记一个时间水印，回放时跳过水印之前的消息；
        ``message`` 表一行不删，所以她的记忆 / 召回 / 关系演化不受任何影响（永远记得）。
        """
        marker = await self._d.db.load_state(HIST_SCOPE, life_id, HIST_KIND, user_id=user_id) or {}
        cleared_at = _parse_ts(marker.get("at"))
        msgs = await self._d.db.load_recent(life_id, user_id, limit=limit)
        out: list[dict[str, Any]] = []
        for m in msgs:
            if m.role not in (Role.USER, Role.ASSISTANT, Role.PROACTIVE) or not m.content.strip():
                continue
            if cleared_at is not None and m.created_at <= cleared_at:
                continue  # 用户已清空的旧回放（数据仍在库里，她依然记得）
            out.append({
                "role": m.role.value,
                "content": m.content,
                "at": m.created_at.isoformat(timespec="seconds"),
                # 她的多段连发结构（meta.segments）：前端据此还原成多条气泡；
                # 用户消息 / 旧数据没有该字段 → null，按整段渲染（兼容无损）。
                "segments": (m.meta or {}).get("segments") or None,
            })
        return out

    # ---- 入站闸门（4.6）------------------------------------------------
    async def _on_incoming(self, incoming: IncomingMessage) -> None:
        life_id = incoming.life_id
        try:
            rt = await self._d.registry.ensure(life_id)
        except Exception:
            logger.exception(f"生命体 {life_id} 加载失败，忽略消息")
            return
        session = self.get(life_id, incoming.user_id)
        presence = rt.controller.presence

        # 聊天正嗨 → 推迟入睡（4.3 状态可被事件打断）
        if presence in (PresenceState.ACTIVE, PresenceState.BUSY):
            await rt.controller.note_activity()

        # ① 完全不可达：积压，不响应
        if presence.is_unreachable:
            session.enqueue(incoming)
            await self._emit_notice(session, f"（她现在{rt.controller.availability_note()}，消息先攒着）")
            await self._broadcast_presence(life_id)
            return

        # ② 休息中：累积唤醒值
        if presence.is_resting:
            session.enqueue(incoming)
            result = self._wake.accumulate(
                life_id, incoming.content, presence, self._d.clock.now(),
                life_name=rt.life.name,
            )
            await self._emit(
                OutgoingEvent(
                    type="wake", life_id=life_id, user_id=incoming.user_id,
                    meta={"progress": round(result.progress, 3), "delta": round(result.delta, 3),
                          "reason": result.reason},
                )
            )
            if not result.woke:
                return
            # 唤醒 → 起床反应 + 补看积压
            wake_reason = self._wake.wake_greeting_reason(presence, result)
            await rt.controller.wake_up(reason=result.reason)
            await self._broadcast_presence(life_id)
            batch = session.inbox.drain_backlog()
            await session.start_turn(TurnRequest(batch=batch, task="wake", wake_reason=wake_reason))
            return

        # ③ AFK：等回来统一补看
        if presence == PresenceState.AFK:
            session.enqueue(incoming)
            await self._emit_notice(session, "（她暂时离开了，回来会一起看）")
            return

        # ④ 可及：入队后按她"开口没开口"决定怎么处理
        session.enqueue(incoming)
        if session.busy:
            # 她还没开口（感知 / 思考 / 结算中）：本条并入本轮一起理解，
            # 不重开一轮、也不重置等待倒计时（否则 BUSY 长等待下永远等不到回复）
            if not session.speaking:
                return
            # 她正在说话：默认也不打断，只有"连发达阈值 / 命中喊停词"才打断重开，
            # 否则入队、等她说完整轮由收尾续跑逻辑合并处理。
            if not session.should_interrupt(incoming, self._d.clock.now()):
                return
            logger.debug(f"[{life_id}] 她说话中，消息达打断阈值 → 打断重开")
            batch = session.inbox.take_batch()
            if not batch:
                return
            await session.start_turn(TurnRequest(batch=batch, task="reply"))
            return
        batch = session.inbox.take_batch()
        if not batch:
            return
        await session.start_turn(TurnRequest(batch=batch, task="reply"))

    # ---- 生命状态变化：回来补看积压 ------------------------------------
    async def _on_presence_change(self, life_id: str, state: PresenceState, meta: dict) -> None:
        await self._broadcast_presence(life_id)
        prev = meta.get("prev", "")
        if state == PresenceState.ACTIVE and prev in ("afk", "offline", "dnd"):
            note = "你刚才在忙别的（吃饭 / 洗澡 / 出门），刚回来。"
            for session in self.sessions_of(life_id):
                batch = session.inbox.drain_backlog()
                if not batch:
                    continue
                await session.start_turn(
                    TurnRequest(batch=batch, task="reply", meta={"absence_note": note})
                )

    # ---- 控制指令（调试面板）------------------------------------------
    async def _on_control(self, payload: dict) -> None:
        action = (payload or {}).get("action")
        registry = self._d.registry
        if action == "force_stop":
            await self.get(payload["life_id"], payload["user_id"]).force_stop()
        elif action == "force_state":
            rt = registry.get(payload["life_id"])
            if rt is not None:
                await rt.controller.force_state(PresenceState(payload["state"]), reason="调试强切")
        elif action == "set_time_scale":
            self._d.clock.set_time_scale(float(payload["scale"]))
            logger.info(f"时间倍速 → {payload['scale']}x")
            await self._d.bus.emit(TOPIC_OUTBOUND, OutgoingEvent(
                type="notice", life_id=payload.get("life_id", ""), user_id=payload.get("user_id", ""),
                text=f"时间倍速已切换为 {payload['scale']}x",
            ))
        elif action == "wake_now":
            rt = registry.get(payload["life_id"])
            if rt is not None:
                await rt.controller.wake_up(reason="调试唤醒")
        elif action == "sleep_back":
            rt = registry.get(payload["life_id"])
            if rt is not None:
                await rt.controller.maybe_sleep_back()
        else:
            logger.warning(f"未知控制指令：{action!r}")

    # ---- 主动发起（M11 用）--------------------------------------------
    async def proactive_turn(self, life_id: str, user_id: str, kind: str, seed_text: str = "") -> None:
        session = self.get(life_id, user_id)
        if session.busy:
            return
        await session.start_turn(
            TurnRequest(
                batch=[],
                task="proactive",
                meta={"proactive_kind": kind, "seed": seed_text},
            )
        )

    # ---- 情绪闭环（M14 用）--------------------------------------------
    async def override_closure_turn(self, life_id: str, user_id: str, text: str) -> None:
        """上头结束后主动找补：以"主动发起"的形式说一句后悔/找补的话。"""
        session = self.get(life_id, user_id)
        if session.busy:
            return
        await session.start_turn(
            TurnRequest(
                batch=[],
                task="proactive",
                meta={"proactive_kind": "care", "closure": text},
            )
        )

    # ---- 调试数据 ------------------------------------------------------
    def debug_snapshot(self, life_id: str, user_id: str) -> dict:
        rt = self._d.registry.get(life_id)
        if rt is None:
            return {}
        session = self.get(life_id, user_id)
        data = self._d.registry.debug_snapshot(life_id, self._wake.progress(life_id))
        data["turn_state"] = session.state.value
        data["inbox"] = session.inbox.peek()
        data["time_scale"] = self._d.clock.time_scale
        data["reply_intent"] = session.last_intent
        return data

    # ---- 事件辅助 ------------------------------------------------------
    async def _emit(self, event: OutgoingEvent) -> None:
        await self._bus.emit(TOPIC_OUTBOUND, event)

    async def _emit_notice(self, session: Session, text: str) -> None:
        await self._emit(
            OutgoingEvent(type="notice", life_id=session.life_id, user_id=session.user_id, text=text)
        )

    async def _broadcast_presence(self, life_id: str) -> None:
        rt = self._d.registry.get(life_id)
        if rt is None:
            return
        for session in self.sessions_of(life_id):
            await self._emit(
                OutgoingEvent(
                    type="presence", life_id=life_id, user_id=session.user_id,
                    presence=rt.controller.presence,
                    meta={
                        "just_woke": rt.controller.just_woke,
                        "note": rt.controller.availability_note(),
                    },
                )
            )

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            await session.shutdown()