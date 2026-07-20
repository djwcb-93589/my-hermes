from __future__ import annotations

import sqlite3

def _create_gateway_file_delivery_schema(conn: sqlite3.Connection) -> None:
    """创建带上传快照、Outbox 关联和 fencing claim 的文件任务表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_file_deliveries (
            id TEXT PRIMARY KEY,
            origin_kind TEXT NOT NULL DEFAULT 'gateway' CHECK (
                origin_kind IN ('gateway', 'cron')
            ),
            approval_id TEXT UNIQUE,
            cron_run_id TEXT,
            route_key TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            reply_to_message_id TEXT,
            thread_id TEXT,
            local_path TEXT NOT NULL,
            display_name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            sha256 TEXT NOT NULL,
            platform_file_key TEXT,
            outbox_id TEXT UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN (
                    'pending', 'uploading', 'uploaded', 'retry_wait',
                    'outbox_created', 'delivered', 'cancelled',
                    'permanent_failed'
                )
            ),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at REAL,
            last_error TEXT,
            last_error_code TEXT,
            claimed_by TEXT,
            claim_epoch INTEGER CHECK (claim_epoch IS NULL OR claim_epoch > 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (approval_id)
                REFERENCES gateway_approval_requests(id) ON DELETE RESTRICT,
            FOREIGN KEY (cron_run_id)
                REFERENCES cron_runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY (outbox_id)
                REFERENCES gateway_outbox(id) ON DELETE SET NULL,
            FOREIGN KEY (conversation_id)
                REFERENCES sessions(id) ON DELETE CASCADE,
            CHECK (
                (claimed_by IS NULL AND claim_epoch IS NULL)
                OR (claimed_by IS NOT NULL AND claim_epoch IS NOT NULL)
            ),
            CHECK (
                (origin_kind='gateway' AND approval_id IS NOT NULL
                 AND cron_run_id IS NULL)
                OR
                (origin_kind='cron' AND approval_id IS NULL
                 AND cron_run_id IS NOT NULL)
            )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_file_delivery_status_retry
        ON gateway_file_deliveries(status, next_attempt_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gateway_file_delivery_route
        ON gateway_file_deliveries(route_key, created_at, id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_file_delivery_identity
        ON gateway_file_deliveries(cron_run_id, local_path)
        WHERE origin_kind='cron'
        """
    )

