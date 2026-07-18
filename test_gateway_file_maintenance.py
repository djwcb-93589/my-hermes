"""Gateway 文件缓存维护与永久失败通知回归测试。"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from hermes.db import (
    DBError,
    fail_gateway_file_delivery,
    get_gateway_file_delivery,
    get_gateway_outbox,
    init_db,
)
from hermes.gateway.files.cache import (
    CacheCleanupResult,
    cleanup_expired_cache,
)
from hermes.gateway.runner import GatewayRunner
from hermes.gateway.types import (
    MessageEvent,
    SessionSource,
    build_session_key,
)


class _RetentionPersistence:
    def __init__(self):
        self.operations = []

    async def call(self, operation, *args, **kwargs):
        self.operations.append(operation.__name__)
        return 0


class _CleanupAdapter:
    def __init__(self):
        self.calls = 0

    async def cleanup_file_cache(self):
        self.calls += 1
        return CacheCleanupResult(scanned_files=2, removed_files=1)


def test_retention_cycle_invokes_adapter_file_cache_cleanup():
    runner = object.__new__(GatewayRunner)
    runner.persistence = _RetentionPersistence()
    adapter = _CleanupAdapter()
    runner.adapters = {"feishu": adapter}
    runner.outbox_retention_seconds = 3600
    runner.ownership_retention_seconds = 3600
    runner.retention_cleanup_batch_size = 20
    runner._runtime_lease_epoch = 3

    asyncio.run(runner._run_retention_cleanup())

    assert adapter.calls == 1
    assert runner.persistence.operations == [
        "prune_gateway_terminal_outbox",
        "prune_gateway_terminal_ownership",
    ]


def test_cache_cleanup_removes_only_expired_generated_files(tmp_path):
    generated = tmp_path / f"hermes_{'a' * 24}_{'b' * 32}.txt"
    ordinary = tmp_path / "user-owned.txt"
    generated.write_text("expired", encoding="utf-8")
    ordinary.write_text("keep", encoding="utf-8")
    old_timestamp = time.time() - 120
    os.utime(generated, (old_timestamp, old_timestamp))
    os.utime(ordinary, (old_timestamp, old_timestamp))

    result = asyncio.run(cleanup_expired_cache(tmp_path, 60))

    assert result.scanned_files == 1
    assert result.removed_files == 1
    assert result.failed_files == 0
    assert not generated.exists()
    assert ordinary.exists()


class _PermanentFailureAdapter:
    def __init__(self):
        self.notification_content = ""

    async def upload_file_delivery(self, **kwargs):
        return SimpleNamespace(
            platform_file_key=None,
            retryable=False,
            error_code="platform_permission_denied",
            retry_after_seconds=None,
        )

    def prepare_outbound(self, content, *, delivery_id):
        self.notification_content = content
        return [{"content": content, "request_uuid": delivery_id}]


class _FailurePersistence:
    def __init__(self, delivery):
        self.delivery = delivery
        self.failure_args = None

    async def call(self, operation, *args, **kwargs):
        if operation.__name__ == "claim_gateway_file_delivery":
            return {
                **self.delivery,
                "status": "uploading",
                "attempt_count": 1,
            }
        if operation.__name__ == "gateway_file_delivery_claim_is_valid":
            return True
        if operation.__name__ == "fail_gateway_file_delivery":
            self.failure_args = (args, kwargs)
            return True
        raise AssertionError(f"unexpected persistence operation: {operation}")


def test_permanent_upload_failure_persists_user_notification_outbox():
    source = SessionSource(
        platform="feishu",
        account_id="app-test",
        chat_id="chat-test",
        user_id="user-test",
    )
    source_event = MessageEvent(
        message_id="message-test",
        text="发送报告",
        source=source,
    )
    route_key = build_session_key(source, "main")
    delivery = {
        "id": "delivery-test",
        "route_key": route_key,
        "source_message_id": source_event.message_id,
        "platform": source.platform,
        "chat_id": source.chat_id,
        "reply_to_message_id": source_event.message_id,
        "thread_id": None,
        "local_path": "D:/private/report.docx",
        "display_name": "report.docx",
        "size_bytes": 20,
        "sha256": "a" * 64,
        "status": "pending",
        "attempt_count": 0,
        "source_event_json": GatewayRunner._serialize_event(source_event),
    }
    runner = object.__new__(GatewayRunner)
    adapter = _PermanentFailureAdapter()
    persistence = _FailurePersistence(delivery)
    runner.adapters = {"feishu": adapter}
    runner.persistence = persistence
    runner.db_path = "D:/runtime/hermes.db"
    runner.agent_name = "main"
    runner.delivery_max_attempts = 3
    runner.runtime_lease_heartbeat_seconds = 10.0
    runner._runtime_lease_acquired = True
    runner._runtime_lease_name = "gateway-main"
    runner._runtime_instance_id = "instance-test"
    runner._runtime_lease_epoch = 1
    runner._lifecycle_phase = "stopping"

    asyncio.run(runner._process_file_delivery(delivery))

    assert persistence.failure_args is not None
    args, kwargs = persistence.failure_args
    assert args[0:2] == ("delivery-test", "platform_permission_denied")
    failure_outbox = args[2]
    assert failure_outbox["delivery_kind"] == "file_delivery_failure"
    assert failure_outbox["reply_to_message_id"] == "message-test"
    assert "report.docx" in adapter.notification_content
    assert "D:/private" not in adapter.notification_content
    assert "platform_permission_denied" not in adapter.notification_content
    assert kwargs == {
        "lease_name": "gateway-main",
        "instance_id": "instance-test",
        "lease_epoch": 1,
    }


def test_file_failure_and_notification_outbox_commit_atomically(tmp_path):
    db_path = tmp_path / "gateway.db"
    conn = init_db(str(db_path))
    now = time.time()
    source = SessionSource(
        platform="feishu",
        account_id="app-test",
        chat_id="chat-test",
        user_id="user-test",
    )
    route_key = build_session_key(source, "main")
    notification_event = MessageEvent(
        message_id="file-delivery-failure:delivery-test",
        text="文件发送失败",
        source=source,
    )
    failure_outbox = {
        "id": "failure-outbox-test",
        "route_key": route_key,
        "source_message_id": notification_event.message_id,
        "queue_message_id": notification_event.message_id,
        "event_json": GatewayRunner._serialize_event(notification_event),
        "platform": "feishu",
        "chat_id": "chat-test",
        "reply_to_message_id": "message-test",
        "thread_id": None,
        "delivery_kind": "file_delivery_failure",
        "payloads": [{"content": "文件发送失败"}],
    }
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO gateway_runtime_lease (
                lease_name, instance_id, heartbeat_at, expires_at,
                lease_epoch
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("gateway-main", "instance-test", now, now + 60, 1),
        )
        conn.execute(
            """
            INSERT INTO gateway_file_deliveries (
                id, approval_id, route_key, conversation_id,
                source_message_id, platform, chat_id,
                reply_to_message_id, thread_id, local_path,
                display_name, size_bytes, sha256, status, attempt_count,
                claimed_by, claim_epoch, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "delivery-test",
                "approval-test",
                route_key,
                "conversation-test",
                "message-test",
                "feishu",
                "chat-test",
                "message-test",
                None,
                "D:/report.docx",
                "report.docx",
                20,
                "a" * 64,
                "uploading",
                1,
                "instance-test",
                1,
                now,
                now,
            ),
        )
        conn.commit()

        with pytest.raises(DBError):
            fail_gateway_file_delivery(
                conn,
                "delivery-test",
                "upload_failed",
                {},
                lease_name="gateway-main",
                instance_id="instance-test",
                lease_epoch=1,
            )
        assert get_gateway_file_delivery(conn, "delivery-test")["status"] == (
            "uploading"
        )

        assert fail_gateway_file_delivery(
            conn,
            "delivery-test",
            "upload_failed",
            failure_outbox,
            lease_name="gateway-main",
            instance_id="instance-test",
            lease_epoch=1,
        )
        assert get_gateway_file_delivery(conn, "delivery-test")["status"] == (
            "permanent_failed"
        )
        assert get_gateway_outbox(conn, "failure-outbox-test")["status"] == (
            "pending"
        )
    finally:
        conn.close()
