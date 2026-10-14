"""QQAdapter：QQ 接入层（OneBot v11，配合 NapCatQQ / LLOneBot 等实现端）。

WebAdapter 的兄弟模块，同样**只做协议转换**，通过事件总线与引擎解耦：

    QQ(OneBot 正向 WS) --message 事件--> IncomingMessage --> bus.emit(TOPIC_INCOMING)
    bus.emit(TOPIC_OUTBOUND) --> chunk 逐条 --> OneBot ``send_private_msg``

映射约定（v1：私聊）：
  * **一个 OneBot 实例 = 一个 QQ 号 = 一个生命体的"肉身"**（``config.qq.life_id``）；
    想让多个生命体各自上号，就跑多个 OneBot 实例 + 多份配置。
  * QQ 用户号作为引擎 ``user_id``（加 ``qq_`` 前缀，与 web 用户天然隔离——
    同一个人在网页和 QQ 上是两段独立关系与会话）。
  * 打字中 / 状态角标 / 唤醒进度等 QQ 没有的表现全部忽略；
    她的"话痨分条"（chunk 逐条 + 节奏间隔）天然对应 QQ 连发消息。
  * 断线自动重连；重连只影响转发，不影响引擎（消息都在收件箱/存档里）。
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import websockets
from loguru import logger

from ..engine.bus import TOPIC_INCOMING, TOPIC_OUTBOUND, EventBus
from ..engine.config import QQConfig
from ..engine.types import Channel, IncomingMessage, OutgoingEvent

# 需要转发到 QQ 的事件：她说的话（分条）、系统旁白、异常兜底文案。
_FORWARD_KINDS = ("chunk", "notice", "error")


def _qq_uid(qq: int | str) -> str:
    """引擎侧 user_id：qq_ 前缀，与 web 用户隔离。"""
    return f"qq_{qq}"


class QQAdapter:
    """OneBot v11 正向 WebSocket 客户端（我们主动连 NapCat 等实现端）。"""

    def __init__(self, bus: EventBus, config: QQConfig) -> None:
        self._bus = bus
        self._cfg = config
        self._ws: Any = None
        self._echo = itertools.count(1)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ---- 生命周期 ------------------------------------------------------
    async def start(self) -> None:
        if not self._cfg.life_id:
            raise ValueError("qq.life_id 未配置：这个 QQ 号扮演哪个生命体？")
        self._bus.on(TOPIC_OUTBOUND, self._on_outbound)
        self._task = asyncio.create_task(self._run(), name="qq-adapter")
        logger.info(f"QQ 接入启动：{self._cfg.ws_url} → 生命体 {self._cfg.life_id}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        """连接 → 服务 → 断开 → 重连（真实时间，不走 Clock）。"""
        headers = {}
        if self._cfg.access_token:
            headers["Authorization"] = f"Bearer {self._cfg.access_token}"
        while not self._stopping:
            try:
                async with websockets.connect(self._cfg.ws_url, extra_headers=headers) as ws:
                    self._ws = ws
                    logger.info(f"QQ 已连接 OneBot 端点：{self._cfg.ws_url}")
                    async for raw in ws:
                        await self._on_ws_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._ws = None
                if not self._stopping:
                    logger.warning(f"QQ 连接断开（{e}），{self._cfg.reconnect_s}s 后重连")
                    await asyncio.sleep(self._cfg.reconnect_s)

    # ---- 入站：OneBot 事件 → 引擎 ---------------------------------------
    async def _on_ws_message(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if data.get("post_type") != "message":
            return  # 心跳 / API 回包 / 元事件等一概不碰
        if data.get("message_type") != "private":
            return  # v1 只做私聊（群聊等后续按 @ 触发接入）
        if data.get("sub_type") == "group_self":
            return

        qq = int(data.get("user_id") or 0)
        if not qq:
            return
        allow = self._cfg.allow_users
        if allow and qq not in allow:
            logger.debug(f"QQ {qq} 不在白名单，忽略")
            return

        text = str(data.get("raw_message") or "").strip()
        if not text:
            return
        await self._bus.emit(
            TOPIC_INCOMING,
            IncomingMessage(
                life_id=self._cfg.life_id,
                user_id=_qq_uid(qq),
                content=text,
                channel=Channel.QQ,
            ),
        )

    # ---- 出站：引擎事件 → QQ --------------------------------------------
    async def _on_outbound(self, event: OutgoingEvent) -> None:
        if event.life_id != self._cfg.life_id:
            return
        if not event.user_id.startswith("qq_"):
            return  # web 用户的话由 WebAdapter 管
        kind = event.type if isinstance(event.type, str) else event.type.value
        if kind not in _FORWARD_KINDS:
            return
        text = (event.text or "").strip()
        if not text:
            return
        try:
            qq = int(event.user_id[3:])
        except ValueError:
            return
        await self._api("send_private_msg", {"user_id": qq, "message": text})

    async def _api(self, action: str, params: dict) -> None:
        """调用 OneBot API（fire-and-forget：失败记日志，不阻塞引擎）。"""
        ws = self._ws
        if ws is None:
            logger.debug(f"QQ 未连接，丢弃 {action}")
            return
        payload = {"action": action, "params": params, "echo": next(self._echo)}
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            logger.exception(f"QQ 发送失败：{action}")
