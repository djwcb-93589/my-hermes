"""SQLite 数据库固定只读诊断仓储。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes.observability.database_diagnostics import (
    DATABASE_DIAGNOSTICS_BUDGET_MS,
    MAX_DATABASE_NUMERIC_VALUE,
    DatabaseDiagnosticReason,
    DatabaseDiagnosticsRepositoryError,
    DatabaseJournalMetrics,
    DatabaseJournalMode,
    DatabaseSchemaFacts,
    DatabaseStorageMetrics,
)

from .read_only import readonly_connection
from .schema import LATEST_SCHEMA_VERSION


_OBSERVATION_REQUIRED_COLUMNS = frozenset({
    "observation_id",
    "event_type",
    "run_id",
    "parent_run_id",
    "tool_call_id",
    "tool_name",
    "status",
    "success",
    "error_type",
    "finish_reason",
    "has_text",
    "tool_call_count",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "duration_ms",
    "stop_reason",
    "iterations",
    "has_final_reply",
    "created_at",
})
_TOOL_EXECUTION_REQUIRED_COLUMNS = frozenset({
    "execution_id",
    "environment",
    "session_id",
    "source_message_id",
    "cron_run_id",
    "tool_call_id",
    "tool_name",
    "recovery_policy",
    "status",
    "result_json",
    "external_operation_id",
    "attempt_count",
    "created_at",
    "updated_at",
})
_RUNTIME_REQUIRED_COLUMNS = frozenset({
    "component_type",
    "component_id",
    "instance_id",
    "reported_state",
    "started_at",
    "heartbeat_at",
    "stopped_at",
    "error_type",
    "heartbeat_interval_seconds",
    "stale_after_seconds",
    "metadata_json",
    "updated_at",
})
_OBSERVATION_REQUIRED_INDEXES = frozenset({
    "idx_observations_created",
    "idx_observations_run",
    "idx_observations_parent_run",
    "idx_observations_event_type",
    "idx_observations_tool_name",
})
_TOOL_EXECUTION_REQUIRED_INDEXES = frozenset({
    "idx_tool_executions_monitoring_order",
})
_RUNTIME_REQUIRED_INDEXES = frozenset({
    "idx_runtime_snapshots_component_type",
    "idx_runtime_snapshots_reported_state",
    "idx_runtime_snapshots_heartbeat",
})
_REQUIRED_INDEX_CHECKS = (
    (
        "idx_observations_created",
        (("created_at", 1), ("observation_id", 1)),
    ),
    (
        "idx_observations_run",
        (("run_id", 0), ("created_at", 0), ("observation_id", 0)),
    ),
    (
        "idx_observations_parent_run",
        (("parent_run_id", 0), ("created_at", 0), ("observation_id", 0)),
    ),
    (
        "idx_observations_event_type",
        (("event_type", 0), ("created_at", 1), ("observation_id", 1)),
    ),
    (
        "idx_observations_tool_name",
        (("tool_name", 0), ("created_at", 1), ("observation_id", 1)),
    ),
    (
        "idx_tool_executions_monitoring_order",
        (("updated_at", 1), ("execution_id", 1)),
    ),
    (
        "idx_runtime_snapshots_component_type",
        (("component_type", 0),),
    ),
    (
        "idx_runtime_snapshots_reported_state",
        (("reported_state", 0),),
    ),
    (
        "idx_runtime_snapshots_heartbeat",
        (("heartbeat_at", 0),),
    ),
)
_JOURNAL_MODES = {
    mode.value: mode
    for mode in DatabaseJournalMode
    if mode is not DatabaseJournalMode.OTHER
}
_MIN_SQLITE_PAGE_SIZE = 512
_MAX_SQLITE_PAGE_SIZE = 65_536
_SQLITE_PROGRESS_HANDLER_STEPS = 100


class SQLiteDatabaseDiagnosticsRepository:
    """每个固定探针均使用独立、有界生命周期的只读连接。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path):
        """仅规范化配置路径，不连接数据库或访问文件系统。"""
        if not isinstance(db_path, (str, Path)):
            raise TypeError("db_path must be a path")
        normalized = str(db_path)
        if not normalized.strip():
            raise ValueError("db_path must be a non-empty path")
        self._db_path = normalized

    def check_connection(self, timeout_ms: int) -> None:
        """确认数据库能够只读打开并执行一个固定单行读取。"""
        deadline_ns = _probe_deadline_ns(timeout_ms)
        try:
            with _diagnostic_connection(
                self._db_path,
                timeout_ms,
                deadline_ns,
            ) as conn:
                row = conn.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise _sqlite_repository_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
            ) from exc
        if not _is_exact_one_row(row):
            raise _repository_error(DatabaseDiagnosticReason.DATA_INVALID)

    def read_schema_facts(self, timeout_ms: int) -> DatabaseSchemaFacts:
        """读取版本、监控事件与 Runtime 当前表结构，不执行 DDL 或迁移。"""
        deadline_ns = _probe_deadline_ns(timeout_ms)
        try:
            with _diagnostic_connection(
                self._db_path,
                timeout_ms,
                deadline_ns,
            ) as conn:
                schema_table_row = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type=? AND name=? LIMIT 1",
                    ("table", "schema_version"),
                ).fetchone()
                schema_version_column_rows = conn.execute(
                    "PRAGMA table_info(schema_version)"
                ).fetchall()
                schema_version_column_names = {
                    row[1]
                    for row in schema_version_column_rows
                    if isinstance(row, tuple)
                    and len(row) > 1
                    and type(row[1]) is str
                }
                schema_version_row = (
                    conn.execute(
                        "SELECT version FROM schema_version "
                        "ORDER BY version DESC LIMIT 1"
                    ).fetchone()
                    if (
                        schema_table_row is not None
                        and "version" in schema_version_column_names
                    )
                    else None
                )
                user_version_row = conn.execute(
                    "PRAGMA user_version"
                ).fetchone()
                observation_column_rows = conn.execute(
                    "PRAGMA table_info(observations)"
                ).fetchall()
                tool_execution_column_rows = conn.execute(
                    "PRAGMA table_info(tool_executions)"
                ).fetchall()
                runtime_column_rows = conn.execute(
                    "PRAGMA table_info(runtime_component_snapshots)"
                ).fetchall()
                observation_index_rows = conn.execute(
                    "PRAGMA index_list(observations)"
                ).fetchall()
                tool_execution_index_rows = conn.execute(
                    "PRAGMA index_list(tool_executions)"
                ).fetchall()
                runtime_index_rows = conn.execute(
                    "PRAGMA index_list(runtime_component_snapshots)"
                ).fetchall()
                index_signature_rows = (
                    (
                        "idx_observations_created",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_observations_created')"
                        ).fetchall(),
                    ),
                    (
                        "idx_observations_run",
                        conn.execute(
                            "PRAGMA index_xinfo('idx_observations_run')"
                        ).fetchall(),
                    ),
                    (
                        "idx_observations_parent_run",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_observations_parent_run')"
                        ).fetchall(),
                    ),
                    (
                        "idx_observations_event_type",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_observations_event_type')"
                        ).fetchall(),
                    ),
                    (
                        "idx_observations_tool_name",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_observations_tool_name')"
                        ).fetchall(),
                    ),
                    (
                        "idx_tool_executions_monitoring_order",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_tool_executions_monitoring_order')"
                        ).fetchall(),
                    ),
                    (
                        "idx_runtime_snapshots_component_type",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_runtime_snapshots_component_type')"
                        ).fetchall(),
                    ),
                    (
                        "idx_runtime_snapshots_reported_state",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_runtime_snapshots_reported_state')"
                        ).fetchall(),
                    ),
                    (
                        "idx_runtime_snapshots_heartbeat",
                        conn.execute(
                            "PRAGMA index_xinfo("
                            "'idx_runtime_snapshots_heartbeat')"
                        ).fetchall(),
                    ),
                )
        except sqlite3.Error as exc:
            raise _sqlite_repository_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
            ) from exc

        try:
            schema_table_available = _marker_available(schema_table_row)
            schema_version_columns = _pragma_names(
                schema_version_column_rows,
                name_position=1,
            )
            current_version = (
                _optional_schema_version(schema_version_row)
                if (
                    schema_table_available
                    and "version" in schema_version_columns
                )
                else None
            )
            user_version = _required_scalar_integer(
                user_version_row,
                allow_zero=True,
            )
            observation_columns = _pragma_names(
                observation_column_rows,
                name_position=1,
            )
            tool_execution_columns = _pragma_names(
                tool_execution_column_rows,
                name_position=1,
            )
            runtime_columns = _pragma_names(
                runtime_column_rows,
                name_position=1,
            )
            observation_indexes = _pragma_names(
                observation_index_rows,
                name_position=1,
            )
            tool_execution_indexes = _pragma_names(
                tool_execution_index_rows,
                name_position=1,
            )
            runtime_indexes = _pragma_names(
                runtime_index_rows,
                name_position=1,
            )
            signatures = {
                index_name: _index_signature(rows)
                for index_name, rows in index_signature_rows
            }
            required_structures_available = (
                schema_table_available
                and "version" in schema_version_columns
                and _OBSERVATION_REQUIRED_COLUMNS <= observation_columns
                and (
                    _TOOL_EXECUTION_REQUIRED_COLUMNS
                    <= tool_execution_columns
                )
                and _RUNTIME_REQUIRED_COLUMNS <= runtime_columns
                and _OBSERVATION_REQUIRED_INDEXES <= observation_indexes
                and (
                    _TOOL_EXECUTION_REQUIRED_INDEXES
                    <= tool_execution_indexes
                )
                and _RUNTIME_REQUIRED_INDEXES <= runtime_indexes
                and all(
                    signatures[index_name] == expected
                    for index_name, expected in _REQUIRED_INDEX_CHECKS
                )
            )
            return DatabaseSchemaFacts(
                current_version=current_version,
                expected_version=LATEST_SCHEMA_VERSION,
                user_version=user_version,
                required_structures_available=required_structures_available,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATA_INVALID
            ) from exc

    def read_journal_metrics(
        self,
        timeout_ms: int,
    ) -> DatabaseJournalMetrics:
        """读取本次诊断连接的固定 Journal 与连接级安全配置。"""
        deadline_ns = _probe_deadline_ns(timeout_ms)
        try:
            with _diagnostic_connection(
                self._db_path,
                timeout_ms,
                deadline_ns,
            ) as conn:
                journal_mode_row = conn.execute(
                    "PRAGMA journal_mode"
                ).fetchone()
                query_only_row = conn.execute(
                    "PRAGMA query_only"
                ).fetchone()
                foreign_keys_row = conn.execute(
                    "PRAGMA foreign_keys"
                ).fetchone()
                busy_timeout_row = conn.execute(
                    "PRAGMA busy_timeout"
                ).fetchone()
        except sqlite3.Error as exc:
            raise _sqlite_repository_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
            ) from exc

        try:
            return DatabaseJournalMetrics(
                journal_mode=_journal_mode(journal_mode_row),
                query_only=_required_scalar_boolean(query_only_row),
                foreign_keys=_required_scalar_boolean(foreign_keys_row),
                busy_timeout_ms=_required_scalar_integer(
                    busy_timeout_row,
                    allow_zero=True,
                ),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATA_INVALID
            ) from exc

    def read_storage_metrics(
        self,
        timeout_ms: int,
    ) -> DatabaseStorageMetrics:
        """读取固定页指标，并仅 stat 数据库和精确的 WAL sidecar。"""
        deadline_ns = _probe_deadline_ns(timeout_ms)
        try:
            with _diagnostic_connection(
                self._db_path,
                timeout_ms,
                deadline_ns,
            ) as conn:
                page_size_row = conn.execute(
                    "PRAGMA page_size"
                ).fetchone()
                page_count_row = conn.execute(
                    "PRAGMA page_count"
                ).fetchone()
                freelist_count_row = conn.execute(
                    "PRAGMA freelist_count"
                ).fetchone()
        except sqlite3.Error as exc:
            raise _sqlite_repository_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
            ) from exc

        try:
            page_size = _required_scalar_integer(
                page_size_row,
                allow_zero=False,
            )
            if (
                not _MIN_SQLITE_PAGE_SIZE
                <= page_size
                <= _MAX_SQLITE_PAGE_SIZE
                or (page_size & (page_size - 1)) != 0
            ):
                raise ValueError("page size is invalid")
            page_count = _required_scalar_integer(
                page_count_row,
                allow_zero=True,
            )
            freelist_count = _required_scalar_integer(
                freelist_count_row,
                allow_zero=True,
            )
            if freelist_count > page_count:
                raise ValueError("freelist page count is invalid")
            database_size = _checked_product(page_size, page_count)
            free_space = _checked_product(page_size, freelist_count)
            database_file_size = (
                None
                if _deadline_reached(deadline_ns)
                else _optional_file_size(Path(self._db_path))
            )
            wal_present, wal_size = (
                (None, None)
                if _deadline_reached(deadline_ns)
                else _wal_file_metrics(Path(f"{self._db_path}-wal"))
            )
            return DatabaseStorageMetrics(
                page_size_bytes=page_size,
                page_count=page_count,
                freelist_page_count=freelist_count,
                database_size_bytes=database_size,
                free_space_bytes=free_space,
                used_space_bytes=database_size - free_space,
                database_file_size_bytes=database_file_size,
                wal_present=wal_present,
                wal_size_bytes=wal_size,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATA_INVALID
            ) from exc

    def recent_observation_lookup(self, timeout_ms: int) -> int:
        """通过固定排序索引执行至多返回一行的 Observation 探针。"""
        deadline_ns = _probe_deadline_ns(timeout_ms)
        try:
            with _diagnostic_connection(
                self._db_path,
                timeout_ms,
                deadline_ns,
            ) as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM observations INDEXED BY idx_observations_created
                    ORDER BY created_at DESC, observation_id DESC
                    LIMIT 1
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise _sqlite_repository_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
            ) from exc
        return _lookup_row_count(row)

    def recent_tool_execution_lookup(self, timeout_ms: int) -> int:
        """通过固定排序索引执行至多返回一行的执行日志探针。"""
        deadline_ns = _probe_deadline_ns(timeout_ms)
        try:
            with _diagnostic_connection(
                self._db_path,
                timeout_ms,
                deadline_ns,
            ) as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM tool_executions
                        INDEXED BY idx_tool_executions_monitoring_order
                    ORDER BY updated_at DESC, execution_id DESC
                    LIMIT 1
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise _sqlite_repository_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise _repository_error(
                DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
            ) from exc
        return _lookup_row_count(row)


def _probe_deadline_ns(timeout_ms: object) -> int:
    """验证应用层预算并转换为本次探针的单调截止时间。"""
    if (
        type(timeout_ms) is not int
        or timeout_ms < 1
        or timeout_ms > DATABASE_DIAGNOSTICS_BUDGET_MS
    ):
        raise _repository_error(DatabaseDiagnosticReason.DATA_INVALID)
    return time.perf_counter_ns() + timeout_ms * 1_000_000


@contextmanager
def _diagnostic_connection(
    db_path: str,
    timeout_ms: int,
    deadline_ns: int,
) -> Iterator[sqlite3.Connection]:
    """在现有只读连接上收紧 busy wait，并用进度回调执行截止。"""
    with readonly_connection(
        db_path,
        busy_timeout_ms=timeout_ms,
    ) as conn:
        def interrupt_after_deadline() -> int:
            return 1 if _deadline_reached(deadline_ns) else 0

        conn.set_progress_handler(
            interrupt_after_deadline,
            _SQLITE_PROGRESS_HANDLER_STEPS,
        )
        try:
            yield conn
        finally:
            conn.set_progress_handler(None, 0)


def _deadline_reached(deadline_ns: int) -> bool:
    return time.perf_counter_ns() >= deadline_ns


def _marker_available(row: object) -> bool:
    if row is None:
        return False
    if not _is_exact_one_row(row):
        raise ValueError("schema marker is invalid")
    return True


def _lookup_row_count(row: object) -> int:
    if row is None:
        return 0
    if not _is_exact_one_row(row):
        raise _repository_error(DatabaseDiagnosticReason.DATA_INVALID)
    return 1


def _is_exact_one_row(row: object) -> bool:
    return (
        isinstance(row, tuple)
        and len(row) == 1
        and type(row[0]) is int
        and row[0] == 1
    )


def _optional_schema_version(row: object) -> int | None:
    if row is None:
        return None
    return _required_scalar_integer(row, allow_zero=True)


def _required_scalar_integer(
    row: object,
    *,
    allow_zero: bool,
) -> int:
    if not isinstance(row, tuple) or len(row) != 1:
        raise ValueError("diagnostic scalar is invalid")
    value = row[0]
    if type(value) is not int:
        raise TypeError("diagnostic scalar must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum or value > MAX_DATABASE_NUMERIC_VALUE:
        raise ValueError("diagnostic scalar is outside the fixed limit")
    return value


def _required_scalar_boolean(row: object) -> bool:
    value = _required_scalar_integer(row, allow_zero=True)
    if value not in (0, 1):
        raise ValueError("diagnostic boolean is invalid")
    return bool(value)


def _journal_mode(row: object) -> DatabaseJournalMode:
    if not isinstance(row, tuple) or len(row) != 1:
        raise ValueError("journal mode is invalid")
    value = row[0]
    if (
        type(value) is not str
        or not value
        or value != value.strip()
    ):
        raise TypeError("journal mode must be text")
    return _JOURNAL_MODES.get(
        value.lower(),
        DatabaseJournalMode.OTHER,
    )


def _pragma_names(
    rows: object,
    *,
    name_position: int,
) -> frozenset[str]:
    if not isinstance(rows, list):
        raise TypeError("PRAGMA rows must be a list")
    names: list[str] = []
    for row in rows:
        if (
            not isinstance(row, tuple)
            or len(row) <= name_position
            or type(row[name_position]) is not str
            or not row[name_position]
        ):
            raise ValueError("PRAGMA structure row is invalid")
        names.append(row[name_position])
    if len(set(names)) != len(names):
        raise ValueError("PRAGMA structure names are duplicated")
    return frozenset(names)


def _index_signature(rows: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(rows, list):
        raise TypeError("index rows must be a list")
    signature: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, tuple) or len(row) < 6:
            raise ValueError("index structure row is invalid")
        descending = row[3]
        key_column = row[5]
        if type(descending) is not int or descending not in (0, 1):
            raise ValueError("index direction is invalid")
        if type(key_column) is not int or key_column not in (0, 1):
            raise ValueError("index key marker is invalid")
        if key_column == 0:
            continue
        name = row[2]
        if type(name) is not str or not name:
            raise ValueError("index column name is invalid")
        signature.append((name, descending))
    if len({name for name, _ in signature}) != len(signature):
        raise ValueError("index columns are duplicated")
    return tuple(signature)


def _checked_product(left: int, right: int) -> int:
    if left < 0 or right < 0:
        raise ValueError("storage metric is negative")
    if left and right > MAX_DATABASE_NUMERIC_VALUE // left:
        raise OverflowError("storage metric exceeds the fixed limit")
    return left * right


def _optional_file_size(path: Path) -> int | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return _bounded_file_size(size)


def _wal_file_metrics(path: Path) -> tuple[bool | None, int | None]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False, None
    except OSError:
        return None, None
    return True, _bounded_file_size(size)


def _bounded_file_size(value: object) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_DATABASE_NUMERIC_VALUE
    ):
        raise ValueError("file size is invalid")
    return value


def _sqlite_repository_error(
    exc: sqlite3.Error,
) -> DatabaseDiagnosticsRepositoryError:
    message = str(exc).lower()
    if "interrupted" in message:
        reason = DatabaseDiagnosticReason.BUDGET_EXHAUSTED
    elif "locked" in message or "busy" in message:
        reason = DatabaseDiagnosticReason.DATABASE_BUSY
    elif any(
        marker in message
        for marker in (
            "unable to open database file",
            "cannot open",
            "could not open",
        )
    ):
        reason = DatabaseDiagnosticReason.DATABASE_UNAVAILABLE
    elif any(
        marker in message
        for marker in (
            "no such table",
            "no such column",
            "no such index",
            "malformed",
            "not a database",
            "database schema",
            "datatype mismatch",
            "integer overflow",
        )
    ):
        reason = DatabaseDiagnosticReason.DATA_INVALID
    else:
        reason = DatabaseDiagnosticReason.QUERY_FAILED
    return _repository_error(reason)


def _repository_error(
    reason: DatabaseDiagnosticReason,
) -> DatabaseDiagnosticsRepositoryError:
    return DatabaseDiagnosticsRepositoryError(reason)


__all__ = ["SQLiteDatabaseDiagnosticsRepository"]
