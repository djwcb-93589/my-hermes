from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest


# 避免测试环境没有配置真实 API key 时，导入 hermes.config 失败。
# 不会发起任何真实网络请求，所有模型调用都由 FakeClient 接管。
os.environ.setdefault("OPENAI_API_KEY", "test-key")


# ============================================================================
# Fake OpenAI-compatible objects
# ============================================================================

class FakeToolCall:
    def __init__(
        self,
        name: str,
        arguments: str = "{}",
        call_id: str = "call_1",
    ):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeChoice:
    def __init__(
        self,
        message: FakeMessage,
        finish_reason: str = "stop",
    ):
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(
        self,
        message: FakeMessage,
        finish_reason: str = "stop",
    ):
        self.choices = [FakeChoice(message, finish_reason)]


class FakeAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)

        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            raise AssertionError(
                "FakeClient 没有剩余响应；说明 agent 发生了非预期额外模型调用"
            )

        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(outcomes)
        )


class BlockingAsyncCompletions:
    """等待取消的异步模型调用,用于验证 HTTP Task 取消链路。"""

    def __init__(self):
        import asyncio

        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def create(self, **kwargs):
        import asyncio

        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class BlockingAsyncClient:
    def __init__(self):
        self.completions = BlockingAsyncCompletions()
        self.chat = SimpleNamespace(completions=self.completions)

    async def close(self):
        pass


class FakeAsyncCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("FakeAsyncClient 没有剩余响应")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeAsyncClient:
    def __init__(self, outcomes):
        self.completions = FakeAsyncCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    async def close(self):
        self.closed = True


class RecordingRegistry:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or ["ok"])
        self.calls: list[tuple[str, dict, str | None]] = []

    def dispatch(self, name, args, session_key=None):
        self.calls.append((name, args, session_key))

        if self.outcomes:
            outcome = self.outcomes.pop(0)
        else:
            outcome = "ok"

        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def tool_response(
    name: str = "file",
    arguments: str = "{}",
    *,
    call_id: str = "call_1",
    content: str = "",
    finish_reason: str = "tool_calls",
) -> FakeResponse:
    return FakeResponse(
        FakeMessage(
            content=content,
            tool_calls=[FakeToolCall(name, arguments, call_id)],
        ),
        finish_reason=finish_reason,
    )


def text_response(
    content: str = "done",
    *,
    finish_reason: str = "stop",
) -> FakeResponse:
    return FakeResponse(
        FakeMessage(content=content),
        finish_reason=finish_reason,
    )


def make_base_loop(
    *,
    outcomes,
    registry=None,
    max_iterations: int = 10,
    tools=None,
    cancel_checker=None,
    model_kwargs=None,
):
    from hermes.agent_loop import AgentLoop

    return AgentLoop(
        model="fake-model",
        max_iterations=max_iterations,
        tools=tools if tools is not None else [
            {
                "type": "function",
                "function": {
                    "name": "file",
                    "parameters": {},
                },
            }
        ],
        system_prompt="system",
        registry=registry or RecordingRegistry(),
        client=FakeClient(outcomes),
        session_key="session-1",
        cancel_checker=cancel_checker,
        model_kwargs=model_kwargs,
    )


def make_conversation_loop(
    *,
    primary_outcomes,
    registry=None,
    max_iterations: int = 12,
    max_retries: int = 1,
    max_continuations: int = 1,
    compression_threshold: int = 10**9,
):
    import hermes.conversation as conversation

    return conversation.ConversationAgentLoop(
        model="primary-model",
        max_iterations=max_iterations,
        tools=[],
        system_prompt="system",
        registry=registry or RecordingRegistry(),
        client=FakeClient(primary_outcomes),
        session_key="session-1",
        conn=object(),
        db_session_id="db-session-1",
        existing_messages=[],
        max_retries=max_retries,
        max_continuations=max_continuations,
        compression_threshold=compression_threshold,
    )


# ============================================================================
# 1. errors.py：分类、网络识别、backoff、fallback client
# ============================================================================

@pytest.mark.parametrize(
    (
        "status_code",
        "message",
        "reason",
        "retryable",
        "should_compress",
        "should_fallback",
    ),
    [
        (None, "Connection timed out", "network_or_timeout", True, False, False),
        (None, "SSL EOF occurred", "network_or_timeout", True, False, False),
        (429, "too many requests", "rate_limit", True, False, False),
        (400, "maximum context length exceeded", "context_overflow", True, True, False),
        (500, "internal server error", "server_error", True, False, False),
        (502, "bad gateway", "server_error", True, False, False),
        (503, "temporarily unavailable", "server_error", True, False, False),
        (401, "invalid api key", "auth", False, False, True),
        (403, "forbidden", "auth", False, False, True),
        (404, "model not found", "model_not_found", False, False, True),
        (418, "teapot", "unknown", False, False, False),
        (None, "plain local error", "unknown", False, False, False),
    ],
)
def test_classify_error_matrix(
    status_code,
    message,
    reason,
    retryable,
    should_compress,
    should_fallback,
):
    from hermes.errors import classify_error

    result = classify_error(status_code, message)

    assert result == {
        "reason": reason,
        "retryable": retryable,
        "should_compress": should_compress,
        "should_fallback": should_fallback,
    }


@pytest.mark.parametrize(
    "message",
    [
        "TIMEOUT",
        "request timed out",
        "connection reset by peer",
        "Name or service not known",
        "getaddrinfo failed",
        "SSL EOF",
    ],
)
def test_is_network_error_message_positive(message):
    from hermes.errors import is_network_error_message

    assert is_network_error_message(message) is True


@pytest.mark.parametrize("message", ["", "bad request", "invalid JSON", "permission denied"])
def test_is_network_error_message_negative(message):
    from hermes.errors import is_network_error_message

    assert is_network_error_message(message) is False


def test_jittered_backoff_uses_exponential_delay_and_cap(monkeypatch):
    import hermes.errors as errors

    monkeypatch.setattr(errors.random, "uniform", lambda low, high: high)

    assert errors.jittered_backoff(1, base_delay=5, max_delay=120) == 7.5
    assert errors.jittered_backoff(2, base_delay=5, max_delay=120) == 15
    assert errors.jittered_backoff(20, base_delay=5, max_delay=120) == 180


def test_switch_to_fallback_returns_none_when_not_configured(monkeypatch):
    import hermes.errors as errors

    monkeypatch.setattr(errors, "FALLBACK_MODEL", "")

    assert errors.switch_to_fallback() == (None, None)


def test_switch_to_fallback_builds_client_from_fallback_config(monkeypatch):
    import hermes.errors as errors

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(errors, "FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(errors, "FALLBACK_BASE_URL", "https://fallback.invalid/v1")
    monkeypatch.setattr(errors, "FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(errors, "OpenAI", FakeOpenAI)

    client, model = errors.switch_to_fallback()

    assert isinstance(client, FakeOpenAI)
    assert model == "fallback-model"
    assert captured == {
        "base_url": "https://fallback.invalid/v1",
        "api_key": "fallback-key",
        "timeout": errors.MODEL_TIMEOUT_SECONDS,
    }


def test_switch_to_async_fallback_builds_async_client(monkeypatch):
    import hermes.errors as errors

    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(errors, "FALLBACK_MODEL", "fallback-model")
    monkeypatch.setattr(errors, "FALLBACK_BASE_URL", "https://fallback.invalid/v1")
    monkeypatch.setattr(errors, "FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setattr(errors, "AsyncOpenAI", FakeAsyncOpenAI)

    client, model = errors.switch_to_async_fallback()

    assert isinstance(client, FakeAsyncOpenAI)
    assert model == "fallback-model"
    assert captured == {
        "base_url": "https://fallback.invalid/v1",
        "api_key": "fallback-key",
        "timeout": errors.MODEL_TIMEOUT_SECONDS,
    }


# ============================================================================
# 2. 错误信息脱敏、路径处理、fatal marker
# ============================================================================

def test_sanitize_error_message_keeps_only_last_traceback_line_and_redacts_secrets(
    tmp_path,
):
    from hermes.agent_loop import _sanitize_error_message

    workspace = tmp_path / "workspace"
    target = workspace / "data" / "report.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    raw = f"""
Traceback (most recent call last):
  File "/home/user/project/hermes/tools/file.py", line 10, in run
    raise RuntimeError("boom")
RuntimeError: failed reading {target} api_key=sk-abcdefghijk token=my-token
"""

    result = _sanitize_error_message(
        raw,
        max_len=300,
        workspace_root=str(workspace),
    )

    assert "Traceback" not in result
    assert "File \"" not in result
    assert "sk-abcdefghijk" not in result
    assert "my-token" not in result
    assert "<secret>" in result
    assert "data/report.txt" in result
    assert str(workspace) not in result


def test_sanitize_error_message_hides_external_unix_parent_path(tmp_path):
    from hermes.agent_loop import _sanitize_error_message

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _sanitize_error_message(
        "FileNotFoundError: /home/private/documents/report.txt",
        workspace_root=str(workspace),
    )

    assert "/home/private/documents" not in result
    assert "<external_path>/report.txt" in result


def test_sanitize_error_message_hides_sensitive_path_and_password():
    from hermes.agent_loop import _sanitize_error_message

    result = _sanitize_error_message(
        "PermissionError: /home/alice/.ssh/id_rsa password=abc123",
        max_len=300,
    )

    assert "/home/alice/.ssh/id_rsa" not in result
    assert "id_rsa" not in result
    assert "abc123" not in result
    assert "<sensitive_path>" in result
    assert "<secret>" in result


def test_sanitize_error_message_respects_max_len():
    from hermes.agent_loop import _sanitize_error_message

    result = _sanitize_error_message("x" * 1000, max_len=30)

    assert result == ("x" * 30) + "..."


def test_sanitize_error_message_empty_exception_has_safe_fallback():
    from hermes.agent_loop import _sanitize_error_message

    assert _sanitize_error_message("") == "Tool execution failed."


def test_normalize_msys_path_for_windows_workspace():
    from hermes.agent_loop import _normalize_msys_path

    assert (
        _normalize_msys_path(
            "/d/my-hermes/workspace/data/a.txt",
            r"D:\my-hermes\workspace",
        )
        == r"D:\my-hermes\workspace\data\a.txt"
    )


def test_normalize_msys_path_does_not_convert_for_non_windows_workspace():
    from hermes.agent_loop import _normalize_msys_path

    value = "/d/my-hermes/workspace/data/a.txt"

    assert _normalize_msys_path(value, "/srv/workspace") == value


def test_sanitize_external_unc_path():
    from hermes.agent_loop import _sanitize_error_message

    result = _sanitize_error_message(
        r"PermissionError: \\server\share\private\report.txt token=my-token",
        max_len=300,
    )

    assert r"\\server\share" not in result
    assert "<external_path>/report.txt" in result
    assert "my-token" not in result


def test_sanitize_unc_path_inside_unc_workspace_keeps_relative_path():
    from hermes.agent_loop import _sanitize_error_message

    result = _sanitize_error_message(
        r"RuntimeError: \\server\share\project\data\report.txt token=my-token",
        max_len=300,
        workspace_root=r"\\server\share\project",
    )

    assert r"\\server\share" not in result
    assert "data/report.txt" in result
    assert "<secret>" in result


@pytest.mark.skipif(os.name != "nt", reason="完整 Windows Path.resolve 行为只在 Windows 验证")
def test_sanitize_windows_workspace_path_keeps_relative_path(tmp_path):
    from hermes.agent_loop import _sanitize_error_message

    workspace = tmp_path / "workspace"
    target = workspace / "data" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    result = _sanitize_error_message(
        f'FileNotFoundError: "{target}"',
        workspace_root=str(workspace),
    )

    assert "data/a.txt" in result
    assert str(workspace) not in result


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("error: FORBIDDEN", "forbidden"),
        ("permission_denied by backend", "permission_denied"),
        ("PATH_ESCAPE detected", "path_escape"),
        ("request safety_blocked", "safety_blocked"),
        ("persistence_error occurred", "persistence_error"),
        ("operation CANCELLED", "cancelled"),
        ("plain error", None),
        ("", None),
    ],
)
def test_detect_fatal_marker(text, expected):
    from hermes.agent_loop import _detect_fatal_marker

    assert _detect_fatal_marker(text) == expected


# ============================================================================
# 3. assistant message 和 dispatch_tool_call helper
# ============================================================================

def test_build_assistant_msg_dict_without_tool_calls():
    from hermes.agent_loop import build_assistant_msg_dict

    result = build_assistant_msg_dict(FakeMessage(content="hello"))

    assert result == {"role": "assistant", "content": "hello"}


def test_build_assistant_msg_dict_with_tool_calls():
    from hermes.agent_loop import build_assistant_msg_dict

    message = FakeMessage(
        content="calling",
        tool_calls=[
            FakeToolCall(
                "file",
                '{"path":"a.txt"}',
                call_id="call-x",
            )
        ],
    )

    result = build_assistant_msg_dict(message)

    assert result["role"] == "assistant"
    assert result["content"] == "calling"
    assert result["tool_calls"] == [
        {
            "id": "call-x",
            "type": "function",
            "function": {
                "name": "file",
                "arguments": '{"path":"a.txt"}',
            },
        }
    ]


def test_dispatch_tool_call_blocks_tool_before_registry_execution():
    from hermes.agent_loop import dispatch_tool_call

    registry = RecordingRegistry()
    call = FakeToolCall("terminal", '{"command":"rm -rf /"}')

    output, status, detail = dispatch_tool_call(
        call,
        registry,
        session_key="s1",
        blocked_tools={"terminal"},
    )

    assert status == "blocked"
    assert "blocked" in output
    assert "terminal" in detail
    assert registry.calls == []


def test_dispatch_tool_call_invalid_json_returns_recoverable_tool_message():
    from hermes.agent_loop import dispatch_tool_call

    output, status, detail = dispatch_tool_call(
        FakeToolCall("file", "{bad json"),
        RecordingRegistry(),
    )

    assert status == "json"
    assert output.startswith("(error: invalid JSON arguments")
    assert "invalid JSON" in detail
    assert "Traceback" not in output


def test_dispatch_tool_call_registry_exception_is_sanitized():
    from hermes.agent_loop import dispatch_tool_call

    registry = RecordingRegistry(
        [
            RuntimeError(
                "failed /home/alice/.env api_key=sk-abcdefghijk"
            )
        ]
    )

    output, status, detail = dispatch_tool_call(
        FakeToolCall("file", '{"path":"a.txt"}'),
        registry,
        session_key="s1",
    )

    assert status == "dispatch"
    assert "sk-abcdefghijk" not in output
    assert "/home/alice/.env" not in output
    assert "<secret>" in output
    assert "<sensitive_path>" in output
    assert "Traceback" not in detail


def test_dispatch_tool_call_success_forwards_args_and_session_key():
    from hermes.agent_loop import dispatch_tool_call

    registry = RecordingRegistry(["result"])

    result = dispatch_tool_call(
        FakeToolCall("file", '{"path":"a.txt"}'),
        registry,
        session_key="session-x",
    )

    assert result == ("result", None, None)
    assert registry.calls == [
        ("file", {"path": "a.txt"}, "session-x")
    ]


# ============================================================================
# 4. AgentLoop 基础结果、取消、内部异常、max_iterations
# ============================================================================

def test_agent_loop_normal_completion_and_model_kwargs_forwarding():
    loop = make_base_loop(
        outcomes=[text_response("completed")],
        tools=[],
        model_kwargs={"temperature": 0.2, "extra_body": {"x": 1}},
    )

    result = loop.run("hello")
    request = loop.client.chat.completions.requests[0]

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "completed"
    assert result.error is None
    assert result.error_type is None
    assert request["model"] == "fake-model"
    assert request["tools"] is None
    assert request["temperature"] == 0.2
    assert request["extra_body"] == {"x": 1}


def test_agent_loop_default_model_exception_becomes_internal_error():
    loop = make_base_loop(
        outcomes=[RuntimeError("unexpected sdk bug")],
        tools=[],
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "error"
    assert result.error_type == "internal_error"
    assert result.fatal is True
    assert result.retryable is False


def test_agent_loop_cancelled_before_model_call():
    loop = make_base_loop(
        outcomes=[text_response("must not run")],
        tools=[],
        cancel_checker=lambda: True,
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "cancelled"
    assert result.error_type == "cancelled"
    assert result.fatal is True
    assert result.retryable is False
    assert loop.client.chat.completions.calls == 0


def test_agent_loop_cancelled_after_model_call_discards_response():
    checks = iter((False, False, True))
    loop = make_base_loop(
        outcomes=[text_response("stale response")],
        tools=[],
        cancel_checker=lambda: next(checks),
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "cancelled"
    assert result.error_type == "cancelled"
    assert result.summary == ""
    assert loop.client.chat.completions.calls == 1


def test_gateway_runner_discards_cancelled_response_before_reply(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from hermes.gateway.runner import GatewayRunner
    from hermes.gateway.types import MessageEvent, SessionSource

    runner = GatewayRunner(
        config={"gateway": {"agent_name": "main"}},
        db_path=str(tmp_path / "gateway.db"),
    )
    ctx = runner.sessions.get_or_create("route-1", "system")
    ctx.cancel_requested = True
    ctx.busy = True
    event = MessageEvent(
        message_id="m1",
        text="old task",
        source=SessionSource(platform="feishu", chat_id="chat-1"),
    )
    runner._run_agent = AsyncMock(return_value="stale response")
    runner._reply = AsyncMock()

    asyncio.run(runner._process("route-1", event))

    runner._reply.assert_not_awaited()
    assert ctx.busy is False


def test_gateway_new_waits_for_worker_and_preserves_following_messages(tmp_path):
    import asyncio

    from hermes.gateway.runner import GatewayRunner
    from hermes.gateway.types import MessageEvent, SessionSource, build_session_key

    async def scenario():
        runner = GatewayRunner(
            config={
                "gateway": {
                    "agent_name": "main",
                    "max_pending_messages": 3,
                },
            },
            db_path=str(tmp_path / "gateway.db"),
        )
        source = SessionSource(
            platform="feishu",
            account_id="app-1",
            chat_id="chat-1",
            user_id="user-1",
        )

        def event(message_id, text):
            return MessageEvent(
                message_id=message_id,
                text=text,
                source=source,
            )

        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        replies = []
        active = 0
        max_active = 0

        async def run_agent(message, ctx):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            calls.append((message.text, ctx.conversation_id))
            try:
                if message.text == "old task":
                    started.set()
                    await release.wait()
                return f"reply:{message.text}"
            finally:
                active -= 1

        async def reply(message, content):
            replies.append((message.text, content))

        runner._run_agent = run_agent
        runner._reply = reply

        await runner._handle_message(event("m1", "old task"))
        await started.wait()
        route_key = build_session_key(source, "main")
        ctx = runner.sessions.get_or_create(route_key, "system")
        old_conversation_id = ctx.conversation_id

        await runner._handle_message(event("m2", "/new"))
        await runner._handle_message(event("m3", "new task"))

        assert ctx.conversation_id == old_conversation_id
        assert [item.text for item in ctx.pending] == ["/new", "new task"]
        release.set()

        for _ in range(100):
            if not ctx.busy and not ctx.pending and len(calls) == 2:
                break
            await asyncio.sleep(0.01)

        assert [item[0] for item in calls] == ["old task", "new task"]
        assert calls[0][1] == old_conversation_id
        assert calls[1][1] != old_conversation_id
        assert max_active == 1
        assert ("old task", "reply:old task") not in replies
        assert ("/new", "(new conversation started)") in replies
        assert ("new task", "reply:new task") in replies

    asyncio.run(scenario())


def test_gateway_pending_queue_rejects_messages_over_limit(tmp_path):
    import asyncio

    from hermes.db import get_gateway_queued_messages, init_db
    from hermes.gateway.runner import GatewayRunner
    from hermes.gateway.types import MessageEvent, SessionSource, build_session_key

    async def scenario():
        runner = GatewayRunner(
            config={
                "gateway": {
                    "agent_name": "main",
                    "max_pending_messages": 2,
                },
            },
            db_path=str(tmp_path / "gateway.db"),
        )
        source = SessionSource(
            platform="feishu",
            account_id="app-1",
            chat_id="chat-1",
            user_id="user-1",
        )

        def event(message_id, text):
            return MessageEvent(
                message_id=message_id,
                text=text,
                source=source,
            )

        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        replies = []

        async def run_agent(message, _ctx):
            calls.append(message.text)
            if message.text == "active":
                started.set()
                await release.wait()
            return f"reply:{message.text}"

        async def reply(message, content):
            replies.append((message.text, content))

        runner._run_agent = run_agent
        runner._reply = reply

        await runner._handle_message(event("m1", "active"))
        await started.wait()
        await runner._handle_message(event("m2", "pending-1"))
        await runner._handle_message(event("m3", "pending-2"))
        await runner._handle_message(event("m4", "rejected"))

        route_key = build_session_key(source, "main")
        ctx = runner.sessions.get_or_create(route_key, "system")
        assert len(ctx.pending) == 2
        assert runner.sessions.get_status(route_key)["pending_limit"] == 2
        assert (
            "rejected",
            "(queue full: please wait for pending messages)",
        ) in replies
        conn = init_db(runner.db_path)
        try:
            rows = get_gateway_queued_messages(conn)
        finally:
            conn.close()
        assert [row["message_id"] for row in rows] == ["m1", "m2", "m3"]
        assert [row["status"] for row in rows] == [
            "processing",
            "queued",
            "queued",
        ]

        release.set()
        for _ in range(100):
            if not ctx.busy and not ctx.pending and len(calls) == 3:
                break
            await asyncio.sleep(0.01)

        assert calls == ["active", "pending-1", "pending-2"]
        conn = init_db(runner.db_path)
        try:
            assert get_gateway_queued_messages(conn) == []
        finally:
            conn.close()

    asyncio.run(scenario())


def test_gateway_restores_processing_and_pending_messages(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from hermes.db import (
        enqueue_gateway_message,
        get_gateway_queued_messages,
        init_db,
        mark_gateway_message_processing,
    )
    from hermes.gateway.runner import GatewayRunner
    from hermes.gateway.types import (
        MessageEvent,
        SessionSource,
        build_session_key,
    )

    async def scenario():
        runner = GatewayRunner(
            config={"gateway": {"agent_name": "main"}},
            db_path=str(tmp_path / "gateway.db"),
        )
        source = SessionSource(
            platform="feishu",
            account_id="app-1",
            chat_id="chat-1",
            user_id="user-1",
        )
        events = [
            MessageEvent(message_id="m1", text="first", source=source),
            MessageEvent(message_id="m2", text="second", source=source),
        ]
        route_key = build_session_key(source, "main")
        conn = init_db(runner.db_path)
        try:
            for event in events:
                enqueue_gateway_message(
                    conn,
                    route_key,
                    event.message_id,
                    runner._serialize_event(event),
                )
            mark_gateway_message_processing(conn, route_key, "m1")
        finally:
            conn.close()

        calls = []

        async def run_agent(event, _ctx):
            calls.append(event.text)
            return f"reply:{event.text}"

        runner._run_agent = run_agent
        runner._reply = AsyncMock()
        await runner._restore_queued_messages()

        for _ in range(100):
            if not runner._accepted_messages and len(calls) == 2:
                break
            await asyncio.sleep(0.01)

        assert calls == ["first", "second"]
        conn = init_db(runner.db_path)
        try:
            assert get_gateway_queued_messages(conn) == []
        finally:
            conn.close()

    asyncio.run(scenario())


def test_gateway_limits_global_llm_concurrency(tmp_path):
    import asyncio

    from hermes.gateway.runner import GatewayRunner

    async def scenario():
        runner = GatewayRunner(
            config={
                "gateway": {
                    "agent_name": "main",
                    "max_concurrent_llm_requests": 2,
                },
            },
            db_path=str(tmp_path / "gateway.db"),
        )
        active = 0
        max_active = 0

        async def run_async(event, _ctx):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.05)
                return event.text
            finally:
                active -= 1

        runner._run_agent_async = run_async
        events = [SimpleNamespace(text=f"message-{i}") for i in range(4)]
        results = await asyncio.gather(*[
            runner._run_agent(event, SimpleNamespace())
            for event in events
        ])

        assert results == [f"message-{i}" for i in range(4)]
        assert max_active == 2

    asyncio.run(scenario())


def test_run_conversation_async_cancels_model_and_skips_assistant(tmp_path):
    import asyncio

    from hermes.conversation import run_conversation_async
    from hermes.db import create_session, get_session_messages, init_db

    async def scenario():
        conn = init_db(str(tmp_path / "conversation.db"))
        try:
            session_id = create_session(conn)
            fake_client = BlockingAsyncClient()
            task = asyncio.create_task(
                run_conversation_async(
                    "slow request",
                    conn,
                    session_id,
                    "system",
                    async_client=fake_client,
                )
            )
            await asyncio.wait_for(
                fake_client.completions.started.wait(), timeout=1,
            )

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert fake_client.completions.cancelled.is_set()
            assert [
                message["role"]
                for message in get_session_messages(conn, session_id)
            ] == ["user"]
        finally:
            conn.close()

    asyncio.run(scenario())


def test_compress_async_propagates_model_cancellation(monkeypatch):
    import asyncio

    import hermes.tokens as tokens

    async def scenario():
        monkeypatch.setattr(tokens, "PROTECT_FIRST", 0)
        monkeypatch.setattr(tokens, "TAIL_TOKEN_BUDGET", 0)
        fake_client = BlockingAsyncClient()
        task = asyncio.create_task(
            tokens.compress_async(
                [{"role": "user", "content": "long context"}],
                fake_client,
                "fake-model",
            )
        )
        await asyncio.wait_for(
            fake_client.completions.started.wait(), timeout=1,
        )

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake_client.completions.cancelled.is_set()

    asyncio.run(scenario())


def test_run_conversation_async_keeps_sync_result_format(tmp_path):
    import asyncio

    from hermes.conversation import run_conversation_async
    from hermes.db import create_session, get_session_messages, init_db

    async def scenario():
        conn = init_db(str(tmp_path / "conversation.db"))
        try:
            session_id = create_session(conn)
            fake_client = FakeAsyncClient([text_response("async reply")])
            result = await run_conversation_async(
                "hello",
                conn,
                session_id,
                "system",
                async_client=fake_client,
            )

            assert result["ok"] is True
            assert result["status"] == "completed"
            assert result["final_response"] == "async reply"
            assert [
                message["role"]
                for message in get_session_messages(conn, session_id)
            ] == ["user", "assistant"]
            # 外部注入的共享客户端由 Runner 管理,会话入口不能擅自关闭。
            assert fake_client.closed is False
        finally:
            conn.close()

    asyncio.run(scenario())


def test_run_conversation_async_uses_and_closes_fallback(
    tmp_path,
    monkeypatch,
):
    import asyncio

    import hermes.conversation as conversation
    from hermes.db import create_session, init_db

    async def scenario():
        conn = init_db(str(tmp_path / "conversation.db"))
        try:
            session_id = create_session(conn)
            primary = FakeAsyncClient([
                FakeAPIError("unauthorized", status_code=401),
            ])
            fallback = FakeAsyncClient([text_response("fallback reply")])
            monkeypatch.setattr(
                conversation,
                "switch_to_async_fallback",
                lambda: (fallback, "fallback-model"),
            )

            result = await conversation.run_conversation_async(
                "hello",
                conn,
                session_id,
                "system",
                async_client=primary,
            )

            assert result["status"] == "completed"
            assert result["final_response"] == "fallback reply"
            assert primary.closed is False
            assert fallback.closed is True
        finally:
            conn.close()

    asyncio.run(scenario())


def test_gateway_stop_cancels_model_task_and_completes_queue(tmp_path):
    import asyncio

    from hermes.db import get_gateway_queued_messages, init_db
    from hermes.gateway.runner import GatewayRunner
    from hermes.gateway.types import MessageEvent, SessionSource, build_session_key

    async def scenario():
        runner = GatewayRunner(
            config={"gateway": {"agent_name": "main"}},
            db_path=str(tmp_path / "gateway.db"),
        )
        fake_client = BlockingAsyncClient()
        runner._async_client = fake_client
        replies = []

        async def reply(event, content):
            replies.append((event.text, content))

        runner._reply = reply
        source = SessionSource(
            platform="feishu",
            account_id="app-1",
            chat_id="chat-1",
            user_id="user-1",
        )
        message = MessageEvent(
            message_id="m1", text="slow request", source=source,
        )
        stop = MessageEvent(
            message_id="m2", text="/stop", source=source,
        )

        await runner._handle_message(message)
        await asyncio.wait_for(
            fake_client.completions.started.wait(), timeout=1,
        )
        await runner._handle_message(stop)

        route_key = build_session_key(source, "main")
        ctx = runner.sessions.get_or_create(route_key, "system")
        for _ in range(100):
            if not ctx.busy:
                break
            await asyncio.sleep(0.01)

        assert fake_client.completions.cancelled.is_set()
        assert ("/stop", "(cancel requested)") in replies
        assert not any(text == "slow request" for text, _ in replies)
        conn = init_db(runner.db_path)
        try:
            assert get_gateway_queued_messages(conn) == []
        finally:
            conn.close()

    asyncio.run(scenario())


def test_gateway_shutdown_keeps_cancelled_message_for_recovery(tmp_path):
    import asyncio

    from hermes.db import get_gateway_queued_messages, init_db
    from hermes.gateway.runner import GatewayRunner
    from hermes.gateway.types import MessageEvent, SessionSource

    async def scenario():
        runner = GatewayRunner(
            config={"gateway": {"agent_name": "main"}},
            db_path=str(tmp_path / "gateway.db"),
        )
        fake_client = BlockingAsyncClient()
        runner._async_client = fake_client
        source = SessionSource(
            platform="feishu",
            account_id="app-1",
            chat_id="chat-1",
            user_id="user-1",
        )
        message = MessageEvent(
            message_id="m1", text="slow request", source=source,
        )

        await runner._handle_message(message)
        await asyncio.wait_for(
            fake_client.completions.started.wait(), timeout=1,
        )
        await runner.stop()

        assert fake_client.completions.cancelled.is_set()
        conn = init_db(runner.db_path)
        try:
            rows = get_gateway_queued_messages(conn)
        finally:
            conn.close()
        assert [row["message_id"] for row in rows] == ["m1"]
        assert rows[0]["status"] == "processing"

    asyncio.run(scenario())


def test_agent_loop_reaches_max_iterations_when_model_never_finishes():
    responses = [
        tool_response("file", "{}", call_id=f"call-{i}")
        for i in range(3)
    ]
    loop = make_base_loop(
        outcomes=responses,
        registry=RecordingRegistry(["ok", "ok", "ok"]),
        max_iterations=3,
    )

    result = loop.run("keep using tools")

    assert result.ok is False
    assert result.status == "max_iterations"
    assert result.iterations == 3
    assert result.tools_used == ["file"]


def test_malformed_model_response_becomes_internal_error():
    loop = make_base_loop(
        outcomes=[SimpleNamespace(no_choices=True)],
        tools=[],
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "error"
    assert result.error_type == "internal_error"


# ============================================================================
# 5. 工具错误分类
# ============================================================================

@pytest.mark.parametrize(
    "output",
    [
        "normal terminal output mentions permission_denied",
        json.dumps({
            "ok": True,
            "content": "forbidden cancelled path_escape",
        }),
    ],
)
def test_fatal_words_in_successful_output_are_not_misclassified(output):
    from hermes.agent_loop import AgentLoop

    loop = make_base_loop(outcomes=[text_response()], tools=[])

    assert loop._classify_tool_error(output, None) == (False, "")


@pytest.mark.parametrize(
    ("output", "err_status", "expected_type"),
    [
        ("(error: forbidden)", "dispatch", "forbidden"),
        ("(error: permission_denied)", "dispatch", "permission_denied"),
        ("(error: path_escape)", "json", "path_escape"),
        ("(error: safety_blocked)", None, "safety_blocked"),
        ("(error: persistence_error)", None, "persistence_error"),
        ("(error: cancelled)", None, "cancelled"),
    ],
)
def test_confirmed_error_with_fatal_marker_is_fatal(
    output,
    err_status,
    expected_type,
):
    loop = make_base_loop(outcomes=[text_response()], tools=[])

    assert loop._classify_tool_error(output, err_status) == (
        True,
        expected_type,
    )


def test_blocked_status_is_immediately_fatal():
    loop = make_base_loop(outcomes=[text_response()], tools=[])

    assert loop._classify_tool_error(
        "(error: blocked)",
        "blocked",
    ) == (True, "blocked")


@pytest.mark.parametrize("status", ["json", "dispatch"])
def test_plain_json_or_dispatch_status_is_recoverable(status):
    loop = make_base_loop(outcomes=[text_response()], tools=[])

    assert loop._classify_tool_error(
        "(error: ordinary tool failure)",
        status,
    ) == (False, status)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"ok": False, "error_type": "file_not_found", "error": "missing"},
            (False, "file_not_found"),
        ),
        (
            {"ok": False, "error_type": "permission_denied", "error": "denied"},
            (True, "permission_denied"),
        ),
        (
            {"ok": False, "error_type": "custom", "fatal": True},
            (True, "custom"),
        ),
        (
            {"ok": False, "fatal": True},
            (True, "fatal_flagged"),
        ),
        (
            {"error": "Unknown tool: abc"},
            (False, "unknown_error"),
        ),
        (
            {"ok": False, "message": "failed"},
            (False, "unknown_error"),
        ),
        (
            {"ok": True, "content": "success"},
            (False, ""),
        ),
    ],
)
def test_structured_tool_error_classification(payload, expected):
    loop = make_base_loop(outcomes=[text_response()], tools=[])

    assert loop._classify_tool_error(
        json.dumps(payload),
        None,
    ) == expected


def test_terminal_user_denied_is_structured_but_recoverable(monkeypatch):
    from hermes.agent_loop import AgentLoop
    import hermes.tools.terminal as terminal

    monkeypatch.setattr(
        terminal,
        "detect_dangerous_command",
        lambda command: [(0, "danger", "danger")],
    )
    monkeypatch.setattr(
        terminal,
        "approve_command",
        lambda command, matches: False,
    )

    output = terminal.run_terminal({"command": "dangerous command"})
    payload = json.loads(output)

    assert payload == {
        "ok": False,
        "error_type": "user_denied",
        "error": "Command denied by user.",
    }

    loop = make_base_loop(outcomes=[text_response()], tools=[])
    assert loop._classify_tool_error(output, None) == (
        False,
        "user_denied",
    )


# ============================================================================
# 6. 工具错误进入 loop 后的恢复、升级和计数清理
# ============================================================================

def test_recoverable_tool_error_is_returned_to_model_and_loop_continues():
    from hermes.agent_loop import AgentLoop

    class RecoverableLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                json.dumps({
                    "ok": False,
                    "error_type": "file_not_found",
                    "error": "missing.txt",
                }),
                None,
                None,
            )

    client = FakeClient(
        [
            tool_response("file", '{"path":"missing.txt"}'),
            text_response("recovered"),
        ]
    )
    loop = RecoverableLoop(
        model="fake",
        max_iterations=5,
        tools=[],
        system_prompt="system",
        registry=RecordingRegistry(),
        client=client,
    )

    result = loop.run("read file")

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "recovered"
    assert client.chat.completions.calls == 2

    tool_messages = [
        message
        for message in result.messages
        if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    assert "file_not_found" in tool_messages[0]["content"]


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
def test_fatal_dispatch_marker_stops_before_next_model_call(marker):
    from hermes.agent_loop import AgentLoop

    class FatalLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                f"(error: {marker}: denied)",
                "dispatch",
                f"{marker}: denied",
            )

    client = FakeClient(
        [
            tool_response("file"),
            text_response("must not be reached"),
        ]
    )
    loop = FatalLoop(
        model="fake",
        max_iterations=5,
        tools=[],
        system_prompt="system",
        registry=RecordingRegistry(),
        client=client,
    )

    result = loop.run("do it")

    assert result.ok is False
    assert result.status == "tool_error"
    assert result.error_type == marker
    assert result.fatal is True
    assert result.retryable is False
    assert client.chat.completions.calls == 1


def test_repeated_same_tool_error_escalates_at_limit():
    from hermes.agent_loop import AgentLoop

    responses = [
        tool_response("file", "{}", call_id=f"call-{i}")
        for i in range(10)
    ]

    class AlwaysFailLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                json.dumps({
                    "ok": False,
                    "error_type": "file_not_found",
                    "error": "missing",
                }),
                None,
                None,
            )

    client = FakeClient(responses)
    loop = AlwaysFailLoop(
        model="fake",
        max_iterations=10,
        tools=[],
        system_prompt="system",
        registry=RecordingRegistry(),
        client=client,
    )

    result = loop.run("keep trying")

    assert result.ok is False
    assert result.status == "tool_error"
    assert result.error_type == "file_not_found"
    assert result.iterations == loop.TOOL_ERROR_LIMIT
    assert client.chat.completions.calls == loop.TOOL_ERROR_LIMIT


def test_successful_tool_call_clears_previous_error_count():
    from hermes.agent_loop import AgentLoop

    outputs = iter(
        [
            (
                json.dumps({
                    "ok": False,
                    "error_type": "file_not_found",
                    "error": "missing",
                }),
                None,
                None,
            ),
            (
                json.dumps({
                    "ok": False,
                    "error_type": "file_not_found",
                    "error": "missing",
                }),
                None,
                None,
            ),
            ("ok", None, None),
            (
                json.dumps({
                    "ok": False,
                    "error_type": "file_not_found",
                    "error": "missing",
                }),
                None,
                None,
            ),
            (
                json.dumps({
                    "ok": False,
                    "error_type": "file_not_found",
                    "error": "missing",
                }),
                None,
                None,
            ),
        ]
    )

    class ResetCountLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return next(outputs)

    responses = [
        tool_response("file", "{}", call_id=f"call-{i}")
        for i in range(5)
    ] + [text_response("completed")]

    loop = ResetCountLoop(
        model="fake",
        max_iterations=10,
        tools=[],
        system_prompt="system",
        registry=RecordingRegistry(),
        client=FakeClient(responses),
    )

    result = loop.run("try, recover, try again")

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "completed"


def test_error_counts_are_isolated_by_tool_name():
    from hermes.agent_loop import AgentLoop

    class TwoToolLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            return (
                json.dumps({
                    "ok": False,
                    "error_type": "invalid_args",
                    "error": "bad args",
                }),
                None,
                None,
            )

    responses = [
        tool_response("file", call_id="f1"),
        tool_response("terminal", call_id="t1"),
        tool_response("file", call_id="f2"),
        tool_response("terminal", call_id="t2"),
        text_response("done"),
    ]

    loop = TwoToolLoop(
        model="fake",
        max_iterations=10,
        tools=[],
        system_prompt="system",
        registry=RecordingRegistry(),
        client=FakeClient(responses),
    )

    result = loop.run("use two tools")

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "done"


def test_all_tool_messages_are_generated_even_after_first_fatal_error():
    from hermes.agent_loop import AgentLoop

    calls = [
        FakeToolCall("file", "{}", "call-1"),
        FakeToolCall("terminal", "{}", "call-2"),
    ]

    class MultiToolLoop(AgentLoop):
        def dispatch_one(self, tool_call):
            if tool_call.function.name == "file":
                return (
                    "(error: permission_denied)",
                    "dispatch",
                    "permission_denied",
                )
            return ("second tool still produced a result", None, None)

    loop = MultiToolLoop(
        model="fake",
        max_iterations=1,
        tools=[],
        system_prompt="system",
        registry=RecordingRegistry(),
        client=FakeClient([]),
    )
    messages: list[dict] = []

    tool_messages, error = loop.process_tool_calls(calls, messages)

    assert error is not None
    assert error.status == "tool_error"
    assert error.error_type == "permission_denied"
    assert len(tool_messages) == 2
    assert [m["tool_call_id"] for m in tool_messages] == [
        "call-1",
        "call-2",
    ]


def test_real_tool_registry_unknown_tool_is_recoverable():
    from hermes.agent_loop import AgentLoop
    from hermes.tools import ToolRegistry

    registry = ToolRegistry()
    loop = AgentLoop(
        model="fake",
        max_iterations=3,
        tools=[],
        system_prompt="system",
        registry=registry,
        client=FakeClient(
            [
                tool_response("does_not_exist"),
                text_response("used another approach"),
            ]
        ),
    )

    result = loop.run("call missing tool")

    assert result.ok is True
    tool_message = next(
        m for m in result.messages if m.get("role") == "tool"
    )
    payload = json.loads(tool_message["content"])
    assert "Unknown tool" in payload["error"]


# ============================================================================
# 7. ConversationAgentLoop：retry、compression、fallback
# ============================================================================

@pytest.fixture
def no_wait(monkeypatch):
    import hermes.conversation as conversation

    monkeypatch.setattr(conversation.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        conversation,
        "jittered_backoff",
        lambda attempt: 0,
    )
    monkeypatch.setattr(
        conversation,
        "add_messages",
        lambda conn, session_id, messages: None,
    )


@pytest.mark.parametrize(
    ("error", "max_retries"),
    [
        (FakeAPIError("rate limit", 429), 1),
        (FakeAPIError("server error", 500), 1),
        (FakeAPIError("Connection timed out", None), 1),
    ],
)
def test_retryable_model_error_retries_then_succeeds(
    monkeypatch,
    no_wait,
    error,
    max_retries,
):
    import hermes.conversation as conversation

    fallback_calls = {"count": 0}

    def no_fallback():
        fallback_calls["count"] += 1
        return None, None

    monkeypatch.setattr(
        conversation,
        "switch_to_fallback",
        no_fallback,
    )

    loop = make_conversation_loop(
        primary_outcomes=[error, text_response("ok")],
        max_retries=max_retries,
    )

    result = loop.run("hello")

    assert result.ok is True
    assert result.status == "completed"
    assert result.summary == "ok"
    assert loop.client.chat.completions.calls == 2
    assert fallback_calls["count"] == 0


def test_retry_exhaustion_switches_to_fallback_and_fallback_succeeds(
    monkeypatch,
    no_wait,
):
    import hermes.conversation as conversation

    fallback_client = FakeClient([text_response("fallback ok")])
    fallback_calls = {"count": 0}

    def switch():
        fallback_calls["count"] += 1
        return fallback_client, "fallback-model"

    monkeypatch.setattr(conversation, "switch_to_fallback", switch)

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError("primary 500", 500),
            FakeAPIError("primary 500 again", 500),
        ],
        max_retries=1,
    )

    result = loop.run("hello")

    assert result.ok is True
    assert result.summary == "fallback ok"
    assert fallback_calls["count"] == 1
    assert loop.model == "fallback-model"
    assert loop._using_fallback is True


def test_fallback_is_used_only_once_then_model_error_is_returned(
    monkeypatch,
    no_wait,
):
    import hermes.conversation as conversation

    fallback_client = FakeClient(
        [
            FakeAPIError("fallback 500", 500),
            FakeAPIError("fallback 500 again", 500),
        ]
    )
    fallback_calls = {"count": 0}

    def switch():
        fallback_calls["count"] += 1
        return fallback_client, "fallback-model"

    monkeypatch.setattr(conversation, "switch_to_fallback", switch)

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError("primary 500", 500),
            FakeAPIError("primary 500 again", 500),
        ],
        max_retries=1,
        max_iterations=20,
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "model_error"
    assert result.error_type == "model_error"
    assert result.fatal is True
    assert result.retryable is True
    assert fallback_calls["count"] == 1
    assert result.iterations == 4


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_auth_or_model_not_found_goes_directly_to_fallback(
    monkeypatch,
    no_wait,
    status_code,
):
    import hermes.conversation as conversation

    fallback_client = FakeClient([text_response("fallback response")])
    sleep_calls = {"count": 0}

    monkeypatch.setattr(
        conversation.time,
        "sleep",
        lambda delay: sleep_calls.__setitem__(
            "count",
            sleep_calls["count"] + 1,
        ),
    )
    monkeypatch.setattr(
        conversation,
        "switch_to_fallback",
        lambda: (fallback_client, "fallback-model"),
    )

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError("auth/model error", status_code),
        ],
        max_retries=3,
    )

    result = loop.run("hello")

    assert result.ok is True
    assert result.summary == "fallback response"
    assert sleep_calls["count"] == 0
    assert loop.client is fallback_client


def test_unknown_model_error_tries_fallback_immediately(
    monkeypatch,
    no_wait,
):
    import hermes.conversation as conversation

    fallback_client = FakeClient([text_response("fallback")])
    monkeypatch.setattr(
        conversation,
        "switch_to_fallback",
        lambda: (fallback_client, "fallback-model"),
    )

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError("unknown provider failure", 418),
        ],
        max_retries=5,
    )

    result = loop.run("hello")

    assert result.ok is True
    assert result.summary == "fallback"
    assert loop._retry_count == 0


def test_no_fallback_returns_structured_model_error(
    monkeypatch,
    no_wait,
):
    import hermes.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "switch_to_fallback",
        lambda: (None, None),
    )

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError("unknown provider failure", 418),
        ],
        max_retries=3,
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "model_error"
    assert result.error_type == "model_error"
    assert result.fatal is True
    assert result.retryable is True
    assert result.iterations == 1


def test_context_overflow_compresses_messages_then_retries(
    monkeypatch,
    no_wait,
):
    import hermes.conversation as conversation

    compress_calls: list[list[dict]] = []

    def fake_compress(messages):
        compress_calls.append(list(messages))
        return [{"role": "user", "content": "compressed"}]

    monkeypatch.setattr(conversation, "compress", fake_compress)
    monkeypatch.setattr(
        conversation,
        "switch_to_fallback",
        lambda: (None, None),
    )

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError("context length exceeded", 400),
            text_response("after compression"),
        ],
        max_retries=0,
    )

    result = loop.run("large input")

    assert result.ok is True
    assert result.summary == "after compression"
    assert len(compress_calls) == 1
    assert any(
        message.get("content") == "compressed"
        for message in result.messages
    )


def test_successful_assistant_message_resets_retry_count(monkeypatch):
    import hermes.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "add_messages",
        lambda conn, session_id, messages: None,
    )
    loop = make_conversation_loop(
        primary_outcomes=[text_response("ok")],
    )
    loop._retry_count = 2

    result = loop.run("hello")

    assert result.ok is True
    assert loop._retry_count == 0


# ============================================================================
# 8. DB 持久化错误和 assistant/tool 原子写入
# ============================================================================

def test_run_conversation_wraps_initial_db_read_error(monkeypatch):
    import hermes.conversation as conversation

    def fail_read(conn, session_id):
        raise sqlite3.OperationalError(
            "database locked /home/alice/.env api_key=sk-abcdefghijk"
        )

    monkeypatch.setattr(
        conversation,
        "get_session_messages",
        fail_read,
    )

    result = conversation.run_conversation(
        "hello",
        object(),
        "session-1",
        "system",
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["error_type"] == "persistence_error"
    assert result["fatal"] is True
    assert result["retryable"] is False

    text = str(result)
    assert "sk-abcdefghijk" not in text
    assert "/home/alice/.env" not in text
    assert "Traceback" not in text


def test_run_conversation_wraps_initial_user_message_write_error(monkeypatch):
    import hermes.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "get_session_messages",
        lambda conn, session_id: [],
    )

    def fail_write(conn, session_id, messages):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        conversation,
        "add_messages",
        fail_write,
    )

    result = conversation.run_conversation(
        "hello",
        object(),
        "session-1",
        "system",
    )

    assert result["ok"] is False
    assert result["error_type"] == "persistence_error"
    assert result["messages"] == []


def test_normal_assistant_persistence_failure_stops_loop(monkeypatch):
    import hermes.conversation as conversation

    def fail_write(conn, session_id, messages):
        raise sqlite3.OperationalError("assistant write failed")

    monkeypatch.setattr(
        conversation,
        "add_messages",
        fail_write,
    )

    loop = make_conversation_loop(
        primary_outcomes=[text_response("answer")],
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.status == "error"
    assert result.error_type == "persistence_error"
    assert result.fatal is True
    assert result.retryable is False


def test_continuation_message_persistence_failure_stops_loop(monkeypatch):
    import hermes.conversation as conversation

    calls = {"count": 0}

    def add_messages(conn, session_id, messages):
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError(
                "continuation write failed"
            )

    monkeypatch.setattr(
        conversation,
        "add_messages",
        add_messages,
    )

    loop = make_conversation_loop(
        primary_outcomes=[
            text_response("partial", finish_reason="length"),
            text_response("must not be reached"),
        ],
        max_continuations=1,
    )

    result = loop.run("hello")

    assert result.ok is False
    assert result.error_type == "persistence_error"
    assert calls["count"] == 2
    assert loop.client.chat.completions.calls == 1


def test_tool_batch_persistence_failure_stops_before_next_model_call(
    monkeypatch,
):
    import hermes.conversation as conversation

    def fail_write(conn, session_id, messages):
        raise sqlite3.OperationalError("batch write failed")

    monkeypatch.setattr(
        conversation,
        "add_messages",
        fail_write,
    )

    registry = RecordingRegistry(["tool output"])
    loop = make_conversation_loop(
        primary_outcomes=[
            tool_response("file", '{"path":"a.txt"}'),
            text_response("must not be reached"),
        ],
        registry=registry,
    )
    loop.tools = [
        {
            "type": "function",
            "function": {
                "name": "file",
                "parameters": {},
            },
        }
    ]

    result = loop.run("read")

    assert result.ok is False
    assert result.error_type == "persistence_error"
    assert loop.client.chat.completions.calls == 1


def test_assistant_tool_call_and_results_are_persisted_in_one_batch(
    monkeypatch,
):
    import hermes.conversation as conversation

    persisted: list[list[dict]] = []

    def record(conn, session_id, messages):
        persisted.append(list(messages))

    monkeypatch.setattr(conversation, "add_messages", record)

    registry = RecordingRegistry(["tool output"])
    loop = make_conversation_loop(
        primary_outcomes=[
            tool_response("file", '{"path":"a.txt"}'),
            text_response("done"),
        ],
        registry=registry,
    )
    loop.tools = [
        {
            "type": "function",
            "function": {
                "name": "file",
                "parameters": {},
            },
        }
    ]

    result = loop.run("read")

    assert result.ok is True
    assert len(persisted) == 2

    batch = persisted[0]
    assert [m["role"] for m in batch] == [
        "assistant",
        "tool",
    ]
    assert batch[0]["tool_calls"][0]["id"] == batch[1]["tool_call_id"]

    assert persisted[1] == [
        {"role": "assistant", "content": "done"}
    ]


# ============================================================================
# 9. run_conversation 对 AgentLoopResult 的对外映射
# ============================================================================

@pytest.mark.parametrize(
    (
        "loop_result",
        "expected_final",
    ),
    [
        (
            {
                "ok": True,
                "status": "completed",
                "summary": "answer",
                "error": None,
                "error_type": None,
                "fatal": False,
                "retryable": True,
            },
            "answer",
        ),
        (
            {
                "ok": False,
                "status": "max_iterations",
                "summary": "",
                "error": None,
                "error_type": None,
                "fatal": False,
                "retryable": True,
            },
            "(max iterations reached)",
        ),
        (
            {
                "ok": False,
                "status": "cancelled",
                "summary": "",
                "error": "cancel requested",
                "error_type": "cancelled",
                "fatal": True,
                "retryable": False,
            },
            "(cancelled)",
        ),
        (
            {
                "ok": False,
                "status": "model_error",
                "summary": "",
                "error": "provider failed",
                "error_type": "model_error",
                "fatal": True,
                "retryable": True,
            },
            "(agent error: model_error;",
        ),
        (
            {
                "ok": False,
                "status": "tool_error",
                "summary": "",
                "error": "permission denied",
                "error_type": "permission_denied",
                "fatal": True,
                "retryable": False,
            },
            "(agent error: tool_error;",
        ),
        (
            {
                "ok": False,
                "status": "error",
                "summary": "",
                "error": "db failed",
                "error_type": "persistence_error",
                "fatal": True,
                "retryable": False,
            },
            "(agent error: persistence_error;",
        ),
    ],
)
def test_run_conversation_result_mapping(
    monkeypatch,
    loop_result,
    expected_final,
):
    import hermes.conversation as conversation
    from hermes.agent_loop import AgentLoopResult

    result_object = AgentLoopResult(
        messages=[],
        iterations=1,
        tools_used=[],
        **loop_result,
    )

    class StubLoop:
        def __init__(self, **kwargs):
            pass

        def run(self, user_message):
            return result_object

    monkeypatch.setattr(
        conversation,
        "get_session_messages",
        lambda conn, session_id: [],
    )
    monkeypatch.setattr(
        conversation,
        "add_messages",
        lambda conn, session_id, messages: None,
    )
    monkeypatch.setattr(
        conversation.registry,
        "get_definitions",
        lambda enabled: [],
    )
    monkeypatch.setattr(
        conversation,
        "ConversationAgentLoop",
        StubLoop,
    )

    result = conversation.run_conversation(
        "hello",
        object(),
        "session-1",
        "system",
    )

    assert expected_final in result["final_response"]
    assert result["ok"] is loop_result["ok"]
    assert result["status"] == loop_result["status"]
    assert result["error_type"] == loop_result["error_type"]
    assert result["fatal"] is loop_result["fatal"]
    assert result["retryable"] is loop_result["retryable"]


# ============================================================================
# 10. 当前仍存在的脱敏缺口
#
# 这两项按“正确预期”编写，但当前实现中：
# - AgentLoop._model_error_result 接收 repr(exc)
# - AgentLoop._persistence_error_result 接收 repr(exc)
# 尚未统一调用 _sanitize_error_message。
#
# 用 strict xfail 记录：现在运行不阻塞整套测试；修复后 XPASS 会提醒删除 xfail。
# ============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="loop-level model_error detail 尚未统一脱敏",
)
def test_model_error_detail_should_not_leak_secret_or_sensitive_path(
    monkeypatch,
    no_wait,
):
    import hermes.conversation as conversation

    monkeypatch.setattr(
        conversation,
        "switch_to_fallback",
        lambda: (None, None),
    )

    loop = make_conversation_loop(
        primary_outcomes=[
            FakeAPIError(
                "provider failed /home/alice/.env api_key=sk-abcdefghijk",
                418,
            )
        ],
    )

    result = loop.run("hello")
    text = str(result.error)

    assert "sk-abcdefghijk" not in text
    assert "/home/alice/.env" not in text
    assert "Traceback" not in text


@pytest.mark.xfail(
    strict=True,
    reason="loop-level persistence_error detail 尚未统一脱敏",
)
def test_loop_persistence_error_detail_should_not_leak_secret(
    monkeypatch,
):
    import hermes.conversation as conversation

    def fail_write(conn, session_id, messages):
        raise sqlite3.OperationalError(
            "db failed /home/alice/.env token=my-secret-token"
        )

    monkeypatch.setattr(
        conversation,
        "add_messages",
        fail_write,
    )

    loop = make_conversation_loop(
        primary_outcomes=[text_response("answer")],
    )

    result = loop.run("hello")
    text = str(result.error)

    assert "my-secret-token" not in text
    assert "/home/alice/.env" not in text
