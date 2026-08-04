"""Claude Code 终态完成通知的内存观察器。"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from hermes.claude_code.controller import (
    ClaudeCodeController,
    ClaudeCodeControllerError,
    ClaudeCodeControllerOutcome,
    ClaudeCodeControllerResult,
)
from hermes.claude_code.contracts import (
    ClaudeCodeRuntimeError,
    ClaudeCodeState,
)
from hermes.claude_code.normalizer import redact_claude_code_output
from hermes.claude_code.notification import (
    ClaudeCodeNotificationPort,
    ClaudeCodeNotificationReceipt,
    ClaudeCodeNotificationTarget,
    ClaudeCodeTerminalNotification,
)


_TERMINAL_STATES = frozenset(
    {
        ClaudeCodeState.COMPLETED,
        ClaudeCodeState.FAILED,
        ClaudeCodeState.INTERRUPTED,
        ClaudeCodeState.LOST,
    }
)
_MAX_CLOSED_WATCHES = 128
_MAX_DISPLAY_NAME_CHARS = 160


class ClaudeCodeCompletionWatchState(str, Enum):
    """Watch 的当前生命周期，不替代 Controller 或 ProcessStatus。"""

    ACTIVE = "active"
    TERMINAL_DETECTED = "terminal_detected"
    NOTIFICATION_PENDING = "notification_pending"
    NOTIFICATION_ACCEPTED = "notification_accepted"
    NOTIFICATION_FAILED = "notification_failed"
    CLOSED = "closed"


class ClaudeCodeCompletionWatcherError(ClaudeCodeRuntimeError):
    """Watcher 的结构化错误，不暴露 Claude Code 输入或底层异常正文。"""


@dataclass(frozen=True, slots=True)
class ClaudeCodeCompletionWatcherPolicy:
    """限制轮询、通知和关闭行为，避免忙轮询或无限通知重试。"""

    poll_interval: float = 5.0
    max_concurrent_polls: int = 4
    notification_output_tail_limit: int = 4_096
    notification_retry_interval: float = 5.0
    notification_enqueue_attempts: int = 3
    shutdown_timeout: float = 5.0

    def __post_init__(self) -> None:
        self._require_seconds("poll_interval", self.poll_interval, 0.1, 3_600.0)
        self._require_positive_int(
            "max_concurrent_polls",
            self.max_concurrent_polls,
            64,
        )
        self._require_positive_int(
            "notification_output_tail_limit",
            self.notification_output_tail_limit,
            16_384,
        )
        self._require_seconds(
            "notification_retry_interval",
            self.notification_retry_interval,
            0.1,
            3_600.0,
        )
        self._require_positive_int(
            "notification_enqueue_attempts",
            self.notification_enqueue_attempts,
            10,
        )
        self._require_seconds("shutdown_timeout", self.shutdown_timeout, 0.1, 60.0)

    @staticmethod
    def _require_seconds(
        field_name: str,
        value: object,
        minimum: float,
        maximum: float,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum
            or value > maximum
        ):
            raise ValueError(
                f"{field_name} must be between {minimum} and {maximum}"
            )

    @staticmethod
    def _require_positive_int(
        field_name: str,
        value: object,
        maximum: int,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > maximum
        ):
            raise ValueError(f"{field_name} must be between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class ClaudeCodeCompletionWatch:
    """不含目标元数据或输出正文的 Watch 对外状态。"""

    watch_id: str
    process_id: str
    session_owner: str
    target_id: str
    display_name: str | None
    state: ClaudeCodeCompletionWatchState
    terminal_state: ClaudeCodeState | None
    notification_id: str | None
    notification_attempts: int
    last_error_type: str | None
    created_at: float
    updated_at: float
    round_id: str | None = None


@dataclass(slots=True)
class _CompletionWatchRecord:
    """仅在进程内保存单个 Watch 的有限投递状态。"""

    watch_id: str
    process_id: str
    round_id: str
    session_owner: str
    cwd: str
    target: ClaudeCodeNotificationTarget = field(repr=False)
    display_name: str | None
    created_at: float
    updated_at: float
    state: ClaudeCodeCompletionWatchState = ClaudeCodeCompletionWatchState.ACTIVE
    terminal_state: ClaudeCodeState | None = None
    notification_id: str | None = None
    notification: ClaudeCodeTerminalNotification | None = field(
        default=None,
        repr=False,
    )
    notification_attempts: int = 0
    next_attempt_at: float = 0.0
    last_error_type: str | None = None
    in_flight: bool = False
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class ClaudeCodeCompletionWatcher:
    """用一个受控 asyncio Task 观察多个既有 Controller task。"""

    def __init__(
        self,
        controller: ClaudeCodeController,
        notification_port: ClaudeCodeNotificationPort,
        *,
        policy: ClaudeCodeCompletionWatcherPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(controller, ClaudeCodeController):
            raise TypeError("controller must be a ClaudeCodeController")
        if not isinstance(notification_port, ClaudeCodeNotificationPort):
            raise TypeError("notification_port must implement ClaudeCodeNotificationPort")
        if policy is not None and not isinstance(
            policy,
            ClaudeCodeCompletionWatcherPolicy,
        ):
            raise TypeError("policy must be a ClaudeCodeCompletionWatcherPolicy")
        if not callable(clock) or not callable(wall_clock):
            raise TypeError("clock and wall_clock must be callable")
        self._controller = controller
        self._notification_port = notification_port
        self._policy = policy or ClaudeCodeCompletionWatcherPolicy()
        self._clock = clock
        self._wall_clock = wall_clock
        self._guard = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._poll_semaphore = asyncio.Semaphore(
            self._policy.max_concurrent_polls
        )
        self._records_by_round: dict[
            tuple[str, str, str],
            _CompletionWatchRecord,
        ] = {}
        self._records_by_watch_id: dict[str, _CompletionWatchRecord] = {}
        self._closed_watches: OrderedDict[str, ClaudeCodeCompletionWatch] = (
            OrderedDict()
        )
        self._closed_watch_ids_by_round: OrderedDict[
            tuple[str, str, str],
            str,
        ] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._shutting_down = False
        self._closed = False

    @property
    def is_shutdown(self) -> bool:
        """说明该实例已关闭，默认入口可在下一 Gateway 生命周期替换它。"""

        return self._closed

    def uses_notification_port(self, notification_port: object) -> bool:
        """仅供默认组合根确认同一实例没有被不同 Gateway 复用。"""

        return self._notification_port is notification_port

    async def start(self) -> None:
        """启动唯一后台扫描 Task；重复调用保持幂等。"""

        async with self._guard:
            if self._closed or self._shutting_down:
                raise ClaudeCodeCompletionWatcherError(
                    "watcher_shutting_down",
                    "Claude Code completion watcher is shutting down",
                )
            task = self._task
            if task is not None and not task.done():
                self._started = True
                return
            self._started = True
            self._task = asyncio.create_task(
                self._run(),
                name="claude-code-completion-watcher",
            )

    async def shutdown(self) -> None:
        """停止观察自身，不 interrupt、kill、cleanup 或删除任何 CC task。"""

        async with self._guard:
            if self._closed:
                return
            self._shutting_down = True
            self._started = False
            task = self._task
            self._task = None
            self._records_by_round.clear()
            self._records_by_watch_id.clear()
            self._wakeup.set()
            if task is not None and not task.done():
                task.cancel()
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=self._policy.shutdown_timeout,
                )
            except asyncio.TimeoutError:
                if not task.done():
                    task.cancel()
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                raise
            except Exception:
                # Watcher 的单轮错误不得升级为 CC 生命周期操作。
                pass
        async with self._guard:
            self._closed = True
            self._shutting_down = False

    async def register_watch(
        self,
        *,
        process_id: str,
        session_owner: str,
        notification_target: ClaudeCodeNotificationTarget,
        display_name: str | None = None,
        round_id: str | None = None,
    ) -> ClaudeCodeCompletionWatch:
        """验证既有 Controller task 后注册一个进程内完成通知。"""

        process_id = self._require_identifier("process_id", process_id, 512)
        session_owner = self._require_identifier("session_owner", session_owner, 1_024)
        if round_id is not None:
            round_id = self._require_identifier("round_id", round_id, 512)
        if not isinstance(notification_target, ClaudeCodeNotificationTarget):
            raise ClaudeCodeCompletionWatcherError(
                "watch_target_invalid",
                "Claude Code notification target is invalid",
            )
        if notification_target.session_owner is None:
            raise ClaudeCodeCompletionWatcherError(
                "watch_target_invalid",
                "Claude Code notification target has no owner binding",
                details={"process_id": process_id},
            )
        if notification_target.session_owner != session_owner:
            raise ClaudeCodeCompletionWatcherError(
                "watch_owner_mismatch",
                "Claude Code notification target belongs to another owner",
                details={"process_id": process_id},
            )
        display_name = self._normalize_display_name(display_name)
        async with self._guard:
            self._require_started_locked()

        try:
            result = await asyncio.to_thread(
                self._controller.snapshot,
                session_owner=session_owner,
                process_id=process_id,
                round_id=round_id,
            )
        except ClaudeCodeControllerError as error:
            if error.error_type == "controller_owner_mismatch":
                raise ClaudeCodeCompletionWatcherError(
                    "watch_owner_mismatch",
                    "Claude Code completion watch owner does not match the task",
                    details={"process_id": process_id},
                ) from error
            if error.error_type == "controller_task_not_found":
                raise ClaudeCodeCompletionWatcherError(
                    "watch_not_found",
                    "Claude Code Controller task was not found",
                    details={"process_id": process_id},
                ) from error
            if error.error_type == "controller_round_not_found":
                raise ClaudeCodeCompletionWatcherError(
                    "watch_round_unavailable",
                    "Claude Code task round was not found",
                    details={"process_id": process_id},
                ) from error
            raise ClaudeCodeCompletionWatcherError(
                "controller_poll_failed",
                "Claude Code Controller task could not be inspected",
                retryable=error.retryable,
                details={"process_id": process_id},
            ) from error

        snapshot = result.snapshot
        if (
            snapshot.session_ref.process_id != process_id
            or snapshot.session_ref.session_owner != session_owner
        ):
            raise ClaudeCodeCompletionWatcherError(
                "watch_owner_mismatch",
                "Claude Code Controller returned a mismatched task identity",
                details={"process_id": process_id},
            )

        resolved_round_id = result.round_id
        if resolved_round_id is None:
            raise ClaudeCodeCompletionWatcherError(
                "watch_round_unavailable",
                "Claude Code completion watch requires a submitted task round",
                details={"process_id": process_id},
            )
        if round_id is not None and resolved_round_id != round_id:
            raise ClaudeCodeCompletionWatcherError(
                "watch_round_unavailable",
                "Claude Code Controller returned another task round",
                details={"process_id": process_id},
            )

        now = self._now()
        record = _CompletionWatchRecord(
            watch_id=self._make_watch_id(
                process_id,
                session_owner,
                resolved_round_id,
            ),
            process_id=process_id,
            round_id=resolved_round_id,
            session_owner=session_owner,
            cwd=snapshot.session_ref.cwd,
            target=notification_target,
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        async with self._guard:
            self._require_started_locked()
            self._assert_round_available_locked(
                process_id,
                session_owner,
                resolved_round_id,
            )
            self._records_by_round[
                self._round_key(
                    process_id,
                    session_owner,
                    resolved_round_id,
                )
            ] = record
            self._records_by_watch_id[record.watch_id] = record
            if result.terminal:
                record.in_flight = True
            self._wakeup.set()

        if result.terminal:
            try:
                await self._poll_record(record)
            finally:
                async with self._guard:
                    if self._records_by_watch_id.get(record.watch_id) is record:
                        record.in_flight = False
        return await self.get_watch(record.watch_id)

    async def unregister_watch(self, watch_id: str) -> ClaudeCodeCompletionWatch:
        """停止一个未关闭 Watch；不影响其 Controller task 或底层进程。"""

        watch_id = self._require_identifier("watch_id", watch_id, 512)
        async with self._guard:
            record = self._records_by_watch_id.get(watch_id)
            if record is None:
                closed = self._closed_watches.get(watch_id)
                if closed is not None:
                    return closed
                raise ClaudeCodeCompletionWatcherError(
                    "watch_not_found",
                    "Claude Code completion watch was not found",
                )
        async with record.operation_lock:
            async with self._guard:
                current = self._records_by_watch_id.get(watch_id)
                if current is not record:
                    closed = self._closed_watches.get(watch_id)
                    if closed is not None:
                        return closed
                    raise ClaudeCodeCompletionWatcherError(
                        "watch_not_found",
                        "Claude Code completion watch was not found",
                    )
                return self._close_record_locked(
                    record,
                    ClaudeCodeCompletionWatchState.CLOSED,
                )

    async def get_watch(self, watch_id: str) -> ClaudeCodeCompletionWatch:
        """读取单个 Watch 的有限状态，不返回 target metadata 或输出正文。"""

        watch_id = self._require_identifier("watch_id", watch_id, 512)
        async with self._guard:
            record = self._records_by_watch_id.get(watch_id)
            if record is not None:
                return self._public_watch(record)
            closed = self._closed_watches.get(watch_id)
            if closed is not None:
                self._closed_watches.move_to_end(watch_id)
                return closed
        raise ClaudeCodeCompletionWatcherError(
            "watch_not_found",
            "Claude Code completion watch was not found",
        )

    async def list_watches(
        self,
        *,
        include_closed: bool = False,
    ) -> tuple[ClaudeCodeCompletionWatch, ...]:
        """列出当前 Watch；关闭历史仅保留有限的无正文状态。"""

        if not isinstance(include_closed, bool):
            raise TypeError("include_closed must be a boolean")
        async with self._guard:
            watches = [
                self._public_watch(record)
                for record in self._records_by_watch_id.values()
            ]
            if include_closed:
                watches.extend(self._closed_watches.values())
            return tuple(watches)

    async def _run(self) -> None:
        """在单个 Task 中受控扫描；单个 Watch 的失败彼此隔离。"""

        try:
            while True:
                try:
                    await self._scan_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 所有可见的单 Watch 错误已经保存在其有限状态中。
                    pass
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(),
                        timeout=self._policy.poll_interval,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _scan_once(self) -> None:
        now = self._now()
        async with self._guard:
            if self._closed or self._shutting_down:
                return
            candidates: list[_CompletionWatchRecord] = []
            for record in self._records_by_watch_id.values():
                if record.in_flight or record.next_attempt_at > now:
                    continue
                if record.state not in {
                    ClaudeCodeCompletionWatchState.ACTIVE,
                    ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING,
                }:
                    continue
                record.in_flight = True
                candidates.append(record)
        if candidates:
            await asyncio.gather(
                *(self._run_candidate(record) for record in candidates),
                return_exceptions=True,
            )

    async def _run_candidate(self, record: _CompletionWatchRecord) -> None:
        try:
            async with self._guard:
                current = self._records_by_watch_id.get(record.watch_id)
                if current is not record:
                    return
                state = record.state
            if state == ClaudeCodeCompletionWatchState.ACTIVE:
                await self._poll_record(record)
            elif state == ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING:
                await self._submit_pending_notification(record)
        finally:
            async with self._guard:
                current = self._records_by_watch_id.get(record.watch_id)
                if current is record:
                    record.in_flight = False

    async def _poll_record(self, record: _CompletionWatchRecord) -> None:
        """以 Controller.poll 的结果作为唯一终态事实，不读取 Runtime。"""

        try:
            async with self._poll_semaphore:
                result = await asyncio.to_thread(
                    self._controller.poll,
                    session_owner=record.session_owner,
                    process_id=record.process_id,
                    terminal_observation=True,
                    round_id=record.round_id,
                )
        except ClaudeCodeControllerError as error:
            await self._record_controller_error(record, error)
            return
        except Exception:
            await self._record_error(
                record,
                "controller_poll_failed",
                retry_at=self._now() + self._policy.notification_retry_interval,
            )
            return
        if result.round_id != record.round_id:
            await self._record_error(
                record,
                "controller_round_mismatch",
                retry_at=self._now() + self._policy.notification_retry_interval,
            )
            return
        if result.terminal:
            await self._handle_terminal_result(record, result)

    async def _record_controller_error(
        self,
        record: _CompletionWatchRecord,
        error: ClaudeCodeControllerError,
    ) -> None:
        if error.error_type == "controller_owner_mismatch":
            async with record.operation_lock:
                async with self._guard:
                    if self._records_by_watch_id.get(record.watch_id) is record:
                        self._close_record_locked(
                            record,
                            ClaudeCodeCompletionWatchState.CLOSED,
                            error_type="watch_owner_mismatch",
                        )
            return
        if error.error_type == "controller_task_not_found":
            await self._handle_controller_task_lost(record)
            return
        if error.error_type == "controller_round_not_found":
            async with record.operation_lock:
                async with self._guard:
                    if self._records_by_watch_id.get(record.watch_id) is record:
                        self._close_record_locked(
                            record,
                            ClaudeCodeCompletionWatchState.CLOSED,
                            error_type="watch_round_unavailable",
                        )
            return
        if error.error_type == "terminal_observation_reserve_exhausted":
            async with record.operation_lock:
                async with self._guard:
                    if self._records_by_watch_id.get(record.watch_id) is record:
                        self._close_record_locked(
                            record,
                            ClaudeCodeCompletionWatchState.NOTIFICATION_FAILED,
                            error_type=error.error_type,
                        )
            return
        await self._record_error(
            record,
            "controller_poll_failed",
            retry_at=self._now() + self._policy.notification_retry_interval,
        )

    async def _handle_controller_task_lost(
        self,
        record: _CompletionWatchRecord,
    ) -> None:
        """仅把已验证且仍 active 的 Watch 丢失转换为无 Snapshot 的 LOST 通知。"""

        async with record.operation_lock:
            async with self._guard:
                if self._records_by_watch_id.get(record.watch_id) is not record:
                    return
                if record.state != ClaudeCodeCompletionWatchState.ACTIVE:
                    return
                target_owner = record.target.session_owner
                if target_owner is None:
                    self._close_record_locked(
                        record,
                        ClaudeCodeCompletionWatchState.CLOSED,
                        error_type="watch_target_invalid",
                    )
                    return
                if target_owner != record.session_owner:
                    self._close_record_locked(
                        record,
                        ClaudeCodeCompletionWatchState.CLOSED,
                        error_type="watch_owner_mismatch",
                    )
                    return
                if record.notification is not None:
                    return
                record.state = ClaudeCodeCompletionWatchState.TERMINAL_DETECTED
                record.updated_at = self._now()
            try:
                notification = self._build_controller_task_lost_notification(
                    record
                )
            except Exception:
                async with self._guard:
                    if self._records_by_watch_id.get(record.watch_id) is record:
                        self._close_record_locked(
                            record,
                            ClaudeCodeCompletionWatchState.NOTIFICATION_FAILED,
                            error_type="terminal_notification_build_failed",
                        )
                return
            async with self._guard:
                if self._records_by_watch_id.get(record.watch_id) is not record:
                    return
                record.notification = notification
                record.notification_id = notification.notification_id
                record.terminal_state = notification.terminal_state
                record.state = ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING
                record.updated_at = self._now()
            await self._submit_pending_notification_locked(record)

    async def _handle_terminal_result(
        self,
        record: _CompletionWatchRecord,
        result: ClaudeCodeControllerResult,
    ) -> None:
        """冻结 Controller final drain 后的安全 Snapshot，并立即尝试入 Outbox。"""

        if not result.terminal:
            return
        async with record.operation_lock:
            async with self._guard:
                if self._records_by_watch_id.get(record.watch_id) is not record:
                    return
                if record.notification is not None:
                    return
                record.state = ClaudeCodeCompletionWatchState.TERMINAL_DETECTED
                record.updated_at = self._now()
            try:
                notification = self._build_terminal_notification(record, result)
            except Exception:
                async with self._guard:
                    if self._records_by_watch_id.get(record.watch_id) is record:
                        self._close_record_locked(
                            record,
                            ClaudeCodeCompletionWatchState.NOTIFICATION_FAILED,
                            error_type="terminal_notification_build_failed",
                        )
                return
            async with self._guard:
                if self._records_by_watch_id.get(record.watch_id) is not record:
                    return
                record.notification = notification
                record.notification_id = notification.notification_id
                record.terminal_state = notification.terminal_state
                record.state = ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING
                record.updated_at = self._now()
            await self._submit_pending_notification_locked(record)

    async def _submit_pending_notification(
        self,
        record: _CompletionWatchRecord,
    ) -> None:
        async with record.operation_lock:
            await self._submit_pending_notification_locked(record)

    async def _submit_pending_notification_locked(
        self,
        record: _CompletionWatchRecord,
    ) -> None:
        """以同一 notification_id 有界重试入队，不接管平台发送重试。"""

        async with self._guard:
            if self._records_by_watch_id.get(record.watch_id) is not record:
                return
            if (
                record.state != ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING
                or record.notification is None
            ):
                return
            record.notification_attempts += 1
            record.updated_at = self._now()
            notification = record.notification
            target = record.target
        try:
            receipt = await self._notification_port.submit_terminal_notification(
                target=target,
                notification=notification,
            )
            if not isinstance(receipt, ClaudeCodeNotificationReceipt):
                raise TypeError("notification port returned an invalid receipt")
            if receipt.notification_id != notification.notification_id:
                raise ValueError("notification port changed notification identity")
        except asyncio.CancelledError:
            raise
        except Exception:
            receipt = ClaudeCodeNotificationReceipt(
                accepted=False,
                notification_id=notification.notification_id,
                retryable=True,
                error_type="notification_enqueue_failed",
            )

        async with self._guard:
            if self._records_by_watch_id.get(record.watch_id) is not record:
                return
            if receipt.accepted:
                self._close_record_locked(
                    record,
                    ClaudeCodeCompletionWatchState.NOTIFICATION_ACCEPTED,
                )
                return
            error_type = receipt.error_type or "notification_enqueue_failed"
            retryable = receipt.retryable or error_type == "notification_delivery_unknown"
            if (
                retryable
                and record.notification_attempts
                < self._policy.notification_enqueue_attempts
            ):
                record.state = ClaudeCodeCompletionWatchState.NOTIFICATION_PENDING
                record.last_error_type = error_type
                record.next_attempt_at = (
                    self._now() + self._policy.notification_retry_interval
                )
                record.updated_at = self._now()
                return
            self._close_record_locked(
                record,
                ClaudeCodeCompletionWatchState.NOTIFICATION_FAILED,
                error_type=error_type,
            )

    async def _record_error(
        self,
        record: _CompletionWatchRecord,
        error_type: str,
        *,
        retry_at: float,
    ) -> None:
        async with self._guard:
            if self._records_by_watch_id.get(record.watch_id) is not record:
                return
            record.last_error_type = error_type
            record.next_attempt_at = retry_at
            record.updated_at = self._now()

    def _build_terminal_notification(
        self,
        record: _CompletionWatchRecord,
        result: ClaudeCodeControllerResult,
    ) -> ClaudeCodeTerminalNotification:
        """只从 Controller 结构化安全 Snapshot 构造有限、再次脱敏的通知。"""

        snapshot = result.snapshot
        terminal_state = self._terminal_state(result)
        output = redact_claude_code_output(snapshot.normalized_output).strip()
        tail_limit = self._policy.notification_output_tail_limit
        if len(output) > tail_limit:
            output = output[-tail_limit:]
        notification_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "hermes:claude-code-terminal:"
                    f"{record.session_owner}:{record.process_id}:"
                    f"{record.round_id}:{terminal_state.value}"
                ),
            )
        )
        return ClaudeCodeTerminalNotification(
            notification_id=notification_id,
            watch_id=record.watch_id,
            process_id=record.process_id,
            session_owner=record.session_owner,
            cwd=snapshot.session_ref.cwd,
            terminal_state=terminal_state,
            controller_outcome=result.outcome.value,
            process_status=snapshot.process_status,
            exit_code=snapshot.exit_code,
            completed_at=self._wall_now(),
            safe_output_tail=output,
            limits_hit=result.limits_hit,
        )

    def _build_controller_task_lost_notification(
        self,
        record: _CompletionWatchRecord,
    ) -> ClaudeCodeTerminalNotification:
        """在 Controller task 消失时仅使用注册时已验证的安全最小信息。"""

        terminal_state = ClaudeCodeState.LOST
        return ClaudeCodeTerminalNotification(
            notification_id=self._lost_notification_id(record),
            watch_id=record.watch_id,
            process_id=record.process_id,
            session_owner=record.session_owner,
            cwd=record.cwd,
            terminal_state=terminal_state,
            controller_outcome="controller_task_not_found",
            process_status=None,
            exit_code=None,
            completed_at=self._wall_now(),
            safe_output_tail="",
            limits_hit=(),
        )

    @staticmethod
    def _lost_notification_id(record: _CompletionWatchRecord) -> str:
        """同一 Watch 的 LOST 通知始终复用同一稳定身份。"""

        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "hermes:claude-code-terminal:"
                    f"{record.session_owner}:{record.process_id}:"
                    f"{record.round_id}:"
                    f"{ClaudeCodeState.LOST.value}"
                ),
            )
        )

    @staticmethod
    def _terminal_state(
        result: ClaudeCodeControllerResult,
    ) -> ClaudeCodeState:
        """按既有 Controller 终态合同保守映射，不以成功 exit code 猜测完成。"""

        if result.round_terminal_state is not None:
            return result.round_terminal_state
        snapshot = result.snapshot
        if snapshot.state in _TERMINAL_STATES:
            return snapshot.state
        if snapshot.process_status == "lost":
            return ClaudeCodeState.LOST
        if snapshot.process_status == "killed" or result.outcome == (
            ClaudeCodeControllerOutcome.TERMINATED
        ):
            return ClaudeCodeState.INTERRUPTED
        if snapshot.process_status == "failed_start" or (
            snapshot.exit_code is not None and snapshot.exit_code != 0
        ):
            return ClaudeCodeState.FAILED
        if snapshot.process_status == "exited":
            # 缺少 Detector 完成证据时，即使 exit code 为零也不能声称 completed。
            return ClaudeCodeState.FAILED
        return ClaudeCodeState.LOST

    def _close_record_locked(
        self,
        record: _CompletionWatchRecord,
        state: ClaudeCodeCompletionWatchState,
        *,
        error_type: str | None = None,
    ) -> ClaudeCodeCompletionWatch:
        """在注册表锁内注销 Watch，只保留有限且无正文的历史结果。"""

        record.state = state
        record.last_error_type = error_type
        record.next_attempt_at = 0.0
        record.updated_at = self._now()
        public = self._public_watch(record)
        round_key = self._round_key(
            record.process_id,
            record.session_owner,
            record.round_id,
        )
        self._records_by_round.pop(round_key, None)
        self._records_by_watch_id.pop(record.watch_id, None)
        self._closed_watches[record.watch_id] = public
        self._closed_watches.move_to_end(record.watch_id)
        self._closed_watch_ids_by_round[round_key] = record.watch_id
        self._closed_watch_ids_by_round.move_to_end(round_key)
        while len(self._closed_watches) > _MAX_CLOSED_WATCHES:
            _, expired = self._closed_watches.popitem(last=False)
            expired_key = self._round_key(
                expired.process_id,
                expired.session_owner,
                expired.round_id,
            )
            if self._closed_watch_ids_by_round.get(expired_key) == expired.watch_id:
                self._closed_watch_ids_by_round.pop(expired_key, None)
        return public

    def _assert_round_available_locked(
        self,
        process_id: str,
        session_owner: str,
        round_id: str,
    ) -> None:
        """同一进程的不同任务轮次可并存，但同一轮次只允许一个 Watch。"""

        round_key = self._round_key(process_id, session_owner, round_id)
        existing = self._records_by_round.get(round_key)
        if existing is not None:
            if existing.session_owner != session_owner:
                raise ClaudeCodeCompletionWatcherError(
                    "watch_owner_mismatch",
                    "Claude Code completion watch belongs to another owner",
                    details={"process_id": process_id},
                )
            raise ClaudeCodeCompletionWatcherError(
                "watch_already_registered",
                "Claude Code completion watch is already registered",
                details={"process_id": process_id},
            )
        closed_watch_id = self._closed_watch_ids_by_round.get(round_key)
        if closed_watch_id is None:
            return
        closed = self._closed_watches.get(closed_watch_id)
        if closed is not None and closed.session_owner != session_owner:
            raise ClaudeCodeCompletionWatcherError(
                "watch_owner_mismatch",
                "Claude Code completion watch belongs to another owner",
                details={"process_id": process_id},
            )
        raise ClaudeCodeCompletionWatcherError(
            "watch_already_registered",
            "Claude Code completion watch is already registered",
            details={"process_id": process_id},
        )

    def _require_started_locked(self) -> None:
        if self._closed or self._shutting_down:
            raise ClaudeCodeCompletionWatcherError(
                "watcher_shutting_down",
                "Claude Code completion watcher is shutting down",
            )
        if not self._started:
            raise ClaudeCodeCompletionWatcherError(
                "watcher_not_started",
                "Claude Code completion watcher has not been started",
            )

    @staticmethod
    def _normalize_display_name(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("display_name must be a string or None")
        normalized = " ".join(value.split())
        if not normalized:
            return None
        if len(normalized) > _MAX_DISPLAY_NAME_CHARS:
            raise ValueError("display_name exceeds the supported length")
        return normalized

    @staticmethod
    def _require_identifier(field_name: str, value: object, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ClaudeCodeCompletionWatcherError(
                "watch_target_invalid",
                f"Claude Code completion watch requires {field_name}",
            )
        if len(value) > maximum:
            raise ClaudeCodeCompletionWatcherError(
                "watch_target_invalid",
                f"Claude Code completion watch {field_name} is too long",
            )
        return value

    @staticmethod
    def _round_key(
        process_id: str,
        session_owner: str,
        round_id: str,
    ) -> tuple[str, str, str]:
        return (session_owner, process_id, round_id)

    @staticmethod
    def _make_watch_id(
        process_id: str,
        session_owner: str,
        round_id: str,
    ) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "hermes:claude-code-watch:"
                    f"{session_owner}:{process_id}:{round_id}"
                ),
            )
        )

    @staticmethod
    def _public_watch(record: _CompletionWatchRecord) -> ClaudeCodeCompletionWatch:
        return ClaudeCodeCompletionWatch(
            watch_id=record.watch_id,
            process_id=record.process_id,
            round_id=record.round_id,
            session_owner=record.session_owner,
            target_id=record.target.target_id,
            display_name=record.display_name,
            state=record.state,
            terminal_state=record.terminal_state,
            notification_id=record.notification_id,
            notification_attempts=record.notification_attempts,
            last_error_type=record.last_error_type,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise RuntimeError("watcher clock returned a non-finite value")
        return value

    def _wall_now(self) -> float:
        value = float(self._wall_clock())
        if not math.isfinite(value) or value < 0:
            raise RuntimeError("watcher wall_clock returned an invalid value")
        return value


__all__ = [
    "ClaudeCodeCompletionWatch",
    "ClaudeCodeCompletionWatcher",
    "ClaudeCodeCompletionWatcherError",
    "ClaudeCodeCompletionWatcherPolicy",
    "ClaudeCodeCompletionWatchState",
]
