"""有界监控聚合统计的只读 Dashboard 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from hermes.observability.monitoring_aggregation import (
    FinishReasonCount,
    ModelCallMetrics,
    MonitoringOverview,
    MonitoringTimeBucket,
    MonitoringTimeSeries,
    MonitoringWindow,
    RunMetrics,
    ToolCallMetrics,
    ToolErrorCount,
    ToolExecutionMetrics,
    ToolStats,
    ToolStatsItem,
)
from hermes.web.monitoring_aggregation_service import (
    MonitoringAggregationService,
)
from hermes.web.read_context import ReadDataUnavailable
from hermes.web.schemas import (
    FinishReasonCountResponse,
    ModelCallMetricsResponse,
    MonitoringOverviewResponse,
    MonitoringTimeBucketResponse,
    MonitoringTimeSeriesResponse,
    MonitoringWindowResponse,
    RunMetricsResponse,
    ToolCallMetricsResponse,
    ToolErrorCountResponse,
    ToolExecutionMetricsResponse,
    ToolStatsItemResponse,
    ToolStatsResponse,
)


router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


def _service(request: Request) -> MonitoringAggregationService:
    """取得装配期注入的聚合服务，不在路由中创建持久化依赖。"""
    service = getattr(
        request.app.state,
        "monitoring_aggregation_service",
        None,
    )
    if service is None:
        raise ReadDataUnavailable("data_unavailable")
    return service


@router.get(
    "/overview",
    response_model=MonitoringOverviewResponse,
)
def get_monitoring_overview(
    request: Request,
    started_at: float | None = Query(default=None, ge=0),
    ended_at: float | None = Query(default=None, ge=0),
    environment: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
    ),
    tool_name: str | None = Query(
        default=None,
        min_length=1,
        max_length=128,
    ),
) -> MonitoringOverviewResponse:
    """读取同一有界窗口中的四类类型化监控指标。"""
    overview = _service(request).get_overview(
        started_at=started_at,
        ended_at=ended_at,
        environment=environment,
        tool_name=tool_name,
    )
    return _overview_response(overview)


@router.get(
    "/tools/stats",
    response_model=ToolStatsResponse,
)
def list_monitoring_tool_stats(
    request: Request,
    started_at: float | None = Query(default=None, ge=0),
    ended_at: float | None = Query(default=None, ge=0),
    tool_name: str | None = Query(
        default=None,
        min_length=1,
        max_length=128,
    ),
) -> ToolStatsResponse:
    """读取固定排序且最多一百项的工具调用聚合。"""
    stats = _service(request).list_tool_stats(
        started_at=started_at,
        ended_at=ended_at,
        tool_name=tool_name,
    )
    return _tool_stats_response(stats)


@router.get(
    "/timeseries",
    response_model=MonitoringTimeSeriesResponse,
)
def get_monitoring_time_series(
    request: Request,
    started_at: float | None = Query(default=None, ge=0),
    ended_at: float | None = Query(default=None, ge=0),
    tool_name: str | None = Query(
        default=None,
        min_length=1,
        max_length=128,
    ),
    granularity: str = Query(
        default="1h",
        min_length=2,
        max_length=2,
    ),
) -> MonitoringTimeSeriesResponse:
    """读取受跨度限制的固定粒度趋势并返回已补齐空桶。"""
    series = _service(request).get_time_series(
        started_at=started_at,
        ended_at=ended_at,
        tool_name=tool_name,
        granularity=granularity,
    )
    return _time_series_response(series)


def _overview_response(
    overview: MonitoringOverview,
) -> MonitoringOverviewResponse:
    """逐字段转换聚合投影，不透传中立对象内部属性。"""
    return MonitoringOverviewResponse(
        window=_window_response(overview.window),
        runs=_run_metrics_response(overview.runs),
        model_calls=_model_metrics_response(overview.model_calls),
        tool_calls=_tool_metrics_response(overview.tool_calls),
        tool_executions=_execution_metrics_response(
            overview.tool_executions,
        ),
    )


def _window_response(window: MonitoringWindow) -> MonitoringWindowResponse:
    """转换查询窗口及其受控过滤。"""
    return MonitoringWindowResponse(
        started_at=window.started_at,
        ended_at=window.ended_at,
        environment=window.environment,
        tool_name=window.tool_name,
    )


def _run_metrics_response(metrics: RunMetrics) -> RunMetricsResponse:
    """转换 Run 指标。"""
    return RunMetricsResponse(
        run_count=metrics.run_count,
        completed_count=metrics.completed_count,
        failed_count=metrics.failed_count,
        cancelled_count=metrics.cancelled_count,
        other_terminal_count=metrics.other_terminal_count,
        success_rate=metrics.success_rate,
        average_iterations=metrics.average_iterations,
        average_tool_call_count=metrics.average_tool_call_count,
        runs_with_final_reply=metrics.runs_with_final_reply,
        runs_without_final_reply=metrics.runs_without_final_reply,
    )


def _model_metrics_response(
    metrics: ModelCallMetrics,
) -> ModelCallMetricsResponse:
    """转换 Model Call 指标和受控完成原因计数。"""
    return ModelCallMetricsResponse(
        model_call_count=metrics.model_call_count,
        calls_with_text=metrics.calls_with_text,
        calls_without_text=metrics.calls_without_text,
        total_tool_call_count=metrics.total_tool_call_count,
        average_tool_call_count=metrics.average_tool_call_count,
        total_prompt_tokens=metrics.total_prompt_tokens,
        total_completion_tokens=metrics.total_completion_tokens,
        total_tokens=metrics.total_tokens,
        token_coverage_count=metrics.token_coverage_count,
        average_duration_ms=metrics.average_duration_ms,
        finish_reason_counts=[
            _finish_reason_count_response(item)
            for item in metrics.finish_reason_counts
        ],
    )


def _finish_reason_count_response(
    item: FinishReasonCount,
) -> FinishReasonCountResponse:
    """转换单个固定完成原因计数。"""
    return FinishReasonCountResponse(
        finish_reason=item.category,
        count=item.count,
    )


def _tool_metrics_response(
    metrics: ToolCallMetrics,
) -> ToolCallMetricsResponse:
    """转换 Tool Call 指标和受控错误类别计数。"""
    return ToolCallMetricsResponse(
        tool_call_count=metrics.tool_call_count,
        successful_tool_call_count=metrics.successful_tool_call_count,
        failed_tool_call_count=metrics.failed_tool_call_count,
        success_rate=metrics.success_rate,
        average_duration_ms=metrics.average_duration_ms,
        error_type_counts=[
            _tool_error_count_response(item)
            for item in metrics.error_type_counts
        ],
    )


def _tool_error_count_response(
    item: ToolErrorCount,
) -> ToolErrorCountResponse:
    """转换单个固定工具错误类别计数。"""
    return ToolErrorCountResponse(
        error_type=item.category,
        count=item.count,
    )


def _execution_metrics_response(
    metrics: ToolExecutionMetrics,
) -> ToolExecutionMetricsResponse:
    """转换 Tool Execution 当前状态分布。"""
    return ToolExecutionMetricsResponse(
        execution_count=metrics.execution_count,
        prepared_count=metrics.prepared_count,
        awaiting_approval_count=metrics.awaiting_approval_count,
        running_count=metrics.running_count,
        succeeded_count=metrics.succeeded_count,
        failed_count=metrics.failed_count,
        unknown_count=metrics.unknown_count,
        with_result_count=metrics.with_result_count,
        with_external_operation_count=(
            metrics.with_external_operation_count
        ),
        average_attempt_count=metrics.average_attempt_count,
    )


def _tool_stats_response(stats: ToolStats) -> ToolStatsResponse:
    """逐项转换固定上限工具统计。"""
    return ToolStatsResponse(
        window=_window_response(stats.window),
        items=[_tool_stats_item_response(item) for item in stats.items],
    )


def _tool_stats_item_response(
    item: ToolStatsItem,
) -> ToolStatsItemResponse:
    """转换单个工具名称聚合。"""
    return ToolStatsItemResponse(
        tool_name=item.tool_name,
        call_count=item.call_count,
        success_count=item.success_count,
        failure_count=item.failure_count,
        success_rate=item.success_rate,
        average_duration_ms=item.average_duration_ms,
    )


def _time_series_response(
    series: MonitoringTimeSeries,
) -> MonitoringTimeSeriesResponse:
    """逐桶转换补齐后的趋势。"""
    return MonitoringTimeSeriesResponse(
        window=_window_response(series.window),
        granularity=series.granularity,
        buckets=[
            _time_bucket_response(bucket)
            for bucket in series.buckets
        ],
    )


def _time_bucket_response(
    bucket: MonitoringTimeBucket,
) -> MonitoringTimeBucketResponse:
    """转换单个固定 UTC Unix 时间桶。"""
    return MonitoringTimeBucketResponse(
        bucket_started_at=bucket.bucket_started_at,
        run_count=bucket.run_count,
        model_call_count=bucket.model_call_count,
        tool_call_count=bucket.tool_call_count,
        failed_tool_call_count=bucket.failed_tool_call_count,
    )


__all__ = ["router"]
