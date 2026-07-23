"""Gateway 审批领域的历史迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.approval import _create_gateway_approval_schema


def _migrate_v13_to_v14(conn: sqlite3.Connection) -> None:
    _create_gateway_approval_schema(conn)


def _migrate_v28_to_v29(conn: sqlite3.Connection) -> None:
    """为审批恢复保存授权范围和已开始执行标记，并扩展媒体工具。"""
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
    conn.execute("DROP TABLE gateway_approval_requests_legacy")
    _create_gateway_approval_schema(conn)


def _migrate_v29_to_v30(conn: sqlite3.Connection) -> None:
    """扩展审批工具名约束，使既有 Gateway 数据库可恢复浏览器审批。"""
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
    conn.execute("DROP TABLE gateway_approval_requests_v29")
