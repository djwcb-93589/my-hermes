"""Observation 安全事件的 SQLite 结构。"""

from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建只保存中立 Observation 契约字段的单一事件表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS observations (
            observation_id TEXT PRIMARY KEY
                CHECK (length(observation_id) > 0),
            event_type TEXT NOT NULL CHECK (
                event_type IN ('tool_call', 'model_call', 'run_end')
            ),
            run_id TEXT NOT NULL CHECK (length(run_id) > 0),
            parent_run_id TEXT CHECK (
                parent_run_id IS NULL OR length(parent_run_id) > 0
            ),

            tool_call_id TEXT,
            tool_name TEXT,
            status TEXT,
            success INTEGER CHECK (success IS NULL OR success IN (0, 1)),
            error_type TEXT,

            finish_reason TEXT,
            has_text INTEGER CHECK (has_text IS NULL OR has_text IN (0, 1)),
            tool_call_count INTEGER CHECK (
                tool_call_count IS NULL OR tool_call_count >= 0
            ),
            prompt_tokens INTEGER CHECK (
                prompt_tokens IS NULL OR prompt_tokens >= 0
            ),
            completion_tokens INTEGER CHECK (
                completion_tokens IS NULL OR completion_tokens >= 0
            ),
            total_tokens INTEGER CHECK (
                total_tokens IS NULL OR total_tokens >= 0
            ),
            duration_ms INTEGER CHECK (
                duration_ms IS NULL OR duration_ms >= 0
            ),

            stop_reason TEXT,
            iterations INTEGER CHECK (
                iterations IS NULL OR iterations >= 0
            ),
            has_final_reply INTEGER CHECK (
                has_final_reply IS NULL OR has_final_reply IN (0, 1)
            ),
            created_at REAL NOT NULL CHECK (created_at >= 0),

            CHECK (
                (
                    event_type = 'tool_call'
                    AND tool_call_id IS NOT NULL
                    AND length(tool_call_id) > 0
                    AND tool_name IS NOT NULL
                    AND length(tool_name) > 0
                    AND status IS NOT NULL
                    AND length(status) > 0
                    AND success IS NOT NULL
                    AND duration_ms IS NOT NULL
                    AND finish_reason IS NULL
                    AND has_text IS NULL
                    AND tool_call_count IS NULL
                    AND prompt_tokens IS NULL
                    AND completion_tokens IS NULL
                    AND total_tokens IS NULL
                    AND stop_reason IS NULL
                    AND iterations IS NULL
                    AND has_final_reply IS NULL
                )
                OR
                (
                    event_type = 'model_call'
                    AND tool_call_id IS NULL
                    AND tool_name IS NULL
                    AND status IS NULL
                    AND success IS NULL
                    AND error_type IS NULL
                    AND has_text IS NOT NULL
                    AND tool_call_count IS NOT NULL
                    AND duration_ms IS NOT NULL
                    AND stop_reason IS NULL
                    AND iterations IS NULL
                    AND has_final_reply IS NULL
                )
                OR
                (
                    event_type = 'run_end'
                    AND tool_call_id IS NULL
                    AND tool_name IS NULL
                    AND status IS NOT NULL
                    AND length(status) > 0
                    AND success IS NULL
                    AND error_type IS NULL
                    AND finish_reason IS NULL
                    AND has_text IS NULL
                    AND tool_call_count IS NOT NULL
                    AND prompt_tokens IS NULL
                    AND completion_tokens IS NULL
                    AND total_tokens IS NULL
                    AND duration_ms IS NULL
                    AND stop_reason IS NOT NULL
                    AND length(stop_reason) > 0
                    AND iterations IS NOT NULL
                    AND has_final_reply IS NOT NULL
                )
            )
        )
        """
    )
    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_observations_created
            ON observations(created_at DESC, observation_id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_observations_run
            ON observations(run_id, created_at, observation_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_observations_parent_run
            ON observations(parent_run_id, created_at, observation_id)
            WHERE parent_run_id IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_observations_event_type
            ON observations(event_type, created_at DESC, observation_id DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_observations_tool_name
            ON observations(tool_name, created_at DESC, observation_id DESC)
            WHERE event_type = 'tool_call'
        """,
    ):
        conn.execute(statement)
