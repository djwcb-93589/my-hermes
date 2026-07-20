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

