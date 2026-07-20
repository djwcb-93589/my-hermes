from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .database import (
    DBError,
    GATEWAY_FILE_DELIVERY_STATUSES,
    InvalidMessageError,
    transaction,
)
from .core import _insert_message
from .gateway import (
    _finish_gateway_queue_for_delivery, _gateway_outbox_claim_clause,
    _gateway_outbox_fence_values, _gateway_outbox_lease_clause,
    _gateway_lease_epoch_value, _mark_gateway_message_delivery_terminal,
    _set_gateway_queue_status,
    _gateway_terminal_outbox_row, _immediate_transaction,
    _infer_cancelled_gateway_outbox_status, _insert_gateway_outbox,
    _serialize_gateway_json, _update_gateway_outbox_ownership_status,
    gateway_outbox_claim_is_valid, gateway_runtime_lease_is_valid,
)

_GATEWAY_FILE_DELIVERY_COLUMNS = (
    "delivery_id, route_key, source_message_id, queue_message_id, outbox_id, platform, "
    "chat_id, reply_to_message_id, thread_id, local_path, display_name, status, "
    "attempt_count, next_attempt_at, last_error, last_error_code, claimed_by, "
    "claim_epoch, created_at, updated_at"
)

def reconcile_gateway_terminal_deliveries(
    conn: sqlite3.Connection,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> int:
    """收敛旧版本遗留的终态 Outbox 与 ``reply_pending`` queue。

    新代码通过统一终态函数一次提交三层状态；这里仅修复升级前已经形成的
    孤儿记录，以及“取消先提交、最后一个平台成功随后落进度”留下的可推导
    状态。终态审计行不会被删除。
    """
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    manager = (
        _immediate_transaction(conn)
        if fence is not None
        else transaction(conn)
    )
    reconciled = 0
    with manager:
        now = time.time()
        if fence is not None and not gateway_runtime_lease_is_valid(
            conn,
            fence[0],
            fence[1],
            fence[2],
            now=now,
        ):
            return 0
        rows = conn.execute(
            """
            SELECT o.id, o.route_key, o.source_message_id,
                   o.queue_message_id, o.status,
                   o.next_chunk_index, o.payloads_json,
                   q.status, o.event_json
            FROM gateway_outbox AS o
            LEFT JOIN gateway_message_queue AS q
              ON q.route_key=o.route_key
             AND q.message_id=o.queue_message_id
            WHERE (
                o.status IN (
                    'delivered', 'permanent_failed',
                    'cancelled', 'partial_cancelled'
                )
                AND q.status='reply_pending'
            )
            OR (o.status='cancelled' AND o.next_chunk_index > 0)
            OR (
                o.status='partial_cancelled'
                AND json_valid(o.payloads_json)=1
                AND o.next_chunk_index >= json_array_length(o.payloads_json)
            )
            ORDER BY o.created_at, o.id
            """
        ).fetchall()
        for (
            outbox_id,
            route_key,
            source_message_id,
            queue_message_id,
            stored_status,
            next_chunk_index,
            payloads_json,
            queue_status,
            event_json,
        ) in rows:
            status = str(stored_status)
            if status in {"cancelled", "partial_cancelled"}:
                status = _infer_cancelled_gateway_outbox_status(
                    int(next_chunk_index),
                    str(payloads_json),
                )
            if status != stored_status:
                lease_clause, lease_params = _gateway_outbox_lease_clause(
                    fence,
                    now,
                )
                claim_assignment = ""
                claim_params: tuple = ()
                if fence is not None:
                    claim_assignment = ", claimed_by=?, claim_epoch=?"
                    claim_params = (fence[1], fence[2])
                cursor = conn.execute(
                    f"""
                    UPDATE gateway_outbox
                    SET status=?, next_attempt_at=NULL,
                        last_error=CASE WHEN ?='delivered' THEN NULL
                                        ELSE last_error END,
                        last_error_code=CASE WHEN ?='delivered' THEN NULL
                                             ELSE last_error_code END,
                        updated_at=? {claim_assignment}
                    WHERE id=? AND status=?
                    {lease_clause}
                    """,
                    (
                        status,
                        status,
                        status,
                        now,
                        *claim_params,
                        outbox_id,
                        stored_status,
                        *lease_params,
                    ),
                )
                if cursor.rowcount <= 0:
                    continue

            _mark_gateway_message_delivery_terminal(
                conn,
                str(outbox_id),
                status,
                now,
                route_key=str(route_key),
                source_message_id=str(source_message_id),
            )
            _update_gateway_outbox_ownership_status(
                conn,
                outbox_id=str(outbox_id),
                route_key=str(route_key),
                source_message_id=str(source_message_id),
                event_json=str(event_json),
                status=status,
                updated_at=now,
            )
            _sync_gateway_file_delivery_terminal(
                conn,
                str(outbox_id),
                status,
                now,
            )
            if queue_status == "reply_pending":
                if status == "permanent_failed":
                    _finish_gateway_queue_for_delivery(
                        conn,
                        str(route_key),
                        str(queue_message_id),
                        status="delivery_failed",
                        now=now,
                    )
                else:
                    _finish_gateway_queue_for_delivery(
                        conn,
                        str(route_key),
                        str(queue_message_id),
                        status=(
                            "cancelled"
                            if status in {"cancelled", "partial_cancelled"}
                            else "completed"
                        ),
                        now=now,
                    )
            reconciled += 1
    return reconciled


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
) -> str:
    """原子写入最终 assistant 消息、outbox 和 reply_pending 状态。"""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        raise InvalidMessageError(
            "gateway final delivery must reference an assistant message"
        )
    with transaction(conn):
        queue_message_id = str(
            outbox.get("queue_message_id") or outbox["source_message_id"]
        )
        existing = conn.execute(
            """
            SELECT id, queue_message_id
            FROM gateway_outbox
            WHERE route_key=? AND source_message_id=? AND delivery_kind=?
            """,
            (
                str(outbox["route_key"]),
                str(outbox["source_message_id"]),
                str(outbox["delivery_kind"]),
            ),
        ).fetchone()
        if existing is not None:
            if str(existing[1]) != queue_message_id:
                raise DBError("gateway final outbox queue identity mismatch")
            _set_gateway_queue_status(
                conn,
                str(outbox["route_key"]),
                queue_message_id,
                "reply_pending",
                time.time(),
            )
            return str(existing[0])

        assistant_message_id = _insert_message(conn, session_id, msg)
        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        _insert_gateway_message_delivery(
            conn,
            delivery_id=outbox_id,
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
    return outbox_id


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


def _sync_gateway_file_delivery_terminal(
    conn: sqlite3.Connection,
    outbox_id: str,
    outbox_status: str,
    now: float,
    *,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    """在 Outbox 终态事务内同步关联文件任务，旧 Outbox 无关联即跳过。"""
    if outbox_status == "delivered":
        file_status = "delivered"
        safe_error = None
        safe_code = None
    elif outbox_status == "permanent_failed":
        file_status = "permanent_failed"
        safe_error = str(error or "file message delivery failed")[:120]
        safe_code = str(error_code or "outbox_permanent_failed")[:120]
    elif outbox_status in {"cancelled", "partial_cancelled"}:
        file_status = "cancelled"
        safe_error = "file message delivery cancelled"
        safe_code = outbox_status
    else:
        return
    conn.execute(
        """
        UPDATE gateway_file_deliveries
        SET status=?, next_attempt_at=NULL, last_error=?,
            last_error_code=?, claimed_by=NULL, claim_epoch=NULL,
            updated_at=?
        WHERE outbox_id=? AND status='outbox_created'
        """,
        (
            file_status,
            safe_error,
            safe_code,
            float(now),
            str(outbox_id),
        ),
    )
    conn.execute(
        """
        UPDATE cron_run_artifacts
        SET delivery_status=?, updated_at=?
        WHERE delivery_id IN (
            SELECT id FROM gateway_file_deliveries WHERE outbox_id=?
        )
        """,
        (file_status, float(now), str(outbox_id)),
    )

