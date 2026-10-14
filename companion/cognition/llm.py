"""LLM 网关（OpenAI 兼容，流式 + 结构化输出）。

设计要点（9.2 A）：

  * **双模型**：``chat_model`` 负责对话生成，``mind_model`` 负责"心理核算"（JSON）。
    Demo 允许两者同款，配置留好即可后期拆分。
  * **可降级**：未配置 ``api_key`` 时自动启用本地 **模拟大脑**（``MockBackend``），
    Demo 无需联网也能完整跑通"人味"链路（时序/风格/记忆/情绪都能被观察到）。
  * **接口干净**：真实后端与模拟后端对上层完全同构，上层只依赖 ``LLMGateway``。
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field

from ..engine.types import StyleProfile

ModelKind = Literal["chat", "mind"]


class LLMConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    chat_model: str = "gpt-4o-mini"
    mind_model: str = "gpt-4o-mini"
    temperature: float = 0.9
    max_tokens: int = 512
    timeout_s: float = 60.0

    @property
    def usable(self) -> bool:
        """是否配置了可用的远端/本地 OpenAI 兼容端点。"""
        key = (self.api_key or "").strip()
        return bool(key) and not key.lower().startswith(("sk-xxx", "your"))


# ----------------------------------------------------------------------
# 服务商预设（前端"一键填入"用；字段与 LLMConfig 对齐）
# ----------------------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek（官方）",
        "base_url": "https://api.deepseek.com/v1",
        "chat_model": "deepseek-v4-flash",
        "mind_model": "deepseek-v4-flash",
        # deepseek-v4-* 为推理模型（会先花"思考"token，慢且贵）；deepseek-chat 快而省
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"],
        # 推理模型会先消耗"思考"token，额度给小了正文会被挤空，故预设放宽
        "max_tokens": 1024,
    },
    "openai": {
        "label": "OpenAI 官方",
        "base_url": "https://api.openai.com/v1",
        "chat_model": "gpt-4o-mini",
        "mind_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "max_tokens": 512,
    },
    "ollama": {
        "label": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "chat_model": "qwen2.5:7b",
        "mind_model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "max_tokens": 512,
    },
}

# 前端可编辑的字段（单一事实来源，接入层据此做校验与回显）
EDITABLE_FIELDS = ("base_url", "api_key", "chat_model", "mind_model",
                   "temperature", "max_tokens", "timeout_s")


def mask_key(key: str) -> str:
    """脱敏展示 API Key：只留头 3 尾 4，其余打码。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return key[0] + "*" * (len(key) - 1)
    return f"{key[:3]}{'*' * 8}{key[-4:]}"


# ----------------------------------------------------------------------
# 后端接口
# ----------------------------------------------------------------------
class _Backend:
    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        task: str,
        mock_hint: dict[str, Any] | None,
    ) -> AsyncIterator[str]:  # pragma: no cover - 抽象
        raise NotImplementedError
        yield ""  # noqa: 让类型检查器知道这是 async generator

    async def json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        mock_hint: dict[str, Any] | None,
    ) -> dict[str, Any]:  # pragma: no cover - 抽象
        raise NotImplementedError


class OpenAIBackend(_Backend):
    """真实后端：任意 OpenAI 兼容端点（云端 / Ollama / vLLM …）。"""

    def __init__(self, config: LLMConfig) -> None:
        from openai import AsyncOpenAI

        kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_s,
            "max_retries": 1,
        }
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = AsyncOpenAI(**kwargs)

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        task: str,
        mock_hint: dict[str, Any] | None,
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece

    async def json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        mock_hint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.3,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"心理核算返回非法 JSON：{text[:120]!r}")
            return {}


# ----------------------------------------------------------------------
# 本地模拟大脑（无 key 时降级用；也用于确定性测试）
# ----------------------------------------------------------------------
class MockBackend(_Backend):
    """规则化"像人"回复器 —— 不联网也能验证时序 / 风格 / 记忆 / 情绪链路。

    它刻意**不输出 AI 腔**（无分点、无"作为一个AI"），并会按风格档掺入口语词，
    使"去机器味"这件事在没有真实模型时依然可见。
    """

    _QUESTION_HINT = ("?", "？", "吗", "呢", "什么", "为什么", "怎么", "哪", "谁", "多少")

    _INTENTS: dict[str, list[str]] = {
        "greeting": ["在呀", "嗯 在的", "刚在忙 你说", "嘿 怎么了"],
        "farewell": ["那我先忙咯", "去啦 晚点找你", "嗯嗯 你也早点歇", "好 待会儿聊"],
        "thanks": ["跟我还客气啥", "嘿嘿 不用谢", "嗯呐", "别这么见外嘛"],
        "negative": [
            "怎么啦 听着不太开心",
            "嗯… 抱抱你",
            "谁惹你了 跟我说说",
            "别自己憋着 我在呢",
        ],
        "positive": ["哈哈 看你这么开心我也高兴", "不错呀", "可以啊你", "嘿嘿 挺好"],
        "question": [
            "让我想想… 我觉得吧",
            "嗯 这个嘛",
            "唔 你猜猜看",
            "我还真有点想法",
        ],
        "default": [
            "嗯嗯 我听着呢",
            "说来听听",
            "哦哦 然后呢",
            "你这么一说 我也有点感觉",
            "哈 还真有你的",
        ],
    }

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    # ---- 对话生成 -----------------------------------------------------
    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        task: str,
        mock_hint: dict[str, Any] | None,
    ) -> AsyncIterator[str]:
        hint = mock_hint or {}
        await asyncio.sleep(0)  # 保持协作式调度
        text = self._compose(task=task, hint=hint)
        # 模拟流式：按小片段吐出，上层最终仍会整体收集后分条
        for piece in self._chunks(text):
            await asyncio.sleep(0)
            yield piece

    async def json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        mock_hint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        return (mock_hint or {}).get("fallback_json", {}) or {}

    # ---- 内部：组句 ---------------------------------------------------
    def _chunks(self, text: str) -> list[str]:
        """把整段文本切成 2~5 字的碎片，模拟 token 流。"""
        out: list[str] = []
        i = 0
        while i < len(text):
            n = self._rng.randint(1, 4)
            out.append(text[i : i + n])
            i += n
        return out or [""]

    def _pick(self, key: str) -> str:
        return self._rng.choice(self._INTENTS[key])

    def _detect_intent(self, text: str) -> str:
        t = text.strip()
        if not t:
            return "default"
        if any(k in t for k in ("再见", "拜拜", "先忙", "睡了", "晚安", "去吃饭")):
            return "farewell"
        if any(k in t for k in ("谢谢", "谢了", "感谢", "多谢")):
            return "thanks"
        if any(k in t for k in ("你好", "在吗", "在么", "嗨", "哈喽", "早上好", "中午好", "晚上好")):
            return "greeting"
        if any(k in t for k in ("难过", "想哭", "累", "烦", "生气", "讨厌", "不开心", "委屈", "崩溃")):
            return "negative"
        if any(k in t for k in ("开心", "高兴", "太好了", "哈哈", "爽", "棒", "喜欢")):
            return "positive"
        if any(k in t for k in self._QUESTION_HINT):
            return "question"
        return "default"

    def _style_of(self, hint: dict[str, Any]) -> StyleProfile:
        raw = hint.get("style")
        if isinstance(raw, StyleProfile):
            return raw
        if isinstance(raw, dict):
            try:
                return StyleProfile(**raw)
            except Exception:
                return StyleProfile()
        return StyleProfile()

    def _decorate(self, core: str, style: StyleProfile) -> str:
        """按风格档掺入口语词 / 口头禅 / 表情，制造"人味"。"""
        parts: list[str] = []
        if style.fillers and self._rng.random() < 0.55:
            parts.append(self._rng.choice(style.fillers))
        parts.append(core)
        if style.catchphrases and self._rng.random() < 0.30:
            parts.append(self._rng.choice(style.catchphrases))
        text = " ".join(p for p in parts if p)
        if style.emojis and self._rng.random() < style.emoji_rate:
            text += " " + self._rng.choice(style.emojis)
        return text

    def _compose(self, *, task: str, hint: dict[str, Any]) -> str:
        style = self._style_of(hint)
        name = hint.get("life_name") or "我"
        user_text = (hint.get("user_text") or "").strip()
        facts = hint.get("facts_text") or ""
        stage = hint.get("relation_stage") or "陌生"
        mood = hint.get("mood_label") or "平静"

        # --- 主动发起 ---
        if task == "proactive":
            # 情绪上头后的"事后闭环"：直接说那句后悔/找补的话（M14）
            closure = hint.get("closure")
            if closure:
                return closure
            kind = hint.get("proactive_kind") or "topic"
            bank = {
                "greeting": ["早呀 起来没", "这么晚还没睡呀", "在忙吗", "今天过得咋样"],
                "care": ["有点想你了 在干嘛呢", "好久没聊了 还好吗", "今天累不累呀"],
                "share": ["跟你说个事儿 我今儿遇到个挺逗的事", "刚看了个片子 还挺好看", "今天状态一般般"],
                "topic": ["上次说的那个事 后来怎么样了", "对了 你之前提的那件我还记着呢"],
            }
            core = self._rng.choice(bank.get(kind, bank["topic"]))
            return self._decorate(core, style)

        # --- 被叫醒 ---
        if task == "wake":
            bank = ["…嗯？", "唔…几点了", "……啊 怎么了这是", "谁呀…大晚上的"]
            core = self._rng.choice(bank)
            tail = self._rng.choice(["等会儿 我缓缓", "怎么了 出啥事了", "嗯 我醒了我醒了"])
            return core + " " + tail

        # --- 上头（越界）：去掉一切"礼貌"，更冲更短 ---
        if task == "override":
            bank = {
                "cold": ["随你便吧", "你高兴就好", "行 那就这样", "算了 不说了"],
                "angry": ["你过分了啊", "我真服了", "别说了行吗", "有点上火了"],
                "clingy": ["你是不是不在乎我了", "我就要你现在回我", "别走好不好"],
            }
            flavor = hint.get("override_flavor") or "angry"
            return self._rng.choice(bank.get(flavor, bank["angry"]))

        # --- 正常回复 ---
        intent = self._detect_intent(user_text)
        core = self._pick(intent)

        # 偶尔引用记忆，体现"记得你"
        if facts and self._rng.random() < 0.20:
            core = f"（{name}记得 {facts[:18]}…）{core}"

        # 关系越亲，话越软
        if stage == "亲密" and self._rng.random() < 0.35:
            core = self._rng.choice(["哎呀", "真是的", "你可算来了 "]) + core
        elif stage == "陌生":
            core = core.replace("嘿嘿", "").strip()

        # 情绪影响措辞（低落时省略号更多）
        if mood in ("低落", "烦躁") and self._rng.random() < 0.5:
            core += "……"

        # 提问型：偶尔回问，更像人
        if intent == "question" and self._rng.random() < 0.3:
            core += " 你觉得呢"

        return self._decorate(core, style)


# ----------------------------------------------------------------------
# 网关
# ----------------------------------------------------------------------
class LLMGateway:
    """统一入口：按配置择后端，向上提供 ``stream`` / ``json`` 两个能力。"""

    def __init__(self, config: LLMConfig, *, seed: int | None = None) -> None:
        self._config = config
        self._seed = seed
        self._backend, self.mode = self._make_backend(config)

    def _make_backend(self, config: LLMConfig) -> tuple[_Backend, str]:
        """按配置择后端；远端不可用时降级为本地模拟大脑。"""
        if config.usable:
            try:
                backend = OpenAIBackend(config)
                logger.info(
                    f"LLM 就绪（远端）：base_url={config.base_url or '默认'} "
                    f"chat={config.chat_model} mind={config.mind_model}"
                )
                return backend, "openai"
            except Exception:
                logger.exception("初始化 OpenAI 客户端失败，降级为本地模拟大脑")
        else:
            logger.warning("未配置 LLM api_key → 启用本地模拟大脑（Demo 可离线跑通全链路）")
        return MockBackend(self._seed), "mock"

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"

    def apply_config(self, config: LLMConfig) -> None:
        """运行时热更新 LLM 配置（网页里改完立即生效，无需重启）。"""
        self._config = config
        self._backend, self.mode = self._make_backend(config)
        logger.info(
            f"LLM 配置已热更新 → mode={self.mode} base_url={config.base_url or '默认'} "
            f"chat={config.chat_model} mind={config.mind_model}"
        )

    def describe(self) -> dict[str, Any]:
        """给前端的可回显配置（api_key 脱敏，绝不外泄明文）。"""
        c = self._config
        return {
            "mode": self.mode,
            "usable": c.usable,
            "base_url": c.base_url,
            "chat_model": c.chat_model,
            "mind_model": c.mind_model,
            "temperature": c.temperature,
            "max_tokens": c.max_tokens,
            "timeout_s": c.timeout_s,
            "has_key": bool((c.api_key or "").strip()),
            "api_key_masked": mask_key(c.api_key),
            "presets": PRESETS,
        }

    def _model(self, kind: ModelKind) -> str:
        return self._config.chat_model if kind == "chat" else self._config.mind_model

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "reply",
        kind: ModelKind = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
        mock_hint: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """流式生成。调用方负责拼接完整文本。"""
        async for piece in self._backend.stream(
            messages,
            model=self._model(kind),
            temperature=self._config.temperature if temperature is None else temperature,
            max_tokens=self._config.max_tokens if max_tokens is None else max_tokens,
            task=task,
            mock_hint=mock_hint,
        ):
            yield piece

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "reply",
        kind: ModelKind = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
        mock_hint: dict[str, Any] | None = None,
    ) -> str:
        """非流式便捷封装：收集完再返回。"""
        buf: list[str] = []
        async for piece in self.stream(
            messages,
            task=task,
            kind=kind,
            temperature=temperature,
            max_tokens=max_tokens,
            mock_hint=mock_hint,
        ):
            buf.append(piece)
        return "".join(buf).strip()

    async def json(
        self,
        messages: list[dict[str, str]],
        *,
        mock_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """心理核算专用：强制 JSON 输出（小模型）。"""
        try:
            return await self._backend.json(
                messages, model=self._model("mind"), mock_hint=mock_hint
            )
        except Exception:
            logger.exception("心理核算调用失败，跳过本轮核算")
            return {}

    async def probe(self) -> tuple[bool, str]:
        """最小连通性自检：发一条极短请求，返回 (是否成功, 提示文案)。

        注意推理模型会先消耗"思考"token，故这里不看正文、只看到底通不通。
        """
        if self.is_mock:
            return False, "当前是本地模拟大脑（未配置可用的 api_key）"
        try:
            await self.complete(
                [{"role": "user", "content": "ping"}], max_tokens=8, temperature=0.0
            )
            return True, "连接成功"
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:180]}"