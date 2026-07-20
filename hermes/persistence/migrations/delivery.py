from __future__ import annotations

import sqlite3

from ..database import _table_columns, _table_exists
from ..schemas.approval import _create_gateway_approval_schema
from ..schemas.delivery import _create_gateway_file_delivery_schema

def _migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
    """扩展审批工具白名单，并增加带 fencing 的出站文件任务表。"""
    conn.execute(
        "ALTER TABLE gateway_approval_requests "
        "RENAME TO gateway_approval_requests_v15"
    )
    _create_gateway_approval_schema(conn)
    conn.execute(
        """
        INSERT INTO gateway_approval_requests (
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, result_content, source_event_json,
            agent_state_json, created_at, expires_at, updated_at
        )
        SELECT
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, result_content, source_event_json,
            agent_state_json, created_at, expires_at, updated_at
        FROM gateway_approval_requests_v15
        """
    )
    conn.execute("DROP TABLE gateway_approval_requests_v15")
    # 旧表重命名期间同名索引仍存在，删除旧表后补建新版索引。
    _create_gateway_approval_schema(conn)
    _create_gateway_file_delivery_schema(conn)


def _migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
    """为文件任务增加明确的 Outbox 持久关联。"""
    if "outbox_id" in _table_columns(conn, "gateway_file_deliveries"):
        _create_gateway_file_delivery_schema(conn)
        return
    conn.execute(
        "ALTER TABLE gateway_file_deliveries "
        "RENAME TO gateway_file_deliveries_v16"
    )
    _create_gateway_file_delivery_schema(conn)
    conn.execute(
        """
        INSERT INTO gateway_file_deliveries (
            id, approval_id, route_key, conversation_id,
            source_message_id, platform, chat_id, reply_to_message_id,
            thread_id, local_path, display_name, size_bytes, sha256,
            platform_file_key, status, attempt_count, next_attempt_at,
            last_error, last_error_code, claimed_by, claim_epoch,
            created_at, updated_at
        )
        SELECT
            id, approval_id, route_key, conversation_id,
            source_message_id, platform, chat_id, reply_to_message_id,
            thread_id, local_path, display_name, size_bytes, sha256,
            platform_file_key, status, attempt_count, next_attempt_at,
            last_error, last_error_code, claimed_by, claim_epoch,
            created_at, updated_at
        FROM gateway_file_deliveries_v16
        """
    )
    conn.execute("DROP TABLE gateway_file_deliveries_v16")
    _create_gateway_file_delivery_schema(conn)


def _migrate_v21_to_v22(conn: sqlite3.Connection) -> None:
    """为 Cron 独立产物与共享出站文件任务补齐 origin 边界。"""
    if _table_exists(conn, "gateway_file_deliveries"):
        conn.execute(
            "ALTER TABLE gateway_file_deliveries "
            "RENAME TO gateway_file_deliveries_v21"
        )
        _create_gateway_file_delivery_schema(conn)
        conn.execute(
            """
            INSERT INTO gateway_file_deliveries (
                id, origin_kind, approval_id, cron_run_id, route_key,
                conversation_id, source_message_id, platform, chat_id,
                reply_to_message_id, thread_id, local_path, display_name,
                size_bytes, sha256, platform_file_key, outbox_id, status,
                attempt_count, next_attempt_at, last_error, last_error_code,
                claimed_by, claim_epoch, created_at, updated_at
            )
            SELECT
                id, 'gateway', approval_id, NULL, route_key,
                conversation_id, source_message_id, platform, chat_id,
                reply_to_message_id, thread_id, local_path, display_name,
                size_bytes, sha256, platform_file_key, outbox_id, status,
                attempt_count, next_attempt_at, last_error, last_error_code,
                claimed_by, claim_epoch, created_at, updated_at
            FROM gateway_file_deliveries_v21
            """
        )
        conn.execute("DROP TABLE gateway_file_deliveries_v21")
    else:
        _create_gateway_file_delivery_schema(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cron_run_artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            local_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            sha256 TEXT NOT NULL,
            delivery_id TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'not_requested',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (run_id) REFERENCES cron_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY (delivery_id) REFERENCES gateway_file_deliveries(id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_run_artifacts_run "
        "ON cron_run_artifacts(run_id, created_at, artifact_id)"
    )

