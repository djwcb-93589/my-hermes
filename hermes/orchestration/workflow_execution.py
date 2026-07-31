"""中心化 Workflow 顺序执行的稳定契约。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from hermes.orchestration.execution import TaskExecutionOutcome
from hermes.orchestration.models import WorkflowStatus


class WorkflowExecutionKind(StrEnum):
    """一次同步 Workflow Runner 调用的稳定结果分类。"""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    RETRY_LATER = "retry_later"
    BUSY = "busy"
    CLAIM_LOST = "claim_lost"
    UNAVAILABLE = "unavailable"
    PERSISTENCE_UNKNOWN = "persistence_unknown"
    STALLED = "stalled"
    STEP_LIMIT_REACHED = "step_limit_reached"


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    """一次同步 Runner 调用的不可变报告；SQLite 仍是状态真相。"""

    kind: WorkflowExecutionKind
    workflow_id: str
    workflow_status: WorkflowStatus
    executed_task_ids: tuple[str, ...]
    last_task_outcome: TaskExecutionOutcome | None
    total_tasks: int
    todo_tasks: int
    ready_tasks: int
    running_tasks: int
    blocked_tasks: int
    completed_tasks: int
    failed_tasks: int
    cancelled_tasks: int
    error_type: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        try:
            normalized_kind = WorkflowExecutionKind(self.kind)
            normalized_status = WorkflowStatus(self.workflow_status)
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow execution result status is invalid") from exc
        terminal_statuses = {
            WorkflowExecutionKind.COMPLETED: WorkflowStatus.COMPLETED,
            WorkflowExecutionKind.FAILED: WorkflowStatus.FAILED,
            WorkflowExecutionKind.CANCELLED: WorkflowStatus.CANCELLED,
        }
        expected_terminal_status = terminal_statuses.get(normalized_kind)
        if (
            expected_terminal_status is not None
            and normalized_status is not expected_terminal_status
        ):
            raise ValueError(
                "terminal workflow execution kind does not match workflow_status"
            )
        if (
            expected_terminal_status is None
            and normalized_status is not WorkflowStatus.ACTIVE
        ):
            raise ValueError(
                "non-terminal workflow execution kind requires active status"
            )
        if type(self.workflow_id) is not str or not self.workflow_id:
            raise ValueError("workflow_id must be a non-empty string")
        if not isinstance(self.executed_task_ids, (list, tuple)):
            raise TypeError("executed_task_ids must be a sequence")
        executed_task_ids = tuple(self.executed_task_ids)
        if any(
            type(task_id) is not str or not task_id
            for task_id in executed_task_ids
        ):
            raise ValueError("executed_task_ids contains an invalid task_id")
        if self.last_task_outcome is not None and not isinstance(
            self.last_task_outcome,
            TaskExecutionOutcome,
        ):
            raise TypeError(
                "last_task_outcome must be a TaskExecutionOutcome or None"
            )
        if bool(executed_task_ids) != (self.last_task_outcome is not None):
            raise ValueError(
                "last_task_outcome must match whether tasks were executed"
            )
        if self.last_task_outcome is not None and (
            self.last_task_outcome.workflow_id != self.workflow_id
            or self.last_task_outcome.task_id != executed_task_ids[-1]
        ):
            raise ValueError(
                "last_task_outcome does not match the execution sequence"
            )
        count_names = (
            "total_tasks",
            "todo_tasks",
            "ready_tasks",
            "running_tasks",
            "blocked_tasks",
            "completed_tasks",
            "failed_tasks",
            "cancelled_tasks",
        )
        for field_name in count_names:
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(
                    "workflow execution task counts must be non-negative integers"
                )
        classified_total = sum(
            getattr(self, field_name)
            for field_name in count_names[1:]
        )
        if self.total_tasks != classified_total:
            raise ValueError(
                "workflow execution task counts do not match total_tasks"
            )
        for field_name in ("error_type", "error_message"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field_name} must be a string or None")
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "workflow_status", normalized_status)
        object.__setattr__(self, "executed_task_ids", executed_task_ids)


class WorkflowRunner(Protocol):
    """同步运行一个已存在 Workflow 的中心化 Push 执行端口。"""

    def run(
        self,
        workflow_id: str,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
    ) -> WorkflowExecutionResult:
        """顺序执行 ready Task，直到 Workflow 达到明确停止条件。"""


__all__ = [
    "WorkflowExecutionKind",
    "WorkflowExecutionResult",
    "WorkflowRunner",
]
