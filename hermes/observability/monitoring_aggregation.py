"""与 SQLite 和 Dashboard 无关的监控聚合查询契约。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from hermes.redaction import redact_explicit_secrets
from hermes.tool_policy import (
    ExecutionEnvironment,
    normalize_execution_environment,
)


DEFAULT_MONITORING_WINDOW_SECONDS = 24 * 60 * 60
MAX_MONITORING_WINDOW_SECONDS = 31 * 24 * 60 * 60
MAX_MONITORING_TOOL_STATS = 100
MAX_MONITORING_TIME_BUCKETS = 289

_MAX_LABEL_LENGTH = 128
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CONTROL_TEXT_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class MonitoringGranularity(str, Enum):
    """Dashboard 可以请求的固定 UTC 时间桶。"""

    FIVE_MINUTES = "5m"
    HOUR = "1h"
    DAY = "1d"

    @property
    def seconds(self) -> int:
        """返回固定粒度对应的秒数。"""
        return {
            MonitoringGranularity.FIVE_MINUTES: 5 * 60,
            MonitoringGranularity.HOUR: 60 * 60,
            MonitoringGranularity.DAY: 24 * 60 * 60,
        }[self]


class FinishReasonCategory(str, Enum):
    """允许公开的有限模型完成原因。"""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    FUNCTION_CALL = "function_call"
    OTHER = "other"


class ToolErrorCategory(str, Enum):
    """允许公开的有限工具错误类别。"""

    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DISABLED = "disabled"
    DISPATCH = "dispatch"
    FATAL_FLAGGED = "fatal_flagged"
    FORBIDDEN = "forbidden"
    HOOK_BLOCKED = "hook_blocked"
    INTERNAL_ERROR = "internal_error"
    JSON = "json"
    PATH_ESCAPE = "path_escape"
    PERMISSION_DENIED = "permission_denied"
    PERSISTENCE_ERROR = "persistence_error"
    PRIOR_TOOL_FAILURE = "prior_tool_failure"
    SAFETY_BLOCKED = "safety_blocked"
    UNKNOWN_ERROR = "unknown_error"
    UNKNOWN_TOOL = "unknown_tool"
    OTHER = "other"


def _timestamp(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _count(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_count(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return _count(value, field_name)


def _optional_metric(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _optional_rate(value: object, field_name: str) -> float | None:
    normalized = _optional_metric(value, field_name)
    if normalized is not None and normalized > 1:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _safe_label(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_LABEL_LENGTH
        or _CONTROL_TEXT_RE.search(value)
        or not _SAFE_LABEL_RE.fullmatch(value)
        or value.startswith(("/", "\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
        or redact_explicit_secrets(value) != value
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_safe_label(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _safe_label(value, field_name)


def _environment(
    value: ExecutionEnvironment | str | None,
) -> ExecutionEnvironment | None:
    if value is None:
        return None
    try:
        return normalize_execution_environment(value)
    except (TypeError, ValueError):
        raise ValueError("environment is invalid") from None


def _granularity(
    value: MonitoringGranularity | str,
) -> MonitoringGranularity:
    if isinstance(value, MonitoringGranularity):
        return value
    if type(value) is not str:
        raise TypeError("granularity must be a MonitoringGranularity or string")
    try:
        return MonitoringGranularity(value)
    except ValueError:
        raise ValueError("granularity is invalid") from None


def _validate_window(started_at: float, ended_at: float) -> None:
    if started_at > ended_at:
        raise ValueError("started_at must not be after ended_at")
    if ended_at - started_at > MAX_MONITORING_WINDOW_SECONDS:
        raise ValueError("monitoring window exceeds the fixed limit")


def _validate_average_presence(
    count: int,
    values: tuple[float | None, ...],
) -> None:
    if count == 0 and any(value is not None for value in values):
        raise ValueError("empty aggregate averages must be null")
    if count > 0 and any(value is None for value in values):
        raise ValueError("non-empty aggregate averages must be present")


def _validate_rate(
    rate: float | None,
    numerator: int,
    denominator: int,
) -> None:
    if denominator == 0:
        if rate is not None:
            raise ValueError("empty aggregate rate must be null")
        return
    if rate is None or not math.isclose(
        rate,
        numerator / denominator,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("aggregate rate is inconsistent")


@dataclass(frozen=True, slots=True)
class MonitoringWindow:
    """实际使用的半开时间窗口及维度过滤。"""

    started_at: float
    ended_at: float
    environment: ExecutionEnvironment | str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        started_at = _timestamp(self.started_at, "started_at")
        ended_at = _timestamp(self.ended_at, "ended_at")
        _validate_window(started_at, ended_at)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "ended_at", ended_at)
        object.__setattr__(
            self,
            "environment",
            _environment(self.environment),
        )
        object.__setattr__(
            self,
            "tool_name",
            _optional_safe_label(self.tool_name, "tool_name"),
        )


@dataclass(frozen=True, slots=True)
class MonitoringAggregationQuery:
    """所有聚合共用的有界半开查询 ``[started_at, ended_at)``。

    Run 与 Model 指标只按时间窗口聚合；tool_name 只过滤 Tool Call 和
    Tool Execution；environment 只过滤 Tool Execution。现有事实表没有
    可靠的跨领域关联字段，因此契约不会伪造其他维度关联。
    """

    started_at: float
    ended_at: float
    environment: ExecutionEnvironment | str | None = None
    tool_name: str | None = None

    def __post_init__(self) -> None:
        window = MonitoringWindow(
            started_at=self.started_at,
            ended_at=self.ended_at,
            environment=self.environment,
            tool_name=self.tool_name,
        )
        object.__setattr__(self, "started_at", window.started_at)
        object.__setattr__(self, "ended_at", window.ended_at)
        object.__setattr__(self, "environment", window.environment)
        object.__setattr__(self, "tool_name", window.tool_name)

    @classmethod
    def from_optional_window(
        cls,
        *,
        started_at: float | None,
        ended_at: float | None,
        now: float,
        environment: ExecutionEnvironment | str | None = None,
        tool_name: str | None = None,
    ) -> MonitoringAggregationQuery:
        """双空时使用最近 24 小时，单边时间窗口直接拒绝。"""
        if started_at is None and ended_at is None:
            normalized_now = _timestamp(now, "now")
            resolved_end = normalized_now
            resolved_start = max(
                0.0,
                resolved_end - DEFAULT_MONITORING_WINDOW_SECONDS,
            )
        elif started_at is None or ended_at is None:
            raise ValueError(
                "started_at and ended_at must be provided together"
            )
        else:
            resolved_start = started_at
            resolved_end = ended_at
        return cls(
            started_at=resolved_start,
            ended_at=resolved_end,
            environment=environment,
            tool_name=tool_name,
        )

    @property
    def window(self) -> MonitoringWindow:
        """返回与本查询完全一致的不可变窗口投影。"""
        return MonitoringWindow(
            started_at=self.started_at,
            ended_at=self.ended_at,
            environment=self.environment,
            tool_name=self.tool_name,
        )

    @property
    def span_seconds(self) -> float:
        """返回已校验的窗口跨度。"""
        return self.ended_at - self.started_at

    def validate_granularity(
        self,
        value: MonitoringGranularity | str,
    ) -> MonitoringGranularity:
        """按窗口跨度限制可选粒度并返回共享枚举。"""
        normalized = _granularity(value)
        span = self.span_seconds
        if span <= DEFAULT_MONITORING_WINDOW_SECONDS:
            allowed = frozenset({
                MonitoringGranularity.FIVE_MINUTES,
                MonitoringGranularity.HOUR,
            })
        elif span <= 7 * 24 * 60 * 60:
            allowed = frozenset({
                MonitoringGranularity.HOUR,
                MonitoringGranularity.DAY,
            })
        else:
            allowed = frozenset({MonitoringGranularity.DAY})
        if normalized not in allowed:
            raise ValueError("granularity is invalid for the monitoring window")
        return normalized

    def bucket_starts(
        self,
        value: MonitoringGranularity | str,
    ) -> tuple[int, ...]:
        """返回窗口内 epoch 对齐桶起点，零跨度窗口没有时间桶。"""
        normalized = self.validate_granularity(value)
        if self.started_at == self.ended_at:
            return ()
        seconds = normalized.seconds
        first = int(math.floor(self.started_at / seconds)) * seconds
        starts: list[int] = []
        current = first
        while current < self.ended_at:
            starts.append(current)
            if len(starts) > MAX_MONITORING_TIME_BUCKETS:
                raise ValueError("time bucket count exceeds the fixed limit")
            current += seconds
        return tuple(starts)


@dataclass(frozen=True, slots=True)
class FinishReasonCount:
    """一个受控模型完成原因的计数。"""

    category: FinishReasonCategory | str
    count: int

    def __post_init__(self) -> None:
        try:
            category = (
                self.category
                if isinstance(self.category, FinishReasonCategory)
                else FinishReasonCategory(self.category)
            )
        except (TypeError, ValueError):
            raise ValueError("finish reason category is invalid") from None
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "count", _count(self.count, "count"))


@dataclass(frozen=True, slots=True)
class ToolErrorCount:
    """一个受控工具错误类别的失败调用计数。"""

    category: ToolErrorCategory | str
    count: int

    def __post_init__(self) -> None:
        try:
            category = (
                self.category
                if isinstance(self.category, ToolErrorCategory)
                else ToolErrorCategory(self.category)
            )
        except (TypeError, ValueError):
            raise ValueError("tool error category is invalid") from None
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "count", _count(self.count, "count"))


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """基于 run_end Observation 的终态运行统计。

    success_rate 的分母始终为 run_count；空窗口返回 None。
    """

    run_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    other_terminal_count: int
    success_rate: float | None
    average_iterations: float | None
    average_tool_call_count: float | None
    runs_with_final_reply: int
    runs_without_final_reply: int

    def __post_init__(self) -> None:
        for field_name in (
            "run_count",
            "completed_count",
            "failed_count",
            "cancelled_count",
            "other_terminal_count",
            "runs_with_final_reply",
            "runs_without_final_reply",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "success_rate",
            _optional_rate(self.success_rate, "success_rate"),
        )
        for field_name in (
            "average_iterations",
            "average_tool_call_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_metric(getattr(self, field_name), field_name),
            )
        if (
            self.completed_count
            + self.failed_count
            + self.cancelled_count
            + self.other_terminal_count
            != self.run_count
        ):
            raise ValueError("run terminal counts are inconsistent")
        if (
            self.runs_with_final_reply
            + self.runs_without_final_reply
            != self.run_count
        ):
            raise ValueError("run reply counts are inconsistent")
        _validate_rate(
            self.success_rate,
            self.completed_count,
            self.run_count,
        )
        _validate_average_presence(
            self.run_count,
            (
                self.average_iterations,
                self.average_tool_call_count,
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelCallMetrics:
    """基于 model_call Observation 的安全模型统计。

    token_coverage_count 仅统计 prompt、completion、total 三项均存在的
    调用；三个 Token 总量也只累加这些完整调用，完全无覆盖时保持 None。
    """

    model_call_count: int
    calls_with_text: int
    calls_without_text: int
    total_tool_call_count: int
    average_tool_call_count: float | None
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_tokens: int | None
    token_coverage_count: int
    average_duration_ms: float | None
    finish_reason_counts: tuple[FinishReasonCount, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "model_call_count",
            "calls_with_text",
            "calls_without_text",
            "total_tool_call_count",
            "token_coverage_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        for field_name in (
            "total_prompt_tokens",
            "total_completion_tokens",
            "total_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_count(getattr(self, field_name), field_name),
            )
        for field_name in (
            "average_tool_call_count",
            "average_duration_ms",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_metric(getattr(self, field_name), field_name),
            )
        if type(self.finish_reason_counts) is not tuple or any(
            not isinstance(item, FinishReasonCount)
            for item in self.finish_reason_counts
        ):
            raise TypeError(
                "finish_reason_counts must be a tuple of FinishReasonCount"
            )
        if len({item.category for item in self.finish_reason_counts}) != len(
            self.finish_reason_counts
        ):
            raise ValueError("finish reason categories must be unique")
        if self.calls_with_text + self.calls_without_text != self.model_call_count:
            raise ValueError("model text counts are inconsistent")
        if self.token_coverage_count > self.model_call_count:
            raise ValueError("token coverage count is inconsistent")
        token_totals = (
            self.total_prompt_tokens,
            self.total_completion_tokens,
            self.total_tokens,
        )
        if self.token_coverage_count == 0 and any(
            value is not None for value in token_totals
        ):
            raise ValueError("uncovered token totals must be null")
        if self.token_coverage_count > 0 and any(
            value is None for value in token_totals
        ):
            raise ValueError("covered token totals must be present")
        if sum(
            item.count for item in self.finish_reason_counts
        ) != self.model_call_count:
            raise ValueError("finish reason counts are inconsistent")
        _validate_average_presence(
            self.model_call_count,
            (
                self.average_tool_call_count,
                self.average_duration_ms,
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolCallMetrics:
    """基于 tool_call Observation 的安全工具统计。

    success_rate 的分母始终为 tool_call_count；空窗口返回 None。
    """

    tool_call_count: int
    successful_tool_call_count: int
    failed_tool_call_count: int
    success_rate: float | None
    average_duration_ms: float | None
    error_type_counts: tuple[ToolErrorCount, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "tool_call_count",
            "successful_tool_call_count",
            "failed_tool_call_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "success_rate",
            _optional_rate(self.success_rate, "success_rate"),
        )
        object.__setattr__(
            self,
            "average_duration_ms",
            _optional_metric(
                self.average_duration_ms,
                "average_duration_ms",
            ),
        )
        if type(self.error_type_counts) is not tuple or any(
            not isinstance(item, ToolErrorCount)
            for item in self.error_type_counts
        ):
            raise TypeError(
                "error_type_counts must be a tuple of ToolErrorCount"
            )
        if len({item.category for item in self.error_type_counts}) != len(
            self.error_type_counts
        ):
            raise ValueError("tool error categories must be unique")
        if (
            self.successful_tool_call_count
            + self.failed_tool_call_count
            != self.tool_call_count
        ):
            raise ValueError("tool call counts are inconsistent")
        if sum(
            item.count for item in self.error_type_counts
        ) != self.failed_tool_call_count:
            raise ValueError("tool error counts are inconsistent")
        _validate_rate(
            self.success_rate,
            self.successful_tool_call_count,
            self.tool_call_count,
        )
        _validate_average_presence(
            self.tool_call_count,
            (self.average_duration_ms,),
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionMetrics:
    """窗口内最后更新的 Tool Execution 当前状态分布。"""

    execution_count: int
    prepared_count: int
    awaiting_approval_count: int
    running_count: int
    succeeded_count: int
    failed_count: int
    unknown_count: int
    with_result_count: int
    with_external_operation_count: int
    average_attempt_count: float | None

    def __post_init__(self) -> None:
        for field_name in (
            "execution_count",
            "prepared_count",
            "awaiting_approval_count",
            "running_count",
            "succeeded_count",
            "failed_count",
            "unknown_count",
            "with_result_count",
            "with_external_operation_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "average_attempt_count",
            _optional_metric(
                self.average_attempt_count,
                "average_attempt_count",
            ),
        )
        if (
            self.prepared_count
            + self.awaiting_approval_count
            + self.running_count
            + self.succeeded_count
            + self.failed_count
            + self.unknown_count
            != self.execution_count
        ):
            raise ValueError("tool execution status counts are inconsistent")
        if (
            self.with_result_count > self.execution_count
            or self.with_external_operation_count > self.execution_count
        ):
            raise ValueError("tool execution presence counts are inconsistent")
        _validate_average_presence(
            self.execution_count,
            (self.average_attempt_count,),
        )


@dataclass(frozen=True, slots=True)
class ToolStatsItem:
    """一个安全工具名称的固定聚合项。"""

    tool_name: str
    call_count: int
    success_count: int
    failure_count: int
    success_rate: float | None
    average_duration_ms: float | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tool_name",
            _safe_label(self.tool_name, "tool_name"),
        )
        for field_name in (
            "call_count",
            "success_count",
            "failure_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        if self.call_count == 0:
            raise ValueError("tool stats item must contain at least one call")
        object.__setattr__(
            self,
            "success_rate",
            _optional_rate(self.success_rate, "success_rate"),
        )
        object.__setattr__(
            self,
            "average_duration_ms",
            _optional_metric(
                self.average_duration_ms,
                "average_duration_ms",
            ),
        )
        if self.success_count + self.failure_count != self.call_count:
            raise ValueError("tool stats counts are inconsistent")
        _validate_rate(
            self.success_rate,
            self.success_count,
            self.call_count,
        )
        _validate_average_presence(
            self.call_count,
            (self.average_duration_ms,),
        )


@dataclass(frozen=True, slots=True)
class ToolStats:
    """固定排序、固定上限的工具名称聚合。"""

    window: MonitoringWindow
    items: tuple[ToolStatsItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.window, MonitoringWindow):
            raise TypeError("window must be a MonitoringWindow")
        if (
            type(self.items) is not tuple
            or len(self.items) > MAX_MONITORING_TOOL_STATS
            or any(not isinstance(item, ToolStatsItem) for item in self.items)
        ):
            raise TypeError("items must be a bounded tuple of ToolStatsItem")
        if len({item.tool_name for item in self.items}) != len(self.items):
            raise ValueError("tool stats names must be unique")
        expected = tuple(
            sorted(
                self.items,
                key=lambda item: (-item.call_count, item.tool_name),
            )
        )
        if self.items != expected:
            raise ValueError("tool stats items are not in the fixed order")


@dataclass(frozen=True, slots=True)
class MonitoringOverview:
    """同一查询窗口内四类统计的不可变总览。"""

    window: MonitoringWindow
    runs: RunMetrics
    model_calls: ModelCallMetrics
    tool_calls: ToolCallMetrics
    tool_executions: ToolExecutionMetrics

    def __post_init__(self) -> None:
        expected_types = (
            (self.window, MonitoringWindow),
            (self.runs, RunMetrics),
            (self.model_calls, ModelCallMetrics),
            (self.tool_calls, ToolCallMetrics),
            (self.tool_executions, ToolExecutionMetrics),
        )
        if any(
            not isinstance(value, expected_type)
            for value, expected_type in expected_types
        ):
            raise TypeError("monitoring overview projection is invalid")


@dataclass(frozen=True, slots=True)
class MonitoringTimeBucket:
    """一个 epoch 对齐的 UTC 聚合时间桶。"""

    bucket_started_at: int
    run_count: int
    model_call_count: int
    tool_call_count: int
    failed_tool_call_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "bucket_started_at",
            "run_count",
            "model_call_count",
            "tool_call_count",
            "failed_tool_call_count",
        ):
            object.__setattr__(
                self,
                field_name,
                _count(getattr(self, field_name), field_name),
            )
        if self.failed_tool_call_count > self.tool_call_count:
            raise ValueError("failed tool call count is inconsistent")


@dataclass(frozen=True, slots=True)
class MonitoringTimeSeries:
    """按固定粒度补齐空桶后的不可变趋势。"""

    window: MonitoringWindow
    granularity: MonitoringGranularity | str
    buckets: tuple[MonitoringTimeBucket, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.window, MonitoringWindow):
            raise TypeError("window must be a MonitoringWindow")
        normalized = _granularity(self.granularity)
        query = MonitoringAggregationQuery(
            started_at=self.window.started_at,
            ended_at=self.window.ended_at,
            environment=self.window.environment,
            tool_name=self.window.tool_name,
        )
        query.validate_granularity(normalized)
        object.__setattr__(self, "granularity", normalized)
        if (
            type(self.buckets) is not tuple
            or len(self.buckets) > MAX_MONITORING_TIME_BUCKETS
            or any(
                not isinstance(bucket, MonitoringTimeBucket)
                for bucket in self.buckets
            )
        ):
            raise TypeError(
                "buckets must be a bounded tuple of MonitoringTimeBucket"
            )
        expected = query.bucket_starts(normalized)
        if tuple(
            bucket.bucket_started_at for bucket in self.buckets
        ) != expected:
            raise ValueError("time series buckets are not complete and ordered")


class MonitoringAggregationRepository(Protocol):
    """聚合统计的中立只读 Repository 边界。"""

    def overview(
        self,
        query: MonitoringAggregationQuery,
    ) -> MonitoringOverview:
        """返回同一窗口内的四类总览指标。"""

    def tool_stats(
        self,
        query: MonitoringAggregationQuery,
    ) -> ToolStats:
        """返回固定排序且不超过一百项的工具聚合。"""

    def time_buckets(
        self,
        query: MonitoringAggregationQuery,
        granularity: MonitoringGranularity,
    ) -> tuple[MonitoringTimeBucket, ...]:
        """返回不含空桶的有限 SQL 聚合结果。"""


__all__ = [
    "DEFAULT_MONITORING_WINDOW_SECONDS",
    "MAX_MONITORING_TIME_BUCKETS",
    "MAX_MONITORING_TOOL_STATS",
    "MAX_MONITORING_WINDOW_SECONDS",
    "FinishReasonCategory",
    "FinishReasonCount",
    "ModelCallMetrics",
    "MonitoringAggregationQuery",
    "MonitoringAggregationRepository",
    "MonitoringGranularity",
    "MonitoringOverview",
    "MonitoringTimeBucket",
    "MonitoringTimeSeries",
    "MonitoringWindow",
    "RunMetrics",
    "ToolCallMetrics",
    "ToolErrorCategory",
    "ToolErrorCount",
    "ToolExecutionMetrics",
    "ToolStats",
    "ToolStatsItem",
]
