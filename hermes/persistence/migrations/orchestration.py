"""持久化任务编排四张事实表的 schema migration。"""

from __future__ import annotations

import sqlite3

from ..database import DBError
from ..schemas.orchestration import create_schema


def _migrate_v38_to_v39(conn: sqlite3.Connection) -> None:
    """创建 Workflow、Task、Dependency 与 Run 表及索引。"""

    create_schema(conn)


def _migrate_v39_to_v40(conn: sqlite3.Connection) -> None:
    """按正式终结的 Run 历史重算 Task 已消耗的执行预算。"""

    consumed_statuses = ("completed", "failed", "abandoned")
    invalid = conn.execute(
        """
        WITH consumed_runs AS (
            SELECT task_id, COUNT(*) AS consumed_count
            FROM orchestration_task_runs
            WHERE status IN (?, ?, ?)
            GROUP BY task_id
        )
        SELECT 1
        FROM orchestration_tasks AS task
        LEFT JOIN consumed_runs
          ON consumed_runs.task_id=task.task_id
        WHERE COALESCE(consumed_runs.consumed_count, 0) < 0
           OR COALESCE(consumed_runs.consumed_count, 0) > task.max_attempts
        LIMIT 1
        """,
        consumed_statuses,
    ).fetchone()
    if invalid is not None:
        raise DBError(
            "cannot migrate orchestration attempt_count beyond max_attempts"
        )
    conn.execute(
        """
        UPDATE orchestration_tasks
        SET attempt_count=(
            SELECT COUNT(*)
            FROM orchestration_task_runs AS run
            WHERE run.task_id=orchestration_tasks.task_id
              AND run.status IN (?, ?, ?)
        )
        """,
        consumed_statuses,
    )


__all__ = ["_migrate_v38_to_v39", "_migrate_v39_to_v40"]
