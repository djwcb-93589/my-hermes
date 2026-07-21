"""通用工具执行 Journal 的持久化接口。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any

from .database import DBError, transaction
from .core import _insert_message
from .gateway import gateway_runtime_lease_is_valid


TOOL_EXECUTION_STATUSES = frozenset({
    "prepared",
    "running",
    "succeeded",
    "failed",
    "unknown",
})
_INCOMPLETE_STATUSES = frozenset({"prepared", "running", "unknown"})


def _require_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DBError(f"tool execution {field_name} must be a non-empty string")
    return value.strip()


def _serialize_json(value: Any, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DBError(f"tool execution {field_name} JSON serialization failed: {exc}") from exc


def _deserialize_json(value: str | None, field_name: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise DBError(f"tool execution {field_name} JSON deserialization failed: {exc}") from exc


def _arguments_fingerprint(arguments_json: str) -> str:
    return hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()


def _tool_execution_row(row) -> dict | None:
    if row is None:
        return None
    fields = (
        "execution_id", "environment", "session_id", "source_message_id",
        "cron_run_id", "gateway_lease_name", "gateway_instance_id",
        "gateway_lease_epoch", "tool_call_id", "tool_name", "arguments_json",
        "arguments_fingerprint", "recovery_policy", "status", "result_json",
        "external_operation_id", "attempt_count", "created_at", "updated_at",
    )
    values = dict(zip(fields, row))
    values["arguments"] = _deserialize_json(
        values.pop("arguments_json"), "arguments"
    )
    values["result"] = _deserialize_json(values.pop("result_json"), "result")
    return values


_TOOL_EXECUTION_COLUMNS = (
    "execution_id, environment, session_id, source_message_id, cron_run_id, "
    "gateway_lease_name, gateway_instance_id, gateway_lease_epoch, tool_call_id, "
    "tool_name, arguments_json, arguments_fingerprint, "
    "recovery_policy, status, result_json, external_operation_id, attempt_count, "
    "created_at, updated_at"
)


def create_tool_execution(
    conn: sqlite3.Connection,
    *,
    environment: str,
    tool_call_id: str,
    tool_name: str,
    arguments: Any,
    recovery_policy: str,
    session_id: str | None = None,
    source_message_id: str | None = None,
    cron_run_id: str | None = None,
    gateway_lease_name: str | None = None,
    gateway_instance_id: str | None = None,
    gateway_lease_epoch: int | None = None,
    execution_id: str | None = None,
) -> dict:
    """创建或按环境与 tool_call_id 返回同一条 prepared Journal 记录。"""
    normalized_environment = _require_nonempty_string(environment, "environment")
    normalized_call_id = _require_nonempty_string(tool_call_id, "tool_call_id")
    normalized_name = _require_nonempty_string(tool_name, "tool_name")
    normalized_policy = _require_nonempty_string(recovery_policy, "recovery_policy")
    if normalized_policy not in {
        "retry_safe", "unknown_on_crash", "status_check",
    }:
        raise DBError("tool execution recovery_policy is invalid")
    arguments_json = _serialize_json(arguments, "arguments")
    fingerprint = _arguments_fingerprint(arguments_json)
    now = time.time()

    with transaction(conn):
        row = conn.execute(
            f"""
            SELECT {_TOOL_EXECUTION_COLUMNS}
            FROM tool_executions
            WHERE environment=? AND tool_call_id=?
            """,
            (normalized_environment, normalized_call_id),
        ).fetchone()
        existing = _tool_execution_row(row)
        if existing is not None:
            if (
                existing["tool_name"] != normalized_name
                or existing["arguments_fingerprint"] != fingerprint
                or existing["recovery_policy"] != normalized_policy
            ):
                raise DBError("tool execution idempotency identity mismatch")
            return existing

        record_id = execution_id or str(uuid.uuid4())
        if not isinstance(record_id, str) or not record_id:
            raise DBError("tool execution execution_id must be a non-empty string")
        try:
            conn.execute(
                """
                INSERT INTO tool_executions (
                    execution_id, environment, session_id, source_message_id,
                    cron_run_id, gateway_lease_name, gateway_instance_id,
                    gateway_lease_epoch, tool_call_id, tool_name, arguments_json,
                    arguments_fingerprint, recovery_policy, status, result_json,
                    external_operation_id, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, 0, ?, ?)
                """,
                (
                    record_id, normalized_environment, session_id, source_message_id,
                    cron_run_id, gateway_lease_name, gateway_instance_id,
                    gateway_lease_epoch, normalized_call_id, normalized_name, arguments_json,
                    fingerprint, normalized_policy, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DBError(f"tool execution create failed: {exc}") from exc
    return get_tool_execution(conn, record_id)  # type: ignore[return-value]


def get_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
) -> dict | None:
    """按执行记录 ID 查询 Journal。"""
    row = conn.execute(
        f"SELECT {_TOOL_EXECUTION_COLUMNS} FROM tool_executions WHERE execution_id=?",
        (execution_id,),
    ).fetchone()
    return _tool_execution_row(row)


def get_tool_execution_by_call(
    conn: sqlite3.Connection,
    environment: str,
    tool_call_id: str,
) -> dict | None:
    """按幂等键查询同一运行环境中的工具调用。"""
    row = conn.execute(
        f"""
        SELECT {_TOOL_EXECUTION_COLUMNS}
        FROM tool_executions
        WHERE environment=? AND tool_call_id=?
        """,
        (environment, tool_call_id),
    ).fetchone()
    return _tool_execution_row(row)


def start_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    external_operation_id: str | None = None,
) -> dict:
    """将 prepared 记录原子推进到 running，并累计本次执行尝试。"""
    now = time.time()
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE tool_executions
            SET status='running', attempt_count=attempt_count + 1,
                external_operation_id=COALESCE(?, external_operation_id), updated_at=?
            WHERE execution_id=? AND status='prepared'
            """,
            (external_operation_id, now, execution_id),
        ).rowcount
        record = get_tool_execution(conn, execution_id)
        if record is None:
            raise DBError("tool execution not found")
        if changed != 1:
            raise DBError(f"tool execution cannot start from status {record['status']!r}")
        return get_tool_execution(conn, execution_id)  # type: ignore[return-value]


def complete_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    result: Any,
    *,
    external_operation_id: str | None = None,
) -> dict:
    """将运行中的记录原子固化为 succeeded。"""
    return _finish_tool_execution(
        conn,
        execution_id,
        status="succeeded",
        result=result,
        external_operation_id=external_operation_id,
    )


def succeed_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    result: Any,
    *,
    external_operation_id: str | None = None,
) -> dict:
    """以更直接的成功语义完成运行中的工具执行记录。"""
    return complete_tool_execution(
        conn,
        execution_id,
        result,
        external_operation_id=external_operation_id,
    )


def fail_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    result: Any,
    *,
    external_operation_id: str | None = None,
) -> dict:
    """将运行中的记录原子固化为 failed。"""
    return _finish_tool_execution(
        conn,
        execution_id,
        status="failed",
        result=result,
        external_operation_id=external_operation_id,
    )


def _finish_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    status: str,
    result: Any,
    external_operation_id: str | None,
) -> dict:
    result_json = _serialize_json(result, "result")
    now = time.time()
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE tool_executions
            SET status=?, result_json=?,
                external_operation_id=COALESCE(?, external_operation_id), updated_at=?
            WHERE execution_id=? AND status='running'
            """,
            (status, result_json, external_operation_id, now, execution_id),
        ).rowcount
        record = get_tool_execution(conn, execution_id)
        if record is None:
            raise DBError("tool execution not found")
        if changed != 1:
            raise DBError(f"tool execution cannot finish from status {record['status']!r}")
        return get_tool_execution(conn, execution_id)  # type: ignore[return-value]


def mark_tool_execution_unknown(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    result: Any | None = None,
    external_operation_id: str | None = None,
) -> dict:
    """把尚未完成的调用标记为 unknown，表示外部副作用无法确认。"""
    result_json = None if result is None else _serialize_json(result, "result")
    now = time.time()
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE tool_executions
            SET status='unknown', result_json=COALESCE(?, result_json),
                external_operation_id=COALESCE(?, external_operation_id), updated_at=?
            WHERE execution_id=? AND status IN ('prepared', 'running')
            """,
            (result_json, external_operation_id, now, execution_id),
        ).rowcount
        record = get_tool_execution(conn, execution_id)
        if record is None:
            raise DBError("tool execution not found")
        if changed != 1:
            raise DBError(f"tool execution cannot become unknown from status {record['status']!r}")
        return get_tool_execution(conn, execution_id)  # type: ignore[return-value]


def retry_tool_execution(
    conn: sqlite3.Connection,
    execution_id: str,
) -> dict:
    """以原 execution_id 重新准备仅允许安全重试的中断调用。"""
    now = time.time()
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE tool_executions
            SET status='prepared', result_json=NULL, updated_at=?
            WHERE execution_id=? AND status IN ('prepared', 'running', 'unknown')
            """,
            (now, execution_id),
        ).rowcount
        record = get_tool_execution(conn, execution_id)
        if record is None:
            raise DBError("tool execution not found")
        if changed != 1:
            raise DBError(f"tool execution cannot retry from status {record['status']!r}")
        return get_tool_execution(conn, execution_id)  # type: ignore[return-value]


def list_gateway_incomplete_tool_executions(
    conn: sqlite3.Connection,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    limit: int = 100,
) -> list[dict]:
    """仅返回当前有效 Gateway runtime lease 所属的未确定执行。"""
    if not gateway_runtime_lease_is_valid(
        conn, lease_name, instance_id, lease_epoch,
    ):
        raise DBError("gateway tool recovery lease is not valid")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DBError("tool execution incomplete query limit must be positive")
    rows = conn.execute(
        f"""
        SELECT {_TOOL_EXECUTION_COLUMNS}
        FROM tool_executions
        WHERE environment='gateway'
          AND gateway_lease_name=?
          AND status IN ('prepared', 'running', 'unknown')
        ORDER BY updated_at, execution_id
        LIMIT ?
        """,
        (lease_name, limit),
    ).fetchall()
    return [record for row in rows if (record := _tool_execution_row(row)) is not None]


def list_cron_incomplete_tool_executions(
    conn: sqlite3.Connection,
    cron_run_id: str,
    *,
    lease_name: str,
    instance_id: str,
    lease_epoch: int,
    limit: int = 100,
) -> list[dict]:
    """仅返回当前有效 claim fence 所属 Cron Run 的未确定执行。"""
    if not gateway_runtime_lease_is_valid(
        conn, lease_name, instance_id, lease_epoch,
    ):
        raise DBError("cron tool recovery lease is not valid")
    run = conn.execute(
        """
        SELECT 1 FROM cron_runs
        WHERE run_id=? AND claim_lease_name=? AND claim_instance_id=?
          AND claim_epoch=?
        """,
        (cron_run_id, lease_name, instance_id, lease_epoch),
    ).fetchone()
    if run is None:
        raise DBError("cron tool recovery claim is not valid")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DBError("tool execution incomplete query limit must be positive")
    rows = conn.execute(
        f"""
        SELECT {_TOOL_EXECUTION_COLUMNS}
        FROM tool_executions
        WHERE environment='cron' AND cron_run_id=?
          AND status IN ('prepared', 'running', 'unknown')
        ORDER BY updated_at, execution_id
        LIMIT ?
        """,
        (cron_run_id, limit),
    ).fetchall()
    return [record for row in rows if (record := _tool_execution_row(row)) is not None]


def save_recovered_tool_execution_result(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    status: str,
    output: str,
) -> dict:
    """保存恢复结论，并在缺失时补入模型可见的 tool result。"""
    if status not in {"succeeded", "failed", "unknown"}:
        raise DBError("tool execution recovery status is invalid")
    now = time.time()
    with transaction(conn):
        record = get_tool_execution(conn, execution_id)
        if record is None:
            raise DBError("tool execution not found")
        if record["status"] in {"prepared", "running", "unknown"}:
            conn.execute(
                """
                UPDATE tool_executions
                SET status=?, result_json=?, updated_at=?
                WHERE execution_id=? AND status IN ('prepared', 'running', 'unknown')
                """,
                (status, _serialize_json({"output": output}, "result"), now, execution_id),
            )
        elif record["status"] != status:
            raise DBError("tool execution recovery conflicts with terminal status")

        session_id = record.get("session_id")
        if session_id:
            assistant_rows = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE session_id=? AND role='assistant' AND tool_calls IS NOT NULL
                  AND tool_calls LIKE ?
                ORDER BY id DESC LIMIT 1
                """,
                (session_id, f'%"id": "{record["tool_call_id"]}"%'),
            ).fetchone()
            if assistant_rows is None:
                _insert_message(
                    conn,
                    session_id,
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": record["tool_call_id"],
                            "type": "function",
                            "function": {
                                "name": record["tool_name"],
                                "arguments": _serialize_json(
                                    record["arguments"], "arguments"
                                ),
                            },
                        }],
                    },
                )
            exists = conn.execute(
                """
                SELECT 1 FROM messages
                WHERE session_id=? AND role='tool' AND tool_call_id=?
                LIMIT 1
                """,
                (session_id, record["tool_call_id"]),
            ).fetchone()
            if exists is None:
                _insert_message(
                    conn,
                    session_id,
                    {
                        "role": "tool",
                        "tool_call_id": record["tool_call_id"],
                        "content": output,
                    },
                )
    return get_tool_execution(conn, execution_id)  # type: ignore[return-value]


def list_incomplete_tool_executions(
    conn: sqlite3.Connection,
    *,
    environment: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """读取尚未取得确定结果的记录，供未来恢复流程决策。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise DBError("tool execution incomplete query limit must be positive")
    params: list[object] = []
    where = "status IN ('prepared', 'running', 'unknown')"
    if environment is not None:
        where += " AND environment=?"
        params.append(_require_nonempty_string(environment, "environment"))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT {_TOOL_EXECUTION_COLUMNS}
        FROM tool_executions
        WHERE {where}
        ORDER BY updated_at, execution_id
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [record for row in rows if (record := _tool_execution_row(row)) is not None]
