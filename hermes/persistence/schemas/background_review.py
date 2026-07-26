"""Background Review 状态领域的 SQLite 表结构。"""

from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建按会话保存审视进度、水位和领取状态的表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS background_review_state (
            session_id TEXT PRIMARY KEY,

            memory_turn_total INTEGER NOT NULL DEFAULT 0,
            memory_reviewed_total INTEGER NOT NULL DEFAULT 0,

            skill_tool_batch_total INTEGER NOT NULL DEFAULT 0,
            skill_reviewed_total INTEGER NOT NULL DEFAULT 0,

            claim_token TEXT,
            claim_memory_upto INTEGER,
            claim_skill_upto INTEGER,
            claim_started_at REAL,

            retry_after REAL,
            last_attempt_at REAL,
            last_success_at REAL,
            last_error TEXT,
            updated_at REAL NOT NULL,

            FOREIGN KEY(session_id)
                REFERENCES sessions(id)
                ON DELETE CASCADE,

            CHECK(memory_turn_total >= 0),
            CHECK(memory_reviewed_total >= 0),
            CHECK(memory_reviewed_total <= memory_turn_total),

            CHECK(skill_tool_batch_total >= 0),
            CHECK(skill_reviewed_total >= 0),
            CHECK(skill_reviewed_total <= skill_tool_batch_total),

            CHECK(
                claim_memory_upto IS NULL
                OR (
                    claim_memory_upto >= 0
                    AND claim_memory_upto <= memory_turn_total
                )
            ),

            CHECK(
                claim_skill_upto IS NULL
                OR (
                    claim_skill_upto >= 0
                    AND claim_skill_upto <= skill_tool_batch_total
                )
            ),

            CHECK(
                (
                    claim_token IS NULL
                    AND claim_memory_upto IS NULL
                    AND claim_skill_upto IS NULL
                    AND claim_started_at IS NULL
                )
                OR
                (
                    claim_token IS NOT NULL
                    AND claim_started_at IS NOT NULL
                    AND (
                        claim_memory_upto IS NOT NULL
                        OR claim_skill_upto IS NOT NULL
                    )
                )
            )
        )
        """
    )
