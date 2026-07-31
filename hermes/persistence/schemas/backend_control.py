"""Gateway Backend Control 请求与进程绑定的 SQLite 结构。"""

from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建有限动作请求和单 Gateway 进程绑定。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backend_control_requests (
            request_id TEXT PRIMARY KEY CHECK (length(request_id) = 36),
            backend_type TEXT NOT NULL CHECK (backend_type = 'gateway'),
            action TEXT NOT NULL CHECK (action IN ('start', 'stop', 'restart')),
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'claimed', 'executing',
                    'succeeded', 'failed', 'rejected'
                )
            ),
            actor_security_id TEXT NOT NULL CHECK (
                length(actor_security_id) = 64
                AND actor_security_id NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key_digest TEXT NOT NULL CHECK (
                length(idempotency_key_digest) = 64
                AND idempotency_key_digest NOT GLOB '*[^0-9a-f]*'
            ),
            request_fingerprint TEXT NOT NULL CHECK (
                length(request_fingerprint) = 64
                AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            claimed_by_supervisor TEXT CHECK (
                claimed_by_supervisor IS NULL
                OR length(claimed_by_supervisor) = 36
            ),
            claimed_at REAL CHECK (claimed_at IS NULL OR claimed_at >= 0),
            created_at REAL NOT NULL CHECK (created_at >= 0),
            started_at REAL CHECK (started_at IS NULL OR started_at >= 0),
            completed_at REAL CHECK (completed_at IS NULL OR completed_at >= 0),
            execution_stage TEXT CHECK (
                execution_stage IS NULL
                OR execution_stage IN (
                    'starting', 'stopping', 'stopping_old', 'starting_new'
                )
            ),
            result_code TEXT CHECK (
                result_code IS NULL
                OR (
                    length(result_code) BETWEEN 1 AND 64
                    AND result_code NOT GLOB '*[^a-z0-9_]*'
                )
            ),
            result_reference TEXT CHECK (
                result_reference IS NULL OR length(result_reference) = 36
            ),
            exception_type TEXT CHECK (
                exception_type IS NULL
                OR (
                    length(exception_type) BETWEEN 1 AND 128
                    AND exception_type NOT GLOB '*[^A-Za-z0-9_.]*'
                )
            ),
            forced_termination INTEGER NOT NULL DEFAULT 0
                CHECK (forced_termination IN (0, 1)),

            UNIQUE (actor_security_id, idempotency_key_digest),
            CHECK (
                (status = 'pending' AND claimed_by_supervisor IS NULL
                    AND claimed_at IS NULL AND started_at IS NULL
                    AND completed_at IS NULL AND result_code IS NULL)
                OR
                (status = 'claimed' AND claimed_by_supervisor IS NOT NULL
                    AND claimed_at IS NOT NULL AND completed_at IS NULL
                    AND result_code IS NULL)
                OR
                (status = 'executing' AND claimed_by_supervisor IS NOT NULL
                    AND claimed_at IS NOT NULL AND started_at IS NOT NULL
                    AND completed_at IS NULL AND result_code IS NULL)
                OR
                (status IN ('succeeded', 'failed', 'rejected')
                    AND claimed_by_supervisor IS NOT NULL
                    AND claimed_at IS NOT NULL AND started_at IS NOT NULL
                    AND completed_at IS NOT NULL AND result_code IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS backend_process_bindings (
            backend_type TEXT PRIMARY KEY CHECK (backend_type = 'gateway'),
            supervisor_instance_id TEXT CHECK (
                supervisor_instance_id IS NULL
                OR length(supervisor_instance_id) = 36
            ),
            launch_id TEXT CHECK (launch_id IS NULL OR length(launch_id) = 36),
            pid INTEGER CHECK (pid IS NULL OR pid > 0),
            process_identity_token TEXT CHECK (
                process_identity_token IS NULL
                OR length(process_identity_token) BETWEEN 1 AND 256
            ),
            identity_verified INTEGER CHECK (
                identity_verified IS NULL OR identity_verified IN (0, 1)
            ),
            observed_state TEXT NOT NULL CHECK (
                observed_state IN (
                    'stopped', 'starting', 'running',
                    'stopping', 'exited', 'unknown'
                )
            ),
            started_at REAL CHECK (started_at IS NULL OR started_at >= 0),
            config_revision_at_launch TEXT CHECK (
                config_revision_at_launch IS NULL
                OR length(config_revision_at_launch) = 71
            ),
            last_exit_at REAL CHECK (last_exit_at IS NULL OR last_exit_at >= 0),
            last_exit_code INTEGER,
            last_request_id TEXT REFERENCES backend_control_requests(request_id),
            updated_at REAL NOT NULL CHECK (updated_at >= 0),

            CHECK (
                (
                    pid IS NULL AND supervisor_instance_id IS NULL
                    AND launch_id IS NULL AND process_identity_token IS NULL
                    AND identity_verified IS NULL AND started_at IS NULL
                    AND config_revision_at_launch IS NULL
                )
                OR
                (
                    pid IS NOT NULL AND supervisor_instance_id IS NOT NULL
                    AND launch_id IS NOT NULL AND process_identity_token IS NOT NULL
                    AND identity_verified IS NOT NULL AND started_at IS NOT NULL
                    AND config_revision_at_launch IS NOT NULL
                )
            )
        )
        """
    )
    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_backend_control_status_created
            ON backend_control_requests(status, created_at, request_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_backend_control_type_created
            ON backend_control_requests(backend_type, created_at, request_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_backend_process_bindings_backend
            ON backend_process_bindings(backend_type)
        """,
    ):
        conn.execute(statement)


__all__ = ["create_schema"]
