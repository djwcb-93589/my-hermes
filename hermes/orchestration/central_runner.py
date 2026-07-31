"""中心化、顺序、Push 式 Workflow Runner。"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from hermes.orchestration.errors import (
    OrchestrationConflictError,
    OrchestrationNotFoundError,
    OrchestrationValidationError,
    WorkflowRunnerError,
    WorkflowRunnerValidationError,
)
from hermes.orchestration.execution import (
    ClaimedTaskExecutor,
    TaskExecutionOutcome,
    TaskExecutionOutcomeKind,
)
from hermes.orchestration.models import (
    TaskClaim,
    TaskRecord,
    TaskStatus,
    WorkflowRecord,
    WorkflowStatus,
)
from hermes.orchestration.service import OrchestrationService
from hermes.orchestration.workflow_execution import (
    WorkflowExecutionKind,
    WorkflowExecutionResult,
)


_MAX_RUNNER_ID_LENGTH = 256
_MAX_WORKFLOW_ID_LENGTH = 128
_MAX_PARENT_RUN_ID_LENGTH = 512
_MAX_LEASE_SECONDS = 86_400.0
_MAX_STEPS = 10_000


@dataclass(frozen=True, slots=True)
class _WorkflowSnapshot:
    """Runner 最近一次通过 Service 观察到的 Workflow 完整状态。"""

    workflow: WorkflowRecord
    tasks: tuple[TaskRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, WorkflowRecord):
            raise WorkflowRunnerError(
                "workflow service returned an invalid workflow record"
            )
        if not isinstance(self.tasks, (list, tuple)):
            raise WorkflowRunnerError(
                "workflow service returned an invalid task collection"
            )
        tasks = tuple(self.tasks)
        seen_task_ids: set[str] = set()
        for task in tasks:
            if (
                not isinstance(task, TaskRecord)
                or task.workflow_id != self.workflow.workflow_id
                or task.task_id in seen_task_ids
            ):
                raise WorkflowRunnerError(
                    "workflow service returned inconsistent task records"
                )
            seen_task_ids.add(task.task_id)
        object.__setattr__(self, "tasks", tasks)

    def replace_with_claim(self, claim: TaskClaim) -> _WorkflowSnapshot:
        """用已提交 Claim 更新最近快照，不读取或推测后续数据库状态。"""

        if (
            not isinstance(claim, TaskClaim)
            or claim.workflow.workflow_id != self.workflow.workflow_id
            or claim.task.workflow_id != self.workflow.workflow_id
            or claim.task.status is not TaskStatus.RUNNING
        ):
            raise WorkflowRunnerError(
                "workflow reservation returned an invalid task claim"
            )
        replaced = False
        updated_tasks: list[TaskRecord] = []
        for task in self.tasks:
            if task.task_id == claim.task.task_id:
                updated_tasks.append(claim.task)
                replaced = True
            else:
                updated_tasks.append(task)
        if not replaced:
            raise WorkflowRunnerError(
                "workflow reservation returned an unknown task"
            )
        return _WorkflowSnapshot(
            workflow=claim.workflow,
            tasks=tuple(updated_tasks),
        )

    def replace_workflow(
        self,
        workflow: WorkflowRecord,
    ) -> _WorkflowSnapshot:
        """保留最近 Task 计数，同时记录已确认提交的新 Workflow 状态。"""

        if (
            not isinstance(workflow, WorkflowRecord)
            or workflow.workflow_id != self.workflow.workflow_id
        ):
            raise WorkflowRunnerError(
                "workflow service returned an inconsistent workflow record"
            )
        return _WorkflowSnapshot(workflow=workflow, tasks=self.tasks)


class CentralWorkflowRunner:
    """一次只向同步 Worker 推送一个由中央原子保留的 Task。"""

    __slots__ = (
        "_lease_seconds",
        "_max_steps",
        "_runner_id",
        "_service",
        "_task_executor",
    )

    def __init__(
        self,
        *,
        service: OrchestrationService,
        task_executor: ClaimedTaskExecutor,
        runner_id: str,
        lease_seconds: float,
        max_steps: int,
    ) -> None:
        if not isinstance(service, OrchestrationService):
            raise WorkflowRunnerValidationError(
                "service must be an OrchestrationService"
            )
        if not callable(getattr(task_executor, "execute_claim", None)):
            raise WorkflowRunnerValidationError(
                "task_executor must provide execute_claim()"
            )
        self._service = service
        self._task_executor = task_executor
        self._runner_id = self._require_text(
            runner_id,
            "runner_id",
            maximum=_MAX_RUNNER_ID_LENGTH,
        )
        self._lease_seconds = self._require_lease_seconds(lease_seconds)
        if type(max_steps) is not int or not 1 <= max_steps <= _MAX_STEPS:
            raise WorkflowRunnerValidationError(
                "max_steps must be a positive integer within its limit"
            )
        self._max_steps = max_steps

    def run(
        self,
        workflow_id: str,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
    ) -> WorkflowExecutionResult:
        """顺序推动一个已有 Workflow，且只依据重新读取的持久化状态停止。"""

        normalized_workflow_id = self._validate_run_inputs(
            workflow_id=workflow_id,
            cancel_checker=cancel_checker,
            parent_run_id=parent_run_id,
            tool_context=tool_context,
        )
        executed_task_ids: list[str] = []
        last_task_outcome: TaskExecutionOutcome | None = None

        # 先建立真实快照，确保后续只读失败仍能构造类型完整的 Result。
        try:
            snapshot = self._read_snapshot(normalized_workflow_id)
        except OrchestrationNotFoundError:
            raise
        except WorkflowRunnerError:
            raise
        except Exception as exc:
            raise WorkflowRunnerError(
                "initial workflow state could not be read"
            ) from exc

        terminal_result = self._terminal_result(
            snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
        )
        if terminal_result is not None:
            return terminal_result

        for _step in range(self._max_steps):
            if self._is_cancelled(cancel_checker):
                return self._cancel_workflow(
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )

            try:
                snapshot = self._read_snapshot(normalized_workflow_id)
            except WorkflowRunnerError:
                raise
            except Exception:
                return self._result(
                    WorkflowExecutionKind.UNAVAILABLE,
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                    error_type="orchestration_read_unavailable",
                    error_message="workflow state could not be read",
                )

            terminal_result = self._terminal_result(
                snapshot,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
            )
            if terminal_result is not None:
                return terminal_result
            if self._count(snapshot, TaskStatus.RUNNING):
                return self._active_state_result(
                    WorkflowExecutionKind.BUSY,
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )
            if not self._count(snapshot, TaskStatus.READY):
                return self._classify_inactive_frontier(
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )

            try:
                claim = self._service.reserve_next_ready_task(
                    workflow_id=normalized_workflow_id,
                    owner_id=self._runner_id,
                    lease_seconds=self._lease_seconds,
                )
            except OrchestrationValidationError as exc:
                raise WorkflowRunnerError(
                    "workflow reservation arguments were rejected"
                ) from exc
            except Exception:
                return self._result(
                    WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                    error_type="persistence_unknown",
                    error_message="workflow reservation outcome is unknown",
                )

            if claim is None:
                try:
                    snapshot = self._read_snapshot(normalized_workflow_id)
                except WorkflowRunnerError:
                    raise
                except Exception:
                    return self._result(
                        WorkflowExecutionKind.UNAVAILABLE,
                        snapshot,
                        executed_task_ids=executed_task_ids,
                        last_task_outcome=last_task_outcome,
                        error_type="orchestration_read_unavailable",
                        error_message="workflow state could not be read",
                    )
                terminal_result = self._terminal_result(
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )
                if terminal_result is not None:
                    return terminal_result
                if (
                    self._count(snapshot, TaskStatus.RUNNING)
                    or self._count(snapshot, TaskStatus.READY)
                ):
                    return self._active_state_result(
                        WorkflowExecutionKind.BUSY,
                        snapshot,
                        executed_task_ids=executed_task_ids,
                        last_task_outcome=last_task_outcome,
                    )
                return self._classify_inactive_frontier(
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )

            claim_snapshot = snapshot.replace_with_claim(claim)
            if claim.task.claim_owner != self._runner_id:
                raise WorkflowRunnerError(
                    "workflow reservation returned an invalid owner"
                )
            try:
                outcome = self._task_executor.execute_claim(
                    claim,
                    cancel_checker=cancel_checker,
                    hook_registry=hook_registry,
                    parent_run_id=parent_run_id,
                    tool_context=tool_context,
                )
            except Exception as exc:
                raise WorkflowRunnerError(
                    "task executor did not return a stable outcome"
                ) from exc
            self._validate_outcome(claim, outcome)
            executed_task_ids.append(outcome.task_id)
            last_task_outcome = outcome

            if outcome.kind is TaskExecutionOutcomeKind.CLAIM_LOST:
                return self._result(
                    WorkflowExecutionKind.CLAIM_LOST,
                    claim_snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                    error_type="claim_lost",
                    error_message="task claim is no longer current",
                )
            if outcome.kind is TaskExecutionOutcomeKind.PERSISTENCE_UNKNOWN:
                return self._result(
                    WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                    claim_snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                    error_type="persistence_unknown",
                    error_message="task persistence outcome is unknown",
                )
            if outcome.kind is TaskExecutionOutcomeKind.RELEASED:
                if self._is_cancelled(cancel_checker):
                    return self._cancel_workflow(
                        claim_snapshot,
                        executed_task_ids=executed_task_ids,
                        last_task_outcome=last_task_outcome,
                    )
                try:
                    snapshot = self._read_snapshot(normalized_workflow_id)
                except WorkflowRunnerError:
                    raise
                except Exception:
                    return self._result(
                        WorkflowExecutionKind.UNAVAILABLE,
                        claim_snapshot,
                        executed_task_ids=executed_task_ids,
                        last_task_outcome=last_task_outcome,
                        error_type="orchestration_read_unavailable",
                        error_message="workflow state could not be read",
                    )
                terminal_result = self._terminal_result(
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )
                if terminal_result is not None:
                    return terminal_result
                return self._active_state_result(
                    WorkflowExecutionKind.RETRY_LATER,
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )

            try:
                snapshot = self._read_snapshot(normalized_workflow_id)
            except WorkflowRunnerError:
                raise
            except Exception:
                return self._result(
                    WorkflowExecutionKind.UNAVAILABLE,
                    claim_snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                    error_type="orchestration_read_unavailable",
                    error_message="workflow state could not be read",
                )
            terminal_result = self._terminal_result(
                snapshot,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
            )
            if terminal_result is not None:
                return terminal_result
            if outcome.kind in {
                TaskExecutionOutcomeKind.COMPLETED,
                TaskExecutionOutcomeKind.FAILED,
            }:
                continue
            if outcome.kind is TaskExecutionOutcomeKind.BLOCKED:
                if self._count(snapshot, TaskStatus.RUNNING):
                    return self._active_state_result(
                        WorkflowExecutionKind.BUSY,
                        snapshot,
                        executed_task_ids=executed_task_ids,
                        last_task_outcome=last_task_outcome,
                    )
                if self._count(snapshot, TaskStatus.READY):
                    continue
                return self._classify_inactive_frontier(
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                )
            raise WorkflowRunnerError(
                "task executor returned an unsupported outcome"
            )

        try:
            snapshot = self._read_snapshot(normalized_workflow_id)
        except WorkflowRunnerError:
            raise
        except Exception:
            return self._result(
                WorkflowExecutionKind.UNAVAILABLE,
                snapshot,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
                error_type="orchestration_read_unavailable",
                error_message="workflow state could not be read",
            )
        terminal_result = self._terminal_result(
            snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
        )
        if terminal_result is not None:
            return terminal_result
        return self._active_state_result(
            WorkflowExecutionKind.STEP_LIMIT_REACHED,
            snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
        )

    def _read_snapshot(self, workflow_id: str) -> _WorkflowSnapshot:
        workflow = self._service.get_workflow(workflow_id)
        tasks = self._service.list_workflow_tasks(workflow_id)
        return _WorkflowSnapshot(workflow=workflow, tasks=tasks)

    def _cancel_workflow(
        self,
        snapshot: _WorkflowSnapshot,
        *,
        executed_task_ids: list[str],
        last_task_outcome: TaskExecutionOutcome | None,
    ) -> WorkflowExecutionResult:
        try:
            workflow = self._service.cancel_workflow(
                workflow_id=snapshot.workflow.workflow_id
            )
        except OrchestrationValidationError as exc:
            raise WorkflowRunnerValidationError(
                "workflow cancellation arguments are invalid"
            ) from exc
        except OrchestrationConflictError:
            try:
                refreshed = self._read_snapshot(snapshot.workflow.workflow_id)
            except WorkflowRunnerError:
                raise
            except Exception:
                return self._result(
                    WorkflowExecutionKind.UNAVAILABLE,
                    snapshot,
                    executed_task_ids=executed_task_ids,
                    last_task_outcome=last_task_outcome,
                    error_type="orchestration_read_unavailable",
                    error_message="workflow state could not be read",
                )
            terminal_result = self._terminal_result(
                refreshed,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
            )
            if terminal_result is not None:
                return terminal_result
            return self._result(
                WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                refreshed,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
                error_type="persistence_unknown",
                error_message="workflow cancellation outcome is unknown",
            )
        except Exception:
            return self._result(
                WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                snapshot,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
                error_type="persistence_unknown",
                error_message="workflow cancellation outcome is unknown",
            )
        committed_snapshot = snapshot.replace_workflow(workflow)
        if workflow.status is not WorkflowStatus.CANCELLED:
            raise WorkflowRunnerError(
                "workflow cancellation returned a non-cancelled record"
            )
        try:
            committed_snapshot = self._read_snapshot(workflow.workflow_id)
        except WorkflowRunnerError:
            raise
        except Exception:
            pass
        return self._result(
            WorkflowExecutionKind.CANCELLED,
            committed_snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
            error_type=None,
            error_message=None,
        )

    def _classify_inactive_frontier(
        self,
        snapshot: _WorkflowSnapshot,
        *,
        executed_task_ids: list[str],
        last_task_outcome: TaskExecutionOutcome | None,
    ) -> WorkflowExecutionResult:
        if self._count(snapshot, TaskStatus.BLOCKED):
            return self._active_state_result(
                WorkflowExecutionKind.BLOCKED,
                snapshot,
                executed_task_ids=executed_task_ids,
                last_task_outcome=last_task_outcome,
            )
        return self._active_state_result(
            WorkflowExecutionKind.STALLED,
            snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
        )

    def _terminal_result(
        self,
        snapshot: _WorkflowSnapshot,
        *,
        executed_task_ids: list[str],
        last_task_outcome: TaskExecutionOutcome | None,
    ) -> WorkflowExecutionResult | None:
        kind_by_status = {
            WorkflowStatus.COMPLETED: WorkflowExecutionKind.COMPLETED,
            WorkflowStatus.FAILED: WorkflowExecutionKind.FAILED,
            WorkflowStatus.CANCELLED: WorkflowExecutionKind.CANCELLED,
        }
        kind = kind_by_status.get(snapshot.workflow.status)
        if kind is None:
            return None
        error_type = "workflow_failed" if kind is WorkflowExecutionKind.FAILED else None
        error_message = "workflow is failed" if error_type is not None else None
        return self._result(
            kind,
            snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
            error_type=error_type,
            error_message=error_message,
        )

    def _active_state_result(
        self,
        kind: WorkflowExecutionKind,
        snapshot: _WorkflowSnapshot,
        *,
        executed_task_ids: list[str],
        last_task_outcome: TaskExecutionOutcome | None,
    ) -> WorkflowExecutionResult:
        errors = {
            WorkflowExecutionKind.BLOCKED: (
                "workflow_blocked",
                "workflow has blocked tasks",
            ),
            WorkflowExecutionKind.RETRY_LATER: (
                "retry_later",
                "a task claim was safely released",
            ),
            WorkflowExecutionKind.BUSY: (
                "workflow_busy",
                "workflow has another active execution",
            ),
            WorkflowExecutionKind.STALLED: (
                "workflow_stalled",
                "workflow has no executable task frontier",
            ),
            WorkflowExecutionKind.STEP_LIMIT_REACHED: (
                "step_limit_reached",
                "workflow execution reached its step limit",
            ),
        }
        error_type, error_message = errors[kind]
        return self._result(
            kind,
            snapshot,
            executed_task_ids=executed_task_ids,
            last_task_outcome=last_task_outcome,
            error_type=error_type,
            error_message=error_message,
        )

    @staticmethod
    def _result(
        kind: WorkflowExecutionKind,
        snapshot: _WorkflowSnapshot,
        *,
        executed_task_ids: list[str],
        last_task_outcome: TaskExecutionOutcome | None,
        error_type: str | None,
        error_message: str | None,
    ) -> WorkflowExecutionResult:
        counts = {
            status: sum(task.status is status for task in snapshot.tasks)
            for status in TaskStatus
        }
        try:
            return WorkflowExecutionResult(
                kind=kind,
                workflow_id=snapshot.workflow.workflow_id,
                workflow_status=snapshot.workflow.status,
                executed_task_ids=tuple(executed_task_ids),
                last_task_outcome=last_task_outcome,
                total_tasks=len(snapshot.tasks),
                todo_tasks=counts[TaskStatus.TODO],
                ready_tasks=counts[TaskStatus.READY],
                running_tasks=counts[TaskStatus.RUNNING],
                blocked_tasks=counts[TaskStatus.BLOCKED],
                completed_tasks=counts[TaskStatus.COMPLETED],
                failed_tasks=counts[TaskStatus.FAILED],
                cancelled_tasks=counts[TaskStatus.CANCELLED],
                error_type=error_type,
                error_message=error_message,
            )
        except Exception as exc:
            raise WorkflowRunnerError(
                "workflow execution result could not be constructed"
            ) from exc

    @staticmethod
    def _count(snapshot: _WorkflowSnapshot, status: TaskStatus) -> int:
        return sum(task.status is status for task in snapshot.tasks)

    @staticmethod
    def _validate_outcome(
        claim: TaskClaim,
        outcome: object,
    ) -> None:
        if (
            not isinstance(outcome, TaskExecutionOutcome)
            or outcome.workflow_id != claim.workflow.workflow_id
            or outcome.task_id != claim.task.task_id
            or outcome.run_id != claim.run.run_id
        ):
            raise WorkflowRunnerError(
                "task executor returned an invalid execution outcome"
            )

    @staticmethod
    def _is_cancelled(
        cancel_checker: Callable[[], bool] | None,
    ) -> bool:
        if cancel_checker is None:
            return False
        try:
            return bool(cancel_checker())
        except Exception as exc:
            raise WorkflowRunnerError("cancel_checker failed") from exc

    @classmethod
    def _validate_run_inputs(
        cls,
        *,
        workflow_id: object,
        cancel_checker: object,
        parent_run_id: object,
        tool_context: object,
    ) -> str:
        normalized_workflow_id = cls._require_text(
            workflow_id,
            "workflow_id",
            maximum=_MAX_WORKFLOW_ID_LENGTH,
        )
        if cancel_checker is not None and not callable(cancel_checker):
            raise WorkflowRunnerValidationError(
                "cancel_checker must be callable"
            )
        if parent_run_id is not None:
            cls._require_text(
                parent_run_id,
                "parent_run_id",
                maximum=_MAX_PARENT_RUN_ID_LENGTH,
            )
        if tool_context is not None and not isinstance(tool_context, Mapping):
            raise WorkflowRunnerValidationError(
                "tool_context must be a mapping"
            )
        return normalized_workflow_id

    @staticmethod
    def _require_text(
        value: object,
        field_name: str,
        *,
        maximum: int,
    ) -> str:
        if type(value) is not str or not value.strip():
            raise WorkflowRunnerValidationError(
                f"{field_name} must be a non-empty string"
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WorkflowRunnerValidationError(
                f"{field_name} must contain valid Unicode"
            ) from exc
        if len(value) > maximum:
            raise WorkflowRunnerValidationError(
                f"{field_name} exceeds its length limit"
            )
        return value

    @staticmethod
    def _require_lease_seconds(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkflowRunnerValidationError(
                "lease_seconds must be a finite positive number"
            )
        normalized = float(value)
        if (
            not math.isfinite(normalized)
            or normalized <= 0
            or normalized > _MAX_LEASE_SECONDS
        ):
            raise WorkflowRunnerValidationError(
                "lease_seconds must be a finite positive number within its limit"
            )
        return normalized


__all__ = ["CentralWorkflowRunner"]
