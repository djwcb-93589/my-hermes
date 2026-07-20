from __future__ import annotations

import sqlite3

from ..database import _table_columns


def _create_feishu_pending_attachment_schema(
    conn: sqlite3.Connection,
) -> None:
    """创建等待下一条用户指令的飞书附件记录。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feishu_pending_attachments (
            app_id TEXT NOT NULL,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            attachments_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('awaiting_instruction', 'bound')
            ),
            bound_message_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (app_id, route_key, source_message_id),
            CHECK (
                (state='awaiting_instruction' AND bound_message_id IS NULL)
                OR (state='bound' AND bound_message_id IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feishu_pending_attachment_route
        ON feishu_pending_attachments(
            app_id, route_key, state, created_at, source_message_id
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feishu_pending_attachment_bound
        ON feishu_pending_attachments(
            app_id, route_key, bound_message_id
        )
        WHERE state='bound'
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
    # 旧表无法通过 ALTER TABLE 补表级 CHECK,触发器让迁移库与新库保持
    # 相同的状态集合约束;新库上的 CHECK 则提供双重保护。
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


def create_schema(conn: sqlite3.Connection) -> None:
    """按历史建表顺序创建 Feishu Inbox 与 pending attachment 全部对象。"""
    _create_feishu_inbox_schema(conn)
    _create_feishu_pending_attachment_schema(conn)
