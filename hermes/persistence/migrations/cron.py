from __future__ import annotations

import sqlite3

from ..database import _table_columns
from ..schemas.cron import _create_cron_schema

def _migrate_v18_to_v19(conn: sqlite3.Connection) -> None:
    """建立 Cron 任务定义与运行记录的正式持久化 schema。"""
    _create_cron_schema(conn)


def _migrate_v19_to_v20(conn: sqlite3.Connection) -> None:
    """为每个 Cron 任务增加独立 AgentLoop 的轮数上限。"""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(cron_jobs)").fetchall()
    }
    if "max_agent_iterations" not in columns:
        conn.execute(
            """
            ALTER TABLE cron_jobs
            ADD COLUMN max_agent_iterations INTEGER NOT NULL DEFAULT 20
            CHECK (max_agent_iterations > 0)
            """
        )


def _migrate_v20_to_v21(conn: sqlite3.Connection) -> None:
    """为 Gateway Cron 调度补充错过补跑标记和 lease fencing claim。"""
    job_columns = _table_columns(conn, "cron_jobs")
    if "misfire_catch_up" not in job_columns:
        conn.execute(
            "ALTER TABLE cron_jobs ADD COLUMN misfire_catch_up "
            "INTEGER NOT NULL DEFAULT 0 CHECK (misfire_catch_up IN (0, 1))"
        )
    run_columns = _table_columns(conn, "cron_runs")
    if "claim_lease_name" not in run_columns:
        conn.execute("ALTER TABLE cron_runs ADD COLUMN claim_lease_name TEXT")
    if "claim_instance_id" not in run_columns:
        conn.execute("ALTER TABLE cron_runs ADD COLUMN claim_instance_id TEXT")
    if "claim_epoch" not in run_columns:
        conn.execute("ALTER TABLE cron_runs ADD COLUMN claim_epoch INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_runs_active_job "
        "ON cron_runs(job_id, status, scheduled_for)"
    )


def _migrate_v24_to_v25(conn: sqlite3.Connection) -> None:
    """为可恢复投递准备和跨运行重试补充最小运行事实字段。"""
    columns = _table_columns(conn, "cron_runs")
    additions = (
        ("root_run_id", "TEXT"),
        ("attempt_number", "INTEGER NOT NULL DEFAULT 1"),
        ("retry_due_at", "REAL"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE cron_runs ADD COLUMN {name} {definition}")
    artifact_columns = _table_columns(conn, "cron_run_artifacts")
    for name, definition in (
        ("preparation_error_type", "TEXT"),
        ("preparation_retryable", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in artifact_columns:
            conn.execute(
                f"ALTER TABLE cron_run_artifacts ADD COLUMN {name} {definition}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_runs_delivery_prepare "
        "ON cron_runs(delivery_status, updated_at, run_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_runs_retry_due "
        "ON cron_runs(status, retry_due_at, run_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_runs_retry_attempt "
        "ON cron_runs(root_run_id, attempt_number) WHERE root_run_id IS NOT NULL"
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE cron_jobs ADD COLUMN {name} {definition}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cron_jobs_visible_due "
        "ON cron_jobs(deleted_at, paused, next_run_at, job_id)"
    )

