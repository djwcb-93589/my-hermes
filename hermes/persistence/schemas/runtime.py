"""Runtime Component 当前快照的 SQLite 结构。"""

from __future__ import annotations

import sqlite3

from hermes.observability.runtime import MAX_RUNTIME_HEARTBEAT_SECONDS


def create_schema(conn: sqlite3.Connection) -> None:
    """创建每个逻辑组件只保留一个当前有效实例的状态表。"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS runtime_component_snapshots (
            component_type TEXT NOT NULL
                CHECK (
                    length(trim(component_type)) BETWEEN 1 AND 128
                ),
            component_id TEXT NOT NULL
                CHECK (
                    length(trim(component_id)) BETWEEN 1 AND 128
                ),
            instance_id TEXT NOT NULL
                CHECK (
                    length(trim(instance_id)) BETWEEN 1 AND 128
                ),
            reported_state TEXT NOT NULL CHECK (
                reported_state IN (
                    'starting', 'running', 'idle',
                    'stopping', 'stopped', 'failed'
                )
            ),
            started_at REAL CHECK (
                started_at IS NULL OR started_at >= 0
            ),
            heartbeat_at REAL NOT NULL CHECK (heartbeat_at >= 0),
            stopped_at REAL CHECK (
                stopped_at IS NULL OR stopped_at >= 0
            ),
            error_type TEXT CHECK (
                error_type IS NULL
                OR (
                    length(error_type) BETWEEN 1 AND 128
                    AND reported_state = 'failed'
                )
            ),
            heartbeat_interval_seconds REAL NOT NULL CHECK (
                heartbeat_interval_seconds > 0
                AND heartbeat_interval_seconds <=
                    {MAX_RUNTIME_HEARTBEAT_SECONDS}
            ),
            stale_after_seconds REAL NOT NULL CHECK (
                stale_after_seconds > heartbeat_interval_seconds
                AND stale_after_seconds <= {MAX_RUNTIME_HEARTBEAT_SECONDS}
            ),
            metadata_json TEXT NOT NULL CHECK (
                json_valid(metadata_json)
                AND json_type(metadata_json) = 'object'
            ),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),

            PRIMARY KEY (component_type, component_id),
            CHECK (
                started_at IS NULL OR started_at <= heartbeat_at
            ),
            CHECK (
                (
                    reported_state = 'stopped'
                    AND stopped_at IS NOT NULL
                )
                OR
                (
                    reported_state != 'stopped'
                    AND stopped_at IS NULL
                )
            ),
            CHECK (
                stopped_at IS NULL
                OR (
                    (started_at IS NULL OR stopped_at >= started_at)
                    AND stopped_at <= heartbeat_at
                )
            )
        )
        """
    )
    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_snapshots_component_type
            ON runtime_component_snapshots(component_type)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_snapshots_reported_state
            ON runtime_component_snapshots(reported_state)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_snapshots_heartbeat
            ON runtime_component_snapshots(heartbeat_at)
        """,
    ):
        conn.execute(statement)


__all__ = ["create_schema"]
