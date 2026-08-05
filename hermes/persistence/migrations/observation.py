"""Observation 安全事件表的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..database import DBError, _table_columns, _table_exists
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
_OBSERVATION_REQUIRED_COLUMNS = frozenset({
    "observation_id",
    "event_type",
    "run_id",
    "parent_run_id",
    "tool_call_id",
    "tool_name",
    "status",
    "success",
    "error_type",
    "finish_reason",
    "has_text",
    "tool_call_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "duration_ms",
    "stop_reason",
    "iterations",
    "has_final_reply",
    "created_at",
})
_OBSERVATION_CACHE_COLUMNS = frozenset({
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
})
_OBSERVATION_ALLOWED_COLUMNS = (
    _OBSERVATION_REQUIRED_COLUMNS | _OBSERVATION_CACHE_COLUMNS
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
    _rebuild_observations(conn, temporary_name="observations_v40")


def _migrate_v41_to_v42(conn: sqlite3.Connection) -> None:
    """Rebuild v41 observations so every database has the v42 constraints."""
    _rebuild_observations(conn, temporary_name="observations_v41")


def _rebuild_observations(
    conn: sqlite3.Connection,
    *,
    temporary_name: str,
) -> None:
    """Rebuild observations inside the caller-owned migration transaction."""
    if not _table_exists(conn, "observations"):
        raise DBError("observations table is missing")
    if _table_exists(conn, temporary_name):
        raise DBError("observations migration temporary table already exists")
    columns = _table_columns(conn, "observations")
    missing = _OBSERVATION_REQUIRED_COLUMNS - columns
    unknown = columns - _OBSERVATION_ALLOWED_COLUMNS
    if missing or unknown:
        raise DBError("observations table structure is unsupported")
    foreign_keys = conn.execute(
        "PRAGMA foreign_key_list(observations)"
    ).fetchall()
    if foreign_keys:
        raise DBError("observations foreign-key structure is unsupported")
    index_names = {
        str(row[1])
        for row in conn.execute("PRAGMA index_list(observations)").fetchall()
        if not str(row[1]).startswith("sqlite_autoindex_")
    }
    unexpected_indexes = index_names - set(_OBSERVATION_INDEXES)
    if unexpected_indexes:
        raise DBError("observations index structure is unsupported")
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
    old_row_count = int(
        conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    )
    for index_name in _OBSERVATION_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index_name}")
    conn.execute(f"ALTER TABLE observations RENAME TO {temporary_name}")
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
        FROM {temporary_name}
        """
    )
    new_row_count = int(
        conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    )
    if new_row_count != old_row_count:
        raise DBError("observations row count changed during migration")
    rebuilt_indexes = {
        str(row[1])
        for row in conn.execute("PRAGMA index_list(observations)").fetchall()
    }
    if not set(_OBSERVATION_INDEXES).issubset(rebuilt_indexes):
        raise DBError("observations indexes were not rebuilt")
    conn.execute(f"DROP TABLE {temporary_name}")
