"""多 Agent 任务编排 Store Protocol 的 SQLite 原子实现。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from hermes.orchestration.errors import (
    InvalidTaskTransitionError,
    OrchestrationConflictError,
    OrchestrationError,
    OrchestrationNotFoundError,
    OrchestrationPersistenceError,
    OrchestrationValidationError,
    TaskClaimLostError,
)
from hermes.orchestration.models import (
    TaskClaim,
    TaskCreateSpec,
    TaskRecord,
    TaskRunRecord,
    TaskRunStatus,
    TaskStatus,
    WorkflowCreateSpec,
    WorkflowRecord,
    WorkflowStatus,
    plain_json_object,
)
from hermes.orchestration.store import OrchestrationStore

from .database import _immediate_transaction
from .read_only import readonly_connection
from .write_existing import existing_write_connection


_WORKFLOW_COLUMNS = (
    "workflow_id, title, goal, status, created_by_session, "
    "created_at, updated_at, finished_at"
)
_TASK_COLUMNS = (
    "task_id, workflow_id, task_key, title, prompt, role, status, "
    "priority, max_attempts, attempt_count, workdir, input_metadata_json, "
    "claim_owner, claim_token, claim_expires_at, result_summary, "
    "result_metadata_json, error_type, error_message, blocked_reason, "
    "created_at, ready_at, started_at, finished_at, updated_at"
)
_RUN_COLUMNS = (
    "run_id, workflow_id, task_id, attempt_number, worker_id, claim_token, "
    "status, session_key, claimed_at, started_at, heartbeat_at, finished_at, "
    "result_summary, result_metadata_json, error_type, error_message"
)
_ACTIVE_RUN_STATUSES = (
    TaskRunStatus.CLAIMED.value,
    TaskRunStatus.RUNNING.value,
)


@contextmanager
def _database_operation(operation: str) -> Iterator[None]:
    """把 SQLite 基础异常收敛为不泄露数据的稳定领域错误。"""

    try:
        yield
    except OrchestrationError:
        raise
    except sqlite3.Error as exc:
        raise OrchestrationPersistenceError(
            f"orchestration {operation} failed"
        ) from exc


def _argument_text(
    value: object,
    field_name: str,
    *,
    max_length: int = 256,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise OrchestrationValidationError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise OrchestrationValidationError(
            f"{field_name} must be a non-empty string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OrchestrationValidationError(
            f"{field_name} must contain valid Unicode"
        ) from exc
    if len(value) > max_length:
        raise OrchestrationValidationError(
            f"{field_name} exceeds its length limit"
        )
    return value


def _positive_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrchestrationValidationError(
            f"{field_name} must be a finite positive number"
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise OrchestrationValidationError(
            f"{field_name} must be a finite positive number"
        )
    return normalized


def _positive_int(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise OrchestrationValidationError(
            f"{field_name} must be a positive integer within its limit"
        )
    return value


def _encode_json_object(
    value: Mapping[str, object],
    field_name: str,
) -> str:
    plain = plain_json_object(value, field_name=field_name)
    try:
        return json.dumps(
            plain,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise OrchestrationValidationError(
            f"{field_name} must be JSON-compatible"
        ) from exc


def _encode_optional_json_object(
    value: Mapping[str, object] | None,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _encode_json_object(value, field_name)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _decode_json_object(
    value: object,
    field_name: str,
    *,
    nullable: bool,
) -> dict[str, object] | None:
    if value is None:
        if nullable:
            return None
        raise OrchestrationPersistenceError(
            f"stored {field_name} is missing"
        )
    if not isinstance(value, str):
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        )
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        ) from exc
    if not isinstance(decoded, dict):
        raise OrchestrationPersistenceError(
            f"stored {field_name} is not an object"
        )
    try:
        return plain_json_object(decoded, field_name=field_name)
    except OrchestrationValidationError as exc:
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        ) from exc


def _stored_text(
    value: object,
    field_name: str,
    *,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        )
    return value


def _stored_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        )
    return value


def _stored_timestamp(
    value: object,
    field_name: str,
    *,
    nullable: bool = False,
) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise OrchestrationPersistenceError(
            f"stored {field_name} is invalid"
        )
    return normalized


def _workflow_from_row(row: object) -> WorkflowRecord:
    if not isinstance(row, tuple) or len(row) != 8:
        raise OrchestrationPersistenceError("stored workflow row is invalid")
    try:
        return WorkflowRecord(
            workflow_id=_stored_text(row[0], "workflow_id"),
            title=_stored_text(row[1], "workflow title"),
            goal=_stored_text(row[2], "workflow goal"),
            status=WorkflowStatus(_stored_text(row[3], "workflow status")),
            created_by_session=_stored_text(
                row[4],
                "created_by_session",
                nullable=True,
            ),
            created_at=_stored_timestamp(row[5], "workflow created_at"),
            updated_at=_stored_timestamp(row[6], "workflow updated_at"),
            finished_at=_stored_timestamp(
                row[7],
                "workflow finished_at",
                nullable=True,
            ),
        )
    except (TypeError, ValueError, OrchestrationValidationError) as exc:
        raise OrchestrationPersistenceError(
            "stored workflow row is invalid"
        ) from exc


def _task_from_row(row: object) -> TaskRecord:
    if not isinstance(row, tuple) or len(row) != 25:
        raise OrchestrationPersistenceError("stored task row is invalid")
    try:
        return TaskRecord(
            task_id=_stored_text(row[0], "task_id"),
            workflow_id=_stored_text(row[1], "workflow_id"),
            task_key=_stored_text(row[2], "task_key"),
            title=_stored_text(row[3], "task title"),
            prompt=_stored_text(row[4], "task prompt"),
            role=_stored_text(row[5], "task role"),
            status=TaskStatus(_stored_text(row[6], "task status")),
            priority=_stored_int(row[7], "task priority"),
            max_attempts=_stored_int(row[8], "task max_attempts"),
            attempt_count=_stored_int(row[9], "task attempt_count"),
            workdir=_stored_text(row[10], "task workdir", nullable=True),
            input_metadata=_decode_json_object(
                row[11],
                "input_metadata",
                nullable=False,
            ),
            claim_owner=_stored_text(
                row[12],
                "claim_owner",
                nullable=True,
            ),
            claim_token=_stored_text(
                row[13],
                "claim_token",
                nullable=True,
            ),
            claim_expires_at=_stored_timestamp(
                row[14],
                "claim_expires_at",
                nullable=True,
            ),
            result_summary=_stored_text(
                row[15],
                "result_summary",
                nullable=True,
            ),
            result_metadata=_decode_json_object(
                row[16],
                "result_metadata",
                nullable=True,
            ),
            error_type=_stored_text(row[17], "error_type", nullable=True),
            error_message=_stored_text(
                row[18],
                "error_message",
                nullable=True,
            ),
            blocked_reason=_stored_text(
                row[19],
                "blocked_reason",
                nullable=True,
            ),
            created_at=_stored_timestamp(row[20], "task created_at"),
            ready_at=_stored_timestamp(
                row[21],
                "task ready_at",
                nullable=True,
            ),
            started_at=_stored_timestamp(
                row[22],
                "task started_at",
                nullable=True,
            ),
            finished_at=_stored_timestamp(
                row[23],
                "task finished_at",
                nullable=True,
            ),
            updated_at=_stored_timestamp(row[24], "task updated_at"),
        )
    except (TypeError, ValueError, OrchestrationValidationError) as exc:
        raise OrchestrationPersistenceError(
            "stored task row is invalid"
        ) from exc


def _run_from_row(row: object) -> TaskRunRecord:
    if not isinstance(row, tuple) or len(row) != 16:
        raise OrchestrationPersistenceError("stored task run row is invalid")
    try:
        return TaskRunRecord(
            run_id=_stored_text(row[0], "run_id"),
            workflow_id=_stored_text(row[1], "workflow_id"),
            task_id=_stored_text(row[2], "task_id"),
            attempt_number=_stored_int(row[3], "attempt_number"),
            worker_id=_stored_text(row[4], "worker_id"),
            claim_token=_stored_text(row[5], "claim_token"),
            status=TaskRunStatus(_stored_text(row[6], "run status")),
            session_key=_stored_text(
                row[7],
                "session_key",
                nullable=True,
            ),
            claimed_at=_stored_timestamp(row[8], "run claimed_at"),
            started_at=_stored_timestamp(
                row[9],
                "run started_at",
                nullable=True,
            ),
            heartbeat_at=_stored_timestamp(row[10], "run heartbeat_at"),
            finished_at=_stored_timestamp(
                row[11],
                "run finished_at",
                nullable=True,
            ),
            result_summary=_stored_text(
                row[12],
                "run result_summary",
                nullable=True,
            ),
            result_metadata=_decode_json_object(
                row[13],
                "run result_metadata",
                nullable=True,
            ),
            error_type=_stored_text(
                row[14],
                "run error_type",
                nullable=True,
            ),
            error_message=_stored_text(
                row[15],
                "run error_message",
                nullable=True,
            ),
        )
    except (TypeError, ValueError, OrchestrationValidationError) as exc:
        raise OrchestrationPersistenceError(
            "stored task run row is invalid"
        ) from exc


def _read_workflow(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> WorkflowRecord | None:
    row = conn.execute(
        f"SELECT {_WORKFLOW_COLUMNS} FROM orchestration_workflows "
        "WHERE workflow_id=?",
        (workflow_id,),
    ).fetchone()
    return None if row is None else _workflow_from_row(row)


def _read_task(
    conn: sqlite3.Connection,
    task_id: str,
) -> TaskRecord | None:
    row = conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM orchestration_tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    return None if row is None else _task_from_row(row)


def _read_run_by_claim(
    conn: sqlite3.Connection,
    task_id: str,
    claim_token: str,
) -> TaskRunRecord | None:
    row = conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM orchestration_task_runs "
        "WHERE task_id=? AND claim_token=?",
        (task_id, claim_token),
    ).fetchone()
    return None if row is None else _run_from_row(row)


def _require_workflow(
    conn: sqlite3.Connection,
    workflow_id: str,
) -> WorkflowRecord:
    workflow = _read_workflow(conn, workflow_id)
    if workflow is None:
        raise OrchestrationNotFoundError("workflow was not found")
    return workflow


def _require_task(
    conn: sqlite3.Connection,
    task_id: str,
) -> TaskRecord:
    task = _read_task(conn, task_id)
    if task is None:
        raise OrchestrationNotFoundError("task was not found")
    return task


def _require_claim(
    conn: sqlite3.Connection,
    task_id: str,
    claim_token: str,
) -> tuple[TaskRecord, TaskRunRecord]:
    """在调用方写事务内验证 Task 与 Run 的同一 fencing token。"""

    task = _read_task(conn, task_id)
    if (
        task is None
        or task.status is not TaskStatus.RUNNING
        or task.claim_token != claim_token
    ):
        raise TaskClaimLostError("task claim is no longer current")
    run = _read_run_by_claim(conn, task_id, claim_token)
    if (
        run is None
        or run.claim_token != claim_token
        or run.status not in {
            TaskRunStatus.CLAIMED,
            TaskRunStatus.RUNNING,
        }
    ):
        raise TaskClaimLostError("task run claim is no longer current")
    if run.workflow_id != task.workflow_id:
        raise OrchestrationPersistenceError(
            "stored task claim relationship is invalid"
        )
    return task, run


def _claim_record(
    *,
    workflow: WorkflowRecord,
    task: TaskRecord,
    run: TaskRunRecord,
    claim_token: str,
    claim_expires_at: float,
) -> TaskClaim:
    """把跨表一致性异常转换为持久化损坏错误。"""

    try:
        return TaskClaim(
            workflow=workflow,
            task=task,
            run=run,
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
        )
    except OrchestrationValidationError as exc:
        raise OrchestrationPersistenceError(
            "stored task claim relationship is invalid"
        ) from exc


def _advance_direct_downstream(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    completed_task_id: str,
    now: float,
) -> None:
    """显式推进所有父依赖已完成的直接下游 todo Task。"""

    conn.execute(
        """
        UPDATE orchestration_tasks AS child
        SET status=?, ready_at=?, updated_at=?
        WHERE child.workflow_id=?
          AND child.status=?
          AND child.task_id IN (
              SELECT dependency.task_id
              FROM orchestration_task_dependencies AS dependency
              WHERE dependency.workflow_id=?
                AND dependency.depends_on_task_id=?
          )
          AND NOT EXISTS (
              SELECT 1
              FROM orchestration_task_dependencies AS required
              JOIN orchestration_tasks AS parent
                ON parent.task_id=required.depends_on_task_id
               AND parent.workflow_id=required.workflow_id
              WHERE required.task_id=child.task_id
                AND required.workflow_id=child.workflow_id
                AND parent.status!=?
          )
        """,
        (
            TaskStatus.READY.value,
            now,
            now,
            workflow_id,
            TaskStatus.TODO.value,
            workflow_id,
            completed_task_id,
            TaskStatus.COMPLETED.value,
        ),
    )


def _finalize_active_workflow_if_terminal(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    now: float,
) -> None:
    """仅在全部 Task 终结时归并 active Workflow 的最终状态。"""

    rows = conn.execute(
        """
        SELECT status, COUNT(*)
        FROM orchestration_tasks
        WHERE workflow_id=?
        GROUP BY status
        """,
        (workflow_id,),
    ).fetchall()
    counts: dict[str, int] = {}
    try:
        for status, count in rows:
            normalized_status = TaskStatus(str(status))
            counts[normalized_status.value] = int(count)
    except (TypeError, ValueError) as exc:
        raise OrchestrationPersistenceError(
            "stored workflow task statuses are invalid"
        ) from exc
    nonterminal_count = sum(
        counts.get(status.value, 0)
        for status in (
            TaskStatus.TODO,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
        )
    )
    if nonterminal_count:
        return
    if counts.get(TaskStatus.FAILED.value, 0):
        target = WorkflowStatus.FAILED
    elif counts.get(TaskStatus.CANCELLED.value, 0):
        target = WorkflowStatus.CANCELLED
    else:
        target = WorkflowStatus.COMPLETED
    conn.execute(
        """
        UPDATE orchestration_workflows
        SET status=?, updated_at=?, finished_at=?
        WHERE workflow_id=? AND status=?
        """,
        (
            target.value,
            now,
            now,
            workflow_id,
            WorkflowStatus.ACTIVE.value,
        ),
    )


def _fail_active_workflow(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    now: float,
) -> None:
    """终态失败 Workflow，并取消尚未开始的其他 Task。"""

    cursor = conn.execute(
        """
        UPDATE orchestration_workflows
        SET status=?, updated_at=?, finished_at=?
        WHERE workflow_id=? AND status=?
        """,
        (
            WorkflowStatus.FAILED.value,
            now,
            now,
            workflow_id,
            WorkflowStatus.ACTIVE.value,
        ),
    )
    if cursor.rowcount != 1:
        raise OrchestrationPersistenceError(
            "active workflow could not enter failed status"
        )
    conn.execute(
        """
        UPDATE orchestration_tasks
        SET status=?, claim_owner=NULL, claim_token=NULL,
            claim_expires_at=NULL, blocked_reason=NULL,
            error_type=?, error_message=?, finished_at=?, updated_at=?
        WHERE workflow_id=? AND status IN (?, ?, ?)
        """,
        (
            TaskStatus.CANCELLED.value,
            "dependency_or_workflow_failed",
            "workflow failed before this task could run",
            now,
            now,
            workflow_id,
            TaskStatus.TODO.value,
            TaskStatus.READY.value,
            TaskStatus.BLOCKED.value,
        ),
    )


class SQLiteOrchestrationStore(OrchestrationStore):
    """每个操作使用独立连接，并把 SQLite 作为唯一任务事实来源。"""

    __slots__ = (
        "_claim_token_factory",
        "_clock",
        "_db_path",
        "_id_factory",
    )

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], object] = uuid.uuid4,
        claim_token_factory: Callable[[], object] = uuid.uuid4,
    ) -> None:
        if not isinstance(db_path, (str, Path)) or not str(db_path).strip():
            raise TypeError("db_path must be a non-empty path")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(id_factory):
            raise TypeError("id_factory must be callable")
        if not callable(claim_token_factory):
            raise TypeError("claim_token_factory must be callable")
        self._db_path = str(db_path)
        self._clock = clock
        self._id_factory = id_factory
        self._claim_token_factory = claim_token_factory

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise OrchestrationPersistenceError(
                "orchestration clock failed"
            ) from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OrchestrationPersistenceError(
                "orchestration clock returned an invalid timestamp"
            )
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise OrchestrationPersistenceError(
                "orchestration clock returned an invalid timestamp"
            )
        return normalized

    @staticmethod
    def _factory_component(
        factory: Callable[[], object],
        field_name: str,
    ) -> str:
        try:
            value = factory()
        except Exception as exc:
            raise OrchestrationPersistenceError(
                f"{field_name} factory failed"
            ) from exc
        if isinstance(value, uuid.UUID):
            component = value.hex
        elif isinstance(value, str):
            component = value.strip()
        else:
            raise OrchestrationPersistenceError(
                f"{field_name} factory returned an invalid value"
            )
        try:
            component.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OrchestrationPersistenceError(
                f"{field_name} factory returned an invalid value"
            ) from exc
        if not component or len(component) > 120 or "\x00" in component:
            raise OrchestrationPersistenceError(
                f"{field_name} factory returned an invalid value"
            )
        return component

    def _new_id(self, prefix: str) -> str:
        identifier = (
            f"{prefix}_"
            f"{self._factory_component(self._id_factory, prefix + '_id')}"
        )
        if len(identifier) > 128:
            raise OrchestrationPersistenceError(
                f"{prefix}_id factory returned an invalid value"
            )
        return identifier

    def _new_claim_token(self, *, run_id: str, worker_id: str) -> str:
        token = self._factory_component(
            self._claim_token_factory,
            "claim_token",
        )
        if len(token) > 256 or token in {run_id, worker_id}:
            raise OrchestrationPersistenceError(
                "claim_token factory returned an unsafe value"
            )
        return token

    @staticmethod
    def _lease_expiry(now: float, lease_seconds: float) -> float:
        normalized = _positive_number(lease_seconds, "lease_seconds")
        if normalized > 86_400:
            raise OrchestrationValidationError(
                "lease_seconds exceeds its limit"
            )
        expires_at = now + normalized
        if not math.isfinite(expires_at) or expires_at <= now:
            raise OrchestrationValidationError(
                "lease_seconds does not produce a valid future timestamp"
            )
        return expires_at

    def create_workflow(self, spec: WorkflowCreateSpec) -> WorkflowRecord:
        if not isinstance(spec, WorkflowCreateSpec):
            raise OrchestrationValidationError(
                "spec must be a WorkflowCreateSpec"
            )
        if not spec.tasks or any(
            not isinstance(task, TaskCreateSpec) for task in spec.tasks
        ):
            raise OrchestrationValidationError(
                "workflow must contain TaskCreateSpec values"
            )

        workflow_id = self._new_id("wf")
        task_ids: dict[str, str] = {}
        encoded_metadata: dict[str, str] = {}
        for task in spec.tasks:
            if task.key in task_ids:
                raise OrchestrationValidationError(
                    "task keys must be unique within a workflow"
                )
            task_ids[task.key] = self._new_id("task")
            encoded_metadata[task.key] = _encode_json_object(
                task.input_metadata,
                "input_metadata",
            )

        with _database_operation("workflow creation"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    try:
                        conn.execute(
                            """
                            INSERT INTO orchestration_workflows (
                                workflow_id, title, goal, status,
                                created_by_session, created_at,
                                updated_at, finished_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                            """,
                            (
                                workflow_id,
                                spec.title,
                                spec.goal,
                                WorkflowStatus.ACTIVE.value,
                                spec.created_by_session,
                                now,
                                now,
                            ),
                        )
                        for task in spec.tasks:
                            has_dependencies = bool(task.depends_on)
                            conn.execute(
                                """
                                INSERT INTO orchestration_tasks (
                                    task_id, workflow_id, task_key, title,
                                    prompt, role, status, priority,
                                    max_attempts, attempt_count, workdir,
                                    input_metadata_json, claim_owner,
                                    claim_token, claim_expires_at,
                                    result_summary, result_metadata_json,
                                    error_type, error_message, blocked_reason,
                                    created_at, ready_at, started_at,
                                    finished_at, updated_at
                                ) VALUES (
                                    ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?,
                                    NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                                    NULL, ?, ?, NULL, NULL, ?
                                )
                                """,
                                (
                                    task_ids[task.key],
                                    workflow_id,
                                    task.key,
                                    task.title,
                                    task.prompt,
                                    task.role,
                                    (
                                        TaskStatus.TODO.value
                                        if has_dependencies
                                        else TaskStatus.READY.value
                                    ),
                                    task.priority,
                                    task.max_attempts,
                                    task.workdir,
                                    encoded_metadata[task.key],
                                    now,
                                    None if has_dependencies else now,
                                    now,
                                ),
                            )
                        for task in spec.tasks:
                            seen_dependencies: set[str] = set()
                            for dependency_key in task.depends_on:
                                if dependency_key in seen_dependencies:
                                    raise OrchestrationValidationError(
                                        "task dependencies contain a duplicate edge"
                                    )
                                seen_dependencies.add(dependency_key)
                                dependency_id = task_ids.get(dependency_key)
                                if dependency_id is None:
                                    raise OrchestrationValidationError(
                                        "task dependency references an unknown key"
                                    )
                                conn.execute(
                                    """
                                    INSERT INTO orchestration_task_dependencies (
                                        workflow_id, task_id,
                                        depends_on_task_id
                                    ) VALUES (?, ?, ?)
                                    """,
                                    (
                                        workflow_id,
                                        task_ids[task.key],
                                        dependency_id,
                                    ),
                                )
                    except sqlite3.IntegrityError as exc:
                        raise OrchestrationConflictError(
                            "workflow creation conflicted with stored data"
                        ) from exc
                    workflow = _require_workflow(conn, workflow_id)
        return workflow

    def get_workflow(self, workflow_id: str) -> WorkflowRecord | None:
        normalized = _argument_text(
            workflow_id,
            "workflow_id",
            max_length=128,
        )
        with _database_operation("workflow read"):
            with readonly_connection(self._db_path) as conn:
                return _read_workflow(conn, normalized)

    def get_task(self, task_id: str) -> TaskRecord | None:
        normalized = _argument_text(task_id, "task_id", max_length=128)
        with _database_operation("task read"):
            with readonly_connection(self._db_path) as conn:
                return _read_task(conn, normalized)

    def list_workflow_tasks(
        self,
        workflow_id: str,
        *,
        statuses: tuple[TaskStatus, ...] | None = None,
    ) -> tuple[TaskRecord, ...]:
        normalized = _argument_text(
            workflow_id,
            "workflow_id",
            max_length=128,
        )
        if statuses is not None and any(
            not isinstance(status, TaskStatus) for status in statuses
        ):
            raise OrchestrationValidationError(
                "statuses must contain TaskStatus values"
            )
        if statuses is not None and (
            len(statuses) > len(TaskStatus)
            or len(statuses) != len(set(statuses))
        ):
            raise OrchestrationValidationError(
                "statuses must not contain duplicates"
            )
        with _database_operation("workflow task listing"):
            with readonly_connection(self._db_path) as conn:
                _require_workflow(conn, normalized)
                parameters: list[object] = [normalized]
                status_filter = ""
                if statuses is not None:
                    if not statuses:
                        return ()
                    placeholders = ", ".join("?" for _ in statuses)
                    status_filter = f" AND status IN ({placeholders})"
                    parameters.extend(status.value for status in statuses)
                rows = conn.execute(
                    f"SELECT {_TASK_COLUMNS} FROM orchestration_tasks "
                    f"WHERE workflow_id=?{status_filter} "
                    "ORDER BY created_at, task_id",
                    tuple(parameters),
                ).fetchall()
                return tuple(_task_from_row(row) for row in rows)

    def list_task_runs(
        self,
        task_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[TaskRunRecord, ...]:
        normalized = _argument_text(task_id, "task_id", max_length=128)
        normalized_limit = _positive_int(limit, "limit", maximum=1_000)
        if type(offset) is not int or not (0 <= offset <= 1_000_000):
            raise OrchestrationValidationError(
                "offset must be a non-negative integer within its limit"
            )
        with _database_operation("task run listing"):
            with readonly_connection(self._db_path) as conn:
                _require_task(conn, normalized)
                rows = conn.execute(
                    f"SELECT {_RUN_COLUMNS} "
                    "FROM orchestration_task_runs WHERE task_id=? "
                    "ORDER BY attempt_number DESC, claimed_at DESC, run_id "
                    "LIMIT ? OFFSET ?",
                    (normalized, normalized_limit, offset),
                ).fetchall()
                return tuple(_run_from_row(row) for row in rows)

    def claim_ready_tasks(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: float,
    ) -> tuple[TaskClaim, ...]:
        worker = _argument_text(worker_id, "worker_id", max_length=256)
        normalized_limit = _positive_int(limit, "limit", maximum=100)
        claims: list[TaskClaim] = []
        with _database_operation("task claim"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    expires_at = self._lease_expiry(now, lease_seconds)
                    candidates = conn.execute(
                        """
                        SELECT task.task_id
                        FROM orchestration_tasks AS task
                        JOIN orchestration_workflows AS workflow
                          ON workflow.workflow_id=task.workflow_id
                        WHERE workflow.status=?
                          AND task.status=?
                          AND task.attempt_count < task.max_attempts
                        ORDER BY task.priority DESC, task.ready_at,
                                 task.created_at, task.task_id
                        LIMIT ?
                        """,
                        (
                            WorkflowStatus.ACTIVE.value,
                            TaskStatus.READY.value,
                            normalized_limit,
                        ),
                    ).fetchall()
                    for (task_id,) in candidates:
                        run_id = self._new_id("run")
                        claim_token = self._new_claim_token(
                            run_id=run_id,
                            worker_id=worker,
                        )
                        cursor = conn.execute(
                            """
                            UPDATE orchestration_tasks
                            SET status=?, attempt_count=attempt_count + 1,
                                claim_owner=?, claim_token=?,
                                claim_expires_at=?,
                                started_at=COALESCE(started_at, ?),
                                updated_at=?
                            WHERE task_id=? AND status=?
                              AND attempt_count < max_attempts
                              AND EXISTS (
                                  SELECT 1
                                  FROM orchestration_workflows AS workflow
                                  WHERE workflow.workflow_id=
                                        orchestration_tasks.workflow_id
                                    AND workflow.status=?
                              )
                            """,
                            (
                                TaskStatus.RUNNING.value,
                                worker,
                                claim_token,
                                expires_at,
                                now,
                                now,
                                task_id,
                                TaskStatus.READY.value,
                                WorkflowStatus.ACTIVE.value,
                            ),
                        )
                        if cursor.rowcount != 1:
                            continue
                        task = _require_task(conn, str(task_id))
                        try:
                            conn.execute(
                                """
                                INSERT INTO orchestration_task_runs (
                                    run_id, workflow_id, task_id,
                                    attempt_number, worker_id, claim_token,
                                    status, session_key, claimed_at,
                                    started_at, heartbeat_at, finished_at,
                                    result_summary, result_metadata_json,
                                    error_type, error_message
                                ) VALUES (
                                    ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?,
                                    NULL, NULL, NULL, NULL, NULL
                                )
                                """,
                                (
                                    run_id,
                                    task.workflow_id,
                                    task.task_id,
                                    task.attempt_count,
                                    worker,
                                    claim_token,
                                    TaskRunStatus.CLAIMED.value,
                                    now,
                                    now,
                                ),
                            )
                        except sqlite3.IntegrityError as exc:
                            raise OrchestrationPersistenceError(
                                "task claim identifiers conflicted"
                            ) from exc
                        run = _read_run_by_claim(
                            conn,
                            task.task_id,
                            claim_token,
                        )
                        if run is None:
                            raise OrchestrationPersistenceError(
                                "claimed task run could not be read back"
                            )
                        workflow = _require_workflow(conn, task.workflow_id)
                        claims.append(_claim_record(
                            workflow=workflow,
                            task=task,
                            run=run,
                            claim_token=claim_token,
                            claim_expires_at=expires_at,
                        ))
        return tuple(claims)

    def renew_task_claim(
        self,
        *,
        task_id: str,
        claim_token: str,
        lease_seconds: float,
    ) -> TaskClaim:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        token = _argument_text(
            claim_token,
            "claim_token",
            max_length=256,
        )
        with _database_operation("task claim renewal"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    expires_at = self._lease_expiry(now, lease_seconds)
                    _require_claim(conn, normalized_task_id, token)
                    task_cursor = conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET claim_expires_at=?, updated_at=?
                        WHERE task_id=? AND status=? AND claim_token=?
                        """,
                        (
                            expires_at,
                            now,
                            normalized_task_id,
                            TaskStatus.RUNNING.value,
                            token,
                        ),
                    )
                    run_cursor = conn.execute(
                        """
                        UPDATE orchestration_task_runs
                        SET heartbeat_at=?
                        WHERE task_id=? AND claim_token=?
                          AND status IN (?, ?)
                        """,
                        (
                            now,
                            normalized_task_id,
                            token,
                            *_ACTIVE_RUN_STATUSES,
                        ),
                    )
                    if task_cursor.rowcount != 1 or run_cursor.rowcount != 1:
                        raise TaskClaimLostError(
                            "task claim was lost during renewal"
                        )
                    task = _require_task(conn, normalized_task_id)
                    run = _read_run_by_claim(conn, normalized_task_id, token)
                    if run is None:
                        raise OrchestrationPersistenceError(
                            "renewed task run could not be read back"
                        )
                    workflow = _require_workflow(conn, task.workflow_id)
                    result = _claim_record(
                        workflow=workflow,
                        task=task,
                        run=run,
                        claim_token=token,
                        claim_expires_at=expires_at,
                    )
        return result

    def mark_task_run_started(
        self,
        *,
        task_id: str,
        claim_token: str,
        session_key: str | None = None,
    ) -> TaskRunRecord:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        token = _argument_text(
            claim_token,
            "claim_token",
            max_length=256,
        )
        if session_key is not None:
            _argument_text(session_key, "session_key", max_length=512)
        with _database_operation("task run start"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    _task, run = _require_claim(
                        conn,
                        normalized_task_id,
                        token,
                    )
                    if run.status is TaskRunStatus.CLAIMED:
                        cursor = conn.execute(
                            """
                            UPDATE orchestration_task_runs
                            SET status=?, session_key=?, started_at=?,
                                heartbeat_at=?
                            WHERE run_id=? AND claim_token=? AND status=?
                            """,
                            (
                                TaskRunStatus.RUNNING.value,
                                session_key,
                                now,
                                now,
                                run.run_id,
                                token,
                                TaskRunStatus.CLAIMED.value,
                            ),
                        )
                    elif run.session_key == session_key:
                        cursor = conn.execute(
                            """
                            UPDATE orchestration_task_runs
                            SET heartbeat_at=?
                            WHERE run_id=? AND claim_token=? AND status=?
                            """,
                            (
                                now,
                                run.run_id,
                                token,
                                TaskRunStatus.RUNNING.value,
                            ),
                        )
                    else:
                        raise InvalidTaskTransitionError(
                            "running task session_key does not match"
                        )
                    if cursor.rowcount != 1:
                        raise TaskClaimLostError(
                            "task claim was lost while starting its run"
                        )
                    result = _read_run_by_claim(
                        conn,
                        normalized_task_id,
                        token,
                    )
                    if result is None:
                        raise OrchestrationPersistenceError(
                            "started task run could not be read back"
                        )
        return result

    def complete_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        result_summary: str | None,
        result_metadata: Mapping[str, object] | None,
    ) -> TaskRecord:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        token = _argument_text(
            claim_token,
            "claim_token",
            max_length=256,
        )
        if result_summary is not None:
            _argument_text(
                result_summary,
                "result_summary",
                max_length=20_000,
                allow_empty=True,
            )
        encoded_metadata = _encode_optional_json_object(
            result_metadata,
            "result_metadata",
        )
        with _database_operation("task completion"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    task, run = _require_claim(
                        conn,
                        normalized_task_id,
                        token,
                    )
                    run_cursor = conn.execute(
                        """
                        UPDATE orchestration_task_runs
                        SET status=?, finished_at=?, result_summary=?,
                            result_metadata_json=?, error_type=NULL,
                            error_message=NULL
                        WHERE run_id=? AND claim_token=?
                          AND status IN (?, ?)
                        """,
                        (
                            TaskRunStatus.COMPLETED.value,
                            now,
                            result_summary,
                            encoded_metadata,
                            run.run_id,
                            token,
                            *_ACTIVE_RUN_STATUSES,
                        ),
                    )
                    task_cursor = conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET status=?, claim_owner=NULL, claim_token=NULL,
                            claim_expires_at=NULL, result_summary=?,
                            result_metadata_json=?, error_type=NULL,
                            error_message=NULL, blocked_reason=NULL,
                            finished_at=?, updated_at=?
                        WHERE task_id=? AND status=? AND claim_token=?
                        """,
                        (
                            TaskStatus.COMPLETED.value,
                            result_summary,
                            encoded_metadata,
                            now,
                            now,
                            normalized_task_id,
                            TaskStatus.RUNNING.value,
                            token,
                        ),
                    )
                    if run_cursor.rowcount != 1 or task_cursor.rowcount != 1:
                        raise TaskClaimLostError(
                            "task claim was lost during completion"
                        )
                    workflow = _require_workflow(conn, task.workflow_id)
                    if workflow.status is WorkflowStatus.ACTIVE:
                        _advance_direct_downstream(
                            conn,
                            workflow_id=task.workflow_id,
                            completed_task_id=task.task_id,
                            now=now,
                        )
                        _finalize_active_workflow_if_terminal(
                            conn,
                            workflow_id=task.workflow_id,
                            now=now,
                        )
                    result = _require_task(conn, normalized_task_id)
        return result

    def fail_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> TaskRecord:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        token = _argument_text(
            claim_token,
            "claim_token",
            max_length=256,
        )
        normalized_error_type = _argument_text(
            error_type,
            "error_type",
            max_length=256,
        )
        normalized_error_message = _argument_text(
            error_message,
            "error_message",
            max_length=4_000,
            allow_empty=True,
        )
        if type(retryable) is not bool:
            raise OrchestrationValidationError("retryable must be a boolean")
        with _database_operation("task failure"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    task, run = _require_claim(
                        conn,
                        normalized_task_id,
                        token,
                    )
                    workflow = _require_workflow(conn, task.workflow_id)
                    run_cursor = conn.execute(
                        """
                        UPDATE orchestration_task_runs
                        SET status=?, finished_at=?, error_type=?,
                            error_message=?
                        WHERE run_id=? AND claim_token=?
                          AND status IN (?, ?)
                        """,
                        (
                            TaskRunStatus.FAILED.value,
                            now,
                            normalized_error_type,
                            normalized_error_message,
                            run.run_id,
                            token,
                            *_ACTIVE_RUN_STATUSES,
                        ),
                    )
                    should_retry = (
                        retryable
                        and workflow.status is WorkflowStatus.ACTIVE
                        and task.attempt_count < task.max_attempts
                    )
                    if should_retry:
                        target_status = TaskStatus.READY
                        ready_at = now
                        finished_at = None
                    else:
                        target_status = TaskStatus.FAILED
                        ready_at = task.ready_at
                        finished_at = now
                    task_cursor = conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET status=?, claim_owner=NULL, claim_token=NULL,
                            claim_expires_at=NULL, ready_at=?, finished_at=?,
                            error_type=?, error_message=?, updated_at=?
                        WHERE task_id=? AND status=? AND claim_token=?
                        """,
                        (
                            target_status.value,
                            ready_at,
                            finished_at,
                            normalized_error_type,
                            normalized_error_message,
                            now,
                            normalized_task_id,
                            TaskStatus.RUNNING.value,
                            token,
                        ),
                    )
                    if run_cursor.rowcount != 1 or task_cursor.rowcount != 1:
                        raise TaskClaimLostError(
                            "task claim was lost during failure handling"
                        )
                    if not should_retry and workflow.status is WorkflowStatus.ACTIVE:
                        _fail_active_workflow(
                            conn,
                            workflow_id=task.workflow_id,
                            now=now,
                        )
                    result = _require_task(conn, normalized_task_id)
        return result

    def block_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        blocked_reason: str,
    ) -> TaskRecord:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        token = _argument_text(
            claim_token,
            "claim_token",
            max_length=256,
        )
        reason = _argument_text(
            blocked_reason,
            "blocked_reason",
            max_length=4_000,
        )
        with _database_operation("task blocking"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    task, run = _require_claim(
                        conn,
                        normalized_task_id,
                        token,
                    )
                    workflow = _require_workflow(conn, task.workflow_id)
                    if workflow.status is not WorkflowStatus.ACTIVE:
                        raise InvalidTaskTransitionError(
                            "task cannot block after workflow termination"
                        )
                    run_cursor = conn.execute(
                        """
                        UPDATE orchestration_task_runs
                        SET status=?, finished_at=?, error_type=?,
                            error_message=?
                        WHERE run_id=? AND claim_token=?
                          AND status IN (?, ?)
                        """,
                        (
                            TaskRunStatus.BLOCKED.value,
                            now,
                            "blocked",
                            reason,
                            run.run_id,
                            token,
                            *_ACTIVE_RUN_STATUSES,
                        ),
                    )
                    task_cursor = conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET status=?, claim_owner=NULL, claim_token=NULL,
                            claim_expires_at=NULL, blocked_reason=?,
                            error_type=NULL, error_message=NULL,
                            finished_at=NULL, updated_at=?
                        WHERE task_id=? AND status=? AND claim_token=?
                        """,
                        (
                            TaskStatus.BLOCKED.value,
                            reason,
                            now,
                            normalized_task_id,
                            TaskStatus.RUNNING.value,
                            token,
                        ),
                    )
                    if run_cursor.rowcount != 1 or task_cursor.rowcount != 1:
                        raise TaskClaimLostError(
                            "task claim was lost while blocking"
                        )
                    result = _require_task(conn, normalized_task_id)
        return result

    def unblock_task(self, *, task_id: str) -> TaskRecord:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        with _database_operation("task unblocking"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    task = _require_task(conn, normalized_task_id)
                    if task.status is not TaskStatus.BLOCKED:
                        raise InvalidTaskTransitionError(
                            "only blocked tasks can be unblocked"
                        )
                    workflow = _require_workflow(conn, task.workflow_id)
                    if workflow.status is not WorkflowStatus.ACTIVE:
                        raise InvalidTaskTransitionError(
                            "task cannot be unblocked after workflow termination"
                        )
                    incomplete_dependency = conn.execute(
                        """
                        SELECT 1
                        FROM orchestration_task_dependencies AS dependency
                        JOIN orchestration_tasks AS parent
                          ON parent.task_id=dependency.depends_on_task_id
                         AND parent.workflow_id=dependency.workflow_id
                        WHERE dependency.task_id=?
                          AND dependency.workflow_id=?
                          AND parent.status!=?
                        LIMIT 1
                        """,
                        (
                            task.task_id,
                            task.workflow_id,
                            TaskStatus.COMPLETED.value,
                        ),
                    ).fetchone()
                    target_status = (
                        TaskStatus.READY
                        if incomplete_dependency is None
                        else TaskStatus.TODO
                    )
                    cursor = conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET status=?, blocked_reason=NULL, ready_at=?,
                            updated_at=?
                        WHERE task_id=? AND status=?
                        """,
                        (
                            target_status.value,
                            now if target_status is TaskStatus.READY else None,
                            now,
                            normalized_task_id,
                            TaskStatus.BLOCKED.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise InvalidTaskTransitionError(
                            "task state changed before it could be unblocked"
                        )
                    result = _require_task(conn, normalized_task_id)
        return result

    def cancel_task(self, *, task_id: str) -> TaskRecord:
        normalized_task_id = _argument_text(
            task_id,
            "task_id",
            max_length=128,
        )
        with _database_operation("task cancellation"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    task = _require_task(conn, normalized_task_id)
                    if task.status is TaskStatus.CANCELLED:
                        return task
                    if task.status in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                    }:
                        raise InvalidTaskTransitionError(
                            "terminal task cannot be cancelled"
                        )
                    if task.status is TaskStatus.RUNNING:
                        if task.claim_token is None:
                            raise OrchestrationPersistenceError(
                                "running task is missing its claim token"
                            )
                        run_cursor = conn.execute(
                            """
                            UPDATE orchestration_task_runs
                            SET status=?, finished_at=?, error_type=?,
                                error_message=?
                            WHERE task_id=? AND claim_token=?
                              AND status IN (?, ?)
                            """,
                            (
                                TaskRunStatus.CANCELLED.value,
                                now,
                                "task_cancelled",
                                "task was cancelled",
                                task.task_id,
                                task.claim_token,
                                *_ACTIVE_RUN_STATUSES,
                            ),
                        )
                        if run_cursor.rowcount != 1:
                            raise OrchestrationPersistenceError(
                                "running task has no active run"
                            )
                    cursor = conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET status=?, claim_owner=NULL, claim_token=NULL,
                            claim_expires_at=NULL, blocked_reason=NULL,
                            error_type=?, error_message=?, finished_at=?,
                            updated_at=?
                        WHERE task_id=?
                          AND status IN (?, ?, ?, ?)
                        """,
                        (
                            TaskStatus.CANCELLED.value,
                            "task_cancelled",
                            "task was cancelled",
                            now,
                            now,
                            normalized_task_id,
                            TaskStatus.TODO.value,
                            TaskStatus.READY.value,
                            TaskStatus.RUNNING.value,
                            TaskStatus.BLOCKED.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise InvalidTaskTransitionError(
                            "task state changed before cancellation"
                        )
                    conn.execute(
                        """
                        WITH RECURSIVE descendants(task_id) AS (
                            SELECT task_id
                            FROM orchestration_task_dependencies
                            WHERE workflow_id=? AND depends_on_task_id=?
                            UNION
                            SELECT dependency.task_id
                            FROM orchestration_task_dependencies AS dependency
                            JOIN descendants
                              ON dependency.depends_on_task_id=
                                 descendants.task_id
                            WHERE dependency.workflow_id=?
                        )
                        UPDATE orchestration_tasks
                        SET status=?, claim_owner=NULL, claim_token=NULL,
                            claim_expires_at=NULL, blocked_reason=NULL,
                            error_type=?, error_message=?, finished_at=?,
                            updated_at=?
                        WHERE workflow_id=?
                          AND task_id IN (SELECT task_id FROM descendants)
                          AND status IN (?, ?, ?)
                        """,
                        (
                            task.workflow_id,
                            task.task_id,
                            task.workflow_id,
                            TaskStatus.CANCELLED.value,
                            "dependency_cancelled",
                            "a required dependency was cancelled",
                            now,
                            now,
                            task.workflow_id,
                            TaskStatus.TODO.value,
                            TaskStatus.READY.value,
                            TaskStatus.BLOCKED.value,
                        ),
                    )
                    _finalize_active_workflow_if_terminal(
                        conn,
                        workflow_id=task.workflow_id,
                        now=now,
                    )
                    result = _require_task(conn, normalized_task_id)
        return result

    def cancel_workflow(self, *, workflow_id: str) -> WorkflowRecord:
        normalized_workflow_id = _argument_text(
            workflow_id,
            "workflow_id",
            max_length=128,
        )
        with _database_operation("workflow cancellation"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    workflow = _require_workflow(
                        conn,
                        normalized_workflow_id,
                    )
                    if workflow.status is WorkflowStatus.CANCELLED:
                        return workflow
                    if workflow.status in {
                        WorkflowStatus.COMPLETED,
                        WorkflowStatus.FAILED,
                    }:
                        raise InvalidTaskTransitionError(
                            "terminal workflow cannot be cancelled"
                        )
                    conn.execute(
                        """
                        UPDATE orchestration_task_runs
                        SET status=?, finished_at=?, error_type=?,
                            error_message=?
                        WHERE workflow_id=? AND status IN (?, ?)
                        """,
                        (
                            TaskRunStatus.CANCELLED.value,
                            now,
                            "workflow_cancelled",
                            "workflow was cancelled",
                            normalized_workflow_id,
                            *_ACTIVE_RUN_STATUSES,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE orchestration_tasks
                        SET status=?, claim_owner=NULL, claim_token=NULL,
                            claim_expires_at=NULL, blocked_reason=NULL,
                            error_type=?, error_message=?, finished_at=?,
                            updated_at=?
                        WHERE workflow_id=?
                          AND status IN (?, ?, ?, ?)
                        """,
                        (
                            TaskStatus.CANCELLED.value,
                            "workflow_cancelled",
                            "workflow was cancelled",
                            now,
                            now,
                            normalized_workflow_id,
                            TaskStatus.TODO.value,
                            TaskStatus.READY.value,
                            TaskStatus.RUNNING.value,
                            TaskStatus.BLOCKED.value,
                        ),
                    )
                    cursor = conn.execute(
                        """
                        UPDATE orchestration_workflows
                        SET status=?, updated_at=?, finished_at=?
                        WHERE workflow_id=? AND status=?
                        """,
                        (
                            WorkflowStatus.CANCELLED.value,
                            now,
                            now,
                            normalized_workflow_id,
                            WorkflowStatus.ACTIVE.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise InvalidTaskTransitionError(
                            "workflow state changed before cancellation"
                        )
                    result = _require_workflow(
                        conn,
                        normalized_workflow_id,
                    )
        return result

    def recover_expired_claims(
        self,
        *,
        limit: int,
    ) -> tuple[TaskRecord, ...]:
        normalized_limit = _positive_int(limit, "limit", maximum=100)
        recovered_task_ids: list[str] = []
        recovered_records: tuple[TaskRecord, ...] = ()
        with _database_operation("expired claim recovery"):
            with existing_write_connection(self._db_path) as conn:
                with _immediate_transaction(conn):
                    now = self._now()
                    candidates = conn.execute(
                        """
                        SELECT task.task_id, task.claim_token
                        FROM orchestration_tasks AS task
                        JOIN orchestration_task_runs AS run
                          ON run.task_id=task.task_id
                         AND run.claim_token=task.claim_token
                        WHERE task.status=?
                          AND task.claim_expires_at<=?
                          AND run.status IN (?, ?)
                        ORDER BY task.claim_expires_at, task.task_id
                        LIMIT ?
                        """,
                        (
                            TaskStatus.RUNNING.value,
                            now,
                            *_ACTIVE_RUN_STATUSES,
                            normalized_limit,
                        ),
                    ).fetchall()
                    for raw_task_id, raw_claim_token in candidates:
                        task_id_value = str(raw_task_id)
                        token = str(raw_claim_token)
                        task, run = _require_claim(
                            conn,
                            task_id_value,
                            token,
                        )
                        workflow = _require_workflow(conn, task.workflow_id)
                        should_retry = (
                            workflow.status is WorkflowStatus.ACTIVE
                            and task.attempt_count < task.max_attempts
                        )
                        target_status = (
                            TaskStatus.READY
                            if should_retry
                            else TaskStatus.FAILED
                        )
                        task_cursor = conn.execute(
                            """
                            UPDATE orchestration_tasks
                            SET status=?, claim_owner=NULL, claim_token=NULL,
                                claim_expires_at=NULL, ready_at=?,
                                error_type=?, error_message=?, finished_at=?,
                                updated_at=?
                            WHERE task_id=? AND status=? AND claim_token=?
                              AND claim_expires_at<=?
                            """,
                            (
                                target_status.value,
                                now if should_retry else task.ready_at,
                                "claim_expired",
                                "task claim lease expired",
                                None if should_retry else now,
                                now,
                                task.task_id,
                                TaskStatus.RUNNING.value,
                                token,
                                now,
                            ),
                        )
                        if task_cursor.rowcount != 1:
                            continue
                        run_cursor = conn.execute(
                            """
                            UPDATE orchestration_task_runs
                            SET status=?, finished_at=?, error_type=?,
                                error_message=?
                            WHERE run_id=? AND claim_token=?
                              AND status IN (?, ?)
                            """,
                            (
                                TaskRunStatus.ABANDONED.value,
                                now,
                                "claim_expired",
                                "task claim lease expired",
                                run.run_id,
                                token,
                                *_ACTIVE_RUN_STATUSES,
                            ),
                        )
                        if run_cursor.rowcount != 1:
                            raise OrchestrationPersistenceError(
                                "expired task claim has no active run"
                            )
                        recovered_task_ids.append(task.task_id)
                        if (
                            not should_retry
                            and workflow.status is WorkflowStatus.ACTIVE
                        ):
                            _fail_active_workflow(
                                conn,
                                workflow_id=task.workflow_id,
                                now=now,
                            )
                    recovered_records = tuple(
                        _require_task(conn, task_id_value)
                        for task_id_value in recovered_task_ids
                    )
        return recovered_records


__all__ = ["SQLiteOrchestrationStore"]
