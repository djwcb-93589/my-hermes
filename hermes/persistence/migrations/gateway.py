from __future__ import annotations

import json
import sqlite3

from ..database import DBError, _table_columns, _table_exists
from ..schemas.gateway import _create_gateway_runtime_lease_schema, _create_gateway_source_message_ownership_schema, _create_gateway_fencing_triggers

def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_session_routes (
            route_key TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
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


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
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


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_message_deliveries (
            delivery_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            assistant_message_id INTEGER NOT NULL UNIQUE,
            route_key TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'delivered', 'cancelled', 'permanent_failed')
            ),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (delivery_id) REFERENCES gateway_outbox(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gateway_message_deliveries_session_status "
        "ON gateway_message_deliveries(session_id, status, assistant_message_id)"
    )

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
    from ..gateway import (
        _upsert_gateway_source_message_ownership,
        gateway_event_source_message_ids,
    )

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

