"""WebAdapter：FastAPI + WebSocket 接入层。

职责很薄：**只做协议转换**，不掺业务逻辑（引擎与接入层通过事件总线解耦）。

    前端 --(WebSocket)--> 组装 IncomingMessage --> bus.emit(incoming)
    引擎 --bus.emit(outbound)--> 按 (life_id, user_id) 推给对应前端

前端 → 服务端（JSON 文本）：
    {"type": "say",        "content": "...", "life_id": "(可选)"}   发消息
    {"type": "stop"}                                                强制停止（打断）
    {"type": "switch",     "life_id": "..."}                        切换当前对话的生命体
    {"type": "list_lives"}                                          拉取生命体列表
    {"type": "genesis",    "seed": 123, "hint": {...}}              新生一个生命体（M12）
    {"type": "control",    "action": "force_state|set_time_scale|wake_now|sleep_back", ...}
    {"type": "debug"}                                               拉取调试快照（仅回给本人）

服务端 → 前端：``OutgoingEvent``（chunk/message/typing/state/presence/wake/delivered/notice/error）
以及两类仅供调试面板的裸消息：``{"type":"lives"|"debug", ...}``。
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from ..cognition.llm import EDITABLE_FIELDS, LLMConfig, LLMGateway
from ..engine.bus import TOPIC_CONTROL, TOPIC_INCOMING, TOPIC_OUTBOUND, EventBus
from ..engine.config import AppConfig
from ..engine.registry import LifeRegistry
from ..engine.session import HIST_KIND, HIST_SCOPE, SessionManager
from ..engine.types import IncomingMessage, OutgoingEvent, TurnState, now_dt
from ..store.db import Database
from ..store.settings import SettingsStore

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class NoCacheStaticFiles(StaticFiles):
    """静态前端文件补上 `Cache-Control: no-cache`。

    Starlette 的 StaticFiles 默认只带 ETag / Last-Modified，浏览器会做启发式缓存：
    前端改了却看不到（要手动硬刷新）。补上 no-cache 后浏览器每次都带 ETag 回源校验——
    文件没变就 304（省流量），一变立刻生效，且仍然无需重启服务。
    """

    async def get_response(self, path: str, scope: Any):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


class ConnectionManager:
    """维护在线连接，并把出站事件按 (life_id, user_id) 路由过去。

    每个连接自带一份"当前在跟哪个生命体聊"的绑定（支持前端切换）。

    另有一份「在途轮次缓冲」：把**当前这一轮**已发出的出站事件暂存下来。
    这样"你离开页面（WS 断开）/ 刷新 / 跳回来"时，她正在说的话不会凭空消失 ——
    新连接一接上就按原顺序回放一遍，无缝续上；一轮收尾（state=idle）即清空，
    因为内容此时已经落库，历史接口能覆盖，回放不会重复渲染。
    """

    # 参与"在途回放"的事件类型（presence/lives/debug 由快照 / 请求各自负责）
    REPLAY_KINDS = ("state", "typing", "chunk", "notice", "delivered")

    def __init__(self, *, replay_max: int = 400, replay_ttl_s: float = 300.0) -> None:
        self._binding: dict[WebSocket, tuple[str, str]] = {}
        # key → [(记录时刻, 事件载荷)]，只装"尚未落库的在途轮次"
        self._replay: dict[tuple[str, str], list[tuple[float, dict]]] = {}
        self._replay_max = replay_max
        self._replay_ttl_s = replay_ttl_s

    # ---- 连接 ----------------------------------------------------------
    def add(self, ws: WebSocket, life_id: str, user_id: str) -> None:
        self._binding[ws] = (life_id, user_id)
        logger.info(f"前端已连接 ({life_id}, {user_id})（共 {len(self._binding)} 个）")

    def remove(self, ws: WebSocket) -> None:
        key = self._binding.pop(ws, None)
        if key is None:
            return  # 已经清过了，别刷屏
        logger.info(f"前端断开 {key}（剩 {len(self._binding)} 个）")

    @property
    def count(self) -> int:
        return len(self._binding)

    def rebind(self, ws: WebSocket, life_id: str) -> tuple[str, str]:
        _old, user_id = self._binding.get(ws, (life_id, "user_demo"))
        self._binding[ws] = (life_id, user_id)
        return life_id, user_id

    def binding(self, ws: WebSocket) -> tuple[str, str]:
        return self._binding.get(ws, ("", ""))

    # ---- 发送 ----------------------------------------------------------
    async def send_json(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            # 半开连接（网络断了 / 标签页被杀）不会让 receive_text() 抛错，
            # 只能靠发送失败来判定，顺手清理，避免连接表越堆越多
            logger.debug("推送失败，判定连接已断开并清理")
            self.remove(ws)

    async def send_event(self, ws: WebSocket, event: OutgoingEvent) -> None:
        await self.send_json(ws, json.loads(event.model_dump_json()))

    async def broadcast(self, event: OutgoingEvent) -> None:
        key = (event.life_id, event.user_id)
        payload = json.loads(event.model_dump_json())
        self._remember(key, payload)
        for ws in list(self._binding):
            if self._binding.get(ws) == key:
                await self.send_json(ws, payload)

    # ---- 在途轮次缓冲（她的话不因"你看不看"而丢）-------------------------
    def _remember(self, key: tuple[str, str], payload: dict) -> None:
        kind = payload.get("type")
        if kind not in self.REPLAY_KINDS:
            return
        if kind == "state":
            if payload.get("state") == TurnState.IDLE.value:
                self._replay.pop(key, None)        # 一轮收尾 → 内容已落库，清缓冲
                return
            self._replay.setdefault(key, [])       # 新一轮开口 → 起（或续）缓冲
        buf = self._replay.get(key)
        if buf is None:
            return
        buf.append((time.monotonic(), payload))
        if len(buf) > self._replay_max:            # 安全阀：万一收尾事件缺席，别无限涨
            del buf[: len(buf) - self._replay_max]

    async def replay(self, ws: WebSocket, key: tuple[str, str]) -> None:
        """新连接接上时，把"她正在说的这一轮"按原顺序补投一遍。"""
        buf = self._replay.get(key)
        if not buf:
            return
        now = time.monotonic()
        fresh = [(ts, p) for ts, p in buf if now - ts <= self._replay_ttl_s]
        if not fresh:
            self._replay.pop(key, None)
            return
        self._replay[key] = fresh
        logger.debug(f"回放在途轮次 {key}：{len(fresh)} 条")
        for _ts, payload in fresh:
            await self.send_json(ws, payload)


def create_app(
    bus: EventBus,
    *,
    registry: LifeRegistry,
    sessions: SessionManager,
    config: AppConfig,
    llm: LLMGateway,
    settings: SettingsStore,
    db: Database | None = None,
) -> FastAPI:
    app = FastAPI(title="AI 陪伴 Demo", version="0.2.0")
    manager = ConnectionManager()

    # 引擎的每一份出站事件，都经由这里推给对应前端
    bus.on(TOPIC_OUTBOUND, manager.broadcast)

    async def lives_payload() -> list[dict]:
        out: list[dict] = []
        for rt in registry.list_runtimes():
            out.append({
                "life_id": rt.life.life_id,
                "name": rt.life.name,
                "avatar": rt.life.avatar,
                "gender": rt.life.gender,
                "presence": rt.controller.presence.value,
                "mood": rt.psyche.mood_label,
                "stage": None,  # 关系阶段随用户而变，由前端在各自会话里看角标
            })
        return out

    async def send_snapshot(ws: WebSocket, life_id: str, user_id: str) -> None:
        rt = registry.get(life_id)
        if rt is None:
            return
        await manager.send_event(ws, OutgoingEvent(
            type="presence", life_id=life_id, user_id=user_id,
            presence=rt.controller.presence,
            meta={"just_woke": rt.controller.just_woke, "note": rt.controller.availability_note()},
        ))
        # 发**真实**状态：重连时若她正在想 / 在说，别谎报"空闲"
        await manager.send_event(ws, OutgoingEvent(
            type="state", life_id=life_id, user_id=user_id,
            state=sessions.get(life_id, user_id).state,
        ))

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "lives": registry.list_ids(), "connections": manager.count}

    # ---- 历史回显（刷新页面也能看到"她记得什么"）------------------------
    @app.get("/api/history")
    async def get_history(life_id: str = "", user_id: str = "") -> dict:
        lf = life_id or config.identity.life_id
        uf = user_id or config.identity.user_id
        return {"life_id": lf, "user_id": uf, "messages": await sessions.history(lf, uf)}

    @app.post("/api/history/clear")
    async def clear_history(request: Request) -> dict:
        """清空「你看到的」聊天记录 —— 软清空，绝不删库。

        只按 (life_id, user_id) 记一个时间水印；``message`` 表一行不删，
        她的记忆 / 召回 / 关系演化都不受影响（她永远记得你们聊过什么）。
        """
        if db is None:
            return {"ok": False, "error": "数据库未就绪"}
        try:
            data: dict[str, Any] = await request.json()
        except Exception:
            data = {}
        lf = data.get("life_id") or config.identity.life_id
        uf = data.get("user_id") or config.identity.user_id
        stamp = now_dt().isoformat()
        await db.save_state(HIST_SCOPE, lf, HIST_KIND, {"at": stamp}, user_id=uf)
        logger.info(f"[{lf}/{uf}] 用户清空了自己的聊天回放（软清空，AI 记忆未删）")
        return {"ok": True, "cleared_at": stamp}

    # ---- LLM 运行时配置（网页里直接改，热生效 + 持久化）----------------
    @app.get("/api/llm")
    async def get_llm() -> dict:
        """回显当前 LLM 配置（api_key 脱敏）与可选预设。"""
        return llm.describe()

    @app.post("/api/llm")
    async def set_llm(request: Request) -> dict:
        """更新 LLM 配置：校验 → 热生效 → 落盘 → 连通性自检。

        约定：
          * 字段缺失或留空 = 沿用旧值（便于前端只发改动项）；
          * ``api_key`` 留空 = 保持原 Key 不变；
          * ``clear_key: true`` = 清空 Key（回到本地模拟大脑）。
        """
        try:
            data: dict[str, Any] = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体不是合法 JSON"}

        patch: dict[str, Any] = {
            k: data[k] for k in EDITABLE_FIELDS
            if k != "api_key" and k in data and data[k] not in (None, "")
        }
        if data.get("clear_key"):
            patch["api_key"] = ""
        elif str(data.get("api_key") or "").strip():
            patch["api_key"] = str(data["api_key"]).strip()

        merged = llm.config.model_dump()
        merged.update(patch)
        try:
            new_cfg = LLMConfig(**merged)
        except Exception as e:
            return {"ok": False, "error": f"配置非法：{e}"}

        llm.apply_config(new_cfg)
        settings.save({"llm": {k: getattr(new_cfg, k) for k in EDITABLE_FIELDS}})
        ok, tip = await llm.probe()
        return {"ok": True, "check": {"ok": ok, "message": tip}, **llm.describe()}

    # ==================================================================
    # 生命体控制台（14）：管理面 API（创建/预览/编辑/删除/克隆/导出/成长）
    # 管理操作走 REST → registry（与控制会话流解耦，不占 WS 协议）
    # ==================================================================
    async def _cognition_count(life_id: str) -> int:
        if db is None:
            return 0
        try:
            return await db.count_cognition(life_id)
        except Exception:
            return 0

    async def _lives_full() -> list[dict]:
        out: list[dict] = []
        for rt in registry.list_runtimes():
            out.append({
                "life_id": rt.life.life_id,
                "name": rt.life.name,
                "avatar": rt.life.avatar,
                "gender": rt.life.gender,
                "origin": rt.life.origin,
                "awareness": round(rt.life.awareness, 3),
                "presence": rt.controller.presence.value,
                "mood": rt.psyche.mood_label,
                "beliefs": len(rt.life.beliefs),
                "cognition": await _cognition_count(rt.life.life_id),
                "born_at": rt.life.born_at.isoformat(timespec="seconds"),
            })
        return out

    def _build_hint(data: dict) -> dict:
        """把控制台表单字段归纳成 genesis 的 hint（只放非空项）。"""
        hint: dict[str, Any] = {}
        for key in ("name", "gender", "avatar", "city", "job", "workplace", "backstory", "extra"):
            if data.get(key):
                hint[key] = data[key]
        if data.get("age"):
            try:
                hint["age"] = int(data["age"])
            except (TypeError, ValueError):
                pass
        hobbies = data.get("hobbies")
        if isinstance(hobbies, str):
            hobbies = [h.strip() for h in hobbies.replace("、", ",").split(",") if h.strip()]
        if hobbies:
            hint["hobbies"] = hobbies
        if data.get("no_pet"):
            hint["no_pet"] = True
        elif data.get("pet"):
            hint["pet"] = str(data["pet"]).strip()
        if isinstance(data.get("traits"), dict):
            hint["traits"] = data["traits"]
        if isinstance(data.get("beliefs"), list) and data["beliefs"]:
            hint["beliefs"] = data["beliefs"]
        if isinstance(data.get("quirks"), list) and data["quirks"]:
            hint["quirks"] = data["quirks"]
        if isinstance(data.get("needs_fears"), list) and data["needs_fears"]:
            hint["needs_fears"] = data["needs_fears"]
        return hint

    @app.get("/api/lives")
    async def api_list_lives() -> dict:
        return {"ok": True, "lives": await _lives_full()}

    @app.get("/api/lives/{life_id}")
    async def api_life_detail(life_id: str, user_id: str = "") -> dict:
        prof = registry.profile(life_id)
        if prof is None:
            return {"ok": False, "error": f"没有这个生命体：{life_id}"}
        uf = user_id or config.identity.user_id
        growth: dict[str, Any] = {"cognition": [], "cognition_count": 0, "rounds": 0}
        if db is not None:
            growth["cognition"] = await db.load_cognition(life_id, limit=80)
            growth["cognition_count"] = await db.count_cognition(life_id)
            growth["rounds"] = await db.round_count(life_id, uf)
        rel: dict[str, Any] | None = None
        if registry.get(life_id) is not None:
            r = await registry.relationship(life_id, uf)
            rel = {
                "user_id": uf, "stage": r.stage,
                "intimacy": round(r.intimacy, 3), "trust": round(r.trust, 3),
                "dependence": round(r.dependence, 3), "familiarity": round(r.familiarity, 3),
                "interactions": r.interactions,
            }
        return {"ok": True, "profile": prof, "growth": growth, "relationship": rel}

    @app.post("/api/lives/preview")
    async def api_preview_life(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体不是合法 JSON"}
        mode = str(data.get("mode") or "random")
        try:
            seed = int(data.get("seed") or random.randrange(1, 10**9))
        except (TypeError, ValueError):
            seed = random.randrange(1, 10**9)
        life = registry.preview_life(seed, _build_hint(data), mode)
        return {"ok": True, "seed": seed, "life": life.model_dump(mode="json")}

    @app.post("/api/lives")
    async def api_create_life(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体不是合法 JSON"}
        mode = str(data.get("mode") or "random")
        try:
            seed = int(data.get("seed") or random.randrange(1, 10**9))
        except (TypeError, ValueError):
            seed = random.randrange(1, 10**9)
        try:
            life = await registry.create_life(seed, _build_hint(data), mode)
        except Exception as e:
            logger.exception("创世失败")
            return {"ok": False, "error": f"创世失败：{e}"}
        return {"ok": True, "life": life.model_dump(mode="json"), "lives": await _lives_full()}

    @app.patch("/api/lives/{life_id}")
    async def api_update_life(life_id: str, request: Request) -> dict:
        if registry.get(life_id) is None:
            try:
                await registry.ensure(life_id, create_default=False)
            except KeyError:
                return {"ok": False, "error": f"没有这个生命体：{life_id}"}
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体不是合法 JSON"}
        try:
            life = await registry.update_life(life_id, data or {})
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        return {"ok": True, "life": life.model_dump(mode="json")}

    @app.delete("/api/lives/{life_id}")
    async def api_delete_life(life_id: str) -> dict:
        if life_id == config.identity.life_id:
            return {"ok": False, "error": "不能删除当前默认生命体（会影响默认会话），请先切换默认生命体"}
        if registry.get(life_id) is None and not registry.exists(life_id):
            return {"ok": False, "error": f"没有这个生命体：{life_id}"}
        await registry.delete_life(life_id)
        return {"ok": True, "lives": await _lives_full()}

    @app.post("/api/lives/{life_id}/clone")
    async def api_clone_life(life_id: str, request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            data = {}
        try:
            life = await registry.clone_life(life_id, (data or {}).get("name"))
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception("克隆失败")
            return {"ok": False, "error": f"克隆失败：{e}"}
        return {"ok": True, "life": life.model_dump(mode="json"), "lives": await _lives_full()}

    @app.get("/api/lives/{life_id}/export")
    async def api_export_life(life_id: str) -> dict:
        prof = registry.profile(life_id)
        if prof is None:
            return {"ok": False, "error": f"没有这个生命体：{life_id}"}
        return {"ok": True, "life": prof}

    # ---- 认知管理（14.1）：主动教导 / 纠错 / 删除 -----------------------
    async def _cognition_guard(life_id: str) -> dict | None:
        """公共校验：库就绪 + 生命体存在；不通过时返回错误体。"""
        if db is None:
            return {"ok": False, "error": "数据库未就绪"}
        if registry.profile(life_id) is None:
            return {"ok": False, "error": f"没有这个生命体：{life_id}"}
        return None

    @app.post("/api/lives/{life_id}/cognition")
    async def api_teach_cognition(life_id: str, request: Request) -> dict:
        """主动教导：把一条知识直接"教"给她（高确信度、上锁，此后自学不再覆写）。"""
        bad = await _cognition_guard(life_id)
        if bad:
            return bad
        try:
            data: dict[str, Any] = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体不是合法 JSON"}
        topic = str(data.get("topic") or "").strip()
        understanding = str(data.get("understanding") or "").strip()
        if not topic or not understanding:
            return {"ok": False, "error": "话题与她的理解都不能为空"}
        try:
            await db.teach_cognition(life_id, topic, understanding, by_user=config.identity.user_id)
        except Exception as e:
            logger.exception("教导失败")
            return {"ok": False, "error": f"教导失败：{e}"}
        return {"ok": True, "cognition": await db.load_cognition(life_id, limit=80)}

    @app.post("/api/lives/{life_id}/cognition/{cid}/correct")
    async def api_correct_cognition(life_id: str, cid: int, request: Request) -> dict:
        """纠正一条认知：改写理解、抬高确信度并上锁（防止被再次带偏）。"""
        bad = await _cognition_guard(life_id)
        if bad:
            return bad
        try:
            data: dict[str, Any] = await request.json()
        except Exception:
            return {"ok": False, "error": "请求体不是合法 JSON"}
        understanding = str(data.get("understanding") or "").strip()
        if not understanding:
            return {"ok": False, "error": "纠正后的理解不能为空"}
        if await db.get_cognition(life_id, cid) is None:
            return {"ok": False, "error": f"没有这条认知：{cid}"}
        try:
            await db.correct_cognition(life_id, cid, understanding, by_user=config.identity.user_id)
        except Exception as e:
            logger.exception("纠正认知失败")
            return {"ok": False, "error": f"纠正失败：{e}"}
        return {"ok": True, "cognition": await db.load_cognition(life_id, limit=80)}

    @app.delete("/api/lives/{life_id}/cognition/{cid}")
    async def api_delete_cognition(life_id: str, cid: int) -> dict:
        """删除一条认知（仅删这条认知；她的原始聊天记录不受影响）。"""
        bad = await _cognition_guard(life_id)
        if bad:
            return bad
        try:
            changed = await db.delete_cognition(life_id, cid, by_user=config.identity.user_id)
        except Exception as e:
            logger.exception("删除认知失败")
            return {"ok": False, "error": f"删除失败：{e}"}
        if not changed:
            return {"ok": False, "error": f"没有这条认知：{cid}"}
        return {"ok": True, "cognition": await db.load_cognition(life_id, limit=80)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, life_id: str = "", user_id: str = "") -> None:
        await ws.accept()
        # 前端可带上"她上次在聊的那位"（刷新 / 从别的页面跳回来时保持同一个人）。
        # 缺省、或该生命体不存在时回落到默认体——这样"记录对不上人"从根上消失。
        user_id = user_id or config.identity.user_id
        target = life_id or config.identity.life_id
        try:
            await registry.ensure(target, create_default=False)
        except KeyError:
            target = config.identity.life_id
            await registry.ensure(target)
        except Exception:
            logger.exception(f"生命体 {target} 载入失败，回落到默认体")
            target = config.identity.life_id
        life_id = target
        manager.add(ws, life_id, user_id)
        await send_snapshot(ws, life_id, user_id)
        # 接上时补投"她正在说的这一轮"：离开 / 刷新 / 跳回来都不再断片
        await manager.replay(ws, (life_id, user_id))
        await manager.send_json(ws, {"type": "lives", "lives": await lives_payload()})

        try:
            while True:
                raw = await ws.receive_text()
                await _dispatch(raw, ws, bus, manager, registry, sessions, lives_payload)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WebSocket 处理异常")
        finally:
            manager.remove(ws)

    # 静态前端放到最后挂载，避免抢占 /ws 与 /health
    app.mount("/", NoCacheStaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


async def _dispatch(
    raw: str,
    ws: WebSocket,
    bus: EventBus,
    manager: ConnectionManager,
    registry: LifeRegistry,
    sessions: SessionManager,
    lives_payload,
) -> None:
    """协议分发：把前端 JSON 翻译成事件总线上的消息。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"收到非 JSON 消息：{raw[:80]!r}")
        return

    life_id, user_id = manager.binding(ws)
    msg_type = data.get("type")
    payload_life = data.get("life_id") or life_id

    # 前端用下拉框选中生命体时是直接给 .value 赋值，程序化赋值不触发 change，
    # 于是连接还绑在默认生命体上；而她的回复按 (life_id, user_id) 精确路由，
    # 与旧绑定对不上就被静默丢弃——表现成"她在说、你却永远收不到"。
    # 这里以消息里带的 life_id 为准补一次重绑（仅当该生命体确实存在）。
    # switch 分支自己会重绑，不走这里，避免目标不存在时把绑定改坏。
    if msg_type != "switch" and payload_life != life_id and registry.get(payload_life):
        life_id, user_id = manager.rebind(ws, payload_life)

    if msg_type == "say":
        content = (data.get("content") or "").strip()
        if not content:
            return
        await bus.emit(
            TOPIC_INCOMING,
            IncomingMessage(life_id=payload_life, user_id=user_id, content=content),
        )

    elif msg_type == "stop":
        await bus.emit(TOPIC_CONTROL, {"action": "force_stop", "life_id": payload_life, "user_id": user_id})

    elif msg_type == "control":
        payload = {"life_id": payload_life, "user_id": user_id, **data}
        payload.pop("type", None)
        await bus.emit(TOPIC_CONTROL, payload)

    elif msg_type == "switch":
        new_life = data.get("life_id") or life_id
        try:
            await registry.ensure(new_life, create_default=False)
        except KeyError:
            await manager.send_json(ws, {"type": "notice", "text": f"没有这个生命体：{new_life}"})
            return
        bound_life, bound_user = manager.rebind(ws, new_life)
        await manager.send_json(ws, {"type": "notice", "text": f"已切换到「{registry.require(new_life).life.name}」"})
        await send_snapshot_public(manager, registry, sessions, ws, bound_life, bound_user)
        await manager.replay(ws, (bound_life, bound_user))

    elif msg_type == "list_lives":
        await manager.send_json(ws, {"type": "lives", "lives": await lives_payload()})

    elif msg_type == "genesis":
        seed = int(data.get("seed") or 0)
        hint = data.get("hint") or None
        try:
            life = await registry.create_life(seed, hint)
        except Exception:
            logger.exception("创世失败")
            await manager.send_json(ws, {"type": "notice", "text": "创世失败，看看日志吧"})
            return
        await manager.send_json(ws, {"type": "notice", "text": f"新生了一个生命体：「{life.name}」"})
        await manager.send_json(ws, {"type": "lives", "lives": await lives_payload()})

    elif msg_type == "debug":
        snapshot = sessions.debug_snapshot(payload_life, user_id)
        await manager.send_json(ws, {"type": "debug", "life_id": payload_life, "data": snapshot})

    else:
        logger.warning(f"未知消息类型：{msg_type!r}")


async def send_snapshot_public(
    manager: ConnectionManager, registry: LifeRegistry, sessions: SessionManager,
    ws: WebSocket, life_id: str, user_id: str,
) -> None:
    """切换生命体后，把新会话的状态同步给该连接。"""
    rt = registry.get(life_id)
    if rt is None:
        return
    await manager.send_event(ws, OutgoingEvent(
        type="presence", life_id=life_id, user_id=user_id,
        presence=rt.controller.presence,
        meta={"just_woke": rt.controller.just_woke, "note": rt.controller.availability_note()},
    ))
    await manager.send_event(ws, OutgoingEvent(
        type="state", life_id=life_id, user_id=user_id,
        state=sessions.get(life_id, user_id).state,
    ))


def html_index() -> HTMLResponse:
    """兜底：直接读取首页（StaticFiles 不可用时用）。"""
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))