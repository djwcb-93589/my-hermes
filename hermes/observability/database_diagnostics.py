"""与 SQLite 实现和 Dashboard 无关的数据库诊断契约。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar


DATABASE_DIAGNOSTICS_BUDGET_MS = 6_000
MAX_DATABASE_DIAGNOSTIC_PROBES = 6
MAX_DATABASE_PROBE_ROW_COUNT = 1
MAX_DATABASE_NUMERIC_VALUE = (1 << 63) - 1


class DatabaseHealthStatus(str, Enum):
    """数据库诊断允许公开的整体健康状态。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"


class DatabaseProbeStatus(str, Enum):
    """单个固定诊断探针允许公开的执行状态。"""

    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    FAILED = "failed"
    SKIPPED = "skipped"


class DatabaseProbeName(str, Enum):
    """按固定执行顺序声明的数据库诊断探针。"""

    OPEN_CONNECTION = "open_connection"
    READ_SCHEMA_VERSION = "read_schema_version"
    READ_JOURNAL_METRICS = "read_journal_metrics"
    READ_STORAGE_METRICS = "read_storage_metrics"
    RECENT_OBSERVATION_LOOKUP = "recent_observation_lookup"
    RECENT_TOOL_EXECUTION_LOOKUP = "recent_tool_execution_lookup"


class DatabaseDiagnosticReason(str, Enum):
    """数据库诊断失败或跳过的稳定分类。"""

    DATABASE_UNAVAILABLE = "database_unavailable"
    DATABASE_BUSY = "database_busy"
    QUERY_FAILED = "query_failed"
    DATA_INVALID = "data_invalid"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"


class DatabaseJournalMode(str, Enum):
    """允许公开的有限 SQLite Journal Mode。"""

    DELETE = "delete"
    TRUNCATE = "truncate"
    PERSIST = "persist"
    MEMORY = "memory"
    WAL = "wal"
    OFF = "off"
    OTHER = "other"


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum_value(
    value: object,
    enum_type: type[_EnumT],
    field_name: str,
) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a {enum_type.__name__} or string")
    try:
        return enum_type(value)
    except ValueError:
        raise ValueError(f"{field_name} is invalid") from None


def _exact_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _bounded_integer(
    value: object,
    field_name: str,
    *,
    allow_zero: bool = True,
    maximum: int = MAX_DATABASE_NUMERIC_VALUE,
) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} is invalid")
    return value


def _optional_bounded_integer(
    value: object,
    field_name: str,
    *,
    allow_zero: bool = True,
    maximum: int = MAX_DATABASE_NUMERIC_VALUE,
) -> int | None:
    if value is None:
        return None
    return _bounded_integer(
        value,
        field_name,
        allow_zero=allow_zero,
        maximum=maximum,
    )


def _nonnegative_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized < 0
        or normalized > MAX_DATABASE_NUMERIC_VALUE
    ):
        raise ValueError(f"{field_name} is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class DatabaseSchemaFacts:
    """Repository 读取的数据库版本与必要结构事实。"""

    current_version: int | None
    expected_version: int
    user_version: int | None
    required_structures_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "current_version",
            _optional_bounded_integer(
                self.current_version,
                "current_version",
            ),
        )
        object.__setattr__(
            self,
            "expected_version",
            _bounded_integer(
                self.expected_version,
                "expected_version",
                allow_zero=False,
            ),
        )
        object.__setattr__(
            self,
            "user_version",
            _optional_bounded_integer(self.user_version, "user_version"),
        )
        structures_available = _exact_bool(
            self.required_structures_available,
            "required_structures_available",
        )
        object.__setattr__(
            self,
            "required_structures_available",
            structures_available,
        )


@dataclass(frozen=True, slots=True)
class DatabaseSchemaMetrics:
    """应用层推导后的 Schema 兼容性安全摘要。"""

    current_version: int | None
    expected_version: int
    user_version: int | None
    compatible: bool
    required_structures_available: bool

    def __post_init__(self) -> None:
        facts = DatabaseSchemaFacts(
            current_version=self.current_version,
            expected_version=self.expected_version,
            user_version=self.user_version,
            required_structures_available=(
                self.required_structures_available
            ),
        )
        object.__setattr__(
            self,
            "current_version",
            facts.current_version,
        )
        object.__setattr__(
            self,
            "expected_version",
            facts.expected_version,
        )
        object.__setattr__(self, "user_version", facts.user_version)
        object.__setattr__(
            self,
            "required_structures_available",
            facts.required_structures_available,
        )
        compatible = _exact_bool(self.compatible, "compatible")
        object.__setattr__(self, "compatible", compatible)
        expected_compatibility = (
            self.current_version == self.expected_version
            and self.required_structures_available
        )
        if compatible is not expected_compatibility:
            raise ValueError("schema compatibility is inconsistent")


@dataclass(frozen=True, slots=True)
class DatabaseStorageMetrics:
    """数据库页使用量和可选文件大小的无路径安全摘要。"""

    page_size_bytes: int
    page_count: int
    freelist_page_count: int
    database_size_bytes: int
    free_space_bytes: int
    used_space_bytes: int
    database_file_size_bytes: int | None
    wal_present: bool | None
    wal_size_bytes: int | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "page_size_bytes",
            _bounded_integer(
                self.page_size_bytes,
                "page_size_bytes",
                allow_zero=False,
            ),
        )
        for field_name in (
            "page_count",
            "freelist_page_count",
            "database_size_bytes",
            "free_space_bytes",
            "used_space_bytes",
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded_integer(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "database_file_size_bytes",
            _optional_bounded_integer(
                self.database_file_size_bytes,
                "database_file_size_bytes",
            ),
        )
        if self.wal_present is not None:
            object.__setattr__(
                self,
                "wal_present",
                _exact_bool(self.wal_present, "wal_present"),
            )
        object.__setattr__(
            self,
            "wal_size_bytes",
            _optional_bounded_integer(
                self.wal_size_bytes,
                "wal_size_bytes",
            ),
        )
        if self.freelist_page_count > self.page_count:
            raise ValueError("freelist_page_count exceeds page_count")
        expected_database_size = self.page_size_bytes * self.page_count
        expected_free_space = (
            self.page_size_bytes * self.freelist_page_count
        )
        if (
            expected_database_size > MAX_DATABASE_NUMERIC_VALUE
            or expected_free_space > MAX_DATABASE_NUMERIC_VALUE
        ):
            raise ValueError("database storage metric exceeds the fixed limit")
        if self.database_size_bytes != expected_database_size:
            raise ValueError("database_size_bytes is inconsistent")
        if self.free_space_bytes != expected_free_space:
            raise ValueError("free_space_bytes is inconsistent")
        if (
            self.used_space_bytes
            != self.database_size_bytes - self.free_space_bytes
        ):
            raise ValueError("used_space_bytes is inconsistent")
        if self.wal_present is not True and self.wal_size_bytes is not None:
            raise ValueError("wal_size_bytes must be null when WAL is absent")


@dataclass(frozen=True, slots=True)
class DatabaseJournalMetrics:
    """本次只读诊断连接的有限 Journal 与连接状态。"""

    journal_mode: DatabaseJournalMode | str
    query_only: bool
    foreign_keys: bool
    busy_timeout_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "journal_mode",
            _enum_value(
                self.journal_mode,
                DatabaseJournalMode,
                "journal_mode",
            ),
        )
        query_only = _exact_bool(self.query_only, "query_only")
        object.__setattr__(self, "query_only", query_only)
        object.__setattr__(
            self,
            "foreign_keys",
            _exact_bool(self.foreign_keys, "foreign_keys"),
        )
        object.__setattr__(
            self,
            "busy_timeout_ms",
            _bounded_integer(
                self.busy_timeout_ms,
                "busy_timeout_ms",
                maximum=DATABASE_DIAGNOSTICS_BUDGET_MS,
            ),
        )


@dataclass(frozen=True, slots=True)
class DatabaseQueryProbe:
    """一个固定只读数据库探针的本次执行结果。"""

    probe_name: DatabaseProbeName | str
    status: DatabaseProbeStatus | str
    duration_ms: float
    returned_row_count: int | None = None
    reason: DatabaseDiagnosticReason | str | None = None

    def __post_init__(self) -> None:
        probe_name = _enum_value(
            self.probe_name,
            DatabaseProbeName,
            "probe_name",
        )
        status = _enum_value(
            self.status,
            DatabaseProbeStatus,
            "status",
        )
        object.__setattr__(self, "probe_name", probe_name)
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "duration_ms",
            _nonnegative_number(self.duration_ms, "duration_ms"),
        )
        object.__setattr__(
            self,
            "returned_row_count",
            _optional_bounded_integer(
                self.returned_row_count,
                "returned_row_count",
                maximum=MAX_DATABASE_PROBE_ROW_COUNT,
            ),
        )
        reason = (
            None
            if self.reason is None
            else _enum_value(
                self.reason,
                DatabaseDiagnosticReason,
                "reason",
            )
        )
        object.__setattr__(self, "reason", reason)
        if status is DatabaseProbeStatus.SUCCEEDED:
            if reason is not None:
                raise ValueError("successful probe reason must be null")
        elif reason is None:
            raise ValueError("unsuccessful probe reason must be present")
        allowed_reasons = {
            DatabaseProbeStatus.UNAVAILABLE: frozenset({
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE,
            }),
            DatabaseProbeStatus.BUSY: frozenset({
                DatabaseDiagnosticReason.DATABASE_BUSY,
            }),
            DatabaseProbeStatus.FAILED: frozenset({
                DatabaseDiagnosticReason.QUERY_FAILED,
                DatabaseDiagnosticReason.DATA_INVALID,
                DatabaseDiagnosticReason.BUDGET_EXHAUSTED,
            }),
            DatabaseProbeStatus.SKIPPED: frozenset({
                DatabaseDiagnosticReason.BUDGET_EXHAUSTED,
                DatabaseDiagnosticReason.SCHEMA_INCOMPATIBLE,
            }),
        }
        if (
            status is not DatabaseProbeStatus.SUCCEEDED
            and reason not in allowed_reasons[status]
        ):
            raise ValueError("probe status and reason are inconsistent")
        if (
            status is not DatabaseProbeStatus.SUCCEEDED
            and self.returned_row_count is not None
        ):
            raise ValueError(
                "unsuccessful probe returned_row_count must be null"
            )
        if (
            status is DatabaseProbeStatus.SKIPPED
            and self.duration_ms != 0
        ):
            raise ValueError("skipped probe duration_ms must be zero")


@dataclass(frozen=True, slots=True)
class DatabaseQueryProbeSet:
    """具有固定数量、顺序和总预算的单次探针结果集合。"""

    checked_at: float
    total_duration_ms: float
    budget_ms: int
    budget_exhausted: bool
    probes: tuple[DatabaseQueryProbe, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checked_at",
            _nonnegative_number(self.checked_at, "checked_at"),
        )
        object.__setattr__(
            self,
            "total_duration_ms",
            _nonnegative_number(
                self.total_duration_ms,
                "total_duration_ms",
            ),
        )
        budget_ms = _bounded_integer(
            self.budget_ms,
            "budget_ms",
            allow_zero=False,
            maximum=DATABASE_DIAGNOSTICS_BUDGET_MS,
        )
        if budget_ms != DATABASE_DIAGNOSTICS_BUDGET_MS:
            raise ValueError("budget_ms must use the fixed diagnostic budget")
        object.__setattr__(self, "budget_ms", budget_ms)
        budget_exhausted = _exact_bool(
            self.budget_exhausted,
            "budget_exhausted",
        )
        object.__setattr__(
            self,
            "budget_exhausted",
            budget_exhausted,
        )
        if (
            type(self.probes) is not tuple
            or not self.probes
            or len(self.probes) > MAX_DATABASE_DIAGNOSTIC_PROBES
            or any(
                not isinstance(probe, DatabaseQueryProbe)
                for probe in self.probes
            )
        ):
            raise TypeError(
                "probes must be a non-empty bounded tuple of "
                "DatabaseQueryProbe"
            )
        probe_names = tuple(probe.probe_name for probe in self.probes)
        if probe_names != tuple(DatabaseProbeName):
            raise ValueError(
                "probes must contain the complete fixed probe sequence"
            )
        has_budget_exhaustion = any(
            probe.reason is DatabaseDiagnosticReason.BUDGET_EXHAUSTED
            for probe in self.probes
        )
        if has_budget_exhaustion and not budget_exhausted:
            raise ValueError("budget exhaustion state is inconsistent")
        if (
            budget_exhausted
            and not has_budget_exhaustion
            and self.total_duration_ms < budget_ms
        ):
            raise ValueError("budget exhaustion state is inconsistent")
        if (
            self.total_duration_ms >= budget_ms
            and not budget_exhausted
        ):
            raise ValueError("budget exhaustion state is inconsistent")


@dataclass(frozen=True, slots=True)
class DatabaseHealthSnapshot:
    """一次请求内推导且不持久化的数据库健康快照。"""

    checked_at: float
    status: DatabaseHealthStatus | str
    schema: DatabaseSchemaMetrics
    storage: DatabaseStorageMetrics | None
    journal: DatabaseJournalMetrics | None
    probe_set: DatabaseQueryProbeSet

    def __post_init__(self) -> None:
        checked_at = _nonnegative_number(self.checked_at, "checked_at")
        object.__setattr__(self, "checked_at", checked_at)
        status = _enum_value(
            self.status,
            DatabaseHealthStatus,
            "status",
        )
        object.__setattr__(self, "status", status)
        if not isinstance(self.schema, DatabaseSchemaMetrics):
            raise TypeError("schema must be a DatabaseSchemaMetrics")
        if (
            self.storage is not None
            and not isinstance(self.storage, DatabaseStorageMetrics)
        ):
            raise TypeError(
                "storage must be a DatabaseStorageMetrics or null"
            )
        if (
            self.journal is not None
            and not isinstance(self.journal, DatabaseJournalMetrics)
        ):
            raise TypeError(
                "journal must be a DatabaseJournalMetrics or null"
            )
        if not isinstance(self.probe_set, DatabaseQueryProbeSet):
            raise TypeError("probe_set must be a DatabaseQueryProbeSet")
        if checked_at != self.probe_set.checked_at:
            raise ValueError("snapshot checked_at is inconsistent")
        if status in {
            DatabaseHealthStatus.HEALTHY,
            DatabaseHealthStatus.DEGRADED,
        } and not self.schema.compatible:
            raise ValueError(
                "available health status requires a compatible schema"
            )
        if (
            status is DatabaseHealthStatus.INCOMPATIBLE
            and self.schema.compatible
        ):
            raise ValueError(
                "incompatible health status requires an incompatible schema"
            )
        if status is DatabaseHealthStatus.HEALTHY:
            if self.storage is None or self.journal is None:
                raise ValueError(
                    "healthy status requires complete database metrics"
                )
            if any(
                probe.status is not DatabaseProbeStatus.SUCCEEDED
                for probe in self.probe_set.probes
            ):
                raise ValueError(
                    "healthy status requires all probes to succeed"
                )


class DatabaseDiagnosticsRepositoryError(Exception):
    """不携带路径、SQL 或底层异常正文的中立诊断错误。"""

    def __init__(
        self,
        reason: DatabaseDiagnosticReason | str,
    ) -> None:
        self.reason = _enum_value(
            reason,
            DatabaseDiagnosticReason,
            "reason",
        )
        super().__init__(self.reason.value)


class DatabaseDiagnosticsRepository(Protocol):
    """数据库固定只读诊断操作的中立 Repository 边界。"""

    def check_connection(self, timeout_ms: int) -> None:
        """确认能够建立并使用只读查询连接。"""

    def read_schema_facts(self, timeout_ms: int) -> DatabaseSchemaFacts:
        """读取版本和必要结构存在性的安全摘要。"""

    def read_journal_metrics(
        self,
        timeout_ms: int,
    ) -> DatabaseJournalMetrics:
        """读取本次诊断连接的有限 Journal 指标。"""

    def read_storage_metrics(
        self,
        timeout_ms: int,
    ) -> DatabaseStorageMetrics:
        """读取页使用量及可选文件状态摘要。"""

    def recent_observation_lookup(self, timeout_ms: int) -> int:
        """执行固定且最多返回一行的 Observation 索引探针。"""

    def recent_tool_execution_lookup(self, timeout_ms: int) -> int:
        """执行固定且最多返回一行的 Tool Execution 索引探针。"""


__all__ = [
    "DATABASE_DIAGNOSTICS_BUDGET_MS",
    "MAX_DATABASE_DIAGNOSTIC_PROBES",
    "MAX_DATABASE_NUMERIC_VALUE",
    "MAX_DATABASE_PROBE_ROW_COUNT",
    "DatabaseDiagnosticReason",
    "DatabaseDiagnosticsRepository",
    "DatabaseDiagnosticsRepositoryError",
    "DatabaseHealthSnapshot",
    "DatabaseHealthStatus",
    "DatabaseJournalMetrics",
    "DatabaseJournalMode",
    "DatabaseProbeName",
    "DatabaseProbeStatus",
    "DatabaseQueryProbe",
    "DatabaseQueryProbeSet",
    "DatabaseSchemaFacts",
    "DatabaseSchemaMetrics",
    "DatabaseStorageMetrics",
]
