"""主动教导（14.1）：用户用「记住……」这类话**直接把一条认知教给她**。

与认知习得（``learning``）的分工：

    learning 是"她自学"——从对话里自行提取概念，确信度低、可塑性高；
    teaching 是"你教她"——显式指令式的概念，确信度高、直接上锁
              （此后自学无法覆写，见 ``Database.upsert_cognition`` 的上锁保护）。

刻意做成**纯确定性规则**（正则 + 启发式），不依赖任何模型：命中就落盘，
不命中返回空列表、零副作用；任何异常都由调用方兜底，绝不阻塞主链路。
"""
from __future__ import annotations

import re

from loguru import logger

from ..engine.types import TurnContext
from ..store.db import Database

# 句子切分（教导通常按句给，一句最多产出一条）
_SENT_SPLIT = re.compile(r"[。！？!?\n；;]+")

# 显式教导句式：句首是可选礼貌前缀 + 教导引子。
# 只认"你要她记住"（教你……），不认"求你教我"（教我……）——后者方向相反。
_TEACH_RE = re.compile(
    r"^(?:(?:你|您|请|麻烦|帮我|给我|千万|务必|一定|要|得)\s*)*"
    r"(?:记住|牢记|记着|记一下|记好了|教你)"
    r"\s*[：:，,]?\s*(?P<body>.+)"
)

# "X是Y / X叫Y / X就是Y / X指的是Y" —— 取 X 当话题
_DEF_RE = re.compile(r"^(?P<topic>[^，,。！？!?；;、\s]{1,12}?)(?:就是|是|叫|指的是|表示|的意思)")

# 退化取话题时的切分符
_FRAG_SPLIT = re.compile(r"[，,。！？!?；;、\s]")
# 话题首尾要去掉的引号/书名号
_TRIM = "「」《》\"'“”‘’ "


def _extract_topic(body: str) -> str:
    """从"要教的内容"里猜一个简短话题：优先取定义句的主语，否则取片段前若干字。"""
    text = body.strip().strip(_TRIM)
    m = _DEF_RE.match(text)
    if m:
        topic = m.group("topic").strip().strip(_TRIM)
        if topic:
            return topic
    frag = _FRAG_SPLIT.split(text, maxsplit=1)[0].strip().strip(_TRIM)
    return frag[:10]


class TeachingEngine:
    """主动教导引擎（纯确定性，无模型依赖）。"""

    def __init__(self, db: Database, max_per_round: int = 2) -> None:
        self._db = db
        self._max = max(1, max_per_round)

    def detect_teaching(self, user_text: str) -> list[dict]:
        """从用户文本里识别"要她记住"的条目；每句最多 1 条，整轮不超过上限。"""
        out: list[dict] = []
        for sentence in _SENT_SPLIT.split(user_text or ""):
            s = sentence.strip()
            if not s:
                continue
            m = _TEACH_RE.match(s)
            if not m:
                continue
            body = (m.group("body") or "").strip().strip(_TRIM)
            if len(body) < 2:
                continue
            topic = _extract_topic(body)
            if not topic or any(it["topic"] == topic for it in out):
                continue
            out.append({"topic": topic, "understanding": body})
            if len(out) >= self._max:
                break
        return out

    async def apply(self, ctx: TurnContext) -> list[dict]:
        """把本轮识别到的教导落盘（高确信度、上锁）。无命中返回 []，零副作用。"""
        items = self.detect_teaching(ctx.user_text)
        if not items:
            return []
        life = ctx.life
        taught: list[dict] = []
        for it in items:
            try:
                await self._db.teach_cognition(
                    life.life_id, it["topic"], it["understanding"], by_user=ctx.user_id,
                )
                taught.append(it)
            except Exception:
                logger.exception("教导落盘失败")
        if taught:
            logger.info(f"[{life.life_id}] 用户教导 {len(taught)} 条认知："
                        f"{'、'.join(x['topic'] for x in taught)}")
        return taught
