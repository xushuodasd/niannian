"""认知习得（14）：让生命体从对话里**从无到有**地长出对世界的理解。

与三观漂移（``mind.belief``）的分工：

    belief   是"立场"——已有三观被说服着漂移（可能升也可能降）；
    learning 是"认知"——原本空白的概念被**新增**进来（纯洁体就靠这里长大）。

每轮结算时挑出"她第一次接触到的概念"，交给小模型生成一句"她的理解"写进
``cognition`` 表；反复接触的同一条会**毕业**成一条三观。最后按她的认知/信念
存量重算 ``awareness``（成熟度），驱动提示词从"学习模式"逐步切到成人档。

**四重约束（防止"概念表"无限膨胀 + 垃圾概念灌入）**（参数见 ``CognitionConfig``）：

  * 只收"世界知识 / 事物概念"，问候语 / 语气词 / 脏话 / 整句话一律不学；
  * 低于 ``min_confidence`` 不采纳；无可用模型时**不自学**（宁缺毋滥）；
  * 每 ``learn_every_rounds`` 轮才学一次，每轮最多新增 ``max_new_per_round`` 条；
  * 到 ``max_rows`` 后**只更新已有条目、不再新增**（严防无限增长）。

用户主动教导 / 锁定条目走独立链路（``teaching``），不受上述上限影响。
"""
from __future__ import annotations

import re

from loguru import logger

from ..engine.config import CognitionConfig
from ..engine.types import Belief, LifeRecord, TurnContext, clamp
from ..store.db import Database
from .llm import LLMGateway

# 学成一条三观所需的"反复接触"次数
_GRADUATE_AT = 3
# 自动毕业出的三观上限（避免噪声淹没人格）
_MAX_AUTO_BELIEFS = 8
# awareness 到达满值所需的存量
_FULL_COGNITION = 30
_FULL_BELIEFS = 4

_LEARN_SYSTEM = (
    "你是「认知提取器」。给定一段对话和一个刚诞生、见识很有限的存在，"
    "提取她**这一轮第一次接触到的【世界知识或事物概念】**（宁少勿滥，通常 0~1 条），"
    "并为它写一句她会形成的理解（第一人称、口语、可以幼稚）。"
    "只提取【物品、地点、职业、活动、现象、规则】这类可长期复用的概念；"
    "**必须忽略**：问候语、语气词与口头禅、情绪宣泄、指责或脏话、指令与请求、"
    "代词与助词、完整的一整句话、以及对方的名字或私人称呼。"
    "如果没有值得记住的概念，就返回空数组。"
    "只输出 JSON：{\"learned\":[{\"topic\":\"简短名词\",\"understanding\":\"她的理解\",\"confidence\":0.0}]}"
    "；confidence 按你的把握给 0~1，拿不准就给低分甚至不输出。"
)

# 话题必须先"洗干净"：只留中文/字母/数字，去掉标点与残留符号（如模型漏出的 "}"）
_TOPIC_KEEP = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")


def _clean_topic(raw: str) -> str:
    """把模型/兜底给的话题规范成"干净的短名词"，不合法则返回空串。"""
    t = _TOPIC_KEEP.sub("", (raw or "").strip())
    return t if 2 <= len(t) <= 8 else ""


def heuristic_learn(user_text: str, known_topics: set[str]) -> list[dict]:
    """离线兜底：**不再乱学**。

    旧实现把用户话里任意 2~6 字中文片段都当"新概念"（停止词表只有十几个），
    导致问候、语气词、脏话乃至整句话被永久记成"认知"，概念表被垃圾灌满。
    这里改为：**无可用模型时不自学**（返回空），只保留"用户主动教导"这条可靠
    知识入口——宁可学得少，也绝不污染她对世界的理解。
    """
    return []


class CognitionLearner:
    """认知习得服务（异步旁路，跟随"先回复、后结算"落盘）。"""

    def __init__(self, db: Database, llm: LLMGateway, cfg: CognitionConfig | None = None) -> None:
        self._db = db
        self._llm = llm
        self._cfg = cfg or CognitionConfig()
        # 每个生命体"结算过多少轮"的内存计数（决定何时尝试学习；重启不影响数据）
        self._tick: dict[str, int] = {}

    async def learn(self, ctx: TurnContext, round_text: str,
                    exclude_topics: set[str] | None = None) -> list[dict]:
        """结算一步：习得认知 → 可能毕业三观 → 重算成熟度。返回本轮学到的东西。

        ``exclude_topics``：本轮已被"主动教导"处理的话题，跳过不重复自学
        （避免同一条被低确信度地覆写一遍，也避免与上锁行硬碰）。
        """
        life = ctx.life
        cfg = self._cfg
        if not cfg.enabled:
            return []

        # 降频：每 learn_every_rounds 轮（= 每次结算算一轮）才尝试学一次
        tick = self._tick.get(life.life_id, 0) + 1
        self._tick[life.life_id] = tick
        if cfg.learn_every_rounds > 1 and tick % cfg.learn_every_rounds != 0:
            return []

        exclude = exclude_topics or set()
        known = await self._db.load_cognition(life.life_id, limit=max(cfg.max_rows, 60))
        known_topics = {c["topic"] for c in known}
        at_cap = await self._db.count_cognition(life.life_id) >= cfg.max_rows

        fallback = {"learned": heuristic_learn(ctx.user_text, set(known_topics))}
        result = await self._llm.json(
            self._build_messages(ctx, round_text, known),
            mock_hint={"fallback_json": fallback},
        )
        items = result.get("learned") if isinstance(result, dict) else None
        if not items:
            items = fallback["learned"]

        learned: list[dict] = []
        added_new = 0
        for it in (items or []):
            topic = _clean_topic(str((it or {}).get("topic") or ""))
            understanding = str((it or {}).get("understanding") or "").strip()
            if not topic or not understanding or topic in exclude:
                continue
            confidence = clamp(float(it.get("confidence", 0.0) or 0.0), 0.1, 0.9)
            if confidence < cfg.min_confidence:
                continue  # 低确信度"概念"不采纳（旧兜底垃圾 0.32 全在此被挡下）
            is_new = topic not in known_topics
            if is_new and (at_cap or added_new >= cfg.max_new_per_round):
                continue  # 到顶 / 本轮新增额度用尽 → 只更新已有，不再新增
            await self._db.upsert_cognition(
                life.life_id, topic, understanding,
                confidence=confidence, malleability=0.8, source="dialogue",
            )
            known_topics.add(topic)
            if is_new:
                added_new += 1
            learned.append({"topic": topic, "understanding": understanding})
        if learned:
            logger.info(f"[{life.life_id}] 习得 {len(learned)} 条认知："
                        f"{'、'.join(x['topic'] for x in learned)}")

        await self._graduate(life)
        life.awareness = await self.recompute_awareness(life)
        return learned

    # ---- 反复接触 → 长出一条三观 --------------------------------------
    async def _graduate(self, life: LifeRecord) -> None:
        rows = await self._db.load_cognition(life.life_id, limit=80)
        auto_count = sum(1 for b in life.beliefs if b.topic in {r["topic"] for r in rows})
        existing = {b.topic for b in life.beliefs}
        for r in rows:
            if auto_count >= _MAX_AUTO_BELIEFS:
                return
            if r["exposure_count"] < _GRADUATE_AT or r["topic"] in existing:
                continue
            life.beliefs.append(Belief(
                topic=r["topic"], statement=r["understanding"],
                confidence=min(0.6, 0.3 + 0.1 * r["exposure_count"]),
                malleability=0.7,
            ))
            existing.add(r["topic"])
            auto_count += 1
            logger.info(f"[{life.life_id}] 认知「{r['topic']}」反复接触 → 毕业成一条三观")

    # ---- 成熟度：认知/三观存量换算（仅纯洁体需要，其余恒为 1） ---------
    async def recompute_awareness(self, life: LifeRecord) -> float:
        if life.origin != "blank":
            return life.awareness
        cog = await self._db.count_cognition(life.life_id)
        value = (
            0.55 * min(1.0, cog / _FULL_COGNITION)
            + 0.45 * min(1.0, len(life.beliefs) / _FULL_BELIEFS)
        )
        return clamp(value)

    # ---- 小模型提示 ----------------------------------------------------
    def _build_messages(self, ctx: TurnContext, round_text: str, known: list[dict]) -> list[dict]:
        known_text = "\n".join(f"- {c['topic']}：{c['understanding']}" for c in known[:20]) or "（她目前什么都不懂）"
        return [
            {"role": "system", "content": _LEARN_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"她已经懂的：\n{known_text}\n\n"
                    f"这一轮对话：\n{round_text}\n\n"
                    "请只提取她此前不懂的、值得长期记住的世界概念，其余一律忽略。"
                ),
            },
        ]
