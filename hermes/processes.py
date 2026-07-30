"""线程安全的后台进程核心领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import inspect
import math
import secrets
import threading
import time
from typing import Callable, Final, Protocol


@dataclass(frozen=True, slots=True)
class BackgroundProcessOutput:
    """Handle 一次原子输出读取的结果。"""

    text: str
    discarded_chars: int = 0
    read_error: Exception | None = None


class BackgroundProcessHandle(Protocol):
    """ProcessManager 管理后台进程所需的最小句柄能力。"""

    @property
    def pid(self) -> int | None:
        """返回宿主进程标识；后端没有可用标识时返回 None。"""

    def poll(self) -> int | None:
        """仍在运行时返回 None，结束后返回退出码。"""

    def read_available(self) -> BackgroundProcessOutput:
        """返回新输出、此前丢弃的字符数以及输出读取错误。"""

    def wait(self, timeout: float | None = None) -> int | None:
        """等待结束；超时时返回 None，结束后返回退出码。"""

    def interrupt(self) -> bool:
        """发送协作式终止请求时返回 True；调用前已自然结束时返回 False。

        真实发送失败必须抛出异常。
        """

    def kill(self) -> bool:
        """执行强制终止时返回 True；调用前已自然结束时返回 False。

        所有终止方式失败且目标仍运行时必须抛出异常。
        """

    def close(self) -> None:
        """释放管道、句柄或远端临时资源。"""


class ProcessStatus(str, Enum):
    """后台进程的生命周期状态。"""

    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    LOST = "lost"
    FAILED_START = "failed_start"


class ProcessError(Exception):
    """后台进程领域的基础异常。"""


class ProcessNotFoundError(ProcessError):
    """当前会话中找不到指定进程。"""


class ProcessLimitError(ProcessError):
    """活动后台进程数量超过限制。"""


class ProcessStartError(ProcessError):
    """后台进程启动或注册失败。"""


class ProcessTerminationError(ProcessError):
    """无法确认后台进程已经停止。"""


class ProcessWaitCancelled(ProcessError):
    """调用方取消了等待，但没有终止后台进程。"""


class _ProcessStartFailureContext(Exception):
    """保存启动与清理异常对象，但只呈现脱敏的异常类型。"""

    def __init__(
        self,
        *,
        start_error: BaseException,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self._start_error = start_error
        self._cleanup_error = cleanup_error
        start_error_type = type(start_error).__name__
        cleanup_error_type = (
            None
            if cleanup_error is None
            else type(cleanup_error).__name__
        )
        if cleanup_error_type is None:
            message = (
                "Process start diagnostics: "
                f"start_error={start_error_type}"
            )
        else:
            message = (
                "Process start diagnostics: "
                f"start_error={start_error_type}, "
                f"cleanup_error={cleanup_error_type}"
            )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """供调用方读取的后台进程安全状态快照。"""

    process_id: str
    command: str
    backend_type: str
    cwd: str
    pid: int | None
    status: ProcessStatus
    exit_code: int | None
    started_at: float
    finished_at: float | None
    completion_reason: str | None
    termination_source: str | None
    output_base_cursor: int
    output_end_cursor: int


@dataclass(frozen=True, slots=True)
class ProcessLogResult:
    """一次增量日志查询的结果。"""

    process_id: str
    status: ProcessStatus
    output: str
    requested_cursor: int
    available_from_cursor: int
    next_cursor: int
    output_truncated: bool
    exit_code: int | None


@dataclass(slots=True)
class ProcessRecord:
    """仅供 ProcessManager 使用的可变进程记录。"""

    process_id: str
    session_key: str
    command: str
    backend_type: str
    cwd: str
    pid: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: ProcessStatus = ProcessStatus.STARTING
    exit_code: int | None = None
    completion_reason: str | None = None
    termination_source: str | None = None
    handle: BackgroundProcessHandle | None = field(default=None, repr=False)
    output_buffer: str = field(default="", repr=False)
    output_base_cursor: int = 0
    output_end_cursor: int = 0
    completion_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    record_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )
    handle_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )
    reader_thread: threading.Thread | None = field(default=None, repr=False)
    startup_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )
    termination_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )
    termination_requested: bool = field(default=False, repr=False)
    termination_signal_sent: bool = field(default=False, repr=False)
    termination_in_progress: bool = field(default=False, repr=False)
    close_called: bool = field(default=False, repr=False)
    resource_cleanup_pending: bool = field(default=False, repr=False)
    log_read_error_count: int = field(default=0, repr=False)
    poll_error_count: int = field(default=0, repr=False)


_TERMINAL_STATUSES: Final[frozenset[ProcessStatus]] = frozenset(
    {
        ProcessStatus.EXITED,
        ProcessStatus.KILLED,
        ProcessStatus.LOST,
        ProcessStatus.FAILED_START,
    }
)
_ALLOWED_TRANSITIONS: Final[
    dict[ProcessStatus, frozenset[ProcessStatus]]
] = {
    ProcessStatus.STARTING: frozenset(
        {ProcessStatus.RUNNING, ProcessStatus.FAILED_START}
    ),
    # 读取线程启动失败发生在 running 标记之后时，仍属于启动失败。
    ProcessStatus.RUNNING: frozenset(
        {
            ProcessStatus.EXITED,
            ProcessStatus.KILLED,
            ProcessStatus.LOST,
            ProcessStatus.FAILED_START,
        }
    ),
}
_READER_POLL_INTERVAL_SECONDS: Final[float] = 0.05
_TERMINATION_POLL_INTERVAL_SECONDS: Final[float] = 0.05
_CONSECUTIVE_POLL_ERROR_LIMIT: Final[int] = 3
_FORCE_KILL_WAIT_SECONDS: Final[float] = 5.0
_STARTUP_CLEANUP_WAIT_SECONDS: Final[float] = 5.0
_FAILED_START_DISPOSE_WAIT_SECONDS: Final[float] = 5.0
_FAILED_START_DISPOSE_RETRY_WAIT_SECONDS: Final[float] = 5.0
_MAX_LOG_READ_CHARS: Final[int] = 20_000
_FINAL_DRAIN_READ_LIMIT: Final[int] = 64
_UNSET: Final[object] = object()


class ProcessManager:
    """以会话为边界管理后台进程的线程安全协调器。"""

    def __init__(
        self,
        *,
        max_active_processes_per_session: int = 16,
        max_active_processes_global: int = 64,
        max_output_chars: int = 200_000,
        finished_ttl_seconds: float = 1800,
    ) -> None:
        self._validate_positive_int(
            "max_active_processes_per_session",
            max_active_processes_per_session,
        )
        self._validate_positive_int(
            "max_active_processes_global",
            max_active_processes_global,
        )
        self._validate_positive_int("max_output_chars", max_output_chars)
        self._validate_nonnegative_seconds(
            "finished_ttl_seconds",
            finished_ttl_seconds,
        )

        self._max_active_processes_per_session = (
            max_active_processes_per_session
        )
        self._max_active_processes_global = max_active_processes_global
        self._max_output_chars = max_output_chars
        self._finished_ttl_seconds = float(finished_ttl_seconds)
        self._running: dict[str, ProcessRecord] = {}
        self._finished: dict[str, ProcessRecord] = {}
        self._registry_lock = threading.Lock()
        # 供未来会话资源层记录清理失败；本阶段不对外暴露。
        self._cleanup_failures: list[ProcessSnapshot] = []
        self._cleanup_failure_lock = threading.Lock()

    def spawn(
        self,
        *,
        session_key: str,
        command: str,
        backend_type: str,
        cwd: str,
        starter: Callable[[], BackgroundProcessHandle],
    ) -> ProcessSnapshot:
        """启动并登记一个由调用方提供句柄的后台进程。"""

        self.prune()
        session_key = self._validate_session_key(session_key)
        self._validate_metadata("command", command)
        self._validate_metadata("backend_type", backend_type)
        self._validate_metadata("cwd", cwd)
        if not callable(starter):
            raise ProcessError("starter must be callable")

        with self._registry_lock:
            if len(self._running) >= self._max_active_processes_global:
                raise ProcessLimitError("Active process limit reached")
            active_in_session = sum(
                record.session_key == session_key
                for record in self._running.values()
            )
            if active_in_session >= self._max_active_processes_per_session:
                raise ProcessLimitError("Active process limit reached")

            process_id = self._new_process_id_locked()
            record = ProcessRecord(
                process_id=process_id,
                session_key=session_key,
                command=command,
                backend_type=backend_type,
                cwd=cwd,
            )
            self._running[process_id] = record

        try:
            handle = starter()
            if handle is None:
                raise TypeError("starter returned no process handle")
        except Exception as start_error:
            # 延迟导入 Backend 公共异常，避免模块加载阶段形成循环依赖。
            try:
                from hermes.backends import BackgroundProcessStartCleanupError
            except Exception as import_error:
                self._mark_start_failed(record)
                raise ProcessStartError(
                    "Process start failed"
                ) from _ProcessStartFailureContext(
                    start_error=start_error,
                    cleanup_error=import_error,
                )

            if not isinstance(
                start_error,
                BackgroundProcessStartCleanupError,
            ):
                self._mark_start_failed(record)
                raise ProcessStartError(
                    "Process start failed"
                ) from _ProcessStartFailureContext(
                    start_error=start_error,
                )

            try:
                self._adopt_failed_start_cleanup_handle(
                    record,
                    start_error.handle,
                )
            except Exception as protocol_error:
                self._mark_start_failed(record)
                raise ProcessStartError(
                    "Process start failed"
                ) from _ProcessStartFailureContext(
                    start_error=start_error,
                    cleanup_error=protocol_error,
                )

            raise ProcessStartError(
                "Process start failed and cleanup could not be confirmed"
            ) from _ProcessStartFailureContext(
                start_error=start_error,
            )

        with record.record_lock:
            record.handle = handle

        try:
            pid = self._handle_pid(record)
            if pid is not None and (
                isinstance(pid, bool) or not isinstance(pid, int)
            ):
                raise TypeError("process handle pid must be an integer or None")

            reader_thread = threading.Thread(
                target=self._reader_loop,
                args=(record,),
                name=f"hermes-process-reader-{process_id}",
                daemon=True,
            )
            with record.record_lock:
                record.pid = pid
                termination_requested = record.termination_requested
                termination_source = record.termination_source or "kill"
                self._transition_locked(record, ProcessStatus.RUNNING)
                if not termination_requested:
                    record.reader_thread = reader_thread

            if termination_requested:
                # 清理已在 starter 运行期间登记；不再启动普通读取器。
                record.startup_event.set()
            else:
                reader_thread.start()
                record.startup_event.set()
        except Exception as start_error:
            self._prepare_failed_start_cleanup_record(record)
            try:
                self._dispose_failed_start_handle(record)
            except Exception as cleanup_error:
                try:
                    self._retain_failed_start_cleanup_record(record)
                except Exception:
                    record.startup_event.set()
                raise ProcessStartError(
                    (
                        "Process start failed and cleanup could not "
                        "be confirmed"
                    )
                ) from _ProcessStartFailureContext(
                    start_error=start_error,
                    cleanup_error=cleanup_error,
                )
            else:
                # 清理确认、failed_start 迁移和 close 已在同一流程中完成。
                record.startup_event.set()
                raise ProcessStartError(
                    "Process start failed"
                ) from _ProcessStartFailureContext(
                    start_error=start_error,
                )

        # 与正在进入的 cleanup 串行，避免返回一个已被清理请求接管的 running 快照。
        with record.termination_lock:
            with record.record_lock:
                termination_requested = record.termination_requested
                termination_source = record.termination_source or "kill"
        if termination_requested:
            # 启动期间收到清理请求时，句柄就绪后立即进入终止路径。
            return self._terminate_record(
                record,
                grace_seconds=2.0,
                source=termination_source,
            )

        return self._snapshot(record)

    def list(
        self,
        session_key: str,
        *,
        include_finished: bool = True,
    ) -> tuple[ProcessSnapshot, ...]:
        """列出当前会话可见的后台进程。"""

        session_key = self._validate_session_key(session_key)
        if not isinstance(include_finished, bool):
            raise ProcessError("include_finished must be a boolean")
        self.prune()

        with self._registry_lock:
            records = [
                record
                for record in self._running.values()
                if record.session_key == session_key
            ]
            if include_finished:
                records.extend(
                    record
                    for record in self._finished.values()
                    if record.session_key == session_key
                )

        snapshots = [self._snapshot(record) for record in records]
        snapshots.sort(
            key=lambda snapshot: (
                0 if snapshot.status not in _TERMINAL_STATUSES else 1,
                -snapshot.started_at,
                snapshot.process_id,
            )
        )
        return tuple(snapshots)

    def poll(self, session_key: str, process_id: str) -> ProcessSnapshot:
        """只读且非阻塞地返回一个后台进程的当前快照。"""

        record = self._get_owned_record(session_key, process_id)
        return self._snapshot(record)

    def log(
        self,
        session_key: str,
        process_id: str,
        *,
        cursor: int = 0,
        limit: int = _MAX_LOG_READ_CHARS,
    ) -> ProcessLogResult:
        """读取从绝对 cursor 开始的一段受限滚动日志。"""

        record = self._get_owned_record(session_key, process_id)
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            raise ProcessError("cursor must be an integer")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ProcessError("limit must be an integer")
        if limit <= 0 or limit > _MAX_LOG_READ_CHARS:
            raise ProcessError(
                f"limit must be between 1 and {_MAX_LOG_READ_CHARS}"
            )

        with record.record_lock:
            available_from_cursor = record.output_base_cursor
            output_end_cursor = record.output_end_cursor
            effective_cursor = min(
                max(cursor, available_from_cursor),
                output_end_cursor,
            )
            start_index = effective_cursor - available_from_cursor
            output = record.output_buffer[start_index : start_index + limit]
            next_cursor = effective_cursor + len(output)
            return ProcessLogResult(
                process_id=record.process_id,
                status=record.status,
                output=output,
                requested_cursor=cursor,
                available_from_cursor=available_from_cursor,
                next_cursor=next_cursor,
                output_truncated=cursor < available_from_cursor,
                exit_code=record.exit_code,
            )

    def wait(
        self,
        session_key: str,
        process_id: str,
        *,
        timeout: float | None = None,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ProcessSnapshot:
        """等待结束、超时或调用方取消等待。"""

        record = self._get_owned_record(session_key, process_id)
        timeout_seconds = self._validate_timeout(timeout)
        if cancel_checker is not None and not callable(cancel_checker):
            raise ProcessError("cancel_checker must be callable")

        snapshot = self._snapshot(record)
        if snapshot.status in _TERMINAL_STATUSES:
            return snapshot

        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + timeout_seconds
        )
        while True:
            snapshot = self._snapshot(record)
            if snapshot.status in _TERMINAL_STATUSES:
                return snapshot
            if self._wait_cancelled(cancel_checker):
                raise ProcessWaitCancelled("Process wait cancelled")

            if deadline is None:
                wait_seconds = _READER_POLL_INTERVAL_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._snapshot(record)
                wait_seconds = min(_READER_POLL_INTERVAL_SECONDS, remaining)

            if record.completion_event.wait(wait_seconds):
                return self._snapshot(record)

    def kill(
        self,
        session_key: str,
        process_id: str,
        *,
        grace_seconds: float = 2.0,
    ) -> ProcessSnapshot:
        """先协作式中断，再按需强制终止一个后台进程。"""

        record = self._get_owned_record(session_key, process_id)
        grace_seconds = self._validate_grace_seconds(grace_seconds)
        return self._terminate_record(
            record,
            grace_seconds=grace_seconds,
            source="kill",
        )

    def cleanup_session(self, session_key: str) -> None:
        """终止当前会话的运行中进程，并保留已结束记录至 TTL 到期。"""

        session_key = self._validate_session_key(session_key)
        with self._registry_lock:
            records = tuple(
                record
                for record in self._running.values()
                if record.session_key == session_key
            )

        for record in records:
            try:
                self._terminate_record(
                    record,
                    grace_seconds=0.0,
                    source="session_cleanup",
                )
            except Exception:
                # 单个清理失败不能伪装为成功，也不能阻止其余资源清理。
                self._record_cleanup_failure(record)
                continue

    def cleanup_all(self) -> None:
        """终止全部运行中进程，并保留已结束记录至 TTL 到期。"""

        with self._registry_lock:
            records = tuple(self._running.values())

        for record in records:
            try:
                self._terminate_record(
                    record,
                    grace_seconds=0.0,
                    source="global_cleanup",
                )
            except Exception:
                # 单个清理失败不能伪装为成功，也不能阻止其他资源清理。
                self._record_cleanup_failure(record)
                continue

    def prune(self) -> None:
        """删除超过保留期限的终态记录，不影响运行中进程。"""

        deadline = time.time() - self._finished_ttl_seconds
        with self._registry_lock:
            candidates = tuple(self._finished.items())

        stale_records: list[tuple[str, ProcessRecord]] = []
        for process_id, record in candidates:
            with record.record_lock:
                is_stale = (
                    record.status in _TERMINAL_STATUSES
                    and record.finished_at is not None
                    and record.finished_at <= deadline
                )
            if is_stale:
                stale_records.append((process_id, record))

        if not stale_records:
            return
        with self._registry_lock:
            for process_id, record in stale_records:
                if self._finished.get(process_id) is record:
                    del self._finished[process_id]

    def _new_process_id_locked(self) -> str:
        """在注册表锁保护下生成未被占用的进程标识。"""

        while True:
            process_id = f"proc_{secrets.token_hex(6)}"
            if (
                process_id not in self._running
                and process_id not in self._finished
            ):
                return process_id

    def _get_owned_record(
        self,
        session_key: str,
        process_id: str,
    ) -> ProcessRecord:
        """返回当前会话拥有的记录，隐藏其他会话的存在。"""

        session_key = self._validate_session_key(session_key)
        if not isinstance(process_id, str) or not process_id:
            raise ProcessNotFoundError("Process not found")

        with self._registry_lock:
            record = self._running.get(process_id)
            if record is None:
                record = self._finished.get(process_id)
            if record is None or record.session_key != session_key:
                raise ProcessNotFoundError("Process not found")
            return record

    def _snapshot(self, record: ProcessRecord) -> ProcessSnapshot:
        """在记录锁保护下复制安全的状态字段。"""

        with record.record_lock:
            return ProcessSnapshot(
                process_id=record.process_id,
                command=record.command,
                backend_type=record.backend_type,
                cwd=record.cwd,
                pid=record.pid,
                status=record.status,
                exit_code=record.exit_code,
                started_at=record.started_at,
                finished_at=record.finished_at,
                completion_reason=record.completion_reason,
                termination_source=record.termination_source,
                output_base_cursor=record.output_base_cursor,
                output_end_cursor=record.output_end_cursor,
            )

    def _get_handle(
        self,
        record: ProcessRecord,
    ) -> BackgroundProcessHandle | None:
        """读取当前句柄引用而不向调用方暴露记录本身。"""

        with record.record_lock:
            return record.handle

    def _adopt_failed_start_cleanup_handle(
        self,
        record: ProcessRecord,
        handle: object,
    ) -> None:
        """接管 Backend 无法确认清理、但仍可管理的后台句柄。"""

        if handle is None:
            raise TypeError("recoverable start error has no process handle")

        for operation in (
            "poll",
            "read_available",
            "wait",
            "interrupt",
            "kill",
            "close",
        ):
            try:
                member = getattr(handle, operation)
            except Exception as error:
                raise TypeError(
                    "recoverable process handle does not satisfy protocol"
                ) from error
            if not callable(member):
                raise TypeError(
                    "recoverable process handle does not satisfy protocol"
                )

        try:
            inspect.getattr_static(handle, "pid")
        except AttributeError as error:
            raise TypeError(
                "recoverable process handle does not satisfy protocol"
            ) from error

        with record.record_lock:
            record.handle = handle

        try:
            pid = self._handle_pid(record)
        except Exception:
            # PID 仅供诊断；读取失败不能导致进程树控制权丢失。
            pid = None
        if pid is not None and (
            isinstance(pid, bool) or not isinstance(pid, int)
        ):
            pid = None

        with record.record_lock:
            record.pid = pid

        self._retain_failed_start_cleanup_record(record)

    def _handle_pid(self, record: ProcessRecord) -> int | None:
        """在 Handle 操作锁下读取启动阶段的进程标识。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            return handle.pid

    @staticmethod
    def _validate_handle_output(
        result: object,
    ) -> BackgroundProcessOutput:
        """校验 Handle 返回的公共输出批次。"""

        if not isinstance(result, BackgroundProcessOutput):
            raise TypeError(
                "process handle output must be BackgroundProcessOutput"
            )
        if not isinstance(result.text, str):
            raise TypeError("process handle output text must be a string")
        if (
            isinstance(result.discarded_chars, bool)
            or not isinstance(result.discarded_chars, int)
            or result.discarded_chars < 0
        ):
            raise TypeError(
                "process handle discarded_chars must be a nonnegative integer"
            )
        if (
            result.read_error is not None
            and not isinstance(result.read_error, Exception)
        ):
            raise TypeError(
                "process handle read_error must be an exception or None"
            )
        return result

    def _consume_available_output(
        self,
        record: ProcessRecord,
    ) -> tuple[bool, bool]:
        """原子消费输出并按真实顺序更新 cursor 与读取错误状态。"""

        try:
            handle = self._require_handle(record)
        except Exception:
            self._record_log_read_failure(record)
            raise
        with record.handle_lock:
            try:
                self._assert_handle_open_locked(record, handle)
                result = self._validate_handle_output(
                    handle.read_available()
                )
                self._append_output(
                    record,
                    result.text,
                    discarded_chars=result.discarded_chars,
                )
                if result.read_error is None:
                    self._clear_log_read_failures(record)
                else:
                    self._record_log_read_failure(record)
            except Exception:
                self._record_log_read_failure(record)
                raise
        return (
            bool(result.text or result.discarded_chars),
            result.read_error is not None,
        )

    def _handle_poll(self, record: ProcessRecord) -> int | None:
        """串行查询一次进程退出状态。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            exit_code = handle.poll()
        return self._validate_handle_exit_code(exit_code, "poll result")

    def _handle_wait(
        self,
        record: ProcessRecord,
        timeout: float,
    ) -> int | None:
        """串行等待一次有限时长的进程退出结果。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            exit_code = handle.wait(timeout=timeout)
        return self._validate_handle_exit_code(exit_code, "wait result")

    def _handle_interrupt(
        self,
        record: ProcessRecord,
    ) -> tuple[bool, int | None]:
        """串行请求协作式退出，并保留自然退出与已发送信号的区别。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            signal_sent = self._validate_handle_termination_result(
                handle.interrupt(),
                "interrupt result",
            )
            if signal_sent:
                self._mark_termination_signal_sent(record)
                return True, None
            exit_code = self._validate_handle_exit_code(
                handle.poll(),
                "poll result",
            )
            return False, exit_code

    def _handle_kill(
        self,
        record: ProcessRecord,
    ) -> tuple[bool, int | None]:
        """串行请求强制终止，并保留自然退出与已发送信号的区别。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            signal_sent = self._validate_handle_termination_result(
                handle.kill(),
                "kill result",
            )
            if signal_sent:
                self._mark_termination_signal_sent(record)
                return True, None
            exit_code = self._validate_handle_exit_code(
                handle.poll(),
                "poll result",
            )
            return False, exit_code

    def _handle_cleanup_kill(self, record: ProcessRecord) -> bool:
        """为资源清理强杀 Handle，不再依赖已经失效的 poll 路径。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            signal_sent = self._validate_handle_termination_result(
                handle.kill(),
                "kill result",
            )
            if signal_sent:
                self._mark_termination_signal_sent(record)
            return signal_sent

    def _handle_close(self, record: ProcessRecord) -> None:
        """串行关闭句柄；仅在成功后禁止重复调用。"""

        handle = self._get_handle(record)
        if handle is None:
            return
        with record.handle_lock:
            with record.record_lock:
                if record.close_called or record.handle is not handle:
                    return
            try:
                handle.close()
            except Exception as error:
                raise ProcessTerminationError(
                    "Could not close process handle"
                ) from error
            with record.record_lock:
                if record.handle is handle:
                    record.close_called = True

    def _handle_close_best_effort(self, record: ProcessRecord) -> None:
        """普通退出终态沿用尽力释放，failed-start 与 lost 使用严格入口。"""

        try:
            self._handle_close(record)
        except Exception:
            pass

    def _require_handle(self, record: ProcessRecord) -> BackgroundProcessHandle:
        """短暂读取句柄引用，随后由调用方取得 Handle 操作锁。"""

        handle = self._get_handle(record)
        if handle is None:
            raise ProcessError("Process handle is unavailable")
        return handle

    @staticmethod
    def _validate_handle_exit_code(
        exit_code: object,
        operation: str,
    ) -> int | None:
        """校验 Handle 返回的退出码类型。"""

        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise TypeError(
                f"process handle {operation} must be an integer or None"
            )
        return exit_code

    @staticmethod
    def _validate_handle_termination_result(
        signal_sent: object,
        operation: str,
    ) -> bool:
        """校验 Handle 对终止请求是否真实发送的明确结果。"""

        if not isinstance(signal_sent, bool):
            raise TypeError(
                f"process handle {operation} must be a boolean"
            )
        return signal_sent

    @staticmethod
    def _assert_handle_open_locked(
        record: ProcessRecord,
        handle: BackgroundProcessHandle,
    ) -> None:
        """在 Handle 锁下短暂确认句柄仍可使用。"""

        with record.record_lock:
            if record.close_called or record.handle is not handle:
                raise ProcessError("Process handle is unavailable")

    def _reader_loop(self, record: ProcessRecord) -> None:
        """持续收集输出并在进程完成时原子迁移记录。"""

        try:
            if self._get_handle(record) is None:
                if self._complete_lost(record):
                    return

            while True:
                if self._snapshot(record).status in _TERMINAL_STATUSES:
                    return

                try:
                    self._consume_available_output(record)
                except Exception:
                    pass

                try:
                    exit_code = self._handle_poll(record)
                except Exception:
                    poll_error_count = self._record_poll_failure(record)
                    if (
                        poll_error_count >= _CONSECUTIVE_POLL_ERROR_LIMIT
                        and not self._is_termination_in_progress(record)
                        and not self._is_failed_start_cleanup_pending(record)
                    ):
                        if self._complete_lost(record):
                            return
                else:
                    self._clear_poll_failures(record)
                    if exit_code is not None:
                        if self._is_failed_start_cleanup_pending(record):
                            try:
                                if self._finish_failed_start_recovery(record):
                                    return
                            except Exception:
                                # close 失败时保留可重试记录，且不留下失效线程引用。
                                self._record_cleanup_failure(record)
                                with record.record_lock:
                                    if (
                                        record.reader_thread
                                        is threading.current_thread()
                                    ):
                                        record.reader_thread = None
                                return
                        else:
                            self._finish_confirmed_exit(record, exit_code)
                            return

                time.sleep(_READER_POLL_INTERVAL_SECONDS)
        finally:
            snapshot = self._snapshot(record)
            if (
                snapshot.status not in _TERMINAL_STATUSES
                and not self._is_termination_in_progress(record)
                and not self._is_failed_start_cleanup_pending(record)
            ):
                self._complete_lost(record)
            if (
                self._snapshot(record).status in _TERMINAL_STATUSES
                and not self._is_lost_cleanup_pending(record)
            ):
                self._handle_close_best_effort(record)

    def _append_output(
        self,
        record: ProcessRecord,
        output: str,
        *,
        discarded_chars: int = 0,
    ) -> None:
        """追加输出，并在需要时从缓冲区头部滚动裁剪。"""

        if not output and not discarded_chars:
            return
        with record.record_lock:
            if discarded_chars:
                # Backend 丢失造成不可表示的 cursor 缺口，只能保留其后的连续后缀。
                record.output_end_cursor += discarded_chars
                record.output_buffer = ""
                record.output_base_cursor = record.output_end_cursor
            record.output_buffer += output
            record.output_end_cursor += len(output)
            excess = len(record.output_buffer) - self._max_output_chars
            if excess > 0:
                record.output_buffer = record.output_buffer[excess:]
                record.output_base_cursor += excess

    def _drain_final_output(
        self,
        record: ProcessRecord,
    ) -> None:
        """在检测到退出后有限次数地排空剩余输出。"""

        for _ in range(_FINAL_DRAIN_READ_LIMIT):
            try:
                consumed, _ = self._consume_available_output(record)
            except Exception:
                return
            if not consumed:
                return

    def _complete_observed_exit(
        self,
        record: ProcessRecord,
        exit_code: int,
    ) -> None:
        """依据是否已发送终止信号，将观察到的退出归类为正常或终止。"""

        with record.record_lock:
            termination_signal_sent = record.termination_signal_sent
            termination_source = record.termination_source
        if termination_signal_sent:
            self._finalize_record(
                record,
                ProcessStatus.KILLED,
                exit_code=exit_code,
                completion_reason="killed",
                termination_source=termination_source or "kill",
            )
            return
        self._finalize_record(
            record,
            ProcessStatus.EXITED,
            exit_code=exit_code,
            completion_reason="exited",
            termination_source=None,
        )

    def _complete_lost(self, record: ProcessRecord) -> bool:
        """标记逻辑 lost，并为仍持有的 Handle 保留显式清理责任。"""

        with record.record_lock:
            termination_source = record.termination_source
            if (
                termination_source == "failed_start_cleanup"
                or record.termination_in_progress
            ):
                return False
            transitioned = self._transition_locked(
                record,
                ProcessStatus.LOST,
                exit_code=None,
                completion_reason="backend_lost",
                termination_source=termination_source,
            )
            if not transitioned:
                return False
            resource_cleanup_pending = (
                record.handle is not None and not record.close_called
            )
            record.resource_cleanup_pending = resource_cleanup_pending
            record.completion_event.set()
        if not resource_cleanup_pending:
            self._move_to_finished(record)
        return True

    def _finish_confirmed_exit(
        self,
        record: ProcessRecord,
        exit_code: int | None,
    ) -> ProcessSnapshot:
        """确认退出后先排空尾部日志，再完成记录并关闭句柄。"""

        snapshot = self._snapshot(record)
        if snapshot.status in _TERMINAL_STATUSES:
            return snapshot
        if exit_code is None:
            raise ProcessTerminationError(
                "Could not confirm process termination"
            )

        if self._is_failed_start_cleanup_pending(record):
            raise ProcessTerminationError(
                "Could not confirm process termination"
            )

        self._drain_final_output(record)
        self._complete_observed_exit(record, exit_code)
        snapshot = self._snapshot(record)
        if snapshot.status not in (
            ProcessStatus.EXITED,
            ProcessStatus.KILLED,
        ):
            raise ProcessTerminationError(
                "Could not confirm process termination"
            )
        self._handle_close_best_effort(record)
        return self._snapshot(record)

    def _finalize_record(
        self,
        record: ProcessRecord,
        target_status: ProcessStatus,
        *,
        exit_code: int | None,
        completion_reason: str,
        termination_source: str | None,
    ) -> bool:
        """集中完成终态转换、事件通知与注册表迁移。"""

        with record.record_lock:
            transitioned = self._transition_locked(
                record,
                target_status,
                exit_code=exit_code,
                completion_reason=completion_reason,
                termination_source=termination_source,
            )
            record.completion_event.set()
        self._move_to_finished(record)
        return transitioned

    def _mark_start_failed(
        self,
        record: ProcessRecord,
        *,
        termination_source: str | None = None,
    ) -> None:
        """将启动过程中的异常转换为 failed_start 终态。"""

        with record.record_lock:
            if record.handle is not None and not record.close_called:
                raise RuntimeError(
                    "Process handle must be closed before failed_start"
                )
            self._transition_locked(
                record,
                ProcessStatus.FAILED_START,
                exit_code=None,
                completion_reason="failed_start",
                termination_source=termination_source,
            )
            record.completion_event.set()
        record.startup_event.set()
        self._move_to_finished(record)

    def _transition_locked(
        self,
        record: ProcessRecord,
        target_status: ProcessStatus,
        *,
        exit_code: int | None | object = _UNSET,
        completion_reason: str | None | object = _UNSET,
        termination_source: str | None | object = _UNSET,
    ) -> bool:
        """在记录锁内执行唯一的状态转换入口。"""

        current_status = record.status
        if current_status in _TERMINAL_STATUSES:
            return False
        allowed_targets = _ALLOWED_TRANSITIONS.get(current_status, frozenset())
        if target_status not in allowed_targets:
            raise RuntimeError("Invalid process status transition")

        record.status = target_status
        if exit_code is not _UNSET:
            record.exit_code = exit_code
        if completion_reason is not _UNSET:
            record.completion_reason = completion_reason
        if termination_source is not _UNSET:
            record.termination_source = termination_source
        if target_status in _TERMINAL_STATUSES:
            record.finished_at = time.time()
            record.completion_event.set()
        return True

    def _move_to_finished(self, record: ProcessRecord) -> None:
        """幂等地将一个终态记录从运行表移动到结束表。"""

        with record.record_lock:
            if (
                record.status not in _TERMINAL_STATUSES
                or record.resource_cleanup_pending
            ):
                return
        with self._registry_lock:
            if self._running.get(record.process_id) is record:
                del self._running[record.process_id]
                self._finished[record.process_id] = record

    def _dispose_failed_start_handle(self, record: ProcessRecord) -> None:
        """确认注册失败后的句柄已退出，再写入终态并释放资源。"""

        with record.termination_lock:
            self._set_termination_in_progress(record, True)
            try:
                snapshot = self._snapshot(record)
                if snapshot.status is ProcessStatus.FAILED_START:
                    return
                if snapshot.status in _TERMINAL_STATUSES:
                    raise ProcessTerminationError(
                        "Could not confirm process termination"
                    )
                if self._get_handle(record) is None:
                    raise ProcessError("Process handle is unavailable")

                self._request_failed_start_cleanup(record)
                self._confirm_failed_start_handle_stopped(record)
                self._complete_failed_start_record(record)
            finally:
                self._set_termination_in_progress(record, False)

    def _confirm_failed_start_handle_stopped(
        self,
        record: ProcessRecord,
    ) -> None:
        """通过最多两轮树级强杀与有限等待确认启动失败句柄已停止。"""

        confirmed, exit_code = self._try_confirm_exit(record)
        termination_result_confirmed = False
        last_error: Exception | None = None

        for wait_seconds in (
            _FAILED_START_DISPOSE_WAIT_SECONDS,
            _FAILED_START_DISPOSE_RETRY_WAIT_SECONDS,
        ):
            # 即使根进程已退出，也让后端有机会收敛其受管进程树。
            try:
                signal_sent, immediate_exit_code = self._handle_kill(record)
            except Exception as error:
                last_error = error
            else:
                termination_result_confirmed = True
                if not signal_sent and immediate_exit_code is not None:
                    confirmed = True
                    exit_code = immediate_exit_code

            if not confirmed:
                confirmed, exit_code = self._wait_for_exit_confirmation(
                    record,
                    wait_seconds,
                )
            if confirmed and termination_result_confirmed:
                break

        if not confirmed:
            confirmed, exit_code = self._try_confirm_exit(record)
        if (
            not confirmed
            or exit_code is None
            or not termination_result_confirmed
        ):
            cleanup_error = ProcessTerminationError(
                "Could not confirm process termination"
            )
            if last_error is not None:
                raise cleanup_error from last_error
            raise cleanup_error

    def _complete_failed_start_record(
        self,
        record: ProcessRecord,
    ) -> ProcessSnapshot:
        """排空日志并成功关闭句柄后，再稳定写入 failed_start。"""

        self._drain_final_output(record)
        self._handle_close(record)
        self._mark_start_failed(
            record,
            termination_source="failed_start_cleanup",
        )
        return self._snapshot(record)

    def _retain_failed_start_cleanup_record(
        self,
        record: ProcessRecord,
    ) -> None:
        """保留无法确认清理的记录，使后续会话清理仍能找到它。"""

        self._prepare_failed_start_cleanup_record(record)
        self._start_failed_start_recovery_reader(record)

    def _prepare_failed_start_cleanup_record(
        self,
        record: ProcessRecord,
    ) -> None:
        """在终止尝试前公开可管理状态，避免并发清理误走普通终止路径。"""

        with record.record_lock:
            if record.status is ProcessStatus.STARTING:
                self._transition_locked(record, ProcessStatus.RUNNING)
            if record.status is ProcessStatus.RUNNING:
                record.termination_requested = True
                record.termination_source = "failed_start_cleanup"
        record.startup_event.set()

    def _start_failed_start_recovery_reader(
        self,
        record: ProcessRecord,
    ) -> None:
        """为未确认清理的句柄创建新的恢复监控线程。"""

        try:
            with record.record_lock:
                if (
                    record.status in _TERMINAL_STATUSES
                    or record.handle is None
                    or record.close_called
                ):
                    return
                existing_reader = record.reader_thread
                if (
                    existing_reader is not None
                    and existing_reader.is_alive()
                ):
                    return
                recovery_thread = threading.Thread(
                    target=self._failed_start_recovery_loop,
                    args=(record,),
                    name=f"hermes-process-recovery-{record.process_id}",
                    daemon=True,
                )
                record.reader_thread = recovery_thread
        except Exception:
            return

        try:
            recovery_thread.start()
        except Exception:
            with record.record_lock:
                if record.reader_thread is recovery_thread:
                    record.reader_thread = None

    def _request_failed_start_cleanup(self, record: ProcessRecord) -> None:
        """记录注册失败后的强制清理意图。"""

        with record.record_lock:
            if record.status in _TERMINAL_STATUSES:
                return
            record.termination_requested = True
            record.termination_source = "failed_start_cleanup"

    def _failed_start_recovery_loop(self, record: ProcessRecord) -> None:
        """持续观察未确认清理的记录，并在确认退出后收敛为 failed_start。"""

        while True:
            if self._snapshot(record).status in _TERMINAL_STATUSES:
                return

            try:
                self._consume_available_output(record)
            except Exception:
                pass

            try:
                exit_code = self._handle_poll(record)
            except Exception:
                self._record_poll_failure(record)
            else:
                self._clear_poll_failures(record)
                if exit_code is not None:
                    try:
                        if self._finish_failed_start_recovery(record):
                            return
                    except Exception:
                        # 严格 close 失败时保留 running 记录，等待显式清理重试。
                        self._record_cleanup_failure(record)
                        with record.record_lock:
                            if (
                                record.reader_thread
                                is threading.current_thread()
                            ):
                                record.reader_thread = None
                        return

            time.sleep(_READER_POLL_INTERVAL_SECONDS)

    def _finish_failed_start_recovery(self, record: ProcessRecord) -> bool:
        """确认保留记录已退出后写入 failed_start 并关闭句柄。"""

        with record.termination_lock:
            self._set_termination_in_progress(record, True)
            try:
                if self._snapshot(record).status in _TERMINAL_STATUSES:
                    return True
                confirmed, exit_code = self._try_confirm_exit(record)
                if not confirmed or exit_code is None:
                    return False
                self._complete_failed_start_record(record)
                return True
            finally:
                self._set_termination_in_progress(record, False)

    def _terminate_record(
        self,
        record: ProcessRecord,
        *,
        grace_seconds: float,
        source: str,
    ) -> ProcessSnapshot:
        """在不持有全局注册表锁时执行幂等终止流程。"""

        with record.termination_lock:
            self._set_termination_in_progress(record, True)
            try:
                if self._is_failed_start_cleanup_pending(record):
                    return self._terminate_failed_start_record(
                        record,
                        source=source,
                    )
                if self._is_lost_cleanup_pending(record):
                    return self._terminate_lost_record(
                        record,
                        source=source,
                    )

                snapshot = self._snapshot(record)
                if snapshot.status in _TERMINAL_STATUSES:
                    return snapshot

                if not record.startup_event.is_set():
                    self._request_termination(record, source)
                    # 注册尚未结束时等待统一启动事件，避免抢先写入普通终态。
                    record.startup_event.wait(_STARTUP_CLEANUP_WAIT_SECONDS)
                    snapshot = self._snapshot(record)
                    if self._is_lost_cleanup_pending(record):
                        return self._terminate_lost_record(
                            record,
                            source=source,
                        )
                    if snapshot.status in _TERMINAL_STATUSES:
                        return snapshot
                    if self._is_failed_start_cleanup_pending(record):
                        return self._terminate_failed_start_record(
                            record,
                            source=source,
                        )
                    if not record.startup_event.is_set():
                        raise ProcessTerminationError(
                            "Could not confirm process termination"
                        )

                if snapshot.status is ProcessStatus.STARTING:
                    raise ProcessTerminationError(
                        "Could not confirm process termination"
                    )

                if self._get_handle(record) is None:
                    self._request_termination(record, source)
                    raise ProcessTerminationError(
                        "Could not confirm process termination"
                    )

                confirmed, exit_code = self._try_confirm_exit(record)
                if confirmed:
                    return self._finish_confirmed_exit(record, exit_code)

                self._request_termination(record, source)

                # 发出中断前再次查询，避免将已经自然退出的进程误标为 killed。
                confirmed, exit_code = self._try_confirm_exit(record)
                if confirmed:
                    return self._finish_confirmed_exit(record, exit_code)
                terminal_snapshot = self._terminal_result_or_raise(record)
                if terminal_snapshot is not None:
                    return terminal_snapshot

                try:
                    signal_sent, exit_code = self._handle_interrupt(record)
                except Exception:
                    pass
                else:
                    if not signal_sent and exit_code is not None:
                        return self._finish_confirmed_exit(record, exit_code)

                confirmed, exit_code = self._wait_for_exit_confirmation(
                    record,
                    grace_seconds,
                )
                if confirmed:
                    return self._finish_confirmed_exit(record, exit_code)
                terminal_snapshot = self._terminal_result_or_raise(record)
                if terminal_snapshot is not None:
                    return terminal_snapshot

                try:
                    signal_sent, exit_code = self._handle_kill(record)
                except Exception:
                    pass
                else:
                    if not signal_sent and exit_code is not None:
                        return self._finish_confirmed_exit(record, exit_code)

                confirmed, exit_code = self._wait_for_exit_confirmation(
                    record,
                    _FORCE_KILL_WAIT_SECONDS,
                )
                if confirmed:
                    return self._finish_confirmed_exit(record, exit_code)
                terminal_snapshot = self._terminal_result_or_raise(record)
                if terminal_snapshot is not None:
                    return terminal_snapshot
                raise ProcessTerminationError(
                    "Could not confirm process termination"
                )
            finally:
                self._set_termination_in_progress(record, False)

    def _terminate_failed_start_record(
        self,
        record: ProcessRecord,
        *,
        source: str,
    ) -> ProcessSnapshot:
        """收敛仍需接管的启动失败记录，并保持 failed_start 历史语义。"""

        snapshot = self._snapshot(record)
        if snapshot.status in _TERMINAL_STATUSES:
            return snapshot
        if self._get_handle(record) is None:
            self._request_failed_start_cleanup(record)
            raise ProcessTerminationError(
                "Could not confirm process termination"
            )

        # source 仅表示本次触发者；公开终止来源不被它覆盖。
        self._request_failed_start_cleanup(record)
        self._confirm_failed_start_handle_stopped(record)
        return self._complete_failed_start_record(record)

    def _terminate_lost_record(
        self,
        record: ProcessRecord,
        *,
        source: str,
    ) -> ProcessSnapshot:
        """显式终止并严格释放仍由 lost 记录持有的后台资源。"""

        with record.record_lock:
            cleanup_pending = (
                record.status is ProcessStatus.LOST
                and record.resource_cleanup_pending
            )
            if cleanup_pending:
                record.termination_requested = True
                record.termination_source = source
        if not cleanup_pending:
            return self._snapshot(record)

        if self._get_handle(record) is None:
            raise ProcessTerminationError(
                "Could not confirm process termination"
            )

        try:
            self._handle_cleanup_kill(record)
        except Exception as error:
            raise ProcessTerminationError(
                "Could not confirm process termination"
            ) from error

        self._drain_final_output(record)
        self._handle_close(record)

        with record.record_lock:
            if (
                record.status is not ProcessStatus.LOST
                or not record.close_called
            ):
                raise ProcessTerminationError(
                    "Could not close process handle"
                )
            record.resource_cleanup_pending = False
        self._move_to_finished(record)
        return self._snapshot(record)

    def _request_termination(self, record: ProcessRecord, source: str) -> None:
        """记录终止意图，但尚不改变运行状态。"""

        with record.record_lock:
            if record.status in _TERMINAL_STATUSES:
                return
            record.termination_requested = True
            if record.termination_source is None:
                record.termination_source = source

    def _mark_termination_signal_sent(self, record: ProcessRecord) -> None:
        """标记已经向句柄发送终止信号，供读取器稳定归类退出。"""

        with record.record_lock:
            if (
                record.status not in _TERMINAL_STATUSES
                or (
                    record.status is ProcessStatus.LOST
                    and record.resource_cleanup_pending
                )
            ):
                record.termination_signal_sent = True

    def _try_confirm_exit(
        self,
        record: ProcessRecord,
    ) -> tuple[bool, int | None]:
        """尝试通过状态查询确认退出，失败时保留运行记录。"""

        try:
            exit_code = self._handle_poll(record)
        except Exception:
            return False, None
        return exit_code is not None, exit_code

    def _wait_for_exit_confirmation(
        self,
        record: ProcessRecord,
        timeout: float,
    ) -> tuple[bool, int | None]:
        """优先使用 Handle.wait，并以有限轮询作为状态确认兜底。"""

        deadline = time.monotonic() + timeout
        while True:
            snapshot = self._snapshot(record)
            if snapshot.status in (ProcessStatus.EXITED, ProcessStatus.KILLED):
                return True, snapshot.exit_code
            if snapshot.status in _TERMINAL_STATUSES:
                return False, None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, None
            wait_seconds = min(_TERMINATION_POLL_INTERVAL_SECONDS, remaining)

            try:
                exit_code = self._handle_wait(record, wait_seconds)
            except Exception:
                exit_code = None
            if exit_code is not None:
                return True, exit_code

            confirmed, exit_code = self._try_confirm_exit(record)
            if confirmed:
                return True, exit_code

            record.completion_event.wait(wait_seconds)

    def _terminal_result_or_raise(
        self,
        record: ProcessRecord,
    ) -> ProcessSnapshot | None:
        """返回已确认终态；lost 不得被当作终止成功。"""

        snapshot = self._snapshot(record)
        if snapshot.status in (
            ProcessStatus.EXITED,
            ProcessStatus.KILLED,
            ProcessStatus.FAILED_START,
        ):
            return snapshot
        if snapshot.status is ProcessStatus.LOST:
            raise ProcessTerminationError(
                "Could not confirm process termination"
            )
        return None

    def _set_termination_in_progress(
        self,
        record: ProcessRecord,
        in_progress: bool,
    ) -> None:
        """标记终止流程占用，避免读取器在流程中抢先关闭句柄。"""

        with record.record_lock:
            record.termination_in_progress = in_progress

    def _is_termination_in_progress(self, record: ProcessRecord) -> bool:
        """读取当前是否由终止流程负责状态确认。"""

        with record.record_lock:
            return record.termination_in_progress

    def _is_failed_start_cleanup_pending(self, record: ProcessRecord) -> bool:
        """确认注册失败后的未收敛记录仍需保留给后续显式清理。"""

        with record.record_lock:
            return (
                record.status is ProcessStatus.RUNNING
                and record.termination_source == "failed_start_cleanup"
            )

    def _is_lost_cleanup_pending(self, record: ProcessRecord) -> bool:
        """确认 lost 记录仍持有必须由显式清理释放的 Handle。"""

        with record.record_lock:
            return (
                record.status is ProcessStatus.LOST
                and record.resource_cleanup_pending
            )

    def _record_log_read_failure(self, record: ProcessRecord) -> None:
        """累计日志读取错误，但不据此判定后台进程丢失。"""

        with record.record_lock:
            record.log_read_error_count += 1

    def _clear_log_read_failures(self, record: ProcessRecord) -> None:
        """在日志读取恢复后清空连续错误计数。"""

        with record.record_lock:
            record.log_read_error_count = 0

    def _record_poll_failure(self, record: ProcessRecord) -> int:
        """累计状态查询错误，并返回当前连续次数。"""

        with record.record_lock:
            record.poll_error_count += 1
            return record.poll_error_count

    def _clear_poll_failures(self, record: ProcessRecord) -> None:
        """状态查询成功后清空连续错误计数。"""

        with record.record_lock:
            record.poll_error_count = 0

    def _record_cleanup_failure(self, record: ProcessRecord) -> None:
        """保留有限的清理失败快照，供后续会话资源层诊断。"""

        snapshot = self._snapshot(record)
        with self._cleanup_failure_lock:
            self._cleanup_failures.append(snapshot)
            if len(self._cleanup_failures) > 64:
                del self._cleanup_failures[:-64]

    @staticmethod
    def _wait_cancelled(cancel_checker: Callable[[], bool] | None) -> bool:
        """检查等待取消；调用方检查器异常时按未取消处理。"""

        if cancel_checker is None:
            return False
        try:
            return bool(cancel_checker())
        except Exception:
            return False

    @staticmethod
    def _validate_session_key(session_key: str) -> str:
        """校验会话标识不为空。"""

        if not isinstance(session_key, str) or not session_key.strip():
            raise ProcessError("session_key must be a non-empty string")
        return session_key

    @staticmethod
    def _validate_metadata(name: str, value: str) -> None:
        """校验创建后会保留的不可变描述字段。"""

        if not isinstance(value, str):
            raise ProcessError(f"{name} must be a string")

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> None:
        """校验正整数配置。"""

        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ProcessError(f"{name} must be a positive integer")

    @staticmethod
    def _validate_nonnegative_seconds(name: str, value: float) -> None:
        """校验非负且有限的秒数配置。"""

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ProcessError(f"{name} must be a non-negative finite number")

    @classmethod
    def _validate_timeout(cls, timeout: float | None) -> float | None:
        """校验等待超时。"""

        if timeout is None:
            return None
        cls._validate_nonnegative_seconds("timeout", timeout)
        return float(timeout)

    @classmethod
    def _validate_grace_seconds(cls, grace_seconds: float) -> float:
        """校验终止宽限期。"""

        cls._validate_nonnegative_seconds("grace_seconds", grace_seconds)
        return float(grace_seconds)


process_manager = ProcessManager()


__all__ = [
    "BackgroundProcessHandle",
    "BackgroundProcessOutput",
    "ProcessError",
    "ProcessLimitError",
    "ProcessLogResult",
    "ProcessManager",
    "ProcessNotFoundError",
    "ProcessSnapshot",
    "ProcessStartError",
    "ProcessStatus",
    "ProcessTerminationError",
    "ProcessWaitCancelled",
    "process_manager",
]
