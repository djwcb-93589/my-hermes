"""Dashboard 监控读取的应用层编排。"""

from __future__ import annotations

import math
import re

from hermes.observability.monitoring import (
    DEFAULT_MONITORING_PAGE_LIMIT,
    MAX_MONITORING_PAGE_LIMIT,
    MonitoringRecordInvalid,
    MonitoringRepositoryUnavailable,
    ObservationEventType,
    ObservationPage,
    ObservationQuery,
    ObservationReadRepository,
    ToolExecutionPage,
    ToolExecutionQuery,
    ToolExecutionReadRepository,
)
from hermes.observability.tool_execution import ToolExecutionDetail
from hermes.web.read_context import (
    ReadDataUnavailable,
    ReadInvalidRequest,
    ResourceNotFound,
)


_MAX_FILTER_TEXT_LENGTH = 256
_SAFE_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$"
)
_UNAVAILABLE_REASON_CODES = frozenset({
    "database_busy",
    "database_unavailable",
})


class MonitoringReadService:
    """仅通过中立 Repository Protocol 编排监控读取。"""

    def __init__(
        self,
        observation_repository: ObservationReadRepository,
        tool_execution_repository: ToolExecutionReadRepository,
    ) -> None:
        self._observations = observation_repository
        self._tool_executions = tool_execution_repository

    def list_tool_executions(
        self,
        *,
        environment: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        session_id: str | None = None,
        cron_run_id: str | None = None,
        limit: int = DEFAULT_MONITORING_PAGE_LIMIT,
        offset: int = 0,
    ) -> ToolExecutionPage:
        """校验过滤和分页后读取安全的 Tool Execution 摘要。"""
        normalized_limit, normalized_offset = _page(limit, offset)
        try:
            query = ToolExecutionQuery(
                environment=_optional_filter(environment, "environment"),
                status=_optional_filter(status, "status"),
                tool_name=_optional_filter(tool_name, "tool_name"),
                session_id=_optional_filter(session_id, "session_id"),
                cron_run_id=_optional_filter(cron_run_id, "cron_run_id"),
                limit=normalized_limit + 1,
                offset=normalized_offset,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadInvalidRequest() from exc
        try:
            records = self._tool_executions.list_tool_executions(query)
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        try:
            return ToolExecutionPage(
                items=tuple(records[:normalized_limit]),
                limit=normalized_limit,
                offset=normalized_offset,
                has_more=len(records) > normalized_limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def get_tool_execution(self, execution_id: str) -> ToolExecutionDetail:
        """按稳定 ID 读取一条不含参数和结果正文的执行详情。"""
        normalized_id = _required_identifier(execution_id, "execution_id")
        try:
            detail = self._tool_executions.get_tool_execution(normalized_id)
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        if detail is None:
            raise ResourceNotFound()
        return detail

    def list_observations(
        self,
        *,
        event_type: str | ObservationEventType | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        limit: int = DEFAULT_MONITORING_PAGE_LIMIT,
        offset: int = 0,
    ) -> ObservationPage:
        """校验时间范围和过滤条件后读取安全 Observation。"""
        normalized_limit, normalized_offset = _page(limit, offset)
        normalized_started_at = _optional_timestamp(started_at, "started_at")
        normalized_ended_at = _optional_timestamp(ended_at, "ended_at")
        if (
            normalized_started_at is not None
            and normalized_ended_at is not None
            and normalized_started_at > normalized_ended_at
        ):
            raise ReadInvalidRequest()
        try:
            query = ObservationQuery(
                event_type=_optional_event_type(event_type),
                run_id=_optional_filter(run_id, "run_id"),
                parent_run_id=_optional_filter(parent_run_id, "parent_run_id"),
                tool_name=_optional_filter(tool_name, "tool_name"),
                status=_optional_filter(status, "status"),
                started_at=normalized_started_at,
                ended_at=normalized_ended_at,
                limit=normalized_limit + 1,
                offset=normalized_offset,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadInvalidRequest() from exc
        try:
            records = self._observations.list_observations(query)
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        try:
            return ObservationPage(
                items=tuple(records[:normalized_limit]),
                limit=normalized_limit,
                offset=normalized_offset,
                has_more=len(records) > normalized_limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def get_run_timeline(
        self,
        run_id: str,
        *,
        limit: int = DEFAULT_MONITORING_PAGE_LIMIT,
        offset: int = 0,
    ) -> ObservationPage:
        """读取单次运行的有界时间线，并区分不存在与空页。"""
        normalized_run_id = _required_identifier(run_id, "run_id")
        normalized_limit, normalized_offset = _page(limit, offset)
        try:
            timeline = self._observations.list_run_timeline(
                normalized_run_id,
                limit=normalized_limit + 1,
                offset=normalized_offset,
            )
        except MonitoringRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except MonitoringRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        if timeline is None:
            raise ResourceNotFound()
        try:
            return ObservationPage(
                items=tuple(timeline[:normalized_limit]),
                limit=normalized_limit,
                offset=normalized_offset,
                has_more=len(timeline) > normalized_limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc


def _page(limit: object, offset: object) -> tuple[int, int]:
    """验证所有监控查询共享的固定分页边界。"""
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_MONITORING_PAGE_LIMIT
    ):
        raise ReadInvalidRequest()
    if type(offset) is not int or offset < 0:
        raise ReadInvalidRequest()
    return limit, offset


def _required_identifier(value: object, field_name: str) -> str:
    """验证路径身份字段，不把原值加入异常。"""
    normalized = _optional_filter(value, field_name)
    if normalized is None or not _SAFE_IDENTIFIER_RE.fullmatch(normalized):
        raise ReadInvalidRequest()
    return normalized


def _optional_filter(value: object, field_name: str) -> str | None:
    """验证短的单行 SQL 绑定值，不执行隐式字符串转换。"""
    del field_name
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_FILTER_TEXT_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReadInvalidRequest()
    return value


def _optional_event_type(
    value: object,
) -> ObservationEventType | None:
    """使用中立事件枚举规范化 Observation 类型。"""
    if value is None:
        return None
    if isinstance(value, ObservationEventType):
        return value
    if type(value) is not str:
        raise ReadInvalidRequest()
    try:
        return ObservationEventType(value)
    except ValueError as exc:
        raise ReadInvalidRequest() from exc


def _optional_timestamp(value: object, field_name: str) -> float | None:
    """验证查询时间边界，拒绝布尔值和非有限数。"""
    del field_name
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReadInvalidRequest()
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ReadInvalidRequest()
    return normalized


def _unavailable_error(
    exc: MonitoringRepositoryUnavailable,
) -> ReadDataUnavailable:
    """仅映射中立稳定原因码，不传播持久化异常文本。"""
    reason_code = getattr(exc, "reason_code", "database_unavailable")
    if reason_code not in _UNAVAILABLE_REASON_CODES:
        reason_code = "database_unavailable"
    return ReadDataUnavailable(reason_code)


__all__ = ["MonitoringReadService"]
