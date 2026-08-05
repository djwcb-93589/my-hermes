"""Observation 与 Tool Execution 监控读取共用的中立契约。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias

from hermes.observability.tool_execution import (
    ToolExecutionDetail,
    ToolExecutionSummary,
)
from hermes.redaction import redact_explicit_secrets
from hermes.tool_policy import normalize_execution_environment


DEFAULT_MONITORING_PAGE_LIMIT = 50
MAX_MONITORING_PAGE_LIMIT = 200
_MAX_MONITORING_FETCH_LIMIT = MAX_MONITORING_PAGE_LIMIT + 1

_MAX_IDENTIFIER_LENGTH = 256
_MAX_LABEL_LENGTH = 128
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_TEXT_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_TOOL_EXECUTION_STATUSES = frozenset({
    "prepared",
    "awaiting_approval",
    "running",
    "succeeded",
    "failed",
    "unknown",
})


class ObservationEventType(str, Enum):
    """持久化 Observation 使用的稳定事件类型。"""

    TOOL_CALL = "tool_call"
    MODEL_CALL = "model_call"
    RUN_END = "run_end"


def _safe_text(
    value: object,
    field_name: str,
    *,
    label: bool = False,
) -> str:
    """只接收不会承载正文、路径或凭证的紧凑文本。"""
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} is invalid")
    limit = _MAX_LABEL_LENGTH if label else _MAX_IDENTIFIER_LENGTH
    pattern = _SAFE_LABEL_RE if label else _SAFE_IDENTIFIER_RE
    if (
        len(value) > limit
        or _CONTROL_TEXT_RE.search(value)
        or not pattern.fullmatch(value)
        or value.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
        or redact_explicit_secrets(value) != value
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_safe_text(
    value: object,
    field_name: str,
    *,
    label: bool = False,
) -> str | None:
    if value is None:
        return None
    return _safe_text(value, field_name, label=label)


def _timestamp(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _optional_timestamp(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    return _timestamp(value, field_name)


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_nonnegative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field_name)


def _page_limit(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_MONITORING_PAGE_LIMIT
    ):
        raise ValueError(
            "limit must be between 1 and "
            f"{MAX_MONITORING_PAGE_LIMIT}"
        )
    return value


def _query_limit(value: object) -> int:
    """允许应用层为判断 has_more 额外读取一条记录。"""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > _MAX_MONITORING_FETCH_LIMIT
    ):
        raise ValueError(
            "query limit must be between 1 and "
            f"{_MAX_MONITORING_FETCH_LIMIT}"
        )
    return value


def _page_offset(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be a non-negative integer")
    return value


def _event_type(
    value: ObservationEventType | str | None,
) -> ObservationEventType | None:
    if value is None:
        return None
    try:
        return (
            value
            if isinstance(value, ObservationEventType)
            else ObservationEventType(value)
        )
    except (TypeError, ValueError):
        raise ValueError("event_type is invalid") from None


@dataclass(frozen=True, slots=True)
class ObservationSummary:
    """所有 Observation 安全读取投影共用的关联和时间字段。"""

    observation_id: str
    event_type: ObservationEventType | str
    run_id: str
    parent_run_id: str | None
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            _safe_text(self.observation_id, "observation_id"),
        )
        normalized_event_type = _event_type(self.event_type)
        if normalized_event_type is None:
            raise ValueError("event_type is invalid")
        object.__setattr__(self, "event_type", normalized_event_type)
        object.__setattr__(self, "run_id", _safe_text(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "parent_run_id",
            _optional_safe_text(self.parent_run_id, "parent_run_id"),
        )
        object.__setattr__(
            self,
            "created_at",
            _timestamp(self.created_at, "created_at"),
        )


@dataclass(frozen=True, slots=True)
class ToolCallObservationView(ObservationSummary):
    """不含工具参数和结果的工具调用读取投影。"""

    tool_call_id: str
    tool_name: str
    status: str
    success: bool
    error_type: str | None
    duration_ms: int

    def __post_init__(self) -> None:
        ObservationSummary.__post_init__(self)
        if self.event_type is not ObservationEventType.TOOL_CALL:
            raise ValueError("tool call observation event_type is invalid")
        object.__setattr__(
            self,
            "tool_call_id",
            _safe_text(self.tool_call_id, "tool_call_id"),
        )
        object.__setattr__(
            self,
            "tool_name",
            _safe_text(self.tool_name, "tool_name", label=True),
        )
        object.__setattr__(
            self,
            "status",
            _safe_text(self.status, "status", label=True),
        )
        if type(self.success) is not bool:
            raise TypeError("success must be a boolean")
        object.__setattr__(
            self,
            "error_type",
            _optional_safe_text(self.error_type, "error_type", label=True),
        )
        object.__setattr__(
            self,
            "duration_ms",
            _nonnegative_int(self.duration_ms, "duration_ms"),
        )


@dataclass(frozen=True, slots=True)
class ModelCallObservationView(ObservationSummary):
    """不含 Prompt、回复或推理正文的模型调用读取投影。"""

    finish_reason: str | None
    has_text: bool
    tool_call_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None

    def __post_init__(self) -> None:
        ObservationSummary.__post_init__(self)
        if self.event_type is not ObservationEventType.MODEL_CALL:
            raise ValueError("model call observation event_type is invalid")
        object.__setattr__(
            self,
            "finish_reason",
            _optional_safe_text(
                self.finish_reason,
                "finish_reason",
                label=True,
            ),
        )
        if type(self.has_text) is not bool:
            raise TypeError("has_text must be a boolean")
        object.__setattr__(
            self,
            "tool_call_count",
            _nonnegative_int(self.tool_call_count, "tool_call_count"),
        )
        for field_name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_nonnegative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        for field_name in (
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_nonnegative_int(
                    getattr(self, field_name),
                    field_name,
                ),
            )
        cache_hit_tokens = self.prompt_cache_hit_tokens
        cache_miss_tokens = self.prompt_cache_miss_tokens
        if (cache_hit_tokens is None) != (cache_miss_tokens is None):
            raise ValueError(
                "prompt cache hit and miss tokens must be provided together"
            )
        if cache_hit_tokens is not None:
            if self.prompt_tokens is None:
                raise ValueError(
                    "prompt cache tokens require prompt_tokens"
                )
            if self.prompt_tokens != cache_hit_tokens + cache_miss_tokens:
                raise ValueError(
                    "prompt cache tokens must sum to prompt_tokens"
                )
        object.__setattr__(
            self,
            "duration_ms",
            _nonnegative_int(self.duration_ms, "duration_ms"),
        )


@dataclass(frozen=True, slots=True)
class RunObservationView(ObservationSummary):
    """不含最终回复正文的一次运行结束读取投影。"""

    status: str
    stop_reason: str
    iterations: int
    tool_call_count: int
    has_final_reply: bool

    def __post_init__(self) -> None:
        ObservationSummary.__post_init__(self)
        if self.event_type is not ObservationEventType.RUN_END:
            raise ValueError("run observation event_type is invalid")
        object.__setattr__(
            self,
            "status",
            _safe_text(self.status, "status", label=True),
        )
        object.__setattr__(
            self,
            "stop_reason",
            _safe_text(self.stop_reason, "stop_reason", label=True),
        )
        object.__setattr__(
            self,
            "iterations",
            _nonnegative_int(self.iterations, "iterations"),
        )
        object.__setattr__(
            self,
            "tool_call_count",
            _nonnegative_int(self.tool_call_count, "tool_call_count"),
        )
        if type(self.has_final_reply) is not bool:
            raise TypeError("has_final_reply must be a boolean")


RunTimelineEntry: TypeAlias = (
    ToolCallObservationView
    | ModelCallObservationView
    | RunObservationView
)


@dataclass(frozen=True, slots=True)
class ObservationQuery:
    """已规范化的 Observation 列表过滤与分页条件。"""

    event_type: ObservationEventType | str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    limit: int = DEFAULT_MONITORING_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", _event_type(self.event_type))
        for field_name in ("run_id", "parent_run_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_safe_text(getattr(self, field_name), field_name),
            )
        for field_name in ("tool_name", "status"):
            object.__setattr__(
                self,
                field_name,
                _optional_safe_text(
                    getattr(self, field_name),
                    field_name,
                    label=True,
                ),
            )
        object.__setattr__(
            self,
            "started_at",
            _optional_timestamp(self.started_at, "started_at"),
        )
        object.__setattr__(
            self,
            "ended_at",
            _optional_timestamp(self.ended_at, "ended_at"),
        )
        if (
            self.started_at is not None
            and self.ended_at is not None
            and self.started_at > self.ended_at
        ):
            raise ValueError("started_at must not be after ended_at")
        object.__setattr__(self, "limit", _query_limit(self.limit))
        object.__setattr__(self, "offset", _page_offset(self.offset))


@dataclass(frozen=True, slots=True)
class ToolExecutionQuery:
    """已规范化的 Tool Execution 列表过滤与分页条件。"""

    environment: str | None = None
    status: str | None = None
    tool_name: str | None = None
    session_id: str | None = None
    cron_run_id: str | None = None
    limit: int = DEFAULT_MONITORING_PAGE_LIMIT
    offset: int = 0

    def __post_init__(self) -> None:
        if self.environment is not None:
            try:
                environment = normalize_execution_environment(
                    self.environment
                ).value
            except (TypeError, ValueError):
                raise ValueError("environment is invalid") from None
            object.__setattr__(self, "environment", environment)
        if self.status is not None:
            status = _safe_text(self.status, "status", label=True)
            if status not in _TOOL_EXECUTION_STATUSES:
                raise ValueError("status is invalid")
            object.__setattr__(self, "status", status)
        for field_name in ("tool_name",):
            object.__setattr__(
                self,
                field_name,
                _optional_safe_text(
                    getattr(self, field_name),
                    field_name,
                    label=True,
                ),
            )
        for field_name in ("session_id", "cron_run_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_safe_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "limit", _query_limit(self.limit))
        object.__setattr__(self, "offset", _page_offset(self.offset))


@dataclass(frozen=True, slots=True)
class ObservationPage:
    """一页明确类型的 Observation 安全投影。"""

    items: tuple[RunTimelineEntry, ...]
    limit: int
    offset: int
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            not isinstance(
                item,
                (
                    ToolCallObservationView,
                    ModelCallObservationView,
                    RunObservationView,
                ),
            )
            for item in self.items
        ):
            raise TypeError("items must be a tuple of observation views")
        object.__setattr__(self, "limit", _page_limit(self.limit))
        object.__setattr__(self, "offset", _page_offset(self.offset))
        if type(self.has_more) is not bool:
            raise TypeError("has_more must be a boolean")


@dataclass(frozen=True, slots=True)
class ToolExecutionPage:
    """一页现有 Tool Execution Journal 安全投影。"""

    items: tuple[ToolExecutionSummary, ...]
    limit: int
    offset: int
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            not isinstance(item, ToolExecutionSummary)
            for item in self.items
        ):
            raise TypeError("items must be a tuple of tool execution summaries")
        object.__setattr__(self, "limit", _page_limit(self.limit))
        object.__setattr__(self, "offset", _page_offset(self.offset))
        if type(self.has_more) is not bool:
            raise TypeError("has_more must be a boolean")


class MonitoringRepositoryError(Exception):
    """不携带 SQL、路径或损坏记录内容的中立读取错误。"""

    def __init__(self, reason_code: str):
        self.reason_code = _safe_text(
            reason_code,
            "reason_code",
            label=True,
        )
        super().__init__(self.reason_code)


class MonitoringRepositoryUnavailable(MonitoringRepositoryError):
    """底层数据源当前无法安全读取。"""

    def __init__(self, reason_code: str = "database_unavailable"):
        super().__init__(reason_code)


class MonitoringRecordInvalid(MonitoringRepositoryError):
    """持久化记录不符合中立安全投影契约。"""

    def __init__(self):
        super().__init__("data_invalid")


class ObservationReadRepository(Protocol):
    """只读 Observation 的中立仓储边界。"""

    def list_observations(
        self,
        query: ObservationQuery,
    ) -> tuple[RunTimelineEntry, ...]:
        """按固定过滤条件读取不超过 query.limit 条 Observation。"""

    def list_run_timeline(
        self,
        run_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[RunTimelineEntry, ...] | None:
        """读取运行时间线；None 表示 run 不存在。"""


class ToolExecutionReadRepository(Protocol):
    """只读现有 Tool Execution Journal 的中立仓储边界。"""

    def list_tool_executions(
        self,
        query: ToolExecutionQuery,
    ) -> tuple[ToolExecutionSummary, ...]:
        """按固定过滤条件读取不超过 query.limit 条安全摘要。"""

    def get_tool_execution(
        self,
        execution_id: str,
    ) -> ToolExecutionDetail | None:
        """按 execution_id 读取安全详情。"""


__all__ = [
    "DEFAULT_MONITORING_PAGE_LIMIT",
    "MAX_MONITORING_PAGE_LIMIT",
    "ModelCallObservationView",
    "MonitoringRecordInvalid",
    "MonitoringRepositoryError",
    "MonitoringRepositoryUnavailable",
    "ObservationEventType",
    "ObservationPage",
    "ObservationQuery",
    "ObservationReadRepository",
    "ObservationSummary",
    "RunObservationView",
    "RunTimelineEntry",
    "ToolCallObservationView",
    "ToolExecutionPage",
    "ToolExecutionQuery",
    "ToolExecutionReadRepository",
    "ToolExecutionDetail",
    "ToolExecutionSummary",
]
