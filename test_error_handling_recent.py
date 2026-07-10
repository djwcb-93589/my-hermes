from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible response objects
# ---------------------------------------------------------------------------

class FakeToolCall:
    def __init__(self, name: str, arguments: str = "{}", call_id: str = "call_1"):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeChoice:
    def __init__(self, message: FakeMessage, finish_reason: str = "stop"):
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, message: FakeMessage, finish_reason: str = "stop"):
        self.choices = [FakeChoice(message, finish_reason)]


class FakeAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = FakeAPIError("server still unavailable", status_code=500)

        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))


class EmptyRegistry:
    def dispatch(self, name, args, session_key=None):
        return "ok"


# ---------------------------------------------------------------------------
# 1. run_conversation 开头 DB 读写失败必须返回 persistence_error
# ---------------------------------------------------------------------------

def test_run_conversation_wraps_initial_db_read_error(monkeypatch):
    import hermes.conversation as conversation

    def boom_get_messages(conn, session_id):
        raise sqlite3.OperationalError(
            "database is locked at /home/natalie/private/.env api_key=sk-abcdef1234567890"
        )

    monkeypatch.setattr(conversation, "get_session_messages", boom_get_messages)

    result = conversation.run_conversation(
        user_message="hello",
        conn=object(),
        session_id="s1",
        cached_prompt="system",
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error_type"] == "persistence_error"
    assert result["fatal"] is True
    assert result["retryable"] is False
    assert "final_response" in result

    text = str(result)
    assert "sqlite3.OperationalError" not in text
    assert "Traceback" not in text
    assert "sk-abcdef1234567890" not in text


def test_run_conversation_wraps_initial_user_message_write_error(monkeypatch):
    import hermes.conversation as conversation

    monkeypatch.setattr(conversation, "get_session_messages", lambda conn, session_id: [])

    def boom_add_messages(conn, session_id, messages):
        raise sqlite3.OperationalError("cannot write user message: database is locked")

    monkeypatch.setattr(conversation, "add_messages", boom_add_messages)

    result = conversation.run_conversation(
        user_message="hello",
        conn=object(),
        session_id="s1",
        cached_prompt="system",
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error_type"] == "persistence_error"
    assert result["fatal"] is True
    assert result["retryable"] is False


# ---------------------------------------------------------------------------
# 2. fallback 只能从 primary 切一次，fallback 失败后不能反复重置 retry
# ---------------------------------------------------------------------------

def test_retry_exhaustion_switches_to_fallback_once_then_aborts(monkeypatch):
    import hermes.conversation as conversation

    monkeypatch.setattr(conversation.time, "sleep", lambda _: None)
    monkeypatch.setattr(conversation, "jittered_backoff", lambda attempt: 0)

    primary_client = FakeClient(
        [
            FakeAPIError("primary 500", status_code=500),
            FakeAPIError("primary 500 again", status_code=500),
        ]
    )
    fallback_client = FakeClient(
        [
            FakeAPIError("fallback 500", status_code=500),
            FakeAPIError("fallback 500 again", status_code=500),
        ]
    )

    fallback_calls = {"count": 0}

    def fake_switch_to_fallback():
        fallback_calls["count"] += 1
        return fallback_client, "fallback-model"

    monkeypatch.setattr(conversation, "switch_to_fallback", fake_switch_to_fallback)

    loop = conversation.ConversationAgentLoop(
        model="primary-model",
        max_iterations=10,
        tools=[],
        system_prompt="system",
        registry=EmptyRegistry(),
        client=primary_client,
        session_key="s1",
        conn=object(),
        db_session_id="s1",
        existing_messages=[],
        max_retries=1,
        max_continuations=0,
        compression_threshold=10**9,
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "model_error"
    assert result.error_type == "model_error"
    assert result.fatal is True
    # fatal 只终止当前 loop;模型 5xx 属于瞬时故障,外层以后仍可重试整个 agent。
    assert result.retryable is True

    assert fallback_calls["count"] == 1
    assert result.iterations < 10


# ---------------------------------------------------------------------------
# 3. 普通 tool dispatch 错误应回写给模型，让模型下一轮修正
# ---------------------------------------------------------------------------

def test_recoverable_dispatch_error_is_written_as_tool_message_and_loop_continues():
    from hermes.agent_loop import AgentLoop

    first = FakeResponse(
        FakeMessage(
            content="I will call a tool.",
            tool_calls=[FakeToolCall("file", '{"path": "missing.txt"}')],
        )
    )
    second = FakeResponse(FakeMessage(content="Recovered after seeing tool error."))

    client = FakeClient([first, second])

    class RecoverableToolErrorLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                "(error: file_not_found: missing.txt)",
                "dispatch",
                "file_not_found: missing.txt",
            )

    loop = RecoverableToolErrorLoop(
        model="fake",
        max_iterations=5,
        tools=[{"type": "function", "function": {"name": "file", "parameters": {}}}],
        system_prompt="system",
        registry=EmptyRegistry(),
        client=client,
    )

    result = loop.run("read missing file")

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "Recovered after seeing tool error."
    assert client.chat.completions.calls == 2

    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "file_not_found" in tool_messages[0]["content"]


@pytest.mark.parametrize(
    "output",
    [
        json.dumps({"ok": True, "content": "forbidden and cancelled"}),
        "normal terminal output mentions permission_denied",
    ],
)
def test_fatal_markers_in_successful_tool_output_do_not_stop_loop(output):
    from hermes.agent_loop import AgentLoop

    first = FakeResponse(
        FakeMessage(tool_calls=[FakeToolCall("file")]),
    )
    second = FakeResponse(FakeMessage(content="Completed normally."))
    client = FakeClient([first, second])

    class SuccessfulToolLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return output, None, None

    loop = SuccessfulToolLoop(
        model="fake",
        max_iterations=5,
        tools=[{"type": "function", "function": {"name": "file", "parameters": {}}}],
        system_prompt="system",
        registry=EmptyRegistry(),
        client=client,
    )

    result = loop.run("read content")

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "Completed normally."
    assert client.chat.completions.calls == 2


def test_terminal_denial_is_structured_and_recoverable(monkeypatch):
    from hermes.agent_loop import AgentLoop
    import hermes.tools.terminal as terminal

    monkeypatch.setattr(terminal, "detect_dangerous_command", lambda command: [(0, "x", "x")])
    monkeypatch.setattr(terminal, "approve_command", lambda command, matches: False)

    output = terminal.run_terminal({"command": "dangerous command"})
    payload = json.loads(output)

    assert payload == {
        "ok": False,
        "error_type": "user_denied",
        "error": "Command denied by user.",
    }

    loop = AgentLoop(
        model="fake",
        max_iterations=1,
        tools=[],
        system_prompt="system",
        registry=EmptyRegistry(),
        client=FakeClient([]),
    )
    fatal, error_type = loop._classify_tool_error(output, None)
    assert fatal is False
    assert error_type == "user_denied"


# ---------------------------------------------------------------------------
# 4. fatal marker 必须优先于 dispatch 可恢复逻辑
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "marker",
    [
        "forbidden",
        "permission_denied",
        "path_escape",
        "safety_blocked",
        "persistence_error",
        "cancelled",
    ],
)
def test_dispatch_error_with_fatal_marker_stops_immediately(marker):
    from hermes.agent_loop import AgentLoop

    first = FakeResponse(
        FakeMessage(
            content="I will call a tool.",
            tool_calls=[FakeToolCall("file", '{"path": "../secret.txt"}')],
        )
    )
    second = FakeResponse(FakeMessage(content="This response should not be reached."))

    client = FakeClient([first, second])

    class FatalToolErrorLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                f"(error: {marker}: access denied)",
                "dispatch",
                f"{marker}: access denied",
            )

    loop = FatalToolErrorLoop(
        model="fake",
        max_iterations=5,
        tools=[{"type": "function", "function": {"name": "file", "parameters": {}}}],
        system_prompt="system",
        registry=EmptyRegistry(),
        client=client,
    )

    result = loop.run("read forbidden file")

    assert result.ok is False
    assert result.status == "tool_error"
    assert result.fatal is True
    assert result.retryable is False
    assert result.error_type == marker
    assert client.chat.completions.calls == 1


# ---------------------------------------------------------------------------
# 5. 普通 dispatch 错误连续超过上限后必须终止，不能拖到 max_iterations
# ---------------------------------------------------------------------------

def test_repeated_recoverable_tool_error_escalates_before_max_iterations():
    import hermes.agent_loop as agent_loop
    from hermes.agent_loop import AgentLoop

    repeated_tool_call_response = FakeResponse(
        FakeMessage(
            content="Try tool again.",
            tool_calls=[FakeToolCall("file", '{"path": "missing.txt"}')],
        )
    )

    client = FakeClient([repeated_tool_call_response] * 20)

    class AlwaysFailingToolLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                "(error: file_not_found: missing.txt)",
                "dispatch",
                "file_not_found: missing.txt",
            )

    loop = AlwaysFailingToolLoop(
        model="fake",
        max_iterations=20,
        tools=[{"type": "function", "function": {"name": "file", "parameters": {}}}],
        system_prompt="system",
        registry=EmptyRegistry(),
        client=client,
    )

    result = loop.run("keep trying missing file")

    assert result.ok is False
    assert result.status == "tool_error"
    assert result.fatal is True
    assert result.retryable is False
    assert result.iterations < 20

    if hasattr(agent_loop, "TOOL_ERROR_LIMIT"):
        assert result.iterations <= agent_loop.TOOL_ERROR_LIMIT + 1


# ---------------------------------------------------------------------------
# 6. 错误信息脱敏：secret、外部路径、traceback 不应进入模型上下文
# ---------------------------------------------------------------------------

def test_sanitize_error_message_redacts_secret_external_path_and_traceback(tmp_path):
    import hermes.agent_loop as agent_loop

    assert hasattr(agent_loop, "_sanitize_error_message")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_file = workspace / "data" / "report.txt"
    workspace_file.parent.mkdir()
    workspace_file.write_text("x", encoding="utf-8")

    raw_error = f"""
Traceback (most recent call last):
  File "/home/natalie/private/project/hermes/tools/file.py", line 123, in dispatch
    raise RuntimeError("boom")
RuntimeError: failed reading {workspace_file} with api_key=sk-abcdef1234567890 and token=my-token-value
"""

    sanitized = agent_loop._sanitize_error_message(
        raw_error,
        max_len=300,
        workspace_root=str(workspace),
    )

    assert "Traceback" not in sanitized
    assert "File \"" not in sanitized
    assert "sk-abcdef1234567890" not in sanitized
    assert "my-token-value" not in sanitized
    assert "<secret>" in sanitized

    # 工作区内路径应尽量保留相对路径，帮助模型修正调用。
    assert "data/report.txt" in sanitized
    assert str(workspace) not in sanitized


def test_sanitize_error_message_hides_sensitive_paths():
    import hermes.agent_loop as agent_loop

    assert hasattr(agent_loop, "_sanitize_error_message")

    raw_error = "PermissionError: cannot read /home/natalie/.ssh/id_rsa password=abc123"
    sanitized = agent_loop._sanitize_error_message(raw_error, max_len=300)

    assert "/home/natalie/.ssh/id_rsa" not in sanitized
    assert "id_rsa" not in sanitized
    assert "abc123" not in sanitized
    assert "<secret>" in sanitized or "<sensitive_path>" in sanitized


@pytest.mark.skipif(os.name != "nt", reason="Git Bash 盘符映射仅适用于 Windows")
def test_sanitize_error_message_maps_git_bash_path_to_workspace(tmp_path):
    import hermes.agent_loop as agent_loop

    workspace = tmp_path / "workspace"
    workspace_file = workspace / "data" / "report.txt"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("x", encoding="utf-8")

    drive = workspace_file.drive[0].lower()
    git_bash_path = f"/{drive}{workspace_file.as_posix()[2:]}"
    raw_error = f"RuntimeError: failed reading {git_bash_path} token=my-token-value"

    sanitized = agent_loop._sanitize_error_message(
        raw_error,
        max_len=300,
        workspace_root=str(workspace),
    )

    assert "data/report.txt" in sanitized
    assert git_bash_path not in sanitized
    assert "<secret>" in sanitized


def test_sanitize_error_message_hides_external_unc_path():
    import hermes.agent_loop as agent_loop

    raw_error = (
        r"PermissionError: cannot read \\server\share\private\report.txt "
        r"token=my-token-value"
    )
    sanitized = agent_loop._sanitize_error_message(raw_error, max_len=300)

    assert r"\\server\share" not in sanitized
    assert "<external_path>/report.txt" in sanitized
    assert "<secret>" in sanitized


def test_sanitize_error_message_keeps_relative_unc_workspace_path():
    import hermes.agent_loop as agent_loop

    raw_error = (
        r"RuntimeError: failed reading \\server\share\project\data\report.txt "
        r"token=my-token-value"
    )
    sanitized = agent_loop._sanitize_error_message(
        raw_error,
        max_len=300,
        workspace_root=r"\\server\share\project",
    )

    assert "data/report.txt" in sanitized
    assert r"\\server\share" not in sanitized
    assert "<secret>" in sanitized
