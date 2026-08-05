"""Observation 安全事件表的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.observation import create_schema


_OBSERVATION_INDEXES = (
    "idx_observations_created",
    "idx_observations_run",
    "idx_observations_parent_run",
    "idx_observations_event_type",
    "idx_observations_tool_name",
)

_OBSERVATION_COLUMNS = (
    "observation_id, event_type, run_id, parent_run_id, tool_call_id, "
    "tool_name, status, success, error_type, finish_reason, has_text, "
    "tool_call_count, prompt_tokens, completion_tokens, total_tokens, "
    "prompt_cache_hit_tokens, prompt_cache_miss_tokens, duration_ms, "
    "stop_reason, iterations, has_final_reply, created_at"
)


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
    """Rebuild observations with the v41 cache-token constraints."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(observations)").fetchall()
    }
    cache_hit = (
        "prompt_cache_hit_tokens"
        if "prompt_cache_hit_tokens" in columns
        else "NULL"
    )
    cache_miss = (
        "prompt_cache_miss_tokens"
        if "prompt_cache_miss_tokens" in columns
        else "NULL"
    )
    for index_name in _OBSERVATION_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    conn.execute("ALTER TABLE observations RENAME TO observations_v40")
    create_schema(conn)
    conn.execute(
        f"""
        INSERT INTO observations ({_OBSERVATION_COLUMNS})
        SELECT
            observation_id, event_type, run_id, parent_run_id, tool_call_id,
            tool_name, status, success, error_type, finish_reason, has_text,
            tool_call_count, prompt_tokens, completion_tokens, total_tokens,
            {cache_hit}, {cache_miss}, duration_ms, stop_reason, iterations,
            has_final_reply, created_at
        FROM observations_v40
        """
    )
    conn.execute("DROP TABLE observations_v40")
