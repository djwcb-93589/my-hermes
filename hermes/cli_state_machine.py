"""默认 CLI 的单 worker 路由与数据库连接边界。"""

from __future__ import annotations

from collections import deque
import json
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable, Protocol

from hermes.cli_approval import execute_cli_approval
from hermes.claude_code.invocation_context import (
    prepare_cli_claude_code_invocation,
)
from hermes.claude_code.agent_adapter import (
    ClaudeCodeAgentAdapter,
    ClaudeCodeOwner,
    create_cli_claude_code_continuation_grant,
)
from hermes.claude_code.continuation import (
    ClaudeCodeContinuationStore,
    ClaudeCodePendingInteraction,
    classify_continuation_error,
    is_startup_interaction_resolved,
    render_claude_code_interaction,
    safe_observation_from_controller_result,
    startup_interaction_resolved_message,
)
from hermes.claude_code.contracts import ClaudeCodeRuntimeError
from hermes.claude_code.request_detector import (
    ClaudeCodeRequestOperation,
    detect_claude_code_request,
)
from hermes.config import DB_PATH
from hermes.conversation import run_conversation
from hermes.hooks import SyncHookRegistry
from hermes.db import (
    create_session,
    ensure_session,
    get_session_messages,
    init_db,
    list_cli_sessions,
    replace_tool_message_content,
    session_exists,
)
from hermes.session_idle import SessionProcessLifecycle
from hermes.steering import SteerEntry, SteerMailbox


if TYPE_CHECKING:
    from hermes.session_resources import IdleSessionCleanupReport


logger = logging.getLogger(__name__)

DEFAULT_CLI_MESSAGE_QUEUE_LIMIT = 20
DEFAULT_CLI_IDLE_TIMEOUT_SECONDS = 86400.0
DEFAULT_CLI_IDLE_CLEANUP_INTERVAL_SECONDS = 600.0


def _finite_seconds(
    value: object,
    field_name: str,
    *,
    allow_zero: bool,
) -> float:
    """按 Gateway 风格校验 CLI idle 秒数配置。"""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if (
        not math.isfinite(seconds)
        or seconds < 0
        or (not allow_zero and seconds == 0)
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field_name} must be a finite {qualifier} number")
    return seconds


class CLIProcessManager(SessionProcessLifecycle, Protocol):
    """CLI idle maintenance 需要的共享 ProcessManager 最小接口。"""

    def prune(self) -> None:
        """删除超过 TTL 的终态 Process 记录。"""


class CLIMessageQueue:
    """由终端主线程使用的有界普通消息队列。"""

    def __init__(self, limit: int = DEFAULT_CLI_MESSAGE_QUEUE_LIMIT) -> None:
        if limit <= 0:
            raise ValueError("CLI message queue limit must be positive")
        self._limit = limit
        self._messages: deque[str] = deque()

    def enqueue(self, message: str) -> bool:
        """在未达到上限时保存一条原始用户文本。"""
        if self.is_full():
            return False
        self._messages.append(message)
        return True

    def peek(self) -> str | None:
        """查看下一条消息，但不改变队列。"""
        return self._messages[0] if self._messages else None

    def dequeue(self) -> str | None:
        """取出最早进入队列的消息。"""
        return self._messages.popleft() if self._messages else None

    def restore_front(self, messages: list[str] | tuple[str, ...]) -> None:
        """按原顺序把未消费的 steer 文本放回普通队首。"""
        restored = tuple(messages)
        if any(not isinstance(message, str) for message in restored):
            raise TypeError("restored CLI messages must be strings")
        self._messages.extendleft(reversed(restored))

    def clear(self) -> int:
        """清空尚未提交的消息，并返回清空数量。"""
        count = len(self._messages)
        self._messages.clear()
        return count

    def is_empty(self) -> bool:
        """返回是否没有待处理消息。"""
        return not self._messages

    def is_full(self) -> bool:
        """返回是否已经达到消息上限。"""
        return len(self._messages) >= self._limit

    @property
    def limit(self) -> int:
        """返回当前队列允许保存的最大消息数。"""
        return self._limit

    def __len__(self) -> int:
        return len(self._messages)


@dataclass(frozen=True)
class CLIWorkerTask:
    """主线程交给 CLI worker 的单项工作。"""

    kind: str
    session_id: str | None = None
    user_input: str = ""
    cached_prompt: str = ""
    tool_policy: object | None = None
    tool_context: dict | None = None
    approval_request: dict | None = None
    approval_scope: str = "once"
    current_session_id: str | None = None
    cancel_event: threading.Event | None = None
    steer_mailbox: SteerMailbox | None = None
    last_activity_at: float | None = None
    idle_timeout_seconds: float | None = None
    foreground_active: bool = False
    deferred_session_id: str | None = None
    continuation_pending: dict | None = None


@dataclass(frozen=True)
class CLISessionReleaseResult:
    """worker 返回的单个旧 session 释放结果。"""

    session_id: str
    completed: bool
    retry: bool
    reason: str
    error_type: str | None = None


@dataclass(frozen=True)
class CLIWorkerResult:
    """worker 完成一项工作后交回主线程的结果。"""

    kind: str
    session_id: str | None = None
    conversation_result: dict | None = None
    sessions: tuple[dict, ...] = ()
    messages: tuple[dict, ...] = ()
    current_session_id: str | None = None
    error: str | None = None
    pending_steer: tuple[SteerEntry, ...] = ()
    idle_cleanup_report: IdleSessionCleanupReport | None = None
    idle_cleanup_error_type: str | None = None
    prior_session_release: CLISessionReleaseResult | None = None
    deferred_session_release: CLISessionReleaseResult | None = None


class CLIEventType(str, Enum):
    """CLI 内部事件的来源类型。"""

    USER_INPUT = "user_input"
    WORKER_RESULT = "worker_result"
    STREAM_EVENT = "stream_event"
    CANCEL_REQUEST = "cancel_request"
    MAINTENANCE_TICK = "maintenance_tick"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class CLIEvent:
    """交给 CLI controller 串行处理的一项输入或后台通知。"""

    event_type: CLIEventType
    user_input: str = ""
    worker_result: CLIWorkerResult | None = None
    stream_event: object | None = None

    @classmethod
    def user_input_event(cls, user_input: str) -> "CLIEvent":
        return cls(event_type=CLIEventType.USER_INPUT, user_input=user_input)

    @classmethod
    def worker_result_event(cls, result: CLIWorkerResult) -> "CLIEvent":
        return cls(event_type=CLIEventType.WORKER_RESULT, worker_result=result)

    @classmethod
    def stream_event_event(cls, stream_event: object) -> "CLIEvent":
        return cls(event_type=CLIEventType.STREAM_EVENT, stream_event=stream_event)

    @classmethod
    def shutdown_event(cls) -> "CLIEvent":
        return cls(event_type=CLIEventType.SHUTDOWN)

    @classmethod
    def cancel_request_event(cls) -> "CLIEvent":
        return cls(event_type=CLIEventType.CANCEL_REQUEST)

    @classmethod
    def maintenance_tick_event(cls) -> "CLIEvent":
        return cls(event_type=CLIEventType.MAINTENANCE_TICK)


class CLIEventQueue:
    """连接 CLI UI、controller 和 worker 的线程安全事件队列。"""

    def __init__(self) -> None:
        self._events: queue.Queue[CLIEvent] = queue.Queue()
        self._maintenance_lock = threading.Lock()
        self._maintenance_pending = False

    def put(self, event: CLIEvent) -> None:
        self._events.put(event)

    def get(self) -> CLIEvent:
        return self._events.get()

    def post_user_input(self, user_input: str) -> None:
        self.put(CLIEvent.user_input_event(user_input))

    def post_worker_result(self, result: CLIWorkerResult) -> None:
        self.put(CLIEvent.worker_result_event(result))

    def post_stream_event(self, stream_event: object) -> None:
        self.put(CLIEvent.stream_event_event(stream_event))

    def post_shutdown(self) -> None:
        self.put(CLIEvent.shutdown_event())

    def post_cancel_request(self) -> None:
        self.put(CLIEvent.cancel_request_event())

    def post_maintenance_tick(self) -> bool:
        """最多保留一个尚未被 Controller 消费的 maintenance tick。"""

        with self._maintenance_lock:
            if self._maintenance_pending:
                return False
            self._maintenance_pending = True
        self.put(CLIEvent.maintenance_tick_event())
        return True

    def acknowledge_maintenance_tick(self) -> None:
        """由 Controller 在消费 maintenance tick 时释放投递资格。"""

        with self._maintenance_lock:
            self._maintenance_pending = False


class CLIMaintenanceTicker:
    """单线程定期发布去重 maintenance 事件，不接触运行资源。"""

    def __init__(
        self,
        *,
        interval_seconds: float,
        publish_tick: Callable[[], bool],
    ) -> None:
        self._interval_seconds = _finite_seconds(
            interval_seconds,
            "idle_cleanup_interval_seconds",
            allow_zero=False,
        )
        self._publish_tick = publish_tick
        self._shutdown_event = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-cli-maintenance",
            daemon=True,
        )

    def start(self) -> None:
        """启动唯一 maintenance ticker。"""

        if self._started:
            raise RuntimeError("CLI maintenance ticker has already started")
        self._started = True
        self._thread.start()

    def shutdown(self) -> None:
        """唤醒并等待 ticker 退出。"""

        if not self._started:
            return
        self._shutdown_event.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._shutdown_event.wait(self._interval_seconds):
            try:
                self._publish_tick()
            except Exception:
                logger.warning("CLI maintenance tick publish failed")


class CLIWorker:
    """串行执行 CLI 工作，并让每项数据库工作在线程内独占连接。"""

    def __init__(
        self,
        *,
        stream_sink: Callable[[object], None] | None,
        publish_result: Callable[[CLIWorkerResult], None],
        hook_registry: SyncHookRegistry | None = None,
        process_manager: CLIProcessManager | None = None,
        claude_code_adapter: ClaudeCodeAgentAdapter | None = None,
    ) -> None:
        if hook_registry is not None and not isinstance(
            hook_registry,
            SyncHookRegistry,
        ):
            raise TypeError("hook_registry must be a SyncHookRegistry or None")
        self._stream_sink = stream_sink
        self._publish_result = publish_result
        self._hook_registry = hook_registry
        self._process_manager = process_manager
        # 仅在真正收到 continuation 回复时惰性创建，避免普通 CLI 启动触碰 CC runtime。
        self._claude_code_adapter = claude_code_adapter
        self._tasks: queue.Queue[CLIWorkerTask | None] = queue.Queue()
        self._results: queue.Queue[CLIWorkerResult] = queue.Queue()
        self._lock = threading.Lock()
        self._accepting = True
        self._busy = False
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-cli-worker",
            daemon=False,
        )

    def start(self) -> None:
        """启动单一 worker 线程。"""
        self._thread.start()

    def submit(self, task: CLIWorkerTask) -> bool:
        """仅在没有未消费工作结果时接受下一项工作。"""
        with self._lock:
            if not self._accepting or self._busy:
                return False
            self._busy = True
        self._tasks.put(task)
        return True

    def is_busy(self) -> bool:
        """返回 worker 是否仍有运行中或待主线程消费的工作。"""
        with self._lock:
            return self._busy

    def drain_results(self) -> list[CLIWorkerResult]:
        """让主线程消费完成结果，并释放下一次提交资格。"""
        results: list[CLIWorkerResult] = []
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                break
            results.append(result)

        if results:
            with self._lock:
                self._busy = False
        return results

    def shutdown(self) -> None:
        """等待当前工作收尾后停止线程，避免遗留非守护线程。"""
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        self._tasks.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                result = self._execute(task)
            except Exception as exc:
                deferred_release = (
                    CLISessionReleaseResult(
                        session_id=task.deferred_session_id,
                        completed=False,
                        retry=True,
                        reason="worker_exception",
                        error_type=type(exc).__name__,
                    )
                    if (
                        task.kind == "idle_cleanup"
                        and task.deferred_session_id is not None
                    )
                    else None
                )
                result = CLIWorkerResult(
                    kind=task.kind,
                    session_id=task.session_id,
                    error=f"worker task failed: {type(exc).__name__}",
                    deferred_session_release=deferred_release,
                )
            self._results.put(result)
            try:
                self._publish_result(result)
            except Exception:
                pass

    def _execute(self, task: CLIWorkerTask) -> CLIWorkerResult:
        """为单项工作创建、使用并关闭 SQLite 连接。"""
        if task.kind == "idle_cleanup":
            return self._run_idle_cleanup_task(task)
        if task.kind == "claude_code_reply":
            return self._run_claude_code_reply_task(task)
        conn = init_db(DB_PATH)
        try:
            if task.kind == "conversation":
                return self._run_conversation_task(conn, task)
            if task.kind == "list_sessions":
                sessions = list_cli_sessions(conn, limit=10, offset=0)
                return CLIWorkerResult(
                    kind=task.kind,
                    sessions=tuple(sessions),
                    current_session_id=task.current_session_id,
                )
            if task.kind == "resume":
                return self._resume_session(conn, task)
            if task.kind == "approve":
                return self._approve_and_resume(conn, task)
            if task.kind == "deny":
                return self._deny_approval(conn, task)
            if task.kind == "cancel_approval":
                return self._cancel_pending_approval(conn, task)
            return CLIWorkerResult(
                kind=task.kind,
                error=f"unsupported CLI worker task: {task.kind}",
            )
        finally:
            conn.close()

    def _run_claude_code_reply_task(self, task: CLIWorkerTask) -> CLIWorkerResult:
        """不经过 AgentLoop，按 pending 身份执行一次原样交互回复。"""

        pending = task.continuation_pending
        if task.session_id is None or not isinstance(pending, dict):
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error="claude_code interaction is no longer available",
            )
        try:
            adapter = self._claude_code_adapter
            if adapter is None:
                adapter = ClaudeCodeAgentAdapter()
                self._claude_code_adapter = adapter
            grant = create_cli_claude_code_continuation_grant(
                session_key=task.session_id,
                turn_id=uuid.uuid4().hex,
                created_at=time.time(),
                expires_at=time.time() + 300.0,
            )
            result = adapter.reply_to_interaction(
                grant=grant,
                process_id=str(pending.get("process_id", "")),
                round_id=pending.get("round_id"),
                action_id=str(pending.get("action_id", "")),
                response=task.user_input,
            )
            owner = ClaudeCodeOwner.from_cli_session_key(task.session_id).session_owner
            accepted_pending = ClaudeCodePendingInteraction.from_safe_dict(pending)
            observation = safe_observation_from_controller_result(
                result,
                environment="cli",
                owner=owner,
                conversation_id=str(pending.get("conversation_id", task.session_id)),
                operation="reply",
                accepted_pending=accepted_pending,
            )
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                conversation_result={
                    "claude_code_interaction": observation,
                    "final_response": "",
                    "status": observation.get("continuation_status"),
                },
                error=(
                    startup_interaction_resolved_message()
                    if is_startup_interaction_resolved(observation)
                    else None
                ),
            )
        except ClaudeCodeRuntimeError as error:
            if error.delivery_unknown:
                retained = dict(pending)
                retained["delivery_unknown"] = True
                observation = {
                    "observed": True,
                    "owner": pending.get("owner", ""),
                    "observed_process_id": pending.get("process_id", ""),
                    "observed_round_id": pending.get("round_id"),
                    "action_present": True,
                    "pending": retained,
                    "delivery_unknown": True,
                }
                return CLIWorkerResult(
                    kind=task.kind,
                    session_id=task.session_id,
                    conversation_result={"claude_code_interaction": observation},
                    error="claude_code_delivery_unknown: Claude Code reply delivery is unknown; no retry was sent",
                )
            classification = classify_continuation_error(
                error.error_type,
                retryable=error.retryable,
            )
            if classification != "stale":
                retained = dict(pending)
                observation = {
                    "observed": True,
                    "owner": pending.get("owner", ""),
                    "observed_process_id": pending.get("process_id", ""),
                    "observed_round_id": pending.get("round_id"),
                    "action_present": True,
                    "pending": retained,
                }
                return CLIWorkerResult(
                    kind=task.kind,
                    session_id=task.session_id,
                    conversation_result={"claude_code_interaction": observation},
                    error=(
                        "claude_code_interaction_retryable: "
                        "Claude Code interaction could not be read or delivered; "
                        "no retry was sent"
                    ),
                )
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                conversation_result={
                    "claude_code_interaction": {
                        "observed": True,
                        "clear_identity": {
                            "owner": pending.get("owner", ""),
                            "process_id": pending.get("process_id", ""),
                            "action_id": pending.get("action_id", ""),
                            "round_id": pending.get("round_id"),
                            "strict_round": True,
                        },
                    }
                },
                error=f"claude_code_interaction_stale: {error.safe_message}",
            )
        except (TypeError, ValueError):
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                conversation_result={
                    "claude_code_interaction": {
                        "observed": True,
                        "owner": pending.get("owner", ""),
                        "observed_process_id": pending.get("process_id", ""),
                        "observed_round_id": pending.get("round_id"),
                        "action_present": True,
                        "pending": dict(pending),
                    }
                },
                error=(
                    "claude_code_interaction_retryable: "
                    "Claude Code interaction reply was rejected; no retry was sent"
                ),
            )

    def _run_idle_cleanup_task(
        self,
        task: CLIWorkerTask,
    ) -> CLIWorkerResult:
        """在现有 CLI worker 中串行执行当前与旧 session 维护。"""

        process_manager = self._process_manager
        if task.session_id is None and task.deferred_session_id is None:
            return CLIWorkerResult(
                kind=task.kind,
                error="idle cleanup task is invalid",
            )

        idle_cleanup_report = None
        idle_cleanup_error_type = None
        if task.session_id is not None:
            if (
                task.last_activity_at is None
                or task.idle_timeout_seconds is None
                or process_manager is None
            ):
                idle_cleanup_error_type = "InvalidMaintenanceTask"
            else:
                from hermes.session_resources import (
                    cleanup_idle_session_resources,
                )

                try:
                    idle_cleanup_report = cleanup_idle_session_resources(
                        task.session_id,
                        last_activity_at=task.last_activity_at,
                        idle_timeout_seconds=task.idle_timeout_seconds,
                        foreground_active=task.foreground_active,
                        process_manager=process_manager,
                    )
                except Exception as error:
                    idle_cleanup_error_type = type(error).__name__

        deferred_release = None
        deferred_session_id = task.deferred_session_id
        if deferred_session_id is not None:
            if deferred_session_id == task.current_session_id:
                deferred_release = CLISessionReleaseResult(
                    session_id=deferred_session_id,
                    completed=False,
                    retry=False,
                    reason="current_session",
                )
            else:
                deferred_release = self._release_session_resources(
                    deferred_session_id,
                )

        return CLIWorkerResult(
            kind=task.kind,
            session_id=task.session_id,
            idle_cleanup_report=idle_cleanup_report,
            idle_cleanup_error_type=idle_cleanup_error_type,
            deferred_session_release=deferred_release,
        )

    @staticmethod
    def _close_and_drain_steer_mailbox(
        mailbox: SteerMailbox | None,
    ) -> tuple[SteerEntry, ...]:
        """异常路径尽力收口 mailbox，且不覆盖原始 Worker 异常。"""
        if mailbox is None:
            return ()
        try:
            return mailbox.close_and_drain()
        except Exception:
            return ()

    def _run_conversation_task(self, conn, task: CLIWorkerTask) -> CLIWorkerResult:
        session_id = task.session_id
        if session_id is None:
            session_id = create_session(conn)
        else:
            # 显式 Claude Code 首轮可能先在 Controller 生成 session key，Worker
            # 只负责建立对应持久化 session，不把 Grant 写入数据库。
            ensure_session(conn, session_id, source="cli")
        steer_kwargs = (
            {"steer_mailbox": task.steer_mailbox}
            if task.steer_mailbox is not None
            else {}
        )
        try:
            result = run_conversation(
                task.user_input,
                conn,
                session_id,
                task.cached_prompt,
                session_key=session_id,
                cancel_checker=(
                    task.cancel_event.is_set if task.cancel_event is not None else None
                ),
                tool_policy=task.tool_policy,
                tool_context=task.tool_context,
                stream_sink=self._stream_sink,
                hook_registry=self._hook_registry,
                **steer_kwargs,
            )
        except Exception as exc:
            pending_steer = self._close_and_drain_steer_mailbox(
                task.steer_mailbox,
            )
            return CLIWorkerResult(
                kind=task.kind,
                session_id=session_id,
                error=f"worker task failed: {type(exc).__name__}",
                pending_steer=pending_steer,
            )
        return CLIWorkerResult(
            kind=task.kind,
            session_id=session_id,
            conversation_result=result,
        )

    def _resume_session(self, conn, task: CLIWorkerTask) -> CLIWorkerResult:
        session_id = task.session_id
        if not session_id or not session_exists(conn, session_id, source="cli"):
            return CLIWorkerResult(
                kind=task.kind,
                error=f"session not found: {session_id or ''}",
            )
        messages = tuple(get_session_messages(conn, session_id))
        old_session_id = task.current_session_id
        prior_session_release = None
        if old_session_id and old_session_id != session_id:
            prior_session_release = self._release_session_before_resume(
                old_session_id,
            )
        return CLIWorkerResult(
            kind=task.kind,
            session_id=session_id,
            messages=messages,
            prior_session_release=prior_session_release,
        )

    def _release_session_before_resume(
        self,
        session_id: str,
    ) -> CLISessionReleaseResult:
        """按共享 Process 保护策略尽力释放离开的旧 session。"""

        return self._release_session_resources(session_id)

    def _release_session_resources(
        self,
        session_id: str,
    ) -> CLISessionReleaseResult:
        """评估并尽力清理旧 session，不向 Controller 传递异常对象。"""

        from hermes.session_idle import evaluate_session_release
        from hermes.session_resources import cleanup_session_resources

        process_manager = self._process_manager
        if process_manager is None:
            return CLISessionReleaseResult(
                session_id=session_id,
                completed=False,
                retry=True,
                reason="process_state_unknown",
            )
        try:
            decision = evaluate_session_release(
                session_id,
                foreground_active=False,
                process_manager=process_manager,
            )
        except Exception as error:
            return CLISessionReleaseResult(
                session_id=session_id,
                completed=False,
                retry=True,
                reason="process_state_unknown",
                error_type=type(error).__name__,
            )
        if not decision.cleanup_allowed:
            return CLISessionReleaseResult(
                session_id=session_id,
                completed=False,
                retry=True,
                reason=decision.reason,
            )

        try:
            report = cleanup_session_resources(
                session_id,
                process_manager=process_manager,
            )
        except Exception as error:
            return CLISessionReleaseResult(
                session_id=session_id,
                completed=False,
                retry=True,
                reason="cleanup_exception",
                error_type=type(error).__name__,
            )
        if report.complete:
            return CLISessionReleaseResult(
                session_id=session_id,
                completed=True,
                retry=False,
                reason="cleanup_completed",
            )
        return CLISessionReleaseResult(
            session_id=session_id,
            completed=False,
            retry=True,
            reason="cleanup_incomplete",
        )

    def _approve_and_resume(self, conn, task: CLIWorkerTask) -> CLIWorkerResult:
        if task.session_id is None or task.approval_request is None:
            return CLIWorkerResult(
                kind=task.kind,
                error="approval request is invalid",
            )
        try:
            execute_cli_approval(
                conn,
                session_id=task.session_id,
                request=task.approval_request,
                scope=task.approval_scope,
                cancel_checker=(
                    task.cancel_event.is_set if task.cancel_event is not None else None
                ),
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error=f"approval execution failed: {exc}",
            )

        steer_kwargs = (
            {"steer_mailbox": task.steer_mailbox}
            if task.steer_mailbox is not None
            else {}
        )
        try:
            result = run_conversation(
                "",
                conn,
                task.session_id,
                task.cached_prompt,
                session_key=task.session_id,
                cancel_checker=(
                    task.cancel_event.is_set
                    if task.cancel_event is not None
                    else None
                ),
                resume_from_history=True,
                tool_policy=task.tool_policy,
                stream_sink=self._stream_sink,
                hook_registry=self._hook_registry,
                **steer_kwargs,
            )
        except Exception as exc:
            pending_steer = self._close_and_drain_steer_mailbox(
                task.steer_mailbox,
            )
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error=f"worker task failed: {type(exc).__name__}",
                pending_steer=pending_steer,
            )
        return CLIWorkerResult(
            kind=task.kind,
            session_id=task.session_id,
            conversation_result=result,
        )

    @staticmethod
    def _deny_approval(conn, task: CLIWorkerTask) -> CLIWorkerResult:
        if task.session_id is None or task.approval_request is None:
            return CLIWorkerResult(
                kind=task.kind,
                error="approval request is invalid",
            )
        denied = json.dumps({
            "ok": False,
            "error_type": "approval_denied",
            "error": "operation was denied by the user",
        }, ensure_ascii=False)
        if not replace_tool_message_content(
            conn,
            task.session_id,
            str(task.approval_request.get("tool_call_id", "")),
            denied,
        ):
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error="approval result could not be recorded",
            )
        return CLIWorkerResult(kind=task.kind, session_id=task.session_id)

    @staticmethod
    def _cancel_pending_approval(
        conn,
        task: CLIWorkerTask,
    ) -> CLIWorkerResult:
        """将等待中的审批记录为用户取消，不执行工具或恢复 AgentLoop。"""
        if task.session_id is None or not isinstance(
            task.approval_request,
            dict,
        ):
            return CLIWorkerResult(
                kind=task.kind,
                error="approval request is invalid",
            )
        cancelled = json.dumps({
            "ok": False,
            "error_type": "cancelled",
            "error": "operation was cancelled by the user",
        }, ensure_ascii=False)
        if not replace_tool_message_content(
            conn,
            task.session_id,
            str(task.approval_request.get("tool_call_id", "")),
            cancelled,
        ):
            return CLIWorkerResult(
                kind=task.kind,
                session_id=task.session_id,
                error="approval result could not be recorded",
            )
        return CLIWorkerResult(kind=task.kind, session_id=task.session_id)


class CLIControllerUI(Protocol):
    """controller 需要的终端显示与输入协调能力。"""

    def begin_stream_request(self) -> None: ...

    def handle_stream_event(self, event: object) -> None: ...

    def show_worker_result(self, result: CLIWorkerResult) -> None: ...

    def show_message(self, message: str) -> None: ...

    def allow_next_input(self) -> None: ...

    def stop_input(self) -> None: ...


class CLIController:
    """串行处理 CLI 事件，并独占会话、审批和普通消息队列状态。"""

    def __init__(
        self,
        *,
        events: CLIEventQueue,
        worker: CLIWorker,
        ui: CLIControllerUI,
        cached_prompt: str,
        tool_policy: object,
        process_manager: CLIProcessManager | None = None,
        idle_timeout_seconds: float = DEFAULT_CLI_IDLE_TIMEOUT_SECONDS,
        idle_cleanup_interval_seconds: float = (
            DEFAULT_CLI_IDLE_CLEANUP_INTERVAL_SECONDS
        ),
        continuation_store: ClaudeCodeContinuationStore | None = None,
    ) -> None:
        self._events = events
        self._worker = worker
        self._ui = ui
        self._cached_prompt = cached_prompt
        self._tool_policy = tool_policy
        self._process_manager = process_manager
        self._continuation_store = continuation_store or ClaudeCodeContinuationStore()
        self._session_id: str | None = None
        self._pending_approval: dict | None = None
        self._session_choices: dict[str, str] = {}
        self._message_queue = CLIMessageQueue()
        self._deferred_release_sessions: deque[str] = deque()
        self._deferred_release_members: set[str] = set()
        self._running = False
        self._shutting_down = False
        self._current_cancel_event: threading.Event | None = None
        self._active_steer_mailbox: SteerMailbox | None = None
        self._last_activity_at = time.time()
        self._idle_timeout_seconds = _finite_seconds(
            idle_timeout_seconds,
            "idle_timeout_seconds",
            allow_zero=True,
        )
        self._maintenance_ticker = CLIMaintenanceTicker(
            interval_seconds=idle_cleanup_interval_seconds,
            publish_tick=self._events.post_maintenance_tick,
        )

    @property
    def current_session_id(self) -> str | None:
        """返回 controller 当前持有的只读会话标识。"""

        return self._session_id

    def _mark_activity(self) -> None:
        """只由 CLI 事件线程刷新用户或前台任务活动时间。"""

        self._last_activity_at = time.time()

    def _defer_session_release(self, session_id: str) -> bool:
        """把旧 session 去重加入稳定顺序的延迟回收队列。"""

        if not isinstance(session_id, str) or not session_id.strip():
            return False
        if session_id == self._session_id:
            self._forget_deferred_session(session_id)
            return False
        if session_id in self._deferred_release_members:
            return False
        self._deferred_release_sessions.append(session_id)
        self._deferred_release_members.add(session_id)
        return True

    def _forget_deferred_session(self, session_id: str) -> bool:
        """从延迟回收队列和 membership set 同时移除 session。"""

        removed = session_id in self._deferred_release_members
        self._deferred_release_members.discard(session_id)
        while True:
            try:
                self._deferred_release_sessions.remove(session_id)
                removed = True
            except ValueError:
                return removed

    def _take_next_deferred_session(self) -> str | None:
        """按队首取出一个旧 session，并保持 deque/set 一致。"""

        while self._deferred_release_sessions:
            session_id = self._deferred_release_sessions.popleft()
            if session_id not in self._deferred_release_members:
                continue
            self._deferred_release_members.remove(session_id)
            if session_id == self._session_id:
                continue
            return session_id
        return None

    def _requeue_deferred_session(self, session_id: str) -> bool:
        """把仍需重试的旧 session 追加到队尾。"""

        return self._defer_session_release(session_id)

    def _clear_deferred_sessions(self) -> None:
        """只清空 CLI 私有记录，实际退出清理由全局路径负责。"""

        self._deferred_release_sessions.clear()
        self._deferred_release_members.clear()

    def run(self) -> None:
        """阻塞等待事件，并由单一 ticker 提供去重维护通知。"""

        self._maintenance_ticker.start()
        try:
            while True:
                try:
                    event = self._events.get()
                except KeyboardInterrupt:
                    # 保留主线程收到外部 SIGINT 时的事件化兼容兜底。
                    self._events.post_cancel_request()
                    continue
                self._handle_event(event)
                if (
                    event.event_type == CLIEventType.USER_INPUT
                    and not self._shutting_down
                ):
                    self._ui.allow_next_input()
                if self._shutting_down and not self._running:
                    return
        finally:
            self._maintenance_ticker.shutdown()

    def _handle_event(self, event: CLIEvent) -> None:
        if event.event_type == CLIEventType.USER_INPUT:
            self._handle_user_input(event.user_input)
            return
        if event.event_type == CLIEventType.WORKER_RESULT:
            self._handle_worker_results()
            return
        if event.event_type == CLIEventType.STREAM_EVENT:
            if (
                event.stream_event is not None
                and (
                    self._current_cancel_event is None
                    or not self._current_cancel_event.is_set()
                )
            ):
                self._ui.handle_stream_event(event.stream_event)
            return
        if event.event_type == CLIEventType.CANCEL_REQUEST:
            if self._handle_cancel_request(announce_idle=False):
                self._mark_activity()
            return
        if event.event_type == CLIEventType.MAINTENANCE_TICK:
            self._events.acknowledge_maintenance_tick()
            self._handle_maintenance_tick()
            return
        if event.event_type == CLIEventType.SHUTDOWN:
            self._begin_shutdown()

    def _handle_user_input(self, raw_user_input: str) -> None:
        stripped_user_input = raw_user_input.lstrip()
        literal_input = (
            raw_user_input.startswith("//") or raw_user_input[:1].isspace()
        )
        user_input = (
            stripped_user_input[1:]
            if literal_input and stripped_user_input.startswith("//")
            else raw_user_input.strip()
        )
        if not user_input or (
            not literal_input and user_input.lower() in ("quit", "exit")
        ):
            self._begin_shutdown()
            return

        command, _, command_argument = user_input.partition(" ")
        command = "" if literal_input else command.lower()
        if command in {"/quit", "/exit"}:
            self._begin_shutdown()
            return
        if command == "/stop":
            if self._handle_cancel_request(announce_idle=True):
                self._mark_activity()
            return

        if command in {"/sessions", "/resume", "/new"} and (
            self._running or not self._message_queue.is_empty()
        ):
            self._ui.show_message(
                "cannot change sessions while agent is running or messages are queued."
            )
            return

        if (
            self._running
            and self._active_steer_mailbox is not None
            and not command.startswith("/")
        ):
            self._submit_or_queue_message(user_input)
            return

        if self._pending_approval is not None:
            self._handle_pending_approval(command, command_argument)
            return

        if command in {"/approve", "/deny"}:
            self._ui.show_message("no approval is pending")
            return

        if command == "/new":
            if self._reset_current_session():
                self._mark_activity()
            return

        if command == "/resume":
            selection = command_argument.strip()
            task = (
                CLIWorkerTask(
                    kind="list_sessions",
                    current_session_id=self._session_id,
                )
                if not selection
                else CLIWorkerTask(
                    kind="resume",
                    session_id=self._session_choices.get(selection, selection),
                    current_session_id=self._session_id,
                )
            )
            if self._submit_task(task):
                self._mark_activity()
            return
        if command == "/sessions":
            if self._submit_task(
                CLIWorkerTask(
                    kind="list_sessions",
                    current_session_id=self._session_id,
                )
            ):
                self._mark_activity()
            return
        if command.startswith("/"):
            self._ui.show_message(f"unknown command: {command}")
            return

        self._submit_or_queue_message(user_input)

    def _reset_current_session(self) -> bool:
        """仅在统一资源清理完整成功后放弃当前会话。"""

        session_id = self._session_id
        if session_id is None:
            self._pending_approval = None
            self._ui.show_message(
                "new session will start with your next message"
            )
            return True

        try:
            from hermes.session_resources import cleanup_session_resources

            report = cleanup_session_resources(
                session_id,
                process_manager=self._process_manager,
            )
            cleanup_complete = report.complete
        except Exception:
            cleanup_complete = False

        if not cleanup_complete:
            self._ui.show_message(
                "session cleanup did not complete; current session was kept"
            )
            return False

        self._session_id = None
        try:
            owner = ClaudeCodeOwner.from_cli_session_key(session_id).session_owner
            self._continuation_store.clear(owner)
        except (TypeError, ValueError):
            pass
        self._forget_deferred_session(session_id)
        self._pending_approval = None
        self._ui.show_message(
            "new session will start with your next message"
        )
        return True

    def _handle_pending_approval(self, command: str, argument: str) -> None:
        if self._running:
            self._ui.show_message("agent is running; approval is still pending.")
            return
        if command == "/deny":
            task = CLIWorkerTask(
                kind="deny",
                session_id=self._session_id,
                approval_request=self._pending_approval,
            )
        elif command == "/approve":
            task = CLIWorkerTask(
                kind="approve",
                session_id=self._session_id,
                cached_prompt=self._cached_prompt,
                tool_policy=self._tool_policy,
                approval_request=self._pending_approval,
                approval_scope=argument.strip().lower() or "once",
            )
        else:
            self._ui.show_message("enter /approve [once|session] or /deny")
            return
        if self._session_id is None or not self._submit_task(
            task,
            begins_stream=True,
        ):
            self._ui.show_message("agent is running; approval is still pending.")
            return
        self._mark_activity()

    def _submit_or_queue_message(self, user_input: str) -> bool:
        # continuation 必须先于 steer/普通队列判定，避免原生回复被模型消费。
        if self._route_pending_claude_code_message(user_input):
            return True
        if self._running:
            mailbox = self._active_steer_mailbox
            if mailbox is not None:
                entry = SteerEntry(
                    steer_id=f"cli-steer:{uuid.uuid4().hex}",
                    text=user_input,
                )
                try:
                    submitted = mailbox.submit(entry)
                except Exception:
                    submitted = False
                if submitted:
                    self._mark_activity()
                    self._ui.show_message("已向当前任务发送引导。")
                    return True
        if self._running or not self._message_queue.is_empty():
            if self._message_queue.enqueue(user_input):
                self._mark_activity()
                self._ui.show_message("message queued.")
                return True
            else:
                self._ui.show_message(
                    f"message queue is full (limit: {self._message_queue.limit})."
                )
            return False
        task = self._conversation_task(user_input)
        if self._submit_task(task, begins_stream=True):
            self._mark_activity()
            return True
        if self._message_queue.enqueue(user_input):
            self._mark_activity()
            self._ui.show_message("message queued.")
            return True
        self._ui.show_message(
            f"message queue is full (limit: {self._message_queue.limit})."
        )
        return False

    def _pending_claude_code_for_current_session(self):
        session_id = self._session_id
        if session_id is None:
            return None
        try:
            owner = ClaudeCodeOwner.from_cli_session_key(session_id).session_owner
        except (TypeError, ValueError):
            return None
        return self._continuation_store.get(owner)

    def _route_pending_claude_code_message(self, user_input: str) -> bool:
        """在普通 AgentLoop 前确定性消费当前会话的 Claude Code action。"""

        session_id = self._session_id
        if session_id is None:
            return False
        try:
            owner = ClaudeCodeOwner.from_cli_session_key(session_id).session_owner
        except (TypeError, ValueError):
            return False
        pending = self._continuation_store.get(owner)
        if pending is None:
            return False
        request = detect_claude_code_request(user_input)
        if request is not None:
            if request.operation in {
                ClaudeCodeRequestOperation.POLL,
                ClaudeCodeRequestOperation.REQUEST_INTERRUPT,
                ClaudeCodeRequestOperation.TERMINATE,
            }:
                # 控制请求继续走正常 Grant 流程，不被 pending 当作回复。
                return False
            self._ui.show_message(
                "claude_code_interaction_pending: Claude Code 正在等待交互，请先回复当前提示，不能启动新的任务。"
            )
            return True
        if pending.delivery_unknown:
            self._ui.show_message(
                "claude_code_delivery_unknown: 上一条 Claude Code 回复送达状态未知，请先使用 poll、interrupt 或 terminate。"
            )
            return True
        if pending.originating_conversation_id != session_id:
            self._ui.show_message(
                "claude_code_interaction_pending: 当前会话与待处理交互不匹配，请显式 poll 或切换会话。"
            )
            return True
        task = CLIWorkerTask(
            kind="claude_code_reply",
            session_id=session_id,
            user_input=user_input,
            continuation_pending=pending.to_safe_dict(),
        )
        if self._submit_task(task, begins_stream=False):
            self._mark_activity()
            return True
        self._ui.show_message(
            "claude_code_interaction_retryable: 当前 worker 忙，请稍后重新提交原生回复。"
        )
        return True

    def _conversation_task(self, user_input: str) -> CLIWorkerTask:
        session_id = self._session_id
        tool_policy = self._tool_policy
        tool_context = None
        request = detect_claude_code_request(user_input)
        if request is not None:
            # 首条 start 请求可以在 Worker 建库前生成可信 session key；控制请求
            # 没有现有 session 时则保持默认隐藏，避免凭空创建可接管 owner。
            candidate_session_id = session_id
            if (
                candidate_session_id is None
                and request.operation == ClaudeCodeRequestOperation.START
            ):
                candidate_session_id = str(uuid.uuid4())
            if candidate_session_id is not None:
                from hermes.tools import registry

                invocation = prepare_cli_claude_code_invocation(
                    user_input,
                    session_key=candidate_session_id,
                    base_policy=self._tool_policy,
                    registry=registry,
                    originating_conversation_id=candidate_session_id,
                )
                if invocation is not None:
                    session_id = candidate_session_id
                    tool_policy = invocation.tool_policy
                    tool_context = invocation.tool_context
        # Grant 只在本轮任务对象中短暂传递，绝不进入消息或 session 持久化。
        return CLIWorkerTask(
            kind="conversation",
            session_id=session_id,
            user_input=user_input,
            cached_prompt=self._cached_prompt,
            tool_policy=tool_policy,
            tool_context=tool_context,
        )

    def _submit_task(self, task: CLIWorkerTask, *, begins_stream: bool = False) -> bool:
        if self._shutting_down or self._running:
            return False
        starts_agent = task.kind in {"conversation", "approve"}
        cancel_event = threading.Event() if starts_agent else None
        steer_mailbox = SteerMailbox() if starts_agent else None
        if starts_agent:
            task = replace(
                task,
                cancel_event=cancel_event,
                steer_mailbox=steer_mailbox,
            )
        if begins_stream:
            self._ui.begin_stream_request()
        if not self._worker.submit(task):
            return False
        self._running = True
        self._current_cancel_event = cancel_event
        self._active_steer_mailbox = steer_mailbox
        return True

    def _handle_maintenance_tick(self) -> None:
        """在事件线程筛选维护任务，并交给现有 CLI worker 执行清理。"""

        if self._shutting_down:
            return
        if self._process_manager is None:
            logger.warning(
                "CLI process prune failed: process manager unavailable"
            )
            return
        else:
            try:
                self._process_manager.prune()
            except Exception as error:
                logger.warning(
                    "CLI process prune failed: exception_type=%s",
                    type(error).__name__,
                )

        if self._running:
            return
        foreground_active = bool(
            not self._message_queue.is_empty()
            or self._pending_approval is not None
        )
        current_session_id = self._session_id
        idle_session_id = (
            None if foreground_active else current_session_id
        )
        deferred_session_id = self._take_next_deferred_session()
        if (
            deferred_session_id is not None
            and deferred_session_id == current_session_id
        ):
            self._forget_deferred_session(deferred_session_id)
            deferred_session_id = None

        if idle_session_id is None and deferred_session_id is None:
            return

        submitted = self._submit_task(
            CLIWorkerTask(
                kind="idle_cleanup",
                session_id=idle_session_id,
                deferred_session_id=deferred_session_id,
                current_session_id=current_session_id,
                last_activity_at=self._last_activity_at,
                idle_timeout_seconds=self._idle_timeout_seconds,
                foreground_active=foreground_active,
            )
        )
        if not submitted and deferred_session_id is not None:
            self._requeue_deferred_session(deferred_session_id)

    def _apply_session_release_result(
        self,
        release: CLISessionReleaseResult | None,
        *,
        source: str,
    ) -> None:
        """在事件线程应用旧 session 释放结果并维护公平重试。"""

        if release is None:
            return
        session_id = release.session_id
        if self._shutting_down:
            self._forget_deferred_session(session_id)
            return
        if session_id == self._session_id:
            self._forget_deferred_session(session_id)
            if source == "maintenance":
                logger.debug(
                    "CLI resumed session removed from deferred cleanup"
                )
            return
        if release.completed:
            self._forget_deferred_session(session_id)
            if source == "resume":
                logger.debug(
                    "CLI old session resources cleaned before resume"
                )
            else:
                logger.debug("CLI deferred session cleanup completed")
            return
        if not release.retry:
            self._forget_deferred_session(session_id)
            return

        self._requeue_deferred_session(session_id)
        if release.reason == "active_processes":
            if source == "resume":
                logger.debug(
                    "CLI old session deferred because active processes exist"
                )
            else:
                logger.debug(
                    (
                        "CLI deferred session cleanup will retry: "
                        "reason=active_processes"
                    )
                )
            return
        if release.reason == "process_state_unknown":
            if source == "resume" or release.error_type is not None:
                if release.error_type is None:
                    logger.warning(
                        (
                            "CLI old session deferred because process state "
                            "is unavailable"
                        )
                    )
                else:
                    logger.warning(
                        (
                            "CLI session release deferred because process "
                            "state is unavailable: exception_type=%s"
                        ),
                        release.error_type,
                    )
            else:
                logger.debug(
                    (
                        "CLI deferred session cleanup will retry: "
                        "reason=process_state_unknown"
                    )
                )
            return
        if release.reason == "cleanup_incomplete":
            if source == "resume":
                logger.warning(
                    "CLI old session cleanup incomplete; deferred release queued"
                )
            else:
                logger.warning("CLI deferred session cleanup incomplete")
            return
        logger.warning(
            (
                "CLI deferred session cleanup will retry: "
                "reason=%s exception_type=%s"
            ),
            release.reason,
            release.error_type or "unknown",
        )

    def _handle_worker_results(self) -> None:
        for result in self._worker.drain_results():
            self._running = False
            self._current_cancel_event = None
            self._active_steer_mailbox = None
            if result.kind == "idle_cleanup":
                self._handle_idle_cleanup_result(result)
                self._submit_next_queued_message()
                continue
            self._restore_pending_steer(result)
            self._apply_worker_result(result)
            self._mark_activity()
            if result.kind == "claude_code_reply":
                if result.error is not None:
                    self._ui.show_message(result.error)
            elif result.kind == "cancel_approval" and result.error is None:
                self._ui.show_message("当前审批已取消。")
            else:
                self._ui.show_worker_result(result)
            self._submit_next_queued_message()

    def _handle_idle_cleanup_result(
        self,
        result: CLIWorkerResult,
    ) -> None:
        """仅在事件线程应用当前与 deferred session 的维护结果。"""

        if result.error is not None:
            logger.warning("CLI idle cleanup worker failed")
        elif result.idle_cleanup_error_type is not None:
            logger.warning(
                "CLI idle cleanup failed: exception_type=%s",
                result.idle_cleanup_error_type,
            )
        elif result.session_id is not None and result.idle_cleanup_report is None:
            logger.warning("CLI idle cleanup returned no report")
        elif (
            result.idle_cleanup_report is not None
            and not result.idle_cleanup_report.attempted
        ):
            report = result.idle_cleanup_report
            if report.decision.reason == "active_processes":
                logger.debug(
                    "CLI idle cleanup skipped: active process count=%d",
                    report.decision.active_process_count,
                )
        elif (
            result.idle_cleanup_report is not None
            and not result.idle_cleanup_report.complete
        ):
            logger.warning("CLI idle cleanup incomplete")
        elif (
            result.idle_cleanup_report is not None
            and result.session_id == self._session_id
        ):
            self._mark_activity()
            logger.debug("CLI idle cleanup completed")
        self._apply_session_release_result(
            result.deferred_session_release,
            source="maintenance",
        )

    def _restore_pending_steer(self, result: CLIWorkerResult) -> None:
        """把 AgentLoop 未消费的 steer 文本原序恢复到普通队首。"""
        pending_entries: list[SteerEntry] = []
        conversation_result = result.conversation_result
        if isinstance(conversation_result, dict):
            conversation_pending = conversation_result.get("pending_steer")
            if isinstance(conversation_pending, (list, tuple)):
                pending_entries.extend(conversation_pending)
        pending_entries.extend(result.pending_steer)

        seen_ids: set[str] = set()
        messages: list[str] = []
        for entry in pending_entries:
            if (
                not isinstance(entry, SteerEntry)
                or entry.steer_id in seen_ids
            ):
                continue
            seen_ids.add(entry.steer_id)
            messages.append(entry.text)
        self._message_queue.restore_front(messages)

    def _apply_worker_result(self, result: CLIWorkerResult) -> None:
        if result.kind == "list_sessions":
            if result.error is None:
                self._session_choices = {
                    str(index): str(session["session_id"])
                    for index, session in enumerate(result.sessions, start=1)
                }
            return
        if result.kind == "resume":
            if result.error is None:
                self._session_id = result.session_id
                if (
                    result.session_id is not None
                    and self._forget_deferred_session(result.session_id)
                ):
                    logger.debug(
                        "CLI resumed session removed from deferred cleanup"
                    )
                self._apply_session_release_result(
                    result.prior_session_release,
                    source="resume",
                )
            return
        if result.session_id is not None:
            self._session_id = result.session_id
            self._forget_deferred_session(result.session_id)
        if result.kind in {"deny", "cancel_approval"}:
            if result.error is None:
                self._pending_approval = None
            return
        if result.kind == "claude_code_reply":
            conversation_result = result.conversation_result
            observation = (
                conversation_result.get("claude_code_interaction")
                if isinstance(conversation_result, dict)
                else None
            )
            if isinstance(observation, dict):
                _status, pending = self._continuation_store.apply_observation(observation)
                if pending is not None:
                    rendered = render_claude_code_interaction(pending)
                    if _status == "conflict":
                        rendered = "claude_code_interaction_conflict: another interaction is still pending.\n" + rendered
                    self._ui.show_message(rendered)
            return
        conversation_result = result.conversation_result
        if not isinstance(conversation_result, dict):
            return
        if conversation_result.get("status") == "awaiting_approval":
            request = conversation_result.get("approval_request")
            self._pending_approval = request if isinstance(request, dict) else None
        else:
            self._pending_approval = None
        observation = conversation_result.get("claude_code_interaction")
        if isinstance(observation, dict):
            _status, pending = self._continuation_store.apply_observation(observation)
            if pending is not None:
                rendered = render_claude_code_interaction(pending)
                if _status == "conflict":
                    rendered = "claude_code_interaction_conflict: another interaction is still pending.\n" + rendered
                self._ui.show_message(rendered)

    def _submit_next_queued_message(self) -> None:
        if (
            self._shutting_down
            or self._running
            or self._pending_approval is not None
        ):
            return
        user_input = self._message_queue.peek()
        if user_input is None:
            return
        pending = self._pending_claude_code_for_current_session()
        if pending is not None:
            # 普通队列没有可靠的 action identity，出现 pending 后必须暂停，
            # 不能猜测队列文本是原生回复并送入 AgentLoop。
            self._ui.show_message(
                "claude_code_interaction_pending: 当前有待处理的 Claude Code 交互，请显式回复或执行控制操作。"
            )
            return
        if self._submit_task(self._conversation_task(user_input), begins_stream=True):
            self._message_queue.dequeue()

    def _begin_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._maintenance_ticker.shutdown()
        self._clear_deferred_sessions()
        cleared = self._message_queue.clear()
        if cleared:
            self._ui.show_message(f"discarded {cleared} queued message(s)")
        self._request_current_cancellation()
        self._ui.stop_input()

    def _handle_cancel_request(
        self,
        *,
        announce_idle: bool,
    ) -> bool:
        if self._pending_approval is not None and not self._running:
            task = CLIWorkerTask(
                kind="cancel_approval",
                session_id=self._session_id,
                approval_request=self._pending_approval,
            )
            if self._session_id is None or not self._submit_task(task):
                self._ui.show_message("审批取消请求未提交，请重试。")
                return False
            else:
                self._ui.show_message("已请求取消当前审批")
                return True
        if not self._running:
            if announce_idle:
                self._ui.show_message("当前没有正在运行的任务。")
            return False
        if self._current_cancel_event is None:
            if announce_idle:
                self._ui.show_message("当前控制任务正在收尾，无法中途停止。")
            return False
        if self._current_cancel_event.is_set():
            if announce_idle:
                self._ui.show_message("当前任务已经请求停止。")
            return False
        self._current_cancel_event.set()
        self._ui.show_message("已请求停止当前任务")
        return True

    def _request_current_cancellation(self) -> None:
        if self._current_cancel_event is not None:
            self._current_cancel_event.set()
