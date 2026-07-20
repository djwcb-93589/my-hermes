from __future__ import annotations

import json
import sqlite3
import time

from .database import (
    DBError, FEISHU_INBOX_STATUSES, InvalidFeishuInboxPayloadError, transaction,
    _cleanup_batch_limit as _shared_cleanup_batch_limit,
    _derive_feishu_inbox_route_key,
    _immediate_transaction,
)

def _validate_feishu_inbox_identity(app_id: str, message_id: str) -> None:
    """校验 Inbox 复合身份，避免写入不可寻址记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    if not isinstance(message_id, str) or not message_id:
        raise DBError("Feishu Inbox message_id must not be empty")


def _validate_feishu_inbox_status(status: str) -> None:
    """校验 Inbox 状态，错误在进入 SQL 前显式暴露。"""
    if status not in FEISHU_INBOX_STATUSES:
        raise DBError(f"invalid Feishu Inbox status: {status}")


def insert_feishu_inbox_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    payload: dict,
    *,
    route_key: str | None = None,
    received_at: float | None = None,
) -> bool:
    """幂等插入 pending 消息，并原子分配应用内接收序号。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    if not isinstance(payload, dict):
        raise DBError("Feishu Inbox payload must contain an object")
    try:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DBError("Feishu Inbox payload JSON serialization failed") from exc
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        normalized_route_key = _derive_feishu_inbox_route_key(
            app_id,
            message_id,
            encoded_payload,
        )

    timestamp = time.time() if received_at is None else float(received_at)
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO feishu_message_inbox (
                app_id, message_id, route_key, payload, received_at,
                receive_sequence,
                status, attempt_count, next_attempt_at, last_error,
                updated_at, completed_at, batch_message_id
            ) VALUES (
                ?, ?, ?, ?, ?,
                COALESCE((
                    SELECT MAX(receive_sequence) + 1
                    FROM feishu_message_inbox
                    WHERE app_id = ?
                ), 1),
                'pending', 0, NULL, NULL, ?, NULL, NULL
            )
            ON CONFLICT(app_id, message_id) DO NOTHING
            """,
            (
                app_id,
                message_id,
                normalized_route_key,
                encoded_payload,
                timestamp,
                app_id,
                timestamp,
            ),
        )
    return cursor.rowcount == 1


def get_feishu_inbox_status(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
) -> str | None:
    """读取单条 Inbox 状态。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    row = conn.execute(
        """
        SELECT status
        FROM feishu_message_inbox
        WHERE app_id=? AND message_id=?
        """,
        (app_id, message_id),
    ).fetchone()
    return str(row[0]) if row is not None else None


def get_feishu_inbox_payload(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    status: str | None = None,
) -> dict | None:
    """按身份和可选状态读取、反序列化 Inbox 原始事件。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    if status is None:
        row = conn.execute(
            """
            SELECT payload
            FROM feishu_message_inbox
            WHERE app_id=? AND message_id=?
            """,
            (app_id, message_id),
        ).fetchone()
    else:
        _validate_feishu_inbox_status(status)
        row = conn.execute(
            """
            SELECT payload
            FROM feishu_message_inbox
            WHERE app_id=? AND message_id=? AND status=?
            """,
            (app_id, message_id, status),
        ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise InvalidFeishuInboxPayloadError(
            "Feishu Inbox payload JSON deserialization failed"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidFeishuInboxPayloadError(
            "Feishu Inbox payload must contain an object"
        )
    return payload


def get_feishu_inbox_dispatch_candidates(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: float | None = None,
    limit: int = 64,
) -> list[str]:
    """按接收顺序读取当前可执行的 pending 或到期 retry_wait 记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        raise DBError("Feishu Inbox dispatch limit must be greater than 0")
    timestamp = time.time() if now is None else float(now)
    rows = conn.execute(
        """
        SELECT message_id
        FROM feishu_message_inbox
        WHERE app_id=?
          AND (
              status='pending'
              OR (
                  status='retry_wait'
                  AND next_attempt_at IS NOT NULL
                  AND next_attempt_at<=?
              )
          )
        ORDER BY received_at, receive_sequence
        LIMIT ?
        """,
        (app_id, timestamp, normalized_limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


def get_feishu_inbox_dispatch_routes(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: float | None = None,
    limit: int = 64,
) -> list[str]:
    """读取每个路由严格队首中当前可执行的 route_key。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    normalized_limit = int(limit)
    if normalized_limit <= 0:
        raise DBError("Feishu Inbox dispatch limit must be greater than 0")
    timestamp = time.time() if now is None else float(now)
    rows = conn.execute(
        """
        WITH route_heads AS (
            SELECT current.route_key, current.status,
                   current.next_attempt_at, current.received_at,
                   current.receive_sequence
            FROM feishu_message_inbox AS current
            WHERE current.app_id=?
              AND current.status IN (
                  'pending', 'processing', 'retry_wait'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM feishu_message_inbox AS prior
                  WHERE prior.app_id=current.app_id
                    AND prior.route_key=current.route_key
                    AND prior.status IN (
                        'pending', 'processing', 'retry_wait'
                    )
                    AND (
                        prior.received_at < current.received_at
                        OR (
                            prior.received_at = current.received_at
                            AND prior.receive_sequence
                                < current.receive_sequence
                        )
                    )
              )
        )
        SELECT route_key
        FROM route_heads
        WHERE status='pending'
           OR (
               status='retry_wait'
               AND next_attempt_at IS NOT NULL
               AND next_attempt_at<=?
           )
        ORDER BY received_at, receive_sequence
        LIMIT ?
        """,
        (app_id, timestamp, normalized_limit),
    ).fetchall()
    return [str(row[0]) for row in rows]


def get_feishu_inbox_route_next(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
) -> dict | None:
    """查看路由中尚未终结且未被当前消费者 claim 的严格队首。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        raise DBError("Feishu Inbox route_key must not be empty")
    row = conn.execute(
        """
        SELECT message_id, status, attempt_count, next_attempt_at,
               received_at, receive_sequence
        FROM feishu_message_inbox
        WHERE app_id=? AND route_key=?
          AND status IN ('pending', 'retry_wait')
        ORDER BY received_at, receive_sequence
        LIMIT 1
        """,
        (app_id, normalized_route_key),
    ).fetchone()
    if row is None:
        return None
    return {
        "message_id": str(row[0]),
        "status": str(row[1]),
        "attempt_count": int(row[2]),
        "next_attempt_at": None if row[3] is None else float(row[3]),
        "received_at": float(row[4]),
        "receive_sequence": int(row[5]),
    }


def claim_feishu_inbox_route_message(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
    message_id: str,
    *,
    now: float | None = None,
    allow_existing_processing: bool = False,
) -> dict | None:
    """仅 claim 路由未终结队首，并原子切换为 processing。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        raise DBError("Feishu Inbox route_key must not be empty")
    timestamp = time.time() if now is None else float(now)
    with _immediate_transaction(conn):
        if not allow_existing_processing:
            processing = conn.execute(
                """
                SELECT 1
                FROM feishu_message_inbox
                WHERE app_id=? AND route_key=? AND status='processing'
                LIMIT 1
                """,
                (app_id, normalized_route_key),
            ).fetchone()
            if processing is not None:
                return None
        head = conn.execute(
            """
            SELECT message_id
            FROM feishu_message_inbox
            WHERE app_id=? AND route_key=?
              AND status IN ('pending', 'retry_wait')
            ORDER BY received_at, receive_sequence
            LIMIT 1
            """,
            (app_id, normalized_route_key),
        ).fetchone()
        if head is None or str(head[0]) != message_id:
            return None
        row = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='processing', updated_at=?, next_attempt_at=NULL,
                completed_at=NULL
            WHERE app_id=? AND route_key=? AND message_id=?
              AND (
                  status='pending'
                  OR (
                      status='retry_wait'
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at<=?
                  )
              )
            RETURNING attempt_count, received_at, receive_sequence
            """,
            (
                timestamp,
                app_id,
                normalized_route_key,
                message_id,
                timestamp,
            ),
        ).fetchone()
    if row is None:
        return None
    return {
        "message_id": message_id,
        "route_key": normalized_route_key,
        "attempt_count": int(row[0]),
        "received_at": float(row[1]),
        "receive_sequence": int(row[2]),
    }


def claim_feishu_inbox_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    now: float | None = None,
) -> dict | None:
    """条件 claim 可执行记录并切换为 processing，竞争失败返回 None。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='processing', updated_at=?, next_attempt_at=NULL,
                completed_at=NULL
            WHERE app_id=? AND message_id=?
              AND (
                  status='pending'
                  OR (
                      status='retry_wait'
                      AND next_attempt_at IS NOT NULL
                      AND next_attempt_at<=?
                  )
              )
            """,
            (timestamp, app_id, message_id, timestamp),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute(
            """
            SELECT attempt_count, receive_sequence
            FROM feishu_message_inbox
            WHERE app_id=? AND message_id=? AND status='processing'
            """,
            (app_id, message_id),
        ).fetchone()
        if row is None:
            raise DBError("claimed Feishu Inbox message disappeared")
    return {
        "message_id": message_id,
        "attempt_count": int(row[0]),
        "receive_sequence": int(row[1]),
    }


def update_feishu_inbox_status(
    conn: sqlite3.Connection,
    app_id: str,
    message_ids: list[str],
    status: str,
    *,
    completed_at: float | None = None,
    batch_message_id: str | None = None,
    updated_at: float | None = None,
    expected_statuses: tuple[str, ...] | None = None,
) -> int:
    """原子更新一组 Inbox 消息的状态和当前批次追踪信息。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    _validate_feishu_inbox_status(status)
    if not message_ids:
        return 0
    normalized_message_ids = []
    for message_id in message_ids:
        _validate_feishu_inbox_identity(app_id, message_id)
        normalized_message_ids.append(message_id)
    timestamp = time.time() if updated_at is None else float(updated_at)
    normalized_completed_at = (
        None if completed_at is None else float(completed_at)
    )
    normalized_expected_statuses = None
    if expected_statuses is not None:
        normalized_expected_statuses = tuple(dict.fromkeys(expected_statuses))
        if not normalized_expected_statuses:
            return 0
        for expected_status in normalized_expected_statuses:
            _validate_feishu_inbox_status(expected_status)
    status_clause = ""
    status_params: tuple[str, ...] = ()
    if normalized_expected_statuses is not None:
        placeholders = ", ".join("?" for _ in normalized_expected_statuses)
        status_clause = f" AND status IN ({placeholders})"
        status_params = normalized_expected_statuses
    with transaction(conn):
        cursor = conn.executemany(
            f"""
            UPDATE feishu_message_inbox
            SET status=?, updated_at=?, completed_at=?, batch_message_id=?,
                next_attempt_at=NULL
            WHERE app_id=? AND message_id=?
            {status_clause}
            """,
            [
                (
                    status,
                    timestamp,
                    normalized_completed_at,
                    batch_message_id,
                    app_id,
                    message_id,
                    *status_params,
                )
                for message_id in normalized_message_ids
            ],
        )
    return max(0, cursor.rowcount)


def fail_feishu_inbox_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    last_error: str,
    next_attempt_at: float | None,
    max_attempts: int,
    permanent: bool = False,
    now: float | None = None,
) -> dict | None:
    """记录一次失败，并按永久性或最大次数决定 retry_wait/终态。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    normalized_max_attempts = int(max_attempts)
    if normalized_max_attempts <= 0:
        raise DBError("Feishu Inbox max_attempts must be greater than 0")
    normalized_error = str(last_error or "inbox_failure")[:256]
    timestamp = time.time() if now is None else float(now)
    normalized_next_attempt = (
        None if next_attempt_at is None else float(next_attempt_at)
    )
    with transaction(conn):
        row = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET attempt_count=attempt_count + 1,
                status=CASE
                    WHEN ? OR attempt_count + 1 >= ?
                    THEN 'permanent_failed'
                    ELSE 'retry_wait'
                END,
                next_attempt_at=CASE
                    WHEN ? OR attempt_count + 1 >= ?
                    THEN NULL
                    ELSE ?
                END,
                last_error=?,
                updated_at=?,
                completed_at=CASE
                    WHEN ? OR attempt_count + 1 >= ?
                    THEN ?
                    ELSE NULL
                END
            WHERE app_id=? AND message_id=? AND status='processing'
            RETURNING status, attempt_count, next_attempt_at
            """,
            (
                bool(permanent),
                normalized_max_attempts,
                bool(permanent),
                normalized_max_attempts,
                normalized_next_attempt,
                normalized_error,
                timestamp,
                bool(permanent),
                normalized_max_attempts,
                timestamp,
                app_id,
                message_id,
            ),
        ).fetchone()
    if row is None:
        return None
    return {
        "status": str(row[0]),
        "attempt_count": int(row[1]),
        "next_attempt_at": (
            None if row[2] is None else float(row[2])
        ),
    }


def reset_feishu_inbox_processing(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    now: float | None = None,
) -> int:
    """启动时把异常退出遗留的 processing 收敛为立即可恢复状态。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='retry_wait', next_attempt_at=?, updated_at=?,
                completed_at=NULL,
                last_error='gateway_restart:processing_recovered'
            WHERE app_id=? AND status='processing'
            """,
            (timestamp, timestamp, app_id),
        )
    return cursor.rowcount


def release_feishu_inbox_processing_message(
    conn: sqlite3.Connection,
    app_id: str,
    message_id: str,
    *,
    last_error: str,
    now: float | None = None,
) -> bool:
    """不增加尝试次数地释放 processing，供有序 shutdown 恢复。"""
    _validate_feishu_inbox_identity(app_id, message_id)
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE feishu_message_inbox
            SET status='retry_wait', next_attempt_at=?, updated_at=?,
                completed_at=NULL, last_error=?
            WHERE app_id=? AND message_id=? AND status='processing'
            """,
            (
                timestamp,
                timestamp,
                str(last_error or "gateway_stopping")[:256],
                app_id,
                message_id,
            ),
        )
    return cursor.rowcount == 1


def _validate_feishu_pending_attachment_route(
    app_id: str,
    route_key: str,
) -> str:
    """校验待绑定附件的应用与路由身份。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu pending attachment app_id must not be empty")
    normalized_route_key = str(route_key or "")
    if not normalized_route_key:
        raise DBError(
            "Feishu pending attachment route_key must not be empty"
        )
    return normalized_route_key


def _normalize_feishu_pending_message_ids(
    app_id: str,
    message_ids: list[str],
) -> list[str]:
    """规范化 source 或绑定消息 ID，保留平台顺序并去重。"""
    if not isinstance(message_ids, list) or not message_ids:
        raise DBError("Feishu pending attachment message_ids must not be empty")
    normalized: list[str] = []
    for message_id in message_ids:
        _validate_feishu_inbox_identity(app_id, message_id)
        if message_id not in normalized:
            normalized.append(message_id)
    return normalized


def _decode_feishu_pending_attachments(
    encoded: object,
) -> list[dict]:
    """读取待绑定附件 JSON，不让损坏记录静默变成空附件。"""
    try:
        decoded = json.loads(str(encoded))
    except (TypeError, ValueError) as exc:
        raise DBError(
            "Feishu pending attachment JSON deserialization failed"
        ) from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or not all(isinstance(item, dict) for item in decoded)
    ):
        raise DBError("Feishu pending attachment JSON is invalid")
    return [dict(item) for item in decoded]


def get_feishu_pending_attachment(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
    source_message_id: str,
) -> list[dict] | None:
    """读取某个已物化附件，供确认回执重试时复用本地事实。"""
    normalized_route_key = _validate_feishu_pending_attachment_route(
        app_id,
        route_key,
    )
    _validate_feishu_inbox_identity(app_id, source_message_id)
    row = conn.execute(
        """
        SELECT attachments_json
        FROM feishu_pending_attachments
        WHERE app_id=? AND route_key=? AND source_message_id=?
        """,
        (app_id, normalized_route_key, source_message_id),
    ).fetchone()
    if row is None:
        return None
    return _decode_feishu_pending_attachments(row[0])


def upsert_feishu_pending_attachment(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
    source_message_id: str,
    attachments: list[dict],
    *,
    now: float | None = None,
) -> bool:
    """持久化已下载附件；已经绑定给文本的记录不得被重置。"""
    normalized_route_key = _validate_feishu_pending_attachment_route(
        app_id,
        route_key,
    )
    _validate_feishu_inbox_identity(app_id, source_message_id)
    if (
        not isinstance(attachments, list)
        or not attachments
        or not all(isinstance(item, dict) for item in attachments)
    ):
        raise DBError("Feishu pending attachment list is invalid")
    try:
        encoded_attachments = json.dumps(
            attachments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DBError(
            "Feishu pending attachment JSON serialization failed"
        ) from exc
    timestamp = time.time() if now is None else float(now)
    with transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO feishu_pending_attachments (
                app_id, route_key, source_message_id, attachments_json,
                state, bound_message_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'awaiting_instruction', NULL, ?, ?)
            ON CONFLICT(app_id, route_key, source_message_id) DO UPDATE SET
                attachments_json=excluded.attachments_json,
                updated_at=excluded.updated_at
            WHERE feishu_pending_attachments.state='awaiting_instruction'
            """,
            (
                app_id,
                normalized_route_key,
                source_message_id,
                encoded_attachments,
                timestamp,
                timestamp,
            ),
        )
    return cursor.rowcount == 1


def claim_feishu_pending_attachments(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
    source_message_ids: list[str],
    *,
    now: float | None = None,
) -> list[dict]:
    """把一个文本批次绑定到当前 route 全部待指令附件。"""
    normalized_route_key = _validate_feishu_pending_attachment_route(
        app_id,
        route_key,
    )
    normalized_message_ids = _normalize_feishu_pending_message_ids(
        app_id,
        source_message_ids,
    )
    timestamp = time.time() if now is None else float(now)
    placeholders = ", ".join("?" for _ in normalized_message_ids)
    with _immediate_transaction(conn):
        rows = conn.execute(
            f"""
            SELECT source_message_id, attachments_json
            FROM feishu_pending_attachments
            WHERE app_id=? AND route_key=? AND state='bound'
              AND bound_message_id IN ({placeholders})
            ORDER BY created_at, source_message_id
            """,
            (app_id, normalized_route_key, *normalized_message_ids),
        ).fetchall()
        if not rows:
            conn.execute(
                """
                UPDATE feishu_pending_attachments
                SET state='bound', bound_message_id=?, updated_at=?
                WHERE app_id=? AND route_key=?
                  AND state='awaiting_instruction'
                """,
                (
                    normalized_message_ids[-1],
                    timestamp,
                    app_id,
                    normalized_route_key,
                ),
            )
            rows = conn.execute(
                """
                SELECT source_message_id, attachments_json
                FROM feishu_pending_attachments
                WHERE app_id=? AND route_key=? AND state='bound'
                  AND bound_message_id=?
                ORDER BY created_at, source_message_id
                """,
                (
                    app_id,
                    normalized_route_key,
                    normalized_message_ids[-1],
                ),
            ).fetchall()

    claimed: list[dict] = []
    for source_message_id, encoded_attachments in rows:
        claimed.append({
            "source_message_id": str(source_message_id),
            "attachments": _decode_feishu_pending_attachments(
                encoded_attachments,
            ),
        })
    return claimed


def delete_feishu_pending_attachments(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
    source_message_ids: list[str],
) -> int:
    """在文本已经被 Gateway 接受后删除已消费的附件记录。"""
    normalized_route_key = _validate_feishu_pending_attachment_route(
        app_id,
        route_key,
    )
    normalized_message_ids = _normalize_feishu_pending_message_ids(
        app_id,
        source_message_ids,
    )
    placeholders = ", ".join("?" for _ in normalized_message_ids)
    with transaction(conn):
        cursor = conn.execute(
            f"""
            DELETE FROM feishu_pending_attachments
            WHERE app_id=? AND route_key=?
              AND source_message_id IN ({placeholders})
            """,
            (app_id, normalized_route_key, *normalized_message_ids),
        )
    return max(0, cursor.rowcount)


def clear_feishu_pending_attachments(
    conn: sqlite3.Connection,
    app_id: str,
    route_key: str,
) -> int:
    """新建会话时清除旧 route 未消费附件，避免跨会话自动绑定。"""
    normalized_route_key = _validate_feishu_pending_attachment_route(
        app_id,
        route_key,
    )
    with transaction(conn):
        cursor = conn.execute(
            """
            DELETE FROM feishu_pending_attachments
            WHERE app_id=? AND route_key=?
            """,
            (app_id, normalized_route_key),
        )
    return max(0, cursor.rowcount)


def prune_feishu_pending_attachments(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    created_before: float,
    limit: int = 200,
) -> int:
    """按有界批次清理超出附件缓存保留期的未消费记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu pending attachment app_id must not be empty")
    batch_limit = _shared_cleanup_batch_limit(limit, "Feishu pending attachment")
    cutoff = float(created_before)
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT route_key, source_message_id
            FROM feishu_pending_attachments
            WHERE app_id=? AND created_at < ?
            ORDER BY created_at, source_message_id
            LIMIT ?
            """,
            (app_id, cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for route_key, source_message_id in rows:
            cursor = conn.execute(
                """
                DELETE FROM feishu_pending_attachments
                WHERE app_id=? AND route_key=? AND source_message_id=?
                  AND created_at < ?
                """,
                (app_id, str(route_key), str(source_message_id), cutoff),
            )
            removed += cursor.rowcount
    return removed


def _cleanup_batch_limit(value, label: str) -> int:
    """统一校验清理批次，避免布尔值或非法字符串进入 LIMIT。"""
    if isinstance(value, bool):
        raise DBError(f"{label} cleanup limit must be positive")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise DBError(f"{label} cleanup limit must be positive") from exc
    if normalized <= 0:
        raise DBError(f"{label} cleanup limit must be positive")
    return normalized


def prune_feishu_inbox_messages(
    conn: sqlite3.Connection,
    app_id: str,
    *,
    completed_before: float,
    limit: int = 200,
) -> int:
    """分批清理超过保留期的 Inbox 终态审计记录。"""
    if not isinstance(app_id, str) or not app_id:
        raise DBError("Feishu Inbox app_id must not be empty")
    batch_limit = _shared_cleanup_batch_limit(limit, "Feishu Inbox")
    cutoff = float(completed_before)
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
            SELECT candidate.message_id
            FROM feishu_message_inbox AS candidate
            WHERE candidate.app_id=?
              AND candidate.completed_at < ?
              AND candidate.status IN (
                  'processed', 'cancelled', 'permanent_failed'
              )
            ORDER BY candidate.completed_at, candidate.receive_sequence
            LIMIT ?
            """,
            (app_id, cutoff, batch_limit),
        ).fetchall()
        removed = 0
        for (message_id,) in rows:
            cursor = conn.execute(
                """
                DELETE FROM feishu_message_inbox
                WHERE app_id=? AND message_id=? AND completed_at < ?
                  AND status IN (
                      'processed', 'cancelled', 'permanent_failed'
                  )
                """,
                (app_id, str(message_id), cutoff),
            )
            removed += cursor.rowcount
    return removed

