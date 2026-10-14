"""装配入口：把各层拼成一台"活着的"服务并常驻。

分层装配（依赖只向下，绝不反向）：

    store  数据库 / 生命体存档 / 状态仓库
      ↑
    mind   心理内核（情绪 / 关系 / 信念 / 上头 / 创世）
      ↑
    cognition  认知层（LLM 网关 / 人设 / 记忆 / 召回 / 回复器）
      ↑
    engine 引擎层（Clock / Bus / 状态机 / 生命状态 / 收件箱 / 唤醒 / 时序 / 表达 / 主动性）
      ↑
    adapters 接入层（FastAPI + WebSocket，只做协议转换）

启动（**必须在项目根 ``KKcc`` 下执行**，因为全程使用包内相对导入）：

    .\\companion\\.venv\\Scripts\\python.exe -m companion.main

然后浏览器打开 http://127.0.0.1:8788 （调试面板：/debug.html）
"""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import uvicorn
from loguru import logger

from .adapters.web import create_app
from .cognition.learning import CognitionLearner
from .cognition.llm import EDITABLE_FIELDS, LLMConfig, LLMGateway
from .cognition.memory import MemoryManager
from .cognition.responder import CompanionResponder
from .cognition.retriever import KeywordRetriever
from .cognition.teaching import TeachingEngine
from .engine.bus import EventBus
from .engine.clock import Clock
from .engine.config import AppConfig, load_config
from .engine.expression import ExpressionController
from .engine.registry import LifeRegistry
from .engine.scheduler import ProactiveScheduler
from .engine.session import SessionDeps, SessionManager
from .engine.timing import TimingModel
from .mind.mind_llm import MindAccountant, ReplyIntentJudge
from .store.db import Database
from .store.life_store import LifeStore
from .store.repo import StateRepo
from .store.settings import SettingsStore

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.toml"


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <level>{message}</level>",
    )


def _resolve_llm_config(config: AppConfig, settings: SettingsStore) -> LLMConfig:
    """在 config.toml 默认值之上，叠加网页端保存的运行时覆盖（settings.json）。"""
    base = LLMConfig(**config.llm.model_dump())
    override = settings.load().get("llm") or {}
    patch = {k: v for k, v in override.items() if k in EDITABLE_FIELDS and v is not None}
    if not patch:
        return base
    try:
        logger.info("已加载网页端保存的 LLM 配置")
        return LLMConfig(**{**base.model_dump(), **patch})
    except Exception:
        logger.warning("运行时 LLM 配置非法，忽略并回退到 config.toml")
        return base


async def run(config: AppConfig) -> None:
    seed = config.engine.rng_seed
    rng = random.Random(seed)
    clock = Clock(time_scale=config.engine.time_scale)
    bus = EventBus()

    # ---- store 层 ------------------------------------------------------
    db = Database(ROOT / config.store.path)
    await db.connect()
    store = LifeStore()
    repo = StateRepo(db)
    settings = SettingsStore((ROOT / config.store.path).parent / "settings.json")

    # ---- mind / cognition 层 ------------------------------------------
    registry = LifeRegistry(store, repo, config, clock, bus, rng, db=db)
    llm = LLMGateway(_resolve_llm_config(config, settings), seed=seed)
    retriever = KeywordRetriever(db, half_life_days=config.memory.recall_half_life_days)
    memory = MemoryManager(db, llm, config.memory, retriever)
    timing = TimingModel(config.timing)
    expression = ExpressionController(timing, config.timing)
    responder = CompanionResponder(llm, expression, config.timing)
    accountant = MindAccountant(llm)
    learner = CognitionLearner(db, llm, config.cognition)
    teaching = TeachingEngine(db, max_per_round=config.teaching.max_per_round)
    reply_judge = ReplyIntentJudge(llm)

    # ---- engine 层 -----------------------------------------------------
    deps = SessionDeps(
        bus=bus,
        clock=clock,
        registry=registry,
        memory=memory,
        responder=responder,
        timing=timing,
        expression=expression,
        accountant=accountant,
        learner=learner,
        teaching=teaching,
        reply_judge=reply_judge,
        config=config,
        rng=rng,
        db=db,
    )
    sessions = SessionManager(deps)

    await registry.ensure_all()
    scheduler = ProactiveScheduler(
        clock=clock, registry=registry, sessions=sessions, db=db, config=config, rng=rng
    )
    await scheduler.start()

    # ---- adapters 层 ---------------------------------------------------
    app = create_app(
        bus, registry=registry, sessions=sessions, config=config, llm=llm,
        settings=settings, db=db,
    )

    logger.info(
        f"启动完成 → http://{config.server.host}:{config.server.port} "
        f"(time_scale={config.engine.time_scale}, LLM={llm.mode}, "
        f"生命体={registry.list_ids()})"
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.server.host, port=config.server.port, log_level="warning")
    )
    try:
        await server.serve()
    finally:
        await scheduler.stop()
        await sessions.shutdown()
        await registry.stop_all()
        await bus.stop()
        await db.close()
        logger.info("已安全退出")


def main() -> None:
    _setup_logging()
    config = load_config(CONFIG_PATH)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()