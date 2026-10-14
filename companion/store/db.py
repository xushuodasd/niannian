"""SQLite 存储层（aiosqlite + FTS5）。

表结构对齐设计文档 7 / 10.4，并保留"决策 trace"表（9.2 J：没有 trace 就没有调参）。

分层：
  * 结构化：``message`` / ``round`` / ``summary`` / ``fact`` / ``diary`` / ``topic`` / ``life_event`` / ``emotion_memory``
  * 快照：``state``（人格之外的运行时状态：psyche / relationship / life_state，JSON 存）
  * 可观测：``trace``
  * 全文检索：``message_fts`` / ``fact_fts``（FTS5，关键词召回）
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from ..engine.types import ChatMessage, Role

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS message (
    id         TEXT PRIMARY KEY,
    life_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_message_session ON message (life_id, user_id, created_at);

CREATE TABLE IF NOT EXISTS round (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    segments    TEXT NOT NULL DEFAULT '[]',
    timing      TEXT NOT NULL DEFAULT '{}',
    emotion_delta REAL NOT NULL DEFAULT 0,
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_round_session ON round (life_id, user_id, id);

CREATE TABLE IF NOT EXISTS summary (
    life_id            TEXT NOT NULL,
    user_id            TEXT NOT NULL,
    rolling_summary    TEXT NOT NULL DEFAULT '',
    covered_until_round INTEGER NOT NULL DEFAULT 0,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (life_id, user_id)
);

CREATE TABLE IF NOT EXISTS fact (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    last_seen  TEXT NOT NULL,
    UNIQUE (life_id, user_id, key)
);
CREATE INDEX IF NOT EXISTS idx_fact_session ON fact (life_id, user_id);

CREATE TABLE IF NOT EXISTS diary (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id   TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    day       TEXT NOT NULL,
    content   TEXT NOT NULL,
    highlights TEXT NOT NULL DEFAULT '[]',
    UNIQUE (life_id, user_id, day)
);

CREATE TABLE IF NOT EXISTS topic (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id       TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    last_round_id INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    UNIQUE (life_id, user_id, title)
);

CREATE TABLE IF NOT EXISTS life_event (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id  TEXT NOT NULL,
    day      TEXT NOT NULL,
    activity TEXT NOT NULL,
    mood_tag TEXT NOT NULL DEFAULT '',
    ts       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emotion_memory (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    trigger TEXT NOT NULL,
    valence REAL NOT NULL DEFAULT 0,
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cognition (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id        TEXT NOT NULL,
    topic          TEXT NOT NULL,
    understanding  TEXT NOT NULL,
    confidence     REAL NOT NULL DEFAULT 0.3,
    malleability   REAL NOT NULL DEFAULT 0.8,
    source         TEXT NOT NULL DEFAULT 'dialogue',
    exposure_count INTEGER NOT NULL DEFAULT 0,
    correction_count INTEGER NOT NULL DEFAULT 0,
    taught_by      TEXT NOT NULL DEFAULT '',
    locked         INTEGER NOT NULL DEFAULT 0,
    learned_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (life_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_cognition_life ON cognition (life_id);

CREATE TABLE IF NOT EXISTS state (
    scope      TEXT NOT NULL,
    life_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, life_id, user_id, kind)
);

CREATE TABLE IF NOT EXISTS trace (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    life_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind    TEXT NOT NULL,
    data    TEXT NOT NULL,
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_session ON trace (life_id, user_id, id);
"""

# FTS5 单独建（某些 SQLite 构建可能未编译 FTS5，故容错）
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    content, message_id UNINDEXED, life_id UNINDEXED, user_id UNINDEXED, tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(
    content, fact_id UNINDEXED, life_id UNINDEXED, user_id UNINDEXED, tokenize='unicode61'
);
"""


# ---- 轻量 schema 迁移 --------------------------------------------------
# CREATE TABLE IF NOT EXISTS 对**已存在**的表不会补列，历史库因此会缺字段
# （例如 M1 期的 message 表没有 meta 列）。这里从 _SCHEMA 声明里解析出每张表
# 应有的列，与库中实际情况对比，缺哪列就 ALTER 补哪列——既自愈老库，
# 让"以后新增字段"零迁移成本，也避免解析重复的列定义（单一事实来源）。
_TABLE_DECL = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.S)
_NOT_A_COLUMN = ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK", "CONSTRAINT")


def _split_column_defs(body: str) -> list[str]:
    """按顶层逗号切分列定义（忽略括号内的逗号，如 PRIMARY KEY (a, b)）。"""
    defs, buf, depth = [], "", 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            defs.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        defs.append(buf)
    return [d.strip() for d in defs if d.strip()]


def _expected_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    """解析建表声明 → {表名: [(列名, 列定义), ...]}。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for m in _TABLE_DECL.finditer(schema):
        table, body = m.group(1), m.group(2)
        cols: list[tuple[str, str]] = []
        for line in _split_column_defs(body):
            if line.upper().startswith(_NOT_A_COLUMN):
                continue
            name = line.split(None, 1)[0].strip('"`[]')
            cols.append((name, line))
        out[table] = cols
    return out


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self.fts_enabled = False

    # ---- 生命周期 ------------------------------------------------------
    async def connect(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        try:
            await self._conn.executescript(_FTS_SCHEMA)
            self.fts_enabled = True
        except Exception:
            logger.warning("SQLite 未启用 FTS5，召回将退化为 LIKE 匹配")
        await self._conn.commit()
        logger.info(f"SQLite 就绪：{self._path}（FTS5={self.fts_enabled}）")

    async def _migrate(self) -> None:
        """把历史库补齐到当前 _SCHEMA 声明的结构（缺列即 ALTER 补列）。"""
        for table, cols in _expected_columns(_SCHEMA).items():
            cursor = await self.conn.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            await cursor.close()
            existing = {r["name"] for r in rows}
            if not existing:
                continue  # 表还不存在（由 executescript 建好），无需补列
            for name, definition in cols:
                if name in existing or "PRIMARY KEY" in definition.upper():
                    continue
                try:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
                    logger.info(f"schema 迁移：{table} 补列 {name}")
                except Exception:
                    logger.warning(f"schema 迁移失败：{table}.{name}")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database 未 connect"
        return self._conn

    async def _exec(self, sql: str, params: tuple = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    # ---- 消息 ----------------------------------------------------------
    async def save_message(self, msg: ChatMessage) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO message "
            "(id, life_id, user_id, role, content, channel, created_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.id, msg.life_id, msg.user_id, msg.role.value, msg.content,
                msg.channel.value if hasattr(msg.channel, "value") else str(msg.channel),
                msg.created_at.isoformat(), json.dumps(msg.meta, ensure_ascii=False),
            ),
        )
        if self.fts_enabled and msg.role in (Role.USER, Role.ASSISTANT):
            await self._exec(
                "INSERT INTO message_fts (content, message_id, life_id, user_id) "
                "VALUES (?, ?, ?, ?)",
                (msg.content, msg.id, msg.life_id, msg.user_id),
            )

    async def load_recent(self, life_id: str, user_id: str, limit: int = 20) -> list[ChatMessage]:
        cursor = await self.conn.execute(
            "SELECT * FROM message WHERE life_id = ? AND user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (life_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ChatMessage(
                id=r["id"], life_id=r["life_id"], user_id=r["user_id"],
                role=Role(r["role"]), content=r["content"], channel=r["channel"],
                created_at=datetime.fromisoformat(r["created_at"]),
                meta=json.loads(r["meta"] or "{}"),
            )
            for r in reversed(rows)
        ]

    # ---- 轮次（round）--------------------------------------------------
    async def add_round(
        self,
        *,
        life_id: str,
        user_id: str,
        role: str,
        content: str,
        segments: list[dict[str, Any]] | None = None,
        timing: dict[str, Any] | None = None,
        emotion_delta: float = 0.0,
        ts: datetime | None = None,
    ) -> int:
        cursor = await self.conn.execute(
            "INSERT INTO round (life_id, user_id, role, content, segments, timing, emotion_delta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                life_id, user_id, role, content,
                json.dumps(segments or [], ensure_ascii=False),
                json.dumps(timing or {}, ensure_ascii=False),
                emotion_delta,
                (ts or datetime.now().astimezone()).isoformat(),
            ),
        )
        await self.conn.commit()
        return int(cursor.lastrowid or 0)

    async def round_count(self, life_id: str, user_id: str) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM round WHERE life_id = ? AND user_id = ?",
            (life_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row else 0

    async def rounds_after(self, life_id: str, user_id: str, after_id: int, limit: int = 50) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM round WHERE life_id = ? AND user_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (life_id, user_id, after_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 滚动摘要 ------------------------------------------------------
    async def get_summary(self, life_id: str, user_id: str) -> tuple[str, int]:
        cursor = await self.conn.execute(
            "SELECT rolling_summary, covered_until_round FROM summary "
            "WHERE life_id = ? AND user_id = ?",
            (life_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return "", 0
        return row["rolling_summary"], int(row["covered_until_round"])

    async def set_summary(self, life_id: str, user_id: str, text: str, covered: int) -> None:
        await self._exec(
            "INSERT INTO summary (life_id, user_id, rolling_summary, covered_until_round, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(life_id, user_id) DO UPDATE SET "
            "rolling_summary=excluded.rolling_summary, "
            "covered_until_round=excluded.covered_until_round, updated_at=excluded.updated_at",
            (life_id, user_id, text, covered, datetime.now().astimezone().isoformat()),
        )

    # ---- 长期事实 ------------------------------------------------------
    async def upsert_fact(self, life_id: str, user_id: str, key: str, value: str, importance: float) -> int:
        now = datetime.now().astimezone().isoformat()
        await self._exec(
            "INSERT INTO fact (life_id, user_id, key, value, importance, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(life_id, user_id, key) DO UPDATE SET "
            "value=excluded.value, importance=MAX(fact.importance, excluded.importance), "
            "last_seen=excluded.last_seen",
            (life_id, user_id, key, value, importance, now),
        )
        cursor = await self.conn.execute(
            "SELECT id FROM fact WHERE life_id=? AND user_id=? AND key=?", (life_id, user_id, key)
        )
        row = await cursor.fetchone()
        await cursor.close()
        fid = int(row["id"]) if row else 0
        if self.fts_enabled and fid:
            await self._exec("DELETE FROM fact_fts WHERE fact_id = ?", (fid,))
            await self._exec(
                "INSERT INTO fact_fts (content, fact_id, life_id, user_id) VALUES (?, ?, ?, ?)",
                (f"{key} {value}", fid, life_id, user_id),
            )
        return fid

    async def load_facts(self, life_id: str, user_id: str, limit: int = 20) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM fact WHERE life_id = ? AND user_id = ? "
            "ORDER BY importance DESC, last_seen DESC LIMIT ?",
            (life_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 日记 ----------------------------------------------------------
    async def upsert_diary(self, life_id: str, user_id: str, day: str, content: str, highlights: list[str]) -> None:
        await self._exec(
            "INSERT INTO diary (life_id, user_id, day, content, highlights) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(life_id, user_id, day) DO UPDATE SET "
            "content=excluded.content, highlights=excluded.highlights",
            (life_id, user_id, day, content, json.dumps(highlights, ensure_ascii=False)),
        )

    async def load_diary(self, life_id: str, user_id: str, limit: int = 7) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM diary WHERE life_id = ? AND user_id = ? ORDER BY day DESC LIMIT ?",
            (life_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 话题栈 --------------------------------------------------------
    async def upsert_topic(self, life_id: str, user_id: str, title: str, status: str, last_round_id: int) -> None:
        await self._exec(
            "INSERT INTO topic (life_id, user_id, title, status, last_round_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(life_id, user_id, title) DO UPDATE SET "
            "status=excluded.status, last_round_id=excluded.last_round_id, updated_at=excluded.updated_at",
            (life_id, user_id, title, status, last_round_id, datetime.now().astimezone().isoformat()),
        )

    async def load_topics(self, life_id: str, user_id: str, status: str = "open", limit: int = 5) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM topic WHERE life_id = ? AND user_id = ? AND status = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (life_id, user_id, status, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 生活线 --------------------------------------------------------
    async def add_life_event(self, life_id: str, day: str, activity: str, mood_tag: str = "") -> None:
        await self._exec(
            "INSERT INTO life_event (life_id, day, activity, mood_tag, ts) VALUES (?, ?, ?, ?, ?)",
            (life_id, day, activity, mood_tag, datetime.now().astimezone().isoformat()),
        )

    async def recent_life_events(self, life_id: str, limit: int = 5) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM life_event WHERE life_id = ? ORDER BY id DESC LIMIT ?",
            (life_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 情绪记忆 ------------------------------------------------------
    async def add_emotion_memory(self, life_id: str, user_id: str, kind: str, trigger: str, valence: float) -> None:
        await self._exec(
            "INSERT INTO emotion_memory (life_id, user_id, kind, trigger, valence, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (life_id, user_id, kind, trigger[:80], valence, datetime.now().astimezone().isoformat()),
        )

    async def load_emotion_memory(self, life_id: str, user_id: str, limit: int = 10) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM emotion_memory WHERE life_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (life_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 通用状态快照（psyche / relationship / life_state）--------------
    async def save_state(self, scope: str, life_id: str, kind: str, data: dict, user_id: str = "") -> None:
        await self._exec(
            "INSERT INTO state (scope, life_id, user_id, kind, data, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, life_id, user_id, kind) DO UPDATE SET "
            "data=excluded.data, updated_at=excluded.updated_at",
            (scope, life_id, user_id, kind, json.dumps(data, ensure_ascii=False),
             datetime.now().astimezone().isoformat()),
        )

    async def load_state(self, scope: str, life_id: str, kind: str, user_id: str = "") -> dict | None:
        cursor = await self.conn.execute(
            "SELECT data FROM state WHERE scope=? AND life_id=? AND user_id=? AND kind=?",
            (scope, life_id, user_id, kind),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return json.loads(row["data"]) if row else None

    # ---- 决策 trace（9.2 J）-------------------------------------------
    async def add_trace(self, life_id: str, user_id: str, kind: str, data: dict) -> None:
        await self._exec(
            "INSERT INTO trace (life_id, user_id, kind, data, ts) VALUES (?, ?, ?, ?, ?)",
            (life_id, user_id, kind, json.dumps(data, ensure_ascii=False),
             datetime.now().astimezone().isoformat()),
        )

    async def load_traces(self, life_id: str, user_id: str, limit: int = 30) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM trace WHERE life_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (life_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    # ---- 认知（14. 认知习得：她对这个世界的理解，跨用户）----------------
    async def upsert_cognition(
        self,
        life_id: str,
        topic: str,
        understanding: str,
        confidence: float = 0.3,
        malleability: float = 0.8,
        source: str = "dialogue",
    ) -> int:
        """写入/强化一条认知：同 topic 反复接触 → 确信度取高者、曝光次数 +1。

        **上锁保护**：``locked=1`` 的行（用户亲口教的 / 用户纠正过的）不允许被
        后续自学覆写——理解、确信度、可塑性都原样保留，只有曝光次数与时间照常更新。
        ``locked=0`` 的普通行行为与以前完全一致。
        """
        now = datetime.now().astimezone().isoformat()
        await self._exec(
            "INSERT INTO cognition "
            "(life_id, topic, understanding, confidence, malleability, source, exposure_count, learned_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(life_id, topic) DO UPDATE SET "
            "understanding=CASE WHEN cognition.locked "
            "  THEN cognition.understanding ELSE excluded.understanding END, "
            "confidence=CASE WHEN cognition.locked "
            "  THEN cognition.confidence ELSE MAX(cognition.confidence, excluded.confidence) END, "
            "malleability=CASE WHEN cognition.locked "
            "  THEN cognition.malleability ELSE MAX(0.1, cognition.malleability - 0.05) END, "
            "exposure_count=cognition.exposure_count + 1, "
            "updated_at=excluded.updated_at",
            (life_id, topic, understanding, confidence, malleability, source, now, now),
        )
        cursor = await self.conn.execute(
            "SELECT id FROM cognition WHERE life_id=? AND topic=?", (life_id, topic)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["id"]) if row else 0

    async def get_cognition(self, life_id: str, cog_id: int) -> dict | None:
        """按 id 取一条认知（校验它确实属于这个生命体）。"""
        cursor = await self.conn.execute(
            "SELECT * FROM cognition WHERE id = ? AND life_id = ?", (cog_id, life_id)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None

    async def teach_cognition(
        self, life_id: str, topic: str, understanding: str, by_user: str = "user_demo"
    ) -> int:
        """用户主动"教"她一条认知：高确信度、低可塑、上锁（自学不再覆写）。

        同 topic 已存在时按"用户教"的档位改写并补上锁；新建则直接落成 taught。
        """
        now = datetime.now().astimezone().isoformat()
        await self._exec(
            "INSERT INTO cognition "
            "(life_id, topic, understanding, confidence, malleability, source, "
            "exposure_count, taught_by, locked, learned_at, updated_at) "
            "VALUES (?, ?, ?, 0.9, 0.3, 'taught', 1, ?, 1, ?, ?) "
            "ON CONFLICT(life_id, topic) DO UPDATE SET "
            "understanding=excluded.understanding, confidence=0.9, malleability=0.3, "
            "source='taught', taught_by=excluded.taught_by, locked=1, updated_at=excluded.updated_at",
            (life_id, topic, understanding, by_user, now, now),
        )
        cursor = await self.conn.execute(
            "SELECT id FROM cognition WHERE life_id=? AND topic=?", (life_id, topic)
        )
        row = await cursor.fetchone()
        await cursor.close()
        cog_id = int(row["id"]) if row else 0
        await self.add_trace(life_id, by_user, "cognition_teach",
                             {"topic": topic, "understanding": understanding, "cognition_id": cog_id})
        return cog_id

    async def correct_cognition(
        self, life_id: str, cog_id: int, understanding: str, by_user: str = "user_demo"
    ) -> bool:
        """用户纠正一条已学会的认知：改写理解、抬高确信度、上锁、纠正次数 +1。"""
        now = datetime.now().astimezone().isoformat()
        cursor = await self.conn.execute(
            "UPDATE cognition SET understanding=?, confidence=0.8, locked=1, "
            "source='corrected', correction_count=correction_count + 1, updated_at=? "
            "WHERE id = ? AND life_id = ?",
            (understanding, now, cog_id, life_id),
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        await self.conn.commit()
        if changed:
            await self.add_trace(life_id, by_user, "cognition_correct",
                                 {"cognition_id": cog_id, "understanding": understanding})
        return changed

    async def delete_cognition(self, life_id: str, cog_id: int, by_user: str = "user_demo") -> bool:
        """硬删单条认知（用户可在控制台移除错误条目；聊天原始消息永不删）。"""
        cursor = await self.conn.execute(
            "DELETE FROM cognition WHERE id = ? AND life_id = ?", (cog_id, life_id)
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        await self.conn.commit()
        if changed:
            await self.add_trace(life_id, by_user, "cognition_delete", {"cognition_id": cog_id})
        return changed

    async def load_cognition(self, life_id: str, limit: int = 20) -> list[dict]:
        cursor = await self.conn.execute(
            "SELECT * FROM cognition WHERE life_id = ? "
            "ORDER BY confidence DESC, exposure_count DESC, updated_at DESC LIMIT ?",
            (life_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def count_cognition(self, life_id: str) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM cognition WHERE life_id = ?", (life_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row["c"]) if row else 0

    # ---- 级联删除：删生命体时清掉它的全部数据，避免脏数据 --------------
    async def delete_life_data(self, life_id: str) -> None:
        tables = (
            "message", "round", "summary", "fact", "diary", "topic",
            "life_event", "emotion_memory", "state", "trace", "cognition",
        )
        for table in tables:
            await self.conn.execute(f"DELETE FROM {table} WHERE life_id = ?", (life_id,))
        if self.fts_enabled:
            for table in ("message_fts", "fact_fts"):
                try:
                    await self.conn.execute(f"DELETE FROM {table} WHERE life_id = ?", (life_id,))
                except Exception:
                    pass
        await self.conn.commit()
        logger.info(f"已清理生命体 {life_id} 的全部持久化数据")

    # ---- 召回 ----------------------------------------------------------
    async def search_text(
        self, table: str, life_id: str, user_id: str, query: str, limit: int = 6
    ) -> list[dict]:
        """关键词召回：FTS5 优先，退化时用 LIKE。

        ``table`` ∈ {``message``, ``fact``}。
        """
        if not query.strip():
            return []
        if self.fts_enabled:
            fts = f"{table}_fts"
            id_col = "message_id" if table == "message" else "fact_id"
            try:
                cursor = await self.conn.execute(
                    f"SELECT {id_col} AS rid FROM {fts} "
                    f"WHERE {fts} MATCH ? AND life_id = ? AND user_id = ? LIMIT ?",
                    (_fts_query(query), life_id, user_id, limit),
                )
                hits = await cursor.fetchall()
                await cursor.close()
                ids = [r["rid"] for r in hits]
                if not ids:
                    return []
                placeholders = ",".join("?" for _ in ids)
                key_col = "id"
                cursor = await self.conn.execute(
                    f"SELECT * FROM {table} WHERE {key_col} IN ({placeholders})", ids
                )
                rows = await cursor.fetchall()
                await cursor.close()
                return [dict(r) for r in rows]
            except Exception:
                logger.debug("FTS 查询失败，退化为 LIKE")
        # LIKE 兜底
        col = "content"
        cursor = await self.conn.execute(
            f"SELECT * FROM {table} WHERE life_id = ? AND user_id = ? AND {col} LIKE ? "
            f"ORDER BY rowid DESC LIMIT ?",
            (life_id, user_id, f"%{query}%", limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]


def _fts_query(text: str) -> str:
    """把用户文本转成安全的 FTS5 查询：按标点/空白切词后用 OR 连接。

    FTS5 的 unicode61 分词对中文按整段处理，这里额外做"字级"切分兜底，
    保证中文也能命中（Demo 阶段够用；后期换 sqlite-vec 时上层零改动）。
    """
    import re

    tokens = [t for t in re.split(r"[\s,，。！？!?、；;:：~～…()（）\[\]【】]+", text) if t]
    # 中文长串再按 2~3 字切，提高召回
    expanded: list[str] = []
    for t in tokens:
        if len(t) > 3 and re.search(r"[\u4e00-\u9fff]", t):
            expanded.extend(t[i : i + 2] for i in range(0, len(t) - 1, 2))
        expanded.append(t)
    uniq = list(dict.fromkeys(expanded))[:12]
    return " OR ".join(f'"{t}"' for t in uniq) if uniq else '""'