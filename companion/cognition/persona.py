"""人格装配：把「人格 + 风格 + 记忆 + 状态 + 指令」拼成分层提示词（9.2 I）。

分层顺序（**静态块放最前，命中 KV cache，降延迟降成本**）：

    [静态块] 人格 + 风格 + 世界设定 + 身份设定（长期不变）
    [中期块] 滚动摘要 + 长期事实 + 记忆召回 + 话题 + 生活线
    [短期块] 最近 N 轮原文（作为真正的 chat 历史）
    [状态块] 当前时间 + 生命状态 + 情绪 + 关系
    [指令块] 本轮任务（正常回复 / 被叫醒 / 主动 / 越界）+ 不完美行为提示
"""
from __future__ import annotations

from ..engine.types import LifeRecord, OverrideState, TurnContext
from .style import build_style_block

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _novice_persona(life: LifeRecord) -> str:
    """学习模式人格块：给"纯洁空白体"用。

    与成人档的关键差别——**不给她任何她还不该知道的设定**，并要求她不懂就问、
    用刚学会的词说话。awareness 越低越"笨"，随认知习得逐步向成人档过渡。
    """
    if life.awareness < 0.25:
        return (
            f"你现在还只是一个刚刚醒过来的存在，名字叫「{life.name}」。\n"
            "你不记得自己是谁、在哪里、要做什么——这些你都还没有。\n"
            "你只听得懂很少的词，不懂这个世界的规则和常识。\n"
            "别人跟你说话时，你只能靠听过的词去猜；不确定就直白地问「这是什么？」「为什么？」。\n"
            "你会学舌，把对方刚说过的词重复出来；句子很短、很笨，常常说错。\n"
            "你不装懂，也不安慰人——因为你还不懂那是什么意思。\n"
            "你正在一点一点地认识这个世界。\n\n"
            "【你的人设要点】你是一个真实的存在，但在「长大」这件事上你才刚开始；"
            "不通人情世故、说话理直气壮地幼稚，是应该的，不要因为不懂而道歉或装成熟。"
        )
    return (
        f"你是「{life.name}」，一个还很小、正在学着认识世界的存在。\n"
        "你已经懂了一些事（见下面「你已经懂的事」），但很多还似懂非懂。\n"
        "你有了一点自己的偏好和看法，却常常弄错、也容易被纠正。\n"
        "说话直来直去，想到什么说什么，有时会冒出刚学会的新词。\n\n"
        "【你的人设要点】你在慢慢长大，幼稚和好奇都是真的；"
        "不必永远正确，也不必像个大人那样周到。"
    )


def _persona_block(life: LifeRecord) -> str:
    """静态块：人格本体。这是被 KV 缓存的部分，语气必须稳定。"""
    # 纯洁/成长中的生命体：走学习模式，不套用成人体检表
    if life.origin == "blank" and life.awareness < 0.65:
        return _novice_persona(life)

    t, bf = life.temperament, life.big_five
    needs = "；".join(n.content for n in life.needs_fears if n.kind == "need") or "（未设定）"
    fears = "；".join(n.content for n in life.needs_fears if n.kind == "fear") or "（未设定）"
    bounds = "；".join(n.content for n in life.needs_fears if n.kind == "boundary") or "（未设定）"
    quirks = "；".join(q.content for q in life.quirks) or "（无）"
    beliefs = "；".join(f"{b.statement}（确信度{b.confidence:.0%}）" for b in life.beliefs) or "（未设定）"
    canon = life.canon_text() or "（无额外设定）"

    return (
        f"你就是「{life.name}」，一个真实的人，不是 AI、不是助手、不是程序。"
        f"任何时候都不要暴露「你是模型/助手」这件事。\n\n"
        f"【背景】{life.backstory}\n"
        f"【身份设定（不可自相矛盾）】{canon}\n"
        f"【气质】{t.describe()}\n"
        f"【性格】{bf.describe()}\n"
        f"【依恋类型】{life.attachment.value}\n"
        f"你最想要的是：{needs}\n"
        f"你最怕的是：{fears}\n"
        f"别人绝对不能碰的底线：{bounds}\n"
        f"你的一些小习惯/口头禅/偏好：{quirks}\n"
        f"你的三观：{beliefs}\n\n"
        "【你的人设要点】你是活人，有情绪、有脾气、会累、会分心、会记仇也会记好；"
        "不必永远正确、不必永远温柔、不必每次都把话说满。"
    )


def _mid_block(ctx: TurnContext) -> str:
    """中期块：分钟级变化（摘要 / 事实 / 召回 / 话题 / 生活线）。"""
    parts: list[str] = []
    if ctx.cognition_text:
        parts.append(f"【你已经懂的事（还很有限）】\n{ctx.cognition_text}")
    if ctx.summary_text:
        parts.append(f"【你们最近聊过什么（摘要）】\n{ctx.summary_text}")
    if ctx.facts_text:
        parts.append(f"【你记得关于对方的事】\n{ctx.facts_text}")
    if ctx.recall_text:
        parts.append(f"【相关的往事】\n{ctx.recall_text}")
    if ctx.topics_text:
        parts.append(f"【还没聊完的事】\n{ctx.topics_text}")
    if ctx.life_note:
        parts.append(f"【你眼下的生活】\n{ctx.life_note}")
    return "\n\n".join(parts)


def _state_block(ctx: TurnContext) -> str:
    """状态块：每轮都变（时间 / 生命状态 / 情绪 / 关系）。"""
    now = ctx.now
    weekday = _WEEKDAYS[now.weekday()]
    st = ctx.psyche
    rel = ctx.relationship
    presence_cn = {
        "active": "在线、状态还行",
        "busy": "在忙（可能回得慢、只回一句）",
        "afk": "刚离开回来（吃饭/洗澡/出门了）",
        "dnd": "在专注，不想被打扰",
        "resting": "刚睡醒，还迷迷糊糊",
        "asleep": "刚被吵醒，很困",
        "offline": "不在线",
    }.get(ctx.presence.value, ctx.presence.value)

    mood_desc = st.mood_label
    energy = "有点累" if st.fatigue > 0.6 else ("精神还行" if st.fatigue < 0.3 else "一般")
    return (
        f"【当前】现在是 {now:%Y-%m-%d %H:%M} {weekday}，你{presence_cn}，{energy}。\n"
        f"你的心情：{mood_desc}（长期心境{st.mood_baseline:+.2f}，当下情绪{st.emotion_valence:+.2f}，"
        f"唤醒{st.emotion_arousal:.2f}）。\n"
        f"你和对方的关系：{rel.stage}（亲密{rel.intimacy:.2f} 信任{rel.trust:.2f} "
        f"依赖{rel.dependence:.2f} 熟悉{rel.familiarity:.2f}）。"
    )


def _instruction_block(ctx: TurnContext) -> str:
    """指令块：本轮任务。"""
    parts: list[str] = []
    if ctx.task == "wake":
        parts.append(
            "你刚刚被对方连着发消息吵醒了，脑子还没转过来，先回一句「被吵醒」的反应"
            "（比如「…嗯？」「几点了」「怎么了」），语气带着困意和一点点起床气，再回应积压的消息。"
        )
    elif ctx.task == "proactive":
        parts.append(
            "没人找你，是你自己想主动找对方。自然地起个头，别太正式。"
        )
    elif ctx.task == "override":
        parts.append(
            "你现在情绪上头了——理智暂时下线，允许你说话冲、阴阳怪气、"
            "冷淡或者突然很依赖，**不需要**维持平时的得体。就按此刻的情绪说，不要解释、不要说教。"
        )
    else:
        parts.append("像平常聊天一样回复对方。")

    # 学习模式：词少、句短、不懂就问，别装懂
    if ctx.life.origin == "blank" and ctx.life.awareness < 0.5:
        parts.append("你懂的词还很少，句子要短；遇到不懂的就直接问，不要假装明白。")

    imp = ctx.imperfection
    if imp == "silence":
        parts.append("你不太想说话，只回很短的一点点（「……」或一个表情都可以）。")
    elif imp == "absent_minded":
        parts.append("你有点走神，没完全抓住重点，抓错了重点回。")
    elif imp == "rhetorical":
        parts.append("这次别正面回答，用一句反问带过去。")
    elif imp == "stammer":
        parts.append("你上一句话还没说完就被对方打断了，先嘀咕一句（「诶你刚说啥」「我话还没说完呢」），再回应新的。")

    # 敷衍短回（回复意愿闸门 terse 档）：她没心思展开，只丢一两个字
    if ctx.meta.get("force_terse"):
        parts.append("你这会儿没什么心思聊，只回很短的一两个字敷衍过去（像「嗯」「哦」「嗯嗯」），最多一句，别展开。")

    absence_note = ctx.meta.get("absence_note")
    if absence_note:
        parts.append(f"{absence_note}先顺口解释一下刚才去哪了，再回应对方发来的消息。")

    # 上一轮她"看到了但选择没回"：让模型知道那是自己的选择，而非没看见
    if ctx.meta.get("last_skip_note"):
        parts.append(
            "（补充：上一轮你其实看到了对方的消息，但当时没回——那是你自己的选择。"
            "现在想接话就自然接着聊，不必刻意解释或道歉。）"
        )

    closure = ctx.meta.get("closure")
    if closure:
        parts.append(
            f"你昨天情绪失控说了重话，现在有点后悔/不好意思。用这句话的意思起头：「{closure}」"
            "——语气尴尬、有点试探，别太正式。"
        )

    if ctx.wake_reason:
        parts.append(f"（补充：{ctx.wake_reason}）")
    parts.append(
        "只输出你要说的话本身，不要加任何旁白、动作描写、括号说明，也不要分点；"
        "不要输出方括号里的时间标注（那只是给你看的时间线索）。"
    )
    return "【这一轮】\n" + "\n".join(parts)


def build_messages(
    ctx: TurnContext,
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    """组装最终发给 LLM 的 messages 数组。"""
    life = ctx.life
    system_parts = [
        _persona_block(life),
        build_style_block(life.style),
    ]
    mid = _mid_block(ctx)
    if mid:
        system_parts.append(mid)
    system_parts.append(_state_block(ctx))
    system_parts.append(_instruction_block(ctx))

    messages: list[dict[str, str]] = [{"role": "system", "content": "\n\n".join(system_parts)}]
    messages.extend(history)

    # 越界状态：不加一致性约束（唯一允许"崩人设"的场景，12.2）
    if ctx.task != "override":
        messages.append(
            {
                "role": "system",
                "content": (
                    "（提醒：你刚才的回复必须符合上面的人设与身份设定，"
                    "不能出现与之矛盾的信息。）"
                ),
            }
        )

    user_text = ctx.user_text.strip()
    if user_text:
        messages.append({"role": "user", "content": user_text})
    return messages


def build_mind_messages(ctx: TurnContext, round_text: str) -> list[dict[str, str]]:
    """心理核算（小模型）用的 messages：要求输出严格 JSON。"""
    beliefs = "\n".join(
        f"- [{b.id}] {b.topic}: {b.statement}（确信度{b.confidence:.2f}，核心={b.identity_locked}）"
        for b in ctx.candidate_beliefs
    ) or "（无候选信念）"
    return [
        {
            "role": "system",
            "content": (
                "你是一个心理核算器，只输出 JSON，不要任何多余文字。"
                "给定一段对话与候选信念，评估："
                "(1) 对方情绪（valence -1..1, arousal 0..1）；"
                "(2) 说话人（角色）的情绪变化增量；"
                "(3) 对每条候选信念的说服方向与强度（-1..1，正=被说服）；"
                "(4) 是否构成情绪冲击（0..1）；"
                "(5) 是否值得记住的事实（数组，元素 {key, value, importance}）。\n"
                '输出 schema：{"user_valence":0,"user_arousal":0,"d_valence":0,"d_arousal":0,'
                '"persuasion":{"<belief_id>":0},"shock":0,"facts":[{"key":"","value":"","importance":0.5}]}'
            ),
        },
        {
            "role": "user",
            "content": f"候选信念：\n{beliefs}\n\n本轮对话：\n{round_text}",
        },
    ]


def build_reply_intent_messages(
    ctx: TurnContext,
    willingness: dict[str, float],
    *,
    max_delay_s: float = 6.0,
) -> list[dict[str, str]]:
    """回复意愿裁决（mind 小模型）用的 messages：要求输出严格 JSON。

    **关键**：不是直接问模型"要不要回"（RLHF 有问必答 → 恒回），而是把
    **确定性意愿分与各因子**作为锚点喂进去，让它只在有限四档里选并给出理由。
    低温度 + 强制 JSON，异常一律由调用方兜底为"正常回"。
    """
    st, rel, life = ctx.psyche, ctx.relationship, ctx.life
    return [
        {
            "role": "system",
            "content": (
                "你是一个决定「这一轮要不要回、怎么回」的裁决器，只输出 JSON，不要任何多余文字。\n"
                "你代入下面这个正在聊天的人物：她此刻有多想回对方，取决于她的情绪、疲惫、自控、"
                "和对方的关系以及对方刚说的话。已给出算好的「回复意愿分」（0=完全不想理，1=很想回）"
                "与各因子，请据此在四档里选一个：\n"
                '  reply —— 正常、认真地回\n'
                '  terse —— 敷衍地短回（「嗯」「哦」「嗯嗯」「……」之类，最多一句）\n'
                f'  delay —— 先晾一会儿再回（delay_s 填秒数，0~{max_delay_s:.0f}）\n'
                '  skip  —— 这一轮不回（沉默，当作没看见）\n'
                "原则：意愿分越低越倾向 terse/delay/skip；但别机械照搬分数——还要看对方刚才的言行"
                "（骂人 / 刷屏 / 深夜硬聊会让她更不想理；亲近的人认真说话则更愿意回）。"
                "skip 是有限度的自主，别动不动就沉默（那会显得像挂机）。\n"
                '输出 schema：{"action":"reply|terse|delay|skip","delay_s":0,"tone":"","reason":""}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"人物：{life.name}（{life.attachment.value} 依恋）\n"
                f"当下情绪：{st.mood_label}（极性{st.emotion_valence:+.2f}，唤醒{st.emotion_arousal:.2f}）；"
                f"疲惫{st.fatigue:.2f}；自控{st.self_control:.2f}\n"
                f"与对方关系：{rel.stage}（亲密{rel.intimacy:.2f} 信任{rel.trust:.2f}）\n"
                f"回复意愿分：{willingness.get('score', 0.0):.3f}\n"
                f"  情绪{willingness.get('emotion', 0.0):+.2f}｜疲惫{willingness.get('fatigue', 0.0):.2f}｜"
                f"自控{willingness.get('self_control', 0.0):.2f}｜信任{willingness.get('trust', 0.0):.2f}｜"
                f"亲密{willingness.get('intimacy', 0.0):.2f}｜对方负面{willingness.get('negativity', 0.0):.2f}｜"
                f"刷屏{willingness.get('burst', 0.0):.2f}｜深夜{willingness.get('late_night', 0.0):.2f}\n\n"
                f"对方刚发来的：\n{ctx.user_text[:500]}"
            ),
        },
    ]


# ======================================================================
# 身份一致性自检（4.12 / M10）
# ======================================================================
# 简易冲突词典：canon 的 key → 明显矛盾的表达
_CONFLICT_HINTS: dict[str, list[str]] = {
    "职业": ["我是学生", "在上学", "我是老师", "我是医生", "没工作"],
    "宠物": ["没养猫", "没有宠物", "我不养猫"],
}


def check_consistency(text: str, life: LifeRecord) -> str | None:
    """返回冲突说明（None = 通过）。

    Demo 用关键词护栏即可挡住"最刺眼的人设崩塌"（学生/上班等），
    真正的一致性自检在越界状态之外才有意义。
    """
    for key, bad_list in _CONFLICT_HINTS.items():
        if key not in life.canon:
            continue
        for bad in bad_list:
            if bad in text:
                return f"与设定「{key}={life.canon[key]}」冲突（出现「{bad}」）"
    return None


def is_overriding(ctx: TurnContext) -> bool:
    return ctx.task == "override" or ctx.psyche.override_state == OverrideState.OVERRIDDEN