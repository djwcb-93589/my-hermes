"""Web API 的隔离响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermes.observability.monitoring_aggregation import (
    MAX_MONITORING_TIME_BUCKETS,
    MAX_MONITORING_TOOL_STATS,
    MAX_MONITORING_WINDOW_SECONDS,
    FinishReasonCategory,
    MonitoringGranularity,
    ToolErrorCategory,
)
from hermes.tool_policy import ExecutionEnvironment
from hermes.web.pagination import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT


class ErrorResponse(BaseModel):
    """对外稳定的错误结构，不包含内部异常信息。"""

    code: str
    message: str


class HealthzResponse(BaseModel):
    """无需认证的最小存活探针，不暴露运行环境细节。"""

    status: Literal["ok"] = "ok"


class DatabaseHealth(BaseModel):
    """数据库只读健康结果。"""

    status: Literal["healthy", "degraded", "unavailable"]
    schema_expected: int
    schema_actual: int | None = None
    required_tables_available: bool
    reason_code: str | None = None


class GatewayHealth(BaseModel):
    """Gateway runtime lease 的公开健康摘要。"""

    status: Literal["running", "stale", "stopped", "unavailable"]
    reason_code: str | None = None
    heartbeat_at: datetime | None = None
    expires_at: datetime | None = None


class StatusResponse(BaseModel):
    """服务和可安全读取依赖的状态摘要。"""

    application_name: str
    project_version: str | None = None
    web_status: str
    database: DatabaseHealth
    gateway: GatewayHealth
    # 保留旧字段以便旧面板平滑迁移；新代码应读取 database / gateway。
    gateway_status: str | None = Field(default=None, deprecated=True)
    database_status: str = Field(default="unavailable", deprecated=True)
    current_time: datetime


class SessionSummary(BaseModel):
    """会话列表的一条脱离持久层的摘要。"""

    conversation_id: str
    preview: str | None = None
    source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int | None = Field(default=None, ge=0)


class PaginationMetadata(BaseModel):
    """所有 Dashboard 列表响应共享的分页元数据。"""

    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
    )
    offset: int = Field(default=0, ge=0)
    has_more: bool = False


class SessionListResponse(PaginationMetadata):
    """分页会话列表。"""

    items: list[SessionSummary]


class MessageDetail(BaseModel):
    """会话中一条已持久化消息的安全表示。"""

    role: str
    content: str
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None
    timestamp: datetime | None = None


class SessionDetailResponse(BaseModel):
    """会话与其消息历史。"""

    conversation_id: str
    source: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    messages: list[MessageDetail]


class CronRunSummary(BaseModel):
    """Cron 任务一次历史运行的非敏感摘要。"""

    run_id: str
    status: str
    scheduled_for: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result_summary: str | None = None


class CronJobSummary(BaseModel):
    """Cron 任务定义的只读摘要。"""

    job_id: str
    name: str
    prompt_preview: str | None = None
    schedule: str
    timezone: str | None = None
    enabled: bool | None = None
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None


class CronJobDetailResponse(CronJobSummary):
    """Cron 任务定义及其公开运行历史。"""

    runs: list[CronRunSummary]
    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
    )
    offset: int = Field(default=0, ge=0)
    has_more: bool = False


class CronJobListResponse(PaginationMetadata):
    """Cron 任务列表。"""

    items: list[CronJobSummary]


class SkillSummary(BaseModel):
    """Skill 发现结果的非敏感部分。"""

    name: str
    description: str | None = None
    version: str | None = None
    available: bool


class SkillListResponse(PaginationMetadata):
    """Skill 目录。"""

    items: list[SkillSummary]


class ToolCapabilitySummary(BaseModel):
    """工具集目录中的单个声明能力，不包含 handler 或完整 schema。"""

    name: str
    description: str | None = None
    parameter_names: list[str]
    required_parameters: list[str]
    environments: list[str]
    default_environments: list[str]
    unattended_allowed: bool
    approval_mode: str
    risk_level: str
    retry_safe: bool
    unknown_on_crash: bool
    supports_cancellation: bool
    has_status_check: bool


class ToolsetSummary(BaseModel):
    """ToolRegistry 声明的一个工具集。"""

    name: str
    available: bool
    environments: list[str]
    tool_count: int = Field(default=0, ge=0)
    default_environments: list[str] = Field(default_factory=list)
    tools: list[ToolCapabilitySummary] = Field(default_factory=list)


class ToolsetListResponse(PaginationMetadata):
    """工具集目录。"""

    items: list[ToolsetSummary]
    tool_details_available: bool


class _MonitoringResponseModel(BaseModel):
    """监控 API 的严格响应基类，不透传未知投影字段。"""

    model_config = ConfigDict(extra="forbid")


_NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
_NonNegativeFiniteFloat = Annotated[
    float,
    Field(strict=True, ge=0, allow_inf_nan=False),
]
_Rate = Annotated[
    float,
    Field(strict=True, ge=0, le=1, allow_inf_nan=False),
]


class MonitoringPaginationResponse(PaginationMetadata):
    """监控列表复用现有分页字段，并拒绝额外响应字段。"""

    model_config = ConfigDict(extra="forbid")


class ToolExecutionListItem(_MonitoringResponseModel):
    """不含参数、结果正文和执行所有权信息的工具执行摘要。"""

    execution_id: str
    environment: str
    session_id: str | None = None
    source_message_id: str | None = None
    cron_run_id: str | None = None
    tool_call_id: str
    tool_name: str
    recovery_policy: str
    status: str
    attempt_count: int = Field(ge=0)
    has_result: bool
    has_external_operation: bool
    created_at: datetime
    updated_at: datetime


class ToolExecutionDetailResponse(ToolExecutionListItem):
    """单条 Tool Execution 的类型化安全详情。"""


class ToolExecutionListResponse(MonitoringPaginationResponse):
    """分页 Tool Execution 安全摘要。"""

    items: list[ToolExecutionListItem]


class _ObservationResponseBase(_MonitoringResponseModel):
    """三类 Observation 响应共享的安全关联字段。"""

    observation_id: str
    run_id: str
    parent_run_id: str | None = None
    created_at: datetime


class ToolObservationResponse(_ObservationResponseBase):
    """不包含工具参数和工具结果的调用事件。"""

    event_type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    status: str
    success: bool
    error_type: str | None = None
    duration_ms: int = Field(ge=0)


class ModelObservationResponse(_ObservationResponseBase):
    """不包含 Prompt 和模型回复正文的调用事件。"""

    event_type: Literal["model_call"] = "model_call"
    finish_reason: str | None = None
    has_text: bool
    tool_call_count: int = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int = Field(ge=0)


class RunObservationResponse(_ObservationResponseBase):
    """不包含最终回答正文的运行结束事件。"""

    event_type: Literal["run_end"] = "run_end"
    status: str
    stop_reason: str
    iterations: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    has_final_reply: bool


ObservationResponse = Annotated[
    ToolObservationResponse
    | ModelObservationResponse
    | RunObservationResponse,
    Field(discriminator="event_type"),
]


class ObservationListResponse(MonitoringPaginationResponse):
    """分页 Observation 安全投影。"""

    items: list[ObservationResponse]


class RunTimelineResponse(MonitoringPaginationResponse):
    """单个 run 的有界 Observation 时间线。"""

    run_id: str
    items: list[ObservationResponse]


class MonitoringWindowResponse(_MonitoringResponseModel):
    """聚合查询实际使用的有界 Unix 时间窗口和受控过滤。"""

    started_at: _NonNegativeFiniteFloat
    ended_at: _NonNegativeFiniteFloat
    environment: ExecutionEnvironment | None = None
    tool_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        """拒绝响应构造阶段出现的反向时间窗口。"""
        if self.started_at > self.ended_at:
            raise ValueError("started_at must not be after ended_at")
        if self.ended_at - self.started_at > MAX_MONITORING_WINDOW_SECONDS:
            raise ValueError("monitoring window exceeds the fixed limit")
        return self


class FinishReasonCountResponse(_MonitoringResponseModel):
    """单个受控模型完成原因的调用数。"""

    finish_reason: FinishReasonCategory
    count: _NonNegativeInt


class ToolErrorCountResponse(_MonitoringResponseModel):
    """单个受控工具错误类别的失败调用数。"""

    error_type: ToolErrorCategory
    count: _NonNegativeInt


class RunMetricsResponse(_MonitoringResponseModel):
    """基于 run_end Observation 的终态运行指标。"""

    run_count: _NonNegativeInt
    completed_count: _NonNegativeInt
    failed_count: _NonNegativeInt
    cancelled_count: _NonNegativeInt
    other_terminal_count: _NonNegativeInt
    success_rate: _Rate | None = None
    average_iterations: _NonNegativeFiniteFloat | None = None
    average_tool_call_count: _NonNegativeFiniteFloat | None = None
    runs_with_final_reply: _NonNegativeInt
    runs_without_final_reply: _NonNegativeInt


class ModelCallMetricsResponse(_MonitoringResponseModel):
    """不包含 Prompt 或回复正文的模型调用指标。"""

    model_call_count: _NonNegativeInt
    calls_with_text: _NonNegativeInt
    calls_without_text: _NonNegativeInt
    total_tool_call_count: _NonNegativeInt
    average_tool_call_count: _NonNegativeFiniteFloat | None = None
    total_prompt_tokens: _NonNegativeInt | None = None
    total_completion_tokens: _NonNegativeInt | None = None
    total_tokens: _NonNegativeInt | None = None
    token_coverage_count: _NonNegativeInt
    average_duration_ms: _NonNegativeFiniteFloat | None = None
    finish_reason_counts: list[FinishReasonCountResponse] = Field(
        default_factory=list,
        max_length=len(FinishReasonCategory),
    )


class ToolCallMetricsResponse(_MonitoringResponseModel):
    """不包含工具参数或结果正文的工具调用指标。"""

    tool_call_count: _NonNegativeInt
    successful_tool_call_count: _NonNegativeInt
    failed_tool_call_count: _NonNegativeInt
    success_rate: _Rate | None = None
    average_duration_ms: _NonNegativeFiniteFloat | None = None
    error_type_counts: list[ToolErrorCountResponse] = Field(
        default_factory=list,
        max_length=len(ToolErrorCategory),
    )


class ToolExecutionMetricsResponse(_MonitoringResponseModel):
    """现有 Tool Execution Journal 的当前状态分布。"""

    execution_count: _NonNegativeInt
    prepared_count: _NonNegativeInt
    awaiting_approval_count: _NonNegativeInt
    running_count: _NonNegativeInt
    succeeded_count: _NonNegativeInt
    failed_count: _NonNegativeInt
    unknown_count: _NonNegativeInt
    with_result_count: _NonNegativeInt
    with_external_operation_count: _NonNegativeInt
    average_attempt_count: _NonNegativeFiniteFloat | None = None


class MonitoringOverviewResponse(_MonitoringResponseModel):
    """一次有界查询中的四类聚合指标。"""

    window: MonitoringWindowResponse
    runs: RunMetricsResponse
    model_calls: ModelCallMetricsResponse
    tool_calls: ToolCallMetricsResponse
    tool_executions: ToolExecutionMetricsResponse


class ToolStatsItemResponse(_MonitoringResponseModel):
    """按稳定工具名称聚合的有限调用统计。"""

    tool_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    call_count: _NonNegativeInt
    success_count: _NonNegativeInt
    failure_count: _NonNegativeInt
    success_rate: _Rate | None = None
    average_duration_ms: _NonNegativeFiniteFloat | None = None


class ToolStatsResponse(_MonitoringResponseModel):
    """固定排序且最多一百项的工具调用聚合。"""

    window: MonitoringWindowResponse
    items: list[ToolStatsItemResponse] = Field(
        max_length=MAX_MONITORING_TOOL_STATS,
    )


class MonitoringTimeBucketResponse(_MonitoringResponseModel):
    """一个 UTC Unix 边界的固定趋势时间桶。"""

    bucket_started_at: _NonNegativeInt
    run_count: _NonNegativeInt
    model_call_count: _NonNegativeInt
    tool_call_count: _NonNegativeInt
    failed_tool_call_count: _NonNegativeInt


class MonitoringTimeSeriesResponse(_MonitoringResponseModel):
    """补齐空桶后的有界监控趋势。"""

    window: MonitoringWindowResponse
    granularity: MonitoringGranularity
    buckets: list[MonitoringTimeBucketResponse] = Field(
        max_length=MAX_MONITORING_TIME_BUCKETS,
    )


class CronControlResponse(BaseModel):
    """暂停或恢复 Cron 任务后的最小控制确认。"""

    job_id: str
    action: str
    status: str


class CronRunRequestResponse(BaseModel):
    """已持久化的手动运行请求，不代表任务已开始执行。"""

    job_id: str
    action: str
    status: str
    run_id: str
