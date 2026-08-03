"""在 Claude Code Runtime 之上提供同步、有界且不自动审批的工作流编排。"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum

from hermes.claude_code.contracts import (
    CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
    ClaudeCodeActionKind,
    ClaudeCodeActionRequired,
    ClaudeCodeCurrentInteraction,
    ClaudeCodeEvent,
    ClaudeCodeEventType,
    ClaudeCodeInteractionResponse,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeRuntimeError,
    ClaudeCodeSnapshot,
    ClaudeCodeState,
    build_claude_code_action_id,
)
from hermes.claude_code.controller_policy import (
    ClaudeCodeControllerPolicy,
)
from hermes.claude_code.runtime import ClaudeCodeRuntime


_TERMINAL_STATES = frozenset(
    {
        ClaudeCodeState.COMPLETED,
        ClaudeCodeState.FAILED,
        ClaudeCodeState.INTERRUPTED,
        ClaudeCodeState.LOST,
    }
)
_MAX_RETAINED_INTERACTION_IDS = 128


class ClaudeCodeControllerOutcome(str, Enum):
    """说明一次 Controller 调用为何返回，不替代 Runtime 状态。"""

    RUNNING = "running"
    ACTION_REQUIRED = "action_required"
    TERMINAL = "terminal"
    TERMINATED = "terminated"
    INTERRUPT_PENDING = "interrupt_pending"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    OBSERVATION_LIMIT_REACHED = "observation_limit_reached"
    STALLED = "stalled"


class ClaudeCodeControllerError(ClaudeCodeRuntimeError):
    """保留 Runtime 结构化错误形态的 Controller 业务错误。"""


@dataclass(frozen=True, slots=True)
class ClaudeCodeControllerResult:
    """返回最新 Runtime Snapshot 与有界工作流计数。"""

    snapshot: ClaudeCodeSnapshot
    outcome: ClaudeCodeControllerOutcome
    observation_count: int
    consecutive_empty_reads: int
    # 仅统计本任务已观察到的原始 cursor 增量，不是输出预算。
    output_used: int
    deadline_remaining: float
    limits_hit: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ClaudeCodeSnapshot):
            raise ValueError("snapshot must be a ClaudeCodeSnapshot")
        if not isinstance(self.outcome, ClaudeCodeControllerOutcome):
            raise ValueError("outcome must be a ClaudeCodeControllerOutcome")
        for name, value in (
            ("observation_count", self.observation_count),
            ("consecutive_empty_reads", self.consecutive_empty_reads),
            ("output_used", self.output_used),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            isinstance(self.deadline_remaining, bool)
            or not isinstance(self.deadline_remaining, (int, float))
            or not math.isfinite(self.deadline_remaining)
            or self.deadline_remaining < 0
        ):
            raise ValueError("deadline_remaining must be non-negative")
        if not isinstance(self.limits_hit, tuple) or any(
            not isinstance(item, str) or not item
            for item in self.limits_hit
        ):
            raise ValueError("limits_hit must contain non-empty strings")

    @property
    def process_id(self) -> str:
        return self.snapshot.session_ref.process_id

    @property
    def action_required(self) -> ClaudeCodeActionRequired | None:
        return self.snapshot.action_required

    @property
    def terminal(self) -> bool:
        return _snapshot_is_terminal(self.snapshot)

@dataclass(slots=True)
class _ControllerTask:
    """仅保存工作流计数，不复制 Runtime 或 ProcessManager registry。"""

    process_id: str
    session_owner: str
    cwd: str
    started_at: float
    deadline: float
    initial_cursor: int
    observation_count: int = 0
    terminal_observation_count: int = 0
    consecutive_empty_reads: int = 0
    output_used: int = 0
    last_snapshot: ClaudeCodeSnapshot | None = None
    last_result: ClaudeCodeControllerResult | None = None
    terminal_events: tuple[ClaudeCodeEvent, ...] = ()
    cancelled: bool = False
    interrupt_requested: bool = False
    interaction_in_progress_id: str | None = None
    resolved_interaction_ids: OrderedDict[str, None] = field(
        default_factory=OrderedDict,
        repr=False,
    )
    invalidated_interaction_ids: OrderedDict[str, None] = field(
        default_factory=OrderedDict,
        repr=False,
    )
    archived: bool = False
    limits_hit: set[str] = field(default_factory=set)
    lock: threading.RLock = field(
        default_factory=threading.RLock,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class _TerminalTask:
    session_owner: str
    result: ClaudeCodeControllerResult


def _snapshot_is_terminal(snapshot: ClaudeCodeSnapshot) -> bool:
    if snapshot.state in _TERMINAL_STATES:
        return True
    return (
        snapshot.process_status is not None
        and snapshot.process_status
        not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
    )


class ClaudeCodeController:
    """串联 Runtime 公共操作，并以每任务锁保护工作流状态。"""

    def __init__(
        self,
        runtime: ClaudeCodeRuntime,
        *,
        policy: ClaudeCodeControllerPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        required = (
            "start",
            "observe",
            "current_interaction",
            "submit",
            "status",
            "wait",
            "interrupt",
            "kill",
        )
        if any(not callable(getattr(runtime, name, None)) for name in required):
            raise TypeError("runtime does not implement the required interface")
        if policy is not None and not isinstance(
            policy,
            ClaudeCodeControllerPolicy,
        ):
            raise TypeError("policy must be a ClaudeCodeControllerPolicy")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(sleeper):
            raise TypeError("sleeper must be callable")
        self._runtime = runtime
        self._policy = policy or ClaudeCodeControllerPolicy()
        self._clock = clock
        self._sleeper = sleeper
        self._tasks_guard = threading.Lock()
        self._tasks: dict[str, _ControllerTask] = {}
        self._terminal_tasks: OrderedDict[str, _TerminalTask] = OrderedDict()

    @property
    def policy(self) -> ClaudeCodeControllerPolicy:
        return self._policy

    def start_task(
        self,
        *,
        user_requested: bool,
        session_owner: str,
        cwd: str,
        task: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeControllerResult:
        """启动、登记并提交一次明确授权的受管 Claude Code 任务。"""

        if user_requested is not True:
            raise ClaudeCodeControllerError(
                "explicit_user_request_required",
                "Claude Code requires an explicit current user request",
            )
        self._require_nonempty("session_owner", session_owner)
        if not isinstance(cwd, str) or not cwd.strip():
            raise ClaudeCodeControllerError(
                "cwd_required",
                "Claude Code Controller requires an explicit working directory",
            )
        if not isinstance(task, str) or not task.strip():
            raise ClaudeCodeControllerError(
                "task_required",
                "Claude Code Controller requires a non-empty task",
            )
        self._require_cancel_checker(cancel_checker)
        if self._call_cancel_checker(cancel_checker, "poll_failed"):
            raise ClaudeCodeControllerError(
                "cancelled",
                "Claude Code task start was cancelled",
            )

        started_at = self._now()
        session = self._runtime.start(
            user_requested=True,
            session_owner=session_owner,
            cwd=cwd,
            cancel_checker=cancel_checker,
        )
        controller_task = _ControllerTask(
            process_id=session.process_id,
            session_owner=session.session_owner,
            cwd=session.cwd,
            started_at=started_at,
            deadline=started_at + self._policy.total_deadline,
            initial_cursor=session.cursor,
        )
        try:
            self._register_task(controller_task)
        except Exception as registration_error:
            try:
                self._kill_until_inactive(
                    session_owner=session.session_owner,
                    process_id=session.process_id,
                )
            except ClaudeCodeControllerError as cleanup_error:
                raise cleanup_error from registration_error
            raise ClaudeCodeControllerError(
                "poll_failed",
                "Claude Code Controller task registration failed",
                details={"process_id": session.process_id},
            ) from registration_error

        pre_submit_error: ClaudeCodeControllerError | None = None
        if self._call_cancel_checker(cancel_checker, "poll_failed"):
            controller_task.cancelled = True
            pre_submit_error = ClaudeCodeControllerError(
                "cancelled",
                "Claude Code task start was cancelled before submission",
                details={"process_id": session.process_id},
            )
        elif self._now() >= controller_task.deadline:
            pre_submit_error = ClaudeCodeControllerError(
                "deadline_exceeded",
                "Claude Code task deadline was reached before submission",
                details={"process_id": session.process_id},
            )
        if pre_submit_error is not None:
            raise pre_submit_error

        try:
            self._runtime.submit(
                session_owner=session_owner,
                process_id=session.process_id,
                data=task,
            )
        except Exception as submit_error:
            try:
                self._kill_until_inactive(
                    session_owner=session_owner,
                    process_id=session.process_id,
                )
            except ClaudeCodeControllerError as cleanup_error:
                raise cleanup_error from submit_error
            self._remove_active_task(controller_task)
            if isinstance(submit_error, ClaudeCodeRuntimeError):
                raise self._wrap_runtime_error(
                    "instruction_failed",
                    "Claude Code initial task submission failed",
                    submit_error,
                    session.process_id,
                ) from submit_error
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code initial task submission failed",
                details={"process_id": session.process_id},
            ) from submit_error

        with controller_task.lock:
            return self._observe_and_resolve_locked(
                controller_task,
                error_type="poll_failed",
            )

    def poll(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
        terminal_observation: bool = False,
    ) -> ClaudeCodeControllerResult:
        """执行恰好一个有界工作轮次，不 sleep、不自动输入。

        ``terminal_observation`` 仅供完成观察器在旧待处理动作存在时复核
        真实终态；普通调用保持默认的待处理动作暂停语义。
        """

        self._require_cancel_checker(cancel_checker)
        if not isinstance(terminal_observation, bool):
            raise TypeError("terminal_observation must be a boolean")
        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            return self._poll_locked(
                task,
                cancel_checker=cancel_checker,
                terminal_observation=terminal_observation,
            )

    def send_instruction(
        self,
        *,
        session_owner: str,
        process_id: str,
        instruction: str,
    ) -> ClaudeCodeControllerResult:
        """提交用户明确给出的普通补充指令，不处理待审批动作。"""

        if not isinstance(instruction, str) or not instruction.strip():
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code instruction must be non-empty",
            )
        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            raise self._terminal_error(process_id)
        assert task is not None
        with task.lock:
            self._guard_input_locked(task)
            action = self._current_snapshot_locked(task).action_required
            if action is not None and action.kind != ClaudeCodeActionKind.STALLED:
                raise ClaudeCodeControllerError(
                    "action_required",
                    "Claude Code has an unresolved action requiring explicit handling",
                    details={
                        "process_id": process_id,
                        "action_kind": action.kind.value,
                    },
                )
            try:
                self._runtime.submit(
                    session_owner=session_owner,
                    process_id=process_id,
                    data=instruction,
                )
            except ClaudeCodeRuntimeError as runtime_error:
                raise self._wrap_runtime_error(
                    "instruction_failed",
                    "Claude Code instruction submission failed",
                    runtime_error,
                    process_id,
                ) from runtime_error
            task.interrupt_requested = False
            task.consecutive_empty_reads = 0
            if action is not None:
                task.last_snapshot = replace(
                    self._current_snapshot_locked(task),
                    state=ClaudeCodeState.UNKNOWN,
                    action_required=None,
                )
            return self._observe_and_resolve_locked(
                task,
                error_type="poll_failed",
            )

    def current_interaction(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeCurrentInteraction | None:
        """返回最新已观察到的原生提示，不读取历史事件也不发送输入。"""

        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            return None
        assert task is not None
        with task.lock:
            if task.archived:
                return None
            snapshot = self._current_snapshot_locked(task)
            if _snapshot_is_terminal(snapshot):
                return None
            action = snapshot.action_required
            if not self._is_native_interaction(action):
                return None
            assert action is not None
            self._assert_interaction_belongs_to_task_locked(task, action)
            if (
                action.action_id in task.resolved_interaction_ids
                or action.action_id in task.invalidated_interaction_ids
                or task.interaction_in_progress_id == action.action_id
            ):
                return None
            try:
                interaction_action = self._runtime.current_interaction(
                    session_owner=session_owner,
                    process_id=process_id,
                )
            except ClaudeCodeRuntimeError as runtime_error:
                raise self._wrap_runtime_error(
                    "poll_failed",
                    "Claude Code current interaction could not be read",
                    runtime_error,
                    process_id,
                ) from runtime_error
            if not self._matches_current_interaction(
                action,
                interaction_action,
            ):
                return None
            assert interaction_action is not None
            return ClaudeCodeCurrentInteraction(
                state=snapshot.state,
                action=interaction_action,
            )

    def reply_to_interaction(
        self,
        *,
        response: ClaudeCodeInteractionResponse,
    ) -> ClaudeCodeControllerResult:
        """原样提交用户明确提供的当前原生交互回复。"""

        if not isinstance(response, ClaudeCodeInteractionResponse):
            raise ClaudeCodeControllerError(
                "interaction_response_required",
                "Claude Code interaction reply is invalid",
            )
        if response.user_confirmed is not True:
            raise ClaudeCodeControllerError(
                "explicit_user_confirmation_required",
                "Claude Code interaction reply requires explicit user confirmation",
                details={"process_id": response.process_id},
            )
        task, terminal = self._resolve_task(
            response.session_owner,
            response.process_id,
        )
        if terminal is not None:
            raise self._terminal_error(response.process_id)
        assert task is not None
        with task.lock:
            self._guard_input_locked(task)
            snapshot = self._observe_once_locked(
                task,
                error_type="interaction_response_failed",
            )
            if _snapshot_is_terminal(snapshot):
                self._finalize_terminal_locked(
                    task,
                    snapshot,
                    outcome=ClaudeCodeControllerOutcome.TERMINAL,
                )
                raise self._terminal_error(response.process_id)
            if snapshot.process_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES:
                raise ClaudeCodeControllerError(
                    "interaction_expired",
                    "Claude Code interaction is no longer active",
                    details={"process_id": response.process_id},
                )

            action = snapshot.action_required
            if not self._is_native_interaction(action):
                raise self._interaction_absent_error_locked(task, response)
            assert action is not None
            self._assert_interaction_belongs_to_task_locked(task, action)
            if action.action_id != response.action_id:
                raise self._interaction_id_error_locked(task, response)
            if action.action_id in task.resolved_interaction_ids:
                raise ClaudeCodeControllerError(
                    "interaction_already_resolved",
                    "Claude Code interaction was already resolved",
                    details={"process_id": response.process_id},
                )
            if action.action_id in task.invalidated_interaction_ids:
                raise ClaudeCodeControllerError(
                    "interaction_expired",
                    "Claude Code interaction is no longer current",
                    details={"process_id": response.process_id},
                )
            if task.interaction_in_progress_id == action.action_id:
                raise ClaudeCodeControllerError(
                    "interaction_in_progress",
                    "Claude Code interaction reply is already being submitted",
                    details={"process_id": response.process_id},
                )

            try:
                interaction_action = self._runtime.current_interaction(
                    session_owner=response.session_owner,
                    process_id=response.process_id,
                )
            except ClaudeCodeRuntimeError as runtime_error:
                raise self._wrap_runtime_error(
                    "interaction_response_failed",
                    "Claude Code current interaction could not be read",
                    runtime_error,
                    response.process_id,
                ) from runtime_error
            if not self._matches_current_interaction(
                action,
                interaction_action,
            ):
                self._clear_current_interaction_locked(
                    task,
                    snapshot=snapshot,
                )
                raise self._interaction_absent_error_locked(task, response)

            task.interaction_in_progress_id = action.action_id
            try:
                self._runtime.submit(
                    session_owner=response.session_owner,
                    process_id=response.process_id,
                    data=response.response,
                )
            except ClaudeCodeRuntimeError as runtime_error:
                if runtime_error.delivery_unknown:
                    self._remember_interaction_id_locked(
                        task.invalidated_interaction_ids,
                        action.action_id,
                    )
                    self._clear_current_interaction_locked(
                        task,
                        snapshot=snapshot,
                    )
                    raise ClaudeCodeControllerError(
                        "interaction_delivery_unknown",
                        "Claude Code interaction reply delivery could not be confirmed",
                        delivery_unknown=True,
                        details={
                            "process_id": response.process_id,
                            "cause_error_type": runtime_error.error_type,
                        },
                    ) from runtime_error
                raise self._wrap_runtime_error(
                    "interaction_response_failed",
                    "Claude Code interaction reply submission failed",
                    runtime_error,
                    response.process_id,
                ) from runtime_error
            else:
                self._remember_interaction_id_locked(
                    task.resolved_interaction_ids,
                    action.action_id,
                )
                task.interrupt_requested = False
                task.consecutive_empty_reads = 0
                self._clear_current_interaction_locked(
                    task,
                    snapshot=snapshot,
                    invalidate=False,
                )
                return self._observe_and_resolve_locked(
                    task,
                    error_type="interaction_response_failed",
                )
            finally:
                if task.interaction_in_progress_id == action.action_id:
                    task.interaction_in_progress_id = None

    def wait_for_action(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeControllerResult:
        """有界等待待处理动作、终态或任一工作流停止条件。"""

        self._require_cancel_checker(cancel_checker)
        result = self.poll(
            session_owner=session_owner,
            process_id=process_id,
            cancel_checker=cancel_checker,
        )
        while not self._wait_for_action_complete(result):
            result = self._wait_and_poll(
                session_owner=session_owner,
                process_id=process_id,
                cancel_checker=cancel_checker,
            )
        return result

    def wait_for_terminal_state(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeControllerResult:
        """有界等待终态，并在任何待处理动作出现时提前返回。"""

        self._require_cancel_checker(cancel_checker)
        result = self.poll(
            session_owner=session_owner,
            process_id=process_id,
            cancel_checker=cancel_checker,
        )
        while not self._wait_for_terminal_complete(result):
            result = self._wait_and_poll(
                session_owner=session_owner,
                process_id=process_id,
                cancel_checker=cancel_checker,
            )
        return result

    def request_interrupt(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeControllerResult:
        """请求一次协作式中断并有限观察；未收敛时不升级为 kill。"""

        self._require_cancel_checker(cancel_checker)
        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if not task.interrupt_requested:
                interrupted_snapshot = task.last_snapshot
                try:
                    self._runtime.interrupt(
                        session_owner=session_owner,
                        process_id=process_id,
                    )
                    task.interrupt_requested = True
                except ClaudeCodeRuntimeError as runtime_error:
                    if runtime_error.delivery_unknown:
                        task.interrupt_requested = True
                        self._clear_interrupt_action_locked(task)
                    raise self._wrap_runtime_error(
                        "interrupt_failed",
                        "Claude Code interrupt could not be confirmed",
                        runtime_error,
                        process_id,
                    ) from runtime_error
                self._clear_interrupt_action_locked(
                    task,
                    snapshot=interrupted_snapshot,
                )

            operation_deadline = self._operation_deadline(
                self._policy.interrupt_observation_attempts
            )
            for attempt in range(
                self._policy.interrupt_observation_attempts
            ):
                if self._call_cancel_checker(
                    cancel_checker,
                    "interrupt_failed",
                ):
                    task.cancelled = True
                    return self._make_result_locked(
                        task,
                        ClaudeCodeControllerOutcome.CANCELLED,
                    )
                if not self._observation_allowed_locked(task):
                    limit_outcome = self._limit_outcome_locked(task)
                    if limit_outcome is None:
                        raise ClaudeCodeControllerError(
                            "interrupt_failed",
                            "Claude Code interrupt observation limit was inconsistent",
                            details={"process_id": process_id},
                        )
                    return self._make_result_locked(
                        task,
                        limit_outcome,
                    )
                result = self._observe_interrupt_locked(
                    task,
                )
                if result is not None:
                    return result
                if (
                    attempt + 1
                    >= self._policy.interrupt_observation_attempts
                ):
                    break
                remaining = operation_deadline - self._now()
                if remaining <= 0:
                    break
                waited = self._wait_runtime_locked(
                    task,
                    timeout=min(
                        self._policy.poll_interval,
                        self._policy.single_wait_limit,
                        remaining,
                    ),
                    error_type="interrupt_failed",
                    cancel_checker=cancel_checker,
                )
                if waited is None:
                    return self._make_result_locked(
                        task,
                        ClaudeCodeControllerOutcome.CANCELLED,
                    )
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.INTERRUPT_PENDING,
            )

    def terminate(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeControllerResult:
        """终止当前受管进程，复核非 active 状态并执行有界 final drain。"""

        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            self._clear_current_interaction_locked(task)
            confirmed = self._kill_until_inactive(
                session_owner=session_owner,
                process_id=process_id,
            )

            if self._observation_allowed_locked(task):
                result = self._observe_and_resolve_locked(
                    task,
                    error_type="terminate_failed",
                    terminal_outcome=ClaudeCodeControllerOutcome.TERMINATED,
                )
                if result.terminal:
                    return result
            else:
                task.last_snapshot = self._snapshot_from_process_status(
                    self._current_snapshot_locked(task),
                    confirmed,
                )

            snapshot = self._current_snapshot_locked(task)
            if not _snapshot_is_terminal(snapshot):
                raise ClaudeCodeControllerError(
                    "terminate_failed",
                    "Claude Code termination did not converge to a terminal snapshot",
                    details={"process_id": process_id},
                )
            return self._finalize_terminal_locked(
                task,
                snapshot,
                outcome=ClaudeCodeControllerOutcome.TERMINATED,
            )

    def snapshot(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeControllerResult:
        """返回 Controller 已保存的最新有界结果，不读取进程。"""

        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            snapshot = self._current_snapshot_locked(task)
            outcome = self._current_outcome_locked(task, snapshot)
            return self._make_result_locked(task, outcome, snapshot=snapshot)

    def _poll_locked(
        self,
        task: _ControllerTask,
        *,
        cancel_checker: Callable[[], bool] | None,
        terminal_observation: bool = False,
    ) -> ClaudeCodeControllerResult:
        if task.last_snapshot is None:
            if task.cancelled or self._call_cancel_checker(
                cancel_checker,
                "poll_failed",
            ):
                task.cancelled = True
                raise ClaudeCodeControllerError(
                    "cancelled",
                    (
                        "Claude Code Controller task is cancelled before "
                        "its first observation"
                    ),
                    details={"process_id": task.process_id},
                )
            if self._now() >= task.deadline:
                raise ClaudeCodeControllerError(
                    "deadline_exceeded",
                    (
                        "Claude Code Controller task deadline was reached "
                        "before its first observation"
                    ),
                    details={"process_id": task.process_id},
                )
            return self._observe_and_resolve_locked(
                task,
                error_type="poll_failed",
            )

        snapshot = self._current_snapshot_locked(task)
        if _snapshot_is_terminal(snapshot):
            return self._finalize_terminal_locked(
                task,
                snapshot,
                outcome=ClaudeCodeControllerOutcome.TERMINAL,
            )
        if (
            terminal_observation
            and self._terminal_probe_is_inactive_locked(task)
        ):
            return self._observe_and_resolve_locked(
                task,
                error_type="poll_failed",
                terminal_observation=True,
            )
        if snapshot.action_required is not None:
            return self._make_result_locked(
                task,
                self._action_outcome(snapshot.action_required),
            )
        if task.cancelled or self._call_cancel_checker(
            cancel_checker,
            "poll_failed",
        ):
            task.cancelled = True
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.CANCELLED,
            )
        limit_outcome = self._limit_outcome_locked(task)
        if limit_outcome is not None:
            return self._make_result_locked(task, limit_outcome)
        if self._now() >= task.deadline:
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED,
            )
        return self._observe_and_resolve_locked(
            task,
            error_type="poll_failed",
        )

    def _terminal_probe_is_inactive_locked(
        self,
        task: _ControllerTask,
    ) -> bool:
        """仅为观察器复核真实进程终态，活跃时不改写当前 Snapshot。"""

        try:
            process_snapshot = self._runtime.status(
                session_owner=task.session_owner,
                process_id=task.process_id,
            )
        except ClaudeCodeRuntimeError as runtime_error:
            raise self._wrap_runtime_error(
                "poll_failed",
                "Claude Code terminal observation probe failed",
                runtime_error,
                task.process_id,
            ) from runtime_error
        return not process_snapshot.active

    def _observe_and_resolve_locked(
        self,
        task: _ControllerTask,
        *,
        error_type: str,
        terminal_observation: bool = False,
        terminal_outcome: ClaudeCodeControllerOutcome = (
            ClaudeCodeControllerOutcome.TERMINAL
        ),
    ) -> ClaudeCodeControllerResult:
        snapshot = self._observe_once_locked(
            task,
            error_type=error_type,
            terminal_observation=terminal_observation,
        )
        if _snapshot_is_terminal(snapshot):
            return self._finalize_terminal_locked(
                task,
                snapshot,
                outcome=terminal_outcome,
            )
        if snapshot.action_required is not None:
            return self._make_result_locked(
                task,
                self._action_outcome(snapshot.action_required),
            )
        if (
            task.consecutive_empty_reads
            >= self._policy.max_consecutive_empty_reads
        ):
            stalled = ClaudeCodeActionRequired(
                kind=ClaudeCodeActionKind.STALLED,
                summary="Claude Code made no observable progress",
                prompt_text="",
                options=(),
                risk="low",
                cursor=snapshot.raw_cursor,
                action_id=build_claude_code_action_id(
                    process_id=task.process_id,
                    session_owner=task.session_owner,
                    kind=ClaudeCodeActionKind.STALLED,
                    prompt_text="",
                    options=(),
                    cursor_start=snapshot.raw_cursor,
                    cursor_end=snapshot.raw_cursor,
                ),
                process_id=task.process_id,
                session_owner=task.session_owner,
                cursor_start=snapshot.raw_cursor,
                cursor_end=snapshot.raw_cursor,
                created_at=(
                    snapshot.last_observed_at
                    if snapshot.last_observed_at is not None
                    else self._now()
                ),
            )
            snapshot = replace(
                snapshot,
                state=ClaudeCodeState.UNKNOWN,
                action_required=stalled,
            )
            task.last_snapshot = snapshot
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.STALLED,
                snapshot=snapshot,
            )
        limit_outcome = self._limit_outcome_locked(task)
        if limit_outcome is not None:
            return self._make_result_locked(task, limit_outcome)
        if self._now() >= task.deadline:
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED,
            )
        return self._make_result_locked(
            task,
            ClaudeCodeControllerOutcome.RUNNING,
        )

    def _observe_interrupt_locked(
        self,
        task: _ControllerTask,
    ) -> ClaudeCodeControllerResult | None:
        """只按真实终态收敛一次 interrupt 观察，不让旧动作提前结束。"""

        snapshot = self._observe_once_locked(
            task,
            error_type="interrupt_failed",
        )
        if _snapshot_is_terminal(snapshot):
            return self._finalize_terminal_locked(
                task,
                snapshot,
                outcome=ClaudeCodeControllerOutcome.TERMINAL,
                suppress_actions=True,
            )
        return None

    def _clear_interrupt_action_locked(
        self,
        task: _ControllerTask,
        *,
        snapshot: ClaudeCodeSnapshot | None = None,
    ) -> ClaudeCodeSnapshot | None:
        """只使已确认送达 interrupt 时的旧提示失效。"""

        return self._clear_current_interaction_locked(
            task,
            snapshot=snapshot,
        )

    def _clear_current_interaction_locked(
        self,
        task: _ControllerTask,
        *,
        snapshot: ClaudeCodeSnapshot | None = None,
        invalidate: bool = True,
    ) -> ClaudeCodeSnapshot | None:
        """清理当前提示缓存，并按需使其身份不可再次提交。"""

        current = snapshot if snapshot is not None else task.last_snapshot
        if current is None or current.action_required is None:
            return current
        action = current.action_required
        if invalidate and self._is_native_interaction(action):
            assert action is not None
            self._remember_interaction_id_locked(
                task.invalidated_interaction_ids,
                action.action_id,
            )
        state = current.state
        if state in {
            ClaudeCodeState.WAITING_INPUT,
            ClaudeCodeState.WAITING_APPROVAL,
        }:
            state = ClaudeCodeState.UNKNOWN
        cleared = replace(
            current,
            state=state,
            action_required=None,
        )
        task.last_snapshot = cleared
        return cleared

    def _observe_once_locked(
        self,
        task: _ControllerTask,
        *,
        error_type: str,
        terminal_observation: bool = False,
    ) -> ClaudeCodeSnapshot:
        terminal_reserve_used = self._claim_observation_slot_locked(
            task,
            terminal_observation=terminal_observation,
        )
        try:
            snapshot = self._runtime.observe(
                session_owner=task.session_owner,
                process_id=task.process_id,
                limit=self._policy.observation_read_limit,
            )
        except ClaudeCodeRuntimeError as runtime_error:
            raise self._wrap_runtime_error(
                error_type,
                "Claude Code observation failed",
                runtime_error,
                task.process_id,
            ) from runtime_error
        self._record_observation_locked(
            task,
            snapshot,
            error_type=error_type,
            terminal_reserve_used=terminal_reserve_used,
        )
        return snapshot

    def _record_observation_locked(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
        *,
        error_type: str,
        terminal_reserve_used: bool,
    ) -> None:
        if snapshot.session_ref.process_id != task.process_id:
            raise ClaudeCodeControllerError(
                error_type,
                "Claude Code observation changed process identity",
                details={"process_id": task.process_id},
            )
        if snapshot.session_ref.session_owner != task.session_owner:
            raise ClaudeCodeControllerError(
                "controller_owner_mismatch",
                "Claude Code observation changed session owner",
                details={"process_id": task.process_id},
            )
        previous = task.last_snapshot
        previous_cursor = (
            previous.raw_cursor
            if previous is not None
            else task.initial_cursor
        )
        if snapshot.raw_cursor < previous_cursor:
            raise ClaudeCodeControllerError(
                error_type,
                "Claude Code observation cursor moved backwards",
                details={"process_id": task.process_id},
            )
        cursor_delta = max(0, snapshot.raw_cursor - previous_cursor)
        task.output_used += cursor_delta
        if not terminal_reserve_used:
            task.observation_count += 1
        empty = previous is not None and (
            snapshot.raw_cursor == previous.raw_cursor
            and snapshot.state == previous.state
            and snapshot.process_status == previous.process_status
            and snapshot.action_required == previous.action_required
            and snapshot.last_activity_at <= previous.last_activity_at
            and not snapshot.events
        )
        if empty:
            task.consecutive_empty_reads += 1
        else:
            task.consecutive_empty_reads = 0
        self._invalidate_replaced_interaction_locked(
            task,
            previous=previous,
            current=snapshot,
        )
        task.last_snapshot = snapshot
        if task.observation_count >= self._policy.max_observation_count:
            task.limits_hit.add("observation_limit_reached")

    def _finalize_terminal_locked(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
        *,
        outcome: ClaudeCodeControllerOutcome,
        suppress_actions: bool = False,
    ) -> ClaudeCodeControllerResult:
        if task.archived:
            return self._archived_result(task)
        if not _snapshot_is_terminal(snapshot):
            raise ClaudeCodeControllerError(
                "final_drain_failed",
                "Claude Code final drain requires a terminal snapshot",
                details={"process_id": task.process_id},
            )

        self._record_terminal_events_locked(task, snapshot.events)
        current = snapshot
        advanced_on_last_attempt = False
        for _ in range(self._policy.final_drain_attempts):
            terminal_observation = self._snapshot_has_inactive_process(current)
            if (
                not terminal_observation
                and not self._observation_allowed_locked(task)
            ):
                break
            previous_cursor = current.raw_cursor
            current = self._observe_once_locked(
                task,
                error_type="final_drain_failed",
                terminal_observation=terminal_observation,
            )
            if suppress_actions:
                cleared_current = self._clear_interrupt_action_locked(
                    task,
                    snapshot=current,
                )
                assert cleared_current is not None
                current = cleared_current
            if not _snapshot_is_terminal(current):
                raise ClaudeCodeControllerError(
                    "final_drain_failed",
                    "Claude Code became active during final drain",
                    details={"process_id": task.process_id},
                )
            self._record_terminal_events_locked(task, current.events)
            advanced_on_last_attempt = current.raw_cursor > previous_cursor
            if not advanced_on_last_attempt:
                break
        else:
            if advanced_on_last_attempt:
                task.limits_hit.add("final_drain_attempts")

        if current.action_required is not None:
            raise ClaudeCodeControllerError(
                "final_drain_failed",
                "Claude Code terminal snapshot retained an action requirement",
                details={"process_id": task.process_id},
            )
        current = replace(current, events=task.terminal_events)
        task.last_snapshot = current
        result = self._make_result_locked(task, outcome, snapshot=current)
        self._archive_task_locked(task, result)
        return result

    def _record_terminal_events_locked(
        self,
        task: _ControllerTask,
        events: tuple[ClaudeCodeEvent, ...],
    ) -> None:
        merged = list(task.terminal_events)
        for event in events:
            if event not in merged:
                merged.append(event)
        task.terminal_events = self._bounded_final_events(task, merged)

    def _bounded_final_events(
        self,
        task: _ControllerTask,
        events: list[ClaudeCodeEvent],
    ) -> tuple[ClaudeCodeEvent, ...]:
        limit = self._policy.final_event_limit
        if len(events) <= limit:
            return tuple(events)
        task.limits_hit.add("final_event_limit")
        exit_indexes = [
            index
            for index, event in enumerate(events)
            if event.event_type == ClaudeCodeEventType.PROCESS_EXIT
        ]
        selected_indexes = list(range(len(events) - limit, len(events)))
        if exit_indexes and exit_indexes[-1] not in selected_indexes:
            selected_indexes[0] = exit_indexes[-1]
            selected_indexes.sort()
        return tuple(events[index] for index in selected_indexes)

    def _wait_and_poll(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None,
    ) -> ClaudeCodeControllerResult:
        task, terminal = self._resolve_task(session_owner, process_id)
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            if task.last_snapshot is None:
                return self._poll_locked(
                    task,
                    cancel_checker=cancel_checker,
                )
            snapshot = self._current_snapshot_locked(task)
            if snapshot.action_required is not None:
                return self._make_result_locked(
                    task,
                    self._action_outcome(snapshot.action_required),
                )
            if task.cancelled or self._call_cancel_checker(
                cancel_checker,
                "poll_failed",
            ):
                task.cancelled = True
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.CANCELLED,
                )
            remaining = task.deadline - self._now()
            if remaining <= 0:
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED,
                )
            limit_outcome = self._limit_outcome_locked(task)
            if limit_outcome is not None:
                return self._make_result_locked(task, limit_outcome)
            waited = self._wait_runtime_locked(
                task,
                timeout=min(
                    self._policy.poll_interval,
                    self._policy.single_wait_limit,
                    remaining,
                ),
                error_type="poll_failed",
                cancel_checker=cancel_checker,
            )
            if waited is None:
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.CANCELLED,
                )
            if not waited.active:
                return self._observe_and_resolve_locked(
                    task,
                    error_type="poll_failed",
                )
            return self._poll_locked(task, cancel_checker=cancel_checker)

    def _wait_runtime_locked(
        self,
        task: _ControllerTask,
        *,
        timeout: float,
        error_type: str,
        cancel_checker: Callable[[], bool] | None,
    ) -> ClaudeCodeProcessSnapshot | None:
        bounded_timeout = min(timeout, self._policy.single_wait_limit)
        if bounded_timeout <= 0:
            raise ClaudeCodeControllerError(
                error_type,
                "Claude Code wait interval was exhausted",
                details={"process_id": task.process_id},
            )
        try:
            return self._runtime.wait(
                session_owner=task.session_owner,
                process_id=task.process_id,
                timeout=bounded_timeout,
                cancel_checker=cancel_checker,
            )
        except ClaudeCodeRuntimeError as runtime_error:
            if self._call_cancel_checker(cancel_checker, error_type):
                task.cancelled = True
                return None
            raise self._wrap_runtime_error(
                error_type,
                "Claude Code bounded wait failed",
                runtime_error,
                task.process_id,
            ) from runtime_error

    def _kill_until_inactive(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeProcessSnapshot:
        last_error: ClaudeCodeRuntimeError | None = None
        operation_deadline = self._operation_deadline(
            self._policy.cleanup_attempts
        )
        for attempt in range(self._policy.cleanup_attempts):
            remaining = operation_deadline - self._now()
            if remaining <= 0:
                break
            try:
                self._runtime.kill(
                    session_owner=session_owner,
                    process_id=process_id,
                    grace_seconds=min(
                        self._policy.terminate_grace_period,
                        self._policy.single_wait_limit,
                        remaining,
                    ),
                )
            except ClaudeCodeRuntimeError as runtime_error:
                last_error = runtime_error
            try:
                confirmed = self._runtime.status(
                    session_owner=session_owner,
                    process_id=process_id,
                )
                if not confirmed.active:
                    return confirmed
            except ClaudeCodeRuntimeError as runtime_error:
                last_error = runtime_error
            if attempt + 1 < self._policy.cleanup_attempts:
                remaining = operation_deadline - self._now()
                if remaining <= 0:
                    break
                self._sleep(
                    min(
                        self._policy.cleanup_retry_interval,
                        self._policy.single_wait_limit,
                        remaining,
                    )
                )
        details: dict[str, object] = {"process_id": process_id}
        if last_error is not None:
            details["cause_error_type"] = last_error.error_type
        raise ClaudeCodeControllerError(
            "cleanup_failed",
            "Claude Code process cleanup did not reach a non-active state",
            retryable=True,
            details=details,
        )

    def _guard_input_locked(self, task: _ControllerTask) -> None:
        snapshot = self._current_snapshot_locked(task)
        if _snapshot_is_terminal(snapshot):
            raise self._terminal_error(task.process_id)
        if task.cancelled:
            raise ClaudeCodeControllerError(
                "cancelled",
                "Claude Code Controller task is cancelled",
                details={"process_id": task.process_id},
            )
        if self._now() >= task.deadline:
            raise ClaudeCodeControllerError(
                "deadline_exceeded",
                "Claude Code Controller task deadline has been reached",
                details={"process_id": task.process_id},
            )
        limit_outcome = self._limit_outcome_locked(task)
        if limit_outcome is not None:
            raise ClaudeCodeControllerError(
                limit_outcome.value,
                "Claude Code Controller task reached a workflow limit",
                details={"process_id": task.process_id},
            )

    def _observation_allowed_locked(self, task: _ControllerTask) -> bool:
        if task.observation_count >= self._policy.max_observation_count:
            task.limits_hit.add("observation_limit_reached")
            return False
        return True

    def _claim_observation_slot_locked(
        self,
        task: _ControllerTask,
        *,
        terminal_observation: bool,
    ) -> bool:
        """申请一次 observe；保留额度只允许已确认非 active 的终态路径调用。"""

        if task.observation_count < self._policy.max_observation_count:
            return False
        task.limits_hit.add("observation_limit_reached")
        if not terminal_observation:
            raise ClaudeCodeControllerError(
                ClaudeCodeControllerOutcome.OBSERVATION_LIMIT_REACHED.value,
                "Claude Code observation is blocked by a Controller limit",
                details={"process_id": task.process_id},
            )
        if (
            task.terminal_observation_count
            >= self._policy.terminal_observation_reserve
        ):
            task.limits_hit.add("terminal_observation_reserve_exhausted")
            raise ClaudeCodeControllerError(
                "terminal_observation_reserve_exhausted",
                "Claude Code terminal observation reserve was exhausted",
                details={
                    "process_id": task.process_id,
                    "terminal_observation_reserve": (
                        self._policy.terminal_observation_reserve
                    ),
                },
            )
        task.terminal_observation_count += 1
        return True

    @staticmethod
    def _snapshot_has_inactive_process(snapshot: ClaudeCodeSnapshot) -> bool:
        """只按已有 ProcessStatus 确认底层进程已非 active。"""

        return bool(
            snapshot.process_status is not None
            and snapshot.process_status
            not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
        )

    def _limit_outcome_locked(
        self,
        task: _ControllerTask,
    ) -> ClaudeCodeControllerOutcome | None:
        if task.observation_count >= self._policy.max_observation_count:
            task.limits_hit.add("observation_limit_reached")
            return ClaudeCodeControllerOutcome.OBSERVATION_LIMIT_REACHED
        return None

    def _current_outcome_locked(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
    ) -> ClaudeCodeControllerOutcome:
        if _snapshot_is_terminal(snapshot):
            return ClaudeCodeControllerOutcome.TERMINAL
        if snapshot.action_required is not None:
            return self._action_outcome(snapshot.action_required)
        if task.cancelled:
            return ClaudeCodeControllerOutcome.CANCELLED
        limit_outcome = self._limit_outcome_locked(task)
        if limit_outcome is not None:
            return limit_outcome
        if self._now() >= task.deadline:
            return ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED
        return ClaudeCodeControllerOutcome.RUNNING

    @staticmethod
    def _action_outcome(
        action: ClaudeCodeActionRequired,
    ) -> ClaudeCodeControllerOutcome:
        if action.kind == ClaudeCodeActionKind.STALLED:
            return ClaudeCodeControllerOutcome.STALLED
        return ClaudeCodeControllerOutcome.ACTION_REQUIRED

    def _make_result_locked(
        self,
        task: _ControllerTask,
        outcome: ClaudeCodeControllerOutcome,
        *,
        snapshot: ClaudeCodeSnapshot | None = None,
    ) -> ClaudeCodeControllerResult:
        current = snapshot or self._current_snapshot_locked(task)
        result = ClaudeCodeControllerResult(
            snapshot=current,
            outcome=outcome,
            observation_count=task.observation_count,
            consecutive_empty_reads=task.consecutive_empty_reads,
            output_used=task.output_used,
            deadline_remaining=max(0.0, task.deadline - self._now()),
            limits_hit=tuple(sorted(task.limits_hit)),
        )
        task.last_result = result
        return result

    def _snapshot_from_process_status(
        self,
        snapshot: ClaudeCodeSnapshot,
        process_snapshot: ClaudeCodeProcessSnapshot,
    ) -> ClaudeCodeSnapshot:
        state = snapshot.state
        if state not in _TERMINAL_STATES:
            state = ClaudeCodeState.UNKNOWN
        return replace(
            snapshot,
            state=state,
            action_required=None,
            process_status=process_snapshot.status,
            exit_code=process_snapshot.exit_code,
        )

    def _register_task(self, task: _ControllerTask) -> None:
        with self._tasks_guard:
            if (
                task.process_id in self._tasks
                or task.process_id in self._terminal_tasks
            ):
                raise ClaudeCodeControllerError(
                    "poll_failed",
                    "Claude Code Controller received a duplicate process id",
                    details={"process_id": task.process_id},
                )
            self._tasks[task.process_id] = task

    def _resolve_task(
        self,
        session_owner: str,
        process_id: str,
    ) -> tuple[_ControllerTask | None, ClaudeCodeControllerResult | None]:
        self._require_nonempty("session_owner", session_owner)
        self._require_nonempty("process_id", process_id)
        with self._tasks_guard:
            task = self._tasks.get(process_id)
            terminal = self._terminal_tasks.get(process_id)
            if task is not None:
                if task.session_owner != session_owner:
                    raise ClaudeCodeControllerError(
                        "controller_owner_mismatch",
                        "Claude Code Controller task belongs to another owner",
                        details={"process_id": process_id},
                    )
                return task, None
            if terminal is not None:
                if terminal.session_owner != session_owner:
                    raise ClaudeCodeControllerError(
                        "controller_owner_mismatch",
                        "Claude Code Controller task belongs to another owner",
                        details={"process_id": process_id},
                    )
                self._terminal_tasks.move_to_end(process_id)
                return None, terminal.result
        raise ClaudeCodeControllerError(
            "controller_task_not_found",
            "Claude Code Controller task was not found",
            details={"process_id": process_id},
        )

    def _archive_task_locked(
        self,
        task: _ControllerTask,
        result: ClaudeCodeControllerResult,
    ) -> None:
        task.archived = True
        task.last_result = result
        with self._tasks_guard:
            if self._tasks.get(task.process_id) is task:
                self._tasks.pop(task.process_id, None)
            self._terminal_tasks[task.process_id] = _TerminalTask(
                session_owner=task.session_owner,
                result=result,
            )
            self._terminal_tasks.move_to_end(task.process_id)
            while (
                len(self._terminal_tasks)
                > self._policy.terminal_snapshot_limit
            ):
                self._terminal_tasks.popitem(last=False)

    def _remove_active_task(self, task: _ControllerTask) -> None:
        with self._tasks_guard:
            if self._tasks.get(task.process_id) is task:
                self._tasks.pop(task.process_id, None)

    @staticmethod
    def _is_native_interaction(
        action: ClaudeCodeActionRequired | None,
    ) -> bool:
        """只把带完整身份的真实 CC Prompt 作为可回复交互。"""

        return bool(
            action is not None
            and action.kind != ClaudeCodeActionKind.STALLED
            and action.action_id
            and action.process_id
            and action.session_owner
            and action.cursor_start is not None
            and action.cursor_end is not None
            and action.created_at is not None
        )

    @classmethod
    def _matches_current_interaction(
        cls,
        safe_action: ClaudeCodeActionRequired,
        interaction_action: ClaudeCodeActionRequired | None,
    ) -> bool:
        """只接受与最新安全 Snapshot 同一身份的短暂原生视图。"""

        if not cls._is_native_interaction(interaction_action):
            return False
        assert interaction_action is not None
        if (
            interaction_action.raw_prompt_text is None
            or interaction_action.raw_options is None
        ):
            return False
        return (
            interaction_action.action_id == safe_action.action_id
            and interaction_action.process_id == safe_action.process_id
            and interaction_action.session_owner == safe_action.session_owner
            and interaction_action.cursor_start == safe_action.cursor_start
            and interaction_action.cursor_end == safe_action.cursor_end
            and interaction_action.created_at == safe_action.created_at
            and interaction_action.kind == safe_action.kind
            and interaction_action.summary == safe_action.summary
            and interaction_action.prompt_text == safe_action.prompt_text
            and interaction_action.options == safe_action.options
            and interaction_action.risk == safe_action.risk
        )

    def _assert_interaction_belongs_to_task_locked(
        self,
        task: _ControllerTask,
        action: ClaudeCodeActionRequired,
    ) -> None:
        """拒绝动作身份与当前受管 task 不一致的内部状态。"""

        if action.session_owner != task.session_owner:
            raise ClaudeCodeControllerError(
                "controller_owner_mismatch",
                "Claude Code interaction belongs to another owner",
                details={"process_id": task.process_id},
            )
        if action.process_id != task.process_id:
            raise ClaudeCodeControllerError(
                "interaction_id_mismatch",
                "Claude Code interaction belongs to another process",
                details={"process_id": task.process_id},
            )

    @staticmethod
    def _remember_interaction_id_locked(
        entries: OrderedDict[str, None],
        action_id: str,
    ) -> None:
        """只保留有界的 opaque 动作身份，不保存任何用户回复。"""

        if not action_id:
            return
        entries[action_id] = None
        entries.move_to_end(action_id)
        while len(entries) > _MAX_RETAINED_INTERACTION_IDS:
            entries.popitem(last=False)

    def _invalidate_replaced_interaction_locked(
        self,
        task: _ControllerTask,
        *,
        previous: ClaudeCodeSnapshot | None,
        current: ClaudeCodeSnapshot,
    ) -> None:
        """新提示、恢复工作或终态出现时使旧提示身份不可复用。"""

        if previous is None:
            return
        previous_action = previous.action_required
        if not self._is_native_interaction(previous_action):
            return
        assert previous_action is not None
        current_action = current.action_required
        if (
            not self._is_native_interaction(current_action)
            or current_action is None
            or current_action.action_id != previous_action.action_id
        ):
            self._remember_interaction_id_locked(
                task.invalidated_interaction_ids,
                previous_action.action_id,
            )

    def _interaction_absent_error_locked(
        self,
        task: _ControllerTask,
        response: ClaudeCodeInteractionResponse,
    ) -> ClaudeCodeControllerError:
        """将已消费和已失效的提示与从未存在的提示区分开。"""

        if response.action_id in task.resolved_interaction_ids:
            return ClaudeCodeControllerError(
                "interaction_already_resolved",
                "Claude Code interaction was already resolved",
                details={"process_id": response.process_id},
            )
        if response.action_id in task.invalidated_interaction_ids:
            return ClaudeCodeControllerError(
                "interaction_expired",
                "Claude Code interaction is no longer current",
                details={"process_id": response.process_id},
            )
        return ClaudeCodeControllerError(
            "interaction_not_found",
            "Claude Code has no current native interaction",
            details={"process_id": response.process_id},
        )

    def _interaction_id_error_locked(
        self,
        task: _ControllerTask,
        response: ClaudeCodeInteractionResponse,
    ) -> ClaudeCodeControllerError:
        """拒绝已被替换、已消费或不属于当前提示的回复。"""

        if response.action_id in task.resolved_interaction_ids:
            return ClaudeCodeControllerError(
                "interaction_already_resolved",
                "Claude Code interaction was already resolved",
                details={"process_id": response.process_id},
            )
        if response.action_id in task.invalidated_interaction_ids:
            return ClaudeCodeControllerError(
                "interaction_expired",
                "Claude Code interaction is no longer current",
                details={"process_id": response.process_id},
            )
        return ClaudeCodeControllerError(
            "interaction_id_mismatch",
            "Claude Code interaction does not match the current prompt",
            details={"process_id": response.process_id},
        )

    @staticmethod
    def _current_snapshot_locked(task: _ControllerTask) -> ClaudeCodeSnapshot:
        if task.last_snapshot is None:
            raise ClaudeCodeControllerError(
                "poll_failed",
                "Claude Code Controller has no observation snapshot",
                details={"process_id": task.process_id},
            )
        return task.last_snapshot

    @staticmethod
    def _archived_result(task: _ControllerTask) -> ClaudeCodeControllerResult:
        if task.last_result is None:
            raise ClaudeCodeControllerError(
                "controller_task_not_found",
                "Claude Code Controller terminal result was not retained",
                details={"process_id": task.process_id},
            )
        return task.last_result

    @staticmethod
    def _wait_for_action_complete(result: ClaudeCodeControllerResult) -> bool:
        return (
            result.action_required is not None
            or result.terminal
            or result.outcome
            != ClaudeCodeControllerOutcome.RUNNING
        )

    @staticmethod
    def _wait_for_terminal_complete(
        result: ClaudeCodeControllerResult,
    ) -> bool:
        if result.terminal or result.action_required is not None:
            return True
        return result.outcome != ClaudeCodeControllerOutcome.RUNNING

    def _operation_deadline(self, attempt_count: int) -> float:
        duration = (
            attempt_count * self._policy.single_wait_limit
            + max(0, attempt_count - 1)
            * self._policy.cleanup_retry_interval
        )
        return self._now() + min(duration, self._policy.total_deadline)

    def _sleep(self, seconds: float) -> None:
        bounded = min(seconds, self._policy.single_wait_limit)
        if bounded <= 0:
            return
        try:
            self._sleeper(bounded)
        except Exception as sleep_error:
            raise ClaudeCodeControllerError(
                "cleanup_failed",
                "Claude Code cleanup retry wait failed",
            ) from sleep_error

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value) or value < 0:
            raise ClaudeCodeControllerError(
                "poll_failed",
                "Claude Code Controller clock returned an invalid value",
            )
        return value

    @staticmethod
    def _require_nonempty(name: str, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            error_type = (
                "controller_task_not_found"
                if name == "process_id"
                else "controller_owner_mismatch"
                if name == "session_owner"
                else "task_required"
            )
            raise ClaudeCodeControllerError(
                error_type,
                f"{name} must be a non-empty string",
            )

    @staticmethod
    def _require_cancel_checker(
        cancel_checker: Callable[[], bool] | None,
    ) -> None:
        if cancel_checker is not None and not callable(cancel_checker):
            raise TypeError("cancel_checker must be callable")

    @staticmethod
    def _call_cancel_checker(
        cancel_checker: Callable[[], bool] | None,
        error_type: str,
    ) -> bool:
        if cancel_checker is None:
            return False
        try:
            return bool(cancel_checker())
        except Exception as cancel_error:
            raise ClaudeCodeControllerError(
                error_type,
                "Claude Code cancellation check failed",
            ) from cancel_error

    @staticmethod
    def _wrap_runtime_error(
        error_type: str,
        message: str,
        runtime_error: ClaudeCodeRuntimeError,
        process_id: str,
    ) -> ClaudeCodeControllerError:
        return ClaudeCodeControllerError(
            error_type,
            message,
            retryable=runtime_error.retryable,
            delivery_unknown=runtime_error.delivery_unknown,
            details={
                "process_id": process_id,
                "cause_error_type": runtime_error.error_type,
            },
        )

    @staticmethod
    def _terminal_error(process_id: str) -> ClaudeCodeControllerError:
        return ClaudeCodeControllerError(
            "terminal_session",
            "Claude Code Controller task is already terminal",
            details={"process_id": process_id},
        )


__all__ = [
    "ClaudeCodeController",
    "ClaudeCodeControllerError",
    "ClaudeCodeControllerOutcome",
    "ClaudeCodeControllerResult",
]
