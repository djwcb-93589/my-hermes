"""Gateway File / Terminal 远程审批专项测试。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9")

from hermes.agent_loop import AgentLoop  # noqa: E402
from hermes.approval import build_approval_required  # noqa: E402
from hermes.db import (  # noqa: E402
    add_messages,
    claim_gateway_approval,
    create_gateway_approval_with_outbox,
    deny_gateway_approval,
    enqueue_gateway_message,
    ensure_session,
    finish_gateway_approval,
    get_gateway_outbox,
    get_pending_gateway_approval,
    get_session_messages,
    init_db,
    recover_gateway_approvals,
)
from hermes.gateway.runner import GatewayRunner  # noqa: E402
from hermes.gateway.runner import _GatewayAgentResult  # noqa: E402
from hermes.gateway.types import MessageEvent, SendResult, SessionSource  # noqa: E402
from hermes.gateway.types import build_session_key  # noqa: E402
from hermes.tools import file as file_tool  # noqa: E402
from hermes.tools import register_all  # noqa: E402
from hermes.tools import terminal as terminal_tool  # noqa: E402


class _Backend:
    def __init__(self, root: Path):
        self.cwd = str(root)
        self.file_root = str(root)
        self.execute_calls: list[str] = []
        self.list_calls: list[str] = []

    def execute(self, command: str) -> dict:
        self.execute_calls.append(command)
        return {"output": "done\n", "returncode": 0}

    def resolve_path(self, path: str) -> str:
        target = Path(path)
        if not target.is_absolute():
            target = Path(self.cwd) / target
        return str(target)

    def stat_file(self, path: str) -> dict:
        target = Path(path)
        return {
            "size": target.stat().st_size,
            "is_dir": target.is_dir(),
            "is_file": target.is_file(),
            "mtime": target.stat().st_mtime,
        }

    def read_file(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        with open(path, "rb") as stream:
            stream.seek(offset)
            return stream.read(limit)

    def write_file(self, path: str, content: bytes, mode: str = "write") -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(target, "ab") as stream:
                stream.write(content)
        else:
            target.write_bytes(content)

    def list_dir(self, path: str) -> list[str]:
        self.list_calls.append(path)
        return [entry.name for entry in Path(path).iterdir()]


@pytest.mark.parametrize(
    "args",
    [
        {"action": "read", "path": "target.txt"},
        {"action": "read_range", "path": "target.txt", "offset": 0, "limit": 1},
        {"action": "write", "path": "new.txt", "content": "new"},
        {"action": "append", "path": "target.txt", "content": "more"},
        {"action": "replace", "path": "target.txt", "find": "a", "replace": "b"},
        {"action": "list", "path": "."},
        {"action": "stat", "path": "target.txt"},
    ],
)
def test_every_file_path_action_waits_for_remote_approval(
    tmp_path,
    monkeypatch,
    args,
):
    backend = _Backend(tmp_path)
    (tmp_path / "target.txt").write_text("abc", encoding="utf-8")
    monkeypatch.setattr(file_tool, "get_backend", lambda session_key: backend)

    payload = json.loads(file_tool.handle_file(
        args,
        approval_mode="remote",
        session_key="session-1",
    ))

    assert payload["approval_required"] is True
    assert payload["error_type"] == "approval_required"
    assert backend.list_calls == []
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "abc"
    assert not (tmp_path / "new.txt").exists()


def test_approved_file_operation_executes_exact_action(tmp_path, monkeypatch):
    backend = _Backend(tmp_path)
    monkeypatch.setattr(file_tool, "get_backend", lambda session_key: backend)

    payload = json.loads(file_tool.handle_file(
        {"action": "write", "path": "approved.txt", "content": "approved"},
        approval_mode="remote",
        approval_grant={
            "id": "approval_test",
            "tool_name": "file",
            "arguments": {
                "action": "write",
                "path": "approved.txt",
                "content": "approved",
            },
        },
        session_key="session-1",
    ))

    assert payload["ok"] is True
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved"


def test_terminal_requires_approval_for_plain_rm_and_compound_cd(
    tmp_path,
    monkeypatch,
):
    backend = _Backend(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_backend", lambda session_key: backend)

    plain_rm = json.loads(terminal_tool.run_terminal(
        {"command": "rm 1.md"},
        approval_mode="remote",
        interactive_approval=False,
    ))
    compound_cd = json.loads(terminal_tool.run_terminal(
        {"command": "cd /d && ls"},
        approval_mode="remote",
        interactive_approval=False,
    ))

    assert plain_rm["approval_required"] is True
    assert compound_cd["approval_required"] is True
    assert backend.execute_calls == []


def test_terminal_allows_only_pure_cwd_command_without_approval(
    tmp_path,
    monkeypatch,
):
    backend = _Backend(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_backend", lambda session_key: backend)

    cd_result = json.loads(terminal_tool.run_terminal(
        {"command": "cd /d"},
        approval_mode="remote",
        interactive_approval=False,
    ))
    pwd_result = json.loads(terminal_tool.run_terminal(
        {"command": "pwd"},
        approval_mode="remote",
        interactive_approval=False,
    ))

    assert cd_result["ok"] is True
    assert pwd_result["ok"] is True
    assert backend.execute_calls == ["cd /d", "pwd"]


def test_approved_terminal_command_bypasses_remote_gate_once(
    tmp_path,
    monkeypatch,
):
    backend = _Backend(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_backend", lambda session_key: backend)

    payload = json.loads(terminal_tool.run_terminal(
        {"command": "rm 1.md"},
        approval_mode="remote",
        approval_grant={
            "id": "approval_test",
            "tool_name": "terminal",
            "arguments": {"command": "rm 1.md"},
        },
        interactive_approval=False,
    ))

    assert payload["ok"] is True
    assert backend.execute_calls == ["rm 1.md"]


def test_approval_grant_cannot_be_reused_for_changed_arguments(
    tmp_path,
    monkeypatch,
):
    backend = _Backend(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_backend", lambda session_key: backend)

    payload = json.loads(terminal_tool.run_terminal(
        {"command": "rm changed.md"},
        approval_mode="remote",
        approval_grant={
            "id": "approval_test",
            "tool_name": "terminal",
            "arguments": {"command": "rm original.md"},
        },
        interactive_approval=False,
    ))

    assert payload["approval_required"] is True
    assert backend.execute_calls == []


def _tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


class _ApprovalRegistry:
    def __init__(self):
        self.calls: list[str] = []

    def dispatch(self, name, args, **kwargs):
        self.calls.append(name)
        return build_approval_required(name, "需要审批")


def test_agent_loop_pauses_on_first_approval_and_defers_later_calls():
    registry = _ApprovalRegistry()
    loop = AgentLoop(
        model="test",
        max_iterations=1,
        tools=[],
        system_prompt="test",
        registry=registry,
    )
    messages: list[dict] = []
    first = _tool_call("call-1", "file", {"action": "list", "path": "."})
    second = _tool_call("call-2", "terminal", {"command": "ls"})

    tool_messages, result = loop.process_tool_calls([first, second], messages)

    assert registry.calls == ["file"]
    assert result is not None
    assert result.status == "awaiting_approval"
    assert result.approval_request["arguments"] == {
        "action": "list",
        "path": ".",
    }
    assert json.loads(tool_messages[1]["content"])["error_type"] == "approval_deferred"


def _approval_fixture(conn):
    route_key = "route-1"
    session_id = "conversation-1"
    ensure_session(conn, session_id, source="feishu")
    enqueue_gateway_message(conn, route_key, "message-1", "{}")
    request_id = "approval_1234567890abcdef"
    add_messages(conn, session_id, [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "rm 1.md"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps({
                "approval_required": True,
                "approval_request": {"id": request_id},
            }),
        },
    ])
    request = {
        "id": request_id,
        "tool_name": "terminal",
        "tool_call_id": "call-1",
        "arguments": {"command": "rm 1.md"},
        "summary": "执行 Terminal 命令",
        "details": {"command": "rm 1.md"},
    }
    outbox = {
        "id": "delivery-1",
        "route_key": route_key,
        "source_message_id": "message-1",
        "event_json": "{}",
        "platform": "feishu",
        "chat_id": "chat-1",
        "reply_to_message_id": "message-1",
        "thread_id": None,
        "delivery_kind": "approval_request",
        "payloads": [{"content": "approve?"}],
    }
    create_gateway_approval_with_outbox(
        conn,
        session_id,
        request,
        "user-1",
        {"role": "assistant", "content": "approve?"},
        outbox,
        600,
    )
    return route_key, session_id, request_id


def test_approval_is_bound_to_user_and_cannot_be_replayed(tmp_path):
    conn = init_db(str(tmp_path / "approval.db"))
    try:
        route_key, session_id, request_id = _approval_fixture(conn)

        forbidden = claim_gateway_approval(
            conn,
            route_key,
            session_id,
            "other-user",
            request_id[:22],
            "decision-other",
        )
        claimed = claim_gateway_approval(
            conn,
            route_key,
            session_id,
            "user-1",
            request_id[:22],
            "decision-1",
        )
        finish_gateway_approval(
            conn,
            request_id,
            json.dumps({"ok": True, "output": "done"}),
            succeeded=True,
        )
        replay = claim_gateway_approval(
            conn,
            route_key,
            session_id,
            "user-1",
            request_id[:22],
            "decision-2",
        )

        assert forbidden["outcome"] == "forbidden"
        assert claimed["outcome"] == "claimed"
        assert replay["outcome"] == "executed"
        assert get_pending_gateway_approval(conn, route_key, session_id) is None
        tool_result = get_session_messages(conn, session_id)[1]
        assert json.loads(tool_result["content"])["output"] == "done"
    finally:
        conn.close()


def test_denied_and_interrupted_approvals_never_execute_again(tmp_path):
    denied_conn = init_db(str(tmp_path / "denied.db"))
    try:
        route_key, session_id, request_id = _approval_fixture(denied_conn)
        denied = deny_gateway_approval(
            denied_conn,
            route_key,
            session_id,
            "user-1",
            request_id,
            "decision-deny",
        )
        assert denied["outcome"] == "denied"
        assert json.loads(get_session_messages(denied_conn, session_id)[1]["content"])[
            "error_type"
        ] == "approval_denied"
    finally:
        denied_conn.close()

    interrupted_conn = init_db(str(tmp_path / "interrupted.db"))
    try:
        route_key, session_id, request_id = _approval_fixture(interrupted_conn)
        claimed = claim_gateway_approval(
            interrupted_conn,
            route_key,
            session_id,
            "user-1",
            request_id,
            "decision-claim",
        )
        assert claimed["outcome"] == "claimed"
        recovered = recover_gateway_approvals(interrupted_conn)
        assert recovered["execution_unknown"] == 1
        replay = claim_gateway_approval(
            interrupted_conn,
            route_key,
            session_id,
            "user-1",
            request_id,
            "decision-replay",
        )
        assert replay["outcome"] == "execution_unknown"
    finally:
        interrupted_conn.close()


def test_expired_approval_is_closed_without_execution(tmp_path):
    conn = init_db(str(tmp_path / "expired.db"))
    try:
        route_key, session_id, request_id = _approval_fixture(conn)
        conn.execute(
            "UPDATE gateway_approval_requests SET expires_at=0 WHERE id=?",
            (request_id,),
        )
        conn.commit()

        assert get_pending_gateway_approval(conn, route_key, session_id) is None
        decision = claim_gateway_approval(
            conn,
            route_key,
            session_id,
            "user-1",
            request_id,
            "decision-late",
        )
        assert decision["outcome"] == "expired"
        assert json.loads(get_session_messages(conn, session_id)[1]["content"])[
            "error_type"
        ] == "approval_expired"
    finally:
        conn.close()


def test_gateway_persists_approval_question_and_request_atomically(
    tmp_path,
    monkeypatch,
):
    import hermes.conversation as conversation

    request_id = "approval_feedface12345678"

    async def fake_run_conversation_async(*args, **kwargs):
        session_id = args[2]
        await kwargs["persistence_call"](
            add_messages,
            session_id,
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-gateway",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "rm 1.md"}),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-gateway",
                    "content": json.dumps({
                        "approval_required": True,
                        "approval_request": {"id": request_id},
                    }),
                },
            ],
        )
        return {
            "ok": False,
            "status": "awaiting_approval",
            "error_type": "approval_required",
            "approval_request": {
                "id": request_id,
                "tool_name": "terminal",
                "tool_call_id": "call-gateway",
                "arguments": {"command": "rm 1.md"},
                "summary": "执行 Terminal 命令",
                "details": {"command": "rm 1.md"},
            },
        }

    monkeypatch.setattr(
        conversation,
        "run_conversation_async",
        fake_run_conversation_async,
    )
    db_path = str(tmp_path / "gateway-approval.db")
    runner = GatewayRunner(
        config={
            "gateway": {
                "platforms": {"feishu": {"toolsets": ["terminal", "file"]}},
            },
        },
        db_path=db_path,
    )
    runner._get_async_client = lambda: object()
    event = MessageEvent(
        message_id="gateway-message",
        text="delete it",
        source=SessionSource(
            platform="feishu",
            account_id="app-1",
            chat_id="chat-1",
            user_id="user-1",
        ),
    )
    route_key = "route-gateway"
    ctx = SimpleNamespace(
        active_generation=0,
        generation=0,
        cancel_requested=False,
        route_key=route_key,
        conversation_id="conversation-gateway",
        system_prompt="system",
        delivery_generation=0,
        delivery_id="delivery-gateway",
    )
    conn = init_db(db_path)
    try:
        enqueue_gateway_message(
            conn,
            route_key,
            event.message_id,
            runner._serialize_event(event),
        )
    finally:
        conn.close()

    result = asyncio.run(runner._run_agent_async(event, ctx))

    conn = init_db(db_path)
    try:
        pending = get_pending_gateway_approval(
            conn,
            route_key,
            ctx.conversation_id,
        )
        outbox = get_gateway_outbox(conn, "delivery-gateway")
        assert result.failed is False
        assert "/approve feedface1234" in result.response
        assert pending["id"] == request_id
        assert pending["requester_user_id"] == "user-1"
        assert outbox["delivery_kind"] == "approval_request"
    finally:
        conn.close()
        asyncio.run(runner.persistence.close())


def test_gateway_approve_executes_exact_request_once_and_resumes(
    tmp_path,
    monkeypatch,
):
    register_all()
    backend = _Backend(tmp_path)
    monkeypatch.setattr(terminal_tool, "get_backend", lambda session_key: backend)
    db_path = str(tmp_path / "gateway-approve-command.db")
    runner = GatewayRunner(
        config={"gateway": {"platforms": {"feishu": {"toolsets": ["terminal"]}}}},
        db_path=db_path,
    )
    source = SessionSource(
        platform="feishu",
        account_id="app-1",
        chat_id="chat-1",
        user_id="user-1",
    )
    route_key = build_session_key(source)

    async def scenario():
        ctx = await runner.sessions.get_or_create_async(route_key, "system")
        conn = init_db(db_path)
        try:
            ensure_session(conn, ctx.conversation_id, source="feishu")
            enqueue_gateway_message(conn, route_key, "original-message", "{}")
            request_id = "approval_aabbccddeeff0011"
            add_messages(conn, ctx.conversation_id, [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-approve",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"command": "rm 1.md"}),
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-approve",
                    "content": json.dumps({
                        "approval_required": True,
                        "approval_request": {"id": request_id},
                    }),
                },
            ])
            create_gateway_approval_with_outbox(
                conn,
                ctx.conversation_id,
                {
                    "id": request_id,
                    "tool_name": "terminal",
                    "tool_call_id": "call-approve",
                    "arguments": {"command": "rm 1.md"},
                    "summary": "执行 Terminal 命令",
                    "details": {"command": "rm 1.md"},
                },
                "user-1",
                {"role": "assistant", "content": "approve?"},
                {
                    "id": "approval-question-delivery",
                    "route_key": route_key,
                    "source_message_id": "original-message",
                    "event_json": "{}",
                    "platform": "feishu",
                    "chat_id": "chat-1",
                    "reply_to_message_id": "original-message",
                    "thread_id": None,
                    "delivery_kind": "approval_request",
                    "payloads": [{"content": "approve?"}],
                },
                600,
            )
        finally:
            conn.close()

        replies: list[str] = []

        async def fake_reply(event, content):
            replies.append(content)
            return SendResult(success=True)

        async def fake_run_agent(event, current_ctx):
            assert event.metadata["gateway_approval_resume"] == request_id
            assert "不要重复执行同一操作" in event.text
            return _GatewayAgentResult("continued")

        monkeypatch.setattr(runner, "_reply", fake_reply)
        monkeypatch.setattr(runner, "_run_agent", fake_run_agent)

        approve_event = MessageEvent(
            message_id="approve-message",
            text="/approve aabbccddeeff",
            source=source,
        )
        await runner._handle_message(approve_event)
        current_ctx = await runner.sessions.get_or_create_async(route_key, "system")
        if current_ctx.worker_task is not None:
            await current_ctx.worker_task

        replay_event = MessageEvent(
            message_id="approve-replay",
            text="/approve aabbccddeeff",
            source=source,
        )
        await runner._handle_message(replay_event)

        assert backend.execute_calls == ["rm 1.md"]
        assert "continued" in replies
        assert any("已经执行" in reply for reply in replies)

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(runner.persistence.close())


def test_v13_database_migrates_to_persistent_approval_schema(tmp_path):
    db_path = str(tmp_path / "v13.db")
    conn = init_db(db_path)
    try:
        conn.execute("DROP TABLE gateway_approval_requests")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (13)")
        conn.commit()
    finally:
        conn.close()

    migrated = init_db(db_path)
    try:
        version = migrated.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        table = migrated.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='gateway_approval_requests'
            """
        ).fetchone()
        assert version == 14
        assert table[0] == "gateway_approval_requests"
    finally:
        migrated.close()
