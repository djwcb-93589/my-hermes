from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .database import DBError, _cleanup_batch_limit, _immediate_transaction, transaction
from .schema import LATEST_SCHEMA_VERSION


_GATEWAY_OUTBOX_COLUMNS = (
    "id, route_key, source_message_id, queue_message_id, event_json, platform, chat_id, "
    "reply_to_message_id, thread_id, delivery_kind, payloads_json, next_chunk_index, "
    "message_ids_json, status, attempt_count, next_attempt_at, last_error, "
    "last_error_code, claimed_by, claim_epoch"
)

def gateway_event_source_message_ids(
    event_json: str,
    fallback_message_id: str,
) -> list[str]:
    """提取 Gateway event 对应的全部原始平台消息 ID。"""
    try:
        payload = json.loads(event_json)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway event JSON deserialization failed") from exc
    if not isinstance(payload, dict):
        raise DBError("gateway event JSON must contain an object")

    metadata = payload.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise DBError("gateway event metadata must contain an object")
    source_message_ids = metadata.get("source_message_ids", [])
    if source_message_ids is None:
        source_message_ids = []
    if not isinstance(source_message_ids, list):
        raise DBError("gateway event source_message_ids must be a list")

    result: list[str] = []
    seen: set[str] = set()

    def append_message_id(value) -> None:
        if value is None:
            return
        message_id = str(value)
        if not message_id or message_id in seen:
            return
        seen.add(message_id)
        result.append(message_id)

    append_message_id(payload.get("message_id"))
    # 旧记录可能没有 message_id 字段，数据库列中的主消息 ID 必须保留。
    append_message_id(fallback_message_id)
    for source_message_id in source_message_ids:
        append_message_id(source_message_id)

    if not result:
        raise DBError("gateway event has no source message id")
    return result


def _upsert_gateway_source_message_ownership(
    conn: sqlite3.Connection,
    route_key: str,
    source_message_ids: list[str],
    *,
    owner_kind: str,
    owner_id: str,
    status: str,
    created_at: float,
    updated_at: float,
) -> None:
    """写入 ownership；Outbox 可接管 Queue，同一 Queue 只能刷新自身。"""
    if owner_kind not in {"queue", "outbox"}:
        raise DBError(f"invalid gateway ownership kind: {owner_kind}")
    if not source_message_ids:
        raise DBError("gateway ownership requires source message ids")

    if owner_kind == "queue":
        conflict_sql = """
            ON CONFLICT(route_key, source_message_id) DO UPDATE SET
                status=excluded.status,
                updated_at=excluded.updated_at
            WHERE gateway_source_message_ownership.owner_kind='queue'
              AND gateway_source_message_ownership.owner_id=excluded.owner_id
        """
    else:
        conflict_sql = """
            ON CONFLICT(route_key, source_message_id) DO UPDATE SET
                owner_kind=excluded.owner_kind,
                owner_id=excluded.owner_id,
                status=excluded.status,
                updated_at=excluded.updated_at
        """

    for source_message_id in source_message_ids:
        conn.execute(
            f"""
            INSERT INTO gateway_source_message_ownership (
                route_key, source_message_id, owner_kind, owner_id, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            {conflict_sql}
            """,
            (
                route_key,
                source_message_id,
                owner_kind,
                owner_id,
                status,
                float(created_at),
                float(updated_at),
            ),
        )


def prune_gateway_terminal_ownership(
    conn: sqlite3.Connection,
    *,
    updated_before: float,
    limit: int = 200,
) -> int:
    """分批删除无 Queue/Outbox 引用的终态 ownership。"""
    batch_limit = _cleanup_batch_limit(limit, "gateway ownership")
    cutoff = float(updated_before)
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT ownership.route_key, ownership.source_message_id
            FROM gateway_source_message_ownership AS ownership
            WHERE ownership.updated_at < ?
              AND ownership.status IN (
                  'completed', 'cancelled', 'delivery_failed', 'delivered',
                  'partial_cancelled', 'permanent_failed'
              )
              AND (
                  (
                      ownership.owner_kind='queue'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM gateway_message_queue AS queue
                          WHERE queue.route_key=ownership.route_key
                            AND queue.message_id=ownership.owner_id
                      )
                  )
                  OR (
                      ownership.owner_kind='outbox'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM gateway_outbox AS outbox
                          WHERE outbox.id=ownership.owner_id
                      )
                  )
              )
            ORDER BY ownership.updated_at, ownership.route_key,
                     ownership.source_message_id
            LIMIT ?
            """,
            (cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for route_key, source_message_id in rows:
            cursor = conn.execute(
                """
                DELETE FROM gateway_source_message_ownership
                WHERE route_key=? AND source_message_id=?
                  AND updated_at < ?
                  AND status IN (
                      'completed', 'cancelled', 'delivery_failed',
                      'delivered', 'partial_cancelled', 'permanent_failed'
                  )
                """,
                (str(route_key), str(source_message_id), cutoff),
            )
            removed += cursor.rowcount
    return removed


def prune_gateway_terminal_outbox(
    conn: sqlite3.Connection,
    *,
    updated_before: float,
    limit: int = 200,
) -> int:
    """分批删除安全的终态 Outbox 审计，并保持消息可见性语义。"""
    batch_limit = _cleanup_batch_limit(limit, "gateway Outbox")
    cutoff = float(updated_before)
    terminal_statuses = (
        "delivered",
        "cancelled",
        "partial_cancelled",
        "permanent_failed",
    )
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT outbox.id, outbox.route_key, outbox.source_message_id,
                   outbox.queue_message_id
            FROM gateway_outbox AS outbox
            WHERE outbox.updated_at < ?
              AND outbox.status IN (
                  'delivered', 'cancelled', 'partial_cancelled',
                  'permanent_failed'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM gateway_message_queue AS queue
                  WHERE queue.route_key=outbox.route_key
                    AND queue.message_id=outbox.queue_message_id
                    AND queue.status IN (
                        'queued', 'processing', 'reply_pending'
                    )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM gateway_message_deliveries AS delivery
                  WHERE delivery.delivery_id=outbox.id
                    AND delivery.status != outbox.status
              )
            ORDER BY outbox.updated_at, outbox.id
            LIMIT ?
            """,
            (cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for outbox_id, route_key, source_message_id, queue_message_id in rows:
            delivery = conn.execute(
                """
                SELECT assistant_message_id, status
                FROM gateway_message_deliveries
                WHERE delivery_id=?
                """,
                (str(outbox_id),),
            ).fetchone()
            if delivery is not None and str(delivery[1]) != "delivered":
                # 未送达回答原本由 delivery 状态隐藏；删除审计前同时删除
                # 对应 assistant，避免历史读取时错误地重新显示。
                conn.execute(
                    "DELETE FROM messages WHERE id=?",
                    (int(delivery[0]),),
                )
            conn.execute(
                "DELETE FROM gateway_message_deliveries WHERE delivery_id=?",
                (str(outbox_id),),
            )
            conn.execute(
                """
                DELETE FROM gateway_message_queue
                WHERE route_key=? AND message_id=?
                  AND status='delivery_failed'
                """,
                (str(route_key), str(queue_message_id)),
            )
            cursor = conn.execute(
                """
                DELETE FROM gateway_outbox
                WHERE id=? AND updated_at < ?
                  AND status IN (?, ?, ?, ?)
                """,
                (str(outbox_id), cutoff, *terminal_statuses),
            )
            removed += cursor.rowcount
    return removed


def _gateway_runtime_lease_values(
    lease_name: str,
    instance_id: str,
    ttl_seconds: float,
) -> tuple[str, str, float]:
    """校验租约标识和 TTL，避免写入不可接管的无效记录。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    if isinstance(ttl_seconds, bool):
        raise DBError("gateway runtime lease ttl must be greater than 0")
    try:
        ttl = float(ttl_seconds)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway runtime lease ttl must be a number") from exc
    if ttl <= 0:
        raise DBError("gateway runtime lease ttl must be greater than 0")
    return lease_name, instance_id, ttl


def _gateway_lease_epoch_value(lease_epoch: int) -> int:
    """校验 fencing epoch，布尔值不能伪装成整数世代。"""
    if isinstance(lease_epoch, bool):
        raise DBError("gateway runtime lease_epoch must be greater than 0")
    try:
        normalized = int(lease_epoch)
    except (TypeError, ValueError) as exc:
        raise DBError("gateway runtime lease_epoch must be an integer") from exc
    if normalized <= 0:
        raise DBError("gateway runtime lease_epoch must be greater than 0")
    return normalized


def acquire_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    ttl_seconds: float,
) -> dict | None:
    """原子获取或接管租约，成功返回实例身份和单调 fencing epoch。"""
    lease_name, instance_id, ttl = _gateway_runtime_lease_values(
        lease_name,
        instance_id,
        ttl_seconds,
    )
    with _immediate_transaction(conn):
        # BEGIN IMMEDIATE 可能等待其他写事务；拿到写锁后再取时间，避免
        # 用等待前的旧时间错误续活或接管 lease。
        now = time.time()
        row = conn.execute(
            """
            SELECT instance_id, lease_epoch, expires_at
            FROM gateway_runtime_lease
            WHERE lease_name=?
            """,
            (lease_name,),
        ).fetchone()
        if row is None:
            lease_epoch = 1
            conn.execute(
                """
                INSERT INTO gateway_runtime_lease (
                    lease_name, instance_id, lease_epoch,
                    heartbeat_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (lease_name, instance_id, lease_epoch, now, now + ttl),
            )
        else:
            current_instance = str(row[0])
            current_epoch = _gateway_lease_epoch_value(row[1])
            current_expires_at = float(row[2])
            if current_instance == instance_id and current_expires_at > now:
                lease_epoch = current_epoch
            elif current_expires_at <= now:
                lease_epoch = current_epoch + 1
            else:
                return None
            cursor = conn.execute(
                """
                UPDATE gateway_runtime_lease
                SET instance_id=?, lease_epoch=?, heartbeat_at=?, expires_at=?
                WHERE lease_name=? AND instance_id=? AND lease_epoch=?
                """,
                (
                    instance_id,
                    lease_epoch,
                    now,
                    now + ttl,
                    lease_name,
                    current_instance,
                    current_epoch,
                ),
            )
            if cursor.rowcount != 1:
                return None
    return {
        "instance_id": instance_id,
        "lease_epoch": lease_epoch,
    }


def renew_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    ttl_seconds: float,
) -> bool:
    """仅允许当前 owner 和 epoch 续租，续租不改变 epoch。"""
    lease_name, instance_id, ttl = _gateway_runtime_lease_values(
        lease_name,
        instance_id,
        ttl_seconds,
    )
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    with _immediate_transaction(conn):
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?, expires_at=?
            WHERE lease_name=? AND instance_id=? AND lease_epoch=?
              AND expires_at > ?
            """,
            (
                now,
                now + ttl,
                lease_name,
                instance_id,
                normalized_epoch,
                now,
            ),
        )
        return cursor.rowcount == 1


def release_gateway_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
) -> bool:
    """仅让当前 epoch 立即过期，并保留世代供下次接管递增。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    with _immediate_transaction(conn):
        now = time.time()
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?, expires_at=0
            WHERE lease_name=? AND instance_id=? AND lease_epoch=?
            """,
            (
                now,
                lease_name,
                instance_id,
                normalized_epoch,
            ),
        )
        return cursor.rowcount == 1


def gateway_runtime_lease_is_valid(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    *,
    now: float | None = None,
) -> bool:
    """同时校验 lease owner、epoch 和有效期。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    timestamp = time.time() if now is None else float(now)
    row = conn.execute(
        """
        SELECT 1
        FROM gateway_runtime_lease
        WHERE lease_name=? AND instance_id=? AND lease_epoch=?
          AND expires_at>?
        """,
        (lease_name, instance_id, normalized_epoch, timestamp),
    ).fetchone()
    return row is not None


def check_gateway_runtime_readiness(
    conn: sqlite3.Connection,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    *,
    now: float | None = None,
) -> bool:
    """在一个轻量写事务内检查数据库可读写且当前 fencing lease 有效。"""
    if not isinstance(lease_name, str) or not lease_name:
        raise DBError("gateway runtime lease_name must not be empty")
    if not isinstance(instance_id, str) or not instance_id:
        raise DBError("gateway runtime instance_id must not be empty")
    normalized_epoch = _gateway_lease_epoch_value(lease_epoch)
    timestamp = time.time() if now is None else float(now)
    with _immediate_transaction(conn):
        schema_row = conn.execute(
            "SELECT 1 FROM schema_version WHERE version=?",
            (LATEST_SCHEMA_VERSION,),
        ).fetchone()
        if schema_row is None:
            return False
        # 只刷新观测时间，不延长 expires_at；健康请求不能替代正式续租。
        cursor = conn.execute(
            """
            UPDATE gateway_runtime_lease
            SET heartbeat_at=?
            WHERE lease_name=? AND instance_id=? AND lease_epoch=?
              AND expires_at>?
            """,
            (
                timestamp,
                lease_name,
                instance_id,
                normalized_epoch,
                timestamp,
            ),
        )
        return cursor.rowcount == 1


def _gateway_outbox_fence_values(
    lease_name: str | None,
    instance_id: str | None,
    lease_epoch: int | None,
) -> tuple[str, str, int] | None:
    """规范化可选 fencing；全空仅保留旧嵌入式调用兼容。"""
    values = (lease_name, instance_id, lease_epoch)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise DBError("gateway Outbox fencing identity is incomplete")
    normalized_name = str(lease_name or "")
    normalized_instance = str(instance_id or "")
    if not normalized_name or not normalized_instance:
        raise DBError("gateway Outbox fencing identity must not be empty")
    return (
        normalized_name,
        normalized_instance,
        _gateway_lease_epoch_value(lease_epoch),
    )


def gateway_outbox_claim_is_valid(
    conn: sqlite3.Connection,
    outbox_id: str,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    *,
    now: float | None = None,
) -> bool:
    """确认数据库租约和 Outbox claim 同时属于当前 fencing 身份。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    assert fence is not None
    timestamp = time.time() if now is None else float(now)
    row = conn.execute(
        """
        SELECT 1
        FROM gateway_outbox AS outbox
        WHERE outbox.id=?
          AND outbox.status='sending'
          AND outbox.claimed_by=?
          AND outbox.claim_epoch=?
          AND EXISTS (
              SELECT 1
              FROM gateway_runtime_lease AS lease
              WHERE lease.lease_name=?
                AND lease.instance_id=?
                AND lease.lease_epoch=?
                AND lease.expires_at>?
          )
        """,
        (
            outbox_id,
            fence[1],
            fence[2],
            fence[0],
            fence[1],
            fence[2],
            timestamp,
        ),
    ).fetchone()
    return row is not None


def get_gateway_conversation_id(
    conn: sqlite3.Connection,
    route_key: str,
) -> str | None:
    """读取 route_key 当前指向的 conversation_id。没有映射时返回 None。"""
    row = conn.execute(
        "SELECT conversation_id FROM gateway_session_routes WHERE route_key=?",
        (route_key,),
    ).fetchone()
    return str(row[0]) if row else None


def set_gateway_conversation_id(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> None:
    """原子更新当前映射，并登记该 route 的历史对话归属。"""
    now = time.time()
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO gateway_session_routes
                (route_key, conversation_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(route_key) DO UPDATE SET
                conversation_id=excluded.conversation_id,
                updated_at=excluded.updated_at
            """,
            (route_key, conversation_id, now),
        )
        conn.execute(
            """
            INSERT INTO gateway_route_conversations (
                route_key, conversation_id, created_at, last_selected_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(route_key, conversation_id) DO UPDATE SET
                last_selected_at=excluded.last_selected_at
            """,
            (route_key, conversation_id, now, now),
        )


def _gateway_conversation_summary(row: sqlite3.Row | tuple) -> dict:
    """把固定列顺序的对话摘要查询结果转换成上层稳定结构。"""
    return {
        "conversation_id": str(row[0]),
        "message_count": int(row[1] or 0),
        "last_message_at": (
            float(row[2]) if row[2] is not None else None
        ),
        "last_selected_at": float(row[3]),
        "preview": str(row[4] or ""),
        "is_current": bool(row[5]),
    }


def list_gateway_conversations(
    conn: sqlite3.Connection,
    route_key: str,
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    """列出单条 route 最近的对话；查询边界不能跨越 route_key。"""
    normalized_limit = max(1, min(10, int(limit)))
    normalized_offset = max(0, int(offset))
    rows = conn.execute(
        """
        WITH route_conversations AS (
            SELECT
                route_key,
                conversation_id,
                last_selected_at
            FROM gateway_route_conversations
            WHERE route_key=?
        ),
        user_stats AS (
            SELECT
                message.session_id,
                COUNT(*) AS message_count,
                MAX(message.timestamp) AS last_message_at
            FROM messages AS message
            INNER JOIN route_conversations AS route_conversation
                ON route_conversation.conversation_id=message.session_id
            WHERE message.role='user'
            GROUP BY message.session_id
        )
        SELECT
            route_conversation.conversation_id,
            COALESCE(user_stat.message_count, 0),
            user_stat.last_message_at,
            route_conversation.last_selected_at,
            COALESCE((
                SELECT message.content
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
                  AND TRIM(
                      COALESCE(message.content, ''),
                      char(9) || char(10) || char(13) || ' '
                  )<>''
                ORDER BY message.timestamp DESC, message.id DESC
                LIMIT 1
            ), ''),
            CASE
                WHEN current_route.conversation_id=
                     route_conversation.conversation_id
                THEN 1 ELSE 0
            END
        FROM route_conversations AS route_conversation
        LEFT JOIN user_stats AS user_stat
            ON user_stat.session_id=route_conversation.conversation_id
        LEFT JOIN gateway_session_routes AS current_route
            ON current_route.route_key=route_conversation.route_key
        ORDER BY
            COALESCE(
                user_stat.last_message_at,
                route_conversation.last_selected_at
            ) DESC,
            route_conversation.last_selected_at DESC,
            route_conversation.conversation_id ASC
        LIMIT ? OFFSET ?
        """,
        (route_key, normalized_limit, normalized_offset),
    ).fetchall()
    return [_gateway_conversation_summary(row) for row in rows]


def get_gateway_conversation_for_route(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> dict | None:
    """按 route 校验并读取对话摘要；找不到时不泄露跨 route 信息。"""
    row = conn.execute(
        """
        SELECT
            route_conversation.conversation_id,
            (
                SELECT COUNT(*)
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
            ),
            (
                SELECT MAX(message.timestamp)
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
            ),
            route_conversation.last_selected_at,
            COALESCE((
                SELECT message.content
                FROM messages AS message
                WHERE message.session_id=route_conversation.conversation_id
                  AND message.role='user'
                  AND TRIM(
                      COALESCE(message.content, ''),
                      char(9) || char(10) || char(13) || ' '
                  )<>''
                ORDER BY message.timestamp DESC, message.id DESC
                LIMIT 1
            ), ''),
            CASE
                WHEN current_route.conversation_id=
                     route_conversation.conversation_id
                THEN 1 ELSE 0
            END
        FROM gateway_route_conversations AS route_conversation
        LEFT JOIN gateway_session_routes AS current_route
            ON current_route.route_key=route_conversation.route_key
        WHERE route_conversation.route_key=?
          AND route_conversation.conversation_id=?
        """,
        (route_key, conversation_id),
    ).fetchone()
    return _gateway_conversation_summary(row) if row is not None else None


def delete_gateway_conversation_for_route(
    conn: sqlite3.Connection,
    route_key: str,
    conversation_id: str,
) -> dict[str, str]:
    """按 route 原子删除非当前对话及其关联消息。"""
    with transaction(conn):
        route_conversation = conn.execute(
            """
            SELECT 1
            FROM gateway_route_conversations
            WHERE route_key=? AND conversation_id=?
            """,
            (route_key, conversation_id),
        ).fetchone()
        if route_conversation is None:
            return {"outcome": "not_found"}

        current_route = conn.execute(
            """
            SELECT 1
            FROM gateway_session_routes
            WHERE route_key=? AND conversation_id=?
            """,
            (route_key, conversation_id),
        ).fetchone()
        if current_route is not None:
            return {"outcome": "current"}

        session = conn.execute(
            "SELECT 1 FROM sessions WHERE id=?",
            (conversation_id,),
        ).fetchone()
        if session is None:
            return {"outcome": "not_found"}

        conn.execute(
            """
            DELETE FROM gateway_route_conversations
            WHERE route_key=? AND conversation_id=?
            """,
            (route_key, conversation_id),
        )
        deleted = conn.execute(
            "DELETE FROM sessions WHERE id=?",
            (conversation_id,),
        )
        if deleted.rowcount != 1:
            raise DBError("gateway conversation deletion failed")
    return {"outcome": "deleted"}


def _enqueue_gateway_message_in_transaction(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    event_json: str,
    *,
    task_kind: str,
    approval_id: str | None,
) -> bool:
    """在调用方事务内写入 Runner queue 与原始消息归属。"""
    if task_kind not in {"external", "approval_resume"}:
        raise DBError("invalid gateway queue task kind")
    if task_kind == "approval_resume" and not approval_id:
        raise DBError("approval resume queue task is missing approval id")
    if task_kind == "external" and approval_id is not None:
        raise DBError("external gateway queue task cannot bind an approval")

    incoming_source_ids = gateway_event_source_message_ids(
        event_json,
        message_id,
    )
    now = time.time()
    placeholders = ",".join("?" for _ in incoming_source_ids)
    existing_owners = conn.execute(
        f"""
        SELECT source_message_id, owner_kind, owner_id
        FROM gateway_source_message_ownership
        WHERE route_key=?
          AND source_message_id IN ({placeholders})
        """,
        (route_key, *incoming_source_ids),
    ).fetchall()
    for _source_message_id, owner_kind, owner_id in existing_owners:
        if str(owner_kind) == "outbox" or str(owner_id) != message_id:
            return False

    conn.execute(
        """
        INSERT OR IGNORE INTO gateway_message_queue (
            route_key, message_id, event_json, status, task_kind,
            approval_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
        """,
        (
            route_key,
            message_id,
            event_json,
            task_kind,
            approval_id,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT event_json, status, created_at, updated_at
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        raise DBError("gateway queue insert did not create a row")
    source_message_ids = gateway_event_source_message_ids(
        str(row[0]),
        message_id,
    )
    _upsert_gateway_source_message_ownership(
        conn,
        route_key,
        source_message_ids,
        owner_kind="queue",
        owner_id=message_id,
        status=str(row[1]),
        created_at=float(row[2]),
        updated_at=float(row[3]),
    )
    return True


def enqueue_gateway_message(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    event_json: str,
) -> bool:
    """原子写入外部 Runner queue 与全部原始消息的 Queue 归属。"""
    with transaction(conn):
        return _enqueue_gateway_message_in_transaction(
            conn,
            route_key,
            message_id,
            event_json,
            task_kind="external",
            approval_id=None,
        )


def _update_gateway_source_message_ownership_status(
    conn: sqlite3.Connection,
    *,
    route_key: str,
    event_json: str,
    fallback_message_id: str,
    owner_kind: str,
    owner_id: str,
    status: str,
    updated_at: float,
) -> None:
    """只更新仍属于指定 owner 的原始消息，避免旧任务覆盖新所有者。"""
    source_message_ids = gateway_event_source_message_ids(
        event_json,
        fallback_message_id,
    )
    for source_message_id in source_message_ids:
        conn.execute(
            """
            UPDATE gateway_source_message_ownership
            SET status=?, updated_at=?
            WHERE route_key=? AND source_message_id=?
              AND owner_kind=? AND owner_id=?
            """,
            (
                status,
                float(updated_at),
                route_key,
                source_message_id,
                owner_kind,
                owner_id,
            ),
        )


def _set_gateway_queue_status(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
    status: str,
    now: float,
) -> bool:
    """更新 Queue 及其原始消息归属；调用方负责事务。"""
    row = conn.execute(
        """
        SELECT event_json
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        return False
    cursor = conn.execute(
        """
        UPDATE gateway_message_queue
        SET status=?, updated_at=?
        WHERE route_key=? AND message_id=?
        """,
        (status, now, route_key, message_id),
    )
    if cursor.rowcount <= 0:
        return False
    _update_gateway_source_message_ownership_status(
        conn,
        route_key=route_key,
        event_json=str(row[0]),
        fallback_message_id=message_id,
        owner_kind="queue",
        owner_id=message_id,
        status=status,
        updated_at=now,
    )
    return True


def mark_gateway_message_processing(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """标记消息已由当前 worker 开始处理。"""
    with transaction(conn):
        _set_gateway_queue_status(
            conn,
            route_key,
            message_id,
            "processing",
            time.time(),
        )


def mark_gateway_message_reply_pending(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """模型已完成,入站消息等待对应 outbox 完整送达。"""
    with transaction(conn):
        _set_gateway_queue_status(
            conn,
            route_key,
            message_id,
            "reply_pending",
            time.time(),
        )


def mark_gateway_message_delivery_failed(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """出站永久失败后保留入站审计记录,但不再重新调用模型。"""
    with transaction(conn):
        _set_gateway_queue_status(
            conn,
            route_key,
            message_id,
            "delivery_failed",
            time.time(),
        )


def complete_gateway_message(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> None:
    """删除 Queue 行，但保留 completed ownership 作为长期去重事实。"""
    with transaction(conn):
        row = conn.execute(
            """
            SELECT event_json
            FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, message_id),
        ).fetchone()
        if row is None:
            return
        now = time.time()
        _update_gateway_source_message_ownership_status(
            conn,
            route_key=route_key,
            event_json=str(row[0]),
            fallback_message_id=message_id,
            owner_kind="queue",
            owner_id=message_id,
            status="completed",
            updated_at=now,
        )
        conn.execute(
            """
            DELETE FROM gateway_message_queue
            WHERE route_key=? AND message_id=?
            """,
            (route_key, message_id),
        )


def delete_gateway_messages(
    conn: sqlite3.Connection,
    route_key: str,
    message_ids: list[str],
) -> None:
    """删除被 /new 取消的 Queue 行，并保留 cancelled ownership。"""
    if not message_ids:
        return
    placeholders = ",".join("?" for _ in message_ids)
    with transaction(conn):
        rows = conn.execute(
            f"""
            SELECT message_id, event_json
            FROM gateway_message_queue
            WHERE route_key=? AND message_id IN ({placeholders})
            """,
            (route_key, *message_ids),
        ).fetchall()
        now = time.time()
        for stored_message_id, event_json in rows:
            _update_gateway_source_message_ownership_status(
                conn,
                route_key=route_key,
                event_json=str(event_json),
                fallback_message_id=str(stored_message_id),
                owner_kind="queue",
                owner_id=str(stored_message_id),
                status="cancelled",
                updated_at=now,
            )
        conn.execute(
            f"""
            DELETE FROM gateway_message_queue
            WHERE route_key=? AND message_id IN ({placeholders})
            """,
            (route_key, *message_ids),
        )


def reset_gateway_processing_messages(conn: sqlite3.Connection) -> None:
    """启动恢复前把上次中断的 processing 重新置为 queued。"""
    with transaction(conn):
        rows = conn.execute(
            """
            SELECT route_key, message_id, event_json
            FROM gateway_message_queue
            WHERE status='processing'
            """
        ).fetchall()
        now = time.time()
        conn.execute(
            """
            UPDATE gateway_message_queue
            SET status='queued', updated_at=?
            WHERE status='processing'
            """,
            (now,),
        )
        for route_key, message_id, event_json in rows:
            _update_gateway_source_message_ownership_status(
                conn,
                route_key=str(route_key),
                event_json=str(event_json),
                fallback_message_id=str(message_id),
                owner_kind="queue",
                owner_id=str(message_id),
                status="queued",
                updated_at=now,
            )


def get_gateway_queued_messages(conn: sqlite3.Connection) -> list[dict]:
    """返回仍需调用模型的消息;等待投递的消息由 outbox 单独恢复。"""
    rows = conn.execute(
        """
        SELECT route_key, message_id, event_json, status, task_kind, approval_id
        FROM gateway_message_queue
        WHERE status IN ('queued', 'processing')
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "route_key": route_key,
            "message_id": message_id,
            "event_json": event_json,
            "status": status,
            "task_kind": task_kind,
            "approval_id": approval_id,
        }
        for (
            route_key,
            message_id,
            event_json,
            status,
            task_kind,
            approval_id,
        ) in rows
    ]


def get_gateway_message_persistence_state(
    conn: sqlite3.Connection,
    route_key: str,
    message_id: str,
) -> dict | None:
    """通过规范化 ownership 主键查询平台消息的持久层归属。"""
    row = conn.execute(
        """
        SELECT owner_kind, status, owner_id
        FROM gateway_source_message_ownership
        WHERE route_key=? AND source_message_id=?
        """,
        (route_key, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "layer": str(row[0]),
        "status": str(row[1]),
        "owner_id": str(row[2]),
    }


def _serialize_gateway_json(value, field_name: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise DBError(f"{field_name} JSON serialization failed: {exc}") from exc


def _insert_gateway_outbox(
    conn: sqlite3.Connection,
    outbox: dict,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> str:
    """插入一条 outbox,不 commit,返回实际使用的 delivery id。"""
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

    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    now = time.time()
    queue_message_id = str(
        outbox.get("queue_message_id") or outbox["source_message_id"]
    )
    values = (
        str(outbox["id"]),
        str(outbox["route_key"]),
        str(outbox["source_message_id"]),
        queue_message_id,
        str(outbox["event_json"]),
        str(outbox["platform"]),
        str(outbox["chat_id"]),
        outbox.get("reply_to_message_id"),
        outbox.get("thread_id"),
        str(outbox["delivery_kind"]),
        _serialize_gateway_json(outbox["payloads"], "payloads"),
        now,
        now,
    )
    if fence is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO gateway_outbox (
                id, route_key, source_message_id, queue_message_id,
                event_json, platform,
                chat_id, reply_to_message_id, thread_id, delivery_kind,
                payloads_json, next_chunk_index, message_ids_json, status,
                attempt_count, next_attempt_at, last_error, last_error_code,
                claimed_by, claim_epoch, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', 'pending', 0,
                      NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            values,
        )
        fence_clause = ""
        fence_params: tuple = ()
    else:
        conn.execute(
            """
            INSERT OR IGNORE INTO gateway_outbox (
                id, route_key, source_message_id, queue_message_id,
                event_json, platform,
                chat_id, reply_to_message_id, thread_id, delivery_kind,
                payloads_json, next_chunk_index, message_ids_json, status,
                attempt_count, next_attempt_at, last_error, last_error_code,
                claimed_by, claim_epoch, created_at, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', 'pending', 0,
                   NULL, NULL, NULL, NULL, NULL, ?, ?
            WHERE EXISTS (
                SELECT 1
                FROM gateway_runtime_lease
                WHERE lease_name=? AND instance_id=? AND lease_epoch=?
                  AND expires_at>?
            )
            """,
            (*values, fence[0], fence[1], fence[2], now),
        )
        fence_clause = """
            AND EXISTS (
                SELECT 1
                FROM gateway_runtime_lease
                WHERE lease_name=? AND instance_id=? AND lease_epoch=?
                  AND expires_at>?
            )
        """
        fence_params = (fence[0], fence[1], fence[2], now)
    row = conn.execute(
        f"""
        SELECT id, event_json, status, created_at, updated_at, queue_message_id
        FROM gateway_outbox
        WHERE route_key=? AND source_message_id=? AND delivery_kind=?
        {fence_clause}
        """,
        (
            str(outbox["route_key"]),
            str(outbox["source_message_id"]),
            str(outbox["delivery_kind"]),
            *fence_params,
        ),
    ).fetchone()
    if row is None:
        raise DBError("gateway outbox insert did not create a row")
    outbox_id = str(row[0])
    if str(row[5]) != queue_message_id:
        raise DBError("gateway outbox queue identity mismatch")
    source_message_ids = gateway_event_source_message_ids(
        str(row[1]),
        str(outbox["source_message_id"]),
    )
    _upsert_gateway_source_message_ownership(
        conn,
        str(outbox["route_key"]),
        source_message_ids,
        owner_kind="outbox",
        owner_id=outbox_id,
        status=str(row[2]),
        created_at=float(row[3]),
        updated_at=float(row[4]),
    )
    return outbox_id


def enqueue_gateway_outbox(
    conn: sqlite3.Connection,
    outbox: dict,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> str:
    """持久化回复并把对应入站消息切换到 reply_pending。"""
    with transaction(conn):
        outbox_id = _insert_gateway_outbox(
            conn,
            outbox,
            lease_name=lease_name,
            instance_id=instance_id,
            lease_epoch=lease_epoch,
        )
        _set_gateway_queue_status(
            conn,
            str(outbox["route_key"]),
            str(outbox.get("queue_message_id") or outbox["source_message_id"]),
            "reply_pending",
            time.time(),
        )
    return outbox_id


def _gateway_outbox_row(row) -> dict | None:
    if row is None:
        return None
    (
        outbox_id,
        route_key,
        source_message_id,
        queue_message_id,
        event_json,
        platform,
        chat_id,
        reply_to_message_id,
        thread_id,
        delivery_kind,
        payloads_json,
        next_chunk_index,
        message_ids_json,
        status,
        attempt_count,
        next_attempt_at,
        last_error,
        last_error_code,
        claimed_by,
        claim_epoch,
    ) = row
    try:
        payloads = json.loads(payloads_json)
        message_ids = json.loads(message_ids_json)
    except (TypeError, ValueError) as exc:
        raise DBError(f"gateway outbox JSON deserialization failed: {exc}") from exc
    if not isinstance(payloads, list) or not isinstance(message_ids, list):
        raise DBError("gateway outbox JSON has invalid structure")
    return {
        "id": str(outbox_id),
        "route_key": str(route_key),
        "source_message_id": str(source_message_id),
        "queue_message_id": str(queue_message_id),
        "event_json": str(event_json),
        "platform": str(platform),
        "chat_id": str(chat_id),
        "reply_to_message_id": reply_to_message_id,
        "thread_id": thread_id,
        "delivery_kind": str(delivery_kind),
        "payloads": payloads,
        "next_chunk_index": int(next_chunk_index),
        "message_ids": [str(item) for item in message_ids],
        "status": str(status),
        "attempt_count": int(attempt_count),
        "next_attempt_at": next_attempt_at,
        "last_error": last_error,
        "last_error_code": last_error_code,
        "claimed_by": claimed_by,
        "claim_epoch": None if claim_epoch is None else int(claim_epoch),
    }


def get_gateway_outbox(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> dict | None:
    """按 delivery id 读取一条 outbox。"""
    row = conn.execute(
        f"SELECT {_GATEWAY_OUTBOX_COLUMNS} FROM gateway_outbox WHERE id=?",
        (outbox_id,),
    ).fetchone()
    return _gateway_outbox_row(row)


def get_recoverable_gateway_outbox(
    conn: sqlite3.Connection,
) -> list[dict]:
    """按创建顺序读取启动后需要恢复的出站回复。"""
    rows = conn.execute(
        f"""
        SELECT {_GATEWAY_OUTBOX_COLUMNS}
        FROM gateway_outbox
        WHERE status IN ('pending', 'sending', 'retry_wait')
        ORDER BY created_at, id
        """
    ).fetchall()
    recovered = []
    for row in rows:
        try:
            recovered.append(_gateway_outbox_row(row))
        except DBError as exc:
            # 保留损坏行供审计；Runner 会隔离对应 route，不让单行 JSON
            # 损坏阻断其他 route 的恢复。
            recovered.append({
                "id": str(row[0]),
                "route_key": str(row[1]),
                "source_message_id": str(row[2]),
                "queue_message_id": str(row[3]),
                "event_json": str(row[4]),
                "platform": str(row[5]),
                "status": str(row[13]),
                "recovery_error": type(exc).__name__,
            })
    return recovered


def get_gateway_routes_with_pending_outbox(
    conn: sqlite3.Connection,
) -> set[str]:
    """返回仍有待投递 Outbox 的 route，供内存会话清理保护。"""
    rows = conn.execute(
        """
        SELECT DISTINCT route_key
        FROM gateway_outbox
        WHERE status IN ('pending', 'sending', 'retry_wait')
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def get_next_recoverable_gateway_outbox_for_route(
    conn: sqlite3.Connection,
    route_key: str,
) -> dict | None:
    """读取某一路由下一条可投递 Outbox，供运行期串行 worker 接力。"""
    row = conn.execute(
        f"""
        SELECT {_GATEWAY_OUTBOX_COLUMNS}
        FROM gateway_outbox
        WHERE route_key=? AND status IN ('pending', 'sending', 'retry_wait')
        ORDER BY created_at, id
        LIMIT 1
        """,
        (route_key,),
    ).fetchone()
    return _gateway_outbox_row(row)


def _update_gateway_outbox_ownership_status(
    conn: sqlite3.Connection,
    *,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    event_json: str,
    status: str,
    updated_at: float,
) -> None:
    """同步仍由指定 Outbox 管理的全部原始消息状态。"""
    _update_gateway_source_message_ownership_status(
        conn,
        route_key=route_key,
        event_json=event_json,
        fallback_message_id=source_message_id,
        owner_kind="outbox",
        owner_id=outbox_id,
        status=status,
        updated_at=updated_at,
    )


def _gateway_outbox_lease_clause(
    fence: tuple[str, str, int] | None,
    timestamp: float,
) -> tuple[str, tuple]:
    """生成必须在同一 UPDATE 中成立的数据库租约条件。"""
    if fence is None:
        return "", ()
    return (
        """
        AND EXISTS (
            SELECT 1
            FROM gateway_runtime_lease AS lease
            WHERE lease.lease_name=?
              AND lease.instance_id=?
              AND lease.lease_epoch=?
              AND lease.expires_at>?
        )
        """,
        (fence[0], fence[1], fence[2], timestamp),
    )


def _gateway_outbox_claim_clause(
    fence: tuple[str, str, int] | None,
    timestamp: float,
) -> tuple[str, tuple]:
    """生成 claim 身份与当前数据库租约同时成立的条件。"""
    if fence is None:
        return "", ()
    lease_clause, lease_params = _gateway_outbox_lease_clause(
        fence,
        timestamp,
    )
    return (
        f"""
        AND claimed_by=? AND claim_epoch=?
        {lease_clause}
        """,
        (fence[1], fence[2], *lease_params),
    )


def reset_gateway_sending_outbox(
    conn: sqlite3.Connection,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> int:
    """由当前 epoch 接管启动遗留的 sending，并恢复为 pending。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with transaction(conn):
        now = time.time()
        lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
        rows = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='pending', next_attempt_at=NULL,
                claimed_by=NULL, claim_epoch=NULL, updated_at=?
            WHERE status='sending'
            {lease_clause}
            RETURNING id, route_key, source_message_id, event_json
            """,
            (now, *lease_params),
        ).fetchall()
        for outbox_id, route_key, source_message_id, event_json in rows:
            _update_gateway_outbox_ownership_status(
                conn,
                outbox_id=str(outbox_id),
                route_key=str(route_key),
                source_message_id=str(source_message_id),
                event_json=str(event_json),
                status="pending",
                updated_at=now,
            )
    return len(rows)


def mark_gateway_outbox_sending(
    conn: sqlite3.Connection,
    outbox_id: str,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """由当前有效 epoch claim Outbox 并切换为 sending。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with transaction(conn):
        row = conn.execute(
            """
            SELECT route_key, source_message_id, event_json, status
            FROM gateway_outbox
            WHERE id=?
            """,
            (outbox_id,),
        ).fetchone()
        if row is None or str(row[3]) not in {
            "pending",
            "sending",
            "retry_wait",
        }:
            return False
        now = time.time()
        lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
        claim_assignment = ""
        claim_params: tuple = ()
        if fence is not None:
            claim_assignment = ", claimed_by=?, claim_epoch=?"
            claim_params = (fence[1], fence[2])
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='sending', updated_at=? {claim_assignment}
            WHERE id=? AND status=?
            {lease_clause}
            """,
            (
                now,
                *claim_params,
                outbox_id,
                str(row[3]),
                *lease_params,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=str(row[0]),
            source_message_id=str(row[1]),
            event_json=str(row[2]),
            status="sending",
            updated_at=now,
        )
    return True


def mark_gateway_outbox_chunk_sent(
    conn: sqlite3.Connection,
    outbox_id: str,
    next_chunk_index: int,
    message_ids: list[str],
    total_chunks: int | None = None,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """只记录平台成功事实；终态由统一 delivery 事务负责收敛。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    saved_index = int(next_chunk_index)
    if total_chunks is None:
        row = conn.execute(
            "SELECT payloads_json FROM gateway_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            payloads = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway outbox JSON deserialization failed: {exc}"
            ) from exc
        if not isinstance(payloads, list):
            raise DBError("gateway outbox payloads JSON has invalid structure")
        total_chunks = len(payloads)
    total_chunks = int(total_chunks)
    if saved_index <= 0 or saved_index > total_chunks:
        raise DBError("gateway outbox chunk progress is out of range")

    now = time.time()
    claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
    cursor = conn.execute(
        f"""
        UPDATE gateway_outbox
        SET next_chunk_index=?, message_ids_json=?, updated_at=?
        WHERE id=?
          AND next_chunk_index <= ?
          AND status IN (
              'pending', 'sending', 'retry_wait',
              'cancelled', 'partial_cancelled'
          )
          {claim_clause}
        """,
        (
            saved_index,
            _serialize_gateway_json(message_ids, "message_ids"),
            now,
            outbox_id,
            saved_index,
            *claim_params,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_gateway_outbox_retry(
    conn: sqlite3.Connection,
    outbox_id: str,
    error: str,
    error_code: str | None,
    next_attempt_at: float,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    with transaction(conn):
        row = conn.execute(
            """
            SELECT route_key, source_message_id, event_json, status
            FROM gateway_outbox
            WHERE id=?
            """,
            (outbox_id,),
        ).fetchone()
        if row is None or str(row[3]) != "sending":
            return False
        now = time.time()
        claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='retry_wait', attempt_count=attempt_count + 1,
                next_attempt_at=?, last_error=?, last_error_code=?, updated_at=?
            WHERE id=? AND status='sending'
            {claim_clause}
            """,
            (
                next_attempt_at,
                error,
                error_code,
                now,
                outbox_id,
                *claim_params,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=str(row[0]),
            source_message_id=str(row[1]),
            event_json=str(row[2]),
            status="retry_wait",
            updated_at=now,
        )
    return True


def _gateway_terminal_outbox_row(
    conn: sqlite3.Connection,
    outbox_id: str,
):
    return conn.execute(
        """
        SELECT route_key, source_message_id, queue_message_id, status,
               next_chunk_index, payloads_json, event_json,
               claimed_by, claim_epoch
        FROM gateway_outbox
        WHERE id=?
        """,
        (outbox_id,),
    ).fetchone()


def _finish_gateway_queue_for_delivery(
    conn: sqlite3.Connection,
    route_key: str,
    queue_message_id: str,
    *,
    status: str,
    now: float,
) -> None:
    """在 Outbox 终态事务内同步其实际关联的 Queue 任务。"""
    row = conn.execute(
        """
        SELECT event_json
        FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, queue_message_id),
    ).fetchone()
    if row is None:
        return
    if status == "delivery_failed":
        _set_gateway_queue_status(
            conn,
            route_key,
            queue_message_id,
            "delivery_failed",
            now,
        )
        return

    ownership_status = "cancelled" if status == "cancelled" else "completed"
    _update_gateway_source_message_ownership_status(
        conn,
        route_key=route_key,
        event_json=str(row[0]),
        fallback_message_id=queue_message_id,
        owner_kind="queue",
        owner_id=queue_message_id,
        status=ownership_status,
        updated_at=now,
    )
    conn.execute(
        """
        DELETE FROM gateway_message_queue
        WHERE route_key=? AND message_id=?
        """,
        (route_key, queue_message_id),
    )


def _mark_gateway_message_delivery_terminal(
    conn: sqlite3.Connection,
    delivery_id: str,
    status: str,
    updated_at: float,
    *,
    route_key: str | None = None,
    source_message_id: str | None = None,
) -> None:
    """与 Outbox 终态同事务更新；没有关联的旧/命令 Outbox 保持兼容。"""
    allowed_sources = {
        "delivered": ("pending", "cancelled", "partial_cancelled"),
        "cancelled": ("pending",),
        "partial_cancelled": ("pending", "cancelled"),
        "permanent_failed": ("pending",),
    }
    if status not in allowed_sources:
        raise DBError(f"invalid gateway delivery terminal status: {status}")
    row = conn.execute(
        """
        SELECT status, route_key, source_message_id
        FROM gateway_message_deliveries
        WHERE delivery_id=?
        """,
        (delivery_id,),
    ).fetchone()
    if row is None:
        return
    if (
        route_key is not None
        and source_message_id is not None
        and (
            str(row[1]) != route_key
            or str(row[2]) != source_message_id
        )
    ):
        raise DBError(
            "gateway assistant delivery identity mismatch "
            f"for outbox {delivery_id}"
        )
    current_status = str(row[0])
    if current_status == status:
        return
    source_statuses = allowed_sources[status]
    if current_status not in source_statuses:
        raise DBError(
            "invalid gateway assistant delivery transition: "
            f"{current_status} -> {status}"
        )
    cursor = conn.execute(
        """
        UPDATE gateway_message_deliveries
        SET status=?, updated_at=?
        WHERE delivery_id=? AND status=?
        """,
        (status, updated_at, delivery_id, current_status),
    )
    if cursor.rowcount <= 0:
        raise DBError(
            "gateway assistant delivery CAS failed "
            f"for outbox {delivery_id}"
        )


def _infer_cancelled_gateway_outbox_status(
    next_chunk_index: int,
    payloads_json: str,
) -> str:
    """按已经持久化的发送进度推导取消类 Outbox 的真实终态。"""
    if int(next_chunk_index) <= 0:
        return "cancelled"
    try:
        payloads = json.loads(payloads_json)
    except (TypeError, ValueError):
        # 损坏 payload 仍保留审计；已有成功进度时至少不能降回完全未发送。
        return "partial_cancelled"
    if not isinstance(payloads, list):
        return "partial_cancelled"
    if payloads and int(next_chunk_index) >= len(payloads):
        return "delivered"
    return "partial_cancelled"

