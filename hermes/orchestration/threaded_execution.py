"""Workflow Task 的进程内线程执行适配器。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, RLock

from hermes.orchestration.errors import WorkflowTaskSubmissionError
from hermes.orchestration.execution import (
    ClaimedTaskExecutor,
    TaskExecutionOutcome,
    TaskExecutionOutcomeKind,
)
from hermes.orchestration.models import TaskClaim
from hermes.orchestration.workflow_execution import (
    WorkflowTaskExecutionHandle,
    WorkflowTaskExecutionPool,
)


_MAX_WORKERS = 16


class _ThreadedWorkflowTaskExecutionHandle:
    """把 Future 与取消 Event 收敛在不透明执行句柄之后。"""

    __slots__ = (
        "_cancel_event",
        "_completion_order",
        "_future",
        "_lock",
        "_run_id",
        "_task_id",
        "_workflow_id",
    )

    def __init__(self, claim: TaskClaim, cancel_event: Event) -> None:
        self._workflow_id = claim.workflow.workflow_id
        self._task_id = claim.task.task_id
        self._run_id = claim.run.run_id
        self._cancel_event = cancel_event
        self._future: Future[TaskExecutionOutcome] | None = None
        self._completion_order: int | None = None
        self._lock = RLock()

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def completion_order(self) -> int | None:
        with self._lock:
            return self._completion_order

    def bind_future(self, future: Future[TaskExecutionOutcome]) -> None:
        with self._lock:
            self._future = future

    def mark_completed(self, order: int) -> None:
        with self._lock:
            if self._completion_order is None:
                self._completion_order = order

    def done(self) -> bool:
        future = self._require_future()
        return self.completion_order is not None or future.cancelled()

    def result(self) -> TaskExecutionOutcome:
        try:
            outcome = self._require_future().result()
        except BaseException:
            return self._unknown_outcome()
        if (
            not isinstance(outcome, TaskExecutionOutcome)
            or outcome.workflow_id != self._workflow_id
            or outcome.task_id != self._task_id
            or outcome.run_id != self._run_id
        ):
            return self._unknown_outcome()
        return outcome

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def _require_future(self) -> Future[TaskExecutionOutcome]:
        with self._lock:
            future = self._future
        if future is None:
            raise RuntimeError("workflow task handle is not bound")
        return future

    def _unknown_outcome(self) -> TaskExecutionOutcome:
        """Worker 可能已有副作用，因此用未知持久化结果保守收敛。"""

        return TaskExecutionOutcome(
            kind=TaskExecutionOutcomeKind.PERSISTENCE_UNKNOWN,
            workflow_id=self._workflow_id,
            task_id=self._task_id,
            run_id=self._run_id,
            session_key=None,
            runtime_status=None,
            summary=None,
            error_type="task_execution_result_unknown",
            error_message="task execution result is unknown",
            retryable=False,
            persisted=False,
        )


class ThreadedWorkflowTaskExecutionPool:
    """一次 Runner 调用局部拥有的有限线程执行 Pool。"""

    __slots__ = (
        "_closed",
        "_completion_sequence",
        "_executor",
        "_handles",
        "_lock",
        "_task_executor",
    )

    def __init__(
        self,
        *,
        task_executor: ClaimedTaskExecutor,
        max_workers: int,
    ) -> None:
        if not callable(getattr(task_executor, "execute_claim", None)):
            raise TypeError("task_executor must provide execute_claim()")
        if type(max_workers) is not int or not 1 <= max_workers <= _MAX_WORKERS:
            raise ValueError(
                "max_workers must be a positive integer within its limit"
            )
        self._task_executor = task_executor
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="hermes-workflow-task",
        )
        self._handles: list[_ThreadedWorkflowTaskExecutionHandle] = []
        self._completion_sequence = 0
        self._closed = False
        self._lock = RLock()

    def submit(
        self,
        claim: TaskClaim,
        *,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
        external_cancel_checker: Callable[[], bool] | None = None,
    ) -> WorkflowTaskExecutionHandle:
        if not isinstance(claim, TaskClaim):
            raise WorkflowTaskSubmissionError(
                "workflow task submission input is invalid",
                accepted=False,
            )
        if (
            external_cancel_checker is not None
            and not callable(external_cancel_checker)
        ):
            raise WorkflowTaskSubmissionError(
                "workflow task cancellation input is invalid",
                accepted=False,
            )
        try:
            cancel_event = Event()
            admission_gate = Event()
            submission_accepted = Event()
            handle = _ThreadedWorkflowTaskExecutionHandle(
                claim,
                cancel_event,
            )
        except Exception as exc:
            raise WorkflowTaskSubmissionError(
                "workflow task submission could not be prepared",
                accepted=False,
            ) from exc

        def combined_cancel_checker() -> bool:
            if cancel_event.is_set():
                return True
            return (
                external_cancel_checker is not None
                and bool(external_cancel_checker())
            )

        def execute_claim() -> TaskExecutionOutcome:
            try:
                admission_gate.wait()
                if not submission_accepted.is_set():
                    # submit() 抛出时可能已入队，但绝不进入 Agent 执行边界。
                    return handle._unknown_outcome()
                return self._task_executor.execute_claim(
                    claim,
                    cancel_checker=combined_cancel_checker,
                    hook_registry=hook_registry,
                    parent_run_id=parent_run_id,
                    tool_context=tool_context,
                )
            finally:
                # 先分配完成序号，避免 Future 已 done 但顺序仍不可见。
                self._record_completion(handle)

        with self._lock:
            if self._closed:
                raise WorkflowTaskSubmissionError(
                    "workflow task execution pool is closed",
                    accepted=False,
                )
            try:
                self._handles.append(handle)
            except Exception as exc:
                raise WorkflowTaskSubmissionError(
                    "workflow task submission could not be prepared",
                    accepted=False,
                ) from exc
            try:
                future = self._executor.submit(execute_claim)
            except BaseException as exc:
                self._handles.pop()
                admission_gate.set()
                if isinstance(exc, Exception):
                    raise WorkflowTaskSubmissionError(
                        "workflow task was not accepted for execution",
                        accepted=False,
                    ) from exc
                raise
            handle.bind_future(future)
            submission_accepted.set()
            admission_gate.set()
        return handle

    def close(self, *, wait: bool) -> None:
        if type(wait) is not bool:
            raise TypeError("wait must be a boolean")
        with self._lock:
            self._closed = True
            handles = tuple(self._handles)
        for handle in handles:
            handle.request_cancel()
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _record_completion(
        self,
        handle: _ThreadedWorkflowTaskExecutionHandle,
    ) -> None:
        with self._lock:
            self._completion_sequence += 1
            order = self._completion_sequence
        handle.mark_completed(order)


class ThreadedWorkflowTaskExecutionPoolFactory:
    """显式创建不跨 run() 共享的线程 Pool。"""

    __slots__ = ()

    def create(
        self,
        *,
        task_executor: ClaimedTaskExecutor,
        max_workers: int,
    ) -> WorkflowTaskExecutionPool:
        return ThreadedWorkflowTaskExecutionPool(
            task_executor=task_executor,
            max_workers=max_workers,
        )


__all__ = [
    "ThreadedWorkflowTaskExecutionPool",
    "ThreadedWorkflowTaskExecutionPoolFactory",
]
