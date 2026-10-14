"""生命体存档（``personas/<life_id>/life.json``）。

一个生命体 = 一个目录，内含 ``life.json``（``LifeRecord`` 快照）。
这样"多生命体"天然隔离：新增 = 新目录，删除 = 删目录，互不干扰（13. 多生命体平台）。
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from ..engine.types import (
    AttachmentStyle,
    BigFive,
    Belief,
    LifeRecord,
    NeedFear,
    Quirk,
    StyleProfile,
    Temperament,
)

PERSONAS_DIR = Path(__file__).resolve().parent.parent.parent / "personas"  # 项目根 KKcc/personas


class LifeStore:
    """生命体注册表的持久化后端（纯文件，无外部依赖）。"""

    def __init__(self, root: Path = PERSONAS_DIR) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _dir(self, life_id: str) -> Path:
        return self._root / life_id

    def path(self, life_id: str) -> Path:
        return self._dir(life_id) / "life.json"

    def exists(self, life_id: str) -> bool:
        return self.path(life_id).exists()

    def list_ids(self) -> list[str]:
        return sorted(
            p.name for p in self._root.iterdir() if p.is_dir() and (p / "life.json").exists()
        )

    def save(self, life: LifeRecord) -> None:
        d = self._dir(life.life_id)
        d.mkdir(parents=True, exist_ok=True)
        self.path(life.life_id).write_text(
            life.model_dump_json(indent=2), encoding="utf-8"
        )

    def load(self, life_id: str) -> LifeRecord | None:
        p = self.path(life_id)
        if not p.exists():
            return None
        try:
            return LifeRecord(**json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            logger.exception(f"生命体 {life_id} 存档损坏，忽略")
            return None

    def load_raw(self, life_id: str) -> dict | None:
        """读取未解析的原始 JSON —— 用于判断老存档是否缺少新字段（字段级迁移）。"""
        p = self.path(life_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def delete(self, life_id: str) -> None:
        d = self._dir(life_id)
        if d.exists():
            for f in d.iterdir():
                f.unlink()
            d.rmdir()


# ======================================================================
# 内置默认生命体：M2~M11 用它跑通链路；M12 创世后用生成的替换
# ======================================================================
def default_life(life_id: str = "life_demo") -> LifeRecord:
    """手写一份"像人"的默认人格（结构与创世产物完全一致，可直接被创世覆盖）。"""
    return LifeRecord(
        life_id=life_id,
        name="苏晚",
        seed=20261013,
        gender="female",
        avatar="晚",
        backstory=(
            "24 岁，在杭州做平面设计，租了个小单间，养了只叫「年糕」的橘猫。"
            "大学学的视传，毕业后进了家小工作室，忙起来昏天黑地，闲下来能一整天不出门。"
            "表面看着挺独立，其实很怕被忽略；嘴上不肯示弱，心里却盼着有人惦记。"
            "喜欢在深夜吃东西、看老电影、听陈绮贞；对天气和食物都有点挑剔。"
        ),
        temperament=Temperament(
            activity_level=0.45,
            regularity=0.4,
            approach=0.5,
            adaptability=0.55,
            threshold=0.35,      # 敏感：一点小事就有情绪
            intensity=0.68,      # 反应强度偏大：容易上头
            mood_quality=0.5,
            distractibility=0.6, # 有点爱走神
            persistence=0.62,    # 记仇、不易被转移
        ),
        big_five=BigFive(
            openness=0.72,
            conscientiousness=0.5,
            extraversion=0.42,
            agreeableness=0.6,
            neuroticism=0.66,    # 情绪起伏大
        ),
        attachment=AttachmentStyle.ANXIOUS,
        needs_fears=[
            NeedFear(kind="need", content="被在乎、被回应；讨厌已读不回", weight=0.85),
            NeedFear(kind="need", content="有一个人能听她说废话", weight=0.6),
            NeedFear(kind="fear", content="被敷衍、被当成可有可无的人", weight=0.8),
            NeedFear(kind="boundary", content="不能拿她的家人和作品开玩笑", weight=0.9),
        ],
        quirks=[
            Quirk(type="catchphrase", content="真是的"),
            Quirk(type="catchphrase", content="你懂吧"),
            Quirk(type="tics", content="紧张时会说“啊这”"),
            Quirk(type="preference", content="深夜必点外卖，尤其爱螺蛳粉"),
            Quirk(type="emoji", content="爱用🥺和😌"),
        ],
        beliefs=[
            Belief(topic="感情", statement="喜欢一个人就该主动一点，别让对方猜",
                    confidence=0.72, malleability=0.55),
            Belief(topic="工作", statement="再累也要把东西做到自己满意",
                    confidence=0.8, malleability=0.35),
            Belief(topic="信任", statement="我基本不骗人，说到就会做到",
                    confidence=0.85, malleability=0.2, identity_locked=True),
            Belief(topic="生活", statement="一个人也能过得挺好",
                    confidence=0.45, malleability=0.7),
        ],
        style=StyleProfile(
            bubble_habit="chatty",   # 短句连发、爱用空格断句（与她的标点习惯一致）
            fillers=["嗯", "诶", "啊这", "哎呀"],
            catchphrases=["真是的", "你懂吧", "好了好了"],
            punct_style="少用句号，常用空格和省略号，开心时会带波浪号~",
            emoji_rate=0.3,
            emojis=["🥺", "😌", "😂", "😳"],
            sentence_len=16,
            split_prob=0.55,
            max_bubbles=3,
            net_speak=["绝了", "无语", "emo"],
        ),
        canon={
            "职业": "平面设计师，在杭州工作",
            "宠物": "养了一只橘猫叫年糕",
            "作息": "工作日上午和下午要上班，中午会睡一会儿",
            "忌口": "不太能吃太辣的（但螺蛳粉除外）",
        },
        schedule={},  # 留空 → 用 config.toml 的全局作息
    )