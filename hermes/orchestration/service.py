"""多 Agent 任务编排的用例校验与领域服务。"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Iterable, Mapping

from hermes.orchestration.errors import (
    OrchestrationNotFoundError,
    OrchestrationValidationError,
    WorkflowCycleError,
)
from hermes.orchestration.models import (
    TaskClaim,
    TaskCreateSpec,
    TaskRecord,
    TaskRunRecord,
    TaskStatus,
    WorkflowCreateSpec,
    WorkflowRecord,
    plain_json_object,
)
from hermes.orchestration.store import OrchestrationStore
from hermes.orchestration.workflow_execution import WorkflowExecutionSnapshot


_MAX_TASKS_PER_WORKFLOW = 10_000
_MAX_TEXT_LENGTH = 100_000
_MAX_TITLE_LENGTH = 500
_MAX_KEY_LENGTH = 128
_MAX_ROLE_LENGTH = 128
_MAX_SESSION_KEY_LENGTH = 512
_MAX_WORKDIR_LENGTH = 4_096
_MAX_METADATA_JSON_LENGTH = 1_000_000
_MAX_RESULT_SUMMARY_LENGTH = 20_000
_MAX_ERROR_TYPE_LENGTH = 256
_MAX_ERROR_MESSAGE_LENGTH = 4_000
_MAX_BLOCKED_REASON_LENGTH = 4_000
_MAX_ATTEMPTS = 100
_MAX_PRIORITY_ABS = 1_000_000
_MAX_CLAIM_LIMIT = 100
_MAX_QUERY_LIMIT = 1_000
_MAX_QUERY_OFFSET = 1_000_000
_MAX_LEASE_SECONDS = 86_400.0


def _require_string(
    value: object,
    field_name: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise OrchestrationValidationError(
            f"{field_name} must be a string"
        )
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


def _require_positive_int(
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


def _require_lease_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrchestrationValidationError(
            "lease_seconds must be a finite positive number"
        )
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0
        or normalized > _MAX_LEASE_SECONDS
    ):
        raise OrchestrationValidationError(
            "lease_seconds must be a finite positive number within its limit"
        )
    return normalized


def _validate_json_object(
    value: Mapping[str, object],
    field_name: str,
) -> dict[str, object]:
    plain = plain_json_object(value, field_name=field_name)
    try:
        encoded = json.dumps(
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
    if len(encoded) > _MAX_METADATA_JSON_LENGTH:
        raise OrchestrationValidationError(
            f"{field_name} exceeds its serialized size limit"
        )
    return plain


def _validate_task_spec(task: TaskCreateSpec) -> None:
    if not isinstance(task, TaskCreateSpec):
        raise OrchestrationValidationError(
            "tasks must contain only TaskCreateSpec values"
        )
    _require_string(task.key, "task key", max_length=_MAX_KEY_LENGTH)
    _require_string(task.title, "task title", max_length=_MAX_TITLE_LENGTH)
    _require_string(task.prompt, "task prompt", max_length=_MAX_TEXT_LENGTH)
    _require_string(task.role, "task role", max_length=_MAX_ROLE_LENGTH)
    if type(task.priority) is not int or not (
        -_MAX_PRIORITY_ABS <= task.priority <= _MAX_PRIORITY_ABS
    ):
        raise OrchestrationValidationError(
            "task priority is outside the supported integer range"
        )
    _require_positive_int(
        task.max_attempts,
        "task max_attempts",
        maximum=_MAX_ATTEMPTS,
    )
    if task.workdir is not None:
        _require_string(
            task.workdir,
            "task workdir",
            max_length=_MAX_WORKDIR_LENGTH,
        )
    if len(task.depends_on) > _MAX_TASKS_PER_WORKFLOW:
        raise OrchestrationValidationError(
            "task dependency count exceeds its limit"
        )
    for dependency_key in task.depends_on:
        _require_string(
            dependency_key,
            "dependency key",
            max_length=_MAX_KEY_LENGTH,
        )
    _validate_json_object(task.input_metadata, "input_metadata")


def _validate_dag(tasks: tuple[TaskCreateSpec, ...]) -> None:
    """用 Kahn 算法拒绝未知边、自环、重复边和完整图环。"""

    task_by_key: dict[str, TaskCreateSpec] = {}
    for task in tasks:
        if task.key in task_by_key:
            raise OrchestrationValidationError(
                "task keys must be unique within a workflow"
            )
        task_by_key[task.key] = task

    indegree = {key: 0 for key in task_by_key}
    downstream = {key: [] for key in task_by_key}
    for task in tasks:
        seen_dependencies: set[str] = set()
        for dependency_key in task.depends_on:
            if dependency_key not in task_by_key:
                raise OrchestrationValidationError(
                    "task dependency references an unknown task key"
                )
            if dependency_key == task.key:
                raise WorkflowCycleError("task cannot depend on itself")
            if dependency_key in seen_dependencies:
                raise OrchestrationValidationError(
                    "task dependencies must not contain duplicate edges"
                )
            seen_dependencies.add(dependency_key)
            indegree[task.key] += 1
            downstream[dependency_key].append(task.key)

    ready = deque(sorted(
        key for key, degree in indegree.items() if degree == 0
    ))
    visited = 0
    while ready:
        key = ready.popleft()
        visited += 1
        for child_key in sorted(downstream[key]):
            indegree[child_key] -= 1
            if indegree[child_key] == 0:
                ready.append(child_key)
    if visited != len(tasks):
        raise WorkflowCycleError("workflow task dependency graph contains a cycle")


class OrchestrationService:
    """校验用例参数，并把原子状态操作委托给 Store。"""

    __slots__ = ("_store",)

    def __init__(self, store: OrchestrationStore) -> None:
        if store is None:
            raise TypeError("store is required")
        self._store = store

    def create_workflow(self, spec: WorkflowCreateSpec) -> WorkflowRecord:
        if not isinstance(spec, WorkflowCreateSpec):
            raise OrchestrationValidationError(
                "spec must be a WorkflowCreateSpec"
            )
        _require_string(spec.title, "title", max_length=_MAX_TITLE_LENGTH)
        _require_string(spec.goal, "goal", max_length=_MAX_TEXT_LENGTH)
        if spec.created_by_session is not None:
            _require_string(
                spec.created_by_session,
                "created_by_session",
                max_length=_MAX_SESSION_KEY_LENGTH,
            )
        if not spec.tasks:
            raise OrchestrationValidationError(
                "workflow must contain at least one task"
            )
        if len(spec.tasks) > _MAX_TASKS_PER_WORKFLOW:
            raise OrchestrationValidationError(
                "workflow task count exceeds its limit"
            )
        for task in spec.tasks:
            _validate_task_spec(task)
        _validate_dag(spec.tasks)
        return self._store.create_workflow(spec)

    def get_workflow(self, workflow_id: str) -> WorkflowRecord:
        normalized = _require_string(
            workflow_id,
            "workflow_id",
            max_length=_MAX_KEY_LENGTH,
        )
        workflow = self._store.get_workflow(normalized)
        if workflow is None:
            raise OrchestrationNotFoundError("workflow was not found")
        return workflow

    def get_workflow_execution_snapshot(
        self,
        *,
        workflow_id: str,
    ) -> WorkflowExecutionSnapshot:
        """原子读取 Runner 状态分类所需的完整 Workflow 快照。"""

        return self._store.get_workflow_execution_snapshot(
            workflow_id=_require_string(
                workflow_id,
                "workflow_id",
                max_length=_MAX_KEY_LENGTH,
            )
        )

    def get_task(self, task_id: str) -> TaskRecord:
        normalized = _require_string(
            task_id,
            "task_id",
            max_length=_MAX_KEY_LENGTH,
        )
        task = self._store.get_task(normalized)
        if task is None:
            raise OrchestrationNotFoundError("task was not found")
        return task

    def list_workflow_tasks(
        self,
        workflow_id: str,
        *,
        statuses: Iterable[TaskStatus | str] | None = None,
    ) -> tuple[TaskRecord, ...]:
        normalized_id = _require_string(
            workflow_id,
            "workflow_id",
            max_length=_MAX_KEY_LENGTH,
        )
        normalized_statuses: tuple[TaskStatus, ...] | None = None
        if statuses is not None:
            if isinstance(statuses, (str, bytes)):
                raise OrchestrationValidationError(
                    "statuses must be an iterable of TaskStatus values"
                )
            try:
                normalized_statuses = tuple(
                    status
                    if isinstance(status, TaskStatus)
                    else TaskStatus(status)
                    for status in statuses
                )
            except (TypeError, ValueError) as exc:
                raise OrchestrationValidationError(
                    "statuses contains an invalid TaskStatus"
                ) from exc
            if len(normalized_statuses) != len(set(normalized_statuses)):
                raise OrchestrationValidationError(
                    "statuses must not contain duplicates"
                )
        return self._store.list_workflow_tasks(
            normalized_id,
            statuses=normalized_statuses,
        )

    def list_task_runs(
        self,
        task_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[TaskRunRecord, ...]:
        normalized_id = _require_string(
            task_id,
            "task_id",
            max_length=_MAX_KEY_LENGTH,
        )
        normalized_limit = _require_positive_int(
            limit,
            "limit",
            maximum=_MAX_QUERY_LIMIT,
        )
        if type(offset) is not int or not (0 <= offset <= _MAX_QUERY_OFFSET):
            raise OrchestrationValidationError(
                "offset must be a non-negative integer within its limit"
            )
        return self._store.list_task_runs(
            normalized_id,
            limit=normalized_limit,
            offset=offset,
        )

    def list_task_dependencies(
        self,
        *,
        task_id: str,
    ) -> tuple[TaskRecord, ...]:
        return self._store.list_task_dependencies(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            )
        )

    def claim_ready_tasks(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: float,
    ) -> tuple[TaskClaim, ...]:
        normalized_worker = _require_string(
            worker_id,
            "worker_id",
            max_length=256,
        )
        normalized_limit = _require_positive_int(
            limit,
            "limit",
            maximum=_MAX_CLAIM_LIMIT,
        )
        return self._store.claim_ready_tasks(
            worker_id=normalized_worker,
            limit=normalized_limit,
            lease_seconds=_require_lease_seconds(lease_seconds),
        )

    def reserve_next_ready_task(
        self,
        *,
        workflow_id: str,
        owner_id: str,
        lease_seconds: float,
    ) -> TaskClaim | None:
        """为中央 Runner 原子保留指定 Workflow 的一个 ready Task。"""

        return self._store.reserve_next_ready_task(
            workflow_id=_require_string(
                workflow_id,
                "workflow_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            owner_id=_require_string(
                owner_id,
                "owner_id",
                max_length=256,
            ),
            lease_seconds=_require_lease_seconds(lease_seconds),
        )

    def reserve_ready_tasks(
        self,
        *,
        workflow_id: str,
        owner_id: str,
        limit: int,
        lease_seconds: float,
    ) -> tuple[TaskClaim, ...]:
        """为中央 Runner 原子保留不超过 limit 个 ready Task。"""

        return self._store.reserve_ready_tasks(
            workflow_id=_require_string(
                workflow_id,
                "workflow_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            owner_id=_require_string(
                owner_id,
                "owner_id",
                max_length=256,
            ),
            limit=_require_positive_int(
                limit,
                "limit",
                maximum=_MAX_CLAIM_LIMIT,
            ),
            lease_seconds=_require_lease_seconds(lease_seconds),
        )

    def renew_task_claim(
        self,
        *,
        task_id: str,
        claim_token: str,
        lease_seconds: float,
    ) -> TaskClaim:
        return self._store.renew_task_claim(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            claim_token=_require_string(
                claim_token,
                "claim_token",
                max_length=256,
            ),
            lease_seconds=_require_lease_seconds(lease_seconds),
        )

    def mark_task_run_started(
        self,
        *,
        task_id: str,
        claim_token: str,
        session_key: str | None = None,
    ) -> TaskRunRecord:
        if session_key is not None:
            _require_string(
                session_key,
                "session_key",
                max_length=_MAX_SESSION_KEY_LENGTH,
            )
        return self._store.mark_task_run_started(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            claim_token=_require_string(
                claim_token,
                "claim_token",
                max_length=256,
            ),
            session_key=session_key,
        )

    def complete_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        result_summary: str | None = None,
        result_metadata: Mapping[str, object] | None = None,
    ) -> TaskRecord:
        if result_summary is not None:
            _require_string(
                result_summary,
                "result_summary",
                max_length=_MAX_RESULT_SUMMARY_LENGTH,
                allow_empty=True,
            )
        normalized_metadata = (
            None
            if result_metadata is None
            else _validate_json_object(result_metadata, "result_metadata")
        )
        return self._store.complete_task(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            claim_token=_require_string(
                claim_token,
                "claim_token",
                max_length=256,
            ),
            result_summary=result_summary,
            result_metadata=normalized_metadata,
        )

    def fail_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        error_type: str,
        error_message: str,
        retryable: bool,
    ) -> TaskRecord:
        if type(retryable) is not bool:
            raise OrchestrationValidationError("retryable must be a boolean")
        return self._store.fail_task(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            claim_token=_require_string(
                claim_token,
                "claim_token",
                max_length=256,
            ),
            error_type=_require_string(
                error_type,
                "error_type",
                max_length=_MAX_ERROR_TYPE_LENGTH,
            ),
            error_message=_require_string(
                error_message,
                "error_message",
                max_length=_MAX_ERROR_MESSAGE_LENGTH,
                allow_empty=True,
            ),
            retryable=retryable,
        )

    def block_task(
        self,
        *,
        task_id: str,
        claim_token: str,
        blocked_reason: str,
    ) -> TaskRecord:
        return self._store.block_task(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            claim_token=_require_string(
                claim_token,
                "claim_token",
                max_length=256,
            ),
            blocked_reason=_require_string(
                blocked_reason,
                "blocked_reason",
                max_length=_MAX_BLOCKED_REASON_LENGTH,
            ),
        )

    def release_task_claim(
        self,
        *,
        task_id: str,
        claim_token: str,
        reason: str,
    ) -> TaskRecord:
        return self._store.release_task_claim(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            ),
            claim_token=_require_string(
                claim_token,
                "claim_token",
                max_length=256,
            ),
            reason=_require_string(
                reason,
                "reason",
                max_length=_MAX_ERROR_MESSAGE_LENGTH,
            ),
        )

    def unblock_task(self, *, task_id: str) -> TaskRecord:
        return self._store.unblock_task(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            )
        )

    def cancel_task(self, *, task_id: str) -> TaskRecord:
        return self._store.cancel_task(
            task_id=_require_string(
                task_id,
                "task_id",
                max_length=_MAX_KEY_LENGTH,
            )
        )

    def cancel_workflow(self, *, workflow_id: str) -> WorkflowRecord:
        return self._store.cancel_workflow(
            workflow_id=_require_string(
                workflow_id,
                "workflow_id",
                max_length=_MAX_KEY_LENGTH,
            )
        )

    def recover_expired_claims(
        self,
        *,
        limit: int,
    ) -> tuple[TaskRecord, ...]:
        return self._store.recover_expired_claims(
            limit=_require_positive_int(
                limit,
                "limit",
                maximum=_MAX_CLAIM_LIMIT,
            )
        )


__all__ = ["OrchestrationService"]
