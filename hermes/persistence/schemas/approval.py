from __future__ import annotations

import sqlite3

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
            tool_name TEXT NOT NULL CHECK (
                tool_name IN ('file', 'terminal', 'gateway_send_file', 'cron')
            ),
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

