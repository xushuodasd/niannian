"""回复生成器：把「人格装配 → LLM 流式 → 去机器味 → 分条」串成一条流水线。

对上层（会话状态机）只暴露一个纯函数式接口 ``respond(ctx, history) -> list[Segment]``，
**无副作用**：不写记忆、不改状态（那些由 turn 结束后统一结算，先回复后结算）。
"""
from __future__ import annotations

import random

from loguru import logger

from ..engine.config import TimingConfig
from ..engine.expression import ExpressionController
from ..engine.types import Role, Segment, TurnContext
from ..mind import psyche as psyche_mod
from . import persona as persona_mod
from .llm import LLMGateway


class CompanionResponder:
    def __init__(
        self,
        llm: LLMGateway,
        expression: ExpressionController,
        cfg: TimingConfig,
    ) -> None:
        self._llm = llm
        self._expression = expression
        self._cfg = cfg

    async def respond(
        self,
        ctx: TurnContext,
        history: list[dict[str, str]],
        rng: random.Random,
    ) -> list[Segment]:
        messages = persona_mod.build_messages(ctx, history)
        raw = await self._llm.complete(
            messages,
            task=ctx.task,
            kind="chat",
            mock_hint=self._mock_hint(ctx),
        )

        # 身份一致性自检（4.12）：越界状态跳过（12.2 唯一允许崩人设的场景）
        if ctx.task != "override" and raw:
            conflict = persona_mod.check_consistency(raw, ctx.life)
            if conflict:
                logger.warning(f"人设自检未通过（{conflict}），重写一次")
                raw = await self._llm.complete(
                    messages
                    + [{
                        "role": "system",
                        "content": f"（注意：{conflict}，请重新回复，不要出现矛盾信息。）",
                    }],
                    task=ctx.task,
                    kind="chat",
                    mock_hint=self._mock_hint(ctx),
                )

        mods = psyche_mod.expression_modifiers(ctx.psyche)
        return self._expression.compose(
            raw,
            ctx.life.style,
            mods,
            rng,
            out_of_character=(ctx.task == "override"),
        )

    # ---- mock 提示（仅本地模拟后端使用）-------------------------------
    def _mock_hint(self, ctx: TurnContext) -> dict:
        return {
            "style": ctx.life.style.model_dump(),
            "life_name": ctx.life.name,
            "user_text": ctx.user_text,
            "facts_text": ctx.facts_text,
            "relation_stage": ctx.relationship.stage,
            "mood_label": ctx.psyche.mood_label,
            "proactive_kind": ctx.meta.get("proactive_kind", "topic"),
            "closure": ctx.meta.get("closure"),
            "override_flavor": ctx.meta.get("override_flavor", "angry"),
            "role": Role.ASSISTANT.value,
        }