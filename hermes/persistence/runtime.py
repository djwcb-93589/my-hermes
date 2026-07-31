"""Runtime Component 当前快照的 SQLite Publisher 与只读 Repository。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from hermes.observability.contracts import freeze_runtime_metadata
from hermes.observability.runtime import (
    MAX_RUNTIME_OFFSET,
    RuntimeComponentRecord,
    RuntimeComponentSnapshot,
    RuntimeComponentState,
    RuntimeLifecycleTransitionError,
    RuntimeStatusRecordInvalid,
    RuntimeStatusRepositoryUnavailable,
    validate_runtime_identity,
    validate_runtime_repository_limit,
    validate_runtime_transition,
)

from .database import DBError, _immediate_transaction
from .read_only import readonly_connection
from .schema import LATEST_SCHEMA_VERSION
from .write_existing import existing_write_connection


_RUNTIME_COLUMNS = (
    "component_type, component_id, instance_id, reported_state, started_at, "
    "heartbeat_at, stopped_at, error_type, heartbeat_interval_seconds, "
    "stale_after_seconds, metadata_json, updated_at"
)
_RUNTIME_UPDATE_COLUMNS = (
    "instance_id=?, reported_state=?, started_at=?, heartbeat_at=?, "
    "stopped_at=?, error_type=?, heartbeat_interval_seconds=?, "
    "stale_after_seconds=?, metadata_json=?, updated_at=?"
)


class RuntimeStatusPersistenceError(DBError):
    """Runtime 当前状态无法安全持久化。"""


class RuntimeSnapshotConflictError(RuntimeStatusPersistenceError):
    """同一时间点或启动代际出现互不相同的快照。"""


def _db_path(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("db_path must be a path")
    normalized = str(value)
    if not normalized.strip():
        raise ValueError("db_path must be a non-empty path")
    return normalized


def _canonical_metadata_json(metadata: Mapping[str, object]) -> str:
    """把已冻结白名单 Metadata 编码成稳定、紧凑的 JSON。"""
    frozen = freeze_runtime_metadata(metadata)
    return json.dumps(
        dict(frozen),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _updated_at(clock: Callable[[], float]) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("runtime persistence clock must return a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("runtime persistence clock must return a number")
    return normalized


def _snapshot_values(
    snapshot: RuntimeComponentSnapshot,
    metadata_json: str,
) -> tuple[object, ...]:
    return (
        snapshot.instance_id,
        snapshot.state.value,
        snapshot.started_at,
        snapshot.heartbeat_at,
        snapshot.stopped_at,
        snapshot.error_type,
        snapshot.heartbeat_interval_seconds,
        snapshot.stale_after_seconds,
        metadata_json,
    )


def _record_values(record: RuntimeComponentRecord) -> tuple[object, ...]:
    return (
        record.instance_id,
        record.reported_state.value,
        record.started_at,
        record.heartbeat_at,
        record.stopped_at,
        record.error_type,
        record.heartbeat_interval_seconds,
        record.stale_after_seconds,
        _canonical_metadata_json(record.metadata),
    )


class SQLiteRuntimeStatusPublisher:
    """在一个短事务中比较并写入逻辑组件的当前有效实例。"""

    __slots__ = ("_clock", "_db_path")

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._db_path = _db_path(db_path)
        self._clock = time.time if clock is None else clock
        if not callable(self._clock):
            raise TypeError("clock must be callable")

    def publish(self, snapshot: RuntimeComponentSnapshot) -> None:
        """幂等写入更晚快照，忽略迟到快照并拒绝歧义冲突。"""
        if not isinstance(snapshot, RuntimeComponentSnapshot):
            raise TypeError("snapshot must be a RuntimeComponentSnapshot")
        try:
            metadata_json = _canonical_metadata_json(snapshot.metadata)
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _validate_runtime_schema(conn)
                    row = conn.execute(
                        f"""
                        SELECT {_RUNTIME_COLUMNS}
                        FROM runtime_component_snapshots
                        WHERE component_type=? AND component_id=?
                        """,
                        (snapshot.component_type, snapshot.component_id),
                    ).fetchone()
                    if row is None:
                        self._insert(
                            conn,
                            snapshot,
                            metadata_json,
                            _updated_at(self._clock),
                        )
                        return
                    current = _runtime_record(row)
                    action = _snapshot_action(
                        current,
                        snapshot,
                        metadata_json,
                    )
                    if action == "ignore":
                        return
                    self._update(
                        conn,
                        snapshot,
                        metadata_json,
                        _updated_at(self._clock),
                    )
        except RuntimeSnapshotConflictError:
            raise
        except (
            RuntimeStatusRecordInvalid,
            RuntimeStatusPersistenceError,
            sqlite3.Error,
            OSError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeStatusPersistenceError(
                "runtime status persistence failed"
            ) from exc

    @staticmethod
    def _insert(
        conn: sqlite3.Connection,
        snapshot: RuntimeComponentSnapshot,
        metadata_json: str,
        updated_at: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO runtime_component_snapshots (
                component_type, component_id, instance_id,
                reported_state, started_at, heartbeat_at,
                stopped_at, error_type, heartbeat_interval_seconds,
                stale_after_seconds, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.component_type,
                snapshot.component_id,
                *_snapshot_values(snapshot, metadata_json),
                updated_at,
            ),
        )

    @staticmethod
    def _update(
        conn: sqlite3.Connection,
        snapshot: RuntimeComponentSnapshot,
        metadata_json: str,
        updated_at: float,
    ) -> None:
        cursor = conn.execute(
            f"""
            UPDATE runtime_component_snapshots
            SET {_RUNTIME_UPDATE_COLUMNS}
            WHERE component_type=? AND component_id=?
            """,
            (
                *_snapshot_values(snapshot, metadata_json),
                updated_at,
                snapshot.component_type,
                snapshot.component_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeStatusPersistenceError(
                "runtime status update did not affect one record"
            )


def _snapshot_action(
    current: RuntimeComponentRecord,
    incoming: RuntimeComponentSnapshot,
    metadata_json: str,
) -> str:
    """返回 update/ignore；冲突直接抛出稳定异常。"""
    if current.instance_id == incoming.instance_id:
        if incoming.heartbeat_at < current.heartbeat_at:
            return "ignore"
        if incoming.heartbeat_at == current.heartbeat_at:
            if _record_values(current) == _snapshot_values(
                incoming,
                metadata_json,
            ):
                return "ignore"
            raise RuntimeSnapshotConflictError(
                "runtime snapshot idempotency conflict"
            )
        if current.started_at != incoming.started_at:
            raise RuntimeSnapshotConflictError(
                "runtime snapshot start generation conflict"
            )
        try:
            validate_runtime_transition(
                current.reported_state,
                incoming.state,
            )
        except RuntimeLifecycleTransitionError as exc:
            raise RuntimeSnapshotConflictError(
                "runtime lifecycle transition conflict"
            ) from exc
        return "update"

    # 不同实例必须以明确启动代际接管；新的缺失代际快照无法证明更新，
    # 因此 fail-closed 保留当前实例，绝不按 instance_id 或迟到心跳排序。
    if incoming.started_at is None:
        return "ignore"
    if current.started_at is None:
        if incoming.started_at < current.heartbeat_at:
            return "ignore"
        if incoming.started_at == current.heartbeat_at:
            raise RuntimeSnapshotConflictError(
                "runtime instance generation conflict"
            )
        return "update"
    if incoming.started_at < current.started_at:
        return "ignore"
    if incoming.started_at == current.started_at:
        raise RuntimeSnapshotConflictError(
            "runtime instance generation conflict"
        )
    return "update"


class SQLiteRuntimeStatusReadRepository:
    """每次调用使用独立只读连接读取当前 Runtime Snapshot。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = _db_path(db_path)

    def list_components(
        self,
        *,
        component_type: str | None = None,
        reported_state: RuntimeComponentState | None = None,
        limit: int,
        offset: int,
    ) -> tuple[RuntimeComponentRecord, ...]:
        """使用固定过滤、排序和最多 101 行的内部分页边界。"""
        resolved_limit = validate_runtime_repository_limit(limit)
        if (
            type(offset) is not int
            or offset < 0
            or offset > MAX_RUNTIME_OFFSET
        ):
            raise ValueError("offset must be a non-negative integer")
        clauses: list[str] = []
        parameters: list[object] = []
        if component_type is not None:
            clauses.append("component_type=?")
            parameters.append(
                validate_runtime_identity(
                    component_type,
                    "component_type",
                )
            )
        if reported_state is not None:
            if not isinstance(reported_state, RuntimeComponentState):
                raise TypeError(
                    "reported_state must be a RuntimeComponentState"
                )
            clauses.append("reported_state=?")
            parameters.append(reported_state.value)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._fetchall(
            f"""
            SELECT {_RUNTIME_COLUMNS}
            FROM runtime_component_snapshots
            {where}
            ORDER BY component_type ASC, component_id ASC
            LIMIT ? OFFSET ?
            """,
            (*parameters, resolved_limit, offset),
        )
        return _runtime_records(rows)

    def get_component(
        self,
        component_type: str,
        component_id: str,
    ) -> RuntimeComponentRecord | None:
        """按复合逻辑主键读取当前实例。"""
        row = self._fetchone(
            f"""
            SELECT {_RUNTIME_COLUMNS}
            FROM runtime_component_snapshots
            WHERE component_type=? AND component_id=?
            """,
            (
                validate_runtime_identity(
                    component_type,
                    "component_type",
                ),
                validate_runtime_identity(component_id, "component_id"),
            ),
        )
        if row is None:
            return None
        try:
            return _runtime_record(row)
        except (
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
        ) as exc:
            raise RuntimeStatusRecordInvalid() from exc

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> list[tuple]:
        try:
            with readonly_connection(self._db_path) as conn:
                _validate_runtime_schema(conn)
                return list(conn.execute(sql, parameters).fetchall())
        except sqlite3.Error as exc:
            raise _runtime_read_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise RuntimeStatusRepositoryUnavailable(
                "database_unavailable"
            ) from exc

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> tuple | None:
        try:
            with readonly_connection(self._db_path) as conn:
                _validate_runtime_schema(conn)
                return conn.execute(sql, parameters).fetchone()
        except sqlite3.Error as exc:
            raise _runtime_read_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise RuntimeStatusRepositoryUnavailable(
                "database_unavailable"
            ) from exc


def _validate_runtime_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if (
        row is None
        or len(row) != 1
        or type(row[0]) is not int
        or row[0] != LATEST_SCHEMA_VERSION
    ):
        raise RuntimeStatusRecordInvalid("schema_incompatible")


def _runtime_records(rows: list[tuple]) -> tuple[RuntimeComponentRecord, ...]:
    try:
        return tuple(_runtime_record(row) for row in rows)
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise RuntimeStatusRecordInvalid() from exc


def _runtime_record(row: tuple) -> RuntimeComponentRecord:
    if not isinstance(row, tuple) or len(row) != 12:
        raise ValueError("runtime record shape is invalid")
    metadata_raw = row[10]
    if type(metadata_raw) is not str:
        raise ValueError("runtime metadata record is invalid")
    metadata = json.loads(
        metadata_raw,
        object_pairs_hook=_unique_json_object,
    )
    if type(metadata) is not dict:
        raise ValueError("runtime metadata record is invalid")
    return RuntimeComponentRecord(
        component_type=row[0],
        component_id=row[1],
        instance_id=row[2],
        reported_state=row[3],
        started_at=row[4],
        heartbeat_at=row[5],
        stopped_at=row[6],
        error_type=row[7],
        heartbeat_interval_seconds=row[8],
        stale_after_seconds=row[9],
        metadata=metadata,
        updated_at=row[11],
    )


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """拒绝重复 JSON 键，避免解析时静默覆盖损坏 Metadata。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("runtime metadata contains duplicate keys")
        result[key] = value
    return result


def _runtime_read_error(
    exc: sqlite3.Error,
) -> RuntimeStatusRecordInvalid | RuntimeStatusRepositoryUnavailable:
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        return RuntimeStatusRepositoryUnavailable("database_busy")
    if any(
        marker in message
        for marker in (
            "no such table",
            "no such column",
            "database schema",
        )
    ):
        return RuntimeStatusRecordInvalid("schema_incompatible")
    if any(
        marker in message
        for marker in (
            "malformed",
            "not a database",
            "datatype mismatch",
        )
    ):
        return RuntimeStatusRecordInvalid()
    return RuntimeStatusRepositoryUnavailable("database_unavailable")


__all__ = [
    "RuntimeSnapshotConflictError",
    "RuntimeStatusPersistenceError",
    "SQLiteRuntimeStatusPublisher",
    "SQLiteRuntimeStatusReadRepository",
]
