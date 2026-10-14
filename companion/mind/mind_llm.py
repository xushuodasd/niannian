"""心理核算（小模型，JSON）—— 9.2 A / E。

每轮把「用户情绪 + 情绪增量 + 信念说服方向强度 + 情绪冲击 + 值得记住的事实」
交给便宜小模型算成 JSON。**纯本地也能跑**：无模型时用启发式规则兜底，
保证"先回复、后结算"的主链路永不阻塞。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from ..cognition.llm import LLMGateway
from ..cognition.persona import build_mind_messages, build_reply_intent_messages
from ..engine.config import ReplyIntentConfig
from ..engine.types import TurnContext
from . import belief as belief_mod

# 简易中文情感词典（本地兜底用；真实场景交给小模型）
_POS = ("开心", "高兴", "喜欢", "爱你", "哈哈", "太好了", "谢谢", "抱抱", "想你", "真好", "幸福", "爽")
_NEG = ("难过", "难受", "累", "烦", "生气", "讨厌", "委屈", "崩溃", "哭", "孤独", "失望", "滚", "烦死")


def heuristic(user_text: str) -> dict[str, Any]:
    """本地启发式核算：给 mock 后端当 fallback_json，也是无模型时的真实兜底。"""
    pos = sum(1 for w in _POS if w in user_text)
    neg = sum(1 for w in _NEG if w in user_text)
    valence = 0.0
    if pos or neg:
        valence = max(-1.0, min(1.0, (pos - neg) / max(1, pos + neg)))
    arousal = min(1.0, 0.25 + 0.15 * (pos + neg))
    shock = min(1.0, 0.15 * (pos + neg))
    return {
        "user_valence": round(valence, 3),
        "user_arousal": round(arousal, 3),
        "d_valence": round(valence * 0.35, 3),
        "d_arousal": round(0.12 * (pos + neg), 3),
        "persuasion": {},
        "shock": round(shock, 3),
        "facts": [],
    }


class MindAccountant:
    """心理核算服务（异步旁路，先回复后结算）。"""

    def __init__(self, llm: LLMGateway) -> None:
        self._llm = llm

    async def account(self, ctx: TurnContext, round_text: str) -> dict[str, Any]:
        fallback = heuristic(round_text)
        # 候选信念的本地说服强度预填，减少对模型的依赖
        for b in ctx.candidate_beliefs:
            fallback["persuasion"][b.id] = belief_mod.argument_strength(round_text) * (
                -0.5 if belief_mod.is_hard_push(round_text) else 0.6
            )
        result = await self._llm.json(
            build_mind_messages(ctx, round_text),
            mock_hint={"fallback_json": fallback},
        )
        if not isinstance(result, dict) or not result:
            return fallback
        # 补齐缺失字段
        for key, value in fallback.items():
            result.setdefault(key, value)
        if not result.get("persuasion"):
            result["persuasion"] = fallback["persuasion"]
        return result


# ======================================================================
# 回复意愿裁决器（"她可以不回"）：确定性定档 + 灰区才调轻量模型
# ======================================================================
_ACTIONS = ("reply", "terse", "delay", "skip")


def _heuristic_intent(willingness: dict[str, Any], cfg: ReplyIntentConfig) -> dict[str, Any]:
    """灰区的本地启发式兜底（也是 mock 后端的返回、以及无模型时的真实兜底）。

    灰区（direct_skip < score < direct_reply）内再分四小档，越接近下界越"不想回"。
    """
    score = float(willingness.get("score", 1.0))
    if score >= cfg.direct_reply:
        return {"action": "reply", "delay_s": 0.0, "tone": "", "reason": "意愿充足"}
    if score <= cfg.direct_skip:
        return {"action": "skip", "delay_s": 0.0, "tone": "", "reason": "意愿过低"}
    if score >= 0.50:
        return {"action": "reply", "delay_s": 0.0, "tone": "", "reason": "意愿尚可"}
    if score >= 0.42:
        return {"action": "terse", "delay_s": 0.0, "tone": "冷淡", "reason": "有点懒得聊"}
    if score >= 0.34:
        span = max(0.0, 0.42 - score)
        delay_s = round(min(cfg.max_delay_s, span / 0.08 * cfg.max_delay_s), 1)
        return {"action": "delay", "delay_s": delay_s, "tone": "敷衍", "reason": "想先晾一会儿"}
    return {"action": "skip", "delay_s": 0.0, "tone": "冷淡", "reason": "没什么心思回"}


class ReplyIntentJudge:
    """她**这一轮回不回**的裁决器（异常一律兜底为"正常回"，绝不阻塞主链路）。"""

    def __init__(self, llm: LLMGateway) -> None:
        self._llm = llm

    async def judge(
        self, ctx: TurnContext, willingness: dict[str, Any], cfg: ReplyIntentConfig
    ) -> dict[str, Any]:
        score = float(willingness.get("score", 1.0))
        # 三档路由：高→直接回（不调模型）；低→直接跳过；中间灰区才惊动轻量模型
        if score >= cfg.direct_reply:
            return {"action": "reply", "delay_s": 0.0, "tone": "", "reason": "意愿充足", "source": "threshold"}
        if score <= cfg.direct_skip:
            return {"action": "skip", "delay_s": 0.0, "tone": "", "reason": "意愿过低", "source": "threshold"}

        fallback = _heuristic_intent(willingness, cfg)
        fallback["source"] = "heuristic"
        if not cfg.use_mind_model:
            return fallback
        try:
            result = await self._llm.json(
                build_reply_intent_messages(ctx, willingness, max_delay_s=cfg.max_delay_s),
                mock_hint={
                    "fallback_json": {
                        "action": fallback["action"],
                        "delay_s": fallback["delay_s"],
                        "reason": fallback["reason"],
                    }
                },
            )
        except Exception:
            logger.exception("回复意愿裁决调用失败，兜底为正常回")
            return {"action": "reply", "delay_s": 0.0, "tone": "", "reason": "异常兜底", "source": "error"}
        return self._normalize(result, fallback, cfg)

    @staticmethod
    def _normalize(
        result: Any, fallback: dict[str, Any], cfg: ReplyIntentConfig
    ) -> dict[str, Any]:
        """校验模型输出：档位非法 / 非 JSON → 用本地兜底；delay_s 夹到上限。"""
        if not isinstance(result, dict) or not result:
            return fallback
        action = str(result.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return fallback
        try:
            delay_s = float(result.get("delay_s") or 0.0)
        except (TypeError, ValueError):
            delay_s = 0.0
        delay_s = max(0.0, min(delay_s, cfg.max_delay_s))
        return {
            "action": action,
            "delay_s": delay_s if action == "delay" else 0.0,
            "tone": str(result.get("tone") or "")[:20],
            "reason": str(result.get("reason") or "")[:80],
            "source": "mind_model",
        }