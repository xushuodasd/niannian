"""轻量事件总线（pub/sub）。

引擎内部各部件**彼此不直接持有引用**，只通过 topic 通信：

  - 接入层收到用户消息  → ``bus.emit(TOPIC_INCOMING, inc)``
  - 会话状态机产出的所有出站动作 → ``bus.emit(TOPIC_OUTBOUND, event)``
  - 接入层订阅 ``TOPIC_OUTBOUND``，按 (life_id, user_id) 推给对应前端

这样多生命体 / 多前端只是多几个订阅者，引擎不用改。
"""
from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

Handler = Callable[[Any], "Awaitable[None] | None"]

# 约定好的主题名（避免各处写错字符串）
TOPIC_INCOMING = "incoming"      # 入站消息：接入层 → 引擎
TOPIC_OUTBOUND = "outbound"      # 出站事件：引擎 → 接入层
TOPIC_CONTROL = "control"        # 控制指令：强切状态 / 切倍速（调试面板用）


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._closed = False

    # ---- 订阅 ----------------------------------------------------------
    def on(self, topic: str, handler: Handler) -> Callable[[], None]:
        """订阅 topic，返回一个"取消订阅"函数。"""
        self._subs[topic].append(handler)

        def _off() -> None:
            self.off(topic, handler)

        return _off

    def off(self, topic: str, handler: Handler) -> None:
        if handler in self._subs.get(topic, []):
            self._subs[topic].remove(handler)

    # ---- 发布 ----------------------------------------------------------
    async def emit(self, topic: str, payload: Any = None) -> None:
        """按订阅顺序分发事件；协程处理器会被 await（保证顺序确定）。

        单个处理器抛错只记录日志，不影响其他处理器——总线不该成为故障扩散点。
        """
        if self._closed:
            return
        for handler in list(self._subs.get(topic, [])):
            try:
                result = handler(payload)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"事件总线处理器异常：topic={topic} handler={handler}")

    async def stop(self) -> None:
        """关闭总线：后续 emit 全部丢弃。"""
        self._closed = True
        self._subs.clear()