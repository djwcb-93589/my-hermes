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
LATEST_SCHEMA_VERSION = 15

_DEFAULT_GATEWAY_APPROVAL_AGENT_STATE = {
    "iterations_used": 0,
    "retry_count": 0,
    "continuation_count": 0,
    "using_fallback": False,
    "active_model": "",
}

GATEWAY_APPROVAL_STATUSES = frozenset({
    "pending",
    "executing",
    "executed",
    "denied",
    "expired",
    "cancelled",
    "failed",
    "execution_unknown",
})

# 允许的 role 白名单。非法 role 显式报错,不静默吞掉。
_ALLOWED_ROLES = {"user", "assistant", "system", "tool"}

# Feishu Inbox 状态由数据库层统一约束，Adapter 只使用这里暴露的访问接口。
FEISHU_INBOX_STATUSES = frozenset({
    "pending",
    "processing",
    "retry_wait",
    "processed",
    "cancelled",
    "permanent_failed",
})


class DBError(Exception):
    """db 层错误基类。"""


class InvalidMessageError(DBError):
    """消息结构非法(role 不允许 / 缺 session_id / 缺 tool_call_id / JSON
    序列化失败等)。"""


class InvalidFeishuInboxPayloadError(DBError):
    """Feishu Inbox payload 已损坏或不是 JSON 对象。"""


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

        -- 保存 route 曾经选择过的全部对话；不增加 sessions 外键，因为
        -- /new 允许先生成 conversation_id，首次模型调用时再创建 session。
        CREATE TABLE IF NOT EXISTS gateway_route_conversations (
            route_key TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_selected_at REAL NOT NULL,
            PRIMARY KEY (route_key, conversation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_route_conversations_recent
            ON gateway_route_conversations(route_key, last_selected_at DESC);

        -- Runner 接受但尚未完成的消息。queued / processing 都会在
        -- Gateway 重启后恢复,完成后删除。
        CREATE TABLE IF NOT EXISTS gateway_message_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_key TEXT NOT NULL,
            message_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            status TEXT NOT NULL,
            task_kind TEXT NOT NULL DEFAULT 'external' CHECK (
                task_kind IN ('external', 'approval_resume')
            ),
            approval_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(route_key, message_id)
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_message_queue_status
            ON gateway_message_queue(status, id);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_approval_resume_task
            ON gateway_message_queue(approval_id)
            WHERE task_kind='approval_resume';

        -- Gateway 已生成但尚未完整送达平台的回复。payloads_json 保存已经
        -- 确定格式和 UUID 的分片,重启后不能重新切分或重新生成 UUID。
        CREATE TABLE IF NOT EXISTS gateway_outbox (
            id TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            queue_message_id TEXT NOT NULL,
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
            claimed_by TEXT,
            claim_epoch INTEGER CHECK (claim_epoch IS NULL OR claim_epoch > 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (
                (claimed_by IS NULL AND claim_epoch IS NULL)
                OR (claimed_by IS NOT NULL AND claim_epoch IS NOT NULL)
            ),
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
    _create_gateway_approval_schema(conn)
    _create_gateway_fencing_triggers(conn)
    _create_feishu_inbox_schema(conn)


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


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """返回表的列名集合，仅供受控的 schema migration 使用。"""
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


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


def build_feishu_inbox_route_key(
    account_id: str,
    chat_type: str,
    chat_id: str,
    user_id: str,
    thread_id: str | None,
) -> str:
    """编码无歧义的 Feishu Inbox 路由身份。"""
    normalized_account_id = str(account_id or "")
    if not normalized_account_id:
        raise DBError("Feishu Inbox account_id must not be empty")
    identity = (
        "feishu",
        normalized_account_id,
        str(chat_type or ""),
        str(chat_id or ""),
        str(user_id or ""),
        str(thread_id or ""),
    )
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _derive_feishu_inbox_route_key(
    app_id: str,
    message_id: str,
    encoded_payload: str,
) -> str:
    """从旧 Inbox payload 回填路由；不可识别记录进入独立隔离路由。"""
    try:
        payload = json.loads(encoded_payload)
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_ids = sender.get("sender_id", {})
        if not all(
            isinstance(value, dict)
            for value in (payload, event, message, sender, sender_ids)
        ):
            raise ValueError("invalid Feishu Inbox route payload")
        chat_id = str(message.get("chat_id", "") or "")
        user_id = str(
            sender_ids.get("open_id")
            or sender_ids.get("user_id")
            or ""
        )
        if not chat_id or not user_id:
            raise ValueError("incomplete Feishu Inbox route identity")
        raw_chat_type = str(message.get("chat_type", "p2p") or "p2p")
        chat_type = "dm" if raw_chat_type == "p2p" else raw_chat_type
        thread_id = message.get("thread_id") or message.get("root_id")
        return build_feishu_inbox_route_key(
            app_id,
            chat_type,
            chat_id,
            user_id,
            str(thread_id) if thread_id else None,
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        # 旧损坏数据无法可靠恢复真实路由，只能按消息隔离；新写入会在 ACK
        # 前保存从已校验 MessageEvent 得到的 route_key，不存在这一信息缺口。
        return build_feishu_inbox_route_key(
            app_id,
            "invalid",
            f"inbox:{message_id}",
            "",
            None,
        )


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
            lease_epoch INTEGER NOT NULL CHECK (lease_epoch > 0),
            heartbeat_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )


def _create_gateway_approval_schema(conn: sqlite3.Connection) -> None:
    """创建远程工具审批表；请求与原始 Tool Result 一一绑定。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_approval_requests (
            id TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            requester_user_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            tool_message_id INTEGER NOT NULL UNIQUE,
            tool_name TEXT NOT NULL CHECK (tool_name IN ('file', 'terminal')),
            tool_args_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'executing', 'executed', 'denied', 'expired',
                    'cancelled', 'failed', 'execution_unknown'
                )
            ),
            decision_message_id TEXT,
            result_content TEXT,
            source_event_json TEXT NOT NULL,
            agent_state_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (conversation_id)
                REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (tool_message_id)
                REFERENCES messages(id) ON DELETE CASCADE,
            UNIQUE(route_key, tool_call_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_approval_route_status
            ON gateway_approval_requests(
                route_key, conversation_id, status, created_at
            )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_approval_expiry
            ON gateway_approval_requests(status, expires_at)
        """
    )


def _create_gateway_fencing_triggers(conn: sqlite3.Connection) -> None:
    """让迁移表与全新表保持相同的 fencing 字段约束。"""
    lease_columns = _table_columns(conn, "gateway_runtime_lease")
    if "lease_epoch" in lease_columns:
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_gateway_lease_epoch_insert
            BEFORE INSERT ON gateway_runtime_lease
            WHEN NEW.lease_epoch IS NULL OR NEW.lease_epoch <= 0
            BEGIN
                SELECT RAISE(ABORT, 'invalid Gateway lease epoch');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_gateway_lease_epoch_update
            BEFORE UPDATE OF lease_epoch ON gateway_runtime_lease
            WHEN NEW.lease_epoch IS NULL OR NEW.lease_epoch <= 0
            BEGIN
                SELECT RAISE(ABORT, 'invalid Gateway lease epoch');
            END
            """
        )

    outbox_columns = _table_columns(conn, "gateway_outbox")
    if {"claimed_by", "claim_epoch"} <= outbox_columns:
        claim_condition = """
            (NEW.claimed_by IS NULL AND NEW.claim_epoch IS NOT NULL)
            OR (NEW.claimed_by IS NOT NULL AND NEW.claim_epoch IS NULL)
            OR (NEW.claim_epoch IS NOT NULL AND NEW.claim_epoch <= 0)
        """
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_gateway_outbox_claim_insert
            BEFORE INSERT ON gateway_outbox
            WHEN {claim_condition}
            BEGIN
                SELECT RAISE(ABORT, 'invalid Gateway Outbox claim');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_gateway_outbox_claim_update
            BEFORE UPDATE OF claimed_by, claim_epoch ON gateway_outbox
            WHEN {claim_condition}
            BEGIN
                SELECT RAISE(ABORT, 'invalid Gateway Outbox claim');
            END
            """
        )


def _create_feishu_inbox_indexes_and_triggers(
    conn: sqlite3.Connection,
) -> None:
    """创建 Feishu Inbox 的顺序、恢复、重试和状态约束对象。"""
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feishu_inbox_receive_sequence
        ON feishu_message_inbox(app_id, receive_sequence)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feishu_inbox_completed
        ON feishu_message_inbox(app_id, completed_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feishu_inbox_recovery
        ON feishu_message_inbox(app_id, status, receive_sequence)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feishu_inbox_retry
        ON feishu_message_inbox(
            app_id, status, next_attempt_at, receive_sequence
        )
        """
    )
    if "route_key" in _table_columns(conn, "feishu_message_inbox"):
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_feishu_inbox_route_order
            ON feishu_message_inbox(
                app_id, route_key, received_at, receive_sequence
            )
            """
        )
    # 旧表无法通过 ALTER TABLE 补表级 CHECK，触发器让迁移库与新库保持
    # 相同的状态集合约束；新库上的 CHECK 则提供双重保护。
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_feishu_inbox_status_insert
        BEFORE INSERT ON feishu_message_inbox
        WHEN NEW.status NOT IN (
            'pending', 'processing', 'retry_wait', 'processed',
            'cancelled', 'permanent_failed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid Feishu Inbox status');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_feishu_inbox_status_update
        BEFORE UPDATE OF status ON feishu_message_inbox
        WHEN NEW.status NOT IN (
            'pending', 'processing', 'retry_wait', 'processed',
            'cancelled', 'permanent_failed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid Feishu Inbox status');
        END
        """
    )
    if "route_key" in _table_columns(conn, "feishu_message_inbox"):
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_feishu_inbox_route_insert
            BEFORE INSERT ON feishu_message_inbox
            WHEN NEW.route_key IS NULL OR NEW.route_key=''
            BEGIN
                SELECT RAISE(ABORT, 'invalid Feishu Inbox route key');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_feishu_inbox_route_update
            BEFORE UPDATE OF route_key ON feishu_message_inbox
            WHEN NEW.route_key IS NULL OR NEW.route_key=''
            BEGIN
                SELECT RAISE(ABORT, 'invalid Feishu Inbox route key');
            END
            """
        )


def _create_feishu_inbox_schema(conn: sqlite3.Connection) -> None:
    """创建最新版 Feishu Inbox schema。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feishu_message_inbox (
            app_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            route_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            received_at REAL NOT NULL,
            receive_sequence INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'processing', 'retry_wait', 'processed',
                    'cancelled', 'permanent_failed'
                )
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at REAL,
            last_error TEXT,
            updated_at REAL NOT NULL,
            completed_at REAL,
            batch_message_id TEXT,
            PRIMARY KEY (app_id, message_id)
        )
        """
    )
    _create_feishu_inbox_indexes_and_triggers(conn)


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


def _migrate_v9_to_v10(conn: sqlite3.Connection) -> None:
    """把 Adapter 旧 Inbox 原地升级为正式 schema，并保留全部记录。"""
    if not _table_exists(conn, "feishu_message_inbox"):
        _create_feishu_inbox_schema(conn)
        return

    columns = _table_columns(conn, "feishu_message_inbox")
    required_legacy_columns = {
        "app_id",
        "message_id",
        "payload",
        "received_at",
        "status",
    }
    missing_legacy_columns = required_legacy_columns - columns
    if missing_legacy_columns:
        missing = ", ".join(sorted(missing_legacy_columns))
        raise DBError(f"Feishu Inbox missing required columns: {missing}")

    # SQLite 不能给旧表补表级 CHECK；先原地补列，后续用触发器约束状态。
    additions = (
        ("receive_sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_attempt_at", "REAL"),
        ("last_error", "TEXT"),
        ("updated_at", "REAL NOT NULL DEFAULT 0"),
        ("completed_at", "REAL"),
        ("batch_message_id", "TEXT"),
    )
    for column_name, definition in additions:
        if column_name in columns:
            continue
        conn.execute(
            f"ALTER TABLE feishu_message_inbox "
            f"ADD COLUMN {column_name} {definition}"
        )
        columns.add(column_name)

    invalid_status = conn.execute(
        """
        SELECT status
        FROM feishu_message_inbox
        WHERE status IS NULL OR status NOT IN (
            'pending', 'processing', 'retry_wait', 'processed',
            'cancelled', 'permanent_failed'
        )
        LIMIT 1
        """
    ).fetchone()
    if invalid_status is not None:
        raise DBError(
            "cannot migrate Feishu Inbox with invalid status: "
            f"{invalid_status[0]}"
        )

    invalid_attempt_count = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        WHERE attempt_count IS NULL OR attempt_count < 0
        LIMIT 1
        """
    ).fetchone()
    if invalid_attempt_count is not None:
        raise DBError("cannot migrate Feishu Inbox with invalid attempt_count")

    # 旧恢复逻辑按 received_at、message_id 排序；首次回填沿用该顺序，
    # 此后 receive_sequence 不再重算，保证重启前后顺序稳定。
    invalid_sequence = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        WHERE receive_sequence IS NULL OR receive_sequence <= 0
        LIMIT 1
        """
    ).fetchone()
    duplicate_sequence = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        GROUP BY app_id, receive_sequence
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if invalid_sequence is not None or duplicate_sequence is not None:
        rows = conn.execute(
            """
            SELECT app_id, message_id
            FROM feishu_message_inbox
            ORDER BY app_id, received_at, message_id
            """
        ).fetchall()
        sequence_by_app: dict[str, int] = {}
        sequence_rows = []
        for app_id, message_id in rows:
            normalized_app_id = str(app_id)
            sequence = sequence_by_app.get(normalized_app_id, 0) + 1
            sequence_by_app[normalized_app_id] = sequence
            sequence_rows.append((sequence, app_id, message_id))
        conn.executemany(
            """
            UPDATE feishu_message_inbox
            SET receive_sequence=?
            WHERE app_id=? AND message_id=?
            """,
            sequence_rows,
        )

    conn.execute(
        """
        UPDATE feishu_message_inbox
        SET updated_at=COALESCE(completed_at, received_at)
        WHERE updated_at IS NULL OR updated_at <= 0
        """
    )
    _create_feishu_inbox_indexes_and_triggers(conn)


def _migrate_v10_to_v11(conn: sqlite3.Connection) -> None:
    """为 Inbox 原地补充持久 route_key，并按旧 payload 回填。"""
    if not _table_exists(conn, "feishu_message_inbox"):
        _create_feishu_inbox_schema(conn)
        return

    columns = _table_columns(conn, "feishu_message_inbox")
    if "route_key" not in columns:
        conn.execute(
            "ALTER TABLE feishu_message_inbox "
            "ADD COLUMN route_key TEXT NOT NULL DEFAULT ''"
        )

    rows = conn.execute(
        """
        SELECT app_id, message_id, payload
        FROM feishu_message_inbox
        WHERE route_key IS NULL OR route_key=''
        ORDER BY app_id, received_at, receive_sequence
        """
    ).fetchall()
    route_rows = [
        (
            _derive_feishu_inbox_route_key(
                str(app_id),
                str(message_id),
                str(payload),
            ),
            app_id,
            message_id,
        )
        for app_id, message_id, payload in rows
    ]
    if route_rows:
        conn.executemany(
            """
            UPDATE feishu_message_inbox
            SET route_key=?
            WHERE app_id=? AND message_id=?
            """,
            route_rows,
        )

    invalid_route = conn.execute(
        """
        SELECT 1
        FROM feishu_message_inbox
        WHERE route_key IS NULL OR route_key=''
        LIMIT 1
        """
    ).fetchone()
    if invalid_route is not None:
        raise DBError("cannot migrate Feishu Inbox with invalid route_key")
    _create_feishu_inbox_indexes_and_triggers(conn)


def _migrate_v11_to_v12(conn: sqlite3.Connection) -> None:
    """为 Gateway lease 和 Outbox claim 原地补充 fencing epoch。"""
    if not _table_exists(conn, "gateway_runtime_lease"):
        _create_gateway_runtime_lease_schema(conn)
    else:
        lease_columns = _table_columns(conn, "gateway_runtime_lease")
        if "lease_epoch" not in lease_columns:
            conn.execute(
                "ALTER TABLE gateway_runtime_lease "
                "ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 1"
            )

    if not _table_exists(conn, "gateway_outbox"):
        raise DBError("Gateway Outbox table is missing during v12 migration")
    outbox_columns = _table_columns(conn, "gateway_outbox")
    if "claimed_by" not in outbox_columns:
        conn.execute(
            "ALTER TABLE gateway_outbox ADD COLUMN claimed_by TEXT"
        )
    if "claim_epoch" not in outbox_columns:
        conn.execute(
            "ALTER TABLE gateway_outbox ADD COLUMN claim_epoch INTEGER"
        )

    invalid_lease = conn.execute(
        """
        SELECT 1
        FROM gateway_runtime_lease
        WHERE lease_epoch IS NULL OR lease_epoch <= 0
        LIMIT 1
        """
    ).fetchone()
    if invalid_lease is not None:
        raise DBError("cannot migrate Gateway runtime lease with invalid epoch")
    invalid_claim = conn.execute(
        """
        SELECT 1
        FROM gateway_outbox
        WHERE (claimed_by IS NULL) != (claim_epoch IS NULL)
           OR (claim_epoch IS NOT NULL AND claim_epoch <= 0)
        LIMIT 1
        """
    ).fetchone()
    if invalid_claim is not None:
        raise DBError("cannot migrate Gateway Outbox with invalid claim")
    _create_gateway_fencing_triggers(conn)


def _migrate_v12_to_v13(conn: sqlite3.Connection) -> None:
    """保存每条 Gateway route 的历史对话归属并回填旧数据。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_route_conversations (
            route_key TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_selected_at REAL NOT NULL,
            PRIMARY KEY (route_key, conversation_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_route_conversations_recent
        ON gateway_route_conversations(route_key, last_selected_at DESC)
        """
    )

    # 当前映射是最可靠的选择时间来源，先完整登记。
    conn.execute(
        """
        INSERT INTO gateway_route_conversations (
            route_key, conversation_id, created_at, last_selected_at
        )
        SELECT route_key, conversation_id, updated_at, updated_at
        FROM gateway_session_routes
        WHERE 1=1
        ON CONFLICT(route_key, conversation_id) DO UPDATE SET
            last_selected_at=MAX(
                gateway_route_conversations.last_selected_at,
                excluded.last_selected_at
            )
        """
    )

    # 旧版只在 delivery 中保留历史 route + session 关系；最早创建时间和
    # 最后更新时间分别作为 created_at / last_selected_at 的合理代理。
    conn.execute(
        """
        INSERT INTO gateway_route_conversations (
            route_key, conversation_id, created_at, last_selected_at
        )
        SELECT
            route_key,
            session_id,
            MIN(created_at),
            MAX(updated_at)
        FROM gateway_message_deliveries
        WHERE 1=1
        GROUP BY route_key, session_id
        ON CONFLICT(route_key, conversation_id) DO UPDATE SET
            created_at=MIN(
                gateway_route_conversations.created_at,
                excluded.created_at
            ),
            last_selected_at=MAX(
                gateway_route_conversations.last_selected_at,
                excluded.last_selected_at
            )
        """
    )


def _migrate_v14_to_v15(conn: sqlite3.Connection) -> None:
    """为审批恢复补充可信队列身份、原始事件和最小循环状态。"""
    queue_columns = _table_columns(conn, "gateway_message_queue")
    if "task_kind" not in queue_columns:
        conn.execute(
            "ALTER TABLE gateway_message_queue "
            "ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'external' "
            "CHECK (task_kind IN ('external', 'approval_resume'))"
        )
    if "approval_id" not in queue_columns:
        conn.execute(
            "ALTER TABLE gateway_message_queue ADD COLUMN approval_id TEXT"
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_approval_resume_task
        ON gateway_message_queue(approval_id)
        WHERE task_kind='approval_resume'
        """
    )

    outbox_columns = _table_columns(conn, "gateway_outbox")
    if "queue_message_id" not in outbox_columns:
        conn.execute(
            "ALTER TABLE gateway_outbox "
            "ADD COLUMN queue_message_id TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        UPDATE gateway_outbox
        SET queue_message_id=source_message_id
        WHERE queue_message_id IS NULL OR queue_message_id=''
        """
    )

    approval_columns = _table_columns(conn, "gateway_approval_requests")
    if "source_event_json" not in approval_columns:
        conn.execute(
            "ALTER TABLE gateway_approval_requests "
            "ADD COLUMN source_event_json TEXT"
        )
    if "agent_state_json" not in approval_columns:
        conn.execute(
            """
            ALTER TABLE gateway_approval_requests
            ADD COLUMN agent_state_json TEXT NOT NULL DEFAULT
            '{"iterations_used":0,"retry_count":0,"continuation_count":0,"using_fallback":false,"active_model":""}'
            """
        )

    # v14 审批问题的 Outbox 保存了原始事件；尽力回填仍在审计表中的旧请求。
    conn.execute(
        """
        UPDATE gateway_approval_requests AS approval
        SET source_event_json=(
            SELECT outbox.event_json
            FROM gateway_outbox AS outbox
            WHERE outbox.route_key=approval.route_key
              AND outbox.source_message_id=approval.source_message_id
              AND outbox.delivery_kind='approval_request'
            ORDER BY outbox.created_at DESC, outbox.id DESC
            LIMIT 1
        )
        WHERE source_event_json IS NULL OR source_event_json=''
        """
    )


def _migrate(conn: sqlite3.Connection, current: int) -> int:
    """按版本号顺序执行 migration,返回最新版本。

    老库 v1 → v2 会重建 sessions/messages,让外键 / NOT NULL
    约束对既有数据库也生效。v2 → v3 新增 Gateway 当前会话映射,
    v3 → v4 新增 Gateway 待处理消息队列,v4 → v5 新增出站回复队列,
    v5 → v6 关联最终回答投递状态,v6 → v7 区分部分取消,
    v7 → v8 增加原始平台消息归属索引，v8 → v9 增加 Gateway 运行租约，
    v9 → v10 正式接管 Feishu Inbox schema，v10 → v11 持久化 Inbox
    route_key，v11 → v12 增加运行租约 epoch 与 Outbox claim fencing，
    v12 → v13 保存每条 route 的历史 conversation 归属，v13 → v14
    增加与 Tool Result 绑定的远程审批请求，v14 → v15 增加持久化审批恢复。
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

    if current < 10:
        conn.execute("BEGIN")
        try:
            _migrate_v9_to_v10(conn)
            _set_schema_version(conn, 10)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 10

    if current < 11:
        conn.execute("BEGIN")
        try:
            _migrate_v10_to_v11(conn)
            _set_schema_version(conn, 11)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 11

    if current < 12:
        conn.execute("BEGIN")
        try:
            _migrate_v11_to_v12(conn)
            _set_schema_version(conn, 12)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 12

    if current < 13:
        conn.execute("BEGIN")
        try:
            _migrate_v12_to_v13(conn)
            _set_schema_version(conn, 13)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 13

    if current < 14:
        conn.execute("BEGIN")
        try:
            _create_gateway_approval_schema(conn)
            _set_schema_version(conn, 14)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 14

    if current < 15:
        conn.execute("BEGIN")
        try:
            _migrate_v14_to_v15(conn)
            _set_schema_version(conn, 15)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            current = 15

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


def reconcile_gateway_terminal_deliveries(
    conn: sqlite3.Connection,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> int:
    """收敛旧版本遗留的终态 Outbox 与 ``reply_pending`` queue。

    新代码通过统一终态函数一次提交三层状态；这里仅修复升级前已经形成的
    孤儿记录，以及“取消先提交、最后一个平台成功随后落进度”留下的可推导
    状态。终态审计行不会被删除。
    """
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    manager = (
        _immediate_transaction(conn)
        if fence is not None
        else transaction(conn)
    )
    reconciled = 0
    with manager:
        now = time.time()
        if fence is not None and not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return 0
        rows = conn.execute(
            """
            SELECT o.id, o.route_key, o.source_message_id,
                   o.queue_message_id, o.status,
                   o.next_chunk_index, o.payloads_json,
                   q.status, o.event_json
            FROM gateway_outbox AS o
            LEFT JOIN gateway_message_queue AS q
              ON q.route_key=o.route_key
             AND q.message_id=o.queue_message_id
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
        for (
            outbox_id,
            route_key,
            source_message_id,
            queue_message_id,
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
                lease_clause, lease_params = _gateway_outbox_lease_clause(
                    fence,
                    now,
                )
                claim_assignment = ""
                claim_params: tuple = ()
                if fence is not None:
                    claim_assignment = ", claimed_by=?, claim_epoch=?"
                    claim_params = (fence[1], fence[2])
                cursor = conn.execute(
                    f"""
                    UPDATE gateway_outbox
                    SET status=?, next_attempt_at=NULL,
                        last_error=CASE WHEN ?='delivered' THEN NULL
                                        ELSE last_error END,
                        last_error_code=CASE WHEN ?='delivered' THEN NULL
                                             ELSE last_error_code END,
                        updated_at=? {claim_assignment}
                    WHERE id=? AND status=?
                    {lease_clause}
                    """,
                    (
                        status,
                        status,
                        status,
                        now,
                        *claim_params,
                        outbox_id,
                        stored_status,
                        *lease_params,
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
                    _finish_gateway_queue_for_delivery(
                        conn,
                        str(route_key),
                        str(queue_message_id),
                        status="delivery_failed",
                        now=now,
                    )
                else:
                    _finish_gateway_queue_for_delivery(
                        conn,
                        str(route_key),
                        str(queue_message_id),
                        status=(
                            "cancelled"
                            if status in {"cancelled", "partial_cancelled"}
                            else "completed"
                        ),
                        now=now,
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


# ===========================================================================
# Feishu Inbox
# ===========================================================================

def _validate_feishu_inbox_identity(app_id: str, message_id: str) -> None:
    """校验 Inbox 复合身份，避免写入不可寻址记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    if not isinstance(message_id, str) or not message_id:
        raise DBError("Feishu Inbox message_id must not be empty")


def _validate_feishu_inbox_status(status: str) -> None:
    """校验 Inbox 状态，错误在进入 SQL 前显式暴露。"""
    if status not in FEISHU_INBOX_STATUSES:
        raise DBError(f"invalid Feishu Inbox status: {status}")


def insert_feishu_inbox_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    payload: dict,
    *,
    route_key: str | None = None,
    received_at: float | None = None,
) -> bool:
    """幂等插入 pending 消息，并原子分配应用内接收序号。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    if not isinstance(payload, dict):
        raise DBError("Feishu Inbox payload must contain an object")
    try:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DBError("Feishu Inbox payload JSON serialization failed") from exc
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        normalized_route_key = _derive_feishu_inbox_route_key(
            app_id,
            message_id,
            encoded_payload,
        )

    timestamp = time.time() if received_at is None else float(received_at)
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO feishu_message_inbox (
                app_id, message_id, route_key, payload, received_at,
                receive_sequence,
                status, attempt_count, next_attempt_at, last_error,
                updated_at, completed_at, batch_message_id
            ) VALUES (
                ?, ?, ?, ?, ?,
                COALESCE((
                    SELECT MAX(receive_sequence) + 1
                    FROM feishu_message_inbox
                    WHERE app_id = ?
                ), 1),
                'pending', 0, NULL, NULL, ?, NULL, NULL
            )
            ON CONFLICT(app_id, message_id) DO NOTHING
            """,
            (
                app_id,
                message_id,
                normalized_route_key,
                encoded_payload,
                timestamp,
                app_id,
                timestamp,
            ),
        )
    return cursor.rowcount == 1


def get_feishu_inbox_status(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
) -> str | None:
    """读取单条 Inbox 状态。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    row = conn.execute(
        """
        SELECT status
        FROM feishu_message_inbox
        WHERE app_id=? AND message_id=?
        """,
        (app_id, message_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def get_feishu_inbox_payload(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    status: str | None = None,
) -> dict | None:
    """按身份和可选状态读取、反序列化 Inbox 原始事件。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    if status is None:
        row = conn.execute(
            """
            SELECT payload
            FROM feishu_message_inbox
            WHERE app_id=? AND message_id=?
            """,
            (app_id, message_id),
        ).fetchone()
    else:
        _validate_feishu_inbox_status(status)
        row = conn.execute(
            """
            SELECT payload
            FROM feishu_message_inbox
            WHERE app_id=? AND message_id=? AND status=?
            """,
            (app_id, message_id, status),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise InvalidFeishuInboxPayloadError(
            "Feishu Inbox payload JSON deserialization failed"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidFeishuInboxPayloadError(
            "Feishu Inbox payload must contain an object"
        )
    return payload


def get_feishu_inbox_dispatch_candidates(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: float | None = None,
    limit: int = 64,
) -> list[str]:
    """按接收顺序读取当前可执行的 pending 或到期 retry_wait 记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        raise DBError("Feishu Inbox dispatch limit must be greater than 0")
    timestamp = time.time() if now is None else float(now)
    rows = conn.execute(
        """
        SELECT message_id
        FROM feishu_message_inbox
        WHERE app_id=?
          AND (
              status='pending'
              OR (
                  status='retry_wait'
                  AND next_attempt_at IS NOT NULL
                  AND next_attempt_at<=?
              )
          )
        ORDER BY received_at, receive_sequence
        LIMIT ?
        """,
        (app_id, timestamp, normalized_limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


def get_feishu_inbox_dispatch_routes(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: float | None = None,
    limit: int = 64,
) -> list[str]:
    """读取每个路由严格队首中当前可执行的 route_key。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        raise DBError("Feishu Inbox dispatch limit must be greater than 0")
    timestamp = time.time() if now is None else float(now)
    rows = conn.execute(
        """
        WITH route_heads AS (
            SELECT current.route_key, current.status,
                   current.next_attempt_at, current.received_at,
                   current.receive_sequence
            FROM feishu_message_inbox AS current
            WHERE current.app_id=?
              AND current.status IN (
                  'pending', 'processing', 'retry_wait'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM feishu_message_inbox AS prior
                  WHERE prior.app_id=current.app_id
                    AND prior.route_key=current.route_key
                    AND prior.status IN (
                        'pending', 'processing', 'retry_wait'
                    )
                    AND (
                        prior.received_at < current.received_at
                        OR (
                            prior.received_at = current.received_at
                            AND prior.receive_sequence
                                < current.receive_sequence
                        )
                    )
              )
        )
        SELECT route_key
        FROM route_heads
        WHERE status='pending'
           OR (
               status='retry_wait'
               AND next_attempt_at IS NOT NULL
               AND next_attempt_at<=?
           )
        ORDER BY received_at, receive_sequence
        LIMIT ?
        """,
        (app_id, timestamp, normalized_limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


def get_feishu_inbox_route_next(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
) -> dict | None:
    """查看路由中尚未终结且未被当前消费者 claim 的严格队首。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        raise DBError("Feishu Inbox route_key must not be empty")
    row = conn.execute(
        """
        SELECT message_id, status, attempt_count, next_attempt_at,
               received_at, receive_sequence
        FROM feishu_message_inbox
        WHERE app_id=? AND route_key=?
          AND status IN ('pending', 'retry_wait')
        ORDER BY received_at, receive_sequence
        LIMIT 1
        """,
        (app_id, normalized_route_key),
    ).fetchone()
    if row is None:
        return None
    return {
        "message_id": str(row[0]),
        "status": str(row[1]),
        "attempt_count": int(row[2]),
        "next_attempt_at": None if row[3] is None else float(row[3]),
        "received_at": float(row[4]),
        "receive_sequence": int(row[5]),
    }


def claim_feishu_inbox_route_message(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
    message_id: str,
    *,
    now: float | None = None,
    allow_existing_processing: bool = False,
) -> dict | None:
    """仅 claim 路由未终结队首，并原子切换为 processing。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        raise DBError("Feishu Inbox route_key must not be empty")
    timestamp = time.time() if now is None else float(now)
    with _immediate_transaction(conn):
        if not allow_existing_processing:
            processing = conn.execute(
                """
                SELECT 1
                FROM feishu_message_inbox
                WHERE app_id=? AND route_key=? AND status='processing'
                LIMIT 1
                """,
                (app_id, normalized_route_key),
            ).fetchone()
            if processing is not None:
                return None
        head = conn.execute(
            """
            SELECT message_id
            FROM feishu_message_inbox
            WHERE app_id=? AND route_key=?
              AND status IN ('pending', 'retry_wait')
            ORDER BY received_at, receive_sequence
            LIMIT 1
            """,
            (app_id, normalized_route_key),
        ).fetchone()
        if head is None or str(head[0]) != message_id:
            return None
        row = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='processing', updated_at=?, next_attempt_at=NULL,
                completed_at=NULL
            WHERE app_id=? AND route_key=? AND message_id=?
              AND (
                  status='pending'
                  OR (
                      status='retry_wait'
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at<=?
                  )
              )
            RETURNING attempt_count, received_at, receive_sequence
            """,
            (
                timestamp,
                app_id,
                normalized_route_key,
                message_id,
                timestamp,
            ),
        ).fetchone()
    if row is None:
        return None
    return {
        "message_id": message_id,
        "route_key": normalized_route_key,
        "attempt_count": int(row[0]),
        "received_at": float(row[1]),
        "receive_sequence": int(row[2]),
    }


def claim_feishu_inbox_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    now: float | None = None,
) -> dict | None:
    """条件 claim 可执行记录并切换为 processing，竞争失败返回 None。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='processing', updated_at=?, next_attempt_at=NULL,
                completed_at=NULL
            WHERE app_id=? AND message_id=?
              AND (
                  status='pending'
                  OR (
                      status='retry_wait'
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at<=?
                  )
              )
            """,
            (timestamp, app_id, message_id, timestamp),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            """
            SELECT attempt_count, receive_sequence
            FROM feishu_message_inbox
            WHERE app_id=? AND message_id=? AND status='processing'
            """,
            (app_id, message_id),
        ).fetchone()
        if row is None:
            raise DBError("claimed Feishu Inbox message disappeared")
    return {
        "message_id": message_id,
        "attempt_count": int(row[0]),
        "receive_sequence": int(row[1]),
    }


def update_feishu_inbox_status(
    conn: sqlite3.Connection,
    app_id: str,
    message_ids: list[str],
    status: str,
    *,
    completed_at: float | None = None,
    batch_message_id: str | None = None,
    updated_at: float | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> int:
    """原子更新一组 Inbox 消息的状态和当前批次追踪信息。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    _validate_feishu_inbox_status(status)
    if not message_ids:
        return 0
    normalized_message_ids = []
    for message_id in message_ids:
        _validate_feishu_inbox_identity(app_id, message_id)
        normalized_message_ids.append(message_id)
    timestamp = time.time() if updated_at is None else float(updated_at)
    normalized_completed_at = (
        None if completed_at is None else float(completed_at)
    )
    normalized_expected_statuses = None
    if expected_statuses is not None:
        normalized_expected_statuses = tuple(dict.fromkeys(expected_statuses))
        if not normalized_expected_statuses:
            return 0
        for expected_status in normalized_expected_statuses:
            _validate_feishu_inbox_status(expected_status)
    status_clause = ""
    status_params: tuple[str, ...] = ()
    if normalized_expected_statuses is not None:
        placeholders = ", ".join("?" for _ in normalized_expected_statuses)
        status_clause = f" AND status IN ({placeholders})"
        status_params = normalized_expected_statuses
    with transaction(conn):
        cursor = conn.executemany(
            f"""
            UPDATE feishu_message_inbox
            SET status=?, updated_at=?, completed_at=?, batch_message_id=?,
                next_attempt_at=NULL
            WHERE app_id=? AND message_id=?
            {status_clause}
            """,
            [
                (
                    status,
                    timestamp,
                    normalized_completed_at,
                    batch_message_id,
                    app_id,
                    message_id,
                    *status_params,
                )
                for message_id in normalized_message_ids
            ],
        )
    return max(0, cursor.rowcount)


def fail_feishu_inbox_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    last_error: str,
    next_attempt_at: float | None,
    max_attempts: int,
    permanent: bool = False,
    now: float | None = None,
) -> dict | None:
    """记录一次失败，并按永久性或最大次数决定 retry_wait/终态。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    normalized_max_attempts = int(max_attempts)
    if normalized_max_attempts <= 0:
        raise DBError("Feishu Inbox max_attempts must be greater than 0")
    normalized_error = str(last_error or "inbox_failure")[:256]
    timestamp = time.time() if now is None else float(now)
    normalized_next_attempt = (
        None if next_attempt_at is None else float(next_attempt_at)
    )
    with transaction(conn):
        row = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET attempt_count=attempt_count + 1,
                status=CASE
                    WHEN ? OR attempt_count + 1 >= ?
                    THEN 'permanent_failed'
                    ELSE 'retry_wait'
                END,
                next_attempt_at=CASE
                    WHEN ? OR attempt_count + 1 >= ?
                    THEN NULL
                    ELSE ?
                END,
                last_error=?,
                updated_at=?,
                completed_at=CASE
                    WHEN ? OR attempt_count + 1 >= ?
                    THEN ?
                    ELSE NULL
                END
            WHERE app_id=? AND message_id=? AND status='processing'
            RETURNING status, attempt_count, next_attempt_at
            """,
            (
                bool(permanent),
                normalized_max_attempts,
                bool(permanent),
                normalized_max_attempts,
                normalized_next_attempt,
                normalized_error,
                timestamp,
                bool(permanent),
                normalized_max_attempts,
                timestamp,
                app_id,
                message_id,
            ),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": str(row[0]),
        "attempt_count": int(row[1]),
        "next_attempt_at": (
            None if row[2] is None else float(row[2])
        ),
    }


def reset_feishu_inbox_processing(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: float | None = None,
) -> int:
    """启动时把异常退出遗留的 processing 收敛为立即可恢复状态。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='retry_wait', next_attempt_at=?, updated_at=?,
                completed_at=NULL,
                last_error='gateway_restart:processing_recovered'
            WHERE app_id=? AND status='processing'
            """,
            (timestamp, timestamp, app_id),
        )
    return cursor.rowcount


def release_feishu_inbox_processing_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    last_error: str,
    now: float | None = None,
) -> bool:
    """不增加尝试次数地释放 processing，供有序 shutdown 恢复。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='retry_wait', next_attempt_at=?, updated_at=?,
                completed_at=NULL, last_error=?
            WHERE app_id=? AND message_id=? AND status='processing'
            """,
            (
                timestamp,
                timestamp,
                str(last_error or "gateway_stopping")[:256],
                app_id,
                message_id,
            ),
        )
    return cursor.rowcount == 1


def _cleanup_batch_limit(value, label: str) -> int:
    """统一校验清理批次，避免布尔值或非法字符串进入 LIMIT。"""
    if isinstance(value, bool):
        raise DBError(f"{label} cleanup limit must be positive")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DBError(f"{label} cleanup limit must be positive") from exc
    if normalized <= 0:
        raise DBError(f"{label} cleanup limit must be positive")
    return normalized


def prune_feishu_inbox_messages(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    completed_before: float,
    limit: int = 200,
) -> int:
    """分批清理超过保留期的 Inbox 终态审计记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    batch_limit = _cleanup_batch_limit(limit, "Feishu Inbox")
    cutoff = float(completed_before)
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT candidate.message_id
            FROM feishu_message_inbox AS candidate
            WHERE candidate.app_id=?
              AND candidate.completed_at < ?
              AND candidate.status IN (
                  'processed', 'cancelled', 'permanent_failed'
              )
            ORDER BY candidate.completed_at, candidate.receive_sequence
            LIMIT ?
            """,
            (app_id, cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for (message_id,) in rows:
            cursor = conn.execute(
                """
                DELETE FROM feishu_message_inbox
                WHERE app_id=? AND message_id=? AND completed_at < ?
                  AND status IN (
                      'processed', 'cancelled', 'permanent_failed'
                  )
                """,
                (app_id, str(message_id), cutoff),
            )
            removed += cursor.rowcount
    return removed


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """以写锁开始短事务，供 lease 竞争、探针和有界清理使用。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def prune_gateway_terminal_ownership(
    conn: sqlite3.Connection,
    *,
    updated_before: float,
    limit: int = 200,
) -> int:
    """分批删除无 Queue/Outbox 引用的终态 ownership。"""
    batch_limit = _cleanup_batch_limit(limit, "gateway ownership")
    cutoff = float(updated_before)
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT ownership.route_key, ownership.source_message_id
            FROM gateway_source_message_ownership AS ownership
            WHERE ownership.updated_at < ?
              AND ownership.status IN (
                  'completed', 'cancelled', 'delivery_failed', 'delivered',
                  'partial_cancelled', 'permanent_failed'
              )
              AND (
                  (
                      ownership.owner_kind='queue'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM gateway_message_queue AS queue
                          WHERE queue.route_key=ownership.route_key
                            AND queue.message_id=ownership.owner_id
                      )
                  )
                  OR (
                      ownership.owner_kind='outbox'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM gateway_outbox AS outbox
                          WHERE outbox.id=ownership.owner_id
                      )
                  )
              )
            ORDER BY ownership.updated_at, ownership.route_key,
                     ownership.source_message_id
            LIMIT ?
            """,
            (cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for route_key, source_message_id in rows:
            cursor = conn.execute(
                """
                DELETE FROM gateway_source_message_ownership
                WHERE route_key=? AND source_message_id=?
                  AND updated_at < ?
                  AND status IN (
                      'completed', 'cancelled', 'delivery_failed',
                      'delivered', 'partial_cancelled', 'permanent_failed'
                  )
                """,
                (str(route_key), str(source_message_id), cutoff),
            )
            removed += cursor.rowcount
    return removed


def prune_gateway_terminal_outbox(
    conn: sqlite3.Connection,
    *,
    updated_before: float,
    limit: int = 200,
) -> int:
    """分批删除安全的终态 Outbox 审计，并保持消息可见性语义。"""
    batch_limit = _cleanup_batch_limit(limit, "gateway Outbox")
    cutoff = float(updated_before)
    terminal_statuses = (
        "delivered",
        "cancelled",
        "partial_cancelled",
        "permanent_failed",
    )
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT outbox.id, outbox.route_key, outbox.source_message_id,
                   outbox.queue_message_id
            FROM gateway_outbox AS outbox
            WHERE outbox.updated_at < ?
              AND outbox.status IN (
                  'delivered', 'cancelled', 'partial_cancelled',
                  'permanent_failed'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM gateway_message_queue AS queue
                  WHERE queue.route_key=outbox.route_key
                    AND queue.message_id=outbox.queue_message_id
                    AND queue.status IN (
                        'queued', 'processing', 'reply_pending'
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM gateway_message_deliveries AS delivery
                  WHERE delivery.delivery_id=outbox.id
                    AND delivery.status != outbox.status
              )
            ORDER BY outbox.updated_at, outbox.id
            LIMIT ?
            """,
            (cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for outbox_id, route_key, source_message_id, queue_message_id in rows:
            delivery = conn.execute(
                """
                SELECT assistant_message_id, status
                FROM gateway_message_deliveries
                WHERE delivery_id=?
                """,
                (str(outbox_id),),
            ).fetchone()
            if delivery is not None and str(delivery[1]) != "delivered":
                # 未送达回答原本由 delivery 状态隐藏；删除审计前同时删除
                # 对应 assistant，避免历史读取时错误地重新显示。
                conn.execute(
                    "DELETE FROM messages WHERE id=?",
                    (int(delivery[0]),),
                )
            conn.execute(
                "DELETE FROM gateway_message_deliveries WHERE delivery_id=?",
                (str(outbox_id),),
            )
            conn.execute(
                """
                DELETE FROM gateway_message_queue
                WHERE route_key=? AND message_id=?
                  AND status='delivery_failed'
                """,
                (str(route_key), str(queue_message_id)),
            )
            cursor = conn.execute(
                """
                DELETE FROM gateway_outbox
                WHERE id=? AND updated_at < ?
                  AND status IN (?, ?, ?, ?)
                """,
                (str(outbox_id), cutoff, *terminal_statuses),
            )
            removed += cursor.rowcount
    return removed


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


def _gateway_lease_epoch_value(lease_epoch: int) -> int:
    """校验 fencing epoch，布尔值不能伪装成整数世代。"""
    if isinstance(lease_epoch, bool):
        raise DBError("gateway runtime lease_epoch must be greater than 0")
    try:
        normalized = int(lease_epoch)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway runtime lease_epoch must be an integer") from exc
    if normalized <= 0:
        raise DBError("gateway runtime lease_epoch must be greater than 0")
    return normalized


def acquire_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    ttl_seconds: float,
) -> dict | None:
    """原子获取或接管租约，成功返回实例身份和单调 fencing epoch。"""
    lease_name, instance_id, ttl = _gateway_runtime_lease_values(
        lease_name,
        instance_id,
        ttl_seconds,
    )
    with _immediate_transaction(conn):
        # BEGIN IMMEDIATE 可能等待其他写事务；拿到写锁后再取时间，避免
        # 用等待前的旧时间错误续活或接管 lease。
        now = time.time()
        row = conn.execute(
            """
            SELECT instance_id, lease_epoch, expires_at
            FROM gateway_runtime_lease
            WHERE lease_name=?
            """,
            (lease_name,),
        ).fetchone()
        if row is None:
            lease_epoch = 1
            conn.execute(
                """
                INSERT INTO gateway_runtime_lease (
                    lease_name, instance_id, lease_epoch,
                    heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (lease_name, instance_id, lease_epoch, now, now + ttl),
            )
        else:
            current_instance = str(row[0])
            current_epoch = _gateway_lease_epoch_value(row[1])
            current_expires_at = float(row[2])
            if current_instance == instance_id and current_expires_at > now:
                lease_epoch = current_epoch
            elif current_expires_at <= now:
                lease_epoch = current_epoch + 1
            else:
                return None
            cursor = conn.execute(
                """
                UPDATE gateway_runtime_lease
                SET instance_id=?, lease_epoch=?, heartbeat_at=?, expires_at=?
                WHERE lease_name=? AND instance_id=? AND lease_epoch=?
                """,
                (
                    instance_id,
                    lease_epoch,
                    now,
                    now + ttl,
                    lease_name,
                    current_instance,
                    current_epoch,
                ),
            )
            if cursor.rowcount != 1:
                return None
    return {
        "instance_id": instance_id,
        "lease_epoch": lease_epoch,
    }


def renew_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    ttl_seconds: float,
) -> bool:
    """仅允许当前 owner 和 epoch 续租，续租不改变 epoch。"""
    lease_name, instance_id, ttl = _gateway_runtime_lease_values(
        lease_name,
        instance_id,
        ttl_seconds,
    )
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    with _immediate_transaction(conn):
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?, expires_at=?
            WHERE lease_name=? AND instance_id=? AND lease_epoch=?
              AND expires_at > ?
            """,
            (
                now,
                now + ttl,
                lease_name,
                instance_id,
                normalized_epoch,
                now,
            ),
        )
        return cursor.rowcount == 1


def release_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """仅让当前 epoch 立即过期，并保留世代供下次接管递增。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    with _immediate_transaction(conn):
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?, expires_at=0
            WHERE lease_name=? AND instance_id=? AND lease_epoch=?
            """,
            (
                now,
                lease_name,
                instance_id,
                normalized_epoch,
            ),
        )
        return cursor.rowcount == 1


def gateway_runtime_lease_is_valid(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    *,
    now: float | None = None,
) -> bool:
    """同时校验 lease owner、epoch 和有效期。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    timestamp = time.time() if now is None else float(now)
    row = conn.execute(
        """
        SELECT 1
        FROM gateway_runtime_lease
        WHERE lease_name=? AND instance_id=? AND lease_epoch=?
          AND expires_at>?
        """,
        (lease_name, instance_id, normalized_epoch, timestamp),
    ).fetchone()
    return row is not None


def check_gateway_runtime_readiness(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    *,
    now: float | None = None,
) -> bool:
    """在一个轻量写事务内检查数据库可读写且当前 fencing lease 有效。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    timestamp = time.time() if now is None else float(now)
    with _immediate_transaction(conn):
        schema_row = conn.execute(
            "SELECT 1 FROM schema_version WHERE version=?",
            (LATEST_SCHEMA_VERSION,),
        ).fetchone()
        if schema_row is None:
            return False
        # 只刷新观测时间，不延长 expires_at；健康请求不能替代正式续租。
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?
            WHERE lease_name=? AND instance_id=? AND lease_epoch=?
              AND expires_at>?
            """,
            (
                timestamp,
                lease_name,
                instance_id,
                normalized_epoch,
                timestamp,
            ),
        )
        return cursor.rowcount == 1


def _gateway_outbox_fence_values(
    lease_name: str | None,
    instance_id: str | None,
    lease_epoch: int | None,
) -> tuple[str, str, int] | None:
    """规范化可选 fencing；全空仅保留旧嵌入式调用兼容。"""
    values = (lease_name, instance_id, lease_epoch)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise DBError("gateway Outbox fencing identity is incomplete")
    normalized_name = str(lease_name or "")
    normalized_instance = str(instance_id or "")
    if not normalized_name or not normalized_instance:
        raise DBError("gateway Outbox fencing identity must not be empty")
    return (
        normalized_name,
        normalized_instance,
        _gateway_lease_epoch_value(lease_epoch),
    )


def gateway_outbox_claim_is_valid(
    conn: sqlite3.Connection,
    outbox_id: str,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    *,
    now: float | None = None,
) -> bool:
    """确认数据库租约和 Outbox claim 同时属于当前 fencing 身份。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    assert fence is not None
    timestamp = time.time() if now is None else float(now)
    row = conn.execute(
        """
        SELECT 1
        FROM gateway_outbox AS outbox
        WHERE outbox.id=?
          AND outbox.status='sending'
          AND outbox.claimed_by=?
          AND outbox.claim_epoch=?
          AND EXISTS (
              SELECT 1
              FROM gateway_runtime_lease AS lease
              WHERE lease.lease_name=?
                AND lease.instance_id=?
                AND lease.lease_epoch=?
                AND lease.expires_at>?
          )
        """,
        (
            outbox_id,
            fence[1],
            fence[2],
            fence[0],
            fence[1],
            fence[2],
            timestamp,
        ),
    ).fetchone()
    return row is not None


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
    """原子更新当前映射，并登记该 route 的历史对话归属。"""
    now = time.time()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO gateway_session_routes
                (route_key, conversation_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(route_key) DO UPDATE SET
                conversation_id=excluded.conversation_id,
                updated_at=excluded.updated_at
            """,
            (route_key, conversation_id, now),
        )
        conn.execute(
            """
            INSERT INTO gateway_route_conversations (
                route_key, conversation_id, created_at, last_selected_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(route_key, conversation_id) DO UPDATE SET
                last_selected_at=excluded.last_selected_at
            """,
            (route_key, conversation_id, now, now),
        )


def _gateway_conversation_summary(row: sqlite3.Row | tuple) -> dict:
    """把固定列顺序的对话摘要查询结果转换成上层稳定结构。"""
    return {
        "conversation_id": str(row[0]),
        "message_count": int(row[1] or 0),
        "last_message_at": (
            float(row[2]) if row[2] is not None else None
        ),
        "last_selected_at": float(row[3]),
        "preview": str(row[4] or ""),
        "is_current": bool(row[5]),
    }


def list_gateway_conversations(
    conn: sqlite3.Connection,
    route_key: str,
    limit: int = 10,
) -> list[dict]:
    """列出单条 route 最近的对话；查询边界不能跨越 route_key。"""
    normalized_limit = max(1, min(10, int(limit)))
    rows = conn.execute(
        """
        WITH route_conversations AS (
            SELECT
                route_key,
                conversation_id,
                last_selected_at
            FROM gateway_route_conversations
            WHERE route_key=?
        ),
        user_stats AS (
            SELECT
                message.session_id,
                COUNT(*) AS message_count,
                MAX(message.timestamp) AS last_message_at
            FROM messages AS message
            INNER JOIN route_conversations AS route_conversation
                ON route_conversation.conversation_id=message.session_id
            WHERE message.role='user'
            GROUP BY message.session_id
        )
        SELECT
            route_conversation.conversation_id,
            COALESCE(user_stat.message_count, 0),
            user_stat.last_message_at,
            route_conversation.last_selected_at,
            COALESCE((
                SELECT message.content
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
                  AND TRIM(
                      COALESCE(message.content, ''),
                      char(9) || char(10) || char(13) || ' '
                  )<>''
                ORDER BY message.timestamp DESC, message.id DESC
                LIMIT 1
            ), ''),
            CASE
                WHEN current_route.conversation_id=
                     route_conversation.conversation_id
                THEN 1 ELSE 0
            END
        FROM route_conversations AS route_conversation
        LEFT JOIN user_stats AS user_stat
            ON user_stat.session_id=route_conversation.conversation_id
        LEFT JOIN gateway_session_routes AS current_route
            ON current_route.route_key=route_conversation.route_key
        ORDER BY
            COALESCE(
                user_stat.last_message_at,
                route_conversation.last_selected_at
            ) DESC,
            route_conversation.last_selected_at DESC,
            route_conversation.conversation_id ASC
        LIMIT ?
        """,
        (route_key, normalized_limit),
    ).fetchall()
    return [_gateway_conversation_summary(row) for row in rows]


def get_gateway_conversation_for_route(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> dict | None:
    """按 route 校验并读取对话摘要；找不到时不泄露跨 route 信息。"""
    row = conn.execute(
        """
        SELECT
            route_conversation.conversation_id,
            (
                SELECT COUNT(*)
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
            ),
            (
                SELECT MAX(message.timestamp)
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
            ),
            route_conversation.last_selected_at,
            COALESCE((
                SELECT message.content
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
                  AND TRIM(
                      COALESCE(message.content, ''),
                      char(9) || char(10) || char(13) || ' '
                  )<>''
                ORDER BY message.timestamp DESC, message.id DESC
                LIMIT 1
            ), ''),
            CASE
                WHEN current_route.conversation_id=
                     route_conversation.conversation_id
                THEN 1 ELSE 0
            END
        FROM gateway_route_conversations AS route_conversation
        LEFT JOIN gateway_session_routes AS current_route
            ON current_route.route_key=route_conversation.route_key
        WHERE route_conversation.route_key=?
          AND route_conversation.conversation_id=?
        """,
        (route_key, conversation_id),
    ).fetchone()
    return _gateway_conversation_summary(row) if row is not None else None


def _enqueue_gateway_message_in_transaction(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    event_json: str,
    *,
    task_kind: str,
    approval_id: str | None,
) -> bool:
    """在调用方事务内写入 Runner queue 与原始消息归属。"""
    if task_kind not in {"external", "approval_resume"}:
        raise DBError("invalid gateway queue task kind")
    if task_kind == "approval_resume" and not approval_id:
        raise DBError("approval resume queue task is missing approval id")
    if task_kind == "external" and approval_id is not None:
        raise DBError("external gateway queue task cannot bind an approval")

    incoming_source_ids = gateway_event_source_message_ids(
        event_json,
        message_id,
    )
    now = time.time()
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
        INSERT OR IGNORE INTO gateway_message_queue (
            route_key, message_id, event_json, status, task_kind,
            approval_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
        """,
        (
            route_key,
            message_id,
            event_json,
            task_kind,
            approval_id,
            now,
            now,
        ),
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


def enqueue_gateway_message(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    event_json: str,
) -> bool:
    """原子写入外部 Runner queue 与全部原始消息的 Queue 归属。"""
    with transaction(conn):
        return _enqueue_gateway_message_in_transaction(
            conn,
            route_key,
            message_id,
            event_json,
            task_kind="external",
            approval_id=None,
        )


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
        SELECT route_key, message_id, event_json, status, task_kind, approval_id
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
            "task_kind": task_kind,
            "approval_id": approval_id,
        }
        for (
            route_key,
            message_id,
            event_json,
            status,
            task_kind,
            approval_id,
        ) in rows
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
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
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

    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    now = time.time()
    queue_message_id = str(
        outbox.get("queue_message_id") or outbox["source_message_id"]
    )
    values = (
        str(outbox["id"]),
        str(outbox["route_key"]),
        str(outbox["source_message_id"]),
        queue_message_id,
        str(outbox["event_json"]),
        str(outbox["platform"]),
        str(outbox["chat_id"]),
        outbox.get("reply_to_message_id"),
        outbox.get("thread_id"),
        str(outbox["delivery_kind"]),
        _serialize_gateway_json(outbox["payloads"], "payloads"),
        now,
        now,
    )
    if fence is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO gateway_outbox (
                id, route_key, source_message_id, queue_message_id,
                event_json, platform,
                chat_id, reply_to_message_id, thread_id, delivery_kind,
                payloads_json, next_chunk_index, message_ids_json, status,
                attempt_count, next_attempt_at, last_error, last_error_code,
                claimed_by, claim_epoch, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', 'pending', 0,
                      NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            values,
        )
        fence_clause = ""
        fence_params: tuple = ()
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO gateway_outbox (
                id, route_key, source_message_id, queue_message_id,
                event_json, platform,
                chat_id, reply_to_message_id, thread_id, delivery_kind,
                payloads_json, next_chunk_index, message_ids_json, status,
                attempt_count, next_attempt_at, last_error, last_error_code,
                claimed_by, claim_epoch, created_at, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', 'pending', 0,
                   NULL, NULL, NULL, NULL, NULL, ?, ?
            WHERE EXISTS (
                SELECT 1
                FROM gateway_runtime_lease
                WHERE lease_name=? AND instance_id=? AND lease_epoch=?
                  AND expires_at>?
            )
            """,
            (*values, fence[0], fence[1], fence[2], now),
        )
        fence_clause = """
            AND EXISTS (
                SELECT 1
                FROM gateway_runtime_lease
                WHERE lease_name=? AND instance_id=? AND lease_epoch=?
                  AND expires_at>?
            )
        """
        fence_params = (fence[0], fence[1], fence[2], now)
    row = conn.execute(
        f"""
        SELECT id, event_json, status, created_at, updated_at, queue_message_id
        FROM gateway_outbox
        WHERE route_key=? AND source_message_id=? AND delivery_kind=?
        {fence_clause}
        """,
        (
            str(outbox["route_key"]),
            str(outbox["source_message_id"]),
            str(outbox["delivery_kind"]),
            *fence_params,
        ),
    ).fetchone()
    if row is None:
        raise DBError("gateway outbox insert did not create a row")
    outbox_id = str(row[0])
    if str(row[5]) != queue_message_id:
        raise DBError("gateway outbox queue identity mismatch")
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
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> str:
    """持久化回复并把对应入站消息切换到 reply_pending。"""
    with transaction(conn):
        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        _set_gateway_queue_status(
            conn,
            str(outbox["route_key"]),
            str(outbox.get("queue_message_id") or outbox["source_message_id"]),
            "reply_pending",
            time.time(),
        )
    return outbox_id


def _gateway_outbox_row(row) -> dict | None:
    if row is None:
        return None
    (
        outbox_id,
        route_key,
        source_message_id,
        queue_message_id,
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
        claimed_by,
        claim_epoch,
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
        "queue_message_id": str(queue_message_id),
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
        "claimed_by": claimed_by,
        "claim_epoch": None if claim_epoch is None else int(claim_epoch),
    }


_GATEWAY_OUTBOX_COLUMNS = """
    id, route_key, source_message_id, queue_message_id, event_json, platform, chat_id,
    reply_to_message_id, thread_id, delivery_kind, payloads_json,
    next_chunk_index, message_ids_json, status, attempt_count,
    next_attempt_at, last_error, last_error_code, claimed_by, claim_epoch
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
                "queue_message_id": str(row[3]),
                "event_json": str(row[4]),
                "platform": str(row[5]),
                "status": str(row[13]),
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


def _gateway_outbox_lease_clause(
    fence: tuple[str, str, int] | None,
    timestamp: float,
) -> tuple[str, tuple]:
    """生成必须在同一 UPDATE 中成立的数据库租约条件。"""
    if fence is None:
        return "", ()
    return (
        """
        AND EXISTS (
            SELECT 1
            FROM gateway_runtime_lease AS lease
            WHERE lease.lease_name=?
              AND lease.instance_id=?
              AND lease.lease_epoch=?
              AND lease.expires_at>?
        )
        """,
        (fence[0], fence[1], fence[2], timestamp),
    )


def _gateway_outbox_claim_clause(
    fence: tuple[str, str, int] | None,
    timestamp: float,
) -> tuple[str, tuple]:
    """生成 claim 身份与当前数据库租约同时成立的条件。"""
    if fence is None:
        return "", ()
    lease_clause, lease_params = _gateway_outbox_lease_clause(
        fence,
        timestamp,
    )
    return (
        f"""
        AND claimed_by=? AND claim_epoch=?
        {lease_clause}
        """,
        (fence[1], fence[2], *lease_params),
    )


def reset_gateway_sending_outbox(
    conn: sqlite3.Connection,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> int:
    """由当前 epoch 接管启动遗留的 sending，并恢复为 pending。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with transaction(conn):
        now = time.time()
        lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
        rows = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='pending', next_attempt_at=NULL,
                claimed_by=NULL, claim_epoch=NULL, updated_at=?
            WHERE status='sending'
            {lease_clause}
            RETURNING id, route_key, source_message_id, event_json
            """,
            (now, *lease_params),
        ).fetchall()
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
    return len(rows)


def mark_gateway_outbox_sending(
    conn: sqlite3.Connection,
    outbox_id: str,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """由当前有效 epoch claim Outbox 并切换为 sending。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
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
        lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
        claim_assignment = ""
        claim_params: tuple = ()
        if fence is not None:
            claim_assignment = ", claimed_by=?, claim_epoch=?"
            claim_params = (fence[1], fence[2])
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='sending', updated_at=? {claim_assignment}
            WHERE id=? AND status=?
            {lease_clause}
            """,
            (
                now,
                *claim_params,
                outbox_id,
                str(row[3]),
                *lease_params,
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
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """只记录平台成功事实；终态由统一 delivery 事务负责收敛。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
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

    now = time.time()
    claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
    cursor = conn.execute(
        f"""
        UPDATE gateway_outbox
        SET next_chunk_index=?, message_ids_json=?, updated_at=?
        WHERE id=?
          AND next_chunk_index <= ?
          AND status IN (
              'pending', 'sending', 'retry_wait',
              'cancelled', 'partial_cancelled'
          )
          {claim_clause}
        """,
        (
            saved_index,
            _serialize_gateway_json(message_ids, "message_ids"),
            now,
            outbox_id,
            saved_index,
            *claim_params,
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
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
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
        claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='retry_wait', attempt_count=attempt_count + 1,
                next_attempt_at=?, last_error=?, last_error_code=?, updated_at=?
            WHERE id=? AND status='sending'
            {claim_clause}
            """,
            (
                next_attempt_at,
                error,
                error_code,
                now,
                outbox_id,
                *claim_params,
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
        SELECT route_key, source_message_id, queue_message_id, status,
               next_chunk_index, payloads_json, event_json,
               claimed_by, claim_epoch
        FROM gateway_outbox
        WHERE id=?
        """,
        (outbox_id,),
    ).fetchone()


def _finish_gateway_queue_for_delivery(
    conn: sqlite3.Connection,
    route_key: str,
    queue_message_id: str,
    *,
    status: str,
    now: float,
) -> None:
    """在 Outbox 终态事务内同步其实际关联的 Queue 任务。"""
    row = conn.execute(
        """
        SELECT event_json
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, queue_message_id),
    ).fetchone()
    if row is None:
        return
    if status == "delivery_failed":
        _set_gateway_queue_status(
            conn,
            route_key,
            queue_message_id,
            "delivery_failed",
            now,
        )
        return

    ownership_status = "cancelled" if status == "cancelled" else "completed"
    _update_gateway_source_message_ownership_status(
        conn,
        route_key=route_key,
        event_json=str(row[0]),
        fallback_message_id=queue_message_id,
        owner_kind="queue",
        owner_id=queue_message_id,
        status=ownership_status,
        updated_at=now,
    )
    conn.execute(
        """
        DELETE FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, queue_message_id),
    )


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
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """原子完成 Outbox、assistant delivery，并删除对应入站 queue。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
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
        old_status = str(row[3])
        if old_status not in {
            "pending",
            "sending",
            "retry_wait",
            "cancelled",
            "partial_cancelled",
        }:
            return False
        try:
            payloads = json.loads(row[5])
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway outbox JSON deserialization failed: {exc}"
            ) from exc
        if not isinstance(payloads, list):
            raise DBError("gateway outbox payloads JSON has invalid structure")
        if int(row[4]) < len(payloads):
            return False

        claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='delivered', next_attempt_at=NULL,
                last_error=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            {claim_clause}
            """,
            (
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
                *claim_params,
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
            event_json=str(row[6]),
            status="delivered",
            updated_at=now,
        )
        _finish_gateway_queue_for_delivery(
            conn,
            route_key,
            str(row[2]),
            status="completed",
            now=now,
        )
    return True


def fail_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    error: str,
    error_code: str | None,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """原子持久化永久失败，并把入站 queue 留作失败审计。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
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
        old_status = str(row[3])
        if old_status not in {"pending", "sending", "retry_wait"}:
            return False
        claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='permanent_failed', last_error=?, last_error_code=?,
                next_attempt_at=NULL, updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            {claim_clause}
            """,
            (
                safe_error,
                safe_error_code,
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
                *claim_params,
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
            event_json=str(row[6]),
            status="permanent_failed",
            updated_at=now,
        )
        _finish_gateway_queue_for_delivery(
            conn,
            route_key,
            str(row[2]),
            status="delivery_failed",
            now=now,
        )
    return True


def cancel_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """按成功进度原子取消剩余投递，并删除对应入站 queue。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
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
        old_status = str(row[3])
        if old_status not in {
            "pending",
            "sending",
            "retry_wait",
            "cancelled",
            "partial_cancelled",
        }:
            return False
        status = _infer_cancelled_gateway_outbox_status(
            int(row[4]),
            str(row[5]),
        )
        if status == old_status:
            return False

        lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
        claim_assignment = ""
        claim_params: tuple = ()
        if fence is not None:
            claim_assignment = ", claimed_by=?, claim_epoch=?"
            claim_params = (fence[1], fence[2])
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status=?, next_attempt_at=NULL,
                last_error=CASE WHEN ?='delivered' THEN NULL
                                ELSE last_error END,
                last_error_code=CASE WHEN ?='delivered' THEN NULL
                                     ELSE last_error_code END,
                updated_at=? {claim_assignment}
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            {lease_clause}
            """,
            (
                status,
                status,
                status,
                now,
                *claim_params,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
                *lease_params,
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
            event_json=str(row[6]),
            status=status,
            updated_at=now,
        )
        _finish_gateway_queue_for_delivery(
            conn,
            route_key,
            str(row[2]),
            status="cancelled",
            now=now,
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
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> str:
    """原子写入最终 assistant 消息、outbox 和 reply_pending 状态。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        raise InvalidMessageError(
            "gateway final delivery must reference an assistant message"
        )
    with transaction(conn):
        queue_message_id = str(
            outbox.get("queue_message_id") or outbox["source_message_id"]
        )
        existing = conn.execute(
            """
            SELECT id, queue_message_id
            FROM gateway_outbox
            WHERE route_key=? AND source_message_id=? AND delivery_kind=?
            """,
            (
                str(outbox["route_key"]),
                str(outbox["source_message_id"]),
                str(outbox["delivery_kind"]),
            ),
        ).fetchone()
        if existing is not None:
            if str(existing[1]) != queue_message_id:
                raise DBError("gateway final outbox queue identity mismatch")
            _set_gateway_queue_status(
                conn,
                str(outbox["route_key"]),
                queue_message_id,
                "reply_pending",
                time.time(),
            )
            return str(existing[0])

        assistant_message_id = _insert_message(conn, session_id, msg)
        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        _insert_gateway_message_delivery(
            conn,
            delivery_id=outbox_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            route_key=str(outbox["route_key"]),
            source_message_id=str(outbox["source_message_id"]),
        )
        _set_gateway_queue_status(
            conn,
            str(outbox["route_key"]),
            str(outbox.get("queue_message_id") or outbox["source_message_id"]),
            "reply_pending",
            time.time(),
        )
    return outbox_id


# ===========================================================================
# Gateway 远程工具审批
# ===========================================================================

_GATEWAY_APPROVAL_COLUMNS = """
    id, route_key, conversation_id, requester_user_id, source_message_id,
    tool_call_id, tool_message_id, tool_name, tool_args_json, summary,
    details_json, status, decision_message_id, result_content,
    source_event_json, agent_state_json, created_at, expires_at, updated_at
"""


def _normalize_gateway_approval_agent_state(value: dict | None) -> dict:
    """校验并收敛可持久化的最小 AgentLoop 状态。"""
    state = dict(_DEFAULT_GATEWAY_APPROVAL_AGENT_STATE)
    if value is not None:
        if not isinstance(value, dict):
            raise DBError("gateway approval agent state must contain an object")
        state.update(value)

    for field in ("iterations_used", "retry_count", "continuation_count"):
        raw = state.get(field)
        if isinstance(raw, bool):
            raise DBError(f"gateway approval agent state {field} is invalid")
        try:
            normalized = int(raw)
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway approval agent state {field} is invalid"
            ) from exc
        if normalized < 0:
            raise DBError(f"gateway approval agent state {field} is invalid")
        state[field] = normalized

    if not isinstance(state.get("using_fallback"), bool):
        raise DBError("gateway approval fallback state is invalid")
    if not isinstance(state.get("active_model"), str):
        raise DBError("gateway approval active model is invalid")
    return {
        "iterations_used": state["iterations_used"],
        "retry_count": state["retry_count"],
        "continuation_count": state["continuation_count"],
        "using_fallback": state["using_fallback"],
        "active_model": state["active_model"],
    }


def _gateway_approval_row(row) -> dict | None:
    """把审批查询行还原为上层可使用的结构。"""
    if row is None:
        return None
    try:
        tool_args = json.loads(row[8])
        details = json.loads(row[10])
        agent_state = json.loads(row[15])
    except (TypeError, ValueError) as exc:
        raise DBError(f"gateway approval JSON deserialization failed: {exc}") from exc
    if not isinstance(tool_args, dict) or not isinstance(details, dict):
        raise DBError("gateway approval JSON has invalid structure")
    agent_state = _normalize_gateway_approval_agent_state(agent_state)
    return {
        "id": str(row[0]),
        "route_key": str(row[1]),
        "conversation_id": str(row[2]),
        "requester_user_id": str(row[3]),
        "source_message_id": str(row[4]),
        "tool_call_id": str(row[5]),
        "tool_message_id": int(row[6]),
        "tool_name": str(row[7]),
        "tool_args": tool_args,
        "summary": str(row[9]),
        "details": details,
        "status": str(row[11]),
        "decision_message_id": (
            str(row[12]) if row[12] is not None else None
        ),
        "result_content": (
            str(row[13]) if row[13] is not None else None
        ),
        "source_event_json": (
            str(row[14]) if row[14] is not None else None
        ),
        "agent_state": agent_state,
        "created_at": float(row[16]),
        "expires_at": float(row[17]),
        "updated_at": float(row[18]),
    }


def _approval_terminal_content(request_id: str, status: str) -> str:
    """生成拒绝、过期等未执行审批的最终 Tool Result。"""
    return json.dumps(
        {
            "ok": False,
            "error_type": f"approval_{status}",
            "approval_request_id": request_id,
            "error": f"Tool operation was not executed: approval {status}.",
        },
        ensure_ascii=False,
    )


def _expire_gateway_approvals_in_transaction(
    conn: sqlite3.Connection,
    now: float,
) -> int:
    """在调用方事务内把超时请求转成终态，并同步 Tool Result。"""
    rows = conn.execute(
        """
        SELECT id, tool_message_id
        FROM gateway_approval_requests
        WHERE status='pending' AND expires_at<=?
        """,
        (now,),
    ).fetchall()
    for request_id, tool_message_id in rows:
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (
                _approval_terminal_content(str(request_id), "expired"),
                int(tool_message_id),
            ),
        )
    if rows:
        conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status='expired', updated_at=?
            WHERE status='pending' AND expires_at<=?
            """,
            (now, now),
        )
    return len(rows)


def expire_gateway_approvals(
    conn: sqlite3.Connection,
    now: float | None = None,
) -> int:
    """公开的审批过期收敛入口。"""
    effective_now = time.time() if now is None else float(now)
    with transaction(conn):
        return _expire_gateway_approvals_in_transaction(conn, effective_now)


def recover_gateway_approvals(conn: sqlite3.Connection) -> dict:
    """启动恢复：过期 pending，executing 转为不可重试的未知结果。"""
    now = time.time()
    with transaction(conn):
        expired = _expire_gateway_approvals_in_transaction(conn, now)
        rows = conn.execute(
            """
            SELECT id, tool_message_id
            FROM gateway_approval_requests
            WHERE status='executing'
            """
        ).fetchall()
        for request_id, tool_message_id in rows:
            conn.execute(
                "UPDATE messages SET content=? WHERE id=?",
                (
                    _approval_terminal_content(
                        str(request_id),
                        "execution_unknown",
                    ),
                    int(tool_message_id),
                ),
            )
        if rows:
            conn.execute(
                """
                UPDATE gateway_approval_requests
                SET status='execution_unknown', updated_at=?
                WHERE status='executing'
                """,
                (now,),
            )
    return {"expired": expired, "execution_unknown": len(rows)}


def create_gateway_approval_with_outbox(
    conn: sqlite3.Connection,
    session_id: str,
    request: dict,
    requester_user_id: str,
    assistant_msg: dict,
    outbox: dict,
    ttl_seconds: float,
    *,
    agent_state: dict | None = None,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> str:
    """原子写入审批请求、审批问题及其 Outbox。"""
    outbox = dict(outbox)
    request_id = str(request.get("id", ""))
    tool_name = str(request.get("tool_name", ""))
    tool_call_id = str(request.get("tool_call_id", ""))
    tool_args = request.get("arguments")
    details = request.get("details", {})
    if not request_id.startswith("approval_"):
        raise DBError("invalid gateway approval request id")
    if tool_name not in {"file", "terminal"}:
        raise DBError("invalid gateway approval tool")
    if not tool_call_id or not isinstance(tool_args, dict):
        raise DBError("invalid gateway approval tool call")
    if not isinstance(details, dict):
        raise DBError("invalid gateway approval details")
    if not isinstance(assistant_msg, dict) or assistant_msg.get("role") != "assistant":
        raise InvalidMessageError("approval delivery must reference an assistant message")
    source_event_json = str(outbox.get("event_json", "") or "")
    if not source_event_json:
        raise DBError("gateway approval source event is missing")
    source_message_id = str(outbox.get("source_message_id", "") or "")
    source_ids = gateway_event_source_message_ids(
        source_event_json,
        source_message_id,
    )
    if source_message_id not in source_ids:
        raise DBError("gateway approval source event identity mismatch")
    normalized_agent_state = _normalize_gateway_approval_agent_state(agent_state)
    encoded_tool_args = _serialize_gateway_json(tool_args, "approval tool args")
    encoded_details = _serialize_gateway_json(details, "approval details")
    encoded_agent_state = _serialize_gateway_json(
        normalized_agent_state,
        "approval agent state",
    )
    ttl = float(ttl_seconds)
    if ttl <= 0:
        raise DBError("gateway approval ttl must be positive")

    now = time.time()
    with transaction(conn):
        if str(outbox.get("delivery_kind", "")) == "approval_request":
            first_request = conn.execute(
                """
                SELECT id
                FROM gateway_approval_requests
                WHERE route_key=? AND source_message_id=?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (str(outbox["route_key"]), source_message_id),
            ).fetchone()
            if first_request is not None and str(first_request[0]) != request_id:
                outbox["delivery_kind"] = f"approval_request:{request_id}"

        tool_row = conn.execute(
            """
            SELECT id, content
            FROM messages
            WHERE session_id=? AND role='tool' AND tool_call_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id, tool_call_id),
        ).fetchone()
        if tool_row is None or request_id not in str(tool_row[1] or ""):
            raise DBError("approval request is not bound to its tool result")

        existing_row = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE id=?
            """,
            (request_id,),
        ).fetchone()
        if existing_row is not None:
            existing = _gateway_approval_row(existing_row)
            if (
                existing["route_key"] != str(outbox["route_key"])
                or existing["conversation_id"] != session_id
                or existing["source_message_id"] != source_message_id
                or existing["tool_call_id"] != tool_call_id
                or existing["tool_message_id"] != int(tool_row[0])
                or existing["tool_name"] != tool_name
                or existing["tool_args"] != tool_args
                or existing["source_event_json"] != source_event_json
                or existing["agent_state"] != normalized_agent_state
            ):
                raise DBError("gateway approval idempotency identity mismatch")
            outbox_row = conn.execute(
                """
                SELECT id
                FROM gateway_outbox
                WHERE route_key=? AND source_message_id=?
                  AND delivery_kind=?
                """,
                (
                    str(outbox["route_key"]),
                    source_message_id,
                    str(outbox["delivery_kind"]),
                ),
            ).fetchone()
            if outbox_row is None:
                raise DBError("gateway approval is missing its outbox")
            return str(outbox_row[0])

        conn.execute(
            """
            INSERT INTO gateway_approval_requests (
                id, route_key, conversation_id, requester_user_id,
                source_message_id, tool_call_id, tool_message_id, tool_name,
                tool_args_json, summary, details_json, status,
                source_event_json, agent_state_json,
                created_at, expires_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?
            )
            """,
            (
                request_id,
                str(outbox["route_key"]),
                session_id,
                str(requester_user_id or ""),
                source_message_id,
                tool_call_id,
                int(tool_row[0]),
                tool_name,
                encoded_tool_args,
                str(request.get("summary", "需要批准的工具操作")),
                encoded_details,
                source_event_json,
                encoded_agent_state,
                now,
                now + ttl,
                now,
            ),
        )
        assistant_message_id = _insert_message(conn, session_id, assistant_msg)
        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        _insert_gateway_message_delivery(
            conn,
            delivery_id=outbox_id,
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            route_key=str(outbox["route_key"]),
            source_message_id=str(outbox["source_message_id"]),
        )
        _set_gateway_queue_status(
            conn,
            str(outbox["route_key"]),
            str(outbox.get("queue_message_id") or source_message_id),
            "reply_pending",
            now,
        )
    return outbox_id


def get_pending_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> dict | None:
    """返回当前对话最早的未决审批；读取前先收敛过期状态。"""
    expire_gateway_approvals(conn)
    row = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE route_key=? AND conversation_id=? AND status='pending'
        ORDER BY created_at
        LIMIT 1
        """,
        (route_key, conversation_id),
    ).fetchone()
    return _gateway_approval_row(row)


def _select_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    selector: str,
) -> tuple[str, dict | None]:
    """按 route 内的完整 ID 或唯一前缀选择审批请求。"""
    normalized = str(selector or "").strip()
    if not normalized or any(
        not (char.isalnum() or char in {"_", "-"})
        for char in normalized
    ):
        return "invalid_id", None
    prefix = normalized if normalized.startswith("approval_") else f"approval_{normalized}"
    rows = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE route_key=? AND substr(id, 1, ?)=?
        ORDER BY created_at DESC
        LIMIT 2
        """,
        (route_key, len(prefix), prefix),
    ).fetchall()
    if not rows:
        return "not_found", None
    if len(rows) > 1:
        return "ambiguous", None
    return "found", _gateway_approval_row(rows[0])


def claim_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    requester_user_id: str,
    selector: str,
    decision_message_id: str,
) -> dict:
    """校验审批归属并以 CAS 把 pending 转为 executing。"""
    now = time.time()
    with transaction(conn):
        _expire_gateway_approvals_in_transaction(conn, now)
        outcome, request = _select_gateway_approval(conn, route_key, selector)
        if request is None:
            return {"outcome": outcome}
        if request["conversation_id"] != conversation_id:
            return {"outcome": "stale_conversation", "request": request}
        if (
            request["requester_user_id"]
            and request["requester_user_id"] != str(requester_user_id or "")
        ):
            return {"outcome": "forbidden", "request": request}
        if request["status"] != "pending":
            return {"outcome": request["status"], "request": request}
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status='executing', decision_message_id=?, updated_at=?
            WHERE id=? AND status='pending'
            """,
            (decision_message_id, now, request["id"]),
        ).rowcount
        if changed != 1:
            return {"outcome": "conflict"}
        request["status"] = "executing"
        request["decision_message_id"] = decision_message_id
        return {"outcome": "claimed", "request": request}


def deny_gateway_approval(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    requester_user_id: str,
    selector: str,
    decision_message_id: str,
) -> dict:
    """校验审批归属并把 pending 原子转为 denied。"""
    now = time.time()
    with transaction(conn):
        _expire_gateway_approvals_in_transaction(conn, now)
        outcome, request = _select_gateway_approval(conn, route_key, selector)
        if request is None:
            return {"outcome": outcome}
        if request["conversation_id"] != conversation_id:
            return {"outcome": "stale_conversation", "request": request}
        if (
            request["requester_user_id"]
            and request["requester_user_id"] != str(requester_user_id or "")
        ):
            return {"outcome": "forbidden", "request": request}
        if request["status"] != "pending":
            return {"outcome": request["status"], "request": request}
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status='denied', decision_message_id=?, updated_at=?
            WHERE id=? AND status='pending'
            """,
            (decision_message_id, now, request["id"]),
        ).rowcount
        if changed != 1:
            return {"outcome": "conflict"}
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (
                _approval_terminal_content(request["id"], "denied"),
                request["tool_message_id"],
            ),
        )
        request["status"] = "denied"
        return {"outcome": "denied", "request": request}


def finish_gateway_approval(
    conn: sqlite3.Connection,
    request_id: str,
    result_content: str,
    *,
    succeeded: bool,
) -> dict:
    """保存一次性执行结果，并用真实结果替换 awaiting Tool Result。"""
    final_status = "executed" if succeeded else "failed"
    now = time.time()
    with transaction(conn):
        row = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE id=?
            """,
            (request_id,),
        ).fetchone()
        request = _gateway_approval_row(row)
        if request is None:
            raise DBError("gateway approval request not found")
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status=?, result_content=?, updated_at=?
            WHERE id=? AND status='executing'
            """,
            (final_status, str(result_content), now, request_id),
        ).rowcount
        if changed != 1:
            raise DBError("gateway approval is not executing")
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (str(result_content), request["tool_message_id"]),
        )
        request["status"] = final_status
        request["result_content"] = str(result_content)
        return request


def _approval_resume_message_id(request_id: str) -> str:
    normalized = str(request_id or "")
    if not normalized.startswith("approval_"):
        raise DBError("invalid gateway approval request id")
    return f"approval-resume:{normalized}"


def _build_gateway_approval_resume_event(
    request: dict,
) -> tuple[str, str]:
    """从审批保存的原始事件构造最小内部恢复事件。"""
    source_event_json = request.get("source_event_json")
    if not isinstance(source_event_json, str) or not source_event_json:
        raise DBError("gateway approval source event is unavailable")
    try:
        source_payload = json.loads(source_event_json)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway approval source event is invalid") from exc
    if not isinstance(source_payload, dict):
        raise DBError("gateway approval source event must contain an object")
    source = source_payload.get("source")
    if not isinstance(source, dict):
        raise DBError("gateway approval source identity is invalid")
    if str(source_payload.get("message_id", "")) != request["source_message_id"]:
        raise DBError("gateway approval source message identity mismatch")

    message_id = _approval_resume_message_id(request["id"])
    event = {
        "message_id": message_id,
        "text": "",
        "message_type": "text",
        "media_urls": [],
        "reply_to_message_id": None,
        "attachments": [],
        "metadata": {
            "gateway_internal_task": "approval_resume",
            "approval_id": request["id"],
        },
        "source": dict(source),
    }
    return message_id, _serialize_gateway_json(event, "approval resume event")


def _gateway_approval_resume_task_row(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT event_json, status, task_kind, approval_id
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "route_key": route_key,
        "message_id": message_id,
        "event_json": str(row[0]),
        "status": str(row[1]),
        "task_kind": str(row[2]),
        "approval_id": str(row[3]) if row[3] is not None else None,
    }


def finish_gateway_approval_and_enqueue_resume(
    conn: sqlite3.Connection,
    request_id: str,
    result_content: str,
    *,
    succeeded: bool,
) -> dict:
    """原子固化工具结果、审批终态和唯一的 history-only 恢复任务。"""
    final_status = "executed" if succeeded else "failed"
    with transaction(conn):
        row = conn.execute(
            f"""
            SELECT {_GATEWAY_APPROVAL_COLUMNS}
            FROM gateway_approval_requests
            WHERE id=?
            """,
            (request_id,),
        ).fetchone()
        request = _gateway_approval_row(row)
        if request is None:
            raise DBError("gateway approval request not found")

        # 终态重复调用只返回已有事实，绝不重新创建已经完成的恢复任务。
        if request["status"] in {"executed", "failed"}:
            message_id = _approval_resume_message_id(request["id"])
            return {
                "approval": request,
                "resume_task": _gateway_approval_resume_task_row(
                    conn,
                    request["route_key"],
                    message_id,
                ),
                "already_finished": True,
            }
        if request["status"] != "executing":
            raise DBError("gateway approval is not executing")

        message_id, event_json = _build_gateway_approval_resume_event(request)
        now = time.time()
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            (str(result_content), request["tool_message_id"]),
        )
        changed = conn.execute(
            """
            UPDATE gateway_approval_requests
            SET status=?, result_content=?, updated_at=?
            WHERE id=? AND status='executing'
            """,
            (final_status, str(result_content), now, request_id),
        ).rowcount
        if changed != 1:
            raise DBError("gateway approval terminal transition failed")

        accepted = _enqueue_gateway_message_in_transaction(
            conn,
            request["route_key"],
            message_id,
            event_json,
            task_kind="approval_resume",
            approval_id=request["id"],
        )
        if not accepted:
            raise DBError("gateway approval resume queue identity is occupied")
        task = _gateway_approval_resume_task_row(
            conn,
            request["route_key"],
            message_id,
        )
        if (
            task is None
            or task["task_kind"] != "approval_resume"
            or task["approval_id"] != request["id"]
            or task["event_json"] != event_json
        ):
            raise DBError("gateway approval resume task identity mismatch")

        request["status"] = final_status
        request["result_content"] = str(result_content)
        request["updated_at"] = now
        return {
            "approval": request,
            "resume_task": task,
            "already_finished": False,
        }


def get_gateway_approval_resume(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
    message_id: str,
    approval_id: str,
) -> dict | None:
    """验证数据库内部恢复任务与审批终态的完整绑定。"""
    expected_message_id = _approval_resume_message_id(approval_id)
    if str(message_id) != expected_message_id:
        return None
    task = _gateway_approval_resume_task_row(
        conn,
        str(route_key),
        expected_message_id,
    )
    if (
        task is None
        or task["status"] not in {"queued", "processing"}
        or task["task_kind"] != "approval_resume"
        or task["approval_id"] != approval_id
    ):
        return None

    row = conn.execute(
        f"""
        SELECT {_GATEWAY_APPROVAL_COLUMNS}
        FROM gateway_approval_requests
        WHERE id=? AND route_key=? AND conversation_id=?
          AND status IN ('executed', 'failed')
        """,
        (approval_id, route_key, conversation_id),
    ).fetchone()
    approval = _gateway_approval_row(row)
    if approval is None:
        return None
    expected_id, expected_event_json = _build_gateway_approval_resume_event(approval)
    if expected_id != message_id or task["event_json"] != expected_event_json:
        return None

    source_event_json = approval.get("source_event_json")
    source_ids = gateway_event_source_message_ids(
        str(source_event_json),
        approval["source_message_id"],
    )
    if approval["source_message_id"] not in source_ids:
        return None
    return {
        "approval": approval,
        "resume_task": task,
    }


def cancel_pending_gateway_approvals(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> int:
    """取消当前对话的全部 pending 请求，不触碰已经开始执行的请求。"""
    now = time.time()
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT id, tool_message_id
            FROM gateway_approval_requests
            WHERE route_key=? AND conversation_id=? AND status='pending'
            """,
            (route_key, conversation_id),
        ).fetchall()
        for request_id, tool_message_id in rows:
            conn.execute(
                "UPDATE messages SET content=? WHERE id=?",
                (
                    _approval_terminal_content(str(request_id), "cancelled"),
                    int(tool_message_id),
                ),
            )
        if rows:
            conn.execute(
                """
                UPDATE gateway_approval_requests
                SET status='cancelled', updated_at=?
                WHERE route_key=? AND conversation_id=? AND status='pending'
                """,
                (now, route_key, conversation_id),
            )
    return len(rows)


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
