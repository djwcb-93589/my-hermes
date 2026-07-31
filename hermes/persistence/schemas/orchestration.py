"""持久化 Workflow、Task、Dependency 与 Run 的 SQLite 结构。"""

from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建编排领域四张事实表及其明确查询索引。"""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_workflows (
            workflow_id TEXT PRIMARY KEY CHECK (
                length(workflow_id) BETWEEN 4 AND 128
            ),
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
            goal TEXT NOT NULL CHECK (length(goal) BETWEEN 1 AND 100000),
            status TEXT NOT NULL CHECK (
                status IN ('active', 'completed', 'failed', 'cancelled')
            ),
            created_by_session TEXT CHECK (
                created_by_session IS NULL
                OR length(created_by_session) BETWEEN 1 AND 512
            ),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            updated_at REAL NOT NULL CHECK (updated_at >= created_at),
            finished_at REAL CHECK (
                finished_at IS NULL OR finished_at >= created_at
            ),
            CHECK (
                (status = 'active' AND finished_at IS NULL)
                OR
                (status IN ('completed', 'failed', 'cancelled')
                    AND finished_at IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_tasks (
            task_id TEXT PRIMARY KEY CHECK (
                length(task_id) BETWEEN 6 AND 128
            ),
            workflow_id TEXT NOT NULL,
            task_key TEXT NOT NULL CHECK (
                length(task_key) BETWEEN 1 AND 128
            ),
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
            prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 100000),
            role TEXT NOT NULL CHECK (length(role) BETWEEN 1 AND 128),
            status TEXT NOT NULL CHECK (
                status IN (
                    'todo', 'ready', 'running', 'blocked',
                    'completed', 'failed', 'cancelled'
                )
            ),
            priority INTEGER NOT NULL CHECK (
                priority BETWEEN -1000000 AND 1000000
            ),
            max_attempts INTEGER NOT NULL CHECK (
                max_attempts BETWEEN 1 AND 100
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
                attempt_count >= 0 AND attempt_count <= max_attempts
            ),
            workdir TEXT CHECK (
                workdir IS NULL OR length(workdir) BETWEEN 1 AND 4096
            ),
            input_metadata_json TEXT NOT NULL CHECK (
                length(input_metadata_json) <= 1000000
                AND json_valid(input_metadata_json)
                AND json_type(input_metadata_json) = 'object'
            ),
            claim_owner TEXT CHECK (
                claim_owner IS NULL OR length(claim_owner) BETWEEN 1 AND 256
            ),
            claim_token TEXT CHECK (
                claim_token IS NULL OR length(claim_token) BETWEEN 1 AND 256
            ),
            claim_expires_at REAL CHECK (
                claim_expires_at IS NULL OR claim_expires_at >= 0
            ),
            result_summary TEXT CHECK (
                result_summary IS NULL OR length(result_summary) <= 20000
            ),
            result_metadata_json TEXT CHECK (
                result_metadata_json IS NULL
                OR (
                    length(result_metadata_json) <= 1000000
                    AND json_valid(result_metadata_json)
                    AND json_type(result_metadata_json) = 'object'
                )
            ),
            error_type TEXT CHECK (
                error_type IS NULL OR length(error_type) BETWEEN 1 AND 256
            ),
            error_message TEXT CHECK (
                error_message IS NULL OR length(error_message) <= 4000
            ),
            blocked_reason TEXT CHECK (
                blocked_reason IS NULL
                OR length(blocked_reason) BETWEEN 1 AND 4000
            ),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            ready_at REAL CHECK (
                ready_at IS NULL OR ready_at >= created_at
            ),
            started_at REAL CHECK (
                started_at IS NULL OR started_at >= created_at
            ),
            finished_at REAL CHECK (
                finished_at IS NULL OR finished_at >= created_at
            ),
            updated_at REAL NOT NULL CHECK (updated_at >= created_at),
            FOREIGN KEY (workflow_id)
                REFERENCES orchestration_workflows(workflow_id)
                ON DELETE CASCADE,
            UNIQUE (workflow_id, task_key),
            UNIQUE (task_id, workflow_id),
            CHECK (
                (
                    status = 'running'
                    AND claim_owner IS NOT NULL
                    AND claim_token IS NOT NULL
                    AND claim_expires_at IS NOT NULL
                    AND claim_expires_at > updated_at
                )
                OR
                (
                    status != 'running'
                    AND claim_owner IS NULL
                    AND claim_token IS NULL
                    AND claim_expires_at IS NULL
                )
            ),
            CHECK (
                (status = 'blocked' AND blocked_reason IS NOT NULL)
                OR (status != 'blocked' AND blocked_reason IS NULL)
            ),
            CHECK (status != 'ready' OR ready_at IS NOT NULL),
            CHECK (
                (
                    status IN ('completed', 'failed', 'cancelled')
                    AND finished_at IS NOT NULL
                )
                OR
                (
                    status NOT IN ('completed', 'failed', 'cancelled')
                    AND finished_at IS NULL
                )
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_task_dependencies (
            workflow_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            depends_on_task_id TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on_task_id),
            CHECK (task_id != depends_on_task_id),
            FOREIGN KEY (task_id, workflow_id)
                REFERENCES orchestration_tasks(task_id, workflow_id)
                ON DELETE CASCADE,
            FOREIGN KEY (depends_on_task_id, workflow_id)
                REFERENCES orchestration_tasks(task_id, workflow_id)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orchestration_task_runs (
            run_id TEXT PRIMARY KEY CHECK (
                length(run_id) BETWEEN 5 AND 128
            ),
            workflow_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
            worker_id TEXT NOT NULL CHECK (
                length(worker_id) BETWEEN 1 AND 256
            ),
            claim_token TEXT NOT NULL UNIQUE CHECK (
                length(claim_token) BETWEEN 1 AND 256
            ),
            status TEXT NOT NULL CHECK (
                status IN (
                    'claimed', 'running', 'completed', 'failed',
                    'blocked', 'cancelled', 'abandoned'
                )
            ),
            session_key TEXT CHECK (
                session_key IS NULL OR length(session_key) BETWEEN 1 AND 512
            ),
            claimed_at REAL NOT NULL CHECK (claimed_at >= 0),
            started_at REAL CHECK (
                started_at IS NULL OR started_at >= claimed_at
            ),
            heartbeat_at REAL NOT NULL CHECK (heartbeat_at >= claimed_at),
            finished_at REAL CHECK (
                finished_at IS NULL OR finished_at >= claimed_at
            ),
            result_summary TEXT CHECK (
                result_summary IS NULL OR length(result_summary) <= 20000
            ),
            result_metadata_json TEXT CHECK (
                result_metadata_json IS NULL
                OR (
                    length(result_metadata_json) <= 1000000
                    AND json_valid(result_metadata_json)
                    AND json_type(result_metadata_json) = 'object'
                )
            ),
            error_type TEXT CHECK (
                error_type IS NULL OR length(error_type) BETWEEN 1 AND 256
            ),
            error_message TEXT CHECK (
                error_message IS NULL OR length(error_message) <= 4000
            ),
            FOREIGN KEY (task_id, workflow_id)
                REFERENCES orchestration_tasks(task_id, workflow_id)
                ON DELETE CASCADE,
            UNIQUE (task_id, attempt_number),
            CHECK (
                (
                    status IN (
                        'completed', 'failed', 'blocked',
                        'cancelled', 'abandoned'
                    )
                    AND finished_at IS NOT NULL
                )
                OR
                (
                    status IN ('claimed', 'running')
                    AND finished_at IS NULL
                )
            ),
            CHECK (status != 'running' OR started_at IS NOT NULL)
        )
        """
    )

    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_workflows_status
            ON orchestration_workflows(status, updated_at, workflow_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_tasks_ready
            ON orchestration_tasks(
                status, priority DESC, ready_at, created_at, task_id
            )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_tasks_workflow_status
            ON orchestration_tasks(workflow_id, status, task_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_tasks_claim_expiry
            ON orchestration_tasks(claim_expires_at, task_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_dependencies_parent
            ON orchestration_task_dependencies(
                depends_on_task_id, workflow_id, task_id
            )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_dependencies_workflow
            ON orchestration_task_dependencies(
                workflow_id, task_id, depends_on_task_id
            )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_runs_task_attempt
            ON orchestration_task_runs(task_id, attempt_number DESC, run_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_runs_status_heartbeat
            ON orchestration_task_runs(status, heartbeat_at, run_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_orchestration_runs_workflow_status
            ON orchestration_task_runs(workflow_id, status, run_id)
        """,
    ):
        conn.execute(statement)


__all__ = ["create_schema"]
