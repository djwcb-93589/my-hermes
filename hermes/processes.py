"""线程安全的后台进程核心领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import secrets
import threading
import time
from typing import Callable, Final, Protocol


class BackgroundProcessHandle(Protocol):
    """ProcessManager 管理后台进程所需的最小句柄能力。"""

    @property
    def pid(self) -> int | None:
        """返回宿主进程标识；后端没有可用标识时返回 None。"""

    def poll(self) -> int | None:
        """仍在运行时返回 None，结束后返回退出码。"""

    def read_available(self) -> str:
        """返回当前可读取的新输出；没有新输出时返回空字符串。"""

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

            with record.record_lock:
                record.handle = handle

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
        except Exception as exc:
            self._mark_start_failed(record)
            self._dispose_failed_start(record)
            raise ProcessStartError("Process start failed") from exc

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

    def _handle_pid(self, record: ProcessRecord) -> int | None:
        """在 Handle 操作锁下读取启动阶段的进程标识。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            return handle.pid

    def _handle_read_available(self, record: ProcessRecord) -> str:
        """串行读取一次当前可用输出。"""

        handle = self._require_handle(record)
        with record.handle_lock:
            self._assert_handle_open_locked(record, handle)
            output = handle.read_available()
        if not isinstance(output, str):
            raise TypeError("process handle output must be a string")
        return output

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

    def _handle_close(self, record: ProcessRecord) -> None:
        """串行关闭句柄，并保证 close 最多执行一次。"""

        handle = self._get_handle(record)
        if handle is None:
            return
        with record.handle_lock:
            with record.record_lock:
                if record.close_called or record.handle is not handle:
                    return
                record.close_called = True
            try:
                handle.close()
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
                self._complete_lost(record)
                return

            while True:
                if self._snapshot(record).status in _TERMINAL_STATUSES:
                    return

                try:
                    self._append_output(
                        record,
                        self._handle_read_available(record),
                    )
                except Exception:
                    self._record_log_read_failure(record)
                else:
                    self._clear_log_read_failures(record)

                try:
                    exit_code = self._handle_poll(record)
                except Exception:
                    poll_error_count = self._record_poll_failure(record)
                    if (
                        poll_error_count >= _CONSECUTIVE_POLL_ERROR_LIMIT
                        and not self._is_termination_in_progress(record)
                    ):
                        self._complete_lost(record)
                        return
                else:
                    self._clear_poll_failures(record)
                    if exit_code is not None:
                        self._finish_confirmed_exit(record, exit_code)
                        return

                time.sleep(_READER_POLL_INTERVAL_SECONDS)
        finally:
            snapshot = self._snapshot(record)
            if (
                snapshot.status not in _TERMINAL_STATUSES
                and not self._is_termination_in_progress(record)
            ):
                self._complete_lost(record)
            if self._snapshot(record).status in _TERMINAL_STATUSES:
                self._handle_close(record)

    def _append_output(self, record: ProcessRecord, output: str) -> None:
        """追加输出，并在需要时从缓冲区头部滚动裁剪。"""

        if not output:
            return
        with record.record_lock:
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
                output = self._handle_read_available(record)
            except Exception:
                return
            if not output:
                return
            self._append_output(record, output)

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

    def _complete_lost(self, record: ProcessRecord) -> None:
        """在持续状态查询失败时标记 lost，而不伪造 killed。"""

        with record.record_lock:
            termination_source = record.termination_source
        self._finalize_record(
            record,
            ProcessStatus.LOST,
            exit_code=None,
            completion_reason="backend_lost",
            termination_source=termination_source,
        )

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
        self._handle_close(record)
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

    def _mark_start_failed(self, record: ProcessRecord) -> None:
        """将启动过程中的异常转换为 failed_start 终态。"""

        with record.record_lock:
            self._transition_locked(
                record,
                ProcessStatus.FAILED_START,
                exit_code=None,
                completion_reason="failed_start",
                termination_source=None,
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
            if record.status not in _TERMINAL_STATUSES:
                return
        with self._registry_lock:
            if self._running.get(record.process_id) is record:
                del self._running[record.process_id]
                self._finished[record.process_id] = record

    def _dispose_failed_start(self, record: ProcessRecord) -> None:
        """尽力终止并释放启动失败时已取得的句柄。"""

        with record.termination_lock:
            try:
                self._handle_kill(record)
            except Exception:
                pass
        self._handle_close(record)

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
                snapshot = self._snapshot(record)
                if snapshot.status in _TERMINAL_STATUSES:
                    return snapshot

                if snapshot.status is ProcessStatus.STARTING:
                    self._request_termination(record, source)
                    # 启动还在外部 starter 中，不能在持锁状态下无限等待。
                    record.startup_event.wait(_STARTUP_CLEANUP_WAIT_SECONDS)
                    snapshot = self._snapshot(record)
                    if snapshot.status in _TERMINAL_STATUSES:
                        return snapshot
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
            if record.status not in _TERMINAL_STATUSES:
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
