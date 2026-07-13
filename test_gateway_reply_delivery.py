"""Gateway 回复发送可靠性专项测试。"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sqlite3
import time
from collections import deque
from types import SimpleNamespace

import pytest

# 导入 conversation / runner 时不需要访问真实模型服务。
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9")

from hermes.conversation import AsyncConversationAgentLoop  # noqa: E402
from hermes.db import (  # noqa: E402
    DBError,
    add_final_message_with_gateway_outbox,
    enqueue_gateway_message,
    ensure_session,
    get_gateway_outbox,
    get_session_messages,
    init_db,
    mark_gateway_outbox_chunk_sent,
)
from hermes.gateway.adapters import BasePlatformAdapter  # noqa: E402
from hermes.gateway.adapters import feishu as feishu_module  # noqa: E402
from hermes.gateway.adapters.feishu import (  # noqa: E402
    FEISHU_POST_LIMIT_BYTES,
    FeishuAdapter,
)
from hermes.gateway.runner import GatewayRunner  # noqa: E402
from hermes.gateway.types import (  # noqa: E402
    MessageEvent,
    SendResult,
    SessionSource,
    build_session_key,
)


def async_test(func):
    """不额外依赖 pytest-asyncio 的轻量异步测试包装器。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


class FakeResponse:
    def __init__(self, status_code: int, data: dict, headers: dict | None = None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = deque(responses)
        self.calls: list[dict] = []

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.popleft()


class FakeAdapter(BasePlatformAdapter):
    def __init__(self, results: list[SendResult]):
        super().__init__("fake")
        self.results = deque(results)
        self.payloads: list[dict] = []
        self._running = True

    async def connect(self) -> bool:
        return True

    async def disconnect(self):
        self._running = False

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        return self.results.popleft()

    def prepare_outbound(
        self,
        content: str,
        *,
        delivery_id: str,
    ) -> list[dict]:
        return [
            {"content": chunk, "request_uuid": f"{delivery_id}-{index}"}
            for index, chunk in enumerate(content.split("|"))
        ]

    async def send_prepared(
        self,
        chat_id: str,
        payload: dict,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> SendResult:
        self.payloads.append(payload)
        return self.results.popleft()


def make_feishu_adapter(**kwargs) -> FeishuAdapter:
    adapter = FeishuAdapter(
        app_id="app",
        app_secret="secret",
        db_path=":memory:",
        verification_token="verify",
        send_retry_base_delay=0,
        send_rate_limit_per_chat=100,
        **kwargs,
    )
    adapter._running = True
    adapter._tenant_token = "old-token"
    adapter._token_expires_at = time.time() + 3600
    return adapter


def make_event(platform: str = "fake") -> MessageEvent:
    return MessageEvent(
        message_id="om-user-1",
        text="问题",
        source=SessionSource(
            platform=platform,
            account_id="app",
            chat_id="chat-1",
            user_id="user-1",
            thread_id="thread-1",
        ),
    )


def test_feishu_markdown_chunks_are_stable_and_within_limit():
    adapter = make_feishu_adapter()
    content = ("## 标题😀\n\n```python\nprint(\"Hermes\")\n```\n\n" * 1200)

    first = adapter.prepare_outbound(content, delivery_id="delivery-1")
    second = adapter.prepare_outbound(content, delivery_id="delivery-1")

    assert len(first) > 1
    assert [item["request_uuid"] for item in first] == [
        item["request_uuid"] for item in second
    ]
    texts = []
    for payload in first:
        body = json.loads(payload["content"])
        text = body["zh_cn"]["content"][0][0]["text"]
        texts.append(text)
        assert adapter._post_payload_size(text) <= FEISHU_POST_LIMIT_BYTES
        assert payload["msg_type"] == "post"
    assert "".join(texts) == content


def test_v4_database_migrates_to_gateway_outbox(tmp_path):
    db_path = tmp_path / "v4.db"
    raw = sqlite3.connect(db_path)
    raw.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    raw.execute("INSERT INTO schema_version(version) VALUES (4)")
    raw.commit()
    raw.close()

    conn = init_db(str(db_path))
    try:
        assert conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == 5
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='gateway_outbox'"
        ).fetchone()[0] == "gateway_outbox"
    finally:
        conn.close()


def test_final_message_rolls_back_when_outbox_insert_fails(tmp_path):
    db_path = str(tmp_path / "rollback.db")
    conn = init_db(db_path)
    ensure_session(conn, "session-1")
    enqueue_gateway_message(conn, "route-1", "message-1", "{}")

    with pytest.raises(DBError):
        add_final_message_with_gateway_outbox(
            conn,
            "session-1",
            {"role": "assistant", "content": "不能留下半条记录"},
            {
                "id": "delivery-1",
                "route_key": "route-1",
                "source_message_id": "message-1",
                "event_json": "{}",
                # 故意缺少 platform,验证 assistant 插入一并回滚。
                "chat_id": "chat-1",
                "delivery_kind": "final",
                "payloads": [{"content": "回答"}],
            },
        )

    assert get_session_messages(conn, "session-1") == []
    status = conn.execute(
        "SELECT status FROM gateway_message_queue WHERE message_id='message-1'"
    ).fetchone()[0]
    assert status == "queued"
    conn.close()


@async_test
async def test_feishu_send_rate_limit_is_scoped_per_chat(monkeypatch):
    adapter = make_feishu_adapter()
    adapter.send_rate_limit_per_chat = 2
    now = [0.0]
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(feishu_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(feishu_module.asyncio, "sleep", fake_sleep)

    await adapter._wait_send_slot("chat-1")
    await adapter._wait_send_slot("chat-1")
    await adapter._wait_send_slot("chat-2")
    await adapter._wait_send_slot("chat-1")

    assert sleeps == [1.0]


@async_test
async def test_feishu_reply_uses_thread_api_and_falls_back_once():
    adapter = make_feishu_adapter(send_max_retries=1)
    adapter._http = FakeHTTP([
        FakeResponse(400, {"code": 230071}),
        FakeResponse(200, {"code": 0, "data": {"message_id": "om-bot"}}),
    ])
    payload = adapter.prepare_outbound("**回答**", delivery_id="d1")[0]

    result = await adapter.send_prepared(
        "chat-1",
        payload,
        reply_to_message_id="om-user",
        thread_id="thread-1",
    )

    assert result.success is True
    assert len(adapter._http.calls) == 2
    assert adapter._http.calls[0]["url"].endswith("/om-user/reply")
    assert adapter._http.calls[0]["json"]["reply_in_thread"] is True
    assert adapter._http.calls[1]["json"]["reply_in_thread"] is False
    assert adapter._http.calls[0]["json"]["uuid"] == payload["request_uuid"]


@async_test
async def test_feishu_permanent_permission_error_is_not_retried():
    adapter = make_feishu_adapter(send_max_retries=3)
    adapter._http = FakeHTTP([
        FakeResponse(403, {"code": 99991672}),
    ])
    payload = adapter.prepare_outbound("回答", delivery_id="d1")[0]

    result = await adapter.send_prepared("chat-1", payload)

    assert result.success is False
    assert result.error == "permission_denied"
    assert result.retryable is False
    assert len(adapter._http.calls) == 1


@async_test
async def test_feishu_invalid_token_is_refreshed_once():
    adapter = make_feishu_adapter(send_max_retries=2)
    adapter._http = FakeHTTP([
        FakeResponse(401, {"code": 99991663}),
        FakeResponse(200, {
            "code": 0,
            "tenant_access_token": "new-token",
            "expire": 7200,
        }),
        FakeResponse(200, {"code": 0, "data": {"message_id": "om-bot"}}),
    ])
    payload = adapter.prepare_outbound("回答", delivery_id="d1")[0]

    result = await adapter.send_prepared("chat-1", payload)

    assert result.success is True
    assert adapter._tenant_token == "new-token"
    assert len(adapter._http.calls) == 3
    assert "/auth/v3/tenant_access_token/internal" in adapter._http.calls[1]["url"]


@async_test
async def test_async_final_message_and_outbox_are_written_together(tmp_path):
    db_path = str(tmp_path / "atomic.db")
    conn = init_db(db_path)
    ensure_session(conn, "session-1")
    enqueue_gateway_message(conn, "route-1", "message-1", "{}")

    message = SimpleNamespace(content="最终回答", tool_calls=None)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
    )

    class Completions:
        async def create(self, **kwargs):
            return response

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )

    def persist_final(db_conn, session_id, msg):
        add_final_message_with_gateway_outbox(
            db_conn,
            session_id,
            msg,
            {
                "id": "delivery-1",
                "route_key": "route-1",
                "source_message_id": "message-1",
                "event_json": "{}",
                "platform": "fake",
                "chat_id": "chat-1",
                "delivery_kind": "final",
                "payloads": [{"content": "最终回答"}],
            },
        )

    loop = AsyncConversationAgentLoop(
        model="test-model",
        max_iterations=1,
        tools=[],
        system_prompt="system",
        registry=SimpleNamespace(),
        client=fake_client,
        session_key="session-1",
        conn=conn,
        db_session_id="session-1",
        existing_messages=[],
        max_retries=0,
        max_continuations=0,
        compression_threshold=100000,
        final_message_callback=persist_final,
    )

    result = await loop.run("问题")

    assert result.status == "completed"
    assert get_session_messages(conn, "session-1")[-1] == {
        "role": "assistant",
        "content": "最终回答",
    }
    assert get_gateway_outbox(conn, "delivery-1")["status"] == "pending"
    status = conn.execute(
        "SELECT status FROM gateway_message_queue WHERE message_id='message-1'"
    ).fetchone()[0]
    assert status == "reply_pending"
    conn.close()


@async_test
async def test_runner_resumes_from_failed_chunk(tmp_path):
    db_path = str(tmp_path / "runner.db")
    runner = GatewayRunner(
        config={
            "gateway": {
                "delivery_max_attempts": 3,
                "delivery_retry_base_delay": 0.1,
                "delivery_retry_max_delay": 0.1,
            },
        },
        db_path=db_path,
    )
    adapter = FakeAdapter([
        SendResult(success=True, message_id="m1"),
        SendResult(success=False, error="network", retryable=True),
        SendResult(success=True, message_id="m2"),
    ])
    runner.add_adapter(adapter)
    event = make_event()
    route_key = build_session_key(event.source, runner.agent_name)
    runner._persist_event(route_key, event)
    outbox = runner._build_outbox(
        route_key,
        event,
        "第一段|第二段",
        "delivery-1",
        "final",
    )
    runner._enqueue_outbox(outbox)

    delivered = await runner._deliver_outbox(
        route_key,
        event,
        "delivery-1",
    )

    assert delivered is True
    assert [payload["content"] for payload in adapter.payloads] == [
        "第一段",
        "第二段",
        "第二段",
    ]
    row = runner._load_outbox("delivery-1")
    assert row["status"] == "delivered"
    assert row["next_chunk_index"] == 2
    assert row["message_ids"] == ["m1", "m2"]


@async_test
async def test_runner_restart_restores_outbox_without_running_model(tmp_path):
    from unittest.mock import AsyncMock

    db_path = str(tmp_path / "restart.db")
    first_runner = GatewayRunner(config={"gateway": {}}, db_path=db_path)
    first_runner.add_adapter(FakeAdapter([]))
    event = make_event()
    route_key = build_session_key(event.source, first_runner.agent_name)
    first_runner._persist_event(route_key, event)
    outbox = first_runner._build_outbox(
        route_key,
        event,
        "第一段|第二段",
        "delivery-1",
        "final",
    )
    first_runner._enqueue_outbox(outbox)
    conn = init_db(db_path)
    try:
        mark_gateway_outbox_chunk_sent(conn, "delivery-1", 1, ["m1"])
    finally:
        conn.close()

    second_runner = GatewayRunner(config={"gateway": {}}, db_path=db_path)
    adapter = FakeAdapter([SendResult(success=True, message_id="m2")])
    second_runner.add_adapter(adapter)
    second_runner._run_agent = AsyncMock()

    await second_runner._restore_outbound_messages()
    await second_runner._restore_queued_messages()
    for _ in range(100):
        row = second_runner._load_outbox("delivery-1")
        if row["status"] == "delivered":
            break
        await asyncio.sleep(0.01)

    assert [payload["content"] for payload in adapter.payloads] == ["第二段"]
    second_runner._run_agent.assert_not_awaited()
    conn = init_db(db_path)
    try:
        assert conn.execute(
            "SELECT 1 FROM gateway_message_queue WHERE message_id=?",
            (event.message_id,),
        ).fetchone() is None
    finally:
        conn.close()


@async_test
async def test_runner_keeps_audit_state_on_permanent_failure(tmp_path):
    db_path = str(tmp_path / "failed.db")
    runner = GatewayRunner(config={"gateway": {}}, db_path=db_path)
    runner.add_adapter(FakeAdapter([
        SendResult(success=False, error="permission_denied", retryable=False),
    ]))
    event = make_event()
    route_key = build_session_key(event.source, runner.agent_name)
    runner._persist_event(route_key, event)
    outbox = runner._build_outbox(
        route_key,
        event,
        "回答",
        "delivery-1",
        "final",
    )
    runner._enqueue_outbox(outbox)

    delivered = await runner._deliver_outbox(
        route_key,
        event,
        "delivery-1",
    )

    assert delivered is False
    conn = init_db(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM gateway_message_queue WHERE message_id=?",
            (event.message_id,),
        ).fetchone()[0]
        assert status == "delivery_failed"
        assert get_gateway_outbox(conn, "delivery-1")["status"] == "permanent_failed"
    finally:
        conn.close()


@async_test
async def test_reply_returns_failure_to_caller(tmp_path):
    runner = GatewayRunner(config={"gateway": {}}, db_path=str(tmp_path / "r.db"))
    runner.add_adapter(FakeAdapter([
        SendResult(success=False, error="network", retryable=True),
    ]))

    result = await runner._reply(make_event(), "回答")

    assert result.success is False
    assert result.retryable is True
