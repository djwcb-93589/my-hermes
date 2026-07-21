"""通用工具执行记录的 DDL 领域。"""

from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建独立于模型消息历史的工具执行恢复记录。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_id TEXT PRIMARY KEY,
            environment TEXT NOT NULL CHECK (length(environment) > 0),
            session_id TEXT,
            source_message_id TEXT,
            cron_run_id TEXT,
            tool_call_id TEXT NOT NULL CHECK (length(tool_call_id) > 0),
            tool_name TEXT NOT NULL CHECK (length(tool_name) > 0),
            arguments_json TEXT NOT NULL,
            arguments_fingerprint TEXT NOT NULL,
            recovery_policy TEXT NOT NULL CHECK (length(recovery_policy) > 0),
            status TEXT NOT NULL CHECK (
                status IN ('prepared', 'running', 'succeeded', 'failed', 'unknown')
            ),
            result_json TEXT,
            external_operation_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(environment, tool_call_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tool_executions_incomplete
            ON tool_executions(status, updated_at, execution_id)
            WHERE status IN ('prepared', 'running', 'unknown');

        CREATE INDEX IF NOT EXISTS idx_tool_executions_cron_run
            ON tool_executions(cron_run_id, created_at, execution_id)
            WHERE cron_run_id IS NOT NULL;
        """
    )
