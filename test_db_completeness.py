"""
tests/test_db_completeness.py

覆盖 my-hermes 当前 db 层的核心完备性：
- 新库初始化
- schema_version
- PRAGMA: foreign_keys / busy_timeout / WAL
- foreign key / cascade
- index
- add_message 兼容
- add_messages 批量事务原子性
- tool_calls JSON 序列化 / 反序列化
- 显式错误处理
- v1 versionless 数据库迁移
- ConversationAgentLoop 的 assistant tool_call + tool results batch 持久化

运行：
    uv run pytest -q tests/test_db_completeness.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

# 避免 import hermes.conversation 时 OpenAI client 因空 key 初始化失败。
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9")

from hermes.db import (  # noqa: E402
    DBError,
    InvalidMessageError,
    LATEST_SCHEMA_VERSION,
    add_message,
    add_messages,
    complete_gateway_message,
    create_session,
    enqueue_gateway_message,
    get_gateway_queued_messages,
    get_session_messages,
    init_db,
    mark_gateway_message_processing,
    reset_gateway_processing_messages,
)
from hermes.conversation import ConversationAgentLoop  # noqa: E402


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()[0]


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "hermes_test.db"
    c = init_db(str(db_path))
    try:
        yield c
    finally:
        c.close()


def test_init_new_db_creates_schema_version_tables_pragmas_and_index(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert {
        "schema_version",
        "sessions",
        "messages",
        "gateway_message_queue",
    } <= tables
    assert scalar(conn, "SELECT version FROM schema_version") == LATEST_SCHEMA_VERSION

    assert scalar(conn, "PRAGMA foreign_keys") == 1
    assert scalar(conn, "PRAGMA busy_timeout") == 5000
    assert scalar(conn, "PRAGMA journal_mode").lower() == "wal"

    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    assert {
        "id",
        "session_id",
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "timestamp",
    } <= columns

    fk_rows = conn.execute("PRAGMA foreign_key_list(messages)").fetchall()
    assert any(row[2] == "sessions" and row[3] == "session_id" for row in fk_rows)

    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(messages)").fetchall()
    }
    assert "idx_messages_session_order" in indexes

    queue_indexes = {
        row[1]
        for row in conn.execute(
            "PRAGMA index_list(gateway_message_queue)"
        ).fetchall()
    }
    assert "idx_gateway_message_queue_status" in queue_indexes


def test_gateway_message_queue_round_trip_and_recovery_state(conn):
    enqueue_gateway_message(
        conn,
        "route-1",
        "message-1",
        '{"text":"hello"}',
    )
    mark_gateway_message_processing(conn, "route-1", "message-1")

    rows = get_gateway_queued_messages(conn)
    assert rows == [
        {
            "route_key": "route-1",
            "message_id": "message-1",
            "event_json": '{"text":"hello"}',
            "status": "processing",
        },
    ]

    reset_gateway_processing_messages(conn)
    assert get_gateway_queued_messages(conn)[0]["status"] == "queued"

    complete_gateway_message(conn, "route-1", "message-1")
    assert get_gateway_queued_messages(conn) == []


def test_repeated_init_preserves_existing_data(tmp_path: Path):
    db_path = tmp_path / "repeat_init.db"

    conn1 = init_db(str(db_path))
    sid = create_session(conn1, source="test")
    add_message(conn1, sid, {"role": "user", "content": "hello"})
    conn1.close()

    conn2 = init_db(str(db_path))
    try:
        assert scalar(conn2, "SELECT version FROM schema_version") == LATEST_SCHEMA_VERSION
        messages = get_session_messages(conn2, sid)
        assert messages == [{"role": "user", "content": "hello"}]
    finally:
        conn2.close()


def test_create_session_and_single_add_message_are_compatible(conn):
    sid = create_session(conn, source="unit")
    add_message(conn, sid, {"role": "user", "content": "first"})
    add_message(conn, sid, {"role": "assistant", "content": "second"})

    row = conn.execute(
        "SELECT source, started_at FROM sessions WHERE id=?",
        (sid,),
    ).fetchone()
    assert row[0] == "unit"
    assert isinstance(row[1], float)

    messages = get_session_messages(conn, sid)
    assert messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]


def test_get_messages_returns_in_insert_order(conn):
    sid = create_session(conn)
    add_messages(
        conn,
        sid,
        [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "3"},
        ],
    )

    assert [m["content"] for m in get_session_messages(conn, sid)] == ["1", "2", "3"]


def test_foreign_key_rejects_orphan_message(conn):
    with pytest.raises(InvalidMessageError):
        add_message(
            conn,
            "missing-session",
            {"role": "user", "content": "orphan"},
        )


def test_delete_session_cascades_messages(conn):
    sid = create_session(conn)
    add_message(conn, sid, {"role": "user", "content": "will be deleted"})

    assert scalar(conn, "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)) == 1

    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()

    assert scalar(conn, "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)) == 0


def test_add_messages_commits_all_on_success(conn):
    sid = create_session(conn)

    batch = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "u2"},
    ]

    add_messages(conn, sid, batch)

    assert get_session_messages(conn, sid) == batch


def test_add_messages_rolls_back_whole_batch_on_invalid_message(conn):
    sid = create_session(conn)

    with pytest.raises(InvalidMessageError):
        add_messages(
            conn,
            sid,
            [
                {"role": "user", "content": "should rollback"},
                {"role": "bad_role", "content": "invalid"},
            ],
        )

    assert get_session_messages(conn, sid) == []


def test_tool_calls_round_trip_as_python_structure(conn):
    sid = create_session(conn)

    assistant_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "pwd"}),
                },
            }
        ],
    }
    tool_msg = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "/tmp/project",
    }

    add_messages(conn, sid, [assistant_msg, tool_msg])

    messages = get_session_messages(conn, sid)
    assert messages[0]["role"] == "assistant"
    assert isinstance(messages[0]["tool_calls"], list)
    assert messages[0]["tool_calls"][0]["function"]["name"] == "terminal"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_1"


def test_empty_tool_calls_are_not_returned_as_fake_calls(conn):
    sid = create_session(conn)

    add_message(
        conn,
        sid,
        {"role": "assistant", "content": "no tools", "tool_calls": []},
    )

    msg = get_session_messages(conn, sid)[0]
    assert msg == {"role": "assistant", "content": "no tools"}
    assert "tool_calls" not in msg


@pytest.mark.parametrize(
    "bad_msg",
    [
        {"role": "invalid", "content": "bad role"},
        {"role": "tool", "content": "tool without tool_call_id"},
        {"role": "assistant", "content": "", "tool_calls": {"not": "a list"}},
        {"role": "assistant", "content": "", "tool_calls": [{"bad": object()}]},
    ],
)
def test_invalid_messages_raise_clear_errors(conn, bad_msg):
    sid = create_session(conn)

    with pytest.raises(InvalidMessageError):
        add_message(conn, sid, bad_msg)

    assert get_session_messages(conn, sid) == []


def test_missing_session_id_raises(conn):
    with pytest.raises(InvalidMessageError):
        add_message(conn, "", {"role": "user", "content": "missing sid"})


def test_add_messages_requires_list_or_tuple(conn):
    sid = create_session(conn)

    with pytest.raises(InvalidMessageError):
        add_messages(conn, sid, {"role": "user", "content": "not a list"})  # type: ignore[arg-type]


def test_newer_schema_version_is_rejected(tmp_path: Path):
    db_path = tmp_path / "future.db"

    raw = sqlite3.connect(db_path)
    raw.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    raw.execute("INSERT INTO schema_version(version) VALUES (?)", (LATEST_SCHEMA_VERSION + 100,))
    raw.commit()
    raw.close()

    with pytest.raises(DBError):
        init_db(str(db_path))


def test_versionless_v1_db_migrates_to_latest_schema(tmp_path: Path):
    db_path = tmp_path / "v1.db"

    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL
        );
        """
    )

    tool_calls_json = json.dumps(
        [
            {
                "id": "old_call_1",
                "type": "function",
                "function": {
                    "name": "file",
                    "arguments": json.dumps({"path": "README.md"}),
                },
            }
        ]
    )

    raw.execute(
        "INSERT INTO sessions(id, source, started_at) VALUES (?, ?, ?)",
        ("old-session", "legacy", 1.0),
    )
    raw.execute(
        """
        INSERT INTO messages(
            session_id, role, content, tool_calls, tool_call_id, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("old-session", "assistant", "", tool_calls_json, None, 2.0),
    )
    raw.execute(
        """
        INSERT INTO messages(
            session_id, role, content, tool_calls, tool_call_id, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("old-session", "tool", "legacy result", None, "old_call_1", 3.0),
    )
    raw.commit()
    raw.close()

    conn = init_db(str(db_path))
    try:
        assert scalar(conn, "SELECT version FROM schema_version") == LATEST_SCHEMA_VERSION

        fk_rows = conn.execute("PRAGMA foreign_key_list(messages)").fetchall()
        assert any(row[2] == "sessions" for row in fk_rows)

        indexes = {
            row[1]
            for row in conn.execute("PRAGMA index_list(messages)").fetchall()
        }
        assert "idx_messages_session_order" in indexes

        messages = get_session_messages(conn, "old-session")
        assert len(messages) == 2
        assert messages[0]["tool_calls"][0]["function"]["name"] == "file"
        assert messages[1]["tool_call_id"] == "old_call_1"
    finally:
        conn.close()


def test_invalid_versionless_v1_db_migration_is_rejected(tmp_path: Path):
    db_path = tmp_path / "bad_v1.db"

    raw = sqlite3.connect(db_path)
    raw.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL
        );

        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL
        );
        """
    )
    raw.execute(
        """
        INSERT INTO messages(
            session_id, role, content, tool_calls, tool_call_id, timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("missing-session", "user", "orphan", None, None, 1.0),
    )
    raw.commit()
    raw.close()

    with pytest.raises(DBError):
        init_db(str(db_path))


# -------------------------
# AgentLoop / Conversation 持久化集成测试
# -------------------------

def tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def message(content: str = "", tool_calls=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
    )


def response(msg, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=msg,
                finish_reason=finish_reason,
            )
        ]
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("fake model received more calls than expected")
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeRegistry:
    def __init__(self, *, should_raise: bool = False):
        self.should_raise = should_raise
        self.calls = []

    def dispatch(self, tool_name, tool_args, session_key=None):
        self.calls.append((tool_name, tool_args, session_key))
        if self.should_raise:
            raise RuntimeError("boom from fake tool")
        return f"ok:{tool_name}:{tool_args}"


def make_loop(
    *,
    conn: sqlite3.Connection,
    sid: str,
    client: FakeClient,
    registry: FakeRegistry,
):
    return ConversationAgentLoop(
        model="fake-model",
        max_iterations=5,
        tools=[],
        system_prompt="system",
        registry=registry,
        client=client,
        session_key=sid,
        conn=conn,
        db_session_id=sid,
        existing_messages=[],
        max_retries=0,
        max_continuations=0,
        compression_threshold=10**9,
        model_kwargs=None,
    )


def test_conversation_tool_call_and_tool_result_are_persisted_as_one_complete_pair(conn):
    sid = create_session(conn)
    add_message(conn, sid, {"role": "user", "content": "use tool"})

    tc = tool_call("call_batch_1", "fake_tool", {"value": 123})
    fake_client = FakeClient(
        [
            response(message("", [tc]), finish_reason="tool_calls"),
            response(message("done"), finish_reason="stop"),
        ]
    )
    fake_registry = FakeRegistry()

    loop = make_loop(conn=conn, sid=sid, client=fake_client, registry=fake_registry)
    result = loop.run("use tool")

    assert result.status == "completed"
    assert result.summary == "done"

    db_messages = get_session_messages(conn, sid)

    assert [m["role"] for m in db_messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]

    assistant_tool = db_messages[1]
    tool_result = db_messages[2]

    assert assistant_tool["tool_calls"][0]["id"] == "call_batch_1"
    assert assistant_tool["tool_calls"][0]["function"]["name"] == "fake_tool"
    assert tool_result["tool_call_id"] == "call_batch_1"
    assert "ok:fake_tool" in tool_result["content"]

    assert fake_registry.calls == [
        ("fake_tool", {"value": 123}, sid),
    ]


def test_conversation_tool_error_is_still_persisted_with_assistant_tool_call(conn):
    sid = create_session(conn)
    add_message(conn, sid, {"role": "user", "content": "use failing tool"})

    tc = tool_call("call_error_1", "bad_tool", {"value": "x"})
    fake_client = FakeClient(
        [
            response(message("", [tc]), finish_reason="tool_calls"),
        ]
    )
    fake_registry = FakeRegistry(should_raise=True)

    loop = make_loop(conn=conn, sid=sid, client=fake_client, registry=fake_registry)
    result = loop.run("use failing tool")

    assert result.status == "tool_error"
    assert "bad_tool" in result.error

    db_messages = get_session_messages(conn, sid)

    assert [m["role"] for m in db_messages] == [
        "user",
        "assistant",
        "tool",
    ]

    assert db_messages[1]["tool_calls"][0]["id"] == "call_error_1"
    assert db_messages[2]["tool_call_id"] == "call_error_1"
    assert "error" in db_messages[2]["content"].lower()
    assert "bad_tool" in db_messages[2]["content"]


def test_conversation_plain_assistant_message_still_persists_normally(conn):
    sid = create_session(conn)
    add_message(conn, sid, {"role": "user", "content": "plain"})

    fake_client = FakeClient(
        [
            response(message("plain answer"), finish_reason="stop"),
        ]
    )
    fake_registry = FakeRegistry()

    loop = make_loop(conn=conn, sid=sid, client=fake_client, registry=fake_registry)
    result = loop.run("plain")

    assert result.status == "completed"

    db_messages = get_session_messages(conn, sid)
    assert db_messages == [
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": "plain answer"},
    ]
