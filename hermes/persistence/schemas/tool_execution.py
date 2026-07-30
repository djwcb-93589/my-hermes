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
            gateway_lease_name TEXT,
            gateway_instance_id TEXT,
            gateway_lease_epoch INTEGER CHECK (
                gateway_lease_epoch IS NULL OR gateway_lease_epoch > 0
            ),
            tool_call_id TEXT NOT NULL CHECK (length(tool_call_id) > 0),
            tool_name TEXT NOT NULL CHECK (length(tool_name) > 0),
            arguments_json TEXT NOT NULL,
            arguments_fingerprint TEXT NOT NULL,
            recovery_policy TEXT NOT NULL CHECK (
                recovery_policy IN ('retry_safe', 'unknown_on_crash', 'status_check')
            ),
            status TEXT NOT NULL CHECK (
                status IN (
                    'prepared', 'awaiting_approval', 'running',
                    'succeeded', 'failed', 'unknown'
                )
            ),
            result_json TEXT,
            external_operation_id TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK (
                (gateway_lease_name IS NULL AND gateway_instance_id IS NULL
                 AND gateway_lease_epoch IS NULL)
                OR
                (gateway_lease_name IS NOT NULL AND gateway_instance_id IS NOT NULL
                 AND gateway_lease_epoch IS NOT NULL)
            ),
            UNIQUE(environment, tool_call_id)
        );

        CREATE INDEX IF NOT EXISTS idx_tool_executions_incomplete
            ON tool_executions(status, updated_at, execution_id)
            WHERE status IN ('prepared', 'running', 'unknown');

        CREATE INDEX IF NOT EXISTS idx_tool_executions_cron_run
            ON tool_executions(cron_run_id, created_at, execution_id)
            WHERE cron_run_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_tool_executions_gateway_lease
            ON tool_executions(
                gateway_lease_name, gateway_instance_id, gateway_lease_epoch,
                status, updated_at, execution_id
            )
            WHERE gateway_lease_name IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_tool_executions_monitoring_order
            ON tool_executions(updated_at DESC, execution_id DESC);
        """
    )
