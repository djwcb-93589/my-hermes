"""
SQLite 持久化层。

集中处理:schema version 管理、PRAGMA、外键约束、索引、事务原子性、
JSON 序列化 / 反序列化。上层调用方不应直接 ``json.dumps(tool_calls)``,
统一走 ``add_message`` / ``get_session_messages``。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


# ===========================================================================
# schema version / migration
# ===========================================================================

# 当前最新 schema 版本。每次升级表结构时 +1,并在 _migrate 里加对应分支。
# 为什么需要 schema version:让 db 启动时知道结构处于哪个版本,需要的话
# 按顺序执行 migration,避免依赖用户手动删库升级。
LATEST_SCHEMA_VERSION = 9

# 允许的 role 白名单。非法 role 显式报错,不静默吞掉。
_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}


class DBError(Exception):
    """db 层错误基类。"""


class InvalidMessageError(DBError):
    """消息结构非法(role 不允许 / 缺 session_id / 缺 tool_call_id / JSON
    序列化失败等)。"""


# ===========================================================================
# PRAGMA
# ===========================================================================

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """每个连接初始化时设置 PRAGMA。"""
    # foreign_keys=ON 让 messages.session_id → sessions.id 外键约束生效,
    # 不存在的 session_id 不能插入 message。
    conn.execute("PRAGMA foreign_keys = ON")
    # busy_timeout:并发写入时等待 5s 而非立即抛 SQLITE_BUSY。
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL 模式:读写不互斥,适合一写多读。会生成 .db-wal / .db-shm。
    conn.execute("PRAGMA journal_mode = WAL")


# ===========================================================================
# schema 定义
# ===========================================================================

def _create_latest_schema(conn: sqlite3.Connection) -> None:
    """直接建最新版 schema(全新库走这条)。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        -- 为什么需要这个 index:get_session_messages 按 session_id 过滤、
        -- 按 id 排序。复合索引 (session_id, id) 让该查询稳定高效。
        CREATE INDEX IF NOT EXISTS idx_messages_session_order
            ON messages(session_id, id);

        -- Gateway 的 route_key 稳定不变,/new 只切换 conversation_id。
        -- 单独持久化当前映射,让进程重启后仍能恢复到最新会话。
        CREATE TABLE IF NOT EXISTS gateway_session_routes (
            route_key TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        -- Runner 接受但尚未完成的消息。queued / processing 都会在
        -- Gateway 重启后恢复,完成后删除。
        CREATE TABLE IF NOT EXISTS gateway_message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_key TEXT NOT NULL,
            message_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(route_key, message_id)
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_message_queue_status
            ON gateway_message_queue(status, id);

        -- Gateway 已生成但尚未完整送达平台的回复。payloads_json 保存已经
        -- 确定格式和 UUID 的分片,重启后不能重新切分或重新生成 UUID。
        CREATE TABLE IF NOT EXISTS gateway_outbox (
            id TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            reply_to_message_id TEXT,
            thread_id TEXT,
            delivery_kind TEXT NOT NULL,
            payloads_json TEXT NOT NULL,
            next_chunk_index INTEGER NOT NULL DEFAULT 0,
            message_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'sending', 'retry_wait', 'delivered',
                    'cancelled', 'partial_cancelled', 'permanent_failed'
                )
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL,
            last_error TEXT,
            last_error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(route_key, source_message_id, delivery_kind)
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_outbox_status_retry
            ON gateway_outbox(status, next_attempt_at, created_at);

        -- 只关联需要投递给平台用户的最终 assistant 消息。工具调用等内部
        -- assistant 消息没有关联记录，因而不受 Gateway 投递可见性过滤影响。
        CREATE TABLE IF NOT EXISTS gateway_message_deliveries (
            delivery_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            assistant_message_id INTEGER NOT NULL UNIQUE,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'delivered', 'cancelled', 'partial_cancelled',
                    'permanent_failed'
                )
            ),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (delivery_id) REFERENCES gateway_outbox(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_message_deliveries_session_status
            ON gateway_message_deliveries(session_id, status, assistant_message_id);
        """
    )
    _create_gateway_source_message_ownership_schema(conn)
    _create_gateway_runtime_lease_schema(conn)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema version。

    返回 0 表示全新库(还没任何表);返回 1 表示老库(v1 时代还没引入
    schema_version 表);返回 >=2 表示已经过 migration。
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cur.fetchone() is None:
        # schema_version 表不存在 —— 可能是全新库,也可能是 v1 老库
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        if cur.fetchone() is None:
            return 0  # 全新库
        return 1  # v1 老库(只有 sessions/messages,无 schema_version 表)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    # schema_version 只保留一行,避免未来版本号堆叠导致判断含糊。
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _count_rows(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def gateway_event_source_message_ids(
    event_json: str,
    fallback_message_id: str,
) -> list[str]:
    """提取 Gateway event 对应的全部原始平台消息 ID。"""
    try:
        payload = json.loads(event_json)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway event JSON deserialization failed") from exc
    if not isinstance(payload, dict):
        raise DBError("gateway event JSON must contain an object")

    metadata = payload.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise DBError("gateway event metadata must contain an object")
    source_message_ids = metadata.get("source_message_ids", [])
    if source_message_ids is None:
        source_message_ids = []
    if not isinstance(source_message_ids, list):
        raise DBError("gateway event source_message_ids must be a list")

    result: list[str] = []
    seen: set[str] = set()

    def append_message_id(value) -> None:
        if value is None:
            return
        message_id = str(value)
        if not message_id or message_id in seen:
            return
        seen.add(message_id)
        result.append(message_id)

    append_message_id(payload.get("message_id"))
    # 旧记录可能没有 message_id 字段，数据库列中的主消息 ID 必须保留。
    append_message_id(fallback_message_id)
    for source_message_id in source_message_ids:
        append_message_id(source_message_id)

    if not result:
        raise DBError("gateway event has no source message id")
    return result


def _create_gateway_source_message_ownership_schema(
    conn: sqlite3.Connection,
) -> None:
    """创建原始平台消息到当前持久层所有者的规范化索引。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_source_message_ownership (
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            owner_kind TEXT NOT NULL CHECK (
                owner_kind IN ('queue', 'outbox')
            ),
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (route_key, source_message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_source_ownership_owner
        ON gateway_source_message_ownership(owner_kind, owner_id)
        """
    )


def _create_gateway_runtime_lease_schema(conn: sqlite3.Connection) -> None:
    """创建 Gateway 单实例运行租约表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_runtime_lease (
            lease_name TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )


def _upsert_gateway_source_message_ownership(
    conn: sqlite3.Connection,
    route_key: str,
    source_message_ids: list[str],
    *,
    owner_kind: str,
    owner_id: str,
    status: str,
    created_at: float,
    updated_at: float,
) -> None:
    """写入 ownership；Outbox 可接管 Queue，同一 Queue 只能刷新自身。"""
    if owner_kind not in {"queue", "outbox"}:
        raise DBError(f"invalid gateway ownership kind: {owner_kind}")
    if not source_message_ids:
        raise DBError("gateway ownership requires source message ids")

    if owner_kind == "queue":
        conflict_sql = """
            ON CONFLICT(route_key, source_message_id) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at
            WHERE gateway_source_message_ownership.owner_kind='queue'
              AND gateway_source_message_ownership.owner_id=excluded.owner_id
        """
    else:
        conflict_sql = """
            ON CONFLICT(route_key, source_message_id) DO UPDATE SET
                owner_kind=excluded.owner_kind,
                owner_id=excluded.owner_id,
                status=excluded.status,
                updated_at=excluded.updated_at
        """

    for source_message_id in source_message_ids:
        conn.execute(
            f"""
            INSERT INTO gateway_source_message_ownership (
                route_key, source_message_id, owner_kind, owner_id, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            {conflict_sql}
            """,
            (
                route_key,
                source_message_id,
                owner_kind,
                owner_id,
                status,
                float(created_at),
                float(updated_at),
            ),
        )


def _validate_v1_data(conn: sqlite3.Connection) -> None:
    """校验 v1 旧数据能否无损迁移到最新 schema。"""
    if not _table_exists(conn, "sessions") or not _table_exists(conn, "messages"):
        raise DBError("v1 db missing sessions/messages table")

    bad_sessions = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM sessions
        WHERE id IS NULL OR started_at IS NULL
        """,
    )
    if bad_sessions:
        raise DBError(
            f"cannot migrate v1 db: {bad_sessions} invalid session rows"
        )

    bad_messages = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM messages AS m
        LEFT JOIN sessions AS s ON s.id = m.session_id
        WHERE m.session_id IS NULL
           OR m.role IS NULL
           OR m.timestamp IS NULL
           OR s.id IS NULL
        """,
    )
    if bad_messages:
        raise DBError(
            f"cannot migrate v1 db: {bad_messages} invalid or orphan messages"
        )


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2:重建表,让旧库也获得 NOT NULL / 外键约束。"""
    _validate_v1_data(conn)

    backup_tables = ("sessions_v1_backup", "messages_v1_backup")
    if any(_table_exists(conn, table) for table in backup_tables):
        raise DBError("cannot migrate v1 db: leftover migration backup table")

    conn.execute("ALTER TABLE sessions RENAME TO sessions_v1_backup")
    conn.execute("ALTER TABLE messages RENAME TO messages_v1_backup")

    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        INSERT INTO sessions (id, source, started_at)
        SELECT id, source, started_at
        FROM sessions_v1_backup
        """
    )
    conn.execute(
        """
        INSERT INTO messages
            (id, session_id, role, content, tool_calls, tool_call_id, timestamp)
        SELECT id, session_id, role, content, tool_calls, tool_call_id, timestamp
        FROM messages_v1_backup
        """
    )

    conn.execute("DROP TABLE messages_v1_backup")
    conn.execute("DROP TABLE sessions_v1_backup")


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """为部分取消增加显式状态，并修正旧 cancelled 行的可推导语义。"""
    outbox_rows = conn.execute(
        """
        SELECT id, route_key, source_message_id, event_json, platform, chat_id,
               reply_to_message_id, thread_id, delivery_kind, payloads_json,
               next_chunk_index, message_ids_json, status, attempt_count,
               next_attempt_at, last_error, last_error_code, created_at, updated_at
        FROM gateway_outbox
        ORDER BY created_at, id
        """
    ).fetchall()
    delivery_rows = conn.execute(
        """
        SELECT delivery_id, session_id, assistant_message_id, route_key,
               source_message_id, status, created_at, updated_at
        FROM gateway_message_deliveries
        """
    ).fetchall()

    reconciled_outbox_rows = []
    reconciled_statuses: dict[str, str] = {}
    for row in outbox_rows:
        values = list(row)
        outbox_id = str(values[0])
        next_chunk_index = int(values[10])
        status = str(values[12])
        if status == "cancelled" and next_chunk_index > 0:
            try:
                payloads = json.loads(values[9])
            except (TypeError, ValueError):
                payloads = None
            if (
                isinstance(payloads, list)
                and payloads
                and next_chunk_index >= len(payloads)
            ):
                status = "delivered"
            else:
                status = "partial_cancelled"
            values[12] = status
        reconciled_statuses[outbox_id] = status
        reconciled_outbox_rows.append(tuple(values))

    reconciled_delivery_rows = []
    for row in delivery_rows:
        values = list(row)
        delivery_id = str(values[0])
        if values[5] == "cancelled":
            outbox_status = reconciled_statuses.get(delivery_id)
            if outbox_status in {"delivered", "partial_cancelled"}:
                values[5] = outbox_status
        reconciled_delivery_rows.append(tuple(values))

    conn.execute(
        "ALTER TABLE gateway_message_deliveries "
        "RENAME TO gateway_message_deliveries_v6_backup"
    )
    conn.execute(
        "ALTER TABLE gateway_outbox RENAME TO gateway_outbox_v6_backup"
    )
    conn.execute(
        """
        CREATE TABLE gateway_outbox (
            id TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            reply_to_message_id TEXT,
            thread_id TEXT,
            delivery_kind TEXT NOT NULL,
            payloads_json TEXT NOT NULL,
            next_chunk_index INTEGER NOT NULL DEFAULT 0,
            message_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'sending', 'retry_wait', 'delivered',
                    'cancelled', 'partial_cancelled', 'permanent_failed'
                )
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL,
            last_error TEXT,
            last_error_code TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(route_key, source_message_id, delivery_kind)
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO gateway_outbox (
            id, route_key, source_message_id, event_json, platform, chat_id,
            reply_to_message_id, thread_id, delivery_kind, payloads_json,
            next_chunk_index, message_ids_json, status, attempt_count,
            next_attempt_at, last_error, last_error_code, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        reconciled_outbox_rows,
    )
    conn.execute(
        """
        CREATE TABLE gateway_message_deliveries (
            delivery_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            assistant_message_id INTEGER NOT NULL UNIQUE,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'delivered', 'cancelled', 'partial_cancelled',
                    'permanent_failed'
                )
            ),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (delivery_id) REFERENCES gateway_outbox(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (assistant_message_id)
                REFERENCES messages(id) ON DELETE CASCADE
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO gateway_message_deliveries (
            delivery_id, session_id, assistant_message_id, route_key,
            source_message_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        reconciled_delivery_rows,
    )
    conn.execute("DROP TABLE gateway_message_deliveries_v6_backup")
    conn.execute("DROP TABLE gateway_outbox_v6_backup")
    conn.execute(
        "CREATE INDEX idx_gateway_outbox_status_retry "
        "ON gateway_outbox(status, next_attempt_at, created_at)"
    )
    conn.execute(
        "CREATE INDEX idx_gateway_message_deliveries_session_status "
        "ON gateway_message_deliveries("
        "session_id, status, assistant_message_id)"
    )


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """建立原始消息归属索引，并一次性回填 Queue 与 Outbox。"""
    _create_gateway_source_message_ownership_schema(conn)

    queue_rows = conn.execute(
        """
        SELECT route_key, message_id, event_json, status,
               created_at, updated_at
        FROM gateway_message_queue
        ORDER BY id
        """
    ).fetchall()
    for (
        route_key,
        message_id,
        event_json,
        status,
        created_at,
        updated_at,
    ) in queue_rows:
        source_message_ids = gateway_event_source_message_ids(
            str(event_json),
            str(message_id),
        )
        _upsert_gateway_source_message_ownership(
            conn,
            str(route_key),
            source_message_ids,
            owner_kind="queue",
            owner_id=str(message_id),
            status=str(status),
            created_at=float(created_at),
            updated_at=float(updated_at),
        )

    # Outbox 后写，确保模型已经完成的消息不会被旧 Queue 重新认领。
    outbox_rows = conn.execute(
        """
        SELECT id, route_key, source_message_id, event_json, status,
               created_at, updated_at
        FROM gateway_outbox
        ORDER BY created_at, id
        """
    ).fetchall()
    for (
        outbox_id,
        route_key,
        source_message_id,
        event_json,
        status,
        created_at,
        updated_at,
    ) in outbox_rows:
        source_message_ids = gateway_event_source_message_ids(
            str(event_json),
            str(source_message_id),
        )
        _upsert_gateway_source_message_ownership(
            conn,
            str(route_key),
            source_message_ids,
            owner_kind="outbox",
            owner_id=str(outbox_id),
            status=str(status),
            created_at=float(created_at),
            updated_at=float(updated_at),
        )


def _migrate_v8_to_v9(conn: sqlite3.Connection) -> None:
    """增加 Gateway 单实例运行租约。"""
    _create_gateway_runtime_lease_schema(conn)


def _migrate(conn: sqlite3.Connection, current: int) -> int:
    """按版本号顺序执行 migration,返回最新版本。

    老库 v1 → v2 会重建 sessions/messages,让外键 / NOT NULL
    约束对既有数据库也生效。v2 → v3 新增 Gateway 当前会话映射,
    v3 → v4 新增 Gateway 待处理消息队列,v4 → v5 新增出站回复队列,
    v5 → v6 关联最终回答投递状态,v6 → v7 区分部分取消,
    v7 → v8 增加原始平台消息归属索引，v8 → v9 增加 Gateway 运行租约。
    旧数据不满足新约束时拒绝迁移。
    """
    if current < 1:
        # 极少见:有 schema_version 表但版本 < 1,补基础表
        _create_latest_schema(conn)
        current = LATEST_SCHEMA_VERSION

    if current < 2:
        # DDL 不依赖 sqlite3.Connection 的 with 协议,这里显式开事务,
        # 避免重建表中途失败时留下半迁移状态。
        conn.execute("BEGIN")
        try:
            _migrate_v1_to_v2(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version("
                "version INTEGER PRIMARY KEY)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session_order "
                "ON messages(session_id, id)"
            )
            _set_schema_version(conn, 2)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 2

    if current < 3:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_session_routes (
                    route_key TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            _set_schema_version(conn, 3)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 3

    if current < 4:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_message_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(route_key, message_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_message_queue_status "
                "ON gateway_message_queue(status, id)"
            )
            _set_schema_version(conn, 4)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 4

    if current < 5:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_outbox (
                    id TEXT PRIMARY KEY,
                    route_key TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    reply_to_message_id TEXT,
                    thread_id TEXT,
                    delivery_kind TEXT NOT NULL,
                    payloads_json TEXT NOT NULL,
                    next_chunk_index INTEGER NOT NULL DEFAULT 0,
                    message_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    last_error TEXT,
                    last_error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(route_key, source_message_id, delivery_kind)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gateway_outbox_status_retry "
                "ON gateway_outbox(status, next_attempt_at, created_at)"
            )
            _set_schema_version(conn, 5)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 5

    if current < 6:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_message_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    assistant_message_id INTEGER NOT NULL UNIQUE,
                    route_key TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'delivered', 'cancelled', 'permanent_failed'
                        )
                    ),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (delivery_id)
                        REFERENCES gateway_outbox(id),
                    FOREIGN KEY (session_id)
                        REFERENCES sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY (assistant_message_id)
                        REFERENCES messages(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_gateway_message_deliveries_session_status "
                "ON gateway_message_deliveries("
                "session_id, status, assistant_message_id)"
            )
            _set_schema_version(conn, 6)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 6

    if current < 7:
        conn.execute("BEGIN")
        try:
            _migrate_v6_to_v7(conn)
            _set_schema_version(conn, 7)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 7

    if current < 8:
        conn.execute("BEGIN")
        try:
            _migrate_v7_to_v8(conn)
            _set_schema_version(conn, 8)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 8

    if current < 9:
        conn.execute("BEGIN")
        try:
            _migrate_v8_to_v9(conn)
            _set_schema_version(conn, 9)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 9

    return current


# ===========================================================================
# init_db
# ===========================================================================

def _infer_cancelled_gateway_outbox_status(
    next_chunk_index: int,
    payloads_json: str,
) -> str:
    """按已经持久化的发送进度推导取消类 Outbox 的真实终态。"""
    if int(next_chunk_index) <= 0:
        return "cancelled"
    try:
        payloads = json.loads(payloads_json)
    except (TypeError, ValueError):
        # 损坏 payload 仍保留审计；已有成功进度时至少不能降回完全未发送。
        return "partial_cancelled"
    if not isinstance(payloads, list):
        return "partial_cancelled"
    if payloads and int(next_chunk_index) >= len(payloads):
        return "delivered"
    return "partial_cancelled"


def reconcile_gateway_terminal_deliveries(conn: sqlite3.Connection) -> int:
    """收敛旧版本遗留的终态 Outbox 与 ``reply_pending`` queue。

    新代码通过统一终态函数一次提交三层状态；这里仅修复升级前已经形成的
    孤儿记录，以及“取消先提交、最后一个平台成功随后落进度”留下的可推导
    状态。终态审计行不会被删除。
    """
    rows = conn.execute(
        """
        SELECT o.id, o.route_key, o.source_message_id, o.status,
               o.next_chunk_index, o.payloads_json,
               q.status, o.event_json
        FROM gateway_outbox AS o
        LEFT JOIN gateway_message_queue AS q
          ON q.route_key=o.route_key
         AND q.message_id=o.source_message_id
        WHERE (
            o.status IN (
                'delivered', 'permanent_failed',
                'cancelled', 'partial_cancelled'
            )
            AND q.status='reply_pending'
        )
        OR (o.status='cancelled' AND o.next_chunk_index > 0)
        OR (
            o.status='partial_cancelled'
            AND json_valid(o.payloads_json)=1
            AND o.next_chunk_index >= json_array_length(o.payloads_json)
        )
        ORDER BY o.created_at, o.id
        """
    ).fetchall()
    if not rows:
        return 0

    reconciled = 0
    now = time.time()
    with transaction(conn):
        for (
            outbox_id,
            route_key,
            source_message_id,
            stored_status,
            next_chunk_index,
            payloads_json,
            queue_status,
            event_json,
        ) in rows:
            status = str(stored_status)
            if status in {"cancelled", "partial_cancelled"}:
                status = _infer_cancelled_gateway_outbox_status(
                    int(next_chunk_index),
                    str(payloads_json),
                )
            if status != stored_status:
                cursor = conn.execute(
                    """
                    UPDATE gateway_outbox
                    SET status=?, next_attempt_at=NULL,
                        last_error=CASE WHEN ?='delivered' THEN NULL
                                        ELSE last_error END,
                        last_error_code=CASE WHEN ?='delivered' THEN NULL
                                             ELSE last_error_code END,
                        updated_at=?
                    WHERE id=? AND status=?
                    """,
                    (
                        status,
                        status,
                        status,
                        now,
                        outbox_id,
                        stored_status,
                    ),
                )
                if cursor.rowcount <= 0:
                    continue

            _mark_gateway_message_delivery_terminal(
                conn,
                str(outbox_id),
                status,
                now,
                route_key=str(route_key),
                source_message_id=str(source_message_id),
            )
            _update_gateway_outbox_ownership_status(
                conn,
                outbox_id=str(outbox_id),
                route_key=str(route_key),
                source_message_id=str(source_message_id),
                event_json=str(event_json),
                status=status,
                updated_at=now,
            )
            if queue_status == "reply_pending":
                if status == "permanent_failed":
                    conn.execute(
                        """
                        UPDATE gateway_message_queue
                        SET status='delivery_failed', updated_at=?
                        WHERE route_key=? AND message_id=?
                          AND status='reply_pending'
                        """,
                        (now, route_key, source_message_id),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM gateway_message_queue
                        WHERE route_key=? AND message_id=?
                          AND status='reply_pending'
                        """,
                        (route_key, source_message_id),
                    )
            reconciled += 1
    return reconciled


def init_db(db_path: str) -> sqlite3.Connection:
    """初始化 db:建父目录、应用 PRAGMA、检查 schema version 并按需 migration。

    全新库直接建最新 schema;老库按序 migration。返回 sqlite3 连接。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        _apply_pragmas(conn)

        current = _get_schema_version(conn)
        if current > LATEST_SCHEMA_VERSION:
            raise DBError(
                "db schema version is newer than this code supports: "
                f"{current} > {LATEST_SCHEMA_VERSION}"
            )
        if current == 0:
            # 全新库:直接建最新 schema
            _create_latest_schema(conn)
            _set_schema_version(conn, LATEST_SCHEMA_VERSION)
        elif current < LATEST_SCHEMA_VERSION:
            _migrate(conn, current)

        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


# ===========================================================================
# 序列化 / 反序列化(集中处理,上层不直接 json.dumps)
# ===========================================================================

def _serialize_tool_calls(tool_calls) -> str | None:
    """把 tool_calls 序列化成 db 字符串。

    None / 空列表 → NULL(避免反序列化时把 [] 当成有意义数据)。
    JSON 失败抛 InvalidMessageError,不静默吞掉。
    """
    if tool_calls is None:
        return None
    if not isinstance(tool_calls, list):
        raise InvalidMessageError(
            f"tool_calls must be a list, got {type(tool_calls).__name__}"
        )
    if not tool_calls:
        return None
    try:
        return json.dumps(tool_calls)
    except (TypeError, ValueError) as exc:
        raise InvalidMessageError(
            f"tool_calls JSON serialization failed: {exc}"
        ) from exc


def _deserialize_tool_calls(s: str | None):
    """从 db 字符串还原 tool_calls。失败抛 InvalidMessageError。"""
    if not s:
        return None
    try:
        return json.loads(s)
    except ValueError as exc:
        raise InvalidMessageError(
            f"tool_calls JSON deserialization failed: {exc}"
        ) from exc


# ===========================================================================
# 事务上下文管理器
# ===========================================================================

@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """事务上下文:进入时开事务,正常退出 COMMIT,异常 ROLLBACK。

    为什么 add_messages 要用事务:批量写入一组消息时,如果中间某条
    失败(结构非法 / 序列化失败 / 外键约束 / 磁盘满等),整组必须回滚,
    不能留下半截消息破坏历史一致性。

    注意:工具执行失败不是事务失败 —— 那种情况应该把错误包装成 tool
    message 正常写入上下文和数据库(让模型看到工具错误)。事务回滚
    只针对真正持久化失败场景。
    """
    # sqlite3.Connection 的 with 协议:__enter__ 返回 self,正常退出
    # commit,异常 rollback。直接复用最简洁,避免与 Python 自动事务
    # 管理冲突。
    with conn:
        yield


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """以写锁开始事务，供跨进程竞争同一租约行。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _gateway_runtime_lease_values(
    lease_name: str,
    instance_id: str,
    ttl_seconds: float,
) -> tuple[str, str, float]:
    """校验租约标识和 TTL，避免写入不可接管的无效记录。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    if isinstance(ttl_seconds, bool):
        raise DBError("gateway runtime lease ttl must be greater than 0")
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway runtime lease ttl must be a number") from exc
    if ttl <= 0:
        raise DBError("gateway runtime lease ttl must be greater than 0")
    return lease_name, instance_id, ttl


def acquire_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    ttl_seconds: float,
) -> bool:
    """原子获取、接管过期租约，或为同一实例续租。"""
    lease_name, instance_id, ttl = _gateway_runtime_lease_values(
        lease_name,
        instance_id,
        ttl_seconds,
    )
    now = time.time()
    with _immediate_transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO gateway_runtime_lease (
                lease_name, instance_id, heartbeat_at, expires_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(lease_name) DO UPDATE SET
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                expires_at=excluded.expires_at
            WHERE gateway_runtime_lease.instance_id=excluded.instance_id
               OR gateway_runtime_lease.expires_at<=excluded.heartbeat_at
            """,
            (lease_name, instance_id, now, now + ttl),
        )
        return cursor.rowcount == 1


def renew_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    ttl_seconds: float,
) -> bool:
    """仅允许当前持有者刷新 heartbeat 和过期时间。"""
    lease_name, instance_id, ttl = _gateway_runtime_lease_values(
        lease_name,
        instance_id,
        ttl_seconds,
    )
    now = time.time()
    with _immediate_transaction(conn):
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?, expires_at=?
            WHERE lease_name=? AND instance_id=?
            """,
            (now, now + ttl, lease_name, instance_id),
        )
        return cursor.rowcount == 1


def release_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
) -> bool:
    """仅删除当前实例持有的租约，不影响已经接管的新实例。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    with _immediate_transaction(conn):
        cursor = conn.execute(
            """
            DELETE FROM gateway_runtime_lease
            WHERE lease_name=? AND instance_id=?
            """,
            (lease_name, instance_id),
        )
        return cursor.rowcount == 1


# ===========================================================================
# CRUD
# ===========================================================================

def create_session(conn: sqlite3.Connection, source: str = "cli") -> str:
    """创建新 session,返回其 ID。"""
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        (session_id, source, time.time()),
    )
    conn.commit()
    return session_id


def ensure_session(
    conn: sqlite3.Connection,
    session_id: str,
    source: str = "gateway",
) -> None:
    """确保 session 存在,不存在则创建。Gateway / runner 统一走这个入口,
    不在上层自行拼 SQL。
    """
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
        (session_id, source, time.time()),
    )
    conn.commit()


def get_gateway_conversation_id(
    conn: sqlite3.Connection,
    route_key: str,
) -> str | None:
    """读取 route_key 当前指向的 conversation_id。没有映射时返回 None。"""
    row = conn.execute(
        "SELECT conversation_id FROM gateway_session_routes WHERE route_key=?",
        (route_key,),
    ).fetchone()
    return str(row[0]) if row else None


def set_gateway_conversation_id(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> None:
    """持久化 route_key 当前指向的 conversation_id。"""
    conn.execute(
        """
        INSERT INTO gateway_session_routes
            (route_key, conversation_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(route_key) DO UPDATE SET
            conversation_id=excluded.conversation_id,
            updated_at=excluded.updated_at
        """,
        (route_key, conversation_id, time.time()),
    )
    conn.commit()


def enqueue_gateway_message(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    event_json: str,
) -> bool:
    """原子写入 Runner queue 与全部原始消息的 Queue 归属。"""
    incoming_source_ids = gateway_event_source_message_ids(
        event_json,
        message_id,
    )
    now = time.time()
    with transaction(conn):
        placeholders = ",".join("?" for _ in incoming_source_ids)
        existing_owners = conn.execute(
            f"""
            SELECT source_message_id, owner_kind, owner_id
            FROM gateway_source_message_ownership
            WHERE route_key=?
              AND source_message_id IN ({placeholders})
            """,
            (route_key, *incoming_source_ids),
        ).fetchall()
        for _source_message_id, owner_kind, owner_id in existing_owners:
            if str(owner_kind) == "outbox" or str(owner_id) != message_id:
                return False

        conn.execute(
            """
            INSERT OR IGNORE INTO gateway_message_queue
                (route_key, message_id, event_json, status, created_at, updated_at)
            VALUES (?, ?, ?, 'queued', ?, ?)
            """,
            (route_key, message_id, event_json, now, now),
        )
        row = conn.execute(
            """
            SELECT event_json, status, created_at, updated_at
            FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, message_id),
        ).fetchone()
        if row is None:
            raise DBError("gateway queue insert did not create a row")
        source_message_ids = gateway_event_source_message_ids(
            str(row[0]),
            message_id,
        )
        _upsert_gateway_source_message_ownership(
            conn,
            route_key,
            source_message_ids,
            owner_kind="queue",
            owner_id=message_id,
            status=str(row[1]),
            created_at=float(row[2]),
            updated_at=float(row[3]),
        )
    return True


def _update_gateway_source_message_ownership_status(
    conn: sqlite3.Connection,
    *,
    route_key: str,
    event_json: str,
    fallback_message_id: str,
    owner_kind: str,
    owner_id: str,
    status: str,
    updated_at: float,
) -> None:
    """只更新仍属于指定 owner 的原始消息，避免旧任务覆盖新所有者。"""
    source_message_ids = gateway_event_source_message_ids(
        event_json,
        fallback_message_id,
    )
    for source_message_id in source_message_ids:
        conn.execute(
            """
            UPDATE gateway_source_message_ownership
            SET status=?, updated_at=?
            WHERE route_key=? AND source_message_id=?
              AND owner_kind=? AND owner_id=?
            """,
            (
                status,
                float(updated_at),
                route_key,
                source_message_id,
                owner_kind,
                owner_id,
            ),
        )


def _set_gateway_queue_status(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    status: str,
    now: float,
) -> bool:
    """更新 Queue 及其原始消息归属；调用方负责事务。"""
    row = conn.execute(
        """
        SELECT event_json
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        return False
    cursor = conn.execute(
        """
        UPDATE gateway_message_queue
        SET status=?, updated_at=?
        WHERE route_key=? AND message_id=?
        """,
        (status, now, route_key, message_id),
    )
    if cursor.rowcount <= 0:
        return False
    _update_gateway_source_message_ownership_status(
        conn,
        route_key=route_key,
        event_json=str(row[0]),
        fallback_message_id=message_id,
        owner_kind="queue",
        owner_id=message_id,
        status=status,
        updated_at=now,
    )
    return True


def mark_gateway_message_processing(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """标记消息已由当前 worker 开始处理。"""
    with transaction(conn):
        _set_gateway_queue_status(
            conn,
            route_key,
            message_id,
            "processing",
            time.time(),
        )


def mark_gateway_message_reply_pending(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """模型已完成,入站消息等待对应 outbox 完整送达。"""
    with transaction(conn):
        _set_gateway_queue_status(
            conn,
            route_key,
            message_id,
            "reply_pending",
            time.time(),
        )


def mark_gateway_message_delivery_failed(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """出站永久失败后保留入站审计记录,但不再重新调用模型。"""
    with transaction(conn):
        _set_gateway_queue_status(
            conn,
            route_key,
            message_id,
            "delivery_failed",
            time.time(),
        )


def complete_gateway_message(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """删除 Queue 行，但保留 completed ownership 作为长期去重事实。"""
    with transaction(conn):
        row = conn.execute(
            """
            SELECT event_json
            FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, message_id),
        ).fetchone()
        if row is None:
            return
        now = time.time()
        _update_gateway_source_message_ownership_status(
            conn,
            route_key=route_key,
            event_json=str(row[0]),
            fallback_message_id=message_id,
            owner_kind="queue",
            owner_id=message_id,
            status="completed",
            updated_at=now,
        )
        conn.execute(
            """
            DELETE FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, message_id),
        )


def delete_gateway_messages(
    conn: sqlite3.Connection,
    route_key: str,
    message_ids: list[str],
) -> None:
    """删除被 /new 取消的 Queue 行，并保留 cancelled ownership。"""
    if not message_ids:
        return
    placeholders = ",".join("?" for _ in message_ids)
    with transaction(conn):
        rows = conn.execute(
            f"""
            SELECT message_id, event_json
            FROM gateway_message_queue
            WHERE route_key=? AND message_id IN ({placeholders})
            """,
            (route_key, *message_ids),
        ).fetchall()
        now = time.time()
        for stored_message_id, event_json in rows:
            _update_gateway_source_message_ownership_status(
                conn,
                route_key=route_key,
                event_json=str(event_json),
                fallback_message_id=str(stored_message_id),
                owner_kind="queue",
                owner_id=str(stored_message_id),
                status="cancelled",
                updated_at=now,
            )
        conn.execute(
            f"""
            DELETE FROM gateway_message_queue
            WHERE route_key=? AND message_id IN ({placeholders})
            """,
            (route_key, *message_ids),
        )


def reset_gateway_processing_messages(conn: sqlite3.Connection) -> None:
    """启动恢复前把上次中断的 processing 重新置为 queued。"""
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT route_key, message_id, event_json
            FROM gateway_message_queue
            WHERE status='processing'
            """
        ).fetchall()
        now = time.time()
        conn.execute(
            """
            UPDATE gateway_message_queue
            SET status='queued', updated_at=?
            WHERE status='processing'
            """,
            (now,),
        )
        for route_key, message_id, event_json in rows:
            _update_gateway_source_message_ownership_status(
                conn,
                route_key=str(route_key),
                event_json=str(event_json),
                fallback_message_id=str(message_id),
                owner_kind="queue",
                owner_id=str(message_id),
                status="queued",
                updated_at=now,
            )


def get_gateway_queued_messages(conn: sqlite3.Connection) -> list[dict]:
    """返回仍需调用模型的消息;等待投递的消息由 outbox 单独恢复。"""
    rows = conn.execute(
        """
        SELECT route_key, message_id, event_json, status
        FROM gateway_message_queue
        WHERE status IN ('queued', 'processing')
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "route_key": route_key,
            "message_id": message_id,
            "event_json": event_json,
            "status": status,
        }
        for route_key, message_id, event_json, status in rows
    ]


def get_gateway_message_persistence_state(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> dict | None:
    """通过规范化 ownership 主键查询平台消息的持久层归属。"""
    row = conn.execute(
        """
        SELECT owner_kind, status, owner_id
        FROM gateway_source_message_ownership
        WHERE route_key=? AND source_message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "layer": str(row[0]),
        "status": str(row[1]),
        "owner_id": str(row[2]),
    }


def _serialize_gateway_json(value, field_name: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DBError(f"{field_name} JSON serialization failed: {exc}") from exc


def _insert_gateway_outbox(
    conn: sqlite3.Connection,
    outbox: dict,
) -> str:
    """插入一条 outbox,不 commit,返回实际使用的 delivery id。"""
    required = (
        "id",
        "route_key",
        "source_message_id",
        "event_json",
        "platform",
        "chat_id",
        "delivery_kind",
        "payloads",
    )
    missing = [name for name in required if not outbox.get(name)]
    if missing:
        raise DBError(f"gateway outbox missing fields: {', '.join(missing)}")
    if not isinstance(outbox["payloads"], list):
        raise DBError("gateway outbox payloads must be a list")

    now = time.time()
    conn.execute(
        """
        INSERT OR IGNORE INTO gateway_outbox (
            id, route_key, source_message_id, event_json, platform, chat_id,
            reply_to_message_id, thread_id, delivery_kind, payloads_json,
            next_chunk_index, message_ids_json, status, attempt_count,
            next_attempt_at, last_error, last_error_code, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', 'pending', 0,
                  NULL, NULL, NULL, ?, ?)
        """,
        (
            str(outbox["id"]),
            str(outbox["route_key"]),
            str(outbox["source_message_id"]),
            str(outbox["event_json"]),
            str(outbox["platform"]),
            str(outbox["chat_id"]),
            outbox.get("reply_to_message_id"),
            outbox.get("thread_id"),
            str(outbox["delivery_kind"]),
            _serialize_gateway_json(outbox["payloads"], "payloads"),
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT id, event_json, status, created_at, updated_at
        FROM gateway_outbox
        WHERE route_key=? AND source_message_id=? AND delivery_kind=?
        """,
        (
            str(outbox["route_key"]),
            str(outbox["source_message_id"]),
            str(outbox["delivery_kind"]),
        ),
    ).fetchone()
    if row is None:
        raise DBError("gateway outbox insert did not create a row")
    outbox_id = str(row[0])
    source_message_ids = gateway_event_source_message_ids(
        str(row[1]),
        str(outbox["source_message_id"]),
    )
    _upsert_gateway_source_message_ownership(
        conn,
        str(outbox["route_key"]),
        source_message_ids,
        owner_kind="outbox",
        owner_id=outbox_id,
        status=str(row[2]),
        created_at=float(row[3]),
        updated_at=float(row[4]),
    )
    return outbox_id


def enqueue_gateway_outbox(
    conn: sqlite3.Connection,
    outbox: dict,
) -> str:
    """持久化回复并把对应入站消息切换到 reply_pending。"""
    with transaction(conn):
        outbox_id = _insert_gateway_outbox(conn, outbox)
        conn.execute(
            """
            UPDATE gateway_message_queue
            SET status='reply_pending', updated_at=?
            WHERE route_key=? AND message_id=?
            """,
            (
                time.time(),
                str(outbox["route_key"]),
                str(outbox["source_message_id"]),
            ),
        )
    return outbox_id


def _gateway_outbox_row(row) -> dict | None:
    if row is None:
        return None
    (
        outbox_id,
        route_key,
        source_message_id,
        event_json,
        platform,
        chat_id,
        reply_to_message_id,
        thread_id,
        delivery_kind,
        payloads_json,
        next_chunk_index,
        message_ids_json,
        status,
        attempt_count,
        next_attempt_at,
        last_error,
        last_error_code,
    ) = row
    try:
        payloads = json.loads(payloads_json)
        message_ids = json.loads(message_ids_json)
    except (TypeError, ValueError) as exc:
        raise DBError(f"gateway outbox JSON deserialization failed: {exc}") from exc
    if not isinstance(payloads, list) or not isinstance(message_ids, list):
        raise DBError("gateway outbox JSON has invalid structure")
    return {
        "id": str(outbox_id),
        "route_key": str(route_key),
        "source_message_id": str(source_message_id),
        "event_json": str(event_json),
        "platform": str(platform),
        "chat_id": str(chat_id),
        "reply_to_message_id": reply_to_message_id,
        "thread_id": thread_id,
        "delivery_kind": str(delivery_kind),
        "payloads": payloads,
        "next_chunk_index": int(next_chunk_index),
        "message_ids": [str(item) for item in message_ids],
        "status": str(status),
        "attempt_count": int(attempt_count),
        "next_attempt_at": next_attempt_at,
        "last_error": last_error,
        "last_error_code": last_error_code,
    }


_GATEWAY_OUTBOX_COLUMNS = """
    id, route_key, source_message_id, event_json, platform, chat_id,
    reply_to_message_id, thread_id, delivery_kind, payloads_json,
    next_chunk_index, message_ids_json, status, attempt_count,
    next_attempt_at, last_error, last_error_code
"""


def get_gateway_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> dict | None:
    """按 delivery id 读取一条 outbox。"""
    row = conn.execute(
        f"SELECT {_GATEWAY_OUTBOX_COLUMNS} FROM gateway_outbox WHERE id=?",
        (outbox_id,),
    ).fetchone()
    return _gateway_outbox_row(row)


def get_recoverable_gateway_outbox(
    conn: sqlite3.Connection,
) -> list[dict]:
    """按创建顺序读取启动后需要恢复的出站回复。"""
    rows = conn.execute(
        f"""
        SELECT {_GATEWAY_OUTBOX_COLUMNS}
        FROM gateway_outbox
        WHERE status IN ('pending', 'sending', 'retry_wait')
        ORDER BY created_at, id
        """
    ).fetchall()
    recovered = []
    for row in rows:
        try:
            recovered.append(_gateway_outbox_row(row))
        except DBError as exc:
            # 保留损坏行供审计；Runner 会隔离对应 route，不让单行 JSON
            # 损坏阻断其他 route 的恢复。
            recovered.append({
                "id": str(row[0]),
                "route_key": str(row[1]),
                "source_message_id": str(row[2]),
                "event_json": str(row[3]),
                "platform": str(row[4]),
                "status": str(row[12]),
                "recovery_error": type(exc).__name__,
            })
    return recovered


def get_gateway_routes_with_pending_outbox(
    conn: sqlite3.Connection,
) -> set[str]:
    """返回仍有待投递 Outbox 的 route，供内存会话清理保护。"""
    rows = conn.execute(
        """
        SELECT DISTINCT route_key
        FROM gateway_outbox
        WHERE status IN ('pending', 'sending', 'retry_wait')
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def get_next_recoverable_gateway_outbox_for_route(
    conn: sqlite3.Connection,
    route_key: str,
) -> dict | None:
    """读取某一路由下一条可投递 Outbox，供运行期串行 worker 接力。"""
    row = conn.execute(
        f"""
        SELECT {_GATEWAY_OUTBOX_COLUMNS}
        FROM gateway_outbox
        WHERE route_key=? AND status IN ('pending', 'sending', 'retry_wait')
        ORDER BY created_at, id
        LIMIT 1
        """,
        (route_key,),
    ).fetchone()
    return _gateway_outbox_row(row)


def _update_gateway_outbox_ownership_status(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    event_json: str,
    status: str,
    updated_at: float,
) -> None:
    """同步仍由指定 Outbox 管理的全部原始消息状态。"""
    _update_gateway_source_message_ownership_status(
        conn,
        route_key=route_key,
        event_json=event_json,
        fallback_message_id=source_message_id,
        owner_kind="outbox",
        owner_id=outbox_id,
        status=status,
        updated_at=updated_at,
    )


def reset_gateway_sending_outbox(conn: sqlite3.Connection) -> None:
    """重启时把中断的 sending 恢复为 pending。"""
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT id, route_key, source_message_id, event_json
            FROM gateway_outbox
            WHERE status='sending'
            """
        ).fetchall()
        now = time.time()
        conn.execute(
            """
            UPDATE gateway_outbox
            SET status='pending', next_attempt_at=NULL, updated_at=?
            WHERE status='sending'
            """,
            (now,),
        )
        for outbox_id, route_key, source_message_id, event_json in rows:
            _update_gateway_outbox_ownership_status(
                conn,
                outbox_id=str(outbox_id),
                route_key=str(route_key),
                source_message_id=str(source_message_id),
                event_json=str(event_json),
                status="pending",
                updated_at=now,
            )


def mark_gateway_outbox_sending(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> bool:
    with transaction(conn):
        row = conn.execute(
            """
            SELECT route_key, source_message_id, event_json, status
            FROM gateway_outbox
            WHERE id=?
            """,
            (outbox_id,),
        ).fetchone()
        if row is None or str(row[3]) not in {
            "pending",
            "sending",
            "retry_wait",
        }:
            return False
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE gateway_outbox
            SET status='sending', updated_at=?
            WHERE id=? AND status=?
            """,
            (now, outbox_id, str(row[3])),
        )
        if cursor.rowcount <= 0:
            return False
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=str(row[0]),
            source_message_id=str(row[1]),
            event_json=str(row[2]),
            status="sending",
            updated_at=now,
        )
    return True


def mark_gateway_outbox_chunk_sent(
    conn: sqlite3.Connection,
    outbox_id: str,
    next_chunk_index: int,
    message_ids: list[str],
    total_chunks: int | None = None,
) -> bool:
    """只记录平台成功事实；终态由统一 delivery 事务负责收敛。"""
    saved_index = int(next_chunk_index)
    if total_chunks is None:
        row = conn.execute(
            "SELECT payloads_json FROM gateway_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            payloads = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway outbox JSON deserialization failed: {exc}"
            ) from exc
        if not isinstance(payloads, list):
            raise DBError("gateway outbox payloads JSON has invalid structure")
        total_chunks = len(payloads)
    total_chunks = int(total_chunks)
    if saved_index <= 0 or saved_index > total_chunks:
        raise DBError("gateway outbox chunk progress is out of range")

    cursor = conn.execute(
        """
        UPDATE gateway_outbox
        SET next_chunk_index=?, message_ids_json=?, updated_at=?
        WHERE id=?
          AND next_chunk_index <= ?
          AND status IN (
              'pending', 'sending', 'retry_wait',
              'cancelled', 'partial_cancelled'
          )
        """,
        (
            saved_index,
            _serialize_gateway_json(message_ids, "message_ids"),
            time.time(),
            outbox_id,
            saved_index,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_gateway_outbox_retry(
    conn: sqlite3.Connection,
    outbox_id: str,
    error: str,
    error_code: str | None,
    next_attempt_at: float,
) -> bool:
    with transaction(conn):
        row = conn.execute(
            """
            SELECT route_key, source_message_id, event_json, status
            FROM gateway_outbox
            WHERE id=?
            """,
            (outbox_id,),
        ).fetchone()
        if row is None or str(row[3]) != "sending":
            return False
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE gateway_outbox
            SET status='retry_wait', attempt_count=attempt_count + 1,
                next_attempt_at=?, last_error=?, last_error_code=?, updated_at=?
            WHERE id=? AND status='sending'
            """,
            (
                next_attempt_at,
                error,
                error_code,
                now,
                outbox_id,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=str(row[0]),
            source_message_id=str(row[1]),
            event_json=str(row[2]),
            status="retry_wait",
            updated_at=now,
        )
    return True


def _gateway_terminal_outbox_row(
    conn: sqlite3.Connection,
    outbox_id: str,
):
    return conn.execute(
        """
        SELECT route_key, source_message_id, status,
               next_chunk_index, payloads_json, event_json
        FROM gateway_outbox
        WHERE id=?
        """,
        (outbox_id,),
    ).fetchone()


def _validate_gateway_delivery_identity(
    outbox_id: str,
    row,
    route_key: str,
    source_message_id: str,
) -> None:
    if str(row[0]) != route_key or str(row[1]) != source_message_id:
        raise DBError(
            "gateway delivery identity mismatch "
            f"for outbox {outbox_id}"
        )


def complete_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
) -> bool:
    """原子完成 Outbox、assistant delivery，并删除对应入站 queue。"""
    now = time.time()
    with transaction(conn):
        row = _gateway_terminal_outbox_row(conn, outbox_id)
        if row is None:
            return False
        _validate_gateway_delivery_identity(
            outbox_id,
            row,
            route_key,
            source_message_id,
        )
        old_status = str(row[2])
        if old_status not in {
            "pending",
            "sending",
            "retry_wait",
            "cancelled",
            "partial_cancelled",
        }:
            return False
        try:
            payloads = json.loads(row[4])
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway outbox JSON deserialization failed: {exc}"
            ) from exc
        if not isinstance(payloads, list):
            raise DBError("gateway outbox payloads JSON has invalid structure")
        if int(row[3]) < len(payloads):
            return False

        cursor = conn.execute(
            """
            UPDATE gateway_outbox
            SET status='delivered', next_attempt_at=NULL,
                last_error=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            """,
            (
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _mark_gateway_message_delivery_terminal(
            conn,
            outbox_id,
            "delivered",
            now,
            route_key=route_key,
            source_message_id=source_message_id,
        )
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=route_key,
            source_message_id=source_message_id,
            event_json=str(row[5]),
            status="delivered",
            updated_at=now,
        )
        conn.execute(
            """
            DELETE FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, source_message_id),
        )
    return True


def fail_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    error: str,
    error_code: str | None,
) -> bool:
    """原子持久化永久失败，并把入站 queue 留作失败审计。"""
    safe_error = str(error)[:120]
    safe_error_code = str(error_code)[:120] if error_code is not None else None
    now = time.time()
    with transaction(conn):
        row = _gateway_terminal_outbox_row(conn, outbox_id)
        if row is None:
            return False
        _validate_gateway_delivery_identity(
            outbox_id,
            row,
            route_key,
            source_message_id,
        )
        old_status = str(row[2])
        if old_status not in {"pending", "sending", "retry_wait"}:
            return False
        cursor = conn.execute(
            """
            UPDATE gateway_outbox
            SET status='permanent_failed', last_error=?, last_error_code=?,
                next_attempt_at=NULL, updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            """,
            (
                safe_error,
                safe_error_code,
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _mark_gateway_message_delivery_terminal(
            conn,
            outbox_id,
            "permanent_failed",
            now,
            route_key=route_key,
            source_message_id=source_message_id,
        )
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=route_key,
            source_message_id=source_message_id,
            event_json=str(row[5]),
            status="permanent_failed",
            updated_at=now,
        )
        conn.execute(
            """
            UPDATE gateway_message_queue
            SET status='delivery_failed', updated_at=?
            WHERE route_key=? AND message_id=?
            """,
            (now, route_key, source_message_id),
        )
    return True


def cancel_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
) -> bool:
    """按成功进度原子取消剩余投递，并删除对应入站 queue。"""
    now = time.time()
    with transaction(conn):
        row = _gateway_terminal_outbox_row(conn, outbox_id)
        if row is None:
            return False
        _validate_gateway_delivery_identity(
            outbox_id,
            row,
            route_key,
            source_message_id,
        )
        old_status = str(row[2])
        if old_status not in {
            "pending",
            "sending",
            "retry_wait",
            "cancelled",
            "partial_cancelled",
        }:
            return False
        status = _infer_cancelled_gateway_outbox_status(
            int(row[3]),
            str(row[4]),
        )
        if status == old_status:
            return False

        cursor = conn.execute(
            """
            UPDATE gateway_outbox
            SET status=?, next_attempt_at=NULL,
                last_error=CASE WHEN ?='delivered' THEN NULL
                                ELSE last_error END,
                last_error_code=CASE WHEN ?='delivered' THEN NULL
                                     ELSE last_error_code END,
                updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            """,
            (
                status,
                status,
                status,
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _mark_gateway_message_delivery_terminal(
            conn,
            outbox_id,
            status,
            now,
            route_key=route_key,
            source_message_id=source_message_id,
        )
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=route_key,
            source_message_id=source_message_id,
            event_json=str(row[5]),
            status=status,
            updated_at=now,
        )
        conn.execute(
            """
            DELETE FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, source_message_id),
        )
    return True


# 旧调用入口保留兼容，但内部同样进入三表统一终态事务。
def mark_gateway_outbox_failed(
    conn: sqlite3.Connection,
    outbox_id: str,
    error: str,
    error_code: str | None,
) -> bool:
    row = _gateway_terminal_outbox_row(conn, outbox_id)
    if row is None:
        return False
    return fail_gateway_delivery(
        conn,
        outbox_id,
        str(row[0]),
        str(row[1]),
        error,
        error_code,
    )


def mark_gateway_outbox_cancelled(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> bool:
    row = _gateway_terminal_outbox_row(conn, outbox_id)
    if row is None:
        return False
    return cancel_gateway_delivery(
        conn,
        outbox_id,
        str(row[0]),
        str(row[1]),
    )


def mark_gateway_outbox_delivered(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> bool:
    row = _gateway_terminal_outbox_row(conn, outbox_id)
    if row is None:
        return False
    return complete_gateway_delivery(
        conn,
        outbox_id,
        str(row[0]),
        str(row[1]),
    )


def _mark_gateway_message_delivery_terminal(
    conn: sqlite3.Connection,
    delivery_id: str,
    status: str,
    updated_at: float,
    *,
    route_key: str | None = None,
    source_message_id: str | None = None,
) -> None:
    """与 Outbox 终态同事务更新；没有关联的旧/命令 Outbox 保持兼容。"""
    allowed_sources = {
        "delivered": ("pending", "cancelled", "partial_cancelled"),
        "cancelled": ("pending",),
        "partial_cancelled": ("pending", "cancelled"),
        "permanent_failed": ("pending",),
    }
    if status not in allowed_sources:
        raise DBError(f"invalid gateway delivery terminal status: {status}")
    row = conn.execute(
        """
        SELECT status, route_key, source_message_id
        FROM gateway_message_deliveries
        WHERE delivery_id=?
        """,
        (delivery_id,),
    ).fetchone()
    if row is None:
        return
    if (
        route_key is not None
        and source_message_id is not None
        and (
            str(row[1]) != route_key
            or str(row[2]) != source_message_id
        )
    ):
        raise DBError(
            "gateway assistant delivery identity mismatch "
            f"for outbox {delivery_id}"
        )
    current_status = str(row[0])
    if current_status == status:
        return
    source_statuses = allowed_sources[status]
    if current_status not in source_statuses:
        raise DBError(
            "invalid gateway assistant delivery transition: "
            f"{current_status} -> {status}"
        )
    cursor = conn.execute(
        """
        UPDATE gateway_message_deliveries
        SET status=?, updated_at=?
        WHERE delivery_id=? AND status=?
        """,
        (status, updated_at, delivery_id, current_status),
    )
    if cursor.rowcount <= 0:
        raise DBError(
            "gateway assistant delivery CAS failed "
            f"for outbox {delivery_id}"
        )


def _insert_message(conn: sqlite3.Connection, session_id: str, msg: dict) -> int:
    """实际 INSERT 一行,不 commit(供 add_message / add_messages 复用)。"""
    if not session_id:
        raise InvalidMessageError("session_id is required")
    if not isinstance(msg, dict):
        raise InvalidMessageError(
            f"msg must be a dict, got {type(msg).__name__}"
        )

    role = msg.get("role")
    if role not in _ALLOWED_ROLES:
        raise InvalidMessageError(
            f"invalid role: {role!r}; allowed: {sorted(_ALLOWED_ROLES)}"
        )

    # content 统一成字符串(None / 缺失 → "")
    content = msg.get("content", "")
    if content is None:
        content = ""
    if not isinstance(content, str):
        # 非 str 内容尝试字符串化,失败则报错
        try:
            content = str(content)
        except Exception as exc:
            raise InvalidMessageError(
                f"content cannot be coerced to str: {exc}"
            ) from exc

    # tool_calls 序列化(集中处理,失败抛 InvalidMessageError)
    tool_calls_json = _serialize_tool_calls(msg.get("tool_calls"))

    tool_call_id = msg.get("tool_call_id")
    # tool 角色必须带 tool_call_id,否则上下文里无法关联到原 tool_call
    if role == "tool" and not tool_call_id:
        raise InvalidMessageError("tool message missing tool_call_id")

    try:
        cursor = conn.execute(
            """
            INSERT INTO messages
                (session_id, role, content, tool_calls, tool_call_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                tool_calls_json,
                tool_call_id,
                time.time(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        # 外键约束失败(session_id 不存在)/ NOT NULL 违反 等
        raise InvalidMessageError(f"db integrity error: {exc}") from exc
    return int(cursor.lastrowid)


def _insert_gateway_message_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    session_id: str,
    assistant_message_id: int,
    route_key: str,
    source_message_id: str,
) -> None:
    """关联最终 assistant 与 Outbox；调用方负责外层事务。"""
    now = time.time()
    conn.execute(
        """
        INSERT INTO gateway_message_deliveries (
            delivery_id, session_id, assistant_message_id, route_key,
            source_message_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            delivery_id,
            session_id,
            int(assistant_message_id),
            route_key,
            source_message_id,
            now,
            now,
        ),
    )


def add_final_message_with_gateway_outbox(
    conn: sqlite3.Connection,
    session_id: str,
    msg: dict,
    outbox: dict,
) -> str:
    """原子写入最终 assistant 消息、outbox 和 reply_pending 状态。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        raise InvalidMessageError(
            "gateway final delivery must reference an assistant message"
        )
    with transaction(conn):
        assistant_message_id = _insert_message(conn, session_id, msg)
        outbox_id = _insert_gateway_outbox(conn, outbox)
        _insert_gateway_message_delivery(
            conn,
            delivery_id=outbox_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            route_key=str(outbox["route_key"]),
            source_message_id=str(outbox["source_message_id"]),
        )
        conn.execute(
            """
            UPDATE gateway_message_queue
            SET status='reply_pending', updated_at=?
            WHERE route_key=? AND message_id=?
            """,
            (
                time.time(),
                str(outbox["route_key"]),
                str(outbox["source_message_id"]),
            ),
        )
    return outbox_id


def add_message(
    conn: sqlite3.Connection,
    session_id: str,
    msg: dict,
) -> None:
    """单条写入 + commit。保持向后兼容。

    失败抛 InvalidMessageError,调用方需要时显式 try/except。
    """
    _insert_message(conn, session_id, msg)
    conn.commit()


def add_messages(
    conn: sqlite3.Connection,
    session_id: str,
    messages: list[dict],
) -> None:
    """批量写入,在单个事务内执行。任一失败则整组回滚。"""
    if not isinstance(messages, (list, tuple)):
        raise InvalidMessageError(
            f"messages must be a list, got {type(messages).__name__}"
        )
    with transaction(conn):
        for msg in messages:
            _insert_message(conn, session_id, msg)


def get_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict]:
    """按插入顺序返回某 session 的全部消息。

    反序列化在 db 层内完成:tool_calls 字段从 JSON 字符串还原成 list/dict,
    上层调用方不应再处理 JSON。
    """
    rows = conn.execute(
        """
        SELECT role, content, tool_calls, tool_call_id
        FROM messages
        WHERE session_id = ?
        ORDER BY id
        """,
        (session_id,),
    ).fetchall()

    messages: list[dict] = []
    for role, content, tool_calls_json, tool_call_id in rows:
        msg: dict = {"role": role, "content": content or ""}
        # tool_calls 反序列化(集中处理)
        calls = _deserialize_tool_calls(tool_calls_json)
        if calls:
            msg["tool_calls"] = calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        messages.append(msg)
    return messages


def get_gateway_visible_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict]:
    """读取 Gateway 用户实际可见的历史，不改变普通 CLI 的读取语义。

    只有与投递记录关联的最终 assistant 回答会被检查状态；没有关联的旧记录
    默认按已送达处理，tool-call/continuation 等内部消息也会完整保留。
    """
    rows = conn.execute(
        """
        SELECT m.role, m.content, m.tool_calls, m.tool_call_id
        FROM messages AS m
        WHERE m.session_id = ?
          AND (
              m.role != 'assistant'
              OR NOT EXISTS (
                  SELECT 1
                  FROM gateway_message_deliveries AS delivery
                  WHERE delivery.assistant_message_id = m.id
                    AND delivery.status != 'delivered'
              )
          )
        ORDER BY m.id
        """,
        (session_id,),
    ).fetchall()

    messages: list[dict] = []
    for role, content, tool_calls_json, tool_call_id in rows:
        msg: dict = {"role": role, "content": content or ""}
        calls = _deserialize_tool_calls(tool_calls_json)
        if calls:
            msg["tool_calls"] = calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        messages.append(msg)
    return messages


# 别名:spec 描述时用了 get_messages,这里暴露同义名称
get_messages = get_session_messages
