from __future__ import annotations

import sqlite3

from ..database import _table_columns

def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gateway_session_routes (
            route_key TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gateway_route_conversations (
            route_key TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_selected_at REAL NOT NULL,
            PRIMARY KEY (route_key, conversation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_route_conversations_recent
            ON gateway_route_conversations(route_key, last_selected_at DESC);

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

        CREATE TABLE IF NOT EXISTS gateway_outbox (
            id TEXT PRIMARY KEY, route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL, queue_message_id TEXT NOT NULL,
            event_json TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,
            reply_to_message_id TEXT, thread_id TEXT, delivery_kind TEXT NOT NULL,
            payloads_json TEXT NOT NULL, next_chunk_index INTEGER NOT NULL DEFAULT 0,
            message_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL CHECK (status IN (
                'pending', 'sending', 'retry_wait', 'delivered',
                'cancelled', 'partial_cancelled', 'permanent_failed'
            )),
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL,
            last_error TEXT, last_error_code TEXT, claimed_by TEXT,
            claim_epoch INTEGER CHECK (claim_epoch IS NULL OR claim_epoch > 0),
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            CHECK ((claimed_by IS NULL AND claim_epoch IS NULL)
                OR (claimed_by IS NOT NULL AND claim_epoch IS NOT NULL)),
            UNIQUE(route_key, source_message_id, delivery_kind)
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_outbox_status_retry
            ON gateway_outbox(status, next_attempt_at, created_at);

        CREATE TABLE IF NOT EXISTS gateway_message_deliveries (
            delivery_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            assistant_message_id INTEGER NOT NULL UNIQUE, route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'pending', 'delivered', 'cancelled', 'partial_cancelled',
                'permanent_failed'
            )),
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            FOREIGN KEY (delivery_id) REFERENCES gateway_outbox(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_gateway_message_deliveries_session_status
            ON gateway_message_deliveries(session_id, status, assistant_message_id);
    """)

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

