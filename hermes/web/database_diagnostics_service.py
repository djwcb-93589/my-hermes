"""Dashboard 数据库诊断的独立应用层编排。"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import TypeVar

from hermes.observability.database_diagnostics import (
    DATABASE_DIAGNOSTICS_BUDGET_MS,
    MAX_DATABASE_NUMERIC_VALUE,
    DatabaseDiagnosticReason,
    DatabaseDiagnosticsRepository,
    DatabaseDiagnosticsRepositoryError,
    DatabaseHealthSnapshot,
    DatabaseHealthStatus,
    DatabaseJournalMetrics,
    DatabaseJournalMode,
    DatabaseProbeName,
    DatabaseProbeStatus,
    DatabaseQueryProbe,
    DatabaseQueryProbeSet,
    DatabaseSchemaFacts,
    DatabaseSchemaMetrics,
    DatabaseStorageMetrics,
)
from hermes.web.read_context import ReadDataUnavailable


logger = logging.getLogger(__name__)

_ResultT = TypeVar("_ResultT")
_NANOSECONDS_PER_MILLISECOND = 1_000_000


class DatabaseDiagnosticsService:
    """通过中立 Repository 执行一次固定且不持久化的数据库诊断。"""

    __slots__ = ("_clock_ns", "_repository", "_wall_clock")

    def __init__(
        self,
        repository: DatabaseDiagnosticsRepository,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if not callable(wall_clock):
            raise TypeError("wall_clock must be callable")
        self._repository = repository
        self._clock_ns = clock_ns
        self._wall_clock = wall_clock

    def inspect(self) -> DatabaseHealthSnapshot:
        """按固定顺序执行核心和可选探针，并集中推导健康状态。"""
        started_ns = self._read_monotonic_ns()
        checked_at = self._read_checked_at()
        probes: list[DatabaseQueryProbe] = []

        _, open_probe = self._run_core_probe(
            DatabaseProbeName.OPEN_CONNECTION,
            self._repository.check_connection,
            started_ns=started_ns,
            expected_type=type(None),
            returned_row_count=1,
        )
        probes.append(open_probe)

        schema_facts, schema_probe = self._run_core_probe(
            DatabaseProbeName.READ_SCHEMA_VERSION,
            self._repository.read_schema_facts,
            started_ns=started_ns,
            expected_type=DatabaseSchemaFacts,
        )
        probes.append(schema_probe)
        schema = _schema_metrics(schema_facts)

        journal, journal_probe = self._run_optional_probe(
            DatabaseProbeName.READ_JOURNAL_METRICS,
            self._repository.read_journal_metrics,
            started_ns=started_ns,
            expected_type=DatabaseJournalMetrics,
        )
        probes.append(journal_probe)

        storage, storage_probe = self._run_optional_probe(
            DatabaseProbeName.READ_STORAGE_METRICS,
            self._repository.read_storage_metrics,
            started_ns=started_ns,
            expected_type=DatabaseStorageMetrics,
        )
        probes.append(storage_probe)

        observation_count, observation_probe = (
            self._run_schema_dependent_probe(
                DatabaseProbeName.RECENT_OBSERVATION_LOOKUP,
                self._repository.recent_observation_lookup,
                started_ns=started_ns,
                schema_compatible=schema.compatible,
            )
        )
        del observation_count
        probes.append(observation_probe)

        execution_count, execution_probe = (
            self._run_schema_dependent_probe(
                DatabaseProbeName.RECENT_TOOL_EXECUTION_LOOKUP,
                self._repository.recent_tool_execution_lookup,
                started_ns=started_ns,
                schema_compatible=schema.compatible,
            )
        )
        del execution_count
        probes.append(execution_probe)

        total_duration_ms = self._elapsed_ms(started_ns)
        budget_exhausted = (
            total_duration_ms >= DATABASE_DIAGNOSTICS_BUDGET_MS
            or any(
                probe.reason
                is DatabaseDiagnosticReason.BUDGET_EXHAUSTED
                for probe in probes
            )
        )
        probe_set = DatabaseQueryProbeSet(
            checked_at=checked_at,
            total_duration_ms=total_duration_ms,
            budget_ms=DATABASE_DIAGNOSTICS_BUDGET_MS,
            budget_exhausted=budget_exhausted,
            probes=tuple(probes),
        )
        status = _health_status(
            schema=schema,
            storage=storage,
            journal=journal,
            probe_set=probe_set,
        )
        return DatabaseHealthSnapshot(
            checked_at=checked_at,
            status=status,
            schema=schema,
            storage=storage,
            journal=journal,
            probe_set=probe_set,
        )

    def _run_core_probe(
        self,
        probe_name: DatabaseProbeName,
        operation: Callable[[int], _ResultT],
        *,
        started_ns: int,
        expected_type: type[_ResultT] | None = None,
        returned_row_count: int | None = None,
    ) -> tuple[_ResultT, DatabaseQueryProbe]:
        """核心探针失败时转换成现有稳定 503 读取错误。"""
        timeout_ms = self._remaining_budget_ms(started_ns)
        if timeout_ms == 0:
            raise ReadDataUnavailable("database_unavailable")
        probe_started_ns = self._read_monotonic_ns()
        try:
            result = operation(timeout_ms)
            _validate_probe_result(result, expected_type)
        except DatabaseDiagnosticsRepositoryError as exc:
            self._log_probe_failure(probe_name, exc, "core")
            raise _core_read_error(exc.reason) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            self._log_probe_failure(probe_name, exc, "core")
            raise ReadDataUnavailable("data_invalid") from exc
        except Exception as exc:
            self._log_probe_failure(probe_name, exc, "core")
            raise ReadDataUnavailable("database_unavailable") from exc
        duration_ms = self._elapsed_ms(probe_started_ns)
        return result, DatabaseQueryProbe(
            probe_name=probe_name,
            status=DatabaseProbeStatus.SUCCEEDED,
            duration_ms=duration_ms,
            returned_row_count=returned_row_count,
        )

    def _run_optional_probe(
        self,
        probe_name: DatabaseProbeName,
        operation: Callable[[int], _ResultT],
        *,
        started_ns: int,
        expected_type: type[_ResultT] | None = None,
    ) -> tuple[_ResultT | None, DatabaseQueryProbe]:
        """隔离非核心探针失败，并在预算耗尽后停止新的读取。"""
        timeout_ms = self._remaining_budget_ms(started_ns)
        if timeout_ms == 0:
            return None, _skipped_probe(
                probe_name,
                DatabaseDiagnosticReason.BUDGET_EXHAUSTED,
            )

        probe_started_ns = self._read_monotonic_ns()
        try:
            result = operation(timeout_ms)
            _validate_probe_result(result, expected_type)
        except DatabaseDiagnosticsRepositoryError as exc:
            self._log_probe_failure(probe_name, exc, "optional")
            status, reason = _probe_failure(exc.reason)
        except (TypeError, ValueError, OverflowError) as exc:
            self._log_probe_failure(probe_name, exc, "optional")
            status = DatabaseProbeStatus.FAILED
            reason = DatabaseDiagnosticReason.DATA_INVALID
        except Exception as exc:
            self._log_probe_failure(probe_name, exc, "optional")
            status = DatabaseProbeStatus.FAILED
            reason = DatabaseDiagnosticReason.QUERY_FAILED
        else:
            return result, DatabaseQueryProbe(
                probe_name=probe_name,
                status=DatabaseProbeStatus.SUCCEEDED,
                duration_ms=self._elapsed_ms(probe_started_ns),
            )
        return None, DatabaseQueryProbe(
            probe_name=probe_name,
            status=status,
            duration_ms=self._elapsed_ms(probe_started_ns),
            reason=reason,
        )

    def _run_schema_dependent_probe(
        self,
        probe_name: DatabaseProbeName,
        operation: Callable[[int], int],
        *,
        started_ns: int,
        schema_compatible: bool,
    ) -> tuple[int | None, DatabaseQueryProbe]:
        """只在兼容 Schema 和剩余预算内执行固定索引查询。"""
        if not schema_compatible:
            return None, _skipped_probe(
                probe_name,
                DatabaseDiagnosticReason.SCHEMA_INCOMPATIBLE,
            )
        timeout_ms = self._remaining_budget_ms(started_ns)
        if timeout_ms == 0:
            return None, _skipped_probe(
                probe_name,
                DatabaseDiagnosticReason.BUDGET_EXHAUSTED,
            )

        probe_started_ns = self._read_monotonic_ns()
        try:
            returned_row_count = operation(timeout_ms)
            if (
                type(returned_row_count) is not int
                or returned_row_count not in (0, 1)
            ):
                raise ValueError("probe row count is invalid")
        except DatabaseDiagnosticsRepositoryError as exc:
            self._log_probe_failure(probe_name, exc, "optional")
            status, reason = _probe_failure(exc.reason)
        except (TypeError, ValueError, OverflowError) as exc:
            self._log_probe_failure(probe_name, exc, "optional")
            status = DatabaseProbeStatus.FAILED
            reason = DatabaseDiagnosticReason.DATA_INVALID
        except Exception as exc:
            self._log_probe_failure(probe_name, exc, "optional")
            status = DatabaseProbeStatus.FAILED
            reason = DatabaseDiagnosticReason.QUERY_FAILED
        else:
            return returned_row_count, DatabaseQueryProbe(
                probe_name=probe_name,
                status=DatabaseProbeStatus.SUCCEEDED,
                duration_ms=self._elapsed_ms(probe_started_ns),
                returned_row_count=returned_row_count,
            )
        return None, DatabaseQueryProbe(
            probe_name=probe_name,
            status=status,
            duration_ms=self._elapsed_ms(probe_started_ns),
            reason=reason,
        )

    def _remaining_budget_ms(self, started_ns: int) -> int:
        """计算下一探针可使用的剩余整毫秒，亚毫秒尾段最多保留一毫秒。"""
        remaining_ms = (
            DATABASE_DIAGNOSTICS_BUDGET_MS
            - self._elapsed_ms(started_ns)
        )
        if remaining_ms <= 0:
            return 0
        return min(
            DATABASE_DIAGNOSTICS_BUDGET_MS,
            max(1, int(remaining_ms)),
        )

    def _read_monotonic_ns(self) -> int:
        value = self._clock_ns()
        if type(value) is not int or value < 0:
            raise ReadDataUnavailable("data_invalid")
        return value

    def _elapsed_ms(self, started_ns: int) -> float:
        ended_ns = self._read_monotonic_ns()
        if ended_ns < started_ns:
            raise ReadDataUnavailable("data_invalid")
        duration_ms = (
            ended_ns - started_ns
        ) / _NANOSECONDS_PER_MILLISECOND
        if not math.isfinite(duration_ms):
            raise ReadDataUnavailable("data_invalid")
        if duration_ms > MAX_DATABASE_NUMERIC_VALUE:
            raise ReadDataUnavailable("data_invalid")
        return duration_ms

    def _read_checked_at(self) -> float:
        value = self._wall_clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReadDataUnavailable("data_invalid")
        checked_at = float(value)
        if (
            not math.isfinite(checked_at)
            or checked_at < 0
            or checked_at > MAX_DATABASE_NUMERIC_VALUE
        ):
            raise ReadDataUnavailable("data_invalid")
        return checked_at

    @staticmethod
    def _log_probe_failure(
        probe_name: DatabaseProbeName,
        exc: Exception,
        stage: str,
    ) -> None:
        """只记录稳定阶段、探针、状态和异常类型。"""
        logger.warning(
            "Database diagnostic probe failed: "
            "diagnostic_stage=%s probe_name=%s status=failed "
            "exception_type=%s",
            stage,
            probe_name.value,
            type(exc).__name__,
        )


def _validate_probe_result(
    result: object,
    expected_type: type[object] | None,
) -> None:
    if expected_type is not None and not isinstance(result, expected_type):
        raise TypeError("repository returned an invalid diagnostic result")


def _schema_metrics(facts: DatabaseSchemaFacts) -> DatabaseSchemaMetrics:
    """由应用层根据版本与固定结构事实推导兼容性。"""
    compatible = (
        facts.current_version == facts.expected_version
        and facts.required_structures_available
    )
    return DatabaseSchemaMetrics(
        current_version=facts.current_version,
        expected_version=facts.expected_version,
        user_version=facts.user_version,
        compatible=compatible,
        required_structures_available=(
            facts.required_structures_available
        ),
    )


def _skipped_probe(
    probe_name: DatabaseProbeName,
    reason: DatabaseDiagnosticReason,
) -> DatabaseQueryProbe:
    return DatabaseQueryProbe(
        probe_name=probe_name,
        status=DatabaseProbeStatus.SKIPPED,
        duration_ms=0.0,
        reason=reason,
    )


def _probe_failure(
    reason: DatabaseDiagnosticReason,
) -> tuple[DatabaseProbeStatus, DatabaseDiagnosticReason]:
    if reason is DatabaseDiagnosticReason.DATABASE_BUSY:
        return DatabaseProbeStatus.BUSY, reason
    if reason is DatabaseDiagnosticReason.DATABASE_UNAVAILABLE:
        return DatabaseProbeStatus.UNAVAILABLE, reason
    if reason is DatabaseDiagnosticReason.DATA_INVALID:
        return DatabaseProbeStatus.FAILED, reason
    if reason is DatabaseDiagnosticReason.BUDGET_EXHAUSTED:
        return DatabaseProbeStatus.FAILED, reason
    return (
        DatabaseProbeStatus.FAILED,
        DatabaseDiagnosticReason.QUERY_FAILED,
    )


def _core_read_error(
    reason: DatabaseDiagnosticReason,
) -> ReadDataUnavailable:
    if reason is DatabaseDiagnosticReason.DATABASE_BUSY:
        return ReadDataUnavailable("database_busy")
    if reason is DatabaseDiagnosticReason.DATA_INVALID:
        return ReadDataUnavailable("data_invalid")
    return ReadDataUnavailable("database_unavailable")


def _health_status(
    *,
    schema: DatabaseSchemaMetrics,
    storage: DatabaseStorageMetrics | None,
    journal: DatabaseJournalMetrics | None,
    probe_set: DatabaseQueryProbeSet,
) -> DatabaseHealthStatus:
    """只依据明确诊断事实分类，不把单次耗时作为故障阈值。"""
    if not schema.compatible:
        return DatabaseHealthStatus.INCOMPATIBLE
    if storage is None or journal is None:
        return DatabaseHealthStatus.DEGRADED
    if (
        not journal.query_only
        or not journal.foreign_keys
        or journal.journal_mode is DatabaseJournalMode.OTHER
    ):
        return DatabaseHealthStatus.DEGRADED
    if (
        storage.database_file_size_bytes is None
        or storage.wal_present is None
        or (
            storage.wal_present
            and storage.wal_size_bytes is None
        )
    ):
        return DatabaseHealthStatus.DEGRADED
    if any(
        probe.status is not DatabaseProbeStatus.SUCCEEDED
        for probe in probe_set.probes
    ):
        return DatabaseHealthStatus.DEGRADED
    return DatabaseHealthStatus.HEALTHY


__all__ = ["DatabaseDiagnosticsService"]
