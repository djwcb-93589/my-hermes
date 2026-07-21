from __future__ import annotations

import sqlite3

def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            reasoning_content TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session_order
            ON messages(session_id, id);

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
        );

        CREATE INDEX IF NOT EXISTS idx_model_call_events_session_order
            ON model_call_events(session_id, id);
    """)
