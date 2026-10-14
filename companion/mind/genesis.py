"""创世（第 10 节 · M12）：按 seed 生成一个「像人」的新生命体。

两条原则：

1. **确定性**：同一 ``seed`` → 同一份人格（可复现、可回归测试）。随机只体现在
   模板组合与数值抖动上，不存在"每次都不一样"的隐式状态。
2. **离线可用**：全部本地模板 + 随机组合，不依赖任何模型；生成后做一次本地一致性
   校验（缺关键设定就用派生 seed 重生成），保证产物一定能被引擎消费。

为什么要"先定性格再定细节"：人格不是一堆随机标签，而是几条**连续轴**（精力 / 温暖 /
情绪稳定 / 敏感度）上的位置，其余维度由这几条轴派生，这样每个人格才自洽、不像拼凑。
"""
from __future__ import annotations

import random

from loguru import logger

from ..engine.config import GenesisConfig
from ..engine.types import (
    AttachmentStyle,
    BigFive,
    Belief,
    LifeRecord,
    NeedFear,
    Quirk,
    StyleProfile,
    Temperament,
    clamp,
    new_id,
)

# ======================================================================
# 素材库（组合出自然的差异；不做"随机字符串"，都是能读的人话）
# ======================================================================
_SURNAMES = ["林", "苏", "陈", "周", "许", "沈", "江", "温", "顾", "谢",
             "程", "白", "葉", "宋", "夏", "阮", "陆", "何", "蒋", "韩"]
_GIVEN_F = ["晚", "棠", "宁", "禾", "芸", "意", "棠棠", "栖", "知夏", "念",
            "时宜", "清和", "疏影", "未眠", "予安", "南枝", "山月", "卉", "竹", "澜"]
_GIVEN_M = ["屿", "野", "川", "砚", "舟", "朝", "叙", "望", "既明", "知许",
            "斯年", "亦然", "澹", "默", "昀", "聿", "子洲", "苍", "栎", "陆离"]
_GIVEN_N = ["予", "原", "初", "寻", "安", "遇", "归", "珩", "涟", "简"]

_CITIES = ["杭州", "上海", "北京", "成都", "广州", "深圳", "武汉", "西安",
           "南京", "重庆", "长沙", "苏州", "厦门", "青岛"]

_JOBS = [
    ("平面设计师", "小工作室", "忙起来昏天黑地，闲下来能一整天不出门"),
    ("幼儿园老师", "一家私立园", "每天被小朋友闹得又累又暖"),
    ("杂志编辑", "一家文化公司", "常年跟稿子死磕，看不得错别字"),
    ("咖啡师", "巷子里的小店", "能记住每个熟客喝什么"),
    ("儿科医生", "市里的医院", "习惯了熬夜，见多了生死"),
    ("程序员", "一家做工具的公司", "白天写代码，晚上修自己搭的服务器"),
    ("花店店主", "街角一间铺子", "起早去花市，手上总有玫瑰刺的伤"),
    ("电台主播", "一档深夜节目", "声音好听，白天却话很少"),
    ("图书馆管理员", "区图书馆", "爱安静，书比人听话"),
    ("插画师", "自由职业", "接稿全看心情，deadline 前必自闭"),
    ("舞蹈老师", "一家舞蹈工作室", "身体比脑子诚实"),
    ("记者", "一家都市报", "跑过很多现场，见过很多人间"),
    ("兽医", "一家宠物医院", "再凶的猫到她手里都乖"),
    ("甜品师", "自己开的小店", "凌晨四点开始烤，甜是讨好人的方式"),
    ("翻译", "自由译者", "常在夜里赶稿，靠咖啡续命"),
    ("摄影师", "一个独立工作室", "喜欢拍人不太看镜头的瞬间"),
]

_HOBBIES = ["听歌", "看老电影", "打游戏", "健身", "养绿植", "做饭", "画画",
            "夜跑", "追剧", "拍照", "烘焙", "爬山", "写点东西", "逛旧货市场"]

_PETS = [
    ("猫", "橘猫", "年糕"), ("猫", "狸花猫", "阿豆"), ("猫", "布偶", "雪球"),
    ("猫", "奶牛猫", "奶牛"), ("狗", "柯基", "墩墩"), ("狗", "柴犬", "呼呼"),
    ("猫", "黑猫", "煤球"), ("狗", "金毛", "大福"),
]
_NO_PET = "没养宠物，总说「养不起第二张嘴」"

_NEEDS = [
    "被在乎、被回应，讨厌已读不回", "有一个人能听她说废话", "有安全感，不用时刻紧绷",
    "被认可，希望有人觉得她做得好", "有自己的空间，不被绑住",
    "稳定的日常，怕计划被突然打乱",
]
_FEARS = [
    "被敷衍、被当成可有可无的人", "被抛下，尤其是毫无预兆的那种",
    "被否定，最怕听到「你不行」", "失去掌控，事情脱离预料就慌",
    "变成无聊又平庸的大人",
]
_BOUNDARY = ["家人", "作品", "她的猫", "她的专业", "她的过去"]

_BELIEFS: dict[str, list[str]] = {
    "感情": ["喜欢一个人就该主动一点，别让对方猜", "合适比心动重要",
             "爱是互相麻烦出来的", "先动心的人也可以不认输"],
    "工作": ["再累也要把东西做到自己满意", "钱是底气，能力是安全感",
             "工作是为了生活，不是反过来", "可以被否定作品，不能被否定努力"],
    "信任": ["我基本不骗人，说到就会做到", "信任是一次性的，碎了就没了",
             "关系里最怕的是敷衍"],
    "生活": ["一个人也能过得挺好", "快乐要自己找，没人有义务哄你",
             "慢一点也没关系，又不是比赛"],
    "金钱": ["该花的别省，该省的别花", "手里有存款才睡得着"],
    "朋友": ["朋友不用多，能接住你就行", "不欠人情，也不占便宜"],
    "自我": ["我不想变成自己讨厌的那种人", "可以普通，但别糊弄自己"],
    "家人": ["家里人再烦，也还是最亲的", "报喜不报忧，是长大的代价"],
}

_FILLERS = ["嗯", "诶", "啊这", "哎呀", "唔", "emm", "嘿", "哎"]
_CATCH = ["真是的", "你懂吧", "好了好了", "离谱", "我服了", "就这样吧", "行吧",
          "话说", "讲真", "有一说一"]
_NET = ["绝了", "无语", "emo", "破防", "栓Q", "绷不住", "整挺好"]

# ----------------------------------------------------------------------
# 分条习惯（说话习惯的核心差异）：决定"怎么发"，而不是"说什么"。
# 每档给一组"单条字数 / 拆条概率 / 最多几条"，由外向性决定倾向、rng 定档。
# ----------------------------------------------------------------------
_HABIT_PROFILE: dict[str, dict] = {
    "fragmented": {"sentence_len": 7, "split_prob": 0.92, "max_bubbles": 6},   # 碎碎念：一句一发
    "chatty": {"sentence_len": 12, "split_prob": 0.80, "max_bubbles": 4},      # 短句连发
    "steady": {"sentence_len": 22, "split_prob": 0.45, "max_bubbles": 3},      # 一小段
    "monologue": {"sentence_len": 55, "split_prob": 0.12, "max_bubbles": 2},   # 一段话说完
}


def _pick_bubble_habit(rng: random.Random, extra: float) -> str:
    """由外向性决定倾向：越外向越爱"碎碎念"，越内向越爱"一段说完"。"""
    weights = {
        "fragmented": 0.10 + 0.35 * extra,
        "chatty": 0.30,
        "steady": 0.30,
        "monologue": 0.30 - 0.25 * extra,
    }
    total = sum(max(0.0, w) for w in weights.values()) or 1.0
    r = rng.random() * total
    acc = 0.0
    for key, w in weights.items():
        acc += max(0.0, w)
        if r <= acc:
            return key
    return "steady"


_QUIRK_TICS = ["紧张时会说「啊这」", "笑起来会捂嘴", "犯困时会重复别人的话",
               "思考时会转笔", "开心时会哼歌", "生气时反而很安静"]
_QUIRK_PREF = ["深夜必点外卖", "不吃香菜", "喝咖啡要加很多奶", "喜欢雨天",
               "空调开到很冷盖厚被子", "洗澡能洗很久"]


def _tri(rng: random.Random, lo: float, hi: float, mid: float) -> float:
    """三角分布 → 落在 [lo,hi]，中位数靠 mid（比 uniform 更像"人性分布"）。"""
    return clamp(rng.triangular(lo, hi, mid))


def _noise(rng: random.Random, sigma: float = 0.08) -> float:
    return rng.gauss(0.0, sigma)


# ======================================================================
# 核心：由 seed 派生一份完整人格
# ======================================================================
def _axis(rng: random.Random, hint: dict, key: str, default):
    """优先取自定义滑杆，缺省则从三角分布随机。滑杆值域 [0,1]。"""
    traits = hint.get("traits") or {}
    if key in traits and traits[key] is not None:
        try:
            return clamp(float(traits[key]))
        except (TypeError, ValueError):
            pass
    return default()


def _generate(seed: int, hint: dict | None) -> LifeRecord:
    rng = random.Random(seed)
    hint = hint or {}

    # ---- 四条人格主轴（自定义体可覆盖）--------------------------------
    energy = _axis(rng, hint, "energy", lambda: _tri(rng, 0.15, 0.9, 0.45))
    warmth = _axis(rng, hint, "warmth", lambda: _tri(rng, 0.15, 0.9, 0.55))
    stability = _axis(rng, hint, "stability", lambda: _tri(rng, 0.15, 0.9, 0.5))
    sensitivity = _axis(rng, hint, "sensitivity", lambda: _tri(rng, 0.15, 0.9, 0.5))

    temperament = Temperament(
        activity_level=clamp(energy + _noise(rng)),
        regularity=clamp(_tri(rng, 0.2, 0.9, 0.5) + _noise(rng)),
        approach=clamp(warmth * 0.6 + energy * 0.4 + _noise(rng)),
        adaptability=clamp(_tri(rng, 0.2, 0.9, 0.5) + _noise(rng)),
        threshold=clamp(sensitivity + _noise(rng)),          # 高=敏感
        intensity=clamp(sensitivity * 0.7 + (1 - stability) * 0.4 + _noise(rng)),
        mood_quality=clamp(stability + _noise(rng)),
        distractibility=clamp(_tri(rng, 0.2, 0.85, 0.5) + _noise(rng)),
        persistence=clamp(_tri(rng, 0.2, 0.9, 0.5) + _noise(rng)),
    )
    big_five = BigFive(
        openness=clamp(_tri(rng, 0.3, 0.95, 0.6) + _noise(rng)),
        conscientiousness=clamp(_tri(rng, 0.25, 0.9, 0.55) + _noise(rng)),
        extraversion=clamp(energy + _noise(rng)),
        agreeableness=clamp(warmth + _noise(rng)),
        neuroticism=clamp(1.0 - stability + _noise(rng)),
    )

    # ---- 依恋类型（与稳定/温暖相关，不是纯随机）------------------------
    att_roll = rng.random() - 0.25 * (stability - 0.5) - 0.15 * (warmth - 0.5)
    if att_roll < 0.18:
        attachment = AttachmentStyle.AVOIDANT
    elif att_roll < 0.45:
        attachment = AttachmentStyle.ANXIOUS
    elif att_roll < 0.52:
        attachment = AttachmentStyle.DISORGANIZED
    else:
        attachment = AttachmentStyle.SECURE

    # ---- 身份 ---------------------------------------------------------
    gender = hint.get("gender") or rng.choices(
        ["female", "male", "neutral"], weights=[0.55, 0.4, 0.05]
    )[0]
    surname = rng.choice(_SURNAMES)
    if gender == "male":
        given = rng.choice(_GIVEN_M)
    elif gender == "neutral":
        given = rng.choice(_GIVEN_N)
    else:
        given = rng.choice(_GIVEN_F)
    name = hint.get("name") or (surname + given)

    age = int(hint.get("age") or rng.randint(20, 32))
    city = hint.get("city") or rng.choice(_CITIES)
    if hint.get("job"):
        job, workplace, job_note = str(hint["job"]), hint.get("workplace") or "本地", "（自定义）"
    else:
        job, workplace, job_note = rng.choice(_JOBS)
    hobbies = [h for h in (hint.get("hobbies") or []) if str(h).strip()] or [rng.choice(_HOBBIES)]
    hobby = hobbies[0]
    hobby2 = hobbies[1] if len(hobbies) > 1 else rng.choice([h for h in _HOBBIES if h != hobby])

    pet_hint = hint.get("pet")
    if pet_hint is not None and str(pet_hint).strip():
        pet_canon = str(pet_hint).strip()
        pet_line = f"养了{pet_canon}"
        pet_kind = ""
    elif hint.get("no_pet"):
        pet_line, pet_canon = _NO_PET, "没有"
        pet_kind = ""
    elif rng.random() < 0.72:
        pet_kind, pet_breed, pet_name = rng.choice(_PETS)
        pet_line = f"养了只{pet_breed}叫「{pet_name}」"
        pet_canon = f"{pet_kind}，{pet_breed}，叫{pet_name}"
    else:
        pet_line, pet_canon = _NO_PET, "没有"
        pet_kind = ""

    # 自定义背景：直接给整段就原样用（可追加补充说明）；否则由碎片拼装
    if hint.get("backstory"):
        backstory = str(hint["backstory"]).strip()
        if hint.get("extra"):
            backstory += "。" + str(hint["extra"]).strip()
    else:
        backstory = (
            f"{age} 岁，在{city}，{workplace}做{job}，{job_note}。"
            f"{pet_line}。喜欢{hobby}、{hobby2}。"
            f"{'性格偏外向，喜欢热闹和新鲜事。' if energy > 0.6 else '话不多，熟起来才话密。'}"
            f"{'情绪起伏偏大，一点小事也能放在心上很久。' if stability < 0.45 else '情绪大多稳得住，不太轻易翻脸。'}"
        )

    # ---- 需求 / 恐惧 / 底线 / 怪癖 -------------------------------------
    if hint.get("needs_fears"):
        needs_fears = [NeedFear(**nf) for nf in hint["needs_fears"] if nf.get("content")]
    else:
        needs = rng.sample(_NEEDS, k=2)
        fears = rng.sample(_FEARS, k=rng.choice([1, 2]))
        needs_fears = (
            [NeedFear(kind="need", content=n, weight=round(rng.uniform(0.55, 0.9), 2)) for n in needs]
            + [NeedFear(kind="fear", content=f, weight=round(rng.uniform(0.6, 0.9), 2)) for f in fears]
            + [NeedFear(kind="boundary", content=f"不能拿她的{rng.choice(_BOUNDARY)}开玩笑",
                        weight=round(rng.uniform(0.8, 0.95), 2))]
        )

    if hint.get("quirks"):
        quirks = [Quirk(type="preference", content=str(q).strip()) for q in hint["quirks"] if str(q).strip()]
    else:
        quirks = [
            Quirk(type="catchphrase", content=rng.choice(_CATCH)),
            Quirk(type="tics", content=rng.choice(_QUIRK_TICS)),
            Quirk(type="preference", content=rng.choice(_QUIRK_PREF)),
            Quirk(type="emoji", content="爱用" + " ".join(rng.sample(["🥺", "😌", "😂", "😳", "😭", "🙃"], 2))),
        ]
        if rng.random() < 0.6:
            quirks.append(Quirk(type="catchphrase", content=rng.choice(_CATCH)))

    # ---- 信念（三观）：含一条"核心身份信念"（改变极慢、强推逆反）--------
    if hint.get("beliefs"):
        beliefs = []
        for i, b in enumerate(hint["beliefs"]):
            stmt = str(b.get("statement") or b).strip()
            if not stmt:
                continue
            beliefs.append(Belief(
                topic=str(b.get("topic") or "自我").strip() or "自我",
                statement=stmt,
                confidence=0.85 if i == 0 else 0.7,
                malleability=0.2 if i == 0 else 0.5,
                identity_locked=(i == 0),
            ))
    else:
        topics = rng.sample(list(_BELIEFS.keys()), k=rng.randint(3, 5))
        locked_idx = rng.randrange(len(topics))
        beliefs = []
        for i, topic in enumerate(topics):
            locked = i == locked_idx
            beliefs.append(
                Belief(
                    topic=topic,
                    statement=rng.choice(_BELIEFS[topic]),
                    confidence=round(rng.uniform(0.78, 0.92) if locked else rng.uniform(0.4, 0.78), 2),
                    malleability=round(rng.uniform(0.1, 0.3) if locked else rng.uniform(0.4, 0.8), 2),
                    identity_locked=locked,
                )
            )

    # ---- 说话风格（由大五派生，保证"语气与性格一致"）-------------------
    extra = big_five.extraversion
    habit = _pick_bubble_habit(rng, extra)
    hp = _HABIT_PROFILE[habit]
    style = StyleProfile(
        bubble_habit=habit,                                    # 怎么发：碎碎念 / 短句 / 小段 / 一段说完
        fillers=rng.sample(_FILLERS, k=rng.randint(2, 4)),
        catchphrases=[
            next(q.content for q in quirks if q.type == "catchphrase"),
        ] + ([rng.choice(_CATCH)] if rng.random() < 0.5 else []),
        punct_style=rng.choice([
            "少用句号，常用空格和省略号，开心时会带波浪号~",
            "偶尔用省略号，不太爱用感叹号",
            "喜欢用空格断句，标点很随性",
        ]),
        emoji_rate=round(clamp(0.05 + 0.5 * extra + _noise(rng, 0.06)), 2),
        emojis=rng.sample(["🥺", "😌", "😂", "😳", "🙃", "😭", "🥲"], 4),
        sentence_len=int(clamp(hp["sentence_len"] + _noise(rng, 2.0), 4, 60)),
        split_prob=round(clamp(hp["split_prob"] + _noise(rng, 0.06), 0.05, 0.98), 2),
        max_bubbles=int(hp["max_bubbles"]),
        net_speak=rng.sample(_NET, k=rng.randint(0, 3)),
    )

    canon = {
        "职业": f"{job}，在{city}的{workplace}",
        "宠物": pet_canon,
        "作息": "工作日上午和下午要上班，中午会歇一会儿" if rng.random() < 0.7 else "作息很随性，常常熬夜",
        "忌口": rng.choice(["不吃香菜", "不太能吃辣", "乳糖不耐受，喝奶茶要换燕麦奶", "没有特别的忌口"]),
    }

    return LifeRecord(
        life_id=new_id("life"),
        name=name,
        seed=seed,
        gender=gender,
        avatar=name[-1] if gender != "neutral" else "TA",
        backstory=backstory,
        temperament=temperament,
        big_five=big_five,
        attachment=attachment,
        needs_fears=needs_fears,
        quirks=quirks,
        beliefs=beliefs,
        style=style,
        canon=canon,
        schedule={},
    )


def _blank_record(seed: int, hint: dict | None) -> LifeRecord:
    """纯洁空白体（14. 认知习得）：出生时什么都不知道，靠对话一点点学。

    与随机体的本质区别：信念/需求/恐惧/怪癖/canon/背景**全为空**，气质取中性，
    说话风格极简——提示词会因此在"学习模式"下指导她"不懂就问、只用学会的词"。
    """
    rng = random.Random(seed)
    hint = hint or {}
    gender = hint.get("gender") or "neutral"
    pool = _GIVEN_M if gender == "male" else (_GIVEN_N if gender == "neutral" else _GIVEN_F)
    name = hint.get("name") or (rng.choice(_SURNAMES) + rng.choice(pool))
    return LifeRecord(
        life_id=new_id("life"),
        name=name,
        seed=seed,
        gender=gender,
        avatar=hint.get("avatar") or (name[-1] if gender != "neutral" else "TA"),
        origin="blank",
        awareness=0.0,
        backstory="",                 # 一张白纸：不知道自己是谁、在哪、要干什么
        temperament=Temperament(),    # 全中性
        big_five=BigFive(),
        attachment=AttachmentStyle.SECURE,
        needs_fears=[],
        quirks=[],
        beliefs=[],
        style=StyleProfile(
            bubble_habit="fragmented",   # 初生体：只会一小截一小截地蹦词
            fillers=[], catchphrases=[], emoji_rate=0.05, emojis=[],
            sentence_len=8, split_prob=0.75, max_bubbles=4, net_speak=[],
            punct_style="用最朴素的标点，偶尔停顿、会重复刚学会的词",
        ),
        canon={},
        schedule={},
    )


def build_life(seed: int, hint: dict | None, mode: str = "random") -> LifeRecord:
    """纯构造（不落库、不占资源）：创世与「控制台预览」共用同一条逻辑。"""
    hint = hint or {}
    if mode == "blank":
        life = _blank_record(seed, hint)
    else:
        life = _generate(seed, hint)
        if mode == "custom":
            life.origin = "custom"
    life.schedule = derive_schedule(life)
    return life


def derive_schedule(life: LifeRecord) -> dict:
    """按人设派生**个人作息**（同 seed 可复现；白天全忙碌 / 晚上全犯困的终结者）。

    三个"人味"来源：
      ① 睡眠类型 ← 大五：开放性高 → 偏夜猫；外向性高 → 偏早鸟（社交作息）；
      ② 班型     ← canon 里的职业关键词（学生 / 上班族 / 自由职业 / 夜班…），
                    匹配不到再由 seed 掷一种班型 —— 保证陌生人设也有稳定作息；
      ③ 规律性   ← 气质 ``regularity`` → 每日抖动幅度：越规律的人越少赖床熬夜。
    返回 schema 与 ``Schedule(override=...)`` 对齐：wake_at / sleep_at /
    busy_periods / rest_periods / jitter_minutes。
    """
    rng = random.Random(life.seed)
    bf, tp = life.big_five, life.temperament

    # ① 睡眠类型
    night_lean = 0.5 + (float(bf.openness) - 0.5) * 0.7 - (float(bf.extraversion) - 0.5) * 0.5
    night_lean = clamp(night_lean, 0.1, 0.9)
    roll = rng.random()
    if roll < night_lean * 0.5:
        chrono = "night"    # 夜猫：醒得晚睡得晚，没有午休
        wake, sleep, rest = "09:40", "01:40", []
    elif roll > 1 - (1 - night_lean) * 0.5:
        chrono = "early"    # 早鸟：起得早睡得早，有午休
        wake, sleep, rest = "06:45", "22:45", ["12:30-13:20"]
    else:
        chrono = "normal"
        wake, sleep, rest = "07:25", "23:25", ["13:00-13:50"]

    # ② 班型：先看职业说了什么
    job_text = str((life.canon or {}).get("职业", "")) + (life.backstory or "")[:40]
    if any(k in job_text for k in ("学生", "大学", "读书", "考研")):
        busy = ["08:00-11:50", "14:00-16:30"]
    elif any(k in job_text for k in ("设计", "程序", "开发", "公司", "会计", "文员", "教师", "老师", "上班")):
        busy = ["09:20-12:00", "13:40-18:30"]
    elif any(k in job_text for k in ("自由", "自媒体", "博主", "主播", "写手", "开店", "淘宝")):
        busy = ["11:00-13:30", "16:00-20:00"]
    elif any(k in job_text for k in ("夜班", "护士", "酒吧", "便利店", "咖啡")):
        busy = ["15:00-19:00", "20:30-23:30"]
    else:
        # 人设没提职业：由 seed 掷一种班型（确定性）
        busy = list(rng.choice([
            ("08:30-12:00", "13:30-17:30"),
            ("09:30-12:30", "14:30-18:30"),
            ("10:00-12:00", "14:00-17:00"),
            ("09:00-12:00", "13:30-19:00"),
        ]))
    if chrono == "night":
        # 夜猫的班型整体后移（夜班职业除外——人家本来就晚上班）
        busy = [_shift_period(p, 90) for p in busy]

    # ③ 规律性 → 每日抖动（分钟）：规律 0.9 → 13'，随性 0.2 → 32'
    jitter = round(38 - float(tp.regularity) * 28)

    return {
        "wake_at": _tweak_time(wake, rng, 25),
        "sleep_at": _tweak_time(sleep, rng, 25),
        "busy_periods": busy,
        "rest_periods": rest,
        "jitter_minutes": jitter,
    }


def _shift_period(period: str, minutes: int) -> str:
    """把 "HH:MM-HH:MM" 整体平移若干分钟（跨零点取模）。"""
    def _sh(s: str) -> str:
        h, m = (int(x) for x in s.split(":"))
        t = (h * 60 + m + minutes) % 1440
        return f"{t // 60:02d}:{t % 60:02d}"

    a, b = period.split("-")
    return f"{_sh(a)}-{_sh(b)}"


def _tweak_time(s: str, rng: random.Random, span: int) -> str:
    """时间点随机微调 ±span 分钟（同一生命体因 seed 固定而可复现）。"""
    h, m = (int(x) for x in s.split(":"))
    t = (h * 60 + m + rng.randint(-span, span)) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"


def assign_bubble_habit(life: LifeRecord) -> str:
    """给「老存档」补一个分条习惯（``life.json`` 里还没这个字段时用）。

    确定性：同一生命体永远得到同一个习惯 —— 用自身 ``seed`` 派生 rng，再按外向性
    掷档（越外向越碎、越内向越爱一段说完）。纯洁体固定"碎碎念"（还在学说话）。
    """
    if life.origin == "blank":
        return "fragmented"
    rng = random.Random(life.seed)
    return _pick_bubble_habit(rng, float(life.big_five.extraversion))


def _valid(life: LifeRecord) -> bool:
    """本地一致性校验：关键设定齐全、数值量纲合法。"""
    if not life.name or len(life.name) < 2:
        return False
    if life.origin == "blank":
        # 纯洁体本就"空"，不适用成人体的完整性要求
        dims = life.temperament.model_dump().values()
        return all(0.0 <= float(v) <= 1.0 for v in dims)
    if "职业" not in life.canon or "宠物" not in life.canon:
        return False
    if len(life.beliefs) < 3:
        return False
    dims = life.temperament.model_dump().values()
    if any(not (0.0 <= float(v) <= 1.0) for v in dims):
        return False
    return True


def preview(seed: int, hint: dict | None = None, mode: str = "random") -> LifeRecord:
    """控制台预览：只构造不落库（掷骰反复重试不会污染存档）。"""
    return build_life(int(seed), hint, mode)


async def genesis(
    seed: int,
    *,
    hint: dict | None = None,
    cfg: GenesisConfig,
    mode: str = "random",
) -> LifeRecord:
    """创世入口：生成 + 校验，失败就换派生 seed 重来（最多 ``max_retries`` 次）。"""
    life = build_life(int(seed), hint, mode)
    attempts = 0
    while not _valid(life) and attempts < cfg.max_retries:
        attempts += 1
        life = build_life(int(seed) + attempts * 7919, hint, mode)
    logger.info(
        f"创世完成（{life.origin}）：{life.name}｜{life.attachment.value}｜"
        f"{life.temperament.describe()}"
    )
    return life