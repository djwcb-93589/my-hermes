"""安全运行事件与工具执行 Journal 的只读路由。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request

from hermes.observability.monitoring import (
    ModelCallObservationView,
    ObservationSummary,
    RunObservationView,
    ToolCallObservationView,
)
from hermes.observability.tool_execution import ToolExecutionSummary
from hermes.web.monitoring_service import MonitoringReadService
from hermes.web.pagination import PageParams, page_params
from hermes.web.read_context import ReadDataUnavailable
from hermes.web.schemas import (
    ModelObservationResponse,
    ObservationListResponse,
    ObservationResponse,
    RunObservationResponse,
    RunTimelineResponse,
    ToolExecutionDetailResponse,
    ToolExecutionListItem,
    ToolExecutionListResponse,
    ToolObservationResponse,
)


router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


def _service(request: Request) -> MonitoringReadService:
    """取得装配期注入的应用服务，不在路由中创建持久化依赖。"""
    service = getattr(request.app.state, "monitoring_read_service", None)
    if service is None:
        raise ReadDataUnavailable("data_unavailable")
    return service


@router.get(
    "/tool-executions",
    response_model=ToolExecutionListResponse,
)
def list_tool_executions(
    request: Request,
    page: PageParams = Depends(page_params),
    environment: str | None = Query(default=None, min_length=1, max_length=256),
    status: str | None = Query(default=None, min_length=1, max_length=256),
    tool_name: str | None = Query(default=None, min_length=1, max_length=256),
    session_id: str | None = Query(default=None, min_length=1, max_length=256),
    cron_run_id: str | None = Query(default=None, min_length=1, max_length=256),
) -> ToolExecutionListResponse:
    """分页读取不含参数、结果和 fencing 身份的执行摘要。"""
    result = _service(request).list_tool_executions(
        environment=environment,
        status=status,
        tool_name=tool_name,
        session_id=session_id,
        cron_run_id=cron_run_id,
        limit=page.limit,
        offset=page.offset,
    )
    return ToolExecutionListResponse(
        items=[_tool_execution_item(item) for item in result.items],
        limit=result.limit,
        offset=result.offset,
        has_more=result.has_more,
    )


@router.get(
    "/tool-executions/{execution_id}",
    response_model=ToolExecutionDetailResponse,
)
def get_tool_execution(
    request: Request,
    execution_id: str,
) -> ToolExecutionDetailResponse:
    """读取单条 Tool Execution 的安全详情。"""
    item = _service(request).get_tool_execution(execution_id)
    return _tool_execution_detail(item)


@router.get(
    "/observations",
    response_model=ObservationListResponse,
)
def list_observations(
    request: Request,
    page: PageParams = Depends(page_params),
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    run_id: str | None = Query(default=None, min_length=1, max_length=256),
    parent_run_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=256,
    ),
    tool_name: str | None = Query(default=None, min_length=1, max_length=256),
    status: str | None = Query(default=None, min_length=1, max_length=256),
    started_at: float | None = Query(default=None, ge=0),
    ended_at: float | None = Query(default=None, ge=0),
) -> ObservationListResponse:
    """分页读取三类类型化安全 Observation。"""
    result = _service(request).list_observations(
        event_type=event_type,
        run_id=run_id,
        parent_run_id=parent_run_id,
        tool_name=tool_name,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        limit=page.limit,
        offset=page.offset,
    )
    return ObservationListResponse(
        items=[_observation_response(item) for item in result.items],
        limit=result.limit,
        offset=result.offset,
        has_more=result.has_more,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunTimelineResponse,
)
def get_run_timeline(
    request: Request,
    run_id: str,
    page: PageParams = Depends(page_params),
) -> RunTimelineResponse:
    """按创建时间正序读取单次运行的安全事件时间线。"""
    result = _service(request).get_run_timeline(
        run_id,
        limit=page.limit,
        offset=page.offset,
    )
    return RunTimelineResponse(
        run_id=run_id,
        items=[_observation_response(item) for item in result.items],
        limit=result.limit,
        offset=result.offset,
        has_more=result.has_more,
    )


def _tool_execution_item(
    item: ToolExecutionSummary,
) -> ToolExecutionListItem:
    """逐字段转换安全摘要，避免 Row 或未知字段透传。"""
    return ToolExecutionListItem(
        execution_id=item.execution_id,
        environment=item.environment,
        session_id=item.session_id,
        source_message_id=item.source_message_id,
        cron_run_id=item.cron_run_id,
        tool_call_id=item.tool_call_id,
        tool_name=item.tool_name,
        recovery_policy=item.recovery_policy,
        status=item.status,
        attempt_count=item.attempt_count,
        has_result=item.has_result,
        has_external_operation=item.has_external_operation,
        created_at=_utc_datetime(item.created_at),
        updated_at=_utc_datetime(item.updated_at),
    )


def _tool_execution_detail(
    item: ToolExecutionSummary,
) -> ToolExecutionDetailResponse:
    """将复用的中立摘要投影为独立详情响应模型。"""
    summary = _tool_execution_item(item)
    return ToolExecutionDetailResponse(
        execution_id=summary.execution_id,
        environment=summary.environment,
        session_id=summary.session_id,
        source_message_id=summary.source_message_id,
        cron_run_id=summary.cron_run_id,
        tool_call_id=summary.tool_call_id,
        tool_name=summary.tool_name,
        recovery_policy=summary.recovery_policy,
        status=summary.status,
        attempt_count=summary.attempt_count,
        has_result=summary.has_result,
        has_external_operation=summary.has_external_operation,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
    )


def _observation_response(item: ObservationSummary) -> ObservationResponse:
    """按明确事件类型构造响应，未知投影统一视为损坏数据。"""
    created_at = _utc_datetime(item.created_at)
    if isinstance(item, ToolCallObservationView):
        return ToolObservationResponse(
            observation_id=item.observation_id,
            run_id=item.run_id,
            parent_run_id=item.parent_run_id,
            created_at=created_at,
            tool_call_id=item.tool_call_id,
            tool_name=item.tool_name,
            status=item.status,
            success=item.success,
            error_type=item.error_type,
            duration_ms=item.duration_ms,
        )
    if isinstance(item, ModelCallObservationView):
        return ModelObservationResponse(
            observation_id=item.observation_id,
            run_id=item.run_id,
            parent_run_id=item.parent_run_id,
            created_at=created_at,
            finish_reason=item.finish_reason,
            has_text=item.has_text,
            tool_call_count=item.tool_call_count,
            prompt_tokens=item.prompt_tokens,
            completion_tokens=item.completion_tokens,
            total_tokens=item.total_tokens,
            duration_ms=item.duration_ms,
        )
    if isinstance(item, RunObservationView):
        return RunObservationResponse(
            observation_id=item.observation_id,
            run_id=item.run_id,
            parent_run_id=item.parent_run_id,
            created_at=created_at,
            status=item.status,
            stop_reason=item.stop_reason,
            iterations=item.iterations,
            tool_call_count=item.tool_call_count,
            has_final_reply=item.has_final_reply,
        )
    raise ReadDataUnavailable("data_invalid")


def _utc_datetime(value: float) -> datetime:
    """将已校验的 Unix 时间转换为明确 UTC 时间。"""
    try:
        return datetime.fromtimestamp(value, UTC)
    except (TypeError, OverflowError, OSError, ValueError) as exc:
        raise ReadDataUnavailable("data_invalid") from exc
