"""中心化、有限并行、Push 式 Workflow Runner。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from hermes.orchestration.errors import (
    OrchestrationConflictError,
    OrchestrationNotFoundError,
    OrchestrationValidationError,
    TaskClaimLostError,
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
    TaskStatus,
    WorkflowStatus,
)
from hermes.orchestration.service import OrchestrationService
from hermes.orchestration.workflow_execution import (
    WorkflowExecutionKind,
    WorkflowExecutionResult,
    WorkflowExecutionSnapshot,
    WorkflowTaskExecutionHandle,
    WorkflowTaskExecutionPool,
    WorkflowTaskExecutionPoolFactory,
)


_MAX_RUNNER_ID_LENGTH = 256
_MAX_WORKFLOW_ID_LENGTH = 128
_MAX_PARENT_RUN_ID_LENGTH = 512
_MAX_CONCURRENCY = 16
_MAX_LEASE_SECONDS = 86_400.0
_MAX_POLL_INTERVAL_SECONDS = 60.0
_MAX_STEPS = 10_000

_STOP_PRIORITY = {
    WorkflowExecutionKind.PERSISTENCE_UNKNOWN: 100,
    WorkflowExecutionKind.CANCELLED: 90,
    WorkflowExecutionKind.CLAIM_LOST: 80,
    WorkflowExecutionKind.FAILED: 70,
    WorkflowExecutionKind.COMPLETED: 60,
    WorkflowExecutionKind.UNAVAILABLE: 55,
    WorkflowExecutionKind.RETRY_LATER: 50,
    WorkflowExecutionKind.BLOCKED: 40,
    WorkflowExecutionKind.BUSY: 30,
    WorkflowExecutionKind.STEP_LIMIT_REACHED: 20,
    WorkflowExecutionKind.STALLED: 10,
}

_DEFAULT_ERRORS = {
    WorkflowExecutionKind.FAILED: (
        "workflow_failed",
        "workflow is failed",
    ),
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
    WorkflowExecutionKind.CLAIM_LOST: (
        "claim_lost",
        "a task claim is no longer current",
    ),
    WorkflowExecutionKind.UNAVAILABLE: (
        "orchestration_read_unavailable",
        "workflow state could not be read",
    ),
    WorkflowExecutionKind.PERSISTENCE_UNKNOWN: (
        "persistence_unknown",
        "workflow persistence outcome is unknown",
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


@dataclass(slots=True)
class _ActiveTaskExecution:
    """一次 run() 内部持有的本地 Worker 与 Claim 租约记录。"""

    claim: TaskClaim
    handle: WorkflowTaskExecutionHandle
    next_renew_at: float
    submission_order: int


@dataclass(slots=True)
class _RunState:
    """单次同步调用的瞬时调度状态，不替代 SQLite 事实。"""

    workflow_id: str
    snapshot: WorkflowExecutionSnapshot | None = None
    snapshot_fresh: bool = False
    scheduled_task_ids: list[str] = field(default_factory=list)
    task_outcomes: list[TaskExecutionOutcome] = field(default_factory=list)
    active: dict[str, _ActiveTaskExecution] = field(default_factory=dict)
    stop_kind: WorkflowExecutionKind | None = None
    error_type: str | None = None
    error_message: str | None = None
    refresh_degraded: bool = False
    cancel_requested: bool = False
    cancel_attempted: bool = False

    @property
    def submitted_steps(self) -> int:
        return len(self.scheduled_task_ids)

    def propose_stop(
        self,
        kind: WorkflowExecutionKind,
        *,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        current_priority = (
            -1 if self.stop_kind is None else _STOP_PRIORITY[self.stop_kind]
        )
        if _STOP_PRIORITY[kind] <= current_priority:
            return
        default_error = _DEFAULT_ERRORS.get(kind, (None, None))
        self.stop_kind = kind
        self.error_type = (
            default_error[0] if error_type is None else error_type
        )
        self.error_message = (
            default_error[1] if error_message is None else error_message
        )


class CentralWorkflowRunner:
    """中央保留 Task、推送 Worker，并集中管理有限并行与租约。"""

    __slots__ = (
        "_clock",
        "_lease_seconds",
        "_max_concurrency",
        "_max_steps",
        "_poll_interval_seconds",
        "_pool_factory",
        "_renew_interval_seconds",
        "_runner_id",
        "_service",
        "_sleeper",
        "_task_executor",
    )

    def __init__(
        self,
        *,
        service: OrchestrationService,
        task_executor: ClaimedTaskExecutor,
        pool_factory: WorkflowTaskExecutionPoolFactory,
        runner_id: str,
        max_concurrency: int,
        lease_seconds: float,
        renew_interval_seconds: float,
        poll_interval_seconds: float,
        max_steps: int,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], object] = time.sleep,
    ) -> None:
        if not isinstance(service, OrchestrationService):
            raise WorkflowRunnerValidationError(
                "service must be an OrchestrationService"
            )
        if not callable(getattr(task_executor, "execute_claim", None)):
            raise WorkflowRunnerValidationError(
                "task_executor must provide execute_claim()"
            )
        if not callable(getattr(pool_factory, "create", None)):
            raise WorkflowRunnerValidationError(
                "pool_factory must provide create()"
            )
        if not callable(clock):
            raise WorkflowRunnerValidationError("clock must be callable")
        if not callable(sleeper):
            raise WorkflowRunnerValidationError("sleeper must be callable")
        if (
            type(max_concurrency) is not int
            or not 1 <= max_concurrency <= _MAX_CONCURRENCY
        ):
            raise WorkflowRunnerValidationError(
                "max_concurrency must be a positive integer within its limit"
            )
        if type(max_steps) is not int or not 1 <= max_steps <= _MAX_STEPS:
            raise WorkflowRunnerValidationError(
                "max_steps must be a positive integer within its limit"
            )

        normalized_lease = self._require_positive_number(
            lease_seconds,
            "lease_seconds",
            maximum=_MAX_LEASE_SECONDS,
        )
        normalized_renew = self._require_positive_number(
            renew_interval_seconds,
            "renew_interval_seconds",
            maximum=_MAX_LEASE_SECONDS,
        )
        if normalized_renew >= normalized_lease:
            raise WorkflowRunnerValidationError(
                "renew_interval_seconds must be less than lease_seconds"
            )

        self._service = service
        self._task_executor = task_executor
        self._pool_factory = pool_factory
        self._runner_id = self._require_text(
            runner_id,
            "runner_id",
            maximum=_MAX_RUNNER_ID_LENGTH,
        )
        self._max_concurrency = max_concurrency
        self._lease_seconds = normalized_lease
        self._renew_interval_seconds = normalized_renew
        self._poll_interval_seconds = self._require_positive_number(
            poll_interval_seconds,
            "poll_interval_seconds",
            maximum=_MAX_POLL_INTERVAL_SECONDS,
        )
        self._max_steps = max_steps
        self._clock = clock
        self._sleeper = sleeper

    def run(
        self,
        workflow_id: str,
        *,
        cancel_checker: Callable[[], bool] | None = None,
        hook_registry: object | None = None,
        parent_run_id: str | None = None,
        tool_context: Mapping[str, object] | None = None,
    ) -> WorkflowExecutionResult:
        """有限并行推动已有 Workflow，并在退出前等待本地 Worker。"""

        normalized_workflow_id = self._validate_run_inputs(
            workflow_id=workflow_id,
            cancel_checker=cancel_checker,
            parent_run_id=parent_run_id,
            tool_context=tool_context,
        )
        try:
            pool = self._pool_factory.create(
                task_executor=self._task_executor,
                max_workers=self._max_concurrency,
            )
        except Exception as exc:
            raise WorkflowRunnerError(
                "workflow task execution pool could not be created"
            ) from exc
        if (
            not callable(getattr(pool, "submit", None))
            or not callable(getattr(pool, "close", None))
        ):
            close = getattr(pool, "close", None)
            if callable(close):
                try:
                    close(wait=True)
                except Exception:
                    pass
            raise WorkflowRunnerError(
                "pool_factory returned an invalid execution pool"
            )

        try:
            return self._run_with_pool(
                workflow_id=normalized_workflow_id,
                pool=pool,
                cancel_checker=cancel_checker,
                hook_registry=hook_registry,
                parent_run_id=parent_run_id,
                tool_context=tool_context,
            )
        finally:
            try:
                pool.close(wait=True)
            except Exception as exc:
                raise WorkflowRunnerError(
                    "workflow task execution pool could not be closed"
                ) from exc

    def _run_with_pool(
        self,
        *,
        workflow_id: str,
        pool: WorkflowTaskExecutionPool,
        cancel_checker: Callable[[], bool] | None,
        hook_registry: object | None,
        parent_run_id: str | None,
        tool_context: Mapping[str, object] | None,
    ) -> WorkflowExecutionResult:
        state = _RunState(workflow_id=workflow_id)
        try:
            state.snapshot = self._service.get_workflow_execution_snapshot(
                workflow_id=workflow_id
            )
            state.snapshot_fresh = True
        except OrchestrationNotFoundError:
            raise
        except Exception:
            state.propose_stop(WorkflowExecutionKind.UNAVAILABLE)
            return self._build_result(state)
        if not isinstance(state.snapshot, WorkflowExecutionSnapshot):
            raise WorkflowRunnerError(
                "workflow service returned an invalid execution snapshot"
            )
        if state.snapshot.workflow.status is not WorkflowStatus.ACTIVE:
            self._observe_snapshot(state)
            return self._build_result(state)

        # 活动 Worker 可能等待不可取消的系统调用，因此使用状态约束循环，
        # 不使用会绕过协作式收尾的固定墙钟超时。
        while state.active or state.stop_kind is None:
            if (
                state.stop_kind is not WorkflowExecutionKind.PERSISTENCE_UNKNOWN
                and self._is_cancelled(cancel_checker)
            ):
                self._begin_workflow_cancel(state)

            self._renew_due_claims(state)
            released_completed = self._collect_completed(state)
            if (
                released_completed
                and not state.cancel_attempted
                and self._is_cancelled(cancel_checker)
            ):
                self._begin_workflow_cancel(state)

            if state.stop_kind is WorkflowExecutionKind.PERSISTENCE_UNKNOWN:
                self._request_cancel_all(state)
                if not state.active:
                    return self._build_result(state)
                self._sleep_for_workers(state)
                continue

            if not self._refresh_snapshot(state):
                if not state.active:
                    state.propose_stop(WorkflowExecutionKind.UNAVAILABLE)
                    return self._build_result(state)
                state.refresh_degraded = True
                self._sleep_for_workers(state)
                continue

            self._observe_snapshot(state)
            if state.stop_kind is not None:
                if state.stop_kind in {
                    WorkflowExecutionKind.CANCELLED,
                    WorkflowExecutionKind.CLAIM_LOST,
                    WorkflowExecutionKind.COMPLETED,
                }:
                    self._request_cancel_all(state)
                if not state.active:
                    return self._build_result(state)
                self._sleep_for_workers(state)
                continue

            if state.refresh_degraded:
                if state.active:
                    self._sleep_for_workers(state)
                    continue
                state.refresh_degraded = False

            if state.cancel_requested:
                if state.active:
                    self._sleep_for_workers(state)
                    continue
                state.propose_stop(WorkflowExecutionKind.RETRY_LATER)
                return self._build_result(state)

            if state.submitted_steps >= self._max_steps:
                state.propose_stop(
                    WorkflowExecutionKind.STEP_LIMIT_REACHED
                )
                if not state.active:
                    return self._build_result(state)
                self._sleep_for_workers(state)
                continue

            snapshot = self._require_snapshot(state)
            available_slots = min(
                self._max_concurrency - len(state.active),
                self._max_steps - state.submitted_steps,
            )
            if available_slots > 0 and snapshot.ready_tasks > 0:
                next_renew_at = (
                    self._clock_now() + self._renew_interval_seconds
                )
                claims = self._reserve_ready_tasks(
                    state,
                    limit=available_slots,
                )
                if claims is None:
                    if state.active:
                        self._sleep_for_workers(state)
                        continue
                    return self._build_result(state)
                if claims:
                    self._submit_claims(
                        state,
                        pool=pool,
                        claims=claims,
                        cancel_checker=cancel_checker,
                        hook_registry=hook_registry,
                        parent_run_id=parent_run_id,
                        tool_context=tool_context,
                        next_renew_at=next_renew_at,
                    )
                    if state.submitted_steps >= self._max_steps:
                        state.propose_stop(
                            WorkflowExecutionKind.STEP_LIMIT_REACHED
                        )
                    if state.stop_kind is not None:
                        self._request_cancel_for_unsafe_stop(state)
                    if state.active:
                        self._sleep_for_workers(state)
                        continue
                    return self._build_result(state)

                if not self._refresh_snapshot(state):
                    if state.active:
                        state.refresh_degraded = True
                        self._sleep_for_workers(state)
                        continue
                    state.propose_stop(WorkflowExecutionKind.UNAVAILABLE)
                    return self._build_result(state)
                self._observe_snapshot(state)
                if (
                    state.stop_kind is None
                    and self._require_snapshot(state).ready_tasks > 0
                ):
                    state.propose_stop(WorkflowExecutionKind.BUSY)

            if state.active:
                self._sleep_for_workers(state)
                continue
            self._classify_idle_state(state)
            return self._build_result(state)

        return self._build_result(state)

    def _reserve_ready_tasks(
        self,
        state: _RunState,
        *,
        limit: int,
    ) -> tuple[TaskClaim, ...] | None:
        try:
            claims = self._service.reserve_ready_tasks(
                workflow_id=state.workflow_id,
                owner_id=self._runner_id,
                limit=limit,
                lease_seconds=self._lease_seconds,
            )
        except OrchestrationValidationError as exc:
            raise WorkflowRunnerError(
                "workflow reservation arguments were rejected"
            ) from exc
        except Exception:
            state.snapshot_fresh = False
            state.propose_stop(
                WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                error_message="workflow reservation outcome is unknown",
            )
            self._request_cancel_all(state)
            return None
        if not isinstance(claims, (list, tuple)) or len(claims) > limit:
            raise WorkflowRunnerError(
                "workflow reservation returned an invalid claim collection"
            )
        normalized = tuple(claims)
        seen_task_ids: set[str] = set()
        seen_run_ids: set[str] = set()
        active_task_ids = {
            active.claim.task.task_id for active in state.active.values()
        }
        for claim in normalized:
            if (
                not isinstance(claim, TaskClaim)
                or claim.workflow.workflow_id != state.workflow_id
                or claim.task.workflow_id != state.workflow_id
                or claim.task.status is not TaskStatus.RUNNING
                or claim.task.claim_owner != self._runner_id
                or claim.task.task_id in seen_task_ids
                or claim.run.run_id in seen_run_ids
                or claim.task.task_id in active_task_ids
                or claim.run.run_id in state.active
            ):
                raise WorkflowRunnerError(
                    "workflow reservation returned an invalid task claim"
                )
            seen_task_ids.add(claim.task.task_id)
            seen_run_ids.add(claim.run.run_id)
        if normalized:
            state.snapshot_fresh = False
        return normalized

    def _submit_claims(
        self,
        state: _RunState,
        *,
        pool: WorkflowTaskExecutionPool,
        claims: tuple[TaskClaim, ...],
        cancel_checker: Callable[[], bool] | None,
        hook_registry: object | None,
        parent_run_id: str | None,
        tool_context: Mapping[str, object] | None,
        next_renew_at: float,
    ) -> None:
        for claim in claims:
            try:
                handle = pool.submit(
                    claim,
                    hook_registry=hook_registry,
                    parent_run_id=parent_run_id,
                    tool_context=tool_context,
                    external_cancel_checker=cancel_checker,
                )
            except Exception:
                state.snapshot_fresh = False
                state.propose_stop(
                    WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                    error_type="task_execution_result_unknown",
                    error_message="task execution result is unknown",
                )
                self._request_cancel_all(state)
                return
            if not self._valid_handle(claim, handle):
                try:
                    handle.request_cancel()
                except Exception:
                    pass
                state.snapshot_fresh = False
                state.propose_stop(
                    WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                    error_type="task_execution_result_unknown",
                    error_message="task execution result is unknown",
                )
                self._request_cancel_all(state)
                return
            submission_order = len(state.scheduled_task_ids)
            state.scheduled_task_ids.append(claim.task.task_id)
            state.active[claim.run.run_id] = _ActiveTaskExecution(
                claim=claim,
                handle=handle,
                next_renew_at=next_renew_at,
                submission_order=submission_order,
            )

    def _renew_due_claims(self, state: _RunState) -> None:
        if not self._renew_allowed(state) or not state.active:
            return
        now = self._clock_now()
        for active in tuple(state.active.values()):
            if (
                self._handle_done(active.handle)
                or now < active.next_renew_at
            ):
                continue
            try:
                renewed = self._service.renew_task_claim(
                    task_id=active.claim.task.task_id,
                    claim_token=active.claim.claim_token,
                    lease_seconds=self._lease_seconds,
                )
            except TaskClaimLostError:
                state.propose_stop(WorkflowExecutionKind.CLAIM_LOST)
                self._request_cancel_all(state)
                return
            except OrchestrationValidationError as exc:
                raise WorkflowRunnerError(
                    "workflow claim renewal arguments were rejected"
                ) from exc
            except Exception:
                state.snapshot_fresh = False
                state.propose_stop(
                    WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                    error_message="task claim renewal outcome is unknown",
                )
                self._request_cancel_all(state)
                return
            if not self._same_claim(active.claim, renewed):
                raise WorkflowRunnerError(
                    "workflow claim renewal returned an invalid claim"
                )
            active.claim = renewed
            active.next_renew_at = now + self._renew_interval_seconds
            state.snapshot_fresh = False

    def _collect_completed(self, state: _RunState) -> bool:
        completed = [
            active
            for active in state.active.values()
            if self._handle_done(active.handle)
        ]
        completed.sort(key=self._completion_sort_key)
        released_completed = False
        for active in completed:
            state.active.pop(active.claim.run.run_id, None)
            try:
                outcome = active.handle.result()
            except Exception:
                outcome = self._unknown_outcome(active.claim)
            if not self._valid_outcome(active.claim, outcome):
                outcome = self._unknown_outcome(active.claim)
            state.task_outcomes.append(outcome)
            state.snapshot_fresh = False

            if outcome.kind is TaskExecutionOutcomeKind.RELEASED:
                released_completed = True
                state.propose_stop(WorkflowExecutionKind.RETRY_LATER)
            elif outcome.kind is TaskExecutionOutcomeKind.CLAIM_LOST:
                state.propose_stop(WorkflowExecutionKind.CLAIM_LOST)
                self._request_cancel_all(state)
            elif outcome.kind is TaskExecutionOutcomeKind.PERSISTENCE_UNKNOWN:
                state.propose_stop(
                    WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                    error_type=(
                        "task_execution_result_unknown"
                        if outcome.error_type == "task_execution_result_unknown"
                        else "persistence_unknown"
                    ),
                    error_message=(
                        "task execution result is unknown"
                        if outcome.error_type == "task_execution_result_unknown"
                        else "task persistence outcome is unknown"
                    ),
                )
                self._request_cancel_all(state)
        return released_completed

    def _begin_workflow_cancel(self, state: _RunState) -> None:
        state.cancel_requested = True
        self._request_cancel_all(state)
        if state.cancel_attempted:
            return
        state.cancel_attempted = True
        try:
            workflow = self._service.cancel_workflow(
                workflow_id=state.workflow_id
            )
        except OrchestrationValidationError as exc:
            raise WorkflowRunnerValidationError(
                "workflow cancellation arguments are invalid"
            ) from exc
        except OrchestrationConflictError:
            return
        except Exception:
            state.snapshot_fresh = False
            state.propose_stop(
                WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                error_message="workflow cancellation outcome is unknown",
            )
            return
        if workflow.status is not WorkflowStatus.CANCELLED:
            raise WorkflowRunnerError(
                "workflow cancellation returned a non-cancelled record"
            )
        # 最终 kind 仍由后续原子 Snapshot 确认，避免拼接写回 Record 与旧 Task。
        state.snapshot_fresh = False

    def _refresh_snapshot(self, state: _RunState) -> bool:
        try:
            snapshot = self._service.get_workflow_execution_snapshot(
                workflow_id=state.workflow_id
            )
        except Exception:
            state.snapshot_fresh = False
            return False
        if not isinstance(snapshot, WorkflowExecutionSnapshot):
            raise WorkflowRunnerError(
                "workflow service returned an invalid execution snapshot"
            )
        state.snapshot = snapshot
        state.snapshot_fresh = True
        return True

    def _observe_snapshot(self, state: _RunState) -> None:
        snapshot = self._require_snapshot(state)
        status = snapshot.workflow.status
        if status is WorkflowStatus.COMPLETED:
            state.propose_stop(WorkflowExecutionKind.COMPLETED)
            return
        if status is WorkflowStatus.FAILED:
            state.propose_stop(WorkflowExecutionKind.FAILED)
            return
        if status is WorkflowStatus.CANCELLED:
            state.propose_stop(WorkflowExecutionKind.CANCELLED)
            return

        local_task_ids = {
            active.claim.task.task_id for active in state.active.values()
        }
        has_external_running = any(
            task.status is TaskStatus.RUNNING
            and task.task_id not in local_task_ids
            for task in snapshot.tasks
        )
        if has_external_running:
            state.propose_stop(WorkflowExecutionKind.BUSY)

    def _classify_idle_state(self, state: _RunState) -> None:
        snapshot = self._require_snapshot(state)
        self._observe_snapshot(state)
        if state.stop_kind is not None:
            return
        if state.cancel_requested:
            state.propose_stop(WorkflowExecutionKind.RETRY_LATER)
            return
        if snapshot.running_tasks:
            state.propose_stop(WorkflowExecutionKind.BUSY)
        elif snapshot.ready_tasks:
            state.propose_stop(WorkflowExecutionKind.BUSY)
        elif snapshot.blocked_tasks:
            state.propose_stop(WorkflowExecutionKind.BLOCKED)
        else:
            state.propose_stop(WorkflowExecutionKind.STALLED)

    def _build_result(self, state: _RunState) -> WorkflowExecutionResult:
        kind = state.stop_kind
        if kind is None:
            raise WorkflowRunnerError(
                "workflow execution stopped without a stable result kind"
            )
        try:
            return WorkflowExecutionResult(
                kind=kind,
                workflow_id=state.workflow_id,
                snapshot=state.snapshot,
                snapshot_fresh=state.snapshot_fresh,
                scheduled_task_ids=tuple(state.scheduled_task_ids),
                task_outcomes=tuple(state.task_outcomes),
                last_task_outcome=(
                    state.task_outcomes[-1]
                    if state.task_outcomes
                    else None
                ),
                error_type=state.error_type,
                error_message=state.error_message,
            )
        except Exception as exc:
            raise WorkflowRunnerError(
                "workflow execution result could not be constructed"
            ) from exc

    def _sleep_for_workers(self, state: _RunState) -> None:
        if not state.active:
            return
        if any(
            self._handle_done(active.handle)
            for active in state.active.values()
        ):
            return
        delay = self._poll_interval_seconds
        if self._renew_allowed(state):
            now = self._clock_now()
            next_renew_at = min(
                active.next_renew_at for active in state.active.values()
            )
            delay = min(delay, max(0.0, next_renew_at - now))
        if delay <= 0:
            return
        try:
            self._sleeper(delay)
        except Exception as exc:
            raise WorkflowRunnerError("workflow runner sleeper failed") from exc

    def _request_cancel_for_unsafe_stop(self, state: _RunState) -> None:
        if state.stop_kind in {
            WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
            WorkflowExecutionKind.CANCELLED,
            WorkflowExecutionKind.CLAIM_LOST,
            WorkflowExecutionKind.COMPLETED,
        }:
            self._request_cancel_all(state)

    @staticmethod
    def _request_cancel_all(state: _RunState) -> None:
        for active in tuple(state.active.values()):
            try:
                active.handle.request_cancel()
            except Exception:
                continue

    @staticmethod
    def _renew_allowed(state: _RunState) -> bool:
        return (
            not state.cancel_requested
            and state.stop_kind not in {
                WorkflowExecutionKind.PERSISTENCE_UNKNOWN,
                WorkflowExecutionKind.CANCELLED,
                WorkflowExecutionKind.CLAIM_LOST,
                WorkflowExecutionKind.COMPLETED,
            }
        )

    @staticmethod
    def _handle_done(handle: WorkflowTaskExecutionHandle) -> bool:
        try:
            return bool(handle.done())
        except Exception:
            return True

    @staticmethod
    def _completion_sort_key(
        active: _ActiveTaskExecution,
    ) -> tuple[int, int]:
        try:
            completion_order = active.handle.completion_order
        except Exception:
            completion_order = None
        return (
            completion_order if type(completion_order) is int else 2**63,
            active.submission_order,
        )

    @staticmethod
    def _valid_handle(
        claim: TaskClaim,
        handle: object,
    ) -> bool:
        try:
            return (
                getattr(handle, "task_id", None) == claim.task.task_id
                and getattr(handle, "run_id", None) == claim.run.run_id
                and callable(getattr(handle, "done", None))
                and callable(getattr(handle, "result", None))
                and callable(getattr(handle, "request_cancel", None))
            )
        except Exception:
            return False

    @staticmethod
    def _valid_outcome(claim: TaskClaim, outcome: object) -> bool:
        return (
            isinstance(outcome, TaskExecutionOutcome)
            and outcome.workflow_id == claim.workflow.workflow_id
            and outcome.task_id == claim.task.task_id
            and outcome.run_id == claim.run.run_id
        )

    @staticmethod
    def _same_claim(original: TaskClaim, renewed: object) -> bool:
        return (
            isinstance(renewed, TaskClaim)
            and renewed.workflow.workflow_id == original.workflow.workflow_id
            and renewed.task.task_id == original.task.task_id
            and renewed.run.run_id == original.run.run_id
            and renewed.claim_token == original.claim_token
        )

    @staticmethod
    def _unknown_outcome(claim: TaskClaim) -> TaskExecutionOutcome:
        return TaskExecutionOutcome(
            kind=TaskExecutionOutcomeKind.PERSISTENCE_UNKNOWN,
            workflow_id=claim.workflow.workflow_id,
            task_id=claim.task.task_id,
            run_id=claim.run.run_id,
            session_key=None,
            runtime_status=None,
            summary=None,
            error_type="task_execution_result_unknown",
            error_message="task execution result is unknown",
            retryable=False,
            persisted=False,
        )

    @staticmethod
    def _require_snapshot(state: _RunState) -> WorkflowExecutionSnapshot:
        if state.snapshot is None:
            raise WorkflowRunnerError(
                "workflow execution requires a confirmed snapshot"
            )
        return state.snapshot

    def _clock_now(self) -> float:
        try:
            value = self._clock()
        except Exception as exc:
            raise WorkflowRunnerError("workflow runner clock failed") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkflowRunnerError(
                "workflow runner clock returned an invalid value"
            )
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise WorkflowRunnerError(
                "workflow runner clock returned an invalid value"
            )
        return normalized

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
    def _require_positive_number(
        value: object,
        field_name: str,
        *,
        maximum: float,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkflowRunnerValidationError(
                f"{field_name} must be a finite positive number"
            )
        normalized = float(value)
        if (
            not math.isfinite(normalized)
            or normalized <= 0
            or normalized > maximum
        ):
            raise WorkflowRunnerValidationError(
                f"{field_name} must be a finite positive number within its limit"
            )
        return normalized


__all__ = ["CentralWorkflowRunner"]
