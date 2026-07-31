"""Dashboard Runtime Component 当前状态的独立只读应用服务。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from hermes.observability.runtime import (
    MAX_RUNTIME_OFFSET,
    MAX_RUNTIME_PAGE_LIMIT,
    RuntimeComponentPage,
    RuntimeComponentRecord,
    RuntimeComponentState,
    RuntimeComponentStatusView,
    RuntimeStatusReadRepository,
    RuntimeStatusRecordInvalid,
    RuntimeStatusRepositoryUnavailable,
    derive_runtime_component_status,
    validate_runtime_identity,
)
from hermes.web.read_context import (
    ReadDataUnavailable,
    ReadInvalidRequest,
    ResourceNotFound,
)


DEFAULT_RUNTIME_PAGE_LIMIT = 50

_UNAVAILABLE_REASON_CODES = frozenset({
    "database_busy",
    "database_unavailable",
})


class RuntimeStatusReadService:
    """只依赖中立 Repository，通过单一 Clock 推导当前有效状态。"""

    __slots__ = ("_clock", "_repository")

    def __init__(
        self,
        repository: RuntimeStatusReadRepository,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._repository = repository
        self._clock = clock

    def list_components(
        self,
        *,
        component_type: str | None = None,
        reported_state: RuntimeComponentState | str | None = None,
        limit: int = DEFAULT_RUNTIME_PAGE_LIMIT,
        offset: int = 0,
    ) -> RuntimeComponentPage:
        """校验有限过滤和分页，并返回同一观察时刻推导的状态页。"""
        normalized_limit, normalized_offset = _page(limit, offset)
        normalized_type = _optional_component_type(component_type)
        normalized_state = _optional_state(reported_state)
        observed_at = _clock_value(self._clock)
        try:
            records = self._repository.list_components(
                component_type=normalized_type,
                reported_state=normalized_state,
                limit=normalized_limit + 1,
                offset=normalized_offset,
            )
        except RuntimeStatusRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except RuntimeStatusRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        if type(records) is not tuple or len(records) > normalized_limit + 1:
            raise ReadDataUnavailable("data_invalid")
        try:
            items = tuple(
                _status_view(record, observed_at)
                for record in records[:normalized_limit]
            )
            return RuntimeComponentPage(
                observed_at=observed_at,
                items=items,
                limit=normalized_limit,
                offset=normalized_offset,
                has_more=len(records) > normalized_limit,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc

    def get_component(
        self,
        component_type: str,
        component_id: str,
    ) -> RuntimeComponentStatusView:
        """按逻辑组件身份读取当前实例，并在不存在时返回标准 404。"""
        normalized_type = _required_component_type(component_type)
        normalized_id = _required_component_id(component_id)
        observed_at = _clock_value(self._clock)
        try:
            record = self._repository.get_component(
                normalized_type,
                normalized_id,
            )
        except RuntimeStatusRepositoryUnavailable as exc:
            raise _unavailable_error(exc) from exc
        except RuntimeStatusRecordInvalid as exc:
            raise ReadDataUnavailable("data_invalid") from exc
        if record is None:
            raise ResourceNotFound()
        try:
            return _status_view(record, observed_at)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReadDataUnavailable("data_invalid") from exc


def _status_view(
    record: RuntimeComponentRecord,
    observed_at: float,
) -> RuntimeComponentStatusView:
    """通过中立纯函数推导状态，避免 Service 复制生命周期规则。"""
    if not isinstance(record, RuntimeComponentRecord):
        raise TypeError("record must be a RuntimeComponentRecord")
    return derive_runtime_component_status(
        record,
        observed_at=observed_at,
    )


def _page(limit: object, offset: object) -> tuple[int, int]:
    """约束 Runtime 当前状态列表的固定分页上限。"""
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_RUNTIME_PAGE_LIMIT
    ):
        raise ReadInvalidRequest()
    if (
        type(offset) is not int
        or offset < 0
        or offset > MAX_RUNTIME_OFFSET
    ):
        raise ReadInvalidRequest()
    return limit, offset


def _optional_component_type(value: object) -> str | None:
    """校验可选的低基数组件类型过滤。"""
    if value is None:
        return None
    return _required_component_type(value)


def _required_component_type(value: object) -> str:
    """校验组件类型，不在异常中回显原始输入。"""
    try:
        return validate_runtime_identity(value, "component_type")
    except (TypeError, ValueError) as exc:
        raise ReadInvalidRequest() from exc


def _required_component_id(value: object) -> str:
    """校验组件逻辑身份，不在异常中回显原始输入。"""
    try:
        return validate_runtime_identity(value, "component_id")
    except (TypeError, ValueError) as exc:
        raise ReadInvalidRequest() from exc


def _optional_state(
    value: RuntimeComponentState | str | None,
) -> RuntimeComponentState | None:
    """使用中立生命周期枚举规范化可选状态过滤。"""
    if value is None:
        return None
    if isinstance(value, RuntimeComponentState):
        return value
    if type(value) is not str:
        raise ReadInvalidRequest()
    try:
        return RuntimeComponentState(value)
    except ValueError as exc:
        raise ReadInvalidRequest() from exc


def _clock_value(clock: Callable[[], float]) -> float:
    """每次请求只读取一次可注入 Clock，并拒绝异常时间。"""
    try:
        value = clock()
    except Exception as exc:
        raise ReadDataUnavailable("data_unavailable") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReadDataUnavailable("data_unavailable")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ReadDataUnavailable("data_unavailable")
    return normalized


def _unavailable_error(
    exc: RuntimeStatusRepositoryUnavailable,
) -> ReadDataUnavailable:
    """仅映射稳定原因码，不传播数据库或底层异常文本。"""
    reason_code = getattr(exc, "reason_code", "database_unavailable")
    if reason_code == "schema_incompatible":
        return ReadDataUnavailable("data_invalid")
    if reason_code not in _UNAVAILABLE_REASON_CODES:
        reason_code = "database_unavailable"
    return ReadDataUnavailable(reason_code)


__all__ = [
    "DEFAULT_RUNTIME_PAGE_LIMIT",
    "MAX_RUNTIME_OFFSET",
    "MAX_RUNTIME_PAGE_LIMIT",
    "RuntimeStatusReadService",
]
