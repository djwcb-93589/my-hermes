"""Gateway 审批领域的历史迁移。"""

from __future__ import annotations

import sqlite3

from ..database import _table_exists
from ..schemas.approval import _create_gateway_approval_schema
from ..schemas.delivery import _create_gateway_file_delivery_schema


_GATEWAY_FILE_DELIVERY_COLUMNS = """
    id, origin_kind, approval_id, cron_run_id, route_key, conversation_id,
    source_message_id, platform, chat_id, reply_to_message_id, thread_id,
    local_path, display_name, size_bytes, sha256, platform_file_key,
    outbox_id, status, attempt_count, next_attempt_at, last_error,
    last_error_code, claimed_by, claim_epoch, created_at, updated_at
"""


def _stash_gateway_file_deliveries(
    conn: sqlite3.Connection,
    suffix: str,
) -> str | None:
    """暂存引用审批表的子表，避免重建父表时触发外键删除限制。"""
    if not _table_exists(conn, "gateway_file_deliveries"):
        return None
    temporary_name = f"gateway_file_deliveries_{suffix}"
    conn.execute(
        f"ALTER TABLE gateway_file_deliveries RENAME TO {temporary_name}"
    )
    return temporary_name


def _restore_gateway_file_deliveries(
    conn: sqlite3.Connection,
    temporary_name: str | None,
) -> None:
    """在新版审批父表已恢复后重建文件任务子表及其外键。"""
    if temporary_name is None:
        return
    _create_gateway_file_delivery_schema(conn)
    conn.execute(
        f"""
        INSERT INTO gateway_file_deliveries ({_GATEWAY_FILE_DELIVERY_COLUMNS})
        SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
        FROM {temporary_name}
        """
    )
    conn.execute(f"DROP TABLE {temporary_name}")


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    _create_gateway_approval_schema(conn)


def _migrate_v28_to_v29(conn: sqlite3.Connection) -> None:
    """为审批恢复保存授权范围和已开始执行标记，并扩展媒体工具。"""
    deliveries = _stash_gateway_file_deliveries(conn, "v28")
    conn.execute(
        "ALTER TABLE gateway_approval_requests "
        "RENAME TO gateway_approval_requests_legacy"
    )
    _create_gateway_approval_schema(conn)
    conn.execute(
        """
        INSERT INTO gateway_approval_requests (
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, result_content, source_event_json,
            agent_state_json, created_at, expires_at, updated_at,
            grant_scope, execution_started
        )
        SELECT
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, result_content, source_event_json,
            agent_state_json, created_at, expires_at, updated_at,
            NULL, 0
        FROM gateway_approval_requests_legacy
        """
    )
    _restore_gateway_file_deliveries(conn, deliveries)
    conn.execute("DROP TABLE gateway_approval_requests_legacy")
    _create_gateway_approval_schema(conn)
    _create_gateway_file_delivery_schema(conn)


def _migrate_v29_to_v30(conn: sqlite3.Connection) -> None:
    """扩展审批工具名约束，使既有 Gateway 数据库可恢复浏览器审批。"""
    deliveries = _stash_gateway_file_deliveries(conn, "v29")
    conn.execute(
        "ALTER TABLE gateway_approval_requests "
        "RENAME TO gateway_approval_requests_v29"
    )
    _create_gateway_approval_schema(conn)
    conn.execute(
        """
        INSERT INTO gateway_approval_requests (
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, grant_scope, result_content,
            source_event_json, agent_state_json, created_at, expires_at,
            updated_at, execution_started
        )
        SELECT
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, grant_scope, result_content,
            source_event_json, agent_state_json, created_at, expires_at,
            updated_at, execution_started
        FROM gateway_approval_requests_v29
        """
    )
    _restore_gateway_file_deliveries(conn, deliveries)
    conn.execute("DROP TABLE gateway_approval_requests_v29")
    _create_gateway_approval_schema(conn)
    _create_gateway_file_delivery_schema(conn)


def _migrate_v30_to_v31(conn: sqlite3.Connection) -> None:
    """移除审批工具名称枚举，保留现有审批记录及其关联状态。"""
    deliveries = _stash_gateway_file_deliveries(conn, "v30")
    conn.execute(
        "ALTER TABLE gateway_approval_requests "
        "RENAME TO gateway_approval_requests_v30"
    )
    _create_gateway_approval_schema(conn)
    conn.execute(
        """
        INSERT INTO gateway_approval_requests (
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, grant_scope, result_content,
            source_event_json, agent_state_json, created_at, expires_at,
            updated_at, execution_started
        )
        SELECT
            id, route_key, conversation_id, requester_user_id,
            source_message_id, tool_call_id, tool_message_id, tool_name,
            tool_args_json, summary, details_json, status,
            decision_message_id, grant_scope, result_content,
            source_event_json, agent_state_json, created_at, expires_at,
            updated_at, execution_started
        FROM gateway_approval_requests_v30
        """
    )
    _restore_gateway_file_deliveries(conn, deliveries)
    conn.execute("DROP TABLE gateway_approval_requests_v30")
    _create_gateway_approval_schema(conn)
    _create_gateway_file_delivery_schema(conn)
