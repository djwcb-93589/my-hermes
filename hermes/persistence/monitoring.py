"""安全监控读取使用的两个独立 SQLite 只读 Repository。"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

from hermes.observability.monitoring import (
    ModelCallObservationView,
    MonitoringRecordInvalid,
    MonitoringRepositoryUnavailable,
    MAX_MONITORING_PAGE_LIMIT,
    ObservationEventType,
    ObservationQuery,
    RunObservationView,
    RunTimelineEntry,
    ToolCallObservationView,
    ToolExecutionDetail,
    ToolExecutionQuery,
)
from hermes.observability.tool_execution import (
    ToolExecutionSummary,
    project_tool_execution,
)
from hermes.tool_policy import ExecutionEnvironment

from .read_only import readonly_connection
from .schema import LATEST_SCHEMA_VERSION


_OBSERVATION_TIMELINE_COLUMNS = (
    "observation_id, event_type, run_id, parent_run_id, created_at, "
    "tool_call_id, tool_name, status, success, error_type, finish_reason, "
    "has_text, tool_call_count, prompt_tokens, completion_tokens, "
    "total_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens, "
    "duration_ms, stop_reason, iterations, has_final_reply"
)
_TOOL_EXECUTION_SAFE_COLUMNS = (
    "execution_id, environment, session_id, source_message_id, cron_run_id, "
    "tool_call_id, tool_name, recovery_policy, status, attempt_count, "
    "CASE WHEN result_json IS NULL THEN 0 ELSE 1 END AS has_result, "
    "CASE WHEN external_operation_id IS NULL THEN 0 ELSE 1 END "
    "AS has_external_operation, created_at, updated_at"
)
_TOOL_EXECUTION_SAFE_FIELD_NAMES = (
    "execution_id",
    "environment",
    "session_id",
    "source_message_id",
    "cron_run_id",
    "tool_call_id",
    "tool_name",
    "recovery_policy",
    "status",
    "attempt_count",
    "has_result",
    "has_external_operation",
    "created_at",
    "updated_at",
)
_TOOL_EXECUTION_ENVIRONMENTS = frozenset(
    environment.value for environment in ExecutionEnvironment
)
_TOOL_EXECUTION_RECOVERY_POLICIES = frozenset({
    "retry_safe",
    "unknown_on_crash",
    "status_check",
})
_TOOL_EXECUTION_STATUSES = frozenset({
    "prepared",
    "awaiting_approval",
    "running",
    "succeeded",
    "failed",
    "unknown",
})


class _SQLiteReadRepository:
    """只共享数据库路径和受控连接错误映射，不承载领域查询。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path):
        if not isinstance(db_path, (str, Path)):
            raise TypeError("db_path must be a path")
        normalized = str(db_path)
        if not normalized.strip():
            raise ValueError("db_path must be a non-empty path")
        self._db_path = normalized

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> list[tuple]:
        """执行固定 SQL 并在退出前复制结果，绝不返回连接或 Row。"""
        try:
            with readonly_connection(self._db_path) as conn:
                _validate_monitoring_schema(conn)
                return list(conn.execute(sql, parameters).fetchall())
        except sqlite3.Error as exc:
            raise _sqlite_read_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise MonitoringRepositoryUnavailable(
                "database_unavailable"
            ) from exc

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> tuple | None:
        """执行固定单行查询并关闭本次独立只读连接。"""
        try:
            with readonly_connection(self._db_path) as conn:
                _validate_monitoring_schema(conn)
                return conn.execute(sql, parameters).fetchone()
        except sqlite3.Error as exc:
            raise _sqlite_read_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise MonitoringRepositoryUnavailable(
                "database_unavailable"
            ) from exc


class SQLiteObservationReadRepository(_SQLiteReadRepository):
    """只读取 Observation 表并构造中立安全投影。"""

    def list_observations(
        self,
        query: ObservationQuery,
    ) -> tuple[RunTimelineEntry, ...]:
        """按固定倒序与有界分页读取 Observation 摘要。"""
        if not isinstance(query, ObservationQuery):
            raise TypeError("query must be an ObservationQuery")
        where, parameters = _observation_filters(query)
        rows = self._fetchall(
            f"""
            SELECT {_OBSERVATION_TIMELINE_COLUMNS}
            FROM observations
            {where}
            ORDER BY created_at DESC, observation_id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, query.limit, query.offset),
        )
        try:
            return tuple(_timeline_entry(row) for row in rows)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc

    def list_run_timeline(
        self,
        run_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[RunTimelineEntry, ...] | None:
        """在同一连接内区分运行不存在和分页越界后的空时间线。"""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if (
            type(limit) is not int
            or limit < 1
            or limit > MAX_MONITORING_PAGE_LIMIT + 1
        ):
            raise ValueError("limit is outside the monitoring read boundary")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        try:
            with readonly_connection(self._db_path) as conn:
                _validate_monitoring_schema(conn)
                exists = conn.execute(
                    "SELECT 1 FROM observations WHERE run_id=? LIMIT 1",
                    (run_id,),
                ).fetchone()
                if exists is None:
                    return None
                rows = conn.execute(
                    f"""
                    SELECT {_OBSERVATION_TIMELINE_COLUMNS}
                    FROM observations
                    WHERE run_id=?
                    ORDER BY created_at ASC, observation_id ASC
                    LIMIT ? OFFSET ?
                    """,
                    (run_id, limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise _sqlite_read_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise MonitoringRepositoryUnavailable(
                "database_unavailable"
            ) from exc
        try:
            return tuple(_timeline_entry(row) for row in rows)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc


class SQLiteToolExecutionReadRepository(_SQLiteReadRepository):
    """只读取现有 Tool Execution Journal 的安全摘要列。"""

    def list_tool_executions(
        self,
        query: ToolExecutionQuery,
    ) -> tuple[ToolExecutionSummary, ...]:
        """按固定倒序与有界分页读取工具执行摘要。"""
        if not isinstance(query, ToolExecutionQuery):
            raise TypeError("query must be a ToolExecutionQuery")
        where, parameters = _tool_execution_filters(query)
        rows = self._fetchall(
            f"""
            SELECT {_TOOL_EXECUTION_SAFE_COLUMNS}
            FROM tool_executions
            {where}
            ORDER BY updated_at DESC, execution_id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, query.limit, query.offset),
        )
        try:
            return tuple(
                _tool_execution_projection(row)
                for row in rows
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc

    def get_tool_execution(
        self,
        execution_id: str,
    ) -> ToolExecutionDetail | None:
        """按 ID 读取安全详情，不读取参数、结果或 fencing 身份。"""
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("execution_id must be a non-empty string")
        row = self._fetchone(
            f"""
            SELECT {_TOOL_EXECUTION_SAFE_COLUMNS}
            FROM tool_executions
            WHERE execution_id=?
            """,
            (execution_id,),
        )
        if row is None:
            return None
        try:
            return _tool_execution_projection(row)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc


def _observation_filters(
    query: ObservationQuery,
) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (
        (
            "event_type",
            query.event_type.value if query.event_type is not None else None,
        ),
        ("run_id", query.run_id),
        ("parent_run_id", query.parent_run_id),
        ("tool_name", query.tool_name),
        ("status", query.status),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            parameters.append(value)
    if query.started_at is not None:
        clauses.append("created_at>=?")
        parameters.append(query.started_at)
    if query.ended_at is not None:
        clauses.append("created_at<=?")
        parameters.append(query.ended_at)
    return (
        ("WHERE " + " AND ".join(clauses)) if clauses else "",
        tuple(parameters),
    )


def _tool_execution_filters(
    query: ToolExecutionQuery,
) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    parameters: list[object] = []
    for column, value in (
        ("environment", query.environment),
        ("status", query.status),
        ("tool_name", query.tool_name),
        ("session_id", query.session_id),
        ("cron_run_id", query.cron_run_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            parameters.append(value)
    return (
        ("WHERE " + " AND ".join(clauses)) if clauses else "",
        tuple(parameters),
    )


def _timeline_entry(row: tuple) -> RunTimelineEntry:
    if len(row) != 22:
        raise ValueError("observation record is invalid")
    common = {
        "observation_id": _required_text(row[0]),
        "event_type": _event_type(row[1]),
        "run_id": _required_text(row[2]),
        "parent_run_id": _optional_text(row[3]),
        "created_at": _timestamp(row[4]),
    }
    event_type = common["event_type"]
    if event_type is ObservationEventType.TOOL_CALL:
        _require_null_fields(
            row,
            (10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21),
        )
        return ToolCallObservationView(
            **common,
            tool_call_id=_required_text(row[5]),
            tool_name=_required_text(row[6]),
            status=_required_text(row[7]),
            success=_database_bool(row[8]),
            error_type=_optional_text(row[9]),
            duration_ms=_nonnegative_int(row[16]),
        )
    if event_type is ObservationEventType.MODEL_CALL:
        _require_null_fields(row, (5, 6, 7, 8, 9, 19, 20, 21))
        return ModelCallObservationView(
            **common,
            finish_reason=_optional_text(row[10]),
            has_text=_database_bool(row[11]),
            tool_call_count=_nonnegative_int(row[12]),
            prompt_tokens=_optional_nonnegative_int(row[13]),
            completion_tokens=_optional_nonnegative_int(row[14]),
            total_tokens=_optional_nonnegative_int(row[15]),
            prompt_cache_hit_tokens=_optional_nonnegative_int(row[16]),
            prompt_cache_miss_tokens=_optional_nonnegative_int(row[17]),
            duration_ms=_nonnegative_int(row[18]),
        )
    if event_type is ObservationEventType.RUN_END:
        _require_null_fields(
            row,
            (5, 6, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18),
        )
        return RunObservationView(
            **common,
            status=_required_text(row[7]),
            stop_reason=_required_text(row[19]),
            iterations=_nonnegative_int(row[20]),
            tool_call_count=_nonnegative_int(row[12]),
            has_final_reply=_database_bool(row[21]),
        )
    raise ValueError("observation event_type is invalid")


def _tool_execution_projection(row: tuple) -> ToolExecutionSummary:
    record = dict(zip(_TOOL_EXECUTION_SAFE_FIELD_NAMES, row, strict=True))
    if record["environment"] not in _TOOL_EXECUTION_ENVIRONMENTS:
        raise ValueError("tool execution environment is invalid")
    if record["recovery_policy"] not in _TOOL_EXECUTION_RECOVERY_POLICIES:
        raise ValueError("tool execution recovery_policy is invalid")
    if record["status"] not in _TOOL_EXECUTION_STATUSES:
        raise ValueError("tool execution status is invalid")
    return project_tool_execution(record)


def _require_null_fields(row: tuple, positions: tuple[int, ...]) -> None:
    if any(row[position] is not None for position in positions):
        raise ValueError("observation event shape is invalid")


def _event_type(value: object) -> ObservationEventType:
    if type(value) is not str:
        raise ValueError("observation event_type is invalid")
    try:
        return ObservationEventType(value)
    except ValueError as exc:
        raise ValueError("observation event_type is invalid") from exc


def _required_text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("observation text field is invalid")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _required_text(value)


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("observation count field is invalid")
    return value


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value)


def _database_bool(value: object) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError("observation boolean field is invalid")
    return bool(value)


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("observation timestamp is invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("observation timestamp is invalid")
    return normalized


def _sqlite_read_error(
    exc: sqlite3.Error,
) -> MonitoringRecordInvalid | MonitoringRepositoryUnavailable:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return MonitoringRepositoryUnavailable("database_busy")
    if any(
        marker in message
        for marker in (
            "no such table",
            "no such column",
            "malformed",
            "not a database",
            "database schema",
            "datatype mismatch",
        )
    ):
        return MonitoringRecordInvalid()
    return MonitoringRepositoryUnavailable("database_unavailable")


def _validate_monitoring_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if (
        row is None
        or len(row) != 1
        or type(row[0]) is not int
        or row[0] != LATEST_SCHEMA_VERSION
    ):
        raise MonitoringRecordInvalid()


__all__ = [
    "SQLiteObservationReadRepository",
    "SQLiteToolExecutionReadRepository",
]
