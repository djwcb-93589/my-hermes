"""基于现有安全事实表的 SQLite 监控聚合只读实现。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hermes.observability.monitoring import (
    MonitoringRecordInvalid,
    MonitoringRepositoryUnavailable,
)
from hermes.observability.monitoring_aggregation import (
    MAX_MONITORING_TIME_BUCKETS,
    MAX_MONITORING_TOOL_STATS,
    FinishReasonCategory,
    FinishReasonCount,
    ModelCallMetrics,
    MonitoringAggregationQuery,
    MonitoringGranularity,
    MonitoringOverview,
    MonitoringTimeBucket,
    RunMetrics,
    ToolCallMetrics,
    ToolErrorCategory,
    ToolErrorCount,
    ToolExecutionMetrics,
    ToolStats,
    ToolStatsItem,
)

from .read_only import readonly_connection
from .schema import LATEST_SCHEMA_VERSION


_FAILED_RUN_STATUSES = (
    "error",
    "hook_blocked",
    "max_iterations",
    "model_error",
    "tool_error",
)
_FINISH_REASON_VALUES = tuple(
    category.value
    for category in FinishReasonCategory
    if category.value != "other"
)
_TOOL_ERROR_VALUES = tuple(
    category.value
    for category in ToolErrorCategory
    if category.value != "other"
)
_FINISH_REASON_PLACEHOLDERS = ", ".join(
    "?" for _ in _FINISH_REASON_VALUES
)
_TOOL_ERROR_PLACEHOLDERS = ", ".join(
    "?" for _ in _TOOL_ERROR_VALUES
)

_RUN_METRICS_SQL = f"""
    SELECT
        COUNT(*) AS run_count,
        COALESCE(SUM(
            CASE
                WHEN status='completed' AND stop_reason='completed'
                THEN 1 ELSE 0
            END
        ), 0) AS completed_count,
        COALESCE(SUM(
            CASE
                WHEN status!='cancelled'
                    AND stop_reason!='cancelled'
                    AND status IN ({", ".join("?" for _ in _FAILED_RUN_STATUSES)})
                THEN 1 ELSE 0
            END
        ), 0) AS failed_count,
        COALESCE(SUM(
            CASE
                WHEN status='cancelled' OR stop_reason='cancelled'
                THEN 1 ELSE 0
            END
        ), 0) AS cancelled_count,
        COALESCE(SUM(
            CASE
                WHEN NOT (
                    status='completed' AND stop_reason='completed'
                )
                    AND status!='cancelled'
                    AND stop_reason!='cancelled'
                    AND status NOT IN (
                        {", ".join("?" for _ in _FAILED_RUN_STATUSES)}
                    )
                THEN 1 ELSE 0
            END
        ), 0) AS other_terminal_count,
        CASE
            WHEN COUNT(*)=0 THEN NULL
            ELSE CAST(SUM(
                CASE
                    WHEN status='completed' AND stop_reason='completed'
                    THEN 1 ELSE 0
                END
            ) AS REAL) / COUNT(*)
        END AS success_rate,
        AVG(iterations) AS average_iterations,
        AVG(tool_call_count) AS average_tool_call_count,
        COALESCE(SUM(
            CASE WHEN has_final_reply=1 THEN 1 ELSE 0 END
        ), 0) AS runs_with_final_reply,
        COALESCE(SUM(
            CASE WHEN has_final_reply=0 THEN 1 ELSE 0 END
        ), 0) AS runs_without_final_reply
    FROM observations
    WHERE event_type='run_end'
        AND created_at>=?
        AND created_at<?
"""

_MODEL_METRICS_SQL = """
    SELECT
        COUNT(*) AS model_call_count,
        COALESCE(SUM(
            CASE WHEN has_text=1 THEN 1 ELSE 0 END
        ), 0) AS calls_with_text,
        COALESCE(SUM(
            CASE WHEN has_text=0 THEN 1 ELSE 0 END
        ), 0) AS calls_without_text,
        COALESCE(SUM(tool_call_count), 0) AS total_tool_call_count,
        AVG(tool_call_count) AS average_tool_call_count,
        SUM(
            CASE
                WHEN prompt_tokens IS NOT NULL
                    AND completion_tokens IS NOT NULL
                    AND total_tokens IS NOT NULL
                THEN prompt_tokens
                ELSE NULL
            END
        ) AS total_prompt_tokens,
        SUM(
            CASE
                WHEN prompt_tokens IS NOT NULL
                    AND completion_tokens IS NOT NULL
                    AND total_tokens IS NOT NULL
                THEN completion_tokens
                ELSE NULL
            END
        ) AS total_completion_tokens,
        SUM(
            CASE
                WHEN prompt_tokens IS NOT NULL
                    AND completion_tokens IS NOT NULL
                    AND total_tokens IS NOT NULL
                THEN total_tokens
                ELSE NULL
            END
        ) AS total_tokens,
        COALESCE(SUM(
            CASE
                WHEN prompt_tokens IS NOT NULL
                    AND completion_tokens IS NOT NULL
                    AND total_tokens IS NOT NULL
                THEN 1 ELSE 0
            END
        ), 0) AS token_coverage_count,
        AVG(duration_ms) AS average_duration_ms
    FROM observations
    WHERE event_type='model_call'
        AND created_at>=?
        AND created_at<?
"""

_FINISH_REASON_COUNTS_SQL = f"""
    SELECT
        CASE
            WHEN finish_reason IN ({_FINISH_REASON_PLACEHOLDERS})
            THEN finish_reason
            ELSE 'other'
        END AS reason_category,
        COUNT(*) AS reason_count
    FROM observations
    WHERE event_type='model_call'
        AND created_at>=?
        AND created_at<?
    GROUP BY reason_category
"""

_TOOL_CALL_METRICS_SQL = """
    SELECT
        COUNT(*) AS tool_call_count,
        COALESCE(SUM(
            CASE WHEN success=1 THEN 1 ELSE 0 END
        ), 0) AS successful_tool_call_count,
        COALESCE(SUM(
            CASE WHEN success=0 THEN 1 ELSE 0 END
        ), 0) AS failed_tool_call_count,
        CASE
            WHEN COUNT(*)=0 THEN NULL
            ELSE CAST(SUM(
                CASE WHEN success=1 THEN 1 ELSE 0 END
            ) AS REAL) / COUNT(*)
        END AS success_rate,
        AVG(duration_ms) AS average_duration_ms
    FROM observations
    WHERE event_type='tool_call'
        AND created_at>=?
        AND created_at<?
        AND (? IS NULL OR tool_name=?)
"""

_TOOL_ERROR_COUNTS_SQL = f"""
    SELECT
        CASE
            WHEN error_type IN ({_TOOL_ERROR_PLACEHOLDERS})
            THEN error_type
            ELSE 'other'
        END AS error_category,
        COUNT(*) AS error_count
    FROM observations
    WHERE event_type='tool_call'
        AND success=0
        AND created_at>=?
        AND created_at<?
        AND (? IS NULL OR tool_name=?)
    GROUP BY error_category
"""

_TOOL_EXECUTION_METRICS_SQL = """
    SELECT
        COUNT(*) AS execution_count,
        COALESCE(SUM(
            CASE WHEN status='prepared' THEN 1 ELSE 0 END
        ), 0) AS prepared_count,
        COALESCE(SUM(
            CASE WHEN status='awaiting_approval' THEN 1 ELSE 0 END
        ), 0) AS awaiting_approval_count,
        COALESCE(SUM(
            CASE WHEN status='running' THEN 1 ELSE 0 END
        ), 0) AS running_count,
        COALESCE(SUM(
            CASE WHEN status='succeeded' THEN 1 ELSE 0 END
        ), 0) AS succeeded_count,
        COALESCE(SUM(
            CASE WHEN status='failed' THEN 1 ELSE 0 END
        ), 0) AS failed_count,
        COALESCE(SUM(
            CASE WHEN status='unknown' THEN 1 ELSE 0 END
        ), 0) AS unknown_count,
        COALESCE(SUM(
            CASE WHEN result_json IS NOT NULL THEN 1 ELSE 0 END
        ), 0) AS with_result_count,
        COALESCE(SUM(
            CASE WHEN external_operation_id IS NOT NULL THEN 1 ELSE 0 END
        ), 0) AS with_external_operation_count,
        AVG(attempt_count) AS average_attempt_count
    FROM tool_executions
    WHERE updated_at>=?
        AND updated_at<?
        AND (? IS NULL OR environment=?)
        AND (? IS NULL OR tool_name=?)
"""

_TOOL_STATS_SQL = """
    SELECT
        tool_name,
        COUNT(*) AS call_count,
        COALESCE(SUM(
            CASE WHEN success=1 THEN 1 ELSE 0 END
        ), 0) AS success_count,
        COALESCE(SUM(
            CASE WHEN success=0 THEN 1 ELSE 0 END
        ), 0) AS failure_count,
        CAST(SUM(
            CASE WHEN success=1 THEN 1 ELSE 0 END
        ) AS REAL) / COUNT(*) AS success_rate,
        AVG(duration_ms) AS average_duration_ms
    FROM observations
    WHERE event_type='tool_call'
        AND created_at>=?
        AND created_at<?
        AND (? IS NULL OR tool_name=?)
    GROUP BY tool_name
    ORDER BY call_count DESC, tool_name ASC
    LIMIT ?
"""

_TIME_BUCKETS_SQL = """
    SELECT
        CAST(created_at / ? AS INTEGER) * ? AS bucket_started_at,
        COALESCE(SUM(
            CASE WHEN event_type='run_end' THEN 1 ELSE 0 END
        ), 0) AS run_count,
        COALESCE(SUM(
            CASE WHEN event_type='model_call' THEN 1 ELSE 0 END
        ), 0) AS model_call_count,
        COALESCE(SUM(
            CASE
                WHEN event_type='tool_call'
                    AND (? IS NULL OR tool_name=?)
                THEN 1 ELSE 0
            END
        ), 0) AS tool_call_count,
        COALESCE(SUM(
            CASE
                WHEN event_type='tool_call'
                    AND success=0
                    AND (? IS NULL OR tool_name=?)
                THEN 1 ELSE 0
            END
        ), 0) AS failed_tool_call_count
    FROM observations
    WHERE created_at>=?
        AND created_at<?
    GROUP BY bucket_started_at
    HAVING
        SUM(CASE WHEN event_type='run_end' THEN 1 ELSE 0 END)>0
        OR SUM(CASE WHEN event_type='model_call' THEN 1 ELSE 0 END)>0
        OR SUM(
            CASE
                WHEN event_type='tool_call'
                    AND (? IS NULL OR tool_name=?)
                THEN 1 ELSE 0
            END
        )>0
    ORDER BY bucket_started_at ASC
    LIMIT ?
"""


class SQLiteMonitoringAggregationRepository:
    """仅在有界时间窗口内聚合现有 Observation 和执行日志。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path):
        if not isinstance(db_path, (str, Path)):
            raise TypeError("db_path must be a path")
        normalized = str(db_path)
        if not normalized.strip():
            raise ValueError("db_path must be a non-empty path")
        self._db_path = normalized

    def overview(
        self,
        query: MonitoringAggregationQuery,
    ) -> MonitoringOverview:
        """在一次独立只读连接中读取四组总览指标。"""
        _require_query(query)
        environment = (
            query.environment.value
            if query.environment is not None
            else None
        )
        try:
            with readonly_connection(self._db_path) as conn:
                # 多条总览聚合共享同一只读快照，避免并发写入造成计数分裂。
                conn.execute("BEGIN")
                _validate_aggregation_schema(conn)
                run_row = conn.execute(
                    _RUN_METRICS_SQL,
                    (
                        *_FAILED_RUN_STATUSES,
                        *_FAILED_RUN_STATUSES,
                        query.started_at,
                        query.ended_at,
                    ),
                ).fetchone()
                model_row = conn.execute(
                    _MODEL_METRICS_SQL,
                    (query.started_at, query.ended_at),
                ).fetchone()
                finish_reason_rows = conn.execute(
                    _FINISH_REASON_COUNTS_SQL,
                    (
                        *_FINISH_REASON_VALUES,
                        query.started_at,
                        query.ended_at,
                    ),
                ).fetchall()
                tool_row = conn.execute(
                    _TOOL_CALL_METRICS_SQL,
                    (
                        query.started_at,
                        query.ended_at,
                        query.tool_name,
                        query.tool_name,
                    ),
                ).fetchone()
                tool_error_rows = conn.execute(
                    _TOOL_ERROR_COUNTS_SQL,
                    (
                        *_TOOL_ERROR_VALUES,
                        query.started_at,
                        query.ended_at,
                        query.tool_name,
                        query.tool_name,
                    ),
                ).fetchall()
                execution_row = conn.execute(
                    _TOOL_EXECUTION_METRICS_SQL,
                    (
                        query.started_at,
                        query.ended_at,
                        environment,
                        environment,
                        query.tool_name,
                        query.tool_name,
                    ),
                ).fetchone()
        except MonitoringRecordInvalid:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_aggregation_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise MonitoringRepositoryUnavailable(
                "database_unavailable"
            ) from exc

        try:
            runs = _run_metrics(run_row)
            model_calls = _model_call_metrics(
                model_row,
                finish_reason_rows,
            )
            tool_calls = _tool_call_metrics(tool_row, tool_error_rows)
            tool_executions = _tool_execution_metrics(execution_row)
            return MonitoringOverview(
                window=query.window,
                runs=runs,
                model_calls=model_calls,
                tool_calls=tool_calls,
                tool_executions=tool_executions,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc

    def tool_stats(
        self,
        query: MonitoringAggregationQuery,
    ) -> ToolStats:
        """按固定排序返回至多一百个工具聚合项。"""
        _require_query(query)
        rows = self._fetchall(
            _TOOL_STATS_SQL,
            (
                query.started_at,
                query.ended_at,
                query.tool_name,
                query.tool_name,
                MAX_MONITORING_TOOL_STATS,
            ),
        )
        try:
            items = tuple(_tool_stats_item(row) for row in rows)
            return ToolStats(
                window=query.window,
                items=items,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc

    def time_buckets(
        self,
        query: MonitoringAggregationQuery,
        granularity: MonitoringGranularity,
    ) -> tuple[MonitoringTimeBucket, ...]:
        """返回有限非空 UTC epoch 桶，空桶由上层服务补齐。"""
        _require_query(query)
        if not isinstance(granularity, MonitoringGranularity):
            raise TypeError("granularity must be a MonitoringGranularity")
        query.validate_granularity(granularity)
        seconds = granularity.seconds
        rows = self._fetchall(
            _TIME_BUCKETS_SQL,
            (
                seconds,
                seconds,
                query.tool_name,
                query.tool_name,
                query.tool_name,
                query.tool_name,
                query.started_at,
                query.ended_at,
                query.tool_name,
                query.tool_name,
                MAX_MONITORING_TIME_BUCKETS + 1,
            ),
        )
        if len(rows) > MAX_MONITORING_TIME_BUCKETS:
            raise MonitoringRecordInvalid()
        try:
            return tuple(_time_bucket(row) for row in rows)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MonitoringRecordInvalid() from exc

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> list[tuple]:
        """每个公开查询方法都以自身独立只读连接完成读取。"""
        try:
            with readonly_connection(self._db_path) as conn:
                _validate_aggregation_schema(conn)
                return list(conn.execute(sql, parameters).fetchall())
        except MonitoringRecordInvalid:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_aggregation_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise MonitoringRepositoryUnavailable(
                "database_unavailable"
            ) from exc


def _require_query(query: MonitoringAggregationQuery) -> None:
    if not isinstance(query, MonitoringAggregationQuery):
        raise TypeError("query must be a MonitoringAggregationQuery")


def _run_metrics(row: tuple | None) -> RunMetrics:
    if row is None or len(row) != 10:
        raise ValueError("run aggregate is invalid")
    metrics = RunMetrics(
        run_count=row[0],
        completed_count=row[1],
        failed_count=row[2],
        cancelled_count=row[3],
        other_terminal_count=row[4],
        success_rate=row[5],
        average_iterations=row[6],
        average_tool_call_count=row[7],
        runs_with_final_reply=row[8],
        runs_without_final_reply=row[9],
    )
    if (
        metrics.completed_count
        + metrics.failed_count
        + metrics.cancelled_count
        + metrics.other_terminal_count
        != metrics.run_count
        or (
            metrics.runs_with_final_reply
            + metrics.runs_without_final_reply
            != metrics.run_count
        )
    ):
        raise ValueError("run aggregate is inconsistent")
    return metrics


def _model_call_metrics(
    row: tuple | None,
    reason_rows: list[tuple],
) -> ModelCallMetrics:
    if row is None or len(row) != 10:
        raise ValueError("model call aggregate is invalid")
    reason_counts = _finish_reason_counts(reason_rows)
    metrics = ModelCallMetrics(
        model_call_count=row[0],
        calls_with_text=row[1],
        calls_without_text=row[2],
        total_tool_call_count=row[3],
        average_tool_call_count=row[4],
        total_prompt_tokens=row[5],
        total_completion_tokens=row[6],
        total_tokens=row[7],
        token_coverage_count=row[8],
        average_duration_ms=row[9],
        finish_reason_counts=reason_counts,
    )
    if (
        metrics.calls_with_text + metrics.calls_without_text
        != metrics.model_call_count
        or sum(item.count for item in reason_counts)
        != metrics.model_call_count
    ):
        raise ValueError("model call aggregate is inconsistent")
    return metrics


def _tool_call_metrics(
    row: tuple | None,
    error_rows: list[tuple],
) -> ToolCallMetrics:
    if row is None or len(row) != 5:
        raise ValueError("tool call aggregate is invalid")
    error_counts = _tool_error_counts(error_rows)
    metrics = ToolCallMetrics(
        tool_call_count=row[0],
        successful_tool_call_count=row[1],
        failed_tool_call_count=row[2],
        success_rate=row[3],
        average_duration_ms=row[4],
        error_type_counts=error_counts,
    )
    if (
        metrics.successful_tool_call_count
        + metrics.failed_tool_call_count
        != metrics.tool_call_count
        or sum(item.count for item in error_counts)
        != metrics.failed_tool_call_count
    ):
        raise ValueError("tool call aggregate is inconsistent")
    return metrics


def _tool_execution_metrics(row: tuple | None) -> ToolExecutionMetrics:
    if row is None or len(row) != 10:
        raise ValueError("tool execution aggregate is invalid")
    metrics = ToolExecutionMetrics(
        execution_count=row[0],
        prepared_count=row[1],
        awaiting_approval_count=row[2],
        running_count=row[3],
        succeeded_count=row[4],
        failed_count=row[5],
        unknown_count=row[6],
        with_result_count=row[7],
        with_external_operation_count=row[8],
        average_attempt_count=row[9],
    )
    if (
        metrics.prepared_count
        + metrics.awaiting_approval_count
        + metrics.running_count
        + metrics.succeeded_count
        + metrics.failed_count
        + metrics.unknown_count
        != metrics.execution_count
    ):
        raise ValueError("tool execution aggregate is inconsistent")
    return metrics


def _finish_reason_counts(
    rows: list[tuple],
) -> tuple[FinishReasonCount, ...]:
    counts = _controlled_category_counts(
        rows,
        FinishReasonCategory,
    )
    return tuple(
        FinishReasonCount(category=category, count=count)
        for category, count in counts
    )


def _tool_error_counts(
    rows: list[tuple],
) -> tuple[ToolErrorCount, ...]:
    counts = _controlled_category_counts(rows, ToolErrorCategory)
    return tuple(
        ToolErrorCount(category=category, count=count)
        for category, count in counts
    )


def _controlled_category_counts(
    rows: list[tuple],
    enum_type: type[FinishReasonCategory] | type[ToolErrorCategory],
) -> tuple[
    tuple[FinishReasonCategory | ToolErrorCategory, int],
    ...,
]:
    by_value: dict[str, int] = {}
    for row in rows:
        if (
            len(row) != 2
            or type(row[0]) is not str
            or type(row[1]) is not int
            or row[1] < 1
            or row[0] in by_value
        ):
            raise ValueError("category aggregate is invalid")
        by_value[row[0]] = row[1]
    allowed = {category.value: category for category in enum_type}
    if not set(by_value).issubset(allowed):
        raise ValueError("category aggregate is invalid")
    return tuple(
        (category, by_value[category.value])
        for category in enum_type
        if category.value in by_value
    )


def _tool_stats_item(row: tuple) -> ToolStatsItem:
    if len(row) != 6:
        raise ValueError("tool stats aggregate is invalid")
    item = ToolStatsItem(
        tool_name=row[0],
        call_count=row[1],
        success_count=row[2],
        failure_count=row[3],
        success_rate=row[4],
        average_duration_ms=row[5],
    )
    if item.success_count + item.failure_count != item.call_count:
        raise ValueError("tool stats aggregate is inconsistent")
    return item


def _time_bucket(row: tuple) -> MonitoringTimeBucket:
    if len(row) != 5:
        raise ValueError("time bucket aggregate is invalid")
    bucket = MonitoringTimeBucket(
        bucket_started_at=row[0],
        run_count=row[1],
        model_call_count=row[2],
        tool_call_count=row[3],
        failed_tool_call_count=row[4],
    )
    if bucket.failed_tool_call_count > bucket.tool_call_count:
        raise ValueError("time bucket aggregate is inconsistent")
    return bucket


def _validate_aggregation_schema(conn: sqlite3.Connection) -> None:
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


def _sqlite_aggregation_error(
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
            "integer overflow",
        )
    ):
        return MonitoringRecordInvalid()
    return MonitoringRepositoryUnavailable("database_unavailable")


__all__ = ["SQLiteMonitoringAggregationRepository"]
