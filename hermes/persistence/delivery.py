from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .database import (
    DBError,
    GATEWAY_FILE_DELIVERY_STATUSES,
    InvalidMessageError,
    _immediate_transaction,
    transaction,
)
from .core import _insert_message
from .gateway import (
    _gateway_outbox_fence_values,
    _gateway_outbox_lease_clause,
    _gateway_lease_epoch_value,
    _insert_gateway_outbox,
    _serialize_gateway_json,
    _set_gateway_queue_status,
    gateway_runtime_lease_is_valid,
)

_GATEWAY_FILE_DELIVERY_COLUMNS = (
    "id, origin_kind, approval_id, cron_run_id, route_key, conversation_id, "
    "source_message_id, platform, chat_id, reply_to_message_id, thread_id, "
    "local_path, display_name, size_bytes, sha256, platform_file_key, status, "
    "attempt_count, next_attempt_at, last_error, last_error_code, claimed_by, "
    "claim_epoch, created_at, updated_at, outbox_id"
)


@dataclass(frozen=True, slots=True)
class GatewayFinalMessagePersistResult:
    """最终消息或协调提示的 Outbox 持久化事实。"""

    delivery_id: str
    created: bool
    reused_existing: bool
    desired_content_persisted: bool


_GATEWAY_OUTBOX_CONTENT_COLUMNS = (
    "id, queue_message_id, payloads_json, status, next_chunk_index, "
    "message_ids_json, attempt_count, next_attempt_at, last_error, "
    "last_error_code, claimed_by, claim_epoch, event_json, platform, chat_id, "
    "reply_to_message_id, thread_id, created_at, updated_at"
)


def _validate_gateway_outbox_content(outbox: dict) -> tuple[str, str]:
    """校验 Outbox 身份并生成用于精确比对的规范化 payload。"""

    if not isinstance(outbox, dict):
        raise DBError("gateway outbox must be an object")
    required = (
        "id",
        "route_key",
        "source_message_id",
        "event_json",
        "platform",
        "chat_id",
        "delivery_kind",
        "payloads",
    )
    missing = [name for name in required if not outbox.get(name)]
    if missing:
        raise DBError(f"gateway outbox missing fields: {', '.join(missing)}")
    if not isinstance(outbox["payloads"], list):
        raise DBError("gateway outbox payloads must be a list")
    queue_message_id = str(
        outbox.get("queue_message_id") or outbox["source_message_id"]
    )
    return queue_message_id, _serialize_gateway_json(
        outbox["payloads"],
        "gateway outbox payloads",
    )


def _select_gateway_outbox_content_row(
    conn: sqlite3.Connection,
    outbox: dict,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
):
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    lease_clause, lease_params = _gateway_outbox_lease_clause(
        fence,
        time.time(),
    )
    return conn.execute(
        f"""
        SELECT {_GATEWAY_OUTBOX_CONTENT_COLUMNS}
        FROM gateway_outbox
        WHERE route_key=? AND source_message_id=? AND delivery_kind=?
        {lease_clause}
        """,
        (
            str(outbox["route_key"]),
            str(outbox["source_message_id"]),
            str(outbox["delivery_kind"]),
            *lease_params,
        ),
    ).fetchone()


def _outbox_row_is_unclaimed_and_unsent(row) -> bool:
    """仅接受数据库能明确证明尚未投递的 Outbox 覆盖候选。"""

    if row is None:
        return False
    try:
        message_ids = json.loads(str(row[5]))
        return (
            str(row[3]) == "pending"
            and int(row[4]) == 0
            and isinstance(message_ids, list)
            and not message_ids
            and int(row[6]) == 0
            and row[7] is None
            and row[8] is None
            and row[9] is None
            and row[10] is None
            and row[11] is None
            and float(row[17]) == float(row[18])
        )
    except (TypeError, ValueError):
        return False


def _outbox_row_matches_delivery_identity(row, outbox: dict) -> bool:
    """确认既有 Outbox 仍属于本次期望的投递目标与事件身份。"""

    return (
        str(row[12]) == str(outbox["event_json"])
        and str(row[13]) == str(outbox["platform"])
        and str(row[14]) == str(outbox["chat_id"])
        and row[15] == outbox.get("reply_to_message_id")
        and row[16] == outbox.get("thread_id")
    )


def _replace_gateway_outbox_payloads_if_safe(
    conn: sqlite3.Connection,
    outbox: dict,
    row,
    desired_payloads_json: str,
    *,
    lease_name: str | None,
    instance_id: str | None,
    lease_epoch: int | None,
) -> bool:
    """只在未领取、未发送的严格状态下以 CAS 更新 Outbox 正文。"""

    if (
        not _outbox_row_matches_delivery_identity(row, outbox)
        or not _outbox_row_is_unclaimed_and_unsent(row)
    ):
        return False
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    now = time.time()
    lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
    cursor = conn.execute(
        f"""
        UPDATE gateway_outbox
        SET payloads_json=?, updated_at=?
        WHERE id=?
          AND route_key=?
          AND source_message_id=?
          AND delivery_kind=?
          AND queue_message_id=?
          AND event_json=?
          AND platform=?
          AND chat_id=?
          AND reply_to_message_id IS ?
          AND thread_id IS ?
          AND payloads_json=?
          AND status='pending'
          AND next_chunk_index=0
          AND message_ids_json=?
          AND attempt_count=0
          AND next_attempt_at IS NULL
          AND last_error IS NULL
          AND last_error_code IS NULL
          AND claimed_by IS NULL
          AND claim_epoch IS NULL
          AND created_at=updated_at
          AND updated_at=?
          {lease_clause}
        """,
        (
            desired_payloads_json,
            now,
            str(row[0]),
            str(outbox["route_key"]),
            str(outbox["source_message_id"]),
            str(outbox["delivery_kind"]),
            str(row[1]),
            str(outbox["event_json"]),
            str(outbox["platform"]),
            str(outbox["chat_id"]),
            outbox.get("reply_to_message_id"),
            outbox.get("thread_id"),
            str(row[2]),
            str(row[5]),
            float(row[18]),
            *lease_params,
        ),
    )
    return cursor.rowcount == 1


def _persist_gateway_outbox_content_in_transaction(
    conn: sqlite3.Connection,
    outbox: dict,
    *,
    allow_payload_update: bool,
    track_source_ownership: bool = True,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> GatewayFinalMessagePersistResult:
    """在调用方事务内精确确认或安全写入指定的 Outbox 正文。"""

    queue_message_id, desired_payloads_json = _validate_gateway_outbox_content(
        outbox,
    )
    row = _select_gateway_outbox_content_row(
        conn,
        outbox,
        lease_name=lease_name,
        instance_id=instance_id,
        lease_epoch=lease_epoch,
    )
    if row is None:
        inserted = _insert_gateway_outbox(
            conn,
            outbox,
            track_source_ownership=track_source_ownership,
            return_created=True,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        delivery_id, created = inserted
        persisted = _select_gateway_outbox_content_row(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        if persisted is None:
            raise DBError("gateway outbox insert did not create a row")
        if str(persisted[1]) != queue_message_id:
            raise DBError("gateway outbox queue identity mismatch")
        return GatewayFinalMessagePersistResult(
            delivery_id=str(delivery_id),
            created=created,
            reused_existing=not created,
            desired_content_persisted=(
                _outbox_row_matches_delivery_identity(persisted, outbox)
                and str(persisted[2]) == desired_payloads_json
            ),
        )

    if str(row[1]) != queue_message_id:
        raise DBError("gateway final outbox queue identity mismatch")
    if (
        _outbox_row_matches_delivery_identity(row, outbox)
        and str(row[2]) == desired_payloads_json
    ):
        return GatewayFinalMessagePersistResult(
            delivery_id=str(row[0]),
            created=False,
            reused_existing=True,
            desired_content_persisted=True,
        )

    if allow_payload_update:
        _replace_gateway_outbox_payloads_if_safe(
            conn,
            outbox,
            row,
            desired_payloads_json,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        row = _select_gateway_outbox_content_row(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        if row is None:
            raise DBError("gateway outbox disappeared during content update")
        if str(row[1]) != queue_message_id:
            raise DBError("gateway final outbox queue identity mismatch")

    return GatewayFinalMessagePersistResult(
        delivery_id=str(row[0]),
        created=False,
        reused_existing=True,
        desired_content_persisted=(
            _outbox_row_matches_delivery_identity(row, outbox)
            and str(row[2]) == desired_payloads_json
        ),
    )


def _insert_gateway_message_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    session_id: str,
    assistant_message_id: int,
    route_key: str,
    source_message_id: str,
) -> None:
    """关联最终 assistant 与 Outbox；调用方负责外层事务。"""
    now = time.time()
    conn.execute(
        """
        INSERT INTO gateway_message_deliveries (
            delivery_id, session_id, assistant_message_id, route_key,
            source_message_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            delivery_id,
            session_id,
            int(assistant_message_id),
            route_key,
            source_message_id,
            now,
            now,
        ),
    )


def add_final_message_with_gateway_outbox(
    conn: sqlite3.Connection,
    session_id: str,
    msg: dict,
    outbox: dict,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> GatewayFinalMessagePersistResult:
    """原子写入最终 assistant 消息、outbox 和 reply_pending 状态。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        raise InvalidMessageError(
            "gateway final delivery must reference an assistant message"
        )
    with transaction(conn):
        result = _persist_gateway_outbox_content_in_transaction(
            conn,
            outbox,
            allow_payload_update=True,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        if result.created:
            assistant_message_id = _insert_message(conn, session_id, msg)
            _insert_gateway_message_delivery(
                conn,
                delivery_id=result.delivery_id,
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                route_key=str(outbox["route_key"]),
                source_message_id=str(outbox["source_message_id"]),
            )
        _set_gateway_queue_status(
            conn,
            str(outbox["route_key"]),
            str(outbox.get("queue_message_id") or outbox["source_message_id"]),
            "reply_pending",
            time.time(),
        )
    return result


def enqueue_gateway_coordination_notice_outbox(
    conn: sqlite3.Connection,
    outbox: dict,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> GatewayFinalMessagePersistResult:
    """持久化独立协调提示，不接管原入站 Queue 或其 ownership。"""

    with transaction(conn):
        return _persist_gateway_outbox_content_in_transaction(
            conn,
            outbox,
            allow_payload_update=True,
            track_source_ownership=False,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )


def _gateway_file_delivery_row(row) -> dict | None:
    """把出站文件任务查询行恢复为稳定字典。"""
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "origin_kind": str(row[1]),
        "approval_id": str(row[2]) if row[2] is not None else None,
        "cron_run_id": str(row[3]) if row[3] is not None else None,
        "route_key": str(row[4]),
        "conversation_id": str(row[5]),
        "source_message_id": str(row[6]),
        "platform": str(row[7]),
        "chat_id": str(row[8]),
        "reply_to_message_id": str(row[9]) if row[9] is not None else None,
        "thread_id": str(row[10]) if row[10] is not None else None,
        "local_path": str(row[11]),
        "display_name": str(row[12]),
        "size_bytes": int(row[13]),
        "sha256": str(row[14]),
        "platform_file_key": str(row[15]) if row[15] is not None else None,
        "status": str(row[16]),
        "attempt_count": int(row[17]),
        "next_attempt_at": float(row[18]) if row[18] is not None else None,
        "last_error": str(row[19]) if row[19] is not None else None,
        "last_error_code": str(row[20]) if row[20] is not None else None,
        "claimed_by": str(row[21]) if row[21] is not None else None,
        "claim_epoch": int(row[22]) if row[22] is not None else None,
        "created_at": float(row[23]),
        "updated_at": float(row[24]),
        "outbox_id": str(row[25]) if row[25] is not None else None,
    }


def _gateway_file_delivery_fence(
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> tuple[str, str, int]:
    """文件任务不提供无 fencing 兼容路径。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway file delivery lease_name is required")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway file delivery instance_id is required")
    return lease_name, instance_id, _gateway_lease_epoch_value(lease_epoch)


def _validate_gateway_file_delivery_identity(delivery: dict) -> dict:
    """校验任务不可变身份，避免模型值直接进入 SQL 状态字段。"""
    if not isinstance(delivery, dict):
        raise DBError("gateway file delivery must be an object")
    normalized = dict(delivery)
    origin_kind = str(normalized.get("origin_kind", "gateway"))
    if origin_kind not in {"gateway", "cron"}:
        raise DBError("invalid gateway file delivery origin kind")
    normalized["origin_kind"] = origin_kind
    required_fields = (
        "id",
        "route_key",
        "conversation_id",
        "source_message_id",
        "platform",
        "chat_id",
        "local_path",
        "display_name",
        "sha256",
    )
    if origin_kind == "gateway":
        required_fields = ("approval_id", *required_fields)
    else:
        required_fields = ("cron_run_id", *required_fields)
    for field_name in required_fields:
        value = normalized.get(field_name)
        if not isinstance(value, str) or not value:
            raise DBError(
                f"gateway file delivery {field_name} is required"
            )
    if not normalized["id"].startswith("delivery_"):
        raise DBError("invalid gateway file delivery id")
    if (
        origin_kind == "gateway"
        and not normalized["approval_id"].startswith("approval_")
    ):
        raise DBError("invalid gateway file delivery approval id")
    if origin_kind == "cron":
        normalized["approval_id"] = None
    digest = normalized["sha256"]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise DBError("invalid gateway file delivery sha256")
    size_bytes = normalized.get("size_bytes")
    if isinstance(size_bytes, bool):
        raise DBError("gateway file delivery size_bytes must be positive")
    try:
        size_bytes = int(size_bytes)
    except (TypeError, ValueError) as exc:
        raise DBError(
            "gateway file delivery size_bytes must be positive"
        ) from exc
    if size_bytes <= 0:
        raise DBError("gateway file delivery size_bytes must be positive")
    normalized["size_bytes"] = size_bytes
    for field_name in ("reply_to_message_id", "thread_id"):
        value = normalized.get(field_name)
        if value is not None and not isinstance(value, str):
            raise DBError(
                f"gateway file delivery {field_name} must be a string or null"
            )
    return normalized


def create_cron_file_delivery(
    conn: sqlite3.Connection,
    delivery: dict,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> dict:
    """创建 Cron 产物文件的持久投递，不伪造 Gateway 审批或入站事件。"""
    normalized = _validate_gateway_file_delivery_identity({
        **delivery,
        "origin_kind": "cron",
    })
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(conn, *fence, now=now):
            raise DBError("gateway runtime lease is not valid")
        existing_row = conn.execute(
            f"""
            SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
            FROM gateway_file_deliveries
            WHERE origin_kind='cron' AND cron_run_id=? AND local_path=?
            """,
            (normalized["cron_run_id"], normalized["local_path"]),
        ).fetchone()
        existing = _gateway_file_delivery_row(existing_row)
        if existing is not None:
            for field_name in (
                "id", "route_key", "conversation_id", "source_message_id",
                "platform", "chat_id", "reply_to_message_id", "thread_id",
                "display_name", "size_bytes", "sha256",
            ):
                if existing[field_name] != normalized.get(field_name):
                    raise DBError("Cron file delivery idempotency identity mismatch")
            return existing
        conn.execute(
            """
            INSERT INTO gateway_file_deliveries (
                id, origin_kind, approval_id, cron_run_id, route_key,
                conversation_id, source_message_id, platform, chat_id,
                reply_to_message_id, thread_id, local_path, display_name,
                size_bytes, sha256, status, attempt_count, created_at, updated_at
            ) VALUES (?, 'cron', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'pending', 0, ?, ?)
            """,
            (
                normalized["id"], normalized["cron_run_id"],
                normalized["route_key"], normalized["conversation_id"],
                normalized["source_message_id"], normalized["platform"],
                normalized["chat_id"], normalized.get("reply_to_message_id"),
                normalized.get("thread_id"), normalized["local_path"],
                normalized["display_name"], normalized["size_bytes"],
                normalized["sha256"], now, now,
            ),
        )
        created = _gateway_file_delivery_row(conn.execute(
            f"SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS} "
            "FROM gateway_file_deliveries WHERE id=?",
            (normalized["id"],),
        ).fetchone())
        if created is None:
            raise DBError("Cron file delivery creation failed")
        return created


def create_gateway_file_delivery(
    conn: sqlite3.Connection,
    delivery: dict,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> dict:
    """在当前 lease 下幂等创建 pending 文件任务，不执行任何平台网络调用。"""
    normalized = _validate_gateway_file_delivery_identity(delivery)
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            raise DBError("gateway runtime lease is not valid")

        existing_row = conn.execute(
            f"""
            SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
            FROM gateway_file_deliveries
            WHERE approval_id=?
            """,
            (normalized["approval_id"],),
        ).fetchone()
        existing = _gateway_file_delivery_row(existing_row)
        if existing is not None:
            for field_name in (
                "id",
                "route_key",
                "conversation_id",
                "source_message_id",
                "platform",
                "chat_id",
                "reply_to_message_id",
                "thread_id",
                "local_path",
                "display_name",
                "size_bytes",
                "sha256",
            ):
                if existing[field_name] != normalized.get(field_name):
                    raise DBError(
                        "gateway file delivery idempotency identity mismatch"
                    )
            return existing

        approval = conn.execute(
            """
            SELECT route_key, conversation_id, source_message_id, tool_name,
                   status
            FROM gateway_approval_requests
            WHERE id=?
            """,
            (normalized["approval_id"],),
        ).fetchone()
        if approval is None or tuple(str(value) for value in approval) != (
            normalized["route_key"],
            normalized["conversation_id"],
            normalized["source_message_id"],
            "gateway_send_file",
            "executing",
        ):
            raise DBError(
                "gateway file delivery is not bound to an executing approval"
            )

        conn.execute(
            """
            INSERT INTO gateway_file_deliveries (
                id, approval_id, route_key, conversation_id,
                source_message_id, platform, chat_id, reply_to_message_id,
                thread_id, local_path, display_name, size_bytes, sha256,
                status, attempt_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                normalized["id"],
                normalized["approval_id"],
                normalized["route_key"],
                normalized["conversation_id"],
                normalized["source_message_id"],
                normalized["platform"],
                normalized["chat_id"],
                normalized.get("reply_to_message_id"),
                normalized.get("thread_id"),
                normalized["local_path"],
                normalized["display_name"],
                normalized["size_bytes"],
                normalized["sha256"],
                now,
                now,
            ),
        )
        row = conn.execute(
            f"""
            SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
            FROM gateway_file_deliveries WHERE id=?
            """,
            (normalized["id"],),
        ).fetchone()
        created = _gateway_file_delivery_row(row)
        if created is None:
            raise DBError("gateway file delivery creation failed")
        return created


def get_gateway_file_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
) -> dict | None:
    """按任务 ID 读取出站文件状态。"""
    row = conn.execute(
        f"""
        SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
        FROM gateway_file_deliveries WHERE id=?
        """,
        (str(delivery_id),),
    ).fetchone()
    return _gateway_file_delivery_row(row)


def claim_gateway_file_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    now: float | None = None,
) -> dict | None:
    """仅由当前 runtime lease 原子 claim 一个待上传任务。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with _immediate_transaction(conn):
        effective_now = time.time() if now is None else float(now)
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=effective_now,
        ):
            return None
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET status='uploading', attempt_count=attempt_count+1,
                next_attempt_at=NULL, claimed_by=?, claim_epoch=?,
                updated_at=?
            WHERE id=?
              AND (
                  status='pending'
                  OR (
                      status='retry_wait'
                      AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                  )
              )
            """,
            (
                fence[1],
                fence[2],
                effective_now,
                str(delivery_id),
                effective_now,
            ),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            f"""
            SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
            FROM gateway_file_deliveries WHERE id=?
            """,
            (str(delivery_id),),
        ).fetchone()
        return _gateway_file_delivery_row(row)


def gateway_file_delivery_claim_is_valid(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    now: float | None = None,
) -> bool:
    """校验 uploading 任务的 claim 与当前未过期 lease 完全一致。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    effective_now = time.time() if now is None else float(now)
    row = conn.execute(
        """
        SELECT 1
        FROM gateway_file_deliveries AS delivery
        JOIN gateway_runtime_lease AS lease
          ON lease.lease_name=?
         AND lease.instance_id=?
         AND lease.lease_epoch=?
         AND lease.expires_at>?
        WHERE delivery.id=? AND delivery.status='uploading'
          AND delivery.claimed_by=? AND delivery.claim_epoch=?
        """,
        (
            fence[0],
            fence[1],
            fence[2],
            effective_now,
            str(delivery_id),
            fence[1],
            fence[2],
        ),
    ).fetchone()
    return row is not None


def get_recoverable_gateway_file_deliveries(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
) -> list[dict]:
    """读取当前可上传或已上传待建 Outbox 的文件任务。"""
    effective_now = time.time() if now is None else float(now)
    rows = conn.execute(
        f"""
        SELECT delivery.*,
               approval.source_event_json
        FROM (
            SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
            FROM gateway_file_deliveries
        ) AS delivery
        LEFT JOIN gateway_approval_requests AS approval
          ON approval.id=delivery.approval_id
        WHERE (
            delivery.status='pending'
            OR (
                delivery.status='retry_wait'
                AND (
                    delivery.next_attempt_at IS NULL
                    OR delivery.next_attempt_at<=?
                )
            )
            OR (
                delivery.status='uploaded'
                AND delivery.platform_file_key IS NOT NULL
                AND delivery.outbox_id IS NULL
                AND (
                    delivery.next_attempt_at IS NULL
                    OR delivery.next_attempt_at<=?
                )
            )
        )
        ORDER BY delivery.created_at, delivery.id
        """,
        (effective_now, effective_now),
    ).fetchall()
    deliveries: list[dict] = []
    for row in rows:
        delivery = _gateway_file_delivery_row(row[:26])
        if delivery is None:
            continue
        delivery["source_event_json"] = (
            str(row[26]) if row[26] is not None else None
        )
        deliveries.append(delivery)
    return deliveries


def mark_gateway_file_delivery_uploaded(
    conn: sqlite3.Connection,
    delivery_id: str,
    platform_file_key: str,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """把平台上传成功事实先于 Outbox 独立持久化。"""
    file_key = str(platform_file_key or "").strip()
    if not file_key:
        raise DBError("platform_file_key must not be empty")
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return False
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET platform_file_key=?, status='uploaded',
                next_attempt_at=NULL, last_error=NULL,
                last_error_code=NULL, claimed_by=NULL, claim_epoch=NULL,
                updated_at=?
            WHERE id=? AND status='uploading'
              AND claimed_by=? AND claim_epoch=?
            """,
            (
                file_key,
                now,
                str(delivery_id),
                fence[1],
                fence[2],
            ),
        )
        return cursor.rowcount == 1


def mark_gateway_file_delivery_retry(
    conn: sqlite3.Connection,
    delivery_id: str,
    error_code: str,
    next_attempt_at: float,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """把当前 claim 的上传故障持久化为可恢复等待。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    safe_code = str(error_code or "upload_failed")[:120]
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return False
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET status='retry_wait', next_attempt_at=?,
                last_error='file upload retry scheduled',
                last_error_code=?, claimed_by=NULL, claim_epoch=NULL,
                updated_at=?
            WHERE id=? AND status='uploading'
              AND claimed_by=? AND claim_epoch=?
            """,
            (
                float(next_attempt_at),
                safe_code,
                now,
                str(delivery_id),
                fence[1],
                fence[2],
            ),
        )
        return cursor.rowcount == 1


def fail_gateway_file_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    error_code: str,
    failure_outbox: dict | None = None,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """原子收敛上传永久失败；提供通知时在同一事务创建 Outbox。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    safe_code = str(error_code or "upload_failed")[:120]
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return False
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET status='permanent_failed', next_attempt_at=NULL,
                last_error='file upload permanently failed',
                last_error_code=?, claimed_by=NULL, claim_epoch=NULL,
                updated_at=?
            WHERE id=? AND status='uploading'
              AND claimed_by=? AND claim_epoch=?
            """,
            (
                safe_code,
                now,
                str(delivery_id),
                fence[1],
                fence[2],
            ),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            """
            UPDATE cron_run_artifacts
            SET delivery_status='permanent_failed', updated_at=?
            WHERE delivery_id=?
            """,
            (now, str(delivery_id)),
        )
        if failure_outbox is not None:
            outbox_id = _insert_gateway_outbox(
                conn,
                failure_outbox,
                lease_name=fence[0],
                instance_id=fence[1],
                lease_epoch=fence[2],
            )
            if outbox_id != str(failure_outbox.get("id", "")):
                raise DBError(
                    "gateway file failure notification identity mismatch"
                )
        return True


def mark_gateway_file_delivery_outbox_retry(
    conn: sqlite3.Connection,
    delivery_id: str,
    error_code: str,
    next_attempt_at: float,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """上传事实保留为 uploaded，只延后 Outbox 创建重试。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    safe_code = str(error_code or "outbox_create_failed")[:120]
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return False
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET next_attempt_at=?, last_error='file Outbox retry scheduled',
                last_error_code=?, updated_at=?
            WHERE id=? AND status='uploaded'
              AND platform_file_key IS NOT NULL AND outbox_id IS NULL
            """,
            (
                float(next_attempt_at),
                safe_code,
                now,
                str(delivery_id),
            ),
        )
        return cursor.rowcount == 1


def create_gateway_file_delivery_outbox(
    conn: sqlite3.Connection,
    delivery_id: str,
    outbox: dict,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> str:
    """复用已保存 file_key 原子创建 Outbox 并推进 outbox_created。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            raise DBError("gateway runtime lease is not valid")
        row = conn.execute(
            f"""
            SELECT {_GATEWAY_FILE_DELIVERY_COLUMNS}
            FROM gateway_file_deliveries WHERE id=?
            """,
            (str(delivery_id),),
        ).fetchone()
        delivery = _gateway_file_delivery_row(row)
        if delivery is None:
            raise DBError("gateway file delivery not found")
        if delivery["status"] == "outbox_created":
            existing_id = delivery.get("outbox_id")
            if not existing_id:
                raise DBError("file delivery Outbox binding is incomplete")
            existing = conn.execute(
                "SELECT 1 FROM gateway_outbox WHERE id=?",
                (existing_id,),
            ).fetchone()
            if existing is None:
                raise DBError("file delivery Outbox is missing")
            return str(existing_id)
        if (
            delivery["status"] != "uploaded"
            or not delivery.get("platform_file_key")
            or delivery.get("outbox_id") is not None
        ):
            raise DBError("gateway file delivery is not ready for Outbox")

        is_cron = delivery.get("origin_kind") == "cron"
        expected_kind = (
            f"cron_file:{delivery['id']}"
            if is_cron else f"file_delivery:{delivery['id']}"
        )
        expected_source = (
            f"cron-file-outbox:{delivery['id']}"
            if is_cron else delivery["id"]
        )
        expected_reply = None if is_cron else delivery["reply_to_message_id"]
        if (
            str(outbox.get("route_key", "")) != delivery["route_key"]
            or str(outbox.get("source_message_id", "")) != expected_source
            or str(outbox.get("platform", "")) != delivery["platform"]
            or str(outbox.get("chat_id", "")) != delivery["chat_id"]
            or outbox.get("reply_to_message_id") != expected_reply
            or outbox.get("thread_id") != delivery["thread_id"]
            or str(outbox.get("delivery_kind", "")) != expected_kind
        ):
            raise DBError("gateway file Outbox identity mismatch")
        payloads = outbox.get("payloads")
        if not isinstance(payloads, list) or len(payloads) != 1:
            raise DBError("gateway file Outbox must contain one payload")
        payload = payloads[0]
        if not isinstance(payload, dict) or payload.get("msg_type") != "file":
            raise DBError("gateway file Outbox payload is invalid")
        try:
            content = json.loads(str(payload.get("content", "")))
        except (TypeError, ValueError) as exc:
            raise DBError("gateway file Outbox content is invalid") from exc
        if (
            not isinstance(content, dict)
            or content.get("file_key") != delivery["platform_file_key"]
        ):
            raise DBError("gateway file Outbox file_key binding mismatch")

        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=fence[0],
            instance_id=fence[1],
            lease_epoch=fence[2],
        )
        stored_outbox = conn.execute(
            """
            SELECT id, event_json, platform, chat_id,
                   reply_to_message_id, thread_id, delivery_kind,
                   payloads_json
            FROM gateway_outbox WHERE id=?
            """,
            (outbox_id,),
        ).fetchone()
        expected_outbox = (
            str(outbox["id"]),
            str(outbox["event_json"]),
            str(outbox["platform"]),
            str(outbox["chat_id"]),
            outbox.get("reply_to_message_id"),
            outbox.get("thread_id"),
            str(outbox["delivery_kind"]),
            _serialize_gateway_json(payloads, "payloads"),
        )
        if stored_outbox is None or tuple(stored_outbox) != expected_outbox:
            raise DBError("gateway file Outbox idempotency mismatch")
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET status='outbox_created', outbox_id=?,
                next_attempt_at=NULL, last_error=NULL,
                last_error_code=NULL, claimed_by=NULL, claim_epoch=NULL,
                updated_at=?
            WHERE id=? AND status='uploaded' AND outbox_id IS NULL
            """,
            (outbox_id, now, delivery["id"]),
        )
        if cursor.rowcount != 1:
            raise DBError("gateway file delivery Outbox transition failed")
        return outbox_id


def reset_gateway_uploading_file_deliveries(
    conn: sqlite3.Connection,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> int:
    """新 lease 接管时按 file_key 边界恢复中断的 uploading。"""
    fence = _gateway_file_delivery_fence(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with _immediate_transaction(conn):
        now = time.time()
        if not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return 0
        cursor = conn.execute(
            """
            UPDATE gateway_file_deliveries
            SET status=CASE
                    WHEN platform_file_key IS NULL THEN 'retry_wait'
                    ELSE 'uploaded'
                END,
                next_attempt_at=CASE
                    WHEN platform_file_key IS NULL THEN ?
                    ELSE NULL
                END,
                last_error='upload interrupted before completion',
                last_error_code='gateway_restart',
                claimed_by=NULL, claim_epoch=NULL, updated_at=?
            WHERE status='uploading'
            """,
            (now, now),
        )
        return cursor.rowcount


