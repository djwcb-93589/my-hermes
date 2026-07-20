"""Gateway Queue / Outbox / Message Delivery / File Delivery 跨表协调层。

本模块专门承载同时操作多个 Gateway 投递相关表的原子逻辑,避免
``gateway.py`` 与 ``delivery.py`` 互相反向导入形成循环。

依赖方向(单向):

    gateway_delivery -> gateway (复用 Outbox / Queue / Message Delivery 单表 helper)
    gateway_delivery -> database (事务与异常)
    gateway_delivery -> 无其他领域模块

``gateway.py`` 与 ``delivery.py`` 都可以从本模块导入协调函数,但本模块
不会反向依赖它们,从而保持以下不变量:

* ``gateway.py`` 不再 import ``delivery.py``;
* ``delivery.py`` 仍可单向 import ``gateway.py`` 的纯辅助函数,但不构成循环;
* 所有跨表操作继续使用调用方传入的同一个 SQLite connection,事务边界与
  状态机语义保持原样。
"""

from __future__ import annotations

import json
import sqlite3
import time

from .database import DBError, _immediate_transaction, transaction
from .gateway import (
    _finish_gateway_queue_for_delivery,
    _gateway_outbox_claim_clause,
    _gateway_outbox_fence_values,
    _gateway_outbox_lease_clause,
    _gateway_terminal_outbox_row,
    _infer_cancelled_gateway_outbox_status,
    _mark_gateway_message_delivery_terminal,
    _update_gateway_outbox_ownership_status,
    gateway_runtime_lease_is_valid,
)


# ---------------------------------------------------------------------------
# 私有 helper
# ---------------------------------------------------------------------------


def _validate_gateway_delivery_identity(
    outbox_id: str,
    row,
    route_key: str,
    source_message_id: str,
) -> None:
    if str(row[0]) != route_key or str(row[1]) != source_message_id:
        raise DBError(
            "gateway delivery identity mismatch "
            f"for outbox {outbox_id}"
        )


def _sync_gateway_file_delivery_terminal(
    conn: sqlite3.Connection,
    outbox_id: str,
    outbox_status: str,
    now: float,
    *,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    """在 Outbox 终态事务内同步关联文件任务,旧 Outbox 无关联即跳过。"""
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


# ---------------------------------------------------------------------------
# 跨表原子操作:Outbox + Message Delivery + Ownership + File Delivery + Queue
# ---------------------------------------------------------------------------


def complete_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """原子完成 Outbox、assistant delivery,并删除对应入站 queue。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    now = time.time()
    with transaction(conn):
        row = _gateway_terminal_outbox_row(conn, outbox_id)
        if row is None:
            return False
        _validate_gateway_delivery_identity(
            outbox_id,
            row,
            route_key,
            source_message_id,
        )
        old_status = str(row[3])
        if old_status not in {
            "pending",
            "sending",
            "retry_wait",
            "cancelled",
            "partial_cancelled",
        }:
            return False
        try:
            payloads = json.loads(row[5])
        except (TypeError, ValueError) as exc:
            raise DBError(
                f"gateway outbox JSON deserialization failed: {exc}"
            ) from exc
        if not isinstance(payloads, list):
            raise DBError("gateway outbox payloads JSON has invalid structure")
        if int(row[4]) < len(payloads):
            return False

        claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='delivered', next_attempt_at=NULL,
                last_error=NULL, last_error_code=NULL, updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            {claim_clause}
            """,
            (
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
                *claim_params,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _mark_gateway_message_delivery_terminal(
            conn,
            outbox_id,
            "delivered",
            now,
            route_key=route_key,
            source_message_id=source_message_id,
        )
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=route_key,
            source_message_id=source_message_id,
            event_json=str(row[6]),
            status="delivered",
            updated_at=now,
        )
        _sync_gateway_file_delivery_terminal(
            conn,
            outbox_id,
            "delivered",
            now,
        )
        _finish_gateway_queue_for_delivery(
            conn,
            route_key,
            str(row[2]),
            status="completed",
            now=now,
        )
    return True


def fail_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    error: str,
    error_code: str | None,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """原子持久化永久失败,并把入站 queue 留作失败审计。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    safe_error = str(error)[:120]
    safe_error_code = str(error_code)[:120] if error_code is not None else None
    now = time.time()
    with transaction(conn):
        row = _gateway_terminal_outbox_row(conn, outbox_id)
        if row is None:
            return False
        _validate_gateway_delivery_identity(
            outbox_id,
            row,
            route_key,
            source_message_id,
        )
        old_status = str(row[3])
        if old_status not in {"pending", "sending", "retry_wait"}:
            return False
        claim_clause, claim_params = _gateway_outbox_claim_clause(fence, now)
        cursor = conn.execute(
            f"""
            UPDATE gateway_outbox
            SET status='permanent_failed', last_error=?, last_error_code=?,
                next_attempt_at=NULL, updated_at=?
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            {claim_clause}
            """,
            (
                safe_error,
                safe_error_code,
                now,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
                *claim_params,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _mark_gateway_message_delivery_terminal(
            conn,
            outbox_id,
            "permanent_failed",
            now,
            route_key=route_key,
            source_message_id=source_message_id,
        )
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=route_key,
            source_message_id=source_message_id,
            event_json=str(row[6]),
            status="permanent_failed",
            updated_at=now,
        )
        _sync_gateway_file_delivery_terminal(
            conn,
            outbox_id,
            "permanent_failed",
            now,
            error=safe_error,
            error_code=safe_error_code,
        )
        _finish_gateway_queue_for_delivery(
            conn,
            route_key,
            str(row[2]),
            status="delivery_failed",
            now=now,
        )
    return True


def cancel_gateway_delivery(
    conn: sqlite3.Connection,
    outbox_id: str,
    route_key: str,
    source_message_id: str,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """按成功进度原子取消剩余投递,并删除对应入站 queue。"""
    fence = _gateway_outbox_fence_values(
        lease_name,
        instance_id,
        lease_epoch,
    )
    now = time.time()
    with transaction(conn):
        row = _gateway_terminal_outbox_row(conn, outbox_id)
        if row is None:
            return False
        _validate_gateway_delivery_identity(
            outbox_id,
            row,
            route_key,
            source_message_id,
        )
        old_status = str(row[3])
        if old_status not in {
            "pending",
            "sending",
            "retry_wait",
            "cancelled",
            "partial_cancelled",
        }:
            return False
        status = _infer_cancelled_gateway_outbox_status(
            int(row[4]),
            str(row[5]),
        )
        if status == old_status:
            return False

        lease_clause, lease_params = _gateway_outbox_lease_clause(fence, now)
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
            WHERE id=? AND route_key=? AND source_message_id=? AND status=?
            {lease_clause}
            """,
            (
                status,
                status,
                status,
                now,
                *claim_params,
                outbox_id,
                route_key,
                source_message_id,
                old_status,
                *lease_params,
            ),
        )
        if cursor.rowcount <= 0:
            return False
        _mark_gateway_message_delivery_terminal(
            conn,
            outbox_id,
            status,
            now,
            route_key=route_key,
            source_message_id=source_message_id,
        )
        _update_gateway_outbox_ownership_status(
            conn,
            outbox_id=outbox_id,
            route_key=route_key,
            source_message_id=source_message_id,
            event_json=str(row[6]),
            status=status,
            updated_at=now,
        )
        _sync_gateway_file_delivery_terminal(
            conn,
            outbox_id,
            status,
            now,
        )
        _finish_gateway_queue_for_delivery(
            conn,
            route_key,
            str(row[2]),
            status="cancelled",
            now=now,
        )
    return True


def mark_gateway_outbox_failed(
    conn: sqlite3.Connection,
    outbox_id: str,
    error: str,
    error_code: str | None,
) -> bool:
    row = _gateway_terminal_outbox_row(conn, outbox_id)
    if row is None:
        return False
    return fail_gateway_delivery(
        conn,
        outbox_id,
        str(row[0]),
        str(row[1]),
        error,
        error_code,
    )


def mark_gateway_outbox_cancelled(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> bool:
    row = _gateway_terminal_outbox_row(conn, outbox_id)
    if row is None:
        return False
    return cancel_gateway_delivery(
        conn,
        outbox_id,
        str(row[0]),
        str(row[1]),
    )


def mark_gateway_outbox_delivered(
    conn: sqlite3.Connection,
    outbox_id: str,
) -> bool:
    row = _gateway_terminal_outbox_row(conn, outbox_id)
    if row is None:
        return False
    return complete_gateway_delivery(
        conn,
        outbox_id,
        str(row[0]),
        str(row[1]),
    )


def reconcile_gateway_terminal_deliveries(
    conn: sqlite3.Connection,
    *,
    lease_name: str | None = None,
    instance_id: str | None = None,
    lease_epoch: int | None = None,
) -> int:
    """收敛旧版本遗留的终态 Outbox 与 ``reply_pending`` queue。

    新代码通过统一终态函数一次提交三层状态;这里仅修复升级前已经形成的
    孤儿记录,以及"取消先提交、最后一个平台成功随后落进度"留下的可推导
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
