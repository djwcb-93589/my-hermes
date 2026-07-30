"""数据库健康诊断与固定查询探针的只读 Dashboard 路由。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from hermes.observability.database_diagnostics import (
    DatabaseHealthSnapshot,
    DatabaseJournalMetrics,
    DatabaseQueryProbe,
    DatabaseQueryProbeSet,
    DatabaseSchemaMetrics,
    DatabaseStorageMetrics,
)
from hermes.web.database_diagnostics_service import (
    DatabaseDiagnosticsService,
)
from hermes.web.read_context import ReadDataUnavailable
from hermes.web.schemas import (
    DatabaseHealthResponse,
    DatabaseJournalMetricsResponse,
    DatabaseProbeResponse,
    DatabaseProbeSetResponse,
    DatabaseSchemaMetricsResponse,
    DatabaseStorageMetricsResponse,
)


router = APIRouter(
    prefix="/api/monitoring/database",
    tags=["monitoring"],
)


def _service(request: Request) -> DatabaseDiagnosticsService:
    """取得装配期注入的独立诊断服务，不在路由中访问数据库。"""
    service = getattr(
        request.app.state,
        "database_diagnostics_service",
        None,
    )
    if service is None:
        raise ReadDataUnavailable("data_unavailable")
    return service


@router.get(
    "/health",
    response_model=DatabaseHealthResponse,
)
def get_database_health(request: Request) -> DatabaseHealthResponse:
    """执行一次固定只读诊断并返回完整健康快照。"""
    return _health_response(_service(request).inspect())


@router.get(
    "/probes",
    response_model=DatabaseProbeSetResponse,
)
def get_database_probes(request: Request) -> DatabaseProbeSetResponse:
    """执行一次固定只读诊断并返回本次探针结果。"""
    snapshot = _service(request).inspect()
    return _probe_set_response(snapshot.probe_set)


def _health_response(
    snapshot: DatabaseHealthSnapshot,
) -> DatabaseHealthResponse:
    """逐字段转换中立快照，不透传 Repository 或内部属性。"""
    return DatabaseHealthResponse(
        checked_at=_utc_datetime(snapshot.checked_at),
        status=snapshot.status,
        schema=_schema_response(snapshot.schema),
        storage=(
            None
            if snapshot.storage is None
            else _storage_response(snapshot.storage)
        ),
        journal=(
            None
            if snapshot.journal is None
            else _journal_response(snapshot.journal)
        ),
        probes=_probe_set_response(snapshot.probe_set),
    )


def _schema_response(
    metrics: DatabaseSchemaMetrics,
) -> DatabaseSchemaMetricsResponse:
    return DatabaseSchemaMetricsResponse(
        current_version=metrics.current_version,
        expected_version=metrics.expected_version,
        user_version=metrics.user_version,
        compatible=metrics.compatible,
        required_structures_available=(
            metrics.required_structures_available
        ),
    )


def _storage_response(
    metrics: DatabaseStorageMetrics,
) -> DatabaseStorageMetricsResponse:
    return DatabaseStorageMetricsResponse(
        page_size_bytes=metrics.page_size_bytes,
        page_count=metrics.page_count,
        freelist_page_count=metrics.freelist_page_count,
        database_size_bytes=metrics.database_size_bytes,
        free_space_bytes=metrics.free_space_bytes,
        used_space_bytes=metrics.used_space_bytes,
        database_file_size_bytes=metrics.database_file_size_bytes,
        wal_present=metrics.wal_present,
        wal_size_bytes=metrics.wal_size_bytes,
    )


def _journal_response(
    metrics: DatabaseJournalMetrics,
) -> DatabaseJournalMetricsResponse:
    return DatabaseJournalMetricsResponse(
        journal_mode=metrics.journal_mode,
        query_only=metrics.query_only,
        foreign_keys=metrics.foreign_keys,
        busy_timeout_ms=metrics.busy_timeout_ms,
    )


def _probe_set_response(
    probe_set: DatabaseQueryProbeSet,
) -> DatabaseProbeSetResponse:
    return DatabaseProbeSetResponse(
        checked_at=_utc_datetime(probe_set.checked_at),
        total_duration_ms=probe_set.total_duration_ms,
        budget_ms=probe_set.budget_ms,
        budget_exhausted=probe_set.budget_exhausted,
        probes=[_probe_response(probe) for probe in probe_set.probes],
    )


def _probe_response(
    probe: DatabaseQueryProbe,
) -> DatabaseProbeResponse:
    return DatabaseProbeResponse(
        probe_name=probe.probe_name,
        status=probe.status,
        duration_ms=probe.duration_ms,
        returned_row_count=probe.returned_row_count,
        reason=probe.reason,
    )


def _utc_datetime(value: float) -> datetime:
    """把已校验 Unix 时间转换为带 UTC 时区的公开时间。"""
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ReadDataUnavailable("data_invalid") from exc


__all__ = ["router"]
