"""通用工具执行记录领域的历史迁移。"""

from __future__ import annotations

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
