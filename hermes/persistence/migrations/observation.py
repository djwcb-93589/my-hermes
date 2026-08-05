"""Observation 安全事件表的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.observation import create_schema


def _migrate_v35_to_v36(conn: sqlite3.Connection) -> None:
    """创建 Observation 表并补充监控列表所需的固定排序索引。"""
    create_schema(conn)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tool_executions_monitoring_order
            ON tool_executions(updated_at DESC, execution_id DESC)
        """
    )


def _migrate_v40_to_v41(conn: sqlite3.Connection) -> None:
    """Add optional DeepSeek prompt-cache counts to observations."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(observations)").fetchall()
    }
    if "prompt_cache_hit_tokens" not in columns:
        conn.execute(
            "ALTER TABLE observations "
            "ADD COLUMN prompt_cache_hit_tokens INTEGER"
        )
    if "prompt_cache_miss_tokens" not in columns:
        conn.execute(
            "ALTER TABLE observations "
            "ADD COLUMN prompt_cache_miss_tokens INTEGER"
        )
