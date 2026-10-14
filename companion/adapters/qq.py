"""QQAdapter：QQ 接入层（OneBot v11，配合 NapCatQQ / LLOneBot 等实现端）。

> 合规边界：只实现 OneBot v11 开放协议客户端；不包含/打包/下载/分发任何第三方
> 协议端（NapCat 等），用户自行部署并自担合规责任。

WebAdapter 的兄弟模块，同样**只做协议转换**，通过事件总线与引擎解耦：

    QQ(OneBot 正向 WS) --message 事件--> IncomingMessage --> bus.emit(TOPIC_INCOMING)
    bus.emit(TOPIC_OUTBOUND) --> chunk 逐条 --> OneBot ``send_private_msg``

映射约定（v1：私聊）：
  * **一个 OneBot 实例 = 一个 QQ 号 = 一个生命体的"肉身"**（``config.qq.life_id``）；
  * QQ 用户号作为引擎 ``user_id``（加 ``qq_`` 前缀，与 web 用户天然隔离）；
  * 打字中 / 状态角标 / 唤醒进度等 QQ 没有的表现全部忽略；
    她的"话痨分条"（chunk 逐条 + 节奏间隔）天然对应 QQ 连发消息。
  * 断线自动重连；重连只影响转发，不影响引擎（消息都在收件箱/存档里）。

``QQManager``：网页端「QQ 接入」配置的管理者——状态快照（连接状态 / 登录
QQ 号 / 最近错误）+ 热更新（停旧建新，改配置不用重启服务）。
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
        self._pending: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        # 运行状态（供网页配置界面展示）
        self.status = "connecting"      # connecting / connected / reconnecting
        self.bot_uin: int | None = None  # 登录的 QQ 号（这个号的"肉身"）
        self.bot_name: str = ""           # QQ 昵称
        self.last_error: str = ""

    # ---- 生命周期 ------------------------------------------------------
    async def start(self) -> None:
        if not self._cfg.life_id:
            raise ValueError("qq.life_id 未配置：这个 QQ 号扮演哪个生命体？")
        self._bus.on(TOPIC_OUTBOUND, self._on_outbound)
        self._task = asyncio.create_task(self._run(), name="qq-adapter")
        logger.info(f"QQ 接入启动：{self._cfg.ws_url} → 生命体 {self._cfg.life_id}")

    async def stop(self) -> None:
        self._stopping = True
        try:
            self._bus.off(TOPIC_OUTBOUND, self._on_outbound)
        except Exception:
            pass
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
                # websockets>=14 的头参数叫 additional_headers（旧名 extra_headers 已移除）
                async with websockets.connect(self._cfg.ws_url, additional_headers=headers) as ws:
                    self._ws = ws
                    self.status = "connected"
                    self.last_error = ""
                    logger.info(f"QQ 已连接 OneBot 端点：{self._cfg.ws_url}")
                    # 主动问一次"这个号是谁"，让配置界面能显示登录身份
                    info = await self._api_wait("get_login_info", {})
                    if isinstance(info, dict) and info.get("status") == "ok":
                        d = info.get("data") or {}
                        self.bot_uin = d.get("user_id")
                        self.bot_name = str(d.get("nickname") or "")
                        logger.info(
                            f"QQ 身份：{self.bot_uin}（{self.bot_name}）→ 生命体 {self._cfg.life_id}"
                        )
                    async for raw in ws:
                        await self._on_ws_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._ws = None
                self.status = "reconnecting"
                self.last_error = str(e)[:200]
                if not self._stopping:
                    logger.warning(f"QQ 连接断开（{e}），{self._cfg.reconnect_s}s 后重连")
                    await asyncio.sleep(self._cfg.reconnect_s)

    # ---- 入站：OneBot 事件 → 引擎 ---------------------------------------
    async def _on_ws_message(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        # API 回包（echo 匹配）
        echo = data.get("echo")
        if echo is not None and "retcode" in data:
            fut = self._pending.get(str(echo))
            if fut is not None and not fut.done():
                fut.set_result(data)
            return
        if data.get("post_type") != "message":
            return  # 心跳 / 元事件等一概不碰
        if data.get("message_type") != "private":
            return  # v1 只做私聊（群聊等后续按 @ 触发接入）
        # 事件自带 self_id：顺手记录登录号（get_login_info 失败时的兜底）
        if data.get("self_id"):
            self.bot_uin = int(data["self_id"])

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

    # ---- OneBot API -----------------------------------------------------
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

    async def _api_wait(self, action: str, params: dict, timeout: float = 6.0) -> dict | None:
        """调用 OneBot API 并等待回包（echo 匹配）；超时返回 None。"""
        ws = self._ws
        if ws is None:
            return None
        echo = str(next(self._echo))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
            return await asyncio.wait_for(fut, timeout)  # type: ignore[arg-type,return-value]
        except Exception:
            return None
        finally:
            self._pending.pop(echo, None)

    def snapshot(self) -> dict:
        """给配置界面的运行状态快照。"""
        return {
            "status": self.status,
            "ws_url": self._cfg.ws_url,
            "life_id": self._cfg.life_id,
            "allow_users": list(self._cfg.allow_users),
            "has_token": bool(self._cfg.access_token),
            "bot_uin": self.bot_uin,
            "bot_name": self.bot_name,
            "last_error": self.last_error,
        }


class QQManager:
    """QQ 接入的运行时管理者：热更新（停旧建新）+ 状态快照。

    网页端 ``POST /api/qq`` 改配置 → ``apply()`` 立即生效（无需重启服务）；
    ``access_token`` 留空时沿用上次的值（界面不回显、也不重复输入）。
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.adapter: QQAdapter | None = None
        self._last_token = ""
        self._lock = asyncio.Lock()

    async def apply(self, cfg: QQConfig) -> dict:
        async with self._lock:
            if self.adapter is not None:
                await self.adapter.stop()
                self.adapter = None
            if not cfg.access_token and self._last_token:
                cfg = cfg.model_copy(update={"access_token": self._last_token})
            self._last_token = cfg.access_token
            if cfg.enabled and cfg.life_id:
                self.adapter = QQAdapter(self._bus, cfg)
                try:
                    await self.adapter.start()
                except Exception:
                    logger.exception("QQ 接入启动失败（检查 life_id 与 OneBot 端点）")
                    self.adapter = None
            return self.snapshot()

    def snapshot(self) -> dict:
        if self.adapter is None:
            return {
                "enabled": False, "status": "disabled", "ws_url": "", "life_id": "",
                "allow_users": [], "has_token": bool(self._last_token),
                "bot_uin": None, "bot_name": "", "last_error": "",
            }
        snap = self.adapter.snapshot()
        snap["enabled"] = True
        return snap

    async def stop(self) -> None:
        if self.adapter is not None:
            await self.adapter.stop()
            self.adapter = None
