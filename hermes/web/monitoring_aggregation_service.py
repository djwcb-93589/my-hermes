"""Dashboard 监控聚合统计的独立应用服务。"""

from __future__ import annotations

import time
from collections.abc import Callable

from hermes.observability.monitoring import (
    MonitoringRecordInvalid,
    MonitoringRepositoryUnavailable,
)
from hermes.observability.monitoring_aggregation import (
    MonitoringAggregationQuery,
    MonitoringAggregationRepository,
    MonitoringGranularity,
    MonitoringOverview,
    MonitoringTimeBucket,
    MonitoringTimeSeries,
    ToolStats,
)
from hermes.tool_policy import ExecutionEnvironment
from hermes.web.read_context import ReadDataUnavailable, ReadInvalidRequest


_UNAVAILABLE_REASON_CODES = frozenset({
    "database_busy",
    "database_unavailable",
})


class MonitoringAggregationService:
    """只通过中立聚合 Repository Protocol 编排有界统计读取。"""

    __slots__ = ("_clock", "_repository")

    def __init__(
        self,
        repository: MonitoringAggregationRepository,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._repository = repository
        self._clock = clock

    def get_overview(
        self,
        *,
        started_at: float | None = None,
        ended_at: float | None = None,
        environment: ExecutionEnvironment | str | None = None,
        tool_name: str | None = None,
    ) -> MonitoringOverview:
        """返回窗口内 Run、Model、Tool 与执行 Journal 聚合。"""
        query = self._query(
            started_at=started_at,
            ended_at=ended_at,
            environment=environment,
            tool_name=tool_name,
        )
        try:
            overview = self._repository.overview(query)
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        if not isinstance(overview, MonitoringOverview):
            raise ReadDataUnavailable("data_invalid")
        if overview.window != query.window:
            raise ReadDataUnavailable("data_invalid")
        return overview

    def list_tool_stats(
        self,
        *,
        started_at: float | None = None,
        ended_at: float | None = None,
        tool_name: str | None = None,
    ) -> ToolStats:
        """返回固定排序、固定上限的按工具名称统计。"""
        query = self._query(
            started_at=started_at,
            ended_at=ended_at,
            environment=None,
            tool_name=tool_name,
        )
        try:
            stats = self._repository.tool_stats(query)
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        if not isinstance(stats, ToolStats) or stats.window != query.window:
            raise ReadDataUnavailable("data_invalid")
        return stats

    def get_time_series(
        self,
        *,
        started_at: float | None = None,
        ended_at: float | None = None,
        tool_name: str | None = None,
        granularity: MonitoringGranularity | str = MonitoringGranularity.HOUR,
    ) -> MonitoringTimeSeries:
        """读取 SQL 聚合桶并补齐窗口内有限的 UTC 空桶。"""
        query = self._query(
            started_at=started_at,
            ended_at=ended_at,
            environment=None,
            tool_name=tool_name,
        )
        try:
            normalized_granularity = query.validate_granularity(granularity)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadInvalidRequest() from exc
        try:
            buckets = self._repository.time_buckets(
                query,
                normalized_granularity,
            )
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        try:
            completed = _fill_time_buckets(
                query,
                normalized_granularity,
                buckets,
            )
            return MonitoringTimeSeries(
                window=query.window,
                granularity=normalized_granularity,
                buckets=completed,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def _query(
        self,
        *,
        started_at: float | None,
        ended_at: float | None,
        environment: ExecutionEnvironment | str | None,
        tool_name: str | None,
    ) -> MonitoringAggregationQuery:
        """统一解析默认窗口和受控过滤，不让 Route 或 Repository 猜测。"""
        try:
            now = (
                self._clock()
                if started_at is None and ended_at is None
                else 0.0
            )
            return MonitoringAggregationQuery.from_optional_window(
                started_at=started_at,
                ended_at=ended_at,
                now=now,
                environment=environment,
                tool_name=tool_name,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadInvalidRequest() from exc


def _fill_time_buckets(
    query: MonitoringAggregationQuery,
    granularity: MonitoringGranularity,
    buckets: tuple[MonitoringTimeBucket, ...],
) -> tuple[MonitoringTimeBucket, ...]:
    """按 Unix epoch 边界补零，不在 Python 中读取或聚合事件明细。"""
    if type(buckets) is not tuple or any(
        not isinstance(bucket, MonitoringTimeBucket)
        for bucket in buckets
    ):
        raise TypeError("buckets must be monitoring time buckets")
    expected_starts = query.bucket_starts(granularity)

    by_start: dict[int, MonitoringTimeBucket] = {}
    for bucket in buckets:
        if bucket.bucket_started_at in by_start:
            raise ValueError("time bucket is duplicated")
        by_start[bucket.bucket_started_at] = bucket
    if any(start not in expected_starts for start in by_start):
        raise ValueError("time bucket is outside the query window")

    return tuple(
        (
            by_start[started_at]
            if started_at in by_start
            else MonitoringTimeBucket(
                bucket_started_at=started_at,
                run_count=0,
                model_call_count=0,
                tool_call_count=0,
                failed_tool_call_count=0,
            )
        )
        for started_at in expected_starts
    )


def _unavailable_error(
    exc: MonitoringRepositoryUnavailable,
) -> ReadDataUnavailable:
    """仅映射稳定原因码，不传播底层异常文本。"""
    reason_code = getattr(exc, "reason_code", "database_unavailable")
    if reason_code not in _UNAVAILABLE_REASON_CODES:
        reason_code = "database_unavailable"
    return ReadDataUnavailable(reason_code)


__all__ = ["MonitoringAggregationService"]
