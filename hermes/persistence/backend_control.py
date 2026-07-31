"""Backend Control 的 SQLite 写仓储与独立只读状态仓储。"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hermes.backend_control import (
    SUPERVISOR_RUNTIME_LEASE_NAME,
    BackendControlAction,
    BackendControlConflict,
    BackendControlCreation,
    BackendControlRequest,
    BackendControlRequestStatus,
    BackendControlResult,
    BackendControlStage,
    BackendControlUnavailable,
    BackendGatewayStatus,
    BackendObservedState,
    BackendOwnershipState,
    BackendProcessBinding,
    BackendResultCode,
    BackendStatusSnapshot,
    BackendSupervisorStatus,
    BackendType,
    RuntimeLeaseSnapshot,
    SupervisorFence,
    SupervisorInstanceState,
    validate_security_digest,
)
from hermes.gateway.constants import GATEWAY_RUNTIME_LEASE_NAME

from .database import _immediate_transaction
from .read_only import readonly_connection
from .write_existing import existing_write_connection


_REQUEST_COLUMNS = (
    "request_id, backend_type, action, status, created_at, started_at, "
    "completed_at, result_code, result_reference, exception_type, "
    "forced_termination, execution_stage"
)
_BINDING_COLUMNS = (
    "backend_type, supervisor_instance_id, launch_id, pid, "
    "process_identity_token, identity_verified, observed_state, started_at, "
    "config_revision_at_launch, last_exit_at, last_exit_code, "
    "last_request_id, updated_at"
)


class BackendControlPersistenceError(BackendControlUnavailable):
    """SQLite 控制设施不可用的稳定边界。"""


def _db_path(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("backend control db_path must be a path")
    normalized = str(value)
    if not normalized.strip():
        raise ValueError("backend control db_path must be non-empty")
    return normalized


def _timestamp(value: datetime, field_name: str) -> float:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    timestamp = value.astimezone(UTC).timestamp()
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError(f"{field_name} is invalid")
    return timestamp


def _datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} is invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} is invalid")
    return datetime.fromtimestamp(normalized, UTC)


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    return None if value is None else _datetime(value, field_name)


def _database_bool(value: object, field_name: str) -> bool:
    if type(value) is not int or value not in {0, 1}:
        raise ValueError(f"{field_name} is invalid")
    return value == 1


def _optional_database_bool(value: object, field_name: str) -> bool | None:
    return None if value is None else _database_bool(value, field_name)


def _database_integer(
    value: object,
    field_name: str,
    *,
    positive: bool = False,
) -> int:
    if type(value) is not int or (positive and value <= 0):
        raise ValueError(f"{field_name} is invalid")
    return value


def _request_from_row(row: object) -> BackendControlRequest:
    if not isinstance(row, tuple) or len(row) != 12:
        raise ValueError("backend control request row is invalid")
    (
        request_id,
        backend_type,
        action,
        status,
        created_at,
        started_at,
        completed_at,
        result_code,
        result_reference,
        exception_type,
        forced_termination,
        execution_stage,
    ) = row
    return BackendControlRequest(
        request_id=str(request_id),
        backend_type=BackendType(str(backend_type)),
        action=BackendControlAction(str(action)),
        status=BackendControlRequestStatus(str(status)),
        created_at=_datetime(created_at, "created_at"),
        started_at=_optional_datetime(started_at, "started_at"),
        completed_at=_optional_datetime(completed_at, "completed_at"),
        result_code=(
            None if result_code is None else BackendResultCode(str(result_code))
        ),
        result_reference=(
            None if result_reference is None else str(result_reference)
        ),
        exception_type=None if exception_type is None else str(exception_type),
        forced_termination=_database_bool(
            forced_termination,
            "forced_termination",
        ),
        execution_stage=(
            None
            if execution_stage is None
            else BackendControlStage(str(execution_stage))
        ),
    )


def _binding_from_row(row: object) -> BackendProcessBinding:
    if not isinstance(row, tuple) or len(row) != 13:
        raise ValueError("backend process binding row is invalid")
    (
        backend_type,
        supervisor_instance_id,
        launch_id,
        pid,
        process_identity_token,
        identity_verified,
        observed_state,
        started_at,
        config_revision_at_launch,
        last_exit_at,
        last_exit_code,
        last_request_id,
        updated_at,
    ) = row
    return BackendProcessBinding(
        backend_type=BackendType(str(backend_type)),
        observed_state=BackendObservedState(str(observed_state)),
        supervisor_instance_id=(
            None if supervisor_instance_id is None else str(supervisor_instance_id)
        ),
        launch_id=None if launch_id is None else str(launch_id),
        pid=(
            None
            if pid is None
            else _database_integer(pid, "pid", positive=True)
        ),
        process_identity_token=(
            None if process_identity_token is None else str(process_identity_token)
        ),
        identity_verified=_optional_database_bool(
            identity_verified,
            "identity_verified",
        ),
        started_at=_optional_datetime(started_at, "started_at"),
        config_revision_at_launch=(
            None
            if config_revision_at_launch is None
            else str(config_revision_at_launch)
        ),
        last_exit_at=_optional_datetime(last_exit_at, "last_exit_at"),
        last_exit_code=(
            None
            if last_exit_code is None
            else _database_integer(last_exit_code, "last_exit_code")
        ),
        last_request_id=None if last_request_id is None else str(last_request_id),
        updated_at=_optional_datetime(updated_at, "updated_at"),
    )


def _runtime_lease_from_row(
    row: object,
    observed_at: datetime,
) -> RuntimeLeaseSnapshot:
    if row is None:
        return RuntimeLeaseSnapshot(active=False)
    if not isinstance(row, tuple) or len(row) != 4:
        raise ValueError("runtime lease row is invalid")
    instance_id, lease_epoch, heartbeat_at, expires_at = row
    heartbeat = _datetime(heartbeat_at, "heartbeat_at")
    expires = _datetime(expires_at, "expires_at")
    return RuntimeLeaseSnapshot(
        active=expires > observed_at.astimezone(UTC),
        instance_id=str(instance_id),
        lease_epoch=_database_integer(
            lease_epoch,
            "lease_epoch",
            positive=True,
        ),
        heartbeat_at=heartbeat,
        expires_at=expires,
    )


def _read_request(
    conn: sqlite3.Connection,
    request_id: str,
) -> BackendControlRequest | None:
    row = conn.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM backend_control_requests WHERE request_id=?",
        (request_id,),
    ).fetchone()
    return None if row is None else _request_from_row(row)


def _read_binding(
    conn: sqlite3.Connection,
    backend_type: BackendType,
) -> BackendProcessBinding | None:
    row = conn.execute(
        f"SELECT {_BINDING_COLUMNS} FROM backend_process_bindings WHERE backend_type=?",
        (backend_type.value,),
    ).fetchone()
    return None if row is None else _binding_from_row(row)


def _read_runtime_lease(
    conn: sqlite3.Connection,
    lease_name: str,
    observed_at: datetime,
) -> RuntimeLeaseSnapshot:
    row = conn.execute(
        """
        SELECT instance_id, lease_epoch, heartbeat_at, expires_at
        FROM gateway_runtime_lease
        WHERE lease_name=?
        """,
        (lease_name,),
    ).fetchone()
    return _runtime_lease_from_row(row, observed_at)


def _require_fence(
    conn: sqlite3.Connection,
    fence: SupervisorFence,
    observed_at: datetime,
) -> None:
    row = conn.execute(
        """
        SELECT 1 FROM gateway_runtime_lease
        WHERE lease_name=? AND instance_id=? AND lease_epoch=? AND expires_at>?
        """,
        (
            SUPERVISOR_RUNTIME_LEASE_NAME,
            fence.instance_id,
            fence.lease_epoch,
            _timestamp(observed_at, "observed_at"),
        ),
    ).fetchone()
    if row is None:
        raise BackendControlPersistenceError("supervisor_unavailable")


class SQLiteBackendControlRepository:
    """短事务提交请求，并用 Supervisor lease fence 串行执行状态机。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = _db_path(db_path)

    def supervisor_online(self, *, observed_at: datetime) -> bool:
        try:
            with readonly_connection(self._db_path) as conn:
                return _read_runtime_lease(
                    conn,
                    SUPERVISOR_RUNTIME_LEASE_NAME,
                    observed_at,
                ).active
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def create_or_get_request(
        self,
        *,
        request_id: str,
        backend_type: BackendType,
        action: BackendControlAction,
        actor_security_id: str,
        idempotency_key_digest: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> BackendControlCreation:
        if backend_type is not BackendType.GATEWAY:
            raise ValueError("backend type is unsupported")
        if not isinstance(action, BackendControlAction):
            raise TypeError("backend action is invalid")
        validate_security_digest(actor_security_id, "actor_security_id")
        validate_security_digest(idempotency_key_digest, "idempotency_key_digest")
        validate_security_digest(request_fingerprint, "request_fingerprint")
        timestamp = _timestamp(created_at, "created_at")
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    existing = conn.execute(
                        """
                        SELECT request_id, backend_type, action, request_fingerprint
                        FROM backend_control_requests
                        WHERE actor_security_id=? AND idempotency_key_digest=?
                        """,
                        (actor_security_id, idempotency_key_digest),
                    ).fetchone()
                    if existing is not None:
                        if (
                            str(existing[1]) != backend_type.value
                            or str(existing[2]) != action.value
                            or str(existing[3]) != request_fingerprint
                        ):
                            raise BackendControlConflict("idempotency_conflict")
                        request = _read_request(conn, str(existing[0]))
                        if request is None:
                            raise ValueError("idempotent request is unavailable")
                        return BackendControlCreation(request=request, created=False)

                    lease = _read_runtime_lease(
                        conn,
                        SUPERVISOR_RUNTIME_LEASE_NAME,
                        created_at,
                    )
                    if not lease.active:
                        raise BackendControlUnavailable("supervisor_unavailable")
                    conn.execute(
                        """
                        INSERT INTO backend_control_requests (
                            request_id, backend_type, action, status,
                            actor_security_id, idempotency_key_digest,
                            request_fingerprint, created_at
                        ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            backend_type.value,
                            action.value,
                            actor_security_id,
                            idempotency_key_digest,
                            request_fingerprint,
                            timestamp,
                        ),
                    )
                    request = _read_request(conn, request_id)
                    if request is None:
                        raise ValueError("created request is unavailable")
                    return BackendControlCreation(request=request, created=True)
        except (BackendControlConflict, BackendControlUnavailable):
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def get_request(self, request_id: str) -> BackendControlRequest | None:
        try:
            with readonly_connection(self._db_path) as conn:
                return _read_request(conn, request_id)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def list_inflight_requests(self) -> tuple[BackendControlRequest, ...]:
        try:
            with readonly_connection(self._db_path) as conn:
                rows = conn.execute(
                    f"""
                    SELECT {_REQUEST_COLUMNS}
                    FROM backend_control_requests
                    WHERE status IN ('claimed', 'executing')
                    ORDER BY created_at, request_id
                    """
                ).fetchall()
            return tuple(_request_from_row(row) for row in rows)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def adopt_request(
        self,
        request_id: str,
        fence: SupervisorFence,
        *,
        claimed_at: datetime,
    ) -> BackendControlRequest | None:
        timestamp = _timestamp(claimed_at, "claimed_at")
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, claimed_at)
                    cursor = conn.execute(
                        """
                        UPDATE backend_control_requests
                        SET status='claimed', claimed_by_supervisor=?, claimed_at=?
                        WHERE request_id=? AND status IN ('claimed', 'executing')
                        """,
                        (fence.instance_id, timestamp, request_id),
                    )
                    if cursor.rowcount != 1:
                        return None
                    return _read_request(conn, request_id)
        except BackendControlUnavailable:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def claim_next_request(
        self,
        fence: SupervisorFence,
        *,
        claimed_at: datetime,
    ) -> BackendControlRequest | None:
        timestamp = _timestamp(claimed_at, "claimed_at")
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, claimed_at)
                    active = conn.execute(
                        """
                        SELECT 1 FROM backend_control_requests
                        WHERE backend_type='gateway'
                          AND status IN ('claimed', 'executing')
                        LIMIT 1
                        """
                    ).fetchone()
                    if active is not None:
                        return None
                    row = conn.execute(
                        """
                        SELECT request_id FROM backend_control_requests
                        WHERE backend_type='gateway' AND status='pending'
                        ORDER BY created_at, request_id
                        LIMIT 1
                        """
                    ).fetchone()
                    if row is None:
                        return None
                    request_id = str(row[0])
                    cursor = conn.execute(
                        """
                        UPDATE backend_control_requests
                        SET status='claimed', claimed_by_supervisor=?, claimed_at=?
                        WHERE request_id=? AND status='pending'
                        """,
                        (fence.instance_id, timestamp, request_id),
                    )
                    if cursor.rowcount != 1:
                        return None
                    return _read_request(conn, request_id)
        except BackendControlUnavailable:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def mark_request_executing(
        self,
        request_id: str,
        fence: SupervisorFence,
        *,
        started_at: datetime,
    ) -> BackendControlRequest:
        timestamp = _timestamp(started_at, "started_at")
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, started_at)
                    cursor = conn.execute(
                        """
                        UPDATE backend_control_requests
                        SET status='executing', started_at=COALESCE(started_at, ?)
                        WHERE request_id=? AND status='claimed'
                          AND claimed_by_supervisor=?
                        """,
                        (timestamp, request_id, fence.instance_id),
                    )
                    if cursor.rowcount != 1:
                        raise BackendControlConflict()
                    request = _read_request(conn, request_id)
                    if request is None:
                        raise ValueError("executing request is unavailable")
                    return request
        except (BackendControlConflict, BackendControlUnavailable):
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def update_request_stage(
        self,
        request_id: str,
        fence: SupervisorFence,
        stage: BackendControlStage,
    ) -> None:
        if not isinstance(stage, BackendControlStage):
            raise TypeError("backend control stage is invalid")
        now = datetime.now(UTC)
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, now)
                    cursor = conn.execute(
                        """
                        UPDATE backend_control_requests
                        SET execution_stage=?
                        WHERE request_id=? AND status='executing'
                          AND claimed_by_supervisor=?
                        """,
                        (stage.value, request_id, fence.instance_id),
                    )
                    if cursor.rowcount != 1:
                        raise BackendControlConflict()
        except (BackendControlConflict, BackendControlUnavailable):
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def complete_request(
        self,
        request_id: str,
        fence: SupervisorFence,
        result: BackendControlResult,
        *,
        completed_at: datetime,
    ) -> BackendControlRequest:
        if not isinstance(result, BackendControlResult):
            raise TypeError("backend control result is invalid")
        timestamp = _timestamp(completed_at, "completed_at")
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, completed_at)
                    cursor = conn.execute(
                        """
                        UPDATE backend_control_requests
                        SET status=?, started_at=COALESCE(started_at, ?),
                            completed_at=?, result_code=?, result_reference=?,
                            exception_type=?, forced_termination=CASE
                                WHEN forced_termination=1 OR ?=1 THEN 1
                                ELSE 0
                            END, execution_stage=NULL
                        WHERE request_id=? AND status IN ('claimed', 'executing')
                          AND claimed_by_supervisor=?
                        """,
                        (
                            result.status.value,
                            timestamp,
                            timestamp,
                            result.result_code.value,
                            result.result_reference,
                            result.exception_type,
                            int(result.forced_termination),
                            request_id,
                            fence.instance_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise BackendControlConflict()
                    request = _read_request(conn, request_id)
                    if request is None:
                        raise ValueError("completed request is unavailable")
                    return request
        except (BackendControlConflict, BackendControlUnavailable):
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def mark_forced_termination(
        self,
        request_id: str,
        fence: SupervisorFence,
    ) -> None:
        now = datetime.now(UTC)
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, now)
                    cursor = conn.execute(
                        """
                        UPDATE backend_control_requests
                        SET forced_termination=1
                        WHERE request_id=? AND status='executing'
                          AND claimed_by_supervisor=?
                        """,
                        (request_id, fence.instance_id),
                    )
                    if cursor.rowcount != 1:
                        raise BackendControlConflict()
        except (BackendControlConflict, BackendControlUnavailable):
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def read_runtime_lease(
        self,
        lease_name: str,
        *,
        observed_at: datetime,
    ) -> RuntimeLeaseSnapshot:
        if lease_name not in {
            SUPERVISOR_RUNTIME_LEASE_NAME,
            GATEWAY_RUNTIME_LEASE_NAME,
        }:
            raise ValueError("runtime lease name is unsupported")
        try:
            with readonly_connection(self._db_path) as conn:
                return _read_runtime_lease(conn, lease_name, observed_at)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def get_process_binding(
        self,
        backend_type: BackendType,
    ) -> BackendProcessBinding | None:
        if backend_type is not BackendType.GATEWAY:
            raise ValueError("backend type is unsupported")
        try:
            with readonly_connection(self._db_path) as conn:
                return _read_binding(conn, backend_type)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def put_process_binding(
        self,
        binding: BackendProcessBinding,
        fence: SupervisorFence,
    ) -> None:
        if not isinstance(binding, BackendProcessBinding):
            raise TypeError("backend process binding is invalid")
        observed_at = binding.updated_at or datetime.now(UTC)
        values = (
            binding.backend_type.value,
            binding.supervisor_instance_id,
            binding.launch_id,
            binding.pid,
            binding.process_identity_token,
            (
                None
                if binding.identity_verified is None
                else int(binding.identity_verified)
            ),
            binding.observed_state.value,
            (
                None
                if binding.started_at is None
                else _timestamp(binding.started_at, "started_at")
            ),
            binding.config_revision_at_launch,
            (
                None
                if binding.last_exit_at is None
                else _timestamp(binding.last_exit_at, "last_exit_at")
            ),
            binding.last_exit_code,
            binding.last_request_id,
            _timestamp(observed_at, "updated_at"),
        )
        try:
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    _require_fence(conn, fence, observed_at)
                    conn.execute(
                        """
                        INSERT INTO backend_process_bindings (
                            backend_type, supervisor_instance_id, launch_id, pid,
                            process_identity_token, identity_verified,
                            observed_state, started_at, config_revision_at_launch,
                            last_exit_at, last_exit_code, last_request_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(backend_type) DO UPDATE SET
                            supervisor_instance_id=excluded.supervisor_instance_id,
                            launch_id=excluded.launch_id,
                            pid=excluded.pid,
                            process_identity_token=excluded.process_identity_token,
                            identity_verified=excluded.identity_verified,
                            observed_state=excluded.observed_state,
                            started_at=excluded.started_at,
                            config_revision_at_launch=excluded.config_revision_at_launch,
                            last_exit_at=excluded.last_exit_at,
                            last_exit_code=excluded.last_exit_code,
                            last_request_id=excluded.last_request_id,
                            updated_at=excluded.updated_at
                        """,
                        values,
                    )
        except BackendControlUnavailable:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc


class SQLiteBackendStatusReadRepository:
    """只读组合 lease、绑定和最新请求，不访问操作系统进程。"""

    __slots__ = ("_db_path",)

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = _db_path(db_path)

    def get_request(self, request_id: str) -> BackendControlRequest | None:
        try:
            with readonly_connection(self._db_path) as conn:
                return _read_request(conn, request_id)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc

    def read_status(
        self,
        *,
        current_config_revision: str | None,
        observed_at: datetime,
    ) -> BackendStatusSnapshot:
        try:
            with readonly_connection(self._db_path) as conn:
                supervisor_lease = _read_runtime_lease(
                    conn,
                    SUPERVISOR_RUNTIME_LEASE_NAME,
                    observed_at,
                )
                gateway_lease = _read_runtime_lease(
                    conn,
                    GATEWAY_RUNTIME_LEASE_NAME,
                    observed_at,
                )
                binding = _read_binding(conn, BackendType.GATEWAY)
                latest_row = conn.execute(
                    f"""
                    SELECT {_REQUEST_COLUMNS}
                    FROM backend_control_requests
                    WHERE backend_type='gateway'
                    ORDER BY created_at DESC, request_id DESC
                    LIMIT 1
                    """
                ).fetchone()
            latest = None if latest_row is None else _request_from_row(latest_row)
            return _status_snapshot(
                observed_at=observed_at,
                supervisor_lease=supervisor_lease,
                gateway_lease=gateway_lease,
                binding=binding,
                current_config_revision=current_config_revision,
                latest_request=latest,
            )
        except BackendControlUnavailable:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise BackendControlPersistenceError() from exc


def _status_snapshot(
    *,
    observed_at: datetime,
    supervisor_lease: RuntimeLeaseSnapshot,
    gateway_lease: RuntimeLeaseSnapshot,
    binding: BackendProcessBinding | None,
    current_config_revision: str | None,
    latest_request: BackendControlRequest | None,
) -> BackendStatusSnapshot:
    supervisor = BackendSupervisorStatus(
        online=supervisor_lease.active,
        lease_expires_at=supervisor_lease.expires_at,
        instance_state=(
            SupervisorInstanceState.ONLINE
            if supervisor_lease.active
            else SupervisorInstanceState.OFFLINE
        ),
    )
    has_process = binding is not None and binding.pid is not None
    current_owner = bool(
        has_process
        and binding is not None
        and binding.identity_verified
        and supervisor_lease.active
        and binding.supervisor_instance_id == supervisor_lease.instance_id
    )
    if gateway_lease.active:
        observed_state = BackendObservedState.RUNNING
        if not has_process:
            ownership = BackendOwnershipState.UNMANAGED
        elif current_owner:
            ownership = BackendOwnershipState.MANAGED
        else:
            ownership = BackendOwnershipState.UNCERTAIN
    elif has_process and binding is not None:
        observed_state = (
            binding.observed_state
            if binding.observed_state in {
                BackendObservedState.STARTING,
                BackendObservedState.STOPPING,
            }
            else BackendObservedState.UNKNOWN
        )
        ownership = (
            BackendOwnershipState.MANAGED
            if current_owner
            else BackendOwnershipState.UNCERTAIN
        )
    else:
        observed_state = BackendObservedState.STOPPED
        ownership = BackendOwnershipState.NONE

    revision_changed: bool | None = None
    if (
        has_process
        and binding is not None
        and binding.config_revision_at_launch is not None
        and current_config_revision is not None
    ):
        revision_changed = (
            binding.config_revision_at_launch != current_config_revision
        )
    gateway = BackendGatewayStatus(
        observed_state=observed_state,
        ownership=ownership,
        lease_active=gateway_lease.active,
        managed=ownership is BackendOwnershipState.MANAGED,
        started_at=None if binding is None else binding.started_at,
        last_exit_at=None if binding is None else binding.last_exit_at,
        last_exit_code=None if binding is None else binding.last_exit_code,
        config_changed_since_start=revision_changed,
        restart_recommended=revision_changed,
    )
    return BackendStatusSnapshot(
        observed_at=observed_at,
        supervisor=supervisor,
        gateway=gateway,
        latest_request=latest_request,
    )


__all__ = [
    "BackendControlPersistenceError",
    "SQLiteBackendControlRepository",
    "SQLiteBackendStatusReadRepository",
]
