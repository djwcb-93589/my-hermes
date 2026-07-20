from __future__ import annotations

import sqlite3

from ..database import _table_columns, _table_exists
from ..schemas.approval import _create_gateway_approval_schema
from ..schemas.cron import _create_cron_schema
from ..schemas.delivery import _create_gateway_file_delivery_schema

def _migrate_v22_to_v23(conn: sqlite3.Connection) -> None:
    """为 Cron 增加可审计、可撤销的持久能力授权。"""
    columns = _table_columns(conn, "cron_jobs")
    if "capability_spec_json" not in columns:
        conn.execute(
            "ALTER TABLE cron_jobs ADD COLUMN capability_spec_json "
            "TEXT NOT NULL DEFAULT '{}'"
        )
    approval_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gateway_approval_requests'"
    ).fetchone()
    approval_sql = str(approval_sql_row[0] or "") if approval_sql_row else ""
    if "'cron'" not in approval_sql:
        # 三张表按外键从子到父暂存，再按父到子重建，保留历史审批和文件投递。
        if _table_exists(conn, "cron_run_artifacts"):
            conn.execute("ALTER TABLE cron_run_artifacts RENAME TO cron_run_artifacts_v22")
        if _table_exists(conn, "gateway_file_deliveries"):
            conn.execute(
                "ALTER TABLE gateway_file_deliveries RENAME TO gateway_file_deliveries_v22"
            )
        conn.execute(
            "ALTER TABLE gateway_approval_requests RENAME TO gateway_approval_requests_v22"
        )
        _create_gateway_approval_schema(conn)
        _create_gateway_file_delivery_schema(conn)
        _create_cron_schema(conn)
        conn.execute(
            """
            INSERT INTO gateway_approval_requests (
                id, route_key, conversation_id, requester_user_id,
                source_message_id, tool_call_id, tool_message_id, tool_name,
                tool_args_json, summary, details_json, status,
                decision_message_id, result_content, source_event_json,
                agent_state_json, created_at, expires_at, updated_at
            )
            SELECT id, route_key, conversation_id, requester_user_id,
                   source_message_id, tool_call_id, tool_message_id, tool_name,
                   tool_args_json, summary, details_json, status,
                   decision_message_id, result_content, source_event_json,
                   agent_state_json, created_at, expires_at, updated_at
            FROM gateway_approval_requests_v22
            """
        )
        if _table_exists(conn, "gateway_file_deliveries_v22"):
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
                SELECT id, origin_kind, approval_id, cron_run_id, route_key,
                       conversation_id, source_message_id, platform, chat_id,
                       reply_to_message_id, thread_id, local_path, display_name,
                       size_bytes, sha256, platform_file_key, outbox_id, status,
                       attempt_count, next_attempt_at, last_error, last_error_code,
                       claimed_by, claim_epoch, created_at, updated_at
                FROM gateway_file_deliveries_v22
                """
            )
        if _table_exists(conn, "cron_run_artifacts_v22"):
            conn.execute(
                """
                INSERT INTO cron_run_artifacts (
                    artifact_id, run_id, display_name, local_path, size_bytes,
                    sha256, delivery_id, delivery_status, created_at, updated_at
                )
                SELECT artifact_id, run_id, display_name, local_path, size_bytes,
                       sha256, delivery_id, delivery_status, created_at, updated_at
                FROM cron_run_artifacts_v22
                """
            )
            conn.execute("DROP TABLE cron_run_artifacts_v22")
        if _table_exists(conn, "gateway_file_deliveries_v22"):
            conn.execute("DROP TABLE gateway_file_deliveries_v22")
        conn.execute("DROP TABLE gateway_approval_requests_v22")
        _create_gateway_approval_schema(conn)
        _create_gateway_file_delivery_schema(conn)
    _create_cron_schema(conn)


def _migrate_v23_to_v24(conn: sqlite3.Connection) -> None:
    """补齐 Cron 生命周期策略与软删除字段。"""
    columns = _table_columns(conn, "cron_jobs")
    additions = (
        ("retry_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("artifact_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("deleted_at", "REAL"),
    )

