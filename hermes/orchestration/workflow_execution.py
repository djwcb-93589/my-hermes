"""中心化 Workflow 执行的稳定快照、结果与并发端口。"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from hermes.orchestration.execution import (
    ClaimedTaskExecutor,
    TaskExecutionOutcome,
)
from hermes.orchestration.models import (
    TaskClaim,
    TaskRecord,
    TaskStatus,
    WorkflowRecord,
    WorkflowStatus,
)


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
class WorkflowExecutionSnapshot:
    """同一个数据库读事务捕获的 Workflow 与 Task 事实投影。"""

    workflow: WorkflowRecord
    tasks: tuple[TaskRecord, ...]
    captured_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, WorkflowRecord):
            raise TypeError("workflow must be a WorkflowRecord")
        if not isinstance(self.tasks, (list, tuple)):
            raise TypeError("tasks must be a sequence of TaskRecord values")
        if (
            isinstance(self.captured_at, bool)
            or not isinstance(self.captured_at, (int, float))
        ):
            raise TypeError("captured_at must be a finite timestamp")
        captured_at = float(self.captured_at)
        if not math.isfinite(captured_at) or captured_at < 0:
            raise ValueError("captured_at must be a finite non-negative timestamp")

        safe_tasks: list[TaskRecord] = []
        seen_task_ids: set[str] = set()
        for task in self.tasks:
            if not isinstance(task, TaskRecord):
                raise TypeError("tasks must contain only TaskRecord values")
            if task.workflow_id != self.workflow.workflow_id:
                raise ValueError("snapshot tasks must belong to one workflow")
            if task.task_id in seen_task_ids:
                raise ValueError("snapshot tasks must not contain duplicates")
            seen_task_ids.add(task.task_id)
            # Snapshot 只暴露状态观察所需事实，不携带内部 fencing token。
            safe_tasks.append(
                task
                if task.claim_token is None
                else replace(task, claim_token=None)
            )
        safe_tasks.sort(key=lambda task: (task.created_at, task.task_id))
        object.__setattr__(self, "tasks", tuple(safe_tasks))
        object.__setattr__(self, "captured_at", captured_at)

    def _count(self, status: TaskStatus) -> int:
        return sum(task.status is status for task in self.tasks)

    @property
    def total_tasks(self) -> int:
        return len(self.tasks)

    @property
    def todo_tasks(self) -> int:
        return self._count(TaskStatus.TODO)

    @property
    def ready_tasks(self) -> int:
        return self._count(TaskStatus.READY)

    @property
    def running_tasks(self) -> int:
        return self._count(TaskStatus.RUNNING)

    @property
    def blocked_tasks(self) -> int:
        return self._count(TaskStatus.BLOCKED)

    @property
    def completed_tasks(self) -> int:
        return self._count(TaskStatus.COMPLETED)

    @property
    def failed_tasks(self) -> int:
        return self._count(TaskStatus.FAILED)

    @property
    def cancelled_tasks(self) -> int:
        return self._count(TaskStatus.CANCELLED)


@dataclass(frozen=True, slots=True)
class WorkflowExecutionResult:
    """一次 Runner 调用的不可变报告，不把旧快照伪装成当前状态。"""

    kind: WorkflowExecutionKind
    workflow_id: str
    snapshot: WorkflowExecutionSnapshot | None
    snapshot_fresh: bool
    scheduled_task_ids: tuple[str, ...]
    task_outcomes: tuple[TaskExecutionOutcome, ...]
    last_task_outcome: TaskExecutionOutcome | None
    error_type: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        try:
            normalized_kind = WorkflowExecutionKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("workflow execution kind is invalid") from exc
        if type(self.workflow_id) is not str or not self.workflow_id:
            raise ValueError("workflow_id must be a non-empty string")
        if type(self.snapshot_fresh) is not bool:
            raise TypeError("snapshot_fresh must be a boolean")
        if self.snapshot is not None and not isinstance(
            self.snapshot,
            WorkflowExecutionSnapshot,
        ):
            raise TypeError(
                "snapshot must be a WorkflowExecutionSnapshot or None"
            )
        if self.snapshot is None and self.snapshot_fresh:
            raise ValueError("a missing snapshot cannot be marked fresh")
        if (
            self.snapshot is None
            and normalized_kind is not WorkflowExecutionKind.UNAVAILABLE
        ):
            raise ValueError("only unavailable results may omit the snapshot")
        if (
            self.snapshot is not None
            and self.snapshot.workflow.workflow_id != self.workflow_id
        ):
            raise ValueError("snapshot does not match workflow_id")
        if (
            normalized_kind is WorkflowExecutionKind.UNAVAILABLE
            and self.snapshot_fresh
        ):
            raise ValueError("unavailable results cannot have a fresh snapshot")

        scheduled_task_ids = self._normalize_task_ids(
            self.scheduled_task_ids,
        )
        if not isinstance(self.task_outcomes, (list, tuple)):
            raise TypeError("task_outcomes must be a sequence")
        task_outcomes = tuple(self.task_outcomes)
        if any(
            not isinstance(outcome, TaskExecutionOutcome)
            or outcome.workflow_id != self.workflow_id
            for outcome in task_outcomes
        ):
            raise ValueError(
                "task_outcomes must describe this workflow"
            )
        scheduled_counts = Counter(scheduled_task_ids)
        outcome_counts = Counter(
            outcome.task_id for outcome in task_outcomes
        )
        if any(
            count > scheduled_counts.get(task_id, 0)
            for task_id, count in outcome_counts.items()
        ):
            raise ValueError(
                "task_outcomes contain tasks that were not scheduled"
            )
        expected_last = task_outcomes[-1] if task_outcomes else None
        if self.last_task_outcome != expected_last:
            raise ValueError(
                "last_task_outcome must be the last completed outcome"
            )
        for field_name in ("error_type", "error_message"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not str:
                raise TypeError(f"{field_name} must be a string or None")

        if self.snapshot_fresh and self.snapshot is not None:
            expected_statuses = {
                WorkflowExecutionKind.COMPLETED: WorkflowStatus.COMPLETED,
                WorkflowExecutionKind.FAILED: WorkflowStatus.FAILED,
                WorkflowExecutionKind.CANCELLED: WorkflowStatus.CANCELLED,
            }
            expected_status = expected_statuses.get(normalized_kind)
            if (
                expected_status is not None
                and self.snapshot.workflow.status is not expected_status
            ):
                raise ValueError(
                    "terminal result does not match the fresh snapshot"
                )

        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "scheduled_task_ids", scheduled_task_ids)
        object.__setattr__(self, "task_outcomes", task_outcomes)

    @staticmethod
    def _normalize_task_ids(value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("scheduled_task_ids must be a sequence")
        task_ids = tuple(value)
        if any(type(task_id) is not str or not task_id for task_id in task_ids):
            raise ValueError("scheduled_task_ids contains an invalid task_id")
        return task_ids

    @property
    def executed_task_ids(self) -> tuple[str, ...]:
        """兼容 P3.1 名称；实际语义是中央提交顺序。"""

        return self.scheduled_task_ids

    @property
    def workflow_status(self) -> WorkflowStatus | None:
        return None if self.snapshot is None else self.snapshot.workflow.status

    @property
    def total_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.total_tasks

    @property
    def todo_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.todo_tasks

    @property
    def ready_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.ready_tasks

    @property
    def running_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.running_tasks

    @property
    def blocked_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.blocked_tasks

    @property
    def completed_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.completed_tasks

    @property
    def failed_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.failed_tasks

    @property
    def cancelled_tasks(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.cancelled_tasks


class WorkflowTaskExecutionHandle(Protocol):
    """不暴露 Future、线程或 claim token 的单 Task 执行句柄。"""

    @property
    def task_id(self) -> str:
        """返回已提交 Task 的稳定 ID。"""

    @property
    def run_id(self) -> str:
        """返回已提交 Run 的稳定 ID。"""

    @property
    def completion_order(self) -> int | None:
        """返回 Pool 内完成序号；尚未完成时返回 None。"""

    def done(self) -> bool:
        """报告 Worker 是否已经结束。"""

    def result(self) -> TaskExecutionOutcome:
        """返回稳定 Outcome；实现不得裸露 Future 异常。"""

    def request_cancel(self) -> None:
        """幂等发送协作式取消信号。"""


class WorkflowTaskExecutionPool(Protocol):
    """只并发调用 ClaimedTaskExecutor 的一次 run 局部资源。"""

    def submit(
        self,
        claim: TaskClaim,
        *,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
        external_cancel_checker: Callable[[], bool] | None = None,
    ) -> WorkflowTaskExecutionHandle:
        """提交已保留 Claim。

        成功返回 Handle；失败抛 WorkflowTaskSubmissionError，并由 accepted
        明确任务是否已进入执行接收边界。
        """

    def close(self, *, wait: bool) -> None:
        """幂等关闭 Pool；wait=True 时等待 Worker 协作式退出。"""


class WorkflowTaskExecutionPoolFactory(Protocol):
    """为每次 Runner 调用创建独立执行 Pool。"""

    def create(
        self,
        *,
        task_executor: ClaimedTaskExecutor,
        max_workers: int,
    ) -> WorkflowTaskExecutionPool:
        """创建不共享线程生命周期的执行 Pool。"""


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
        """有限并行执行 ready Task，直到 Workflow 达到明确停止条件。"""


__all__ = [
    "WorkflowExecutionKind",
    "WorkflowExecutionResult",
    "WorkflowExecutionSnapshot",
    "WorkflowRunner",
    "WorkflowTaskExecutionHandle",
    "WorkflowTaskExecutionPool",
    "WorkflowTaskExecutionPoolFactory",
]
