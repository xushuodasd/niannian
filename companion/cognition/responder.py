"""回复生成器：把「人格装配 → LLM 流式 → 去机器味 → 分条」串成一条流水线。

对上层（会话状态机）只暴露一个纯函数式接口 ``respond(ctx, history) -> list[Segment]``，
**无副作用**：不写记忆、不改状态（那些由 turn 结束后统一结算，先回复后结算）。
"""
from __future__ import annotations

import random

from loguru import logger

from ..engine.config import StickerConfig, TimingConfig
from ..engine.expression import ExpressionController
from ..engine.types import Role, Segment, TurnContext
from ..mind import psyche as psyche_mod
from . import persona as persona_mod
from .llm import LLMGateway
from .stickers import StickerLibrary, parse_poke_line, parse_sticker_line


class CompanionResponder:
    def __init__(
        self,
        llm: LLMGateway,
        expression: ExpressionController,
        cfg: TimingConfig,
        stickers: StickerLibrary,
        sticker_cfg: StickerConfig | None = None,
    ) -> None:
        self._llm = llm
        self._expression = expression
        self._cfg = cfg
        self._stickers = stickers
        self._sticker_cfg = sticker_cfg or StickerConfig()

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
        ooc = ctx.task == "override"
        cfg_s = self._sticker_cfg
        life_id = ctx.life.life_id

        # 语义分块：文本块 → 表达控制器；标记独立行 → 特殊 Segment（表情 / 戳一戳）。
        # 审美约束：上头（override）时不发表情、不戳；sticker 还需本轮允许（sticker_block 非空）。
        sticker_ok = (not ooc) and bool(ctx.sticker_block)
        poke_ok = (not ooc) and bool(ctx.meta.get("poke_allowed"))
        segments: list[Segment] = []
        sticker_used = 0   # 每轮内计数（不跨轮）
        for kind, payload in self._split_blocks(raw):
            if kind == "text":
                segments.extend(
                    self._expression.compose(
                        payload or "", ctx.life.style, mods, rng, out_of_character=ooc
                    )
                )
            elif kind == "sticker":
                if not sticker_ok:
                    logger.debug(f"表情标记丢弃（本轮不允许发表情）：{payload!r}")
                    continue
                if sticker_used >= cfg_s.max_per_round:
                    logger.debug(f"表情标记丢弃（本轮已达上限 {cfg_s.max_per_round}）：{payload!r}")
                    continue
                st = self._stickers.find_by_label_or_tag(life_id, payload or "")
                if st is None:
                    logger.debug(f"表情标记未命中素材，丢弃：{payload!r}")
                    continue
                sticker_used += 1
                segments.append(
                    Segment(
                        text=f"（表情：{payload}）",
                        typing_seconds=rng.uniform(
                            cfg_s.think_seconds_min, cfg_s.think_seconds_max
                        ),
                        meta={
                            "kind": "sticker",
                            "sticker_id": st.id,
                            "sticker_url": st.url,
                            "label": st.label or (payload or ""),
                        },
                    )
                )
            else:  # poke
                if not poke_ok:
                    logger.debug("戳一戳标记丢弃（本轮不允许戳）")
                    continue
                segments.append(
                    Segment(text="（戳了你一下）", typing_seconds=0.6, meta={"kind": "poke"})
                )

        # 兜底：语义分块后一段都没有、但原文本来有内容 → 剥掉标记行整体重走一次，保证不空回
        if not segments and (raw or "").strip():
            fallback = "\n".join(
                ln
                for ln in (raw or "").split("\n")
                if parse_sticker_line(ln) is None and not parse_poke_line(ln)
            )
            if fallback.strip():
                segments.extend(
                    self._expression.compose(
                        fallback, ctx.life.style, mods, rng, out_of_character=ooc
                    )
                )
        return segments

    def _split_blocks(self, raw: str) -> list[tuple[str, str | None]]:
        """按行扫描 raw，识别**独立行**标记，切成语义块序列。

        产出 ``("text", 文本)`` / ``("sticker", 标签)`` / ``("poke", None)``；
        连续文本行合并为一个 text 块（保留换行）。非独立行（如句中 ``[表情:x]``）不命中。
        """
        blocks: list[tuple[str, str | None]] = []
        buf: list[str] = []

        def flush() -> None:
            if not buf:
                return
            text = "\n".join(buf)
            buf.clear()
            if text.strip():
                blocks.append(("text", text))

        for line in (raw or "").split("\n"):
            label = parse_sticker_line(line)
            if label is not None:
                flush()
                blocks.append(("sticker", label))
                continue
            if parse_poke_line(line):
                flush()
                blocks.append(("poke", None))
                continue
            buf.append(line)
        flush()
        return blocks

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