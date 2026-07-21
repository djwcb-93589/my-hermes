from __future__ import annotations

import sqlite3

from ..database import DBError, _count_rows, _table_exists

def _validate_v1_data(conn: sqlite3.Connection) -> None:
    """校验 v1 旧数据能否无损迁移到最新 schema。"""
    if not _table_exists(conn, "sessions") or not _table_exists(conn, "messages"):
        raise DBError("v1 db missing sessions/messages table")

    bad_sessions = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM sessions
        WHERE id IS NULL OR started_at IS NULL
        """,
    )
    if bad_sessions:
        raise DBError(
            f"cannot migrate v1 db: {bad_sessions} invalid session rows"
        )

    bad_messages = _count_rows(
        conn,
        """
        SELECT COUNT(*)
        FROM messages AS m
        LEFT JOIN sessions AS s ON s.id = m.session_id
        WHERE m.session_id IS NULL
           OR m.role IS NULL
           OR m.timestamp IS NULL
           OR s.id IS NULL
        """,
    )
    if bad_messages:
        raise DBError(
            f"cannot migrate v1 db: {bad_messages} invalid or orphan messages"
        )


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2:重建表,让旧库也获得 NOT NULL / 外键约束。"""
    _validate_v1_data(conn)

    backup_tables = ("sessions_v1_backup", "messages_v1_backup")
    if any(_table_exists(conn, table) for table in backup_tables):
        raise DBError("cannot migrate v1 db: leftover migration backup table")

    conn.execute("ALTER TABLE sessions RENAME TO sessions_v1_backup")
    conn.execute("ALTER TABLE messages RENAME TO messages_v1_backup")

    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        INSERT INTO sessions (id, source, started_at)
        SELECT id, source, started_at
        FROM sessions_v1_backup
        """
    )
    conn.execute(
        """
        INSERT INTO messages
            (id, session_id, role, content, tool_calls, tool_call_id, timestamp)
        SELECT id, session_id, role, content, tool_calls, tool_call_id, timestamp
        FROM messages_v1_backup
        """
    )

    conn.execute("DROP TABLE messages_v1_backup")
    conn.execute("DROP TABLE sessions_v1_backup")


def _migrate_v26_to_v27(conn: sqlite3.Connection) -> None:
    """v26 -> v27:持久化思考模型协议字段与脱敏调用诊断。"""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "reasoning_content" not in columns:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN reasoning_content TEXT"
        )
    # 旧记录已经丢失真实推理内容，只能为空；保留字段可满足兼容端协议。
    conn.execute(
        """
        UPDATE messages
        SET reasoning_content = ''
        WHERE role = 'assistant'
          AND tool_calls IS NOT NULL
          AND reasoning_content IS NULL
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS model_call_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            iteration INTEGER NOT NULL,
            model TEXT NOT NULL,
            model_role TEXT NOT NULL,
            outcome TEXT NOT NULL,
            finish_reason TEXT,
            latency_ms INTEGER NOT NULL,
            has_content INTEGER NOT NULL,
            content_chars INTEGER NOT NULL,
            has_reasoning INTEGER NOT NULL,
            reasoning_chars INTEGER NOT NULL,
            tool_call_count INTEGER NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            reasoning_tokens INTEGER,
            cached_tokens INTEGER,
            http_status INTEGER,
            error_category TEXT,
            exception_type TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_model_call_events_session_order
            ON model_call_events(session_id, id)
        """
    )

