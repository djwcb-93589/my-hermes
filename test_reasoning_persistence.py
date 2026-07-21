from __future__ import annotations

import json
import os
import sqlite3

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9")

from hermes.persistence.core import (  # noqa: E402
    add_message,
    add_model_call_event,
    create_session,
    get_gateway_visible_session_messages,
    get_session_messages,
    list_model_call_events,
)
from hermes.persistence.schema import init_db  # noqa: E402
from hermes.tokens import estimate_tokens  # noqa: E402


def _tool_calls() -> list[dict]:
    return [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "cron", "arguments": "{}"},
    }]


def test_v26_migration_backfills_tool_call_reasoning_content(tmp_path):
    db_path = tmp_path / "v26.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version(version) VALUES (26);
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        INSERT INTO sessions(id, source, started_at)
        VALUES ('session-1', 'gateway', 1.0);
        """
    )
    conn.execute(
        """
        INSERT INTO messages(
            session_id, role, content, tool_calls, tool_call_id, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("session-1", "assistant", "", json.dumps(_tool_calls()), None, 2.0),
    )
    conn.commit()
    conn.close()

    migrated = init_db(str(db_path))
    try:
        version = migrated.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        reasoning = migrated.execute(
            "SELECT reasoning_content FROM messages"
        ).fetchone()[0]
        assert version == 27
        assert reasoning == ""
        assert get_session_messages(migrated, "session-1")[0][
            "reasoning_content"
        ] == ""
    finally:
        migrated.close()


def test_reasoning_content_round_trip_and_tool_call_compatibility(tmp_path):
    conn = init_db(str(tmp_path / "messages.db"))
    try:
        session_id = create_session(conn)
        add_message(conn, session_id, {
            "role": "assistant",
            "content": "",
            "tool_calls": _tool_calls(),
            "reasoning_content": "private protocol state",
        })
        add_message(conn, session_id, {
            "role": "assistant",
            "content": "",
            "tool_calls": _tool_calls(),
        })

        messages = get_session_messages(conn, session_id)
        gateway_messages = get_gateway_visible_session_messages(
            conn,
            session_id,
        )
        assert [msg["reasoning_content"] for msg in messages] == [
            "private protocol state",
            "",
        ]
        assert gateway_messages == messages
    finally:
        conn.close()


def test_model_call_events_only_persist_allowlisted_diagnostics(tmp_path):
    conn = init_db(str(tmp_path / "events.db"))
    try:
        session_id = create_session(conn)
        add_model_call_event(conn, session_id, {
            "iteration": 1,
            "model": "fallback-model",
            "model_role": "fallback",
            "outcome": "empty_model_response",
            "finish_reason": "length",
            "latency_ms": 120,
            "has_content": 0,
            "content_chars": 0,
            "has_reasoning": 1,
            "reasoning_chars": 80,
            "tool_call_count": 0,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "reasoning_tokens": 20,
            "cached_tokens": 0,
            "http_status": 200,
            "error_category": "output_length_exhausted",
            "exception_type": None,
            "prompt": "must not be stored",
            "content": "must not be stored",
            "reasoning_content": "must not be stored",
        })

        event = list_model_call_events(conn, session_id)[0]
        assert event["outcome"] == "empty_model_response"
        assert event["reasoning_chars"] == 80
        assert "prompt" not in event
        assert "content" not in event
        assert "reasoning_content" not in event
    finally:
        conn.close()


def test_token_estimate_counts_reasoning_content():
    assert estimate_tokens([{
        "role": "assistant",
        "content": "1234",
        "reasoning_content": "abcdefgh",
    }]) == 3
    assert estimate_tokens([{
        "role": "assistant",
        "content": None,
        "reasoning_content": None,
    }]) == 0
