"""表达控制器：把"想说的话"变成"一条条气泡 + 说话时长"（4.8 / 4.9 / 9.2 D）。

职责链：

    原文 → 去机器味清洗 → 情绪调制（表情/句长/省略号）→ 分句分条 → 每条打字时延 + 条间间隔

它不碰 LLM、不碰状态机，纯粹是"说人话"的最后一公里。
"""
from __future__ import annotations

import random

from loguru import logger

from .config import TimingConfig
from .timing import TimingModel
from .types import Segment, StyleProfile
from ..cognition import style as style_mod


class ExpressionController:
    def __init__(self, timing: TimingModel, cfg: TimingConfig) -> None:
        self._timing = timing
        self._cfg = cfg

    def compose(
        self,
        text: str,
        style: StyleProfile,
        mods: dict[str, float],
        rng: random.Random,
        *,
        out_of_character: bool = False,
    ) -> list[Segment]:
        cleaned = style_mod.clean(text or "", style)
        if not cleaned:
            return self.silence(rng)

        adjusted = self._adjust_style(style, mods)
        bubbles = style_mod.split_bubbles(cleaned, adjusted, rng)
        if not bubbles:
            return self.silence(rng)

        segments: list[Segment] = []
        for i, b in enumerate(bubbles):
            b = self._emotion_touch(b, mods, rng, is_last=(i == len(bubbles) - 1))
            seg = Segment(
                text=b,
                typing_seconds=self._timing.typing_seconds(b, self._cfg.typing_cps, mods.get("speed", 1.0)),
                out_of_character=out_of_character,
            )
            if i < len(bubbles) - 1:
                seg.meta["gap_after"] = self._timing.seg_gap(rng)
            segments.append(seg)
        logger.debug(
            f"表达：{len(cleaned)}字 → {len(segments)} 条气泡，"
            f"预计 {sum(s.typing_seconds for s in segments):.1f}s"
        )
        return segments

    def silence(self, rng: random.Random, text: str = "……") -> list[Segment]:
        """沉默（6.3）：只回"……"或一个表情。"""
        return [Segment(text=text, typing_seconds=1.2)]

    # ---- 内部 ----------------------------------------------------------
    def _adjust_style(self, style: StyleProfile, mods: dict[str, float]) -> StyleProfile:
        """按情绪调制风格档：开心→更多表情；低落→更长的省略。"""
        emoji_rate = min(0.9, style.emoji_rate * mods.get("emoji_boost", 1.0))
        sentence_len = max(6, int(style.sentence_len * mods.get("sentence_scale", 1.0)))
        split_prob = min(0.95, style.split_prob + 0.1 * max(0.0, mods.get("speed", 1.0) - 1.0))
        return style.model_copy(update={
            "emoji_rate": emoji_rate,
            "sentence_len": sentence_len,
            "split_prob": split_prob,
        })

    def _emotion_touch(self, text: str, mods: dict[str, float], rng: random.Random, *, is_last: bool) -> str:
        """情绪 → 标点习惯（低落时"……"、少表情，4.8）。"""
        out = text
        ellipsis = mods.get("ellipsis_boost", 0.3)
        if is_last and ellipsis > 0.8 and rng.random() < 0.45 and not out.endswith(("…", "~", "～", "。", "！", "？")):
            out += "……"
        return out