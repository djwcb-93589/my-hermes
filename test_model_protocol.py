from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9")

from hermes.agent_loop import (  # noqa: E402
    AgentLoop,
    AsyncAgentLoop,
    build_assistant_msg_dict,
)
from hermes.config import CONTINUE_MESSAGE  # noqa: E402
from hermes.conversation import (  # noqa: E402
    AsyncConversationAgentLoop,
    ConversationAgentLoop,
)
from hermes.persistence.core import (  # noqa: E402
    add_message,
    create_session,
    get_session_messages,
)
from hermes.persistence.schema import init_db  # noqa: E402


_MISSING = object()
_SAFE_EVENT_KEYS = {
    "iteration",
    "model",
    "model_role",
    "outcome",
    "finish_reason",
    "latency_ms",
    "has_content",
    "content_chars",
    "has_reasoning",
    "reasoning_chars",
    "tool_call_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "http_status",
    "error_category",
    "exception_type",
}


def _tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="cron", arguments='{"action":"list"}'),
    )


def _assistant(
    *,
    content: str | None,
    reasoning_content=_MISSING,
    tool_calls=None,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning_content is not _MISSING:
        message.reasoning_content = reasoning_content
    return message


def _response(
    *,
    content: str | None,
    reasoning_content=_MISSING,
    finish_reason: str = "stop",
    tool_calls=None,
) -> SimpleNamespace:
    message = _assistant(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    usage = SimpleNamespace(
        prompt_tokens=11,
        completion_tokens=7,
        total_tokens=18,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
        prompt_tokens_details=SimpleNamespace(cached_tokens=3),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def _snapshot(messages: list[dict]) -> list[dict]:
    return [dict(message) for message in messages]


class _SyncProtocolLoop(AgentLoop):
    def __init__(self, responses: list[object]):
        super().__init__(
            model="primary-model",
            max_iterations=3,
            tools=[],
            system_prompt="test",
            registry=None,
            client=object(),
        )
        self.responses = list(responses)
        self.call_histories: list[list[dict]] = []
        self.error_histories: list[list[dict]] = []
        self.events: list[dict] = []

    def call_model(self, messages: list[dict]):
        self.call_histories.append(_snapshot(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def handle_model_error(self, exc, messages) -> str:
        self.error_histories.append(_snapshot(messages))
        if self.responses:
            self._using_fallback = True
            self.model = "fallback-model"
            return "retry"
        return "abort"

    def on_model_call_event(self, event: dict) -> None:
        self.events.append(dict(event))


class _AsyncProtocolLoop(AsyncAgentLoop):
    def __init__(self, responses: list[object]):
        super().__init__(
            model="primary-model",
            max_iterations=3,
            tools=[],
            system_prompt="test",
            registry=None,
            client=object(),
        )
        self.responses = list(responses)
        self.call_histories: list[list[dict]] = []
        self.error_histories: list[list[dict]] = []
        self.events: list[dict] = []

    async def call_model(self, messages: list[dict]):
        self.call_histories.append(_snapshot(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def handle_model_error(self, exc, messages) -> str:
        self.error_histories.append(_snapshot(messages))
        if self.responses:
            self._using_fallback = True
            self.model = "fallback-model"
            return "retry"
        return "abort"

    async def on_model_call_event(self, event: dict) -> None:
        self.events.append(dict(event))


def _assert_safe_events(events: list[dict], forbidden_texts: tuple[str, ...]) -> None:
    assert all(set(event) == _SAFE_EVENT_KEYS for event in events)
    serialized = json.dumps(events, ensure_ascii=False)
    for text in forbidden_texts:
        assert text not in serialized


def _assert_retry_history_is_clean(loop, result) -> None:
    assert result.ok is True
    assert result.status == "completed"
    assert [message["role"] for message in loop.error_histories[0]] == ["user"]
    assert [message["role"] for message in loop.call_histories[1]] == ["user"]
    assert [message["role"] for message in result.messages] == [
        "user",
        "assistant",
    ]
    assert result.messages[-1]["content"] == "final response body"


def test_build_assistant_msg_dict_preserves_reasoning_content():
    result = build_assistant_msg_dict(
        _assistant(
            content=None,
            reasoning_content="private protocol state",
            tool_calls=[_tool_call()],
        )
    )

    assert result["reasoning_content"] == "private protocol state"
    assert result["tool_calls"][0]["function"]["name"] == "cron"


@pytest.mark.parametrize("value", [8192, "8192"])
def test_output_limit_setting_accepts_positive_integer(value):
    from hermes.config import _positive_int_setting

    assert _positive_int_setting(value, "test_limit") == 8192


@pytest.mark.parametrize("value", [True, 0, -1, "0", "1.5", "invalid"])
def test_output_limit_setting_rejects_invalid_values(value):
    from hermes.config import _positive_int_setting

    with pytest.raises(ValueError, match="positive integer"):
        _positive_int_setting(value, "test_limit")


def test_build_assistant_msg_dict_backfills_missing_reasoning_content():
    result = build_assistant_msg_dict(
        _assistant(content=None, tool_calls=[_tool_call()])
    )

    assert result["reasoning_content"] == ""


def test_build_assistant_msg_dict_preserves_reasoning_for_continuation_only():
    assistant = _assistant(
        content=None,
        reasoning_content="truncated protocol state",
    )

    assert "reasoning_content" not in build_assistant_msg_dict(assistant)
    assert build_assistant_msg_dict(
        assistant,
        preserve_reasoning=True,
    )["reasoning_content"] == "truncated protocol state"


def test_length_content_continuation_preserves_reasoning_protocol_state():
    assistant = _assistant(
        content="partial response",
        reasoning_content="partial reasoning",
    )

    result = build_assistant_msg_dict(
        assistant,
        preserve_reasoning=True,
    )

    assert result["content"] == "partial response"
    assert result["reasoning_content"] == "partial reasoning"


@pytest.mark.parametrize(
    ("reasoning_content", "finish_reason", "expected_outcome"),
    [
        ("private reasoning payload", "length", "output_length_exhausted"),
        ("private reasoning payload", "stop", "empty_model_response"),
        (_MISSING, "length", "output_length_exhausted"),
        (_MISSING, "stop", "empty_model_response"),
    ],
)
def test_sync_invalid_response_retries_before_history_append(
    reasoning_content,
    finish_reason,
    expected_outcome,
):
    loop = _SyncProtocolLoop([
        _response(
            content=None,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
        ),
        _response(content="final response body"),
    ])

    result = loop.run("user request")

    _assert_retry_history_is_clean(loop, result)
    assert len(loop.events) == len(loop.call_histories) == 2
    assert [event["model_role"] for event in loop.events] == [
        "primary",
        "fallback",
    ]
    assert [event["outcome"] for event in loop.events] == [
        expected_outcome,
        "success",
    ]
    _assert_safe_events(
        loop.events,
        ("private reasoning payload", "final response body"),
    )


async def _exercise_async_invalid_response(
    reasoning_content,
    finish_reason,
    expected_outcome,
) -> None:
    loop = _AsyncProtocolLoop([
        _response(
            content=None,
            reasoning_content=reasoning_content,
            finish_reason=finish_reason,
        ),
        _response(content="final response body"),
    ])

    result = await loop.run("user request")

    _assert_retry_history_is_clean(loop, result)
    assert len(loop.events) == len(loop.call_histories) == 2
    assert [event["model_role"] for event in loop.events] == [
        "primary",
        "fallback",
    ]
    assert [event["outcome"] for event in loop.events] == [
        expected_outcome,
        "success",
    ]
    _assert_safe_events(
        loop.events,
        ("private reasoning payload", "final response body"),
    )


@pytest.mark.parametrize(
    ("reasoning_content", "finish_reason", "expected_outcome"),
    [
        ("private reasoning payload", "length", "output_length_exhausted"),
        ("private reasoning payload", "stop", "empty_model_response"),
        (_MISSING, "length", "output_length_exhausted"),
        (_MISSING, "stop", "empty_model_response"),
    ],
)
def test_async_invalid_response_retries_before_history_append(
    reasoning_content,
    finish_reason,
    expected_outcome,
):
    asyncio.run(
        _exercise_async_invalid_response(
            reasoning_content,
            finish_reason,
            expected_outcome,
        )
    )


def test_model_error_event_does_not_store_exception_text():
    loop = _SyncProtocolLoop([
        RuntimeError("provider response contains private raw body"),
        _response(content="final response body"),
    ])

    result = loop.run("user request")

    assert result.ok is True
    assert len(loop.events) == len(loop.call_histories) == 2
    assert loop.events[0]["outcome"] == "error"
    assert loop.events[0]["exception_type"] == "RuntimeError"
    assert loop.events[0]["model_role"] == "primary"
    assert loop.events[1]["model_role"] == "fallback"
    _assert_safe_events(
        loop.events,
        ("provider response contains private raw body", "final response body"),
    )


class _CapturingSyncCompletions:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class _CapturingAsyncCompletions:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _client(completions) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )


def _conversation_loop(
    conn: sqlite3.Connection,
    session_id: str,
    completions,
) -> ConversationAgentLoop:
    return ConversationAgentLoop(
        model="primary-model",
        max_iterations=3,
        tools=[],
        system_prompt="test",
        registry=None,
        client=_client(completions),
        session_key=session_id,
        conn=conn,
        db_session_id=session_id,
        existing_messages=[],
        max_retries=0,
        max_continuations=1,
        compression_threshold=100000,
        model_kwargs={"max_tokens": 8192},
    )


def test_sync_reasoning_length_continues_on_primary_with_higher_limit(tmp_path):
    conn = init_db(str(tmp_path / "sync-continuation.db"))
    try:
        session_id = create_session(conn)
        add_message(conn, session_id, {"role": "user", "content": "request"})
        completions = _CapturingSyncCompletions([
            _response(
                content=None,
                reasoning_content="truncated protocol state",
                finish_reason="length",
            ),
            _response(content="final response body"),
        ])
        loop = _conversation_loop(conn, session_id, completions)

        result = loop.run("request")

        assert result.ok is True
        assert loop._using_fallback is False
        assert [request["max_tokens"] for request in completions.requests] == [
            8192,
            8192,
        ]
        second_messages = completions.requests[1]["messages"]
        assert [message["role"] for message in second_messages[-3:]] == [
            "user",
            "assistant",
            "user",
        ]
        assert second_messages[-2]["reasoning_content"] == (
            "truncated protocol state"
        )
        assert second_messages[-1]["content"] == CONTINUE_MESSAGE
        assert [message["role"] for message in get_session_messages(
            conn,
            session_id,
        )] == ["user", "assistant", "user", "assistant"]
    finally:
        conn.close()


async def _exercise_async_reasoning_continuation(tmp_path) -> None:
    conn = init_db(str(tmp_path / "async-continuation.db"))
    try:
        session_id = create_session(conn)
        add_message(conn, session_id, {"role": "user", "content": "request"})
        completions = _CapturingAsyncCompletions([
            _response(
                content=None,
                reasoning_content="truncated protocol state",
                finish_reason="length",
            ),
            _response(content="final response body"),
        ])
        loop = AsyncConversationAgentLoop(
            model="primary-model",
            max_iterations=3,
            tools=[],
            system_prompt="test",
            registry=None,
            client=_client(completions),
            session_key=session_id,
            conn=conn,
            db_session_id=session_id,
            existing_messages=[],
            max_retries=0,
            max_continuations=1,
            compression_threshold=100000,
            model_kwargs={"max_tokens": 8192},
        )

        result = await loop.run("request")

        assert result.ok is True
        assert loop._using_fallback is False
        assert [request["max_tokens"] for request in completions.requests] == [
            8192,
            8192,
        ]
        second_messages = completions.requests[1]["messages"]
        assert second_messages[-2]["reasoning_content"] == (
            "truncated protocol state"
        )
        assert second_messages[-1]["content"] == CONTINUE_MESSAGE
        assert [message["role"] for message in get_session_messages(
            conn,
            session_id,
        )] == ["user", "assistant", "user", "assistant"]
    finally:
        conn.close()


def test_async_reasoning_length_continues_on_primary_with_higher_limit(tmp_path):
    asyncio.run(_exercise_async_reasoning_continuation(tmp_path))


def test_sync_fallback_switches_to_its_own_output_limit(tmp_path, monkeypatch):
    conn = init_db(str(tmp_path / "sync-fallback-limit.db"))
    fallback_client = object()
    try:
        session_id = create_session(conn)
        loop = ConversationAgentLoop(
            model="primary-model",
            max_iterations=1,
            tools=[],
            system_prompt="test",
            registry=None,
            client=object(),
            session_key=session_id,
            conn=conn,
            db_session_id=session_id,
            existing_messages=[],
            max_retries=0,
            max_continuations=0,
            compression_threshold=100000,
            model_kwargs={"max_tokens": 16384},
            fallback_model_kwargs={"max_tokens": 8192},
        )
        monkeypatch.setattr(
            "hermes.conversation.switch_to_fallback",
            lambda: (fallback_client, "fallback-model"),
        )

        assert loop._try_fallback_or_abort() == "retry"
        assert loop.client is fallback_client
        assert loop.model == "fallback-model"
        assert loop.model_kwargs == {"max_tokens": 8192}
    finally:
        conn.close()


def test_async_resume_on_fallback_uses_fallback_output_limit(
    tmp_path,
    monkeypatch,
):
    conn = init_db(str(tmp_path / "async-fallback-limit.db"))
    fallback_client = SimpleNamespace(close=lambda: None)
    try:
        session_id = create_session(conn)
        monkeypatch.setattr(
            "hermes.conversation.switch_to_async_fallback",
            lambda: (fallback_client, "fallback-model"),
        )

        loop = AsyncConversationAgentLoop(
            model="primary-model",
            max_iterations=1,
            tools=[],
            system_prompt="test",
            registry=None,
            client=object(),
            session_key=session_id,
            conn=conn,
            db_session_id=session_id,
            existing_messages=[],
            max_retries=0,
            max_continuations=0,
            compression_threshold=100000,
            model_kwargs={"max_tokens": 16384},
            fallback_model_kwargs={"max_tokens": 8192},
            resume_from_history=True,
            resume_state={
                "using_fallback": True,
                "active_model": "fallback-model",
            },
        )

        assert loop.client is fallback_client
        assert loop.model == "fallback-model"
        assert loop.model_kwargs == {"max_tokens": 8192}
    finally:
        conn.close()
