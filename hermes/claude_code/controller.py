"""在 Claude Code Runtime 之上提供同步、有界且不自动审批的工作流编排。"""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
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
_INTERRUPT_MENU_RE = re.compile(
    r"(?is)(?:"
    r"press\s+ctrl\s*[- ]?c\s+again(?:\s+to\s+exit)?"
    r"|interrupted\b[^\n]{0,160}\bwhat\s+should\s+claude\s+do\s+instead"
    r"|what\s+should\s+claude\s+do\s+instead"
    r")"
)


class ClaudeCodeControllerOutcome(str, Enum):
    """说明一次 Controller 调用为何返回，不替代 Runtime 状态。"""

    RUNNING = "running"
    STARTUP_NOT_READY = "startup_not_ready"
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
    # 只说明本会话首条任务是否成功提交，不保存任务正文。
    initial_instruction_submitted: bool = False
    # 轮次终态与进程终态相互独立；运行中的 CC 也可以结束一个任务轮次。
    round_id: str | None = None
    round_terminal_state: ClaudeCodeState | None = None

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
        if not isinstance(self.initial_instruction_submitted, bool):
            raise ValueError("initial_instruction_submitted must be a boolean")
        if self.round_id is not None and (
            not isinstance(self.round_id, str) or not self.round_id
        ):
            raise ValueError("round_id must be a non-empty string or None")
        if self.round_terminal_state is not None:
            if self.round_terminal_state not in _TERMINAL_STATES:
                raise ValueError("round_terminal_state must be terminal or None")
            if self.round_id is None:
                raise ValueError("round_terminal_state requires round_id")

    @property
    def process_id(self) -> str:
        return self.snapshot.session_ref.process_id

    @property
    def action_required(self) -> ClaudeCodeActionRequired | None:
        return self.snapshot.action_required

    @property
    def state(self) -> ClaudeCodeState:
        """优先暴露当前任务轮次终态，不伪造底层 Snapshot 的进程状态。"""

        return self.round_terminal_state or self.snapshot.state

    @property
    def round_terminal(self) -> bool:
        return self.round_terminal_state is not None

    @property
    def process_active(self) -> bool:
        return self.snapshot.process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES

    @property
    def terminal(self) -> bool:
        return self.round_terminal or _snapshot_is_terminal(self.snapshot)


@dataclass(slots=True)
class _ControllerTaskRound:
    """只保存单次任务轮次的可验证边界与事实，不保存任务或回复正文。"""

    round_id: str
    round_start_cursor: int
    latest_instruction_cursor: int
    round_started_at: float
    instruction_submitted: bool = True
    real_activity_seen: bool = False
    activity_after_instruction_seen: bool = False
    completion_evidence_seen: bool = False
    cursor_continuous: bool = True
    ready_after_instruction_seen: bool = False
    stable_ready_count: int = 0
    interrupt_confirmed: bool = False
    interrupt_requested_cursor: int | None = None
    post_interrupt_observation_seen: bool = False
    ready_after_interrupt_seen: bool = False
    post_interrupt_work_seen: bool = False
    completion_after_interrupt_seen: bool = False
    interrupt_menu_seen: bool = False
    failure_after_instruction_seen: bool = False
    failure_cursor: int | None = None
    observation_sequence: int = 0
    failure_observation_sequence: int | None = None
    ready_after_failure_seen: bool = False
    terminal_state: ClaudeCodeState | None = None
    terminal_at: float | None = None

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
    initial_instruction_submitted: bool = False
    startup_interaction_resolved: bool = False
    startup_ready_observation_count: int = 0
    consecutive_empty_reads: int = 0
    output_used: int = 0
    round_sequence: int = 0
    active_round: _ControllerTaskRound | None = None
    terminal_round_results: OrderedDict[str, ClaudeCodeControllerResult] = field(
        default_factory=OrderedDict,
        repr=False,
    )
    latest_terminal_round_id: str | None = None
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
    round_results: OrderedDict[str, ClaudeCodeControllerResult]


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
        """启动、登记并仅在 READY 后提交一次明确授权的初始任务。"""

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

        with controller_task.lock:
            startup_result = self._await_startup_ready_locked(
                controller_task,
                cancel_checker=cancel_checker,
            )
            if startup_result is not None:
                return startup_result
            return self._submit_initial_task_locked(
                controller_task,
                initial_task=task,
                cancel_checker=cancel_checker,
            )

    def _await_startup_ready_locked(
        self,
        task: _ControllerTask,
        *,
        cancel_checker: Callable[[], bool] | None,
    ) -> ClaudeCodeControllerResult | None:
        """有界等待连续可信 READY，绝不保存或自动重放初始任务。"""

        terminal_observation = False
        for attempt in range(self._policy.startup_observation_attempts):
            use_terminal_observation = terminal_observation
            terminal_observation = False
            result = self._observe_and_resolve_locked(
                task,
                error_type="poll_failed",
                terminal_observation=use_terminal_observation,
            )
            snapshot = result.snapshot
            if (
                result.terminal
                or result.action_required is not None
                or result.outcome != ClaudeCodeControllerOutcome.RUNNING
            ):
                return result
            if task.cancelled or self._call_cancel_checker(
                cancel_checker,
                "poll_failed",
            ):
                task.cancelled = True
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.CANCELLED,
                )
            if self._now() >= task.deadline:
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED,
                )
            if self._deferred_initial_submission_ready(task, snapshot):
                return None
            if attempt + 1 >= self._policy.startup_observation_attempts:
                break
            remaining = task.deadline - self._now()
            if remaining <= 0:
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED,
                )
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
            terminal_observation = not waited.active

        task.limits_hit.add("startup_observation_attempts")
        return self._make_result_locked(
            task,
            ClaudeCodeControllerOutcome.STARTUP_NOT_READY,
        )

    def _submit_initial_task_locked(
        self,
        task: _ControllerTask,
        *,
        initial_task: str,
        cancel_checker: Callable[[], bool] | None,
    ) -> ClaudeCodeControllerResult:
        """只在已观察到 READY 后提交一次初始任务，并保留既有失败清理。"""

        snapshot = self._current_snapshot_locked(task)
        if not self._deferred_initial_submission_ready(task, snapshot):
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.STARTUP_NOT_READY,
            )
        if task.initial_instruction_submitted:
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code initial task was already submitted",
                details={"process_id": task.process_id},
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
        if self._now() >= task.deadline:
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.DEADLINE_EXCEEDED,
            )
        limit_outcome = self._limit_outcome_locked(task)
        if limit_outcome is not None:
            return self._make_result_locked(task, limit_outcome)
        try:
            self._submit_instruction(
                session_owner=task.session_owner,
                process_id=task.process_id,
                data=initial_task,
            )
        except Exception as submit_error:
            try:
                self._kill_until_inactive(
                    session_owner=task.session_owner,
                    process_id=task.process_id,
                )
            except ClaudeCodeControllerError as cleanup_error:
                raise cleanup_error from submit_error
            self._remove_active_task(task)
            if isinstance(submit_error, ClaudeCodeRuntimeError):
                raise self._wrap_runtime_error(
                    "instruction_failed",
                    "Claude Code initial task submission failed",
                    submit_error,
                    task.process_id,
                ) from submit_error
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code initial task submission failed",
                details={"process_id": task.process_id},
            ) from submit_error

        task.initial_instruction_submitted = True
        task.startup_ready_observation_count = 0
        self._begin_round_locked(
            task,
            round_start_cursor=snapshot.raw_cursor,
        )
        return self._observe_and_resolve_locked(
            task,
            error_type="poll_failed",
        )

    @staticmethod
    def _initial_submission_ready(snapshot: ClaudeCodeSnapshot) -> bool:
        """初始任务只能在无待处理交互的活跃 READY 会话中提交。"""

        return (
            snapshot.state == ClaudeCodeState.READY
            and snapshot.action_required is None
            and snapshot.process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
        )

    def _deferred_initial_submission_ready(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
    ) -> bool:
        """首投须经连续 READY 观察，旧交互标记不能替代状态事实。"""

        return (
            self._initial_submission_ready(snapshot)
            and task.startup_ready_observation_count
            >= self._policy.startup_ready_observations
        )

    def _new_round_submission_ready(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
    ) -> bool:
        """首条任务沿用启动门禁；后续轮次仅复用已可信收敛的 READY。"""

        if task.active_round is not None:
            return False
        if not task.initial_instruction_submitted:
            return self._deferred_initial_submission_ready(task, snapshot)
        latest = self._stored_round_result_locked(task, None)
        return bool(
            latest is not None
            and latest.round_terminal
            and self._initial_submission_ready(snapshot)
        )

    def poll(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
        terminal_observation: bool = False,
        round_id: str | None = None,
    ) -> ClaudeCodeControllerResult:
        """执行恰好一个有界工作轮次，不 sleep、不自动输入。

        ``terminal_observation`` 仅供完成观察器在旧待处理动作存在时复核
        真实终态；普通调用保持默认的待处理动作暂停语义。
        """

        self._require_cancel_checker(cancel_checker)
        if not isinstance(terminal_observation, bool):
            raise TypeError("terminal_observation must be a boolean")
        self._require_round_id(round_id)
        task, terminal = self._resolve_task(
            session_owner,
            process_id,
            round_id=round_id,
        )
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            if round_id is None and task.active_round is None:
                return self._poll_between_rounds_locked(
                    task,
                    cancel_checker=cancel_checker,
                )
            stored_round = self._stored_round_result_locked(task, round_id)
            if stored_round is not None:
                return stored_round
            self._assert_active_round_matches_locked(task, round_id)
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
        round_id: str,
        instruction: str,
    ) -> ClaudeCodeControllerResult:
        """提交用户明确给出的补充或延后初始指令，不处理待审批动作。"""

        if not isinstance(instruction, str) or not instruction.strip():
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code instruction must be non-empty",
            )
        self._require_explicit_round_id(round_id)
        task, terminal = self._resolve_task(
            session_owner,
            process_id,
            round_id=round_id,
        )
        if terminal is not None:
            raise self._terminal_error(process_id)
        assert task is not None
        with task.lock:
            # 在 task 锁内重验调用方已知的 round，避免旧上下文自动落到更新后的 round。
            self._assert_send_round_matches_locked(task, round_id)
            snapshot = self._current_snapshot_locked(task)
            action = snapshot.action_required
            if action is not None and action.kind != ClaudeCodeActionKind.STALLED:
                raise ClaudeCodeControllerError(
                    "action_required",
                    "Claude Code has an unresolved action requiring explicit handling",
                    details={
                        "process_id": process_id,
                        "action_kind": action.kind.value,
                    },
                )
            active_round = task.active_round
            initial_submission = not task.initial_instruction_submitted
            starts_new_round = active_round is None
            if starts_new_round:
                self._guard_new_round_input_locked(task, snapshot)
            else:
                self._guard_input_locked(task)
            if starts_new_round and not self._new_round_submission_ready(
                task,
                snapshot,
            ):
                return self._make_result_locked(
                    task,
                    ClaudeCodeControllerOutcome.STARTUP_NOT_READY,
                )
            try:
                self._submit_instruction(
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
            if initial_submission:
                task.initial_instruction_submitted = True
                task.startup_ready_observation_count = 0
            if starts_new_round:
                self._begin_round_locked(
                    task,
                    round_start_cursor=snapshot.raw_cursor,
                )
            else:
                assert active_round is not None
                self._note_round_instruction_locked(
                    task,
                    active_round,
                    instruction_cursor=snapshot.raw_cursor,
                )
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

    def _submit_instruction(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """任务指令优先使用 Runtime 的专用分步提交，不改变交互回复。"""

        submit_task = getattr(self._runtime, "submit_task", None)
        if callable(submit_task):
            return submit_task(
                session_owner=session_owner,
                process_id=process_id,
                data=data,
            )
        return self._runtime.submit(
            session_owner=session_owner,
            process_id=process_id,
            data=data,
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
                if task.active_round is not None:
                    self._note_round_instruction_locked(
                        task,
                        task.active_round,
                        instruction_cursor=snapshot.raw_cursor,
                    )
                self._clear_current_interaction_locked(
                    task,
                    snapshot=snapshot,
                    invalidate=False,
                )
                result = self._observe_and_resolve_locked(
                    task,
                    error_type="interaction_response_failed",
                )
                if not task.initial_instruction_submitted:
                    task.startup_interaction_resolved = (
                        self._initial_submission_ready(result.snapshot)
                    )
                return result
            finally:
                if task.interaction_in_progress_id == action.action_id:
                    task.interaction_in_progress_id = None

    def wait_for_action(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
        round_id: str | None = None,
    ) -> ClaudeCodeControllerResult:
        """有界等待待处理动作、终态或任一工作流停止条件。"""

        self._require_cancel_checker(cancel_checker)
        result = self.poll(
            session_owner=session_owner,
            process_id=process_id,
            cancel_checker=cancel_checker,
            round_id=round_id,
        )
        while not self._wait_for_action_complete(result):
            result = self._wait_and_poll(
                session_owner=session_owner,
                process_id=process_id,
                cancel_checker=cancel_checker,
                round_id=round_id,
            )
        return result

    def wait_for_terminal_state(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
        round_id: str | None = None,
    ) -> ClaudeCodeControllerResult:
        """有界等待终态，并在任何待处理动作出现时提前返回。"""

        self._require_cancel_checker(cancel_checker)
        result = self.poll(
            session_owner=session_owner,
            process_id=process_id,
            cancel_checker=cancel_checker,
            round_id=round_id,
        )
        while not self._wait_for_terminal_complete(result):
            result = self._wait_and_poll(
                session_owner=session_owner,
                process_id=process_id,
                cancel_checker=cancel_checker,
                round_id=round_id,
            )
        return result

    def request_interrupt(
        self,
        *,
        session_owner: str,
        process_id: str,
        cancel_checker: Callable[[], bool] | None = None,
        round_id: str | None = None,
    ) -> ClaudeCodeControllerResult:
        """请求一次协作式中断并有限观察；未收敛时不升级为 kill。"""

        self._require_cancel_checker(cancel_checker)
        self._require_round_id(round_id)
        task, terminal = self._resolve_task(
            session_owner,
            process_id,
            round_id=round_id,
        )
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            stored_round = self._stored_round_result_locked(task, round_id)
            if stored_round is not None:
                return stored_round
            self._assert_active_round_matches_locked(task, round_id)
            current_round = task.active_round
            if current_round is None:
                raise ClaudeCodeControllerError(
                    "no_active_round",
                    "Claude Code has no active task round to interrupt",
                    details={"process_id": process_id},
                )
            latest_snapshot = self._current_snapshot_locked(task)
            if (
                not task.interrupt_requested
                and latest_snapshot.state == ClaudeCodeState.READY
                and current_round.stable_ready_count > 0
            ):
                settled = self._observe_and_resolve_locked(
                    task,
                    error_type="interrupt_failed",
                )
                if settled.round_terminal:
                    return settled
                if settled.snapshot.state == ClaudeCodeState.READY:
                    return settled
            if not task.interrupt_requested:
                interrupted_snapshot = task.last_snapshot
                try:
                    self._runtime.interrupt(
                        session_owner=session_owner,
                        process_id=process_id,
                    )
                    task.interrupt_requested = True
                    current_round.interrupt_confirmed = True
                    current_round.interrupt_requested_cursor = (
                        interrupted_snapshot.raw_cursor
                        if interrupted_snapshot is not None
                        else current_round.latest_instruction_cursor
                    )
                    current_round.post_interrupt_observation_seen = False
                    current_round.ready_after_interrupt_seen = False
                    current_round.post_interrupt_work_seen = False
                    current_round.completion_after_interrupt_seen = False
                    current_round.interrupt_menu_seen = False
                    current_round.stable_ready_count = 0
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
        round_id: str | None = None,
    ) -> ClaudeCodeControllerResult:
        """返回 Controller 已保存的最新有界结果，不读取进程。"""

        self._require_round_id(round_id)
        task, terminal = self._resolve_task(
            session_owner,
            process_id,
            round_id=round_id,
        )
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            stored_round = self._stored_round_result_locked(task, round_id)
            if stored_round is not None:
                return stored_round
            self._assert_active_round_matches_locked(task, round_id)
            if task.active_round is None:
                latest = self._stored_round_result_locked(task, None)
                if latest is not None:
                    return latest
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
        if task.active_round is None:
            return self._poll_between_rounds_locked(
                task,
                cancel_checker=cancel_checker,
            )
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
            if self._is_interrupt_menu_snapshot(task, snapshot):
                round_result = self._try_finalize_running_round_locked(
                    task,
                    snapshot,
                )
                if round_result is not None:
                    return round_result
            return self._make_result_locked(
                task,
                self._action_outcome(snapshot.action_required),
            )
        round_result = self._try_finalize_running_round_locked(task, snapshot)
        if round_result is not None:
            return round_result
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

    def _poll_between_rounds_locked(
        self,
        task: _ControllerTask,
        *,
        cancel_checker: Callable[[], bool] | None,
    ) -> ClaudeCodeControllerResult:
        """保留上一轮冻结结果，同时仍可在真实进程退出时走既有收敛路径。"""

        stored_round = self._stored_round_result_locked(task, None)
        if stored_round is not None:
            if self._terminal_probe_is_inactive_locked(task):
                return self._observe_and_resolve_locked(
                    task,
                    error_type="poll_failed",
                    terminal_observation=True,
                )
            return stored_round
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
        if snapshot.action_required is not None and not (
            self._is_interrupt_menu_snapshot(task, snapshot)
        ):
            return self._make_result_locked(
                task,
                self._action_outcome(snapshot.action_required),
            )
        round_result = self._try_finalize_running_round_locked(task, snapshot)
        if round_result is not None:
            return round_result
        if snapshot.action_required is not None:
            return self._make_result_locked(
                task,
                self._action_outcome(snapshot.action_required),
            )
        if (
            task.consecutive_empty_reads
            >= self._policy.max_consecutive_empty_reads
        ):
            stalled_snapshot = replace(
                snapshot,
                state=ClaudeCodeState.UNKNOWN,
                action_required=None,
            )
            return self._make_result_locked(
                task,
                ClaudeCodeControllerOutcome.STALLED,
                snapshot=stalled_snapshot,
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
        round_result = self._try_finalize_running_round_locked(task, snapshot)
        if round_result is not None:
            return round_result
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
        if task.initial_instruction_submitted:
            task.startup_ready_observation_count = 0
        elif self._initial_submission_ready(snapshot):
            task.startup_ready_observation_count += 1
        else:
            task.startup_ready_observation_count = 0
        self._invalidate_replaced_interaction_locked(
            task,
            previous=previous,
            current=snapshot,
        )
        current_round = task.active_round
        if current_round is not None:
            # 每个 round 独立编号，允许同 cursor 的后续空读参与失败收敛。
            current_round.observation_sequence += 1
        task.last_snapshot = snapshot
        self._record_round_observation_locked(
            task,
            previous=previous,
            snapshot=snapshot,
        )
        if task.observation_count >= self._policy.max_observation_count:
            task.limits_hit.add("observation_limit_reached")

    def _begin_round_locked(
        self,
        task: _ControllerTask,
        *,
        round_start_cursor: int,
    ) -> _ControllerTaskRound:
        """仅在普通任务指令确认送达后建立新轮次，不携带指令正文。"""

        if task.active_round is not None:
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code already has an active task round",
                details={"process_id": task.process_id},
            )
        if (
            isinstance(round_start_cursor, bool)
            or not isinstance(round_start_cursor, int)
            or round_start_cursor < 0
        ):
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code task round cursor is invalid",
                details={"process_id": task.process_id},
            )
        task.round_sequence += 1
        now = self._now()
        round_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "hermes:claude-code-round:"
                    f"{task.session_owner}:{task.process_id}:{task.round_sequence}"
                ),
            )
        )
        current_round = _ControllerTaskRound(
            round_id=round_id,
            round_start_cursor=round_start_cursor,
            latest_instruction_cursor=round_start_cursor,
            round_started_at=now,
        )
        task.active_round = current_round
        task.deadline = now + self._policy.total_deadline
        task.observation_count = 0
        task.terminal_observation_count = 0
        task.consecutive_empty_reads = 0
        task.cancelled = False
        task.interrupt_requested = False
        task.limits_hit.clear()
        task.terminal_events = ()
        task.last_result = None
        return current_round

    def _note_round_instruction_locked(
        self,
        task: _ControllerTask,
        current_round: _ControllerTaskRound,
        *,
        instruction_cursor: int,
    ) -> None:
        """补充输入仍属于当前轮次，但必须等待该输入后的新事实再收敛。"""

        if task.active_round is not current_round:
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code task round is no longer active",
                details={"process_id": task.process_id},
            )
        if (
            isinstance(instruction_cursor, bool)
            or not isinstance(instruction_cursor, int)
            or instruction_cursor < 0
        ):
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code task round cursor is invalid",
                details={"process_id": task.process_id},
            )
        current_round.latest_instruction_cursor = instruction_cursor
        current_round.activity_after_instruction_seen = False
        current_round.ready_after_instruction_seen = False
        current_round.stable_ready_count = 0
        current_round.interrupt_confirmed = False
        current_round.interrupt_requested_cursor = None
        current_round.post_interrupt_observation_seen = False
        current_round.ready_after_interrupt_seen = False
        current_round.post_interrupt_work_seen = False
        current_round.completion_after_interrupt_seen = False
        current_round.interrupt_menu_seen = False
        current_round.failure_after_instruction_seen = False
        current_round.failure_cursor = None
        current_round.failure_observation_sequence = None
        current_round.ready_after_failure_seen = False
        task.interrupt_requested = False
        task.consecutive_empty_reads = 0

    def _record_round_observation_locked(
        self,
        task: _ControllerTask,
        *,
        previous: ClaudeCodeSnapshot | None,
        snapshot: ClaudeCodeSnapshot,
    ) -> None:
        """只把当前轮次边界之后的非 echo 增量事实并入轮次状态。"""

        current_round = task.active_round
        if current_round is None or not current_round.instruction_submitted:
            return
        events = tuple(
            event
            for event in snapshot.events
            if self._event_belongs_to_round(
                task,
                current_round,
                event,
                boundary=current_round.round_start_cursor,
            )
        )
        observation_degraded = self._round_observation_is_degraded(
            task,
            snapshot.events,
        )
        if any(
            event.process_id == task.process_id
            and event.event_type == ClaudeCodeEventType.CURSOR_GAP
            and event.cursor_end > current_round.round_start_cursor
            for event in snapshot.events
        ):
            current_round.cursor_continuous = False
            current_round.stable_ready_count = 0

        events_after_instruction = tuple(
            event
            for event in events
            if self._event_belongs_to_round(
                task,
                current_round,
                event,
                boundary=current_round.latest_instruction_cursor,
            )
        )
        interrupt_menu_snapshot = self._is_interrupt_menu_snapshot(
            task,
            snapshot,
        )
        if interrupt_menu_snapshot and not observation_degraded:
            interrupt_cursor = current_round.interrupt_requested_cursor
            assert interrupt_cursor is not None
            events_after_interrupt = tuple(
                event
                for event in events
                if event.cursor_end > interrupt_cursor
            )
            post_interrupt_work = any(
                event.event_type
                in {
                    ClaudeCodeEventType.PROGRESS,
                    ClaudeCodeEventType.COMPLETION_SIGNAL,
                    ClaudeCodeEventType.FAILURE_SIGNAL,
                }
                or (
                    event.event_type == ClaudeCodeEventType.OUTPUT
                    and event.metadata.get("ready_ui_only") is not True
                    and event.metadata.get("ui_non_activity") is not True
                    and event.metadata.get("source")
                    not in {"input_echo", "unconfirmed_after_input"}
                    and not _INTERRUPT_MENU_RE.search(event.text)
                )
                for event in events_after_interrupt
            )
            if not post_interrupt_work:
                current_round.interrupt_menu_seen = True
                current_round.post_interrupt_observation_seen = True
        failure_seen_this_observation = False
        activity_seen_this_observation = False
        for event in events_after_instruction:
            if self._event_is_round_failure(
                event,
                snapshot=snapshot,
                observation_degraded=observation_degraded,
            ):
                failure_seen_this_observation = True
                current_round.failure_after_instruction_seen = True
                current_round.failure_cursor = max(
                    current_round.failure_cursor or 0,
                    event.cursor_end,
                )
                current_round.real_activity_seen = True
                current_round.activity_after_instruction_seen = True
                current_round.ready_after_instruction_seen = False
                current_round.ready_after_failure_seen = False
                current_round.stable_ready_count = 0
                continue
            if not self._event_counts_as_round_activity(
                event,
                snapshot=snapshot,
                observation_degraded=observation_degraded,
            ):
                continue
            activity_seen_this_observation = True
            current_round.real_activity_seen = True
            current_round.activity_after_instruction_seen = True
            if current_round.failure_after_instruction_seen:
                current_round.ready_after_instruction_seen = False
                current_round.ready_after_failure_seen = False
                current_round.stable_ready_count = 0
            if event.event_type == ClaudeCodeEventType.COMPLETION_SIGNAL:
                current_round.completion_evidence_seen = True

        if current_round.failure_after_instruction_seen and (
            observation_degraded or snapshot.action_required is not None
        ):
            current_round.ready_after_instruction_seen = False
            current_round.ready_after_failure_seen = False
            current_round.stable_ready_count = 0

        if failure_seen_this_observation:
            current_round.failure_observation_sequence = (
                current_round.observation_sequence
            )

        if not observation_degraded and self._is_new_round_ready_observation(
            current_round,
            previous=previous,
            snapshot=snapshot,
            failure_seen_this_observation=failure_seen_this_observation,
            activity_seen_this_observation=activity_seen_this_observation,
        ):
            current_round.ready_after_instruction_seen = True
            if current_round.failure_after_instruction_seen:
                current_round.ready_after_failure_seen = True

        interrupt_cursor = current_round.interrupt_requested_cursor
        if (
            current_round.interrupt_confirmed
            and interrupt_cursor is not None
            and current_round.cursor_continuous
        ):
            events_after_interrupt = tuple(
                event
                for event in events
                if event.cursor_end > interrupt_cursor
            )
            if any(
                event.event_type == ClaudeCodeEventType.PROGRESS
                for event in events_after_interrupt
            ):
                current_round.post_interrupt_work_seen = True
            if any(
                event.event_type == ClaudeCodeEventType.COMPLETION_SIGNAL
                for event in events_after_interrupt
            ):
                current_round.completion_after_interrupt_seen = True
            if self._is_new_interrupt_ready_observation(
                previous=previous,
                snapshot=snapshot,
                interrupt_cursor=interrupt_cursor,
            ):
                current_round.post_interrupt_observation_seen = True
                current_round.ready_after_interrupt_seen = True

        if (
            not observation_degraded
            and self._round_ready_observation(current_round, snapshot)
        ):
            current_round.stable_ready_count += 1
        else:
            current_round.stable_ready_count = 0

    @staticmethod
    def _event_belongs_to_round(
        task: _ControllerTask,
        current_round: _ControllerTaskRound,
        event: ClaudeCodeEvent,
        *,
        boundary: int,
    ) -> bool:
        """按进程身份与绝对 cursor 边界隔离当前轮次的增量事件。"""

        return (
            event.process_id == task.process_id
            and event.cursor_start >= boundary
            and event.cursor_end > current_round.round_start_cursor
            and event.cursor_end > boundary
        )

    @staticmethod
    def _round_observation_is_degraded(
        task: _ControllerTask,
        events: tuple[ClaudeCodeEvent, ...],
    ) -> bool:
        """本次读取出现 gap、错误或安全降级时不采用其中的活动证据。"""

        for event in events:
            if event.process_id != task.process_id:
                continue
            if event.event_type in {
                ClaudeCodeEventType.CURSOR_GAP,
                ClaudeCodeEventType.READ_ERROR,
            }:
                return True
            if event.metadata.get("source") == "cursor_gap":
                return True
            if (
                event.metadata.get("limits_hit")
                or event.metadata.get("event_text_truncated")
            ):
                return True
        return False

    @staticmethod
    def _event_counts_as_round_activity(
        event: ClaudeCodeEvent,
        *,
        snapshot: ClaudeCodeSnapshot,
        observation_degraded: bool,
    ) -> bool:
        """只接受可归属且无降级的真实工作输出，不重新解析任务正文。"""

        if observation_degraded or snapshot.action_required is not None:
            return False
        if event.event_type in {
            ClaudeCodeEventType.PROGRESS,
            ClaudeCodeEventType.COMPLETION_SIGNAL,
        }:
            return True
        if event.event_type != ClaudeCodeEventType.OUTPUT:
            return False
        if (
            event.metadata.get("ready_ui_only") is True
            or event.metadata.get("ui_non_activity") is True
        ):
            return False
        source = event.metadata.get("source")
        if source not in {None, "mixed"}:
            return False
        return not (
            event.metadata.get("limits_hit")
            or event.metadata.get("event_text_truncated")
        )

    @staticmethod
    def _event_is_round_failure(
        event: ClaudeCodeEvent,
        *,
        snapshot: ClaudeCodeSnapshot,
        observation_degraded: bool,
    ) -> bool:
        """只使用 Detector 已结构化且未被安全降级的失败事实。"""

        return (
            event.event_type == ClaudeCodeEventType.FAILURE_SIGNAL
            and not observation_degraded
            and snapshot.action_required is None
        )

    def _is_new_round_ready_observation(
        self,
        current_round: _ControllerTaskRound,
        *,
        previous: ClaudeCodeSnapshot | None,
        snapshot: ClaudeCodeSnapshot,
        failure_seen_this_observation: bool,
        activity_seen_this_observation: bool,
    ) -> bool:
        """只接受提交后新出现的 READY，不让旧 READY 或普通重绘越过轮次边界。"""

        if not (
            current_round.activity_after_instruction_seen
            and snapshot.raw_cursor > current_round.latest_instruction_cursor
            and snapshot.state == ClaudeCodeState.READY
            and snapshot.action_required is None
            and self._snapshot_has_active_process(snapshot)
        ):
            return False
        if current_round.failure_after_instruction_seen:
            # 失败与 READY 同批出现时，下一次观察才可作为失败后的稳定 READY。
            return (
                current_round.failure_cursor is not None
                and current_round.failure_observation_sequence is not None
                and current_round.observation_sequence
                > current_round.failure_observation_sequence
                and not failure_seen_this_observation
                and not activity_seen_this_observation
                and snapshot.raw_cursor >= current_round.failure_cursor
            )
        return previous is None or (
            previous.state != ClaudeCodeState.READY
            or previous.raw_cursor <= current_round.latest_instruction_cursor
        )

    def _is_new_interrupt_ready_observation(
        self,
        *,
        previous: ClaudeCodeSnapshot | None,
        snapshot: ClaudeCodeSnapshot,
        interrupt_cursor: int,
    ) -> bool:
        """中断后的 READY 必须是新的状态事实，而不是 Ctrl+C echo 或旧界面重绘。"""

        if not (
            snapshot.raw_cursor > interrupt_cursor
            and snapshot.state == ClaudeCodeState.READY
            and snapshot.action_required is None
            and self._snapshot_has_active_process(snapshot)
        ):
            return False
        return previous is None or previous.state != ClaudeCodeState.READY

    def _round_ready_observation(
        self,
        current_round: _ControllerTaskRound,
        snapshot: ClaudeCodeSnapshot,
    ) -> bool:
        if not (
            current_round.cursor_continuous
            and current_round.activity_after_instruction_seen
            and current_round.ready_after_instruction_seen
            and (
                not current_round.failure_after_instruction_seen
                or current_round.ready_after_failure_seen
            )
            and snapshot.raw_cursor > current_round.latest_instruction_cursor
            and snapshot.state == ClaudeCodeState.READY
            and snapshot.action_required is None
            and self._snapshot_has_active_process(snapshot)
        ):
            return False
        interrupt_cursor = current_round.interrupt_requested_cursor
        return (
            not current_round.interrupt_confirmed
            or (
                interrupt_cursor is not None
                and current_round.post_interrupt_observation_seen
                and current_round.ready_after_interrupt_seen
                and snapshot.raw_cursor > interrupt_cursor
            )
        )

    def _try_finalize_running_round_locked(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
    ) -> ClaudeCodeControllerResult | None:
        """仅凭本轮提交后的组合事实收敛 running 进程中的任务轮次。"""

        current_round = task.active_round
        if current_round is None or current_round.terminal_state is not None:
            return None
        if snapshot.action_required is not None and not (
            self._is_interrupt_menu_snapshot(task, snapshot)
        ):
            return None
        if not self._snapshot_has_active_process(snapshot):
            return None
        ready_confirmed = (
            current_round.stable_ready_count
            >= self._policy.startup_ready_observations
        )
        if (
            current_round.interrupt_confirmed
            and current_round.real_activity_seen
            and current_round.interrupt_menu_seen
            and not current_round.post_interrupt_work_seen
            and not current_round.completion_after_interrupt_seen
        ):
            return self._finalize_round_locked(
                task,
                snapshot,
                ClaudeCodeState.INTERRUPTED,
            )
        if (
            current_round.interrupt_confirmed
            and current_round.real_activity_seen
            and current_round.post_interrupt_observation_seen
            and not current_round.post_interrupt_work_seen
            and not current_round.completion_after_interrupt_seen
            and ready_confirmed
        ):
            return self._finalize_round_locked(
                task,
                snapshot,
                ClaudeCodeState.INTERRUPTED,
            )
        if task.interrupt_requested and not current_round.interrupt_confirmed:
            return None
        if (
            current_round.instruction_submitted
            and not current_round.interrupt_confirmed
            and current_round.failure_after_instruction_seen
            and current_round.failure_cursor is not None
            and current_round.real_activity_seen
            and current_round.activity_after_instruction_seen
            and current_round.cursor_continuous
            and current_round.ready_after_failure_seen
            and ready_confirmed
        ):
            return self._finalize_round_locked(
                task,
                snapshot,
                ClaudeCodeState.FAILED,
            )
        if (
            current_round.instruction_submitted
            and current_round.real_activity_seen
            and current_round.activity_after_instruction_seen
            and current_round.cursor_continuous
            and not current_round.failure_after_instruction_seen
            and ready_confirmed
        ):
            return self._finalize_round_locked(
                task,
                snapshot,
                ClaudeCodeState.COMPLETED,
            )
        return None

    def _finalize_round_locked(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
        terminal_state: ClaudeCodeState,
    ) -> ClaudeCodeControllerResult:
        """冻结一个 running CC 的任务轮次；不清理进程、SessionRef 或 PTY。"""

        current_round = task.active_round
        if current_round is None:
            raise ClaudeCodeControllerError(
                "controller_round_not_found",
                "Claude Code has no active task round to finalize",
                details={"process_id": task.process_id},
            )
        if terminal_state not in _TERMINAL_STATES:
            raise ClaudeCodeControllerError(
                "instruction_failed",
                "Claude Code task round terminal state is invalid",
                details={"process_id": task.process_id},
            )
        current_round.terminal_state = terminal_state
        current_round.terminal_at = self._now()
        result = self._make_result_locked(
            task,
            ClaudeCodeControllerOutcome.TERMINAL,
            snapshot=snapshot,
            round_id=current_round.round_id,
            round_terminal_state=terminal_state,
        )
        task.active_round = None
        self._store_terminal_round_result_locked(task, result)
        return result

    def _store_terminal_round_result_locked(
        self,
        task: _ControllerTask,
        result: ClaudeCodeControllerResult,
    ) -> None:
        round_id = result.round_id
        if round_id is None:
            return
        task.terminal_round_results[round_id] = result
        task.terminal_round_results.move_to_end(round_id)
        task.latest_terminal_round_id = round_id
        while (
            len(task.terminal_round_results)
            > self._policy.terminal_snapshot_limit
        ):
            removed_round_id, _ = task.terminal_round_results.popitem(
                last=False
            )
            if task.latest_terminal_round_id == removed_round_id:
                task.latest_terminal_round_id = (
                    next(reversed(task.terminal_round_results), None)
                )

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
        current_round = task.active_round
        round_id = (
            current_round.round_id
            if current_round is not None
            else None
        )
        round_terminal_state = self._process_terminal_round_state(
            current,
            outcome=outcome,
        ) if current_round is not None else None
        if current_round is not None:
            current_round.terminal_state = round_terminal_state
            current_round.terminal_at = self._now()
        result = self._make_result_locked(
            task,
            outcome,
            snapshot=current,
            round_id=round_id,
            round_terminal_state=round_terminal_state,
        )
        if current_round is not None:
            task.active_round = None
            self._store_terminal_round_result_locked(task, result)
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
        round_id: str | None = None,
    ) -> ClaudeCodeControllerResult:
        task, terminal = self._resolve_task(
            session_owner,
            process_id,
            round_id=round_id,
        )
        if terminal is not None:
            return terminal
        assert task is not None
        with task.lock:
            if task.archived:
                return self._archived_result(task)
            if round_id is None and task.active_round is None:
                return self._poll_between_rounds_locked(
                    task,
                    cancel_checker=cancel_checker,
                )
            stored_round = self._stored_round_result_locked(task, round_id)
            if stored_round is not None:
                return stored_round
            self._assert_active_round_matches_locked(task, round_id)
            if task.last_snapshot is None:
                return self._poll_locked(
                    task,
                    cancel_checker=cancel_checker,
                )
            snapshot = self._current_snapshot_locked(task)
            if snapshot.action_required is not None:
                if self._is_interrupt_menu_snapshot(task, snapshot):
                    round_result = self._try_finalize_running_round_locked(
                        task,
                        snapshot,
                    )
                    if round_result is not None:
                        return round_result
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

    def _guard_new_round_input_locked(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
    ) -> None:
        """上一轮已结束时仅检查底层会话仍可输入，不继承旧轮次预算。"""

        if _snapshot_is_terminal(snapshot):
            raise self._terminal_error(task.process_id)
        if not task.initial_instruction_submitted:
            self._guard_input_locked(task)

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

    @staticmethod
    def _snapshot_has_active_process(snapshot: ClaudeCodeSnapshot) -> bool:
        """只按已有 ProcessStatus 确认底层进程仍处于 active 状态。"""

        return snapshot.process_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES

    @staticmethod
    def _is_interrupt_menu_action(
        action: ClaudeCodeActionRequired | None,
    ) -> bool:
        """只把本次 Ctrl+C 后的明确中断菜单视为中断证据。"""

        return bool(
            action is not None
            and action.kind == ClaudeCodeActionKind.UNKNOWN_PROMPT
            and _INTERRUPT_MENU_RE.search(action.prompt_text)
        )

    def _is_interrupt_menu_snapshot(
        self,
        task: _ControllerTask,
        snapshot: ClaudeCodeSnapshot,
    ) -> bool:
        current_round = task.active_round
        interrupt_cursor = (
            current_round.interrupt_requested_cursor
            if current_round is not None
            else None
        )
        return bool(
            current_round is not None
            and task.interrupt_requested
            and current_round.interrupt_confirmed
            and interrupt_cursor is not None
            and snapshot.raw_cursor > interrupt_cursor
            and self._snapshot_has_active_process(snapshot)
            and self._is_interrupt_menu_action(snapshot.action_required)
            and snapshot.action_required is not None
            and snapshot.action_required.cursor_end > interrupt_cursor
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
        latest_round = self._stored_round_result_locked(task, None)
        if task.active_round is None and latest_round is not None:
            return latest_round.outcome
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
        round_id: str | None = None,
        round_terminal_state: ClaudeCodeState | None = None,
    ) -> ClaudeCodeControllerResult:
        current = snapshot or self._current_snapshot_locked(task)
        current_round = task.active_round
        if round_id is None and current_round is not None:
            round_id = current_round.round_id
        result = ClaudeCodeControllerResult(
            snapshot=current,
            outcome=outcome,
            observation_count=task.observation_count,
            consecutive_empty_reads=task.consecutive_empty_reads,
            output_used=task.output_used,
            deadline_remaining=max(0.0, task.deadline - self._now()),
            limits_hit=tuple(sorted(task.limits_hit)),
            initial_instruction_submitted=task.initial_instruction_submitted,
            round_id=round_id,
            round_terminal_state=round_terminal_state,
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

    @staticmethod
    def _process_terminal_round_state(
        snapshot: ClaudeCodeSnapshot,
        *,
        outcome: ClaudeCodeControllerOutcome,
    ) -> ClaudeCodeState:
        """沿用现有真实进程终态映射，为正在执行的轮次保留明确结果。"""

        if snapshot.state in _TERMINAL_STATES:
            return snapshot.state
        if snapshot.process_status == "lost":
            return ClaudeCodeState.LOST
        if snapshot.process_status == "killed" or outcome == (
            ClaudeCodeControllerOutcome.TERMINATED
        ):
            return ClaudeCodeState.INTERRUPTED
        if snapshot.process_status == "failed_start" or (
            snapshot.exit_code is not None and snapshot.exit_code != 0
        ):
            return ClaudeCodeState.FAILED
        if snapshot.process_status == "exited":
            return ClaudeCodeState.FAILED
        return ClaudeCodeState.LOST

    def _stored_round_result_locked(
        self,
        task: _ControllerTask,
        round_id: str | None,
    ) -> ClaudeCodeControllerResult | None:
        if round_id is None:
            if task.active_round is not None:
                return None
            latest_round_id = task.latest_terminal_round_id
            if latest_round_id is None:
                return None
            result = task.terminal_round_results.get(latest_round_id)
            if result is not None:
                task.terminal_round_results.move_to_end(latest_round_id)
            return result
        result = task.terminal_round_results.get(round_id)
        if result is not None:
            task.terminal_round_results.move_to_end(round_id)
        return result

    def _assert_active_round_matches_locked(
        self,
        task: _ControllerTask,
        round_id: str | None,
    ) -> None:
        if round_id is None:
            return
        current_round = task.active_round
        if current_round is None or current_round.round_id != round_id:
            raise self._round_not_found_error(task.process_id, round_id)

    def _assert_send_round_matches_locked(
        self,
        task: _ControllerTask,
        round_id: str,
    ) -> None:
        """仅允许基于当前 active round 或最近已终结 round 创建/补充任务。"""

        current_round = task.active_round
        if current_round is not None:
            if current_round.round_id != round_id:
                if round_id in task.terminal_round_results:
                    raise self._round_mismatch_error(task.process_id, round_id)
                raise self._round_not_found_error(task.process_id, round_id)
            return
        if task.latest_terminal_round_id is None:
            raise self._round_not_found_error(task.process_id, round_id)
        if task.latest_terminal_round_id != round_id:
            if round_id in task.terminal_round_results:
                raise self._round_mismatch_error(task.process_id, round_id)
            raise self._round_not_found_error(task.process_id, round_id)

    @staticmethod
    def _round_not_found_error(
        process_id: str,
        round_id: str,
    ) -> ClaudeCodeControllerError:
        return ClaudeCodeControllerError(
            "controller_round_not_found",
            "Claude Code task round was not found",
            details={"process_id": process_id, "round_id": round_id},
        )

    @staticmethod
    def _round_mismatch_error(
        process_id: str,
        round_id: str,
    ) -> ClaudeCodeControllerError:
        """调用方指定的 round 曾有效，但已不是可提交新指令的当前身份。"""

        return ClaudeCodeControllerError(
            "controller_round_mismatch",
            "Claude Code task round is no longer current",
            details={"process_id": process_id, "round_id": round_id},
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
        *,
        round_id: str | None = None,
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
                if round_id is None:
                    return None, terminal.result
                result = terminal.round_results.get(round_id)
                if result is not None:
                    terminal.round_results.move_to_end(round_id)
                    return None, result
                raise self._round_not_found_error(process_id, round_id)
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
                round_results=OrderedDict(task.terminal_round_results),
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
    def _require_round_id(round_id: str | None) -> None:
        if round_id is None:
            return
        if not isinstance(round_id, str) or not round_id:
            raise ClaudeCodeControllerError(
                "controller_round_not_found",
                "round_id must be a non-empty string or None",
            )

    @staticmethod
    def _require_explicit_round_id(round_id: object) -> None:
        """新指令必须携带调用方已知的 round，不允许按“最新”自动选择。"""

        if not isinstance(round_id, str) or not round_id.strip():
            raise ClaudeCodeControllerError(
                "controller_round_not_found",
                "round_id is required for a Claude Code instruction",
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
