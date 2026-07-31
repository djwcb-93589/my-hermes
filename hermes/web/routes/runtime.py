"""长期运行组件当前状态的只读 Dashboard 路由。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request

from hermes.observability.runtime import (
    RuntimeComponentPage,
    RuntimeComponentStatusView,
)
from hermes.web.read_context import ReadDataUnavailable
from hermes.web.runtime_status_service import (
    DEFAULT_RUNTIME_PAGE_LIMIT,
    MAX_RUNTIME_OFFSET,
    MAX_RUNTIME_PAGE_LIMIT,
    RuntimeStatusReadService,
)
from hermes.web.schemas import (
    RuntimeComponentListResponse,
    RuntimeComponentResponse,
)


router = APIRouter(
    prefix="/api/monitoring/runtime",
    tags=["monitoring"],
)


def _service(request: Request) -> RuntimeStatusReadService:
    """取得装配期注入的 Runtime 读取服务，不持有运行组件或 Publisher。"""
    service = getattr(request.app.state, "runtime_status_read_service", None)
    if service is None:
        raise ReadDataUnavailable("data_unavailable")
    return service


@router.get(
    "/components",
    response_model=RuntimeComponentListResponse,
)
def list_runtime_components(
    request: Request,
    component_type: str | None = Query(
        default=None,
        min_length=1,
        max_length=128,
    ),
    reported_state: str | None = Query(
        default=None,
        min_length=1,
        max_length=32,
    ),
    limit: int = Query(
        default=DEFAULT_RUNTIME_PAGE_LIMIT,
        ge=1,
        le=MAX_RUNTIME_PAGE_LIMIT,
    ),
    offset: int = Query(default=0, ge=0, le=MAX_RUNTIME_OFFSET),
) -> RuntimeComponentListResponse:
    """按稳定身份顺序读取当前逻辑组件，不读取状态历史。"""
    page = _service(request).list_components(
        component_type=component_type,
        reported_state=reported_state,
        limit=limit,
        offset=offset,
    )
    return _page_response(page)


@router.get(
    "/components/{component_type}/{component_id}",
    response_model=RuntimeComponentResponse,
)
def get_runtime_component(
    request: Request,
    component_type: str,
    component_id: str,
) -> RuntimeComponentResponse:
    """读取一个逻辑组件当前有效实例的安全投影。"""
    item = _service(request).get_component(component_type, component_id)
    return _component_response(item)


def _page_response(
    page: RuntimeComponentPage,
) -> RuntimeComponentListResponse:
    """逐字段转换中立分页投影，不透传 Repository 内部状态。"""
    if not isinstance(page, RuntimeComponentPage):
        raise ReadDataUnavailable("data_invalid")
    try:
        return RuntimeComponentListResponse(
            observed_at=_utc_datetime(page.observed_at),
            items=[_component_response(item) for item in page.items],
            limit=page.limit,
            offset=page.offset,
            has_more=page.has_more,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReadDataUnavailable("data_invalid") from exc


def _component_response(
    item: RuntimeComponentStatusView,
) -> RuntimeComponentResponse:
    """转换 Runtime 状态并仅物化已冻结的安全 Metadata。"""
    if not isinstance(item, RuntimeComponentStatusView):
        raise ReadDataUnavailable("data_invalid")
    try:
        return RuntimeComponentResponse(
            component_type=item.component_type,
            component_id=item.component_id,
            instance_id=item.instance_id,
            reported_state=item.reported_state,
            freshness=item.freshness,
            effective_status=item.effective_status,
            started_at=_optional_utc_datetime(item.started_at),
            heartbeat_at=_utc_datetime(item.heartbeat_at),
            heartbeat_age_seconds=item.heartbeat_age_seconds,
            heartbeat_interval_seconds=item.heartbeat_interval_seconds,
            stale_after_seconds=item.stale_after_seconds,
            is_stale=item.is_stale,
            stopped_at=_optional_utc_datetime(item.stopped_at),
            error_type=item.error_type,
            metadata=_metadata_mapping(item.metadata),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReadDataUnavailable("data_invalid") from exc


def _optional_utc_datetime(value: float | None) -> datetime | None:
    """将可选 Unix 时间转换为 UTC 时间。"""
    return None if value is None else _utc_datetime(value)


def _utc_datetime(value: float) -> datetime:
    """将中立投影中的有限 Unix 时间转换为明确 UTC 时间。"""
    try:
        return datetime.fromtimestamp(value, UTC)
    except (TypeError, OverflowError, OSError, ValueError) as exc:
        raise ReadDataUnavailable("data_invalid") from exc


def _metadata_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """复制冻结 Metadata，避免响应序列化保留 MappingProxy 引用。"""
    if not isinstance(value, Mapping) or any(
        type(key) is not str for key in value
    ):
        raise ReadDataUnavailable("data_invalid")
    return {
        key: _metadata_value(item)
        for key, item in value.items()
    }


def _metadata_value(value: object) -> object:
    """仅物化契约允许的 JSON 标量、元组和只读映射。"""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ReadDataUnavailable("data_invalid")
        return value
    if isinstance(value, Mapping):
        return _metadata_mapping(value)
    if type(value) is tuple:
        return [_metadata_value(item) for item in value]
    raise ReadDataUnavailable("data_invalid")


__all__ = ["router"]
