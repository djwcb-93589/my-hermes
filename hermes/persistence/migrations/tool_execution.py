"""通用工具执行记录领域的历史迁移。"""

from __future__ import annotations

import json
import sqlite3

from ..schemas.tool_execution import create_schema


def _migrate_v25_to_v26(conn: sqlite3.Connection) -> None:
    """为工具执行恢复 Journal 创建独立表和查询索引。"""
    create_schema(conn)


def _migrate_v27_to_v28(conn: sqlite3.Connection) -> None:
    """为 Gateway Journal 恢复补充 runtime lease 所有权。"""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(tool_executions)").fetchall()
    }
    for name, definition in (
        ("gateway_lease_name", "TEXT"),
        ("gateway_instance_id", "TEXT"),
        ("gateway_lease_epoch", "INTEGER"),
    ):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE tool_executions ADD COLUMN {name} {definition}"
            )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_executions_gateway_lease
            ON tool_executions(
                gateway_lease_name, gateway_instance_id, gateway_lease_epoch,
                status, updated_at, execution_id
            )
            WHERE gateway_lease_name IS NOT NULL
        """
    )


def _migrate_v31_to_v32(conn: sqlite3.Connection) -> None:
    """为等待人工审批的 Journal 记录增加可恢复但不自动重试的状态。"""
    conn.execute("DROP INDEX IF EXISTS idx_tool_executions_incomplete")
    conn.execute("DROP INDEX IF EXISTS idx_tool_executions_cron_run")
    conn.execute("DROP INDEX IF EXISTS idx_tool_executions_gateway_lease")
    conn.execute(
        "ALTER TABLE tool_executions RENAME TO tool_executions_v31"
    )
    create_schema(conn)
    conn.execute(
        """
        INSERT INTO tool_executions (
            execution_id, environment, session_id, source_message_id,
            cron_run_id, gateway_lease_name, gateway_instance_id,
            gateway_lease_epoch, tool_call_id, tool_name, arguments_json,
            arguments_fingerprint, recovery_policy, status, result_json,
            external_operation_id, attempt_count, created_at, updated_at
        )
        SELECT
            execution_id, environment, session_id, source_message_id,
            cron_run_id, gateway_lease_name, gateway_instance_id,
            gateway_lease_epoch, tool_call_id, tool_name, arguments_json,
            arguments_fingerprint, recovery_policy, status, result_json,
            external_operation_id, attempt_count, created_at, updated_at
        FROM tool_executions_v31
        """
    )
    rows = conn.execute(
        """
        SELECT execution_id, result_json
        FROM tool_executions_v31
        WHERE status='failed'
        """
    ).fetchall()
    deferred_ids: list[tuple[str]] = []
    for execution_id, result_json in rows:
        try:
            result = json.loads(str(result_json))
            output = json.loads(str(result.get("output", "")))
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            isinstance(output, dict)
            and output.get("status") == "awaiting_approval"
            and output.get("approval_required") is True
        ):
            deferred_ids.append((str(execution_id),))
    if deferred_ids:
        conn.executemany(
            """
            UPDATE tool_executions
            SET status='awaiting_approval'
            WHERE execution_id=? AND status='failed'
            """,
            deferred_ids,
        )
    conn.execute("DROP TABLE tool_executions_v31")
