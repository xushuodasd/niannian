"""说话风格层（4.9）—— 去"AI 味"最关键的一层。

真人感很大程度上不是内容问题，而是**措辞问题**。这里做三件事：

1. **`build_style_block`**：把风格档写进提示词的独立分块，约束模型；
2. **`clean`**：输出后**正则清洗机器味**（不指望模型自觉）——干掉分点列表、
   "作为一个 AI""希望对你有所帮助"等书面腔，并按习惯规整标点；
3. **`split_bubbles`**：把整段回复切成 1~N 条气泡（真人都是短句连发，不是一大段）。
"""
from __future__ import annotations

import random
import re

from ..engine.types import StyleProfile

# ----------------------------------------------------------------------
# 机器味黑名单（4.9）：命中即删/替换
# ----------------------------------------------------------------------
_BLACKLIST_PATTERNS: list[tuple[str, str]] = [
    (r"作为一个(\s*人工智能|\s*AI|\s*语言模型|助手)", ""),
    (r"作为\s*(AI|人工智能|语言模型|助手)", ""),
    (r"我是(一个)?(人工智能|AI|语言模型|智能助手)[，,。]?", ""),
    (r"希望(能)?(对你|对你有所帮助|帮到你)[。！]?", ""),
    (r"(如果)?(你)?(还)?有(其他)?(任何)?(问题|需要)[^。！？\n]*[，,]?(随时)?(告诉|问|找我)[^。！？\n]*[。！]?", ""),
    (r"需要我(帮你|为你)[^。！？\n]*吗[？?]?", ""),
    (r"(以下|下面)是[^。！？\n]*[：:]\s*", ""),
    (r"总结(一下|如下)[：:]\s*", ""),
    (r"综上(所述)?[，,]?\s*", ""),
    (r"总的(来说|来讲)[，,]?\s*", ""),
    (r"首先[，,]?\s*", ""),
    (r"其次[，,]?\s*", ""),
    (r"最后[，,]?\s*(我想说|说一句)?", ""),
    (r"另外[，,]?\s*再?补充(一点|一句)[，,]?\s*", ""),
    (r"我(很)?(乐意|乐意|愿意)(为你|帮你)[^。！？\n]*[。！]?", ""),
    (r"很(高兴|荣幸)(能|为你)[^。！？\n]*[。！]?", ""),
    # 时间戳标注：模型偶尔会照抄 prompt 里的时间线索（[2026年10月13日 19:43]），兜底剥离
    (r"\[\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*\d{1,2}\s*[:：]\s*\d{1,2}\s*\]\s*", ""),
    (r"\[\s*\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{1,2}\s*\]\s*", ""),
]

# 分点/列表行：整行删除（真人不会用 1. 2. 3. 说话）
_LIST_LINE = re.compile(r"^\s*(?:[-*•·]|\d+[\.、)）]|[（(]\d+[)）])\s*")

# 书面连接词 → 口语化替换（软清洗）
_SOFT_REPLACE: list[tuple[str, str]] = [
    (r"非常", "特别"),
    (r"十分", "挺"),
    (r"此外", "对了"),
    (r"因此", "所以"),
    (r"然而", "不过"),
    (r"并且", "而且"),
    (r"你好，", ""),
    (r"。\s*$", ""),          # 结尾的句号，真人常用空格/波浪号收尾
]

_SENT_END = re.compile(r"[。！？!?～~…]")

# 「怎么发」的分条习惯 —— 把它写进提示词，让模型自己就按习惯分条（胜过事后再切）
_HABIT_LINE = {
    "fragmented": "你说话是「碎碎念」型：几乎每句一发，单条很短（{n} 字以内），经常一口气连着发好几条。",
    "chatty": "你习惯短句连发：单条大概 {n} 字，常分成两三小条发，别攒成一大段。",
    "steady": "你说话不长不短：通常整合成一小段（约 {n} 字），偶尔拆成两条。",
    "monologue": "你习惯「一段话说完」：能一条发完就一条发完（约 {n} 字），不要主动拆成好几条。",
}
_HABIT_LABEL = {"fragmented": "碎碎念", "chatty": "短句连发", "steady": "一小段", "monologue": "一段说完"}
_LOOSE_HABITS = ("chatty", "fragmented")   # 这两类按"空格/逗号"再松一档切


def build_style_block(style: StyleProfile) -> str:
    """把风格档写成提示词的独立分块（静态块的一部分，利于 KV 缓存命中）。"""
    filler = "、".join(style.fillers) if style.fillers else "（无特定）"
    catch = "、".join(style.catchphrases) if style.catchphrases else "（暂无明显口头禅）"
    net = "、".join(style.net_speak) if style.net_speak else "（少用）"
    emoji_desc = (
        f"偶尔用（约{int(style.emoji_rate * 100)}%的消息，可从 {' '.join(style.emojis)} 里挑）"
        if style.emoji_rate > 0.05
        else "基本不用表情"
    )
    habit = getattr(style, "bubble_habit", "steady")
    habit_line = _HABIT_LINE.get(habit, _HABIT_LINE["steady"]).format(n=style.sentence_len)
    return (
        "【说话方式（必须严格遵守，这是你的语气，不是建议）】\n"
        f"- 你是活人，不是助手：不要说\"作为一个AI\"、不要分点罗列、不要用书面语做总结。\n"
        f"- 分条习惯：{habit_line}\n"
        f"- 标点习惯：{style.punct_style}。\n"
        f"- 常用语气词：{filler}。\n"
        f"- 口头禅：{catch}。\n"
        f"- 网络用语：{net}。\n"
        f"- 表情：{emoji_desc}。\n"
        "- 允许口语、省略、语气词、连着发几条；不要每次都把话说满说圆。"
    )


def clean(text: str, style: StyleProfile) -> str:
    """清洗 AI 腔 + 规整标点。"""
    if not text:
        return ""
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if _LIST_LINE.match(line):
            # 去掉列表符号后保留内容（真人可能只是换行，不该整段丢）
            line = _LIST_LINE.sub("", line)
        out_lines.append(line)
    text = "\n".join(out_lines)

    for pat, repl in _BLACKLIST_PATTERNS:
        text = re.sub(pat, repl, text)
    for pat, repl in _SOFT_REPLACE:
        text = re.sub(pat, repl, text)

    # 折叠多余空行/空格，但保留换行（换行也是"分条"信号）
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    """按中英标点断句，保留标点。"""
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if _SENT_END.match(ch):
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p]


def _merge_to_len(units: list[str], target: int) -> list[str]:
    """把过短的句子合并到目标长度附近，避免"嗯。""啊。"这种碎片气泡。"""
    merged: list[str] = []
    buf = ""
    for u in units:
        if not buf:
            buf = u
            continue
        if len(buf) < target * 0.75 and len(buf) + len(u) <= target * 1.6:
            buf += u
        else:
            merged.append(buf)
            buf = u
    if buf:
        merged.append(buf)
    return merged


def _split_loose(text: str) -> list[str]:
    """再松一档：空格、逗号、顿号、句末标点全当断点。

    "碎碎念 / 短句连发"的人发消息基本不带句末标点，一句一发——所以这里连
    ``。！？`` 也切开（切掉标点反而更像他们的手速）。
    """
    return [p for p in re.split(r"[\s，,、；;。！？!?～~…]+", text) if p]


def _merge_loose(units: list[str], target: int) -> list[str]:
    """松口径合并：用空格相连，避免把两个短句粘成一个词。"""
    merged: list[str] = []
    buf = ""
    for u in units:
        if not buf:
            buf = u
            continue
        if len(buf) < target * 0.8 and len(buf) + len(u) + 1 <= target * 1.5:
            buf += " " + u
        else:
            merged.append(buf)
            buf = u
    if buf:
        merged.append(buf)
    return merged


def split_bubbles(text: str, style: StyleProfile, rng: random.Random) -> list[str]:
    """把一整段回复切成气泡 —— **切法由 ``bubble_habit`` 决定**（说话习惯的核心差异）。

    - ``fragmented`` / ``chatty``：短句连发型，按空格/逗号再松一档切，倾向多段；
    - ``steady``：按句末标点切，合成一小段；
    - ``monologue``：一段话说完，只在实在过长时拆两段。
    """
    text = text.strip()
    if not text:
        return []
    habit = getattr(style, "bubble_habit", "steady")
    max_b = max(1, style.max_bubbles)

    # 「一段话说完」：能一条就一条，只有实在过长才拆两段
    if habit == "monologue":
        if len(text) <= 80 or max_b <= 1:
            return [text]
        sents = _split_sentences(text)
        if len(sents) < 2:
            return [text]
        half = max(1, len(sents) // 2)
        return ["".join(sents[:half]).strip(), "".join(sents[half:]).strip()]

    # 先尊重模型自己给的换行（它已经在"分条"）
    blocks = [b.strip() for b in text.split("\n") if b.strip()]
    loose = False
    if len(blocks) >= 2:
        units = blocks
    elif habit in _LOOSE_HABITS:
        units = _merge_loose(_split_loose(text), style.sentence_len)
        loose = True
    else:
        sents = _split_sentences(text)
        if len(sents) >= 2:
            units = _merge_to_len(sents, style.sentence_len)
        else:
            # 模型整段不落句末标点（爱用空格/逗号断句的人）→ 松一档切，别整块憋着
            units = _merge_loose(_split_loose(text), style.sentence_len)
            loose = True

    if len(units) <= 1:
        return units or [text]

    sep = " " if loose else ""
    if len(units) > max_b:
        # 超长回复：把余下的并进最后一条，避免一屏十几条（真实但也别太吵）
        units = units[: max_b - 1] + [sep.join(units[max_b - 1:])]

    # 是否真的分条：习惯越"碎"越倾向拆（split_prob 已按档位给好）
    bias = {"fragmented": 0.55, "chatty": 0.40, "steady": 0.15, "monologue": 0.0}.get(habit, 0.15)
    if len(units) > 1 and rng.random() > style.split_prob + min(bias, len(units) * 0.15):
        return [sep.join(units)]
    return units


def style_digest(style: StyleProfile) -> str:
    """一行摘要，用于调试面板/日志。"""
    habit = getattr(style, "bubble_habit", "steady")
    return (
        f"分条={_HABIT_LABEL.get(habit, '一小段')} "
        f"语气词{len(style.fillers)}个 口头禅{len(style.catchphrases)}个 "
        f"emoji={style.emoji_rate:.2f} 单条≈{style.sentence_len}字 最多{style.max_bubbles}条"
    )