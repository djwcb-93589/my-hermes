"""Background Review 状态领域的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.background_review import create_schema


def _migrate_v32_to_v33(conn: sqlite3.Connection) -> None:
    """为已有数据库创建 Background Review 状态表。"""
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


def _migrate_v33_to_v34(conn: sqlite3.Connection) -> None:
    """为记忆审视补充可重试的消息边界水位。"""
    additions = (
        "memory_message_total_upto INTEGER NOT NULL DEFAULT 0",
        "memory_reviewed_message_id INTEGER NOT NULL DEFAULT 0",
        "claim_memory_message_upto INTEGER",
        "retry_memory_upto INTEGER",
        "retry_memory_message_upto INTEGER",
    )
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(background_review_state)")
    }
    for definition in additions:
        column_name = definition.split(" ", 1)[0]
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE background_review_state ADD COLUMN {definition}"
            )
    # 旧 claim 没有可推导的消息边界，释放它以免把未知范围交给新协议。
    conn.execute(
        """
        UPDATE background_review_state
        SET claim_token=NULL, claim_memory_upto=NULL, claim_skill_upto=NULL,
            claim_started_at=NULL
        WHERE claim_memory_upto IS NOT NULL
        """
    )


def _migrate_v34_to_v35(conn: sqlite3.Connection) -> None:
    """将共享审视状态拆分为独立的 Memory 与 Skill 状态表。"""
    create_schema(conn)
    conn.execute(
        """
        INSERT INTO memory_review_state (
            session_id,
            turn_total,
            reviewed_turn_total,
            message_total_upto,
            reviewed_message_id,
            claim_token,
            claim_turn_upto,
            claim_message_upto,
            claim_started_at,
            retry_turn_upto,
            retry_message_upto,
            retry_after,
            last_attempt_at,
            last_success_at,
            last_error,
            updated_at
        )
        SELECT
            session_id,
            memory_turn_total,
            memory_reviewed_total,
            memory_message_total_upto,
            memory_reviewed_message_id,
            CASE
                WHEN claim_token IS NOT NULL
                 AND claim_memory_upto IS NOT NULL
                 AND claim_memory_message_upto IS NOT NULL
                THEN claim_token
            END,
            CASE
                WHEN claim_token IS NOT NULL
                 AND claim_memory_upto IS NOT NULL
                 AND claim_memory_message_upto IS NOT NULL
                THEN claim_memory_upto
            END,
            CASE
                WHEN claim_token IS NOT NULL
                 AND claim_memory_upto IS NOT NULL
                 AND claim_memory_message_upto IS NOT NULL
                THEN claim_memory_message_upto
            END,
            CASE
                WHEN claim_token IS NOT NULL
                 AND claim_memory_upto IS NOT NULL
                 AND claim_memory_message_upto IS NOT NULL
                THEN claim_started_at
            END,
            retry_memory_upto,
            retry_memory_message_upto,
            CASE
                WHEN retry_memory_upto IS NOT NULL THEN retry_after
            END,
            CASE
                WHEN (
                    claim_token IS NOT NULL
                    AND claim_memory_upto IS NOT NULL
                    AND claim_memory_message_upto IS NOT NULL
                ) OR retry_memory_upto IS NOT NULL
                THEN last_attempt_at
            END,
            NULL,
            CASE
                WHEN retry_memory_upto IS NOT NULL THEN last_error
            END,
            updated_at
        FROM background_review_state
        """
    )
    # 旧 Skill 进度没有独立消息窗口，不能可靠地继续领取或重试。
    conn.execute("DROP TABLE background_review_state")
