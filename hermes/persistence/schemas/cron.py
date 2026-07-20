from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建 Cron 任务定义、运行事实与旧数据导入标记。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cron_jobs (
            job_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            prompt TEXT NOT NULL,
            created_source TEXT NOT NULL,
            creator_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            schedule_type TEXT NOT NULL CHECK (
                schedule_type IN ('one_shot', 'interval', 'cron')
            ),
            schedule_expr TEXT NOT NULL,
            timezone TEXT NOT NULL,
            toolsets_json TEXT NOT NULL DEFAULT '[]',
            skills_json TEXT NOT NULL DEFAULT '[]',
            workdir TEXT,
            execution_timeout_seconds REAL NOT NULL CHECK (
                execution_timeout_seconds > 0
            ),
            max_agent_iterations INTEGER NOT NULL DEFAULT 20 CHECK (
                max_agent_iterations > 0
            ),
            overlap_policy TEXT NOT NULL CHECK (
                overlap_policy IN ('skip', 'queue', 'allow')
            ),
            misfire_policy TEXT NOT NULL CHECK (
                misfire_policy IN ('skip', 'run_once')
            ),
            misfire_catch_up INTEGER NOT NULL DEFAULT 0 CHECK (
                misfire_catch_up IN (0, 1)
            ),
            delivery_config_json TEXT NOT NULL DEFAULT '{}',
            retry_policy_json TEXT NOT NULL DEFAULT '{}',
            artifact_policy_json TEXT NOT NULL DEFAULT '{}',
            capability_spec_json TEXT NOT NULL DEFAULT '{}',
            capability_grant_json TEXT,
            approval_status TEXT NOT NULL CHECK (
                approval_status IN (
                    'not_required', 'pending', 'granted', 'denied',
                    'expired', 'revoked'
                )
            ),
            paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
            next_run_at REAL,
            last_run_at REAL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (
                consecutive_failures >= 0
            ),
            deleted_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_cron_jobs_due
            ON cron_jobs(paused, next_run_at, job_id);

        CREATE TABLE IF NOT EXISTS cron_capability_grants (
            grant_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            job_version INTEGER NOT NULL CHECK (job_version > 0),
            policy_version INTEGER NOT NULL CHECK (policy_version > 0),
            prompt_digest TEXT NOT NULL,
            capability_fingerprint TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            allowed_tool_names_json TEXT NOT NULL,
            creator_id TEXT NOT NULL,
            approval_id TEXT,
            status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
            audit_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            revoked_at REAL,
            revoked_reason TEXT,
            FOREIGN KEY (job_id) REFERENCES cron_jobs(job_id) ON DELETE RESTRICT
        );

        CREATE INDEX IF NOT EXISTS idx_cron_capability_grants_active
            ON cron_capability_grants(job_id, status, updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_capability_grants_one_active
            ON cron_capability_grants(job_id) WHERE status='active';

        CREATE TABLE IF NOT EXISTS cron_runs (
            run_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            scheduled_for REAL NOT NULL,
            claimed_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            execution_instance_id TEXT NOT NULL,
            claim_lease_name TEXT,
            claim_instance_id TEXT,
            claim_epoch INTEGER,
            status TEXT NOT NULL CHECK (
                status IN (
                    'claimed', 'running', 'completed', 'failed',
                    'blocked', 'cancelled'
                )
            ),
            error_type TEXT,
            result_summary TEXT,
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            delivery_status TEXT NOT NULL DEFAULT 'not_requested',
            delivery_ref_json TEXT,
            root_run_id TEXT,
            attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
            retry_due_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(job_id, scheduled_for),
            FOREIGN KEY (job_id) REFERENCES cron_jobs(job_id)
                ON DELETE RESTRICT,
            CHECK (
                (claim_lease_name IS NULL AND claim_instance_id IS NULL
                 AND claim_epoch IS NULL)
                OR
                (claim_lease_name IS NOT NULL AND claim_instance_id IS NOT NULL
                 AND claim_epoch IS NOT NULL AND claim_epoch > 0)
            )
        );

        CREATE INDEX IF NOT EXISTS idx_cron_runs_job_schedule
            ON cron_runs(job_id, scheduled_for DESC, run_id);
        CREATE INDEX IF NOT EXISTS idx_cron_runs_status_claimed
            ON cron_runs(status, claimed_at, run_id);
        CREATE INDEX IF NOT EXISTS idx_cron_runs_active_job
            ON cron_runs(job_id, status, scheduled_for);
        CREATE INDEX IF NOT EXISTS idx_cron_runs_delivery_prepare
            ON cron_runs(delivery_status, updated_at, run_id);
        CREATE INDEX IF NOT EXISTS idx_cron_runs_retry_due
            ON cron_runs(status, retry_due_at, run_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_runs_retry_attempt
            ON cron_runs(root_run_id, attempt_number)
            WHERE root_run_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS cron_legacy_imports (
            source_path TEXT PRIMARY KEY,
            source_sha256 TEXT NOT NULL,
            imported_count INTEGER NOT NULL CHECK (imported_count >= 0),
            imported_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cron_run_artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            local_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            sha256 TEXT NOT NULL,
            delivery_id TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'not_requested',
            preparation_error_type TEXT,
            preparation_retryable INTEGER NOT NULL DEFAULT 0 CHECK (
                preparation_retryable IN (0, 1)
            ),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (run_id) REFERENCES cron_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY (delivery_id) REFERENCES gateway_file_deliveries(id)
                ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_cron_run_artifacts_run
            ON cron_run_artifacts(run_id, created_at, artifact_id);
        """
    )


# 向后兼容:migration 仍通过私有名引用同一份 DDL。
_create_cron_schema = create_schema
