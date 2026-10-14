"""记忆系统：短期窗口 → 滚动摘要 → 长期事实 → 日记 → 话题 / 生活线（设计文档 8. F）。

分层（7. 数据模型）：

  * **短期窗口** 最近 N 轮原文（直接作为 chat 历史）
  * **滚动摘要** 超窗口部分增量压缩（中期块）
  * **长期事实** 跨天记住你是谁（中期块，FTS5 可召回）
  * **日记** 主动搭话的素材
  * **话题栈 / 生活线** 供"延续话题"与"分享生活"

写入策略：**先回复、后结算**，且**落盘即时化**——消息在"发生的那一刻"就写库
（``persist_user`` / ``persist_assistant``），后处理（``finalize_turn``）只在轮末做
摘要 / 事实 / 日记，不阻塞对话主链路，也不会因中途被打断而丢失已发生的内容。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from loguru import logger

from ..engine.config import MemoryConfig
from ..engine.types import (
    ChatMessage,
    IncomingMessage,
    LifeRecord,
    Role,
    Segment,
    TurnContext,
)
from ..store.db import Database
from .llm import LLMGateway
from .retriever import KeywordRetriever


def _stamp(dt: datetime) -> str:
    """把时间格式化成「xx年xx月xx日 几点几分」，作为喂给模型的时间线索。"""
    try:
        return dt.strftime("%Y年%m月%d日 %H:%M")
    except Exception:
        return ""


def _seg_meta(s: Segment) -> dict[str, Any]:
    """把一条 Segment 转成回显用的结构化 dict（前端据此还原气泡类型）。

    表情 / 戳一戳是信道上独立的气泡，文本段仍存正文；前端须兼容旧的 string 元素。
    """
    k = s.meta.get("kind")
    if k == "sticker":
        return {
            "type": "sticker",
            "id": s.meta.get("sticker_id"),
            "url": s.meta.get("sticker_url"),
            "label": s.meta.get("label", ""),
        }
    if k == "poke":
        return {"type": "poke"}
    return {"type": "text", "text": s.text}


class MemoryManager:
    def __init__(
        self,
        db: Database,
        llm: LLMGateway,
        cfg: MemoryConfig,
        retriever: KeywordRetriever,
    ) -> None:
        self._db = db
        self._llm = llm
        self._cfg = cfg
        self._retriever = retriever

    # ==================================================================
    # 读取：把记忆灌进本轮上下文
    # ==================================================================
    async def hydrate(self, ctx: TurnContext) -> list[dict[str, str]]:
        """返回短期窗口（chat 历史），并填充 ctx 的摘要/事实/召回/话题/生活线。"""
        life_id, user_id = ctx.life.life_id, ctx.user_id

        recent = await self._db.load_recent(
            life_id, user_id, limit=self._cfg.short_window_rounds * 2
        )
        # 每条都缀上「xx年xx月xx日 几点几分」，她才"知道啥时候聊的"（不是只知现在几点）
        history: list[dict[str, str]] = []
        for m in recent:
            if m.role not in (Role.USER, Role.ASSISTANT, Role.PROACTIVE) or not m.content.strip():
                continue
            stamp = _stamp(m.created_at)
            history.append({
                "role": "assistant" if m.role in (Role.ASSISTANT, Role.PROACTIVE) else "user",
                "content": f"[{stamp}] {m.content}" if stamp else m.content,
            })

        summary, _covered = await self._db.get_summary(life_id, user_id)
        ctx.summary_text = summary

        facts = await self._db.load_facts(life_id, user_id, limit=18)
        ctx.facts_text = "\n".join(f"- {f['key']}：{f['value']}" for f in facts)

        # 认知习得产物：她对这个世界的理解（跨用户），blank 体靠它"长大"
        cognition = await self._db.load_cognition(life_id, limit=16)
        ctx.cognition_text = "\n".join(
            f"- {c['topic']}：{c['understanding']}" for c in cognition
        )

        query = ctx.user_text.strip()
        if query:
            hits = await self._retriever.recall(life_id, user_id, query, k=self._cfg.recall_top_k)
            ctx.recall_text = "\n".join(f"- {h['text']}" for h in hits)

        topics = await self._db.load_topics(life_id, user_id, status="open", limit=4)
        ctx.topics_text = "\n".join(f"- {t['title']}" for t in topics)

        events = await self._db.recent_life_events(life_id, limit=3)
        if events:
            ctx.life_note = "；".join(e["activity"] for e in events)

        return history

    # ==================================================================
    # 写入：把"已经发生的事实"即时落盘（不再挂在轮末）
    # ==================================================================
    # 以前的落盘挂在轮末（record_turn 在 _settle 里跑），一旦本轮在她"正说话"
    # 时被打断，_settle 根本不会执行 → 用户消息与她已说出口的气泡整轮丢失。
    # 现在拆成三个独立步骤，各自在最靠近"事实发生"的时刻执行：
    #   · persist_user       —— 消息一进轮次就落盘（被打断也不丢）
    #   · persist_assistant  —— 她"说出口的部分"在说话收尾/被打断时落盘
    #   · finalize_turn      —— 只做后处理（摘要/事实/日记），不再负责消息落盘
    async def persist_user(self, ctx: TurnContext) -> int:
        """入站消息落盘（幂等：同一条 IncomingMessage 只写一次）。

        用 ``IncomingMessage.id`` 作 ``ChatMessage.id``，配 ``INSERT OR REPLACE``，
        即使因合并/续跑被重复处理也不会写出重复行。
        """
        life_id, user_id = ctx.life.life_id, ctx.user_id
        written = 0
        for inc in ctx.batch:
            if inc.meta.get("_persisted"):
                continue
            await self._db.save_message(
                ChatMessage(
                    id=inc.id, life_id=life_id, user_id=user_id, role=Role.USER,
                    content=inc.content, channel=inc.channel, created_at=inc.received_at,
                )
            )
            await self._db.add_round(
                life_id=life_id, user_id=user_id, role="user",
                content=inc.content, ts=inc.received_at,
            )
            inc.meta["_persisted"] = True
            written += 1
        return written

    async def persist_assistant(
        self,
        ctx: TurnContext,
        spoken: list[Segment],
        *,
        persist_id: str,
        timing: dict[str, Any] | None = None,
    ) -> None:
        """把"她实际说出口的部分"（``spoken``）落盘。

        正常说完 → spoken 即全部段落；被打断 / 被停止 → spoken 只含已经吐出的
        那几条，剩余未说的一律作废。用固定 ``persist_id`` 保证只落一行。
        """
        if not spoken:
            return
        life_id, user_id = ctx.life.life_id, ctx.user_id
        full = " ".join(s.text for s in spoken).strip()
        # 分段结构随消息落盘（meta.segments）：回显时前端按段还原成多条气泡。
        # 含表情/戳一戳（特殊气泡）时必存结构化；纯文本单段则不存（兼容旧数据）。
        has_special = any(s.meta.get("kind") in ("sticker", "poke") for s in spoken)
        meta = (
            {"segments": [_seg_meta(s) for s in spoken]}
            if (len(spoken) > 1 or has_special)
            else {}
        )
        await self._db.save_message(
            ChatMessage(
                id=persist_id, life_id=life_id, user_id=user_id,
                role=Role.ASSISTANT, content=full,
                # 此前分段只存 round 表而 /api/history 只读 message 表，
                # 导致"实时连发多条、切页回来糊成一整段"。
                meta=meta,
            )
        )
        await self._db.add_round(
            life_id=life_id, user_id=user_id,
            role="proactive" if ctx.task == "proactive" else "assistant",
            content=full,
            segments=[s.model_dump() for s in spoken],
            timing=timing or {},
            emotion_delta=ctx.psyche.emotion_valence,
        )

    async def finalize_turn(
        self,
        ctx: TurnContext,
        segments: list[Segment],
        *,
        timing: dict[str, Any] | None = None,
    ) -> None:
        """轮末后处理（不落消息）：滚动摘要 + 事实抽取 + 跨天日记。"""
        life_id, user_id = ctx.life.life_id, ctx.user_id

        total = await self._db.round_count(life_id, user_id)
        if total and total % self._cfg.summarize_every_rounds == 0:
            await self._summarize(life_id, user_id, total)
        if total and total % self._cfg.fact_extract_every_rounds == 0:
            await self._extract_facts(life_id, user_id, ctx)

        # 跨天写日记
        await self._maybe_diary(life_id, user_id, ctx)

    # ---- 滚动摘要 ------------------------------------------------------
    async def _summarize(self, life_id: str, user_id: str, total: int) -> None:
        _old, covered = await self._db.get_summary(life_id, user_id)
        keep_from = total - self._cfg.short_window_rounds
        if keep_from <= covered:
            return
        rows = await self._db.rounds_after(life_id, user_id, covered, limit=200)
        chunk = [
            f"{'对方' if r['role'] == 'user' else '你'}：{r['content']}"
            for r in rows
            if r["id"] <= keep_from and r["content"].strip()
        ]
        if not chunk:
            return
        prev, _ = await self._db.get_summary(life_id, user_id)
        prompt = (
            "把下面这段聊天记录压缩成一段「关系记忆摘要」，保留：聊过的话题、对方的关键信息、"
            "情绪变化、约定或未完成的事。用第三人称，200 字以内，不要分点。\n\n"
            f"已有摘要：{prev or '（无）'}\n\n新增记录：\n" + "\n".join(chunk[-60:])
        )
        summary = await self._llm.complete(
            [{"role": "system", "content": "你是记忆压缩器，只输出摘要正文。"},
             {"role": "user", "content": prompt}],
            task="summary",
            kind="mind",
            temperature=0.3,
            max_tokens=400,
            mock_hint={"fallback_text": "；".join(chunk[-6:])[:200]},
        )
        if not summary:
            summary = "；".join(chunk[-6:])[:200]
        await self._db.set_summary(life_id, user_id, summary or prev, keep_from)

    # ---- 事实抽取 ------------------------------------------------------
    async def _extract_facts(self, life_id: str, user_id: str, ctx: TurnContext) -> None:
        text = ctx.user_text.strip()
        if not text:
            return
        heuristic = heuristic_facts(text)
        mock_facts = [{"key": k, "value": v, "importance": 0.6} for k, v in heuristic]
        result = await self._llm.json(
            [
                {"role": "system", "content":
                    "从用户的话里抽取值得长期记住的事实，只输出 JSON："
                    '{"facts":[{"key":"","value":"","importance":0.0}]}。'
                    "key 用简短名词（如 名字/职业/爱好/生日/忌口），没有就返回空数组。"},
                {"role": "user", "content": text},
            ],
            mock_hint={"fallback_json": {"facts": mock_facts}},
        )
        facts = result.get("facts") if isinstance(result, dict) else None
        if not facts and mock_facts:
            facts = mock_facts
        for f in facts or []:
            key, value = (f.get("key") or "").strip(), (f.get("value") or "").strip()
            if key and value:
                await self._db.upsert_fact(
                    life_id, user_id, key, value, float(f.get("importance", 0.5))
                )

    # ---- 日记 ----------------------------------------------------------
    async def _maybe_diary(self, life_id: str, user_id: str, ctx: TurnContext) -> None:
        day = ctx.now.strftime("%Y-%m-%d")
        existing = await self._db.load_diary(life_id, user_id, limit=1)
        if existing and existing[0]["day"] == day:
            return
        facts = await self._db.load_facts(life_id, user_id, limit=5)
        highlights = [f"{f['key']}：{f['value']}" for f in facts[:3]]
        content = (
            f"{day} 和对方聊了会儿。"
            + (f"聊到了 {'、'.join(highlights)}。" if highlights else "")
            + f"当时心情{ctx.psyche.mood_label}。"
        )
        await self._db.upsert_diary(life_id, user_id, day, content, highlights)

    # ---- 话题栈 --------------------------------------------------------
    async def touch_topic(self, life_id: str, user_id: str, title: str, status: str = "open") -> None:
        total = await self._db.round_count(life_id, user_id)
        await self._db.upsert_topic(life_id, user_id, title[:40], status, total)


# ======================================================================
# 本地事实抽取（LLM 不可用时的兜底 / 也是 mock 的返回）
# ======================================================================
_PATTERNS: list[tuple[str, str]] = [
    ("名字", r"我(?:叫|是|的名字是)\s*([\u4e00-\u9fffA-Za-z]{1,12})"),
    ("职业", r"我(?:是|在做|从事)\s*([\u4e00-\u9fff]{2,10}(?:师|生|员|工|家|岗|经理|老板))"),
    ("年龄", r"我\s*(\d{1,2})\s*(?:岁|了)"),
    ("城市", r"我(?:在|住在|来自)\s*([\u4e00-\u9fff]{2,10}(?:省|市|区|县|州)?)"),
    ("爱好", r"我(?:喜欢|爱|超爱)\s*([\u4e00-\u9fffA-Za-z0-9]{1,16})"),
    ("生日", r"我(?:的)?生日(?:是)?\s*([0-9]{1,2}[月\-/][0-9]{1,2}[日号]?)"),
    ("忌口", r"我(?:不吃|忌口|讨厌吃)\s*([\u4e00-\u9fff]{1,10})"),
]


def heuristic_facts(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for key, pat in _PATTERNS:
        m = re.search(pat, text)
        if m:
            out.append((key, m.group(1).strip()))
    return out