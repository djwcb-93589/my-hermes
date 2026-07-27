"""Background Review 独立状态表的 SQLite 结构。"""

from __future__ import annotations

import sqlite3


def create_schema(conn: sqlite3.Connection) -> None:
    """创建 Memory 与 Skill 各自独立的审视状态表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_review_state (
            session_id TEXT PRIMARY KEY,

            turn_total INTEGER NOT NULL DEFAULT 0,
            reviewed_turn_total INTEGER NOT NULL DEFAULT 0,
            message_total_upto INTEGER NOT NULL DEFAULT 0,
            reviewed_message_id INTEGER NOT NULL DEFAULT 0,

            claim_token TEXT,
            claim_turn_upto INTEGER,
            claim_message_upto INTEGER,
            claim_started_at REAL,

            retry_turn_upto INTEGER,
            retry_message_upto INTEGER,
            retry_after REAL,

            last_attempt_at REAL,
            last_success_at REAL,
            last_error TEXT,
            updated_at REAL NOT NULL,

            FOREIGN KEY(session_id)
                REFERENCES sessions(id)
                ON DELETE CASCADE,

            CHECK(turn_total >= 0),
            CHECK(reviewed_turn_total >= 0),
            CHECK(reviewed_turn_total <= turn_total),
            CHECK(message_total_upto >= 0),
            CHECK(reviewed_message_id >= 0),
            CHECK(reviewed_message_id <= message_total_upto),

            CHECK(
                claim_turn_upto IS NULL
                OR (claim_turn_upto >= 0 AND claim_turn_upto <= turn_total)
            ),
            CHECK(
                claim_message_upto IS NULL
                OR (
                    claim_message_upto >= 0
                    AND claim_message_upto <= message_total_upto
                )
            ),
            CHECK((claim_turn_upto IS NULL) = (claim_message_upto IS NULL)),

            CHECK(
                retry_turn_upto IS NULL
                OR (retry_turn_upto >= 0 AND retry_turn_upto <= turn_total)
            ),
            CHECK(
                retry_message_upto IS NULL
                OR (
                    retry_message_upto >= 0
                    AND retry_message_upto <= message_total_upto
                )
            ),
            CHECK((retry_turn_upto IS NULL) = (retry_message_upto IS NULL)),

            CHECK(
                (
                    claim_token IS NULL
                    AND claim_turn_upto IS NULL
                    AND claim_message_upto IS NULL
                    AND claim_started_at IS NULL
                )
                OR
                (
                    claim_token IS NOT NULL
                    AND claim_turn_upto IS NOT NULL
                    AND claim_message_upto IS NOT NULL
                    AND claim_started_at IS NOT NULL
                )
            )
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_review_state (
            session_id TEXT PRIMARY KEY,

            tool_batch_total INTEGER NOT NULL DEFAULT 0,
            reviewed_tool_batch_total INTEGER NOT NULL DEFAULT 0,
            message_total_upto INTEGER NOT NULL DEFAULT 0,
            reviewed_message_id INTEGER NOT NULL DEFAULT 0,

            claim_token TEXT,
            claim_tool_batch_upto INTEGER,
            claim_message_upto INTEGER,
            claim_started_at REAL,

            retry_tool_batch_upto INTEGER,
            retry_message_upto INTEGER,
            retry_after REAL,

            last_attempt_at REAL,
            last_success_at REAL,
            last_error TEXT,
            updated_at REAL NOT NULL,

            FOREIGN KEY(session_id)
                REFERENCES sessions(id)
                ON DELETE CASCADE,

            CHECK(tool_batch_total >= 0),
            CHECK(reviewed_tool_batch_total >= 0),
            CHECK(reviewed_tool_batch_total <= tool_batch_total),
            CHECK(message_total_upto >= 0),
            CHECK(reviewed_message_id >= 0),
            CHECK(reviewed_message_id <= message_total_upto),

            CHECK(
                claim_tool_batch_upto IS NULL
                OR (
                    claim_tool_batch_upto >= 0
                    AND claim_tool_batch_upto <= tool_batch_total
                )
            ),
            CHECK(
                claim_message_upto IS NULL
                OR (
                    claim_message_upto >= 0
                    AND claim_message_upto <= message_total_upto
                )
            ),
            CHECK(
                (claim_tool_batch_upto IS NULL) = (claim_message_upto IS NULL)
            ),

            CHECK(
                retry_tool_batch_upto IS NULL
                OR (
                    retry_tool_batch_upto >= 0
                    AND retry_tool_batch_upto <= tool_batch_total
                )
            ),
            CHECK(
                retry_message_upto IS NULL
                OR (
                    retry_message_upto >= 0
                    AND retry_message_upto <= message_total_upto
                )
            ),
            CHECK(
                (retry_tool_batch_upto IS NULL) = (retry_message_upto IS NULL)
            ),

            CHECK(
                (
                    claim_token IS NULL
                    AND claim_tool_batch_upto IS NULL
                    AND claim_message_upto IS NULL
                    AND claim_started_at IS NULL
                )
                OR
                (
                    claim_token IS NOT NULL
                    AND claim_tool_batch_upto IS NOT NULL
                    AND claim_message_upto IS NOT NULL
                    AND claim_started_at IS NOT NULL
                )
            )
        )
        """
    )
