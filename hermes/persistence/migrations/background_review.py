"""Background Review 状态领域的 schema 迁移。"""

from __future__ import annotations

import sqlite3

from ..schemas.background_review import create_schema


def _migrate_v32_to_v33(conn: sqlite3.Connection) -> None:
    """为已有数据库创建 Background Review 状态表。"""
    create_schema(conn)


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
