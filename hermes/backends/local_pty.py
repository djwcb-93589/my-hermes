"""LocalBackend 的后台 PTY 传输适配层。"""

from __future__ import annotations

import codecs
from collections import deque
from collections.abc import Callable
import ctypes
from dataclasses import dataclass, field
import errno
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid

from hermes.backends import (
    BackgroundProcessCancelledError,
    BackgroundPtyDependencyUnavailableError,
    BackgroundPtyStartError,
)
from hermes.backends import local as _local_backend
from hermes.backends.local import (
    BackgroundProcessCleanupError,
    _close_windows_job_strict,
    _create_windows_job,
    _posix_process_group_exists,
    _query_windows_job_active_processes,
    _remove_local_path_strict,
    _terminate_windows_job,
    _terminate_windows_pid,
)
from hermes.processes import (
    BackgroundProcessHandle,
    BackgroundProcessOutput,
    BackgroundProcessStartCleanupError,
    MAX_PROCESS_STDIN_BYTES,
    ProcessInputBusyError,
    ProcessInputCloseUnsupportedError,
    ProcessInputClosedError,
    ProcessInputDeliveryError,
    ProcessInputError,
    ProcessInputUnavailableError,
)


_PTY_READ_CHARS = 8192
_MAX_PENDING_OUTPUT_CHARS = 256_000
_PROCESS_TREE_POLL_SECONDS = 0.05
_FINAL_OUTPUT_WAIT_SECONDS = 0.5
_INPUT_OPERATION_WAIT_SECONDS = 1.0
_CLOSE_TOTAL_WAIT_SECONDS = 1.5
_CLOSE_COMPONENT_WAIT_SECONDS = 0.5
_START_GATE_WAIT_SECONDS = 5.0
_START_GATE_POLL_SECONDS = 0.05
_START_GATE_REMOVE_WAIT_SECONDS = 1.0
_FAILED_START_WAIT_SECONDS = 5.0
_FAILED_START_RETRY_WAIT_SECONDS = 5.0
_PTY_WRITE_RESULT_BYTE_COUNT = "byte_count"
_PTY_WRITE_RESULT_CONPTY_ZERO_SENTINEL = "conpty_zero_sentinel"
_PTY_WRITE_RESULT_CONTRACTS = frozenset({
    _PTY_WRITE_RESULT_BYTE_COUNT,
    _PTY_WRITE_RESULT_CONPTY_ZERO_SENTINEL,
})


class _ConPtyPlatformBackend(str):
    """标记由本模块明确选择的 pywinpty ConPTY backend。"""


_WINDOWS_BOOTSTRAP = """
import pathlib
import subprocess
import sys
import time

gate = pathlib.Path(sys.argv[1])
deadline = time.monotonic() + 30.0
while True:
    try:
        state = gate.read_bytes()
    except OSError:
        state = b""
    if state == b"ready\\n":
        break
    if time.monotonic() >= deadline:
        raise SystemExit(125)
    time.sleep(0.05)

try:
    child = subprocess.Popen(sys.argv[2:])
except BaseException:
    raise SystemExit(126)

try:
    gate.write_bytes(b"accepted\\n")
except BaseException:
    raise SystemExit(127)

try:
    result = child.wait()
except BaseException:
    result = 126
raise SystemExit(result)
"""


@dataclass(slots=True)
class _PtyInputOperation:
    """唯一 PTY 输入 worker 当前执行的一次操作。"""

    kind: str
    payload: str | None = field(default=None, repr=False)
    completed: threading.Event = field(default_factory=threading.Event)
    bytes_written: int = 0
    timed_out: bool = False
    delivery_unknown: bool = False
    error: BaseException | None = field(default=None, repr=False)


@dataclass(slots=True)
class _PtyCloseState:
    """跟踪一次可能阻塞的底层 PTY close。"""

    transport: object
    completed: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    started: bool = False
    error: BaseException | None = field(default=None, repr=False)


class _PtyStartFailureContext(RuntimeError):
    """只在异常链中保存类型，不把底层文本暴露到工具响应。"""

    def __init__(
        self,
        *,
        start_error: BaseException,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self._start_error = start_error
        self._cleanup_error = cleanup_error
        message = f"PTY start error={type(start_error).__name__}"
        if cleanup_error is not None:
            message += f", cleanup_error={type(cleanup_error).__name__}"
        super().__init__(message)


def _open_windows_process_handle_for_job(pid: int) -> object:
    """打开可分配到 Job 的临时进程句柄，不把句柄值写入异常。"""

    if sys.platform != "win32":
        raise RuntimeError("Windows process handles are unavailable")
    process_handle = _local_backend._kernel32.OpenProcess(
        _local_backend._PROCESS_SET_QUOTA
        | _local_backend._PROCESS_TERMINATE,
        False,
        pid,
    )
    if not process_handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    return process_handle


def _assign_windows_process_handle_to_job(
    process_handle: object,
    job_handle: object | None,
) -> None:
    """使用已归 Handle 所有的临时进程句柄执行 Job 分配。"""

    if sys.platform != "win32" or not job_handle:
        raise RuntimeError("Windows Job handle is unavailable")
    if not _local_backend._kernel32.AssignProcessToJobObject(
        job_handle,
        process_handle,
    ):
        raise OSError(
            ctypes.get_last_error(),
            "AssignProcessToJobObject failed",
        )


def _close_windows_process_handle_strict(
    process_handle: object,
) -> None:
    """严格关闭临时进程句柄；失败时由 PTY Handle 保留所有权。"""

    if sys.platform != "win32":
        return
    if not _local_backend._kernel32.CloseHandle(process_handle):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def load_local_pty_binding() -> tuple[object, object | None]:
    """加载 PTY 类及显式 ConPTY 选择，缺失时绝不回退 pipe。"""

    try:
        if sys.platform == "win32":
            from winpty import Backend, PtyProcess

            # pywinpty 3 将 falsy 0 当成“未指定”；字符串 "0" 可稳定强制 ConPTY。
            platform_backend = _ConPtyPlatformBackend(
                str(int(Backend.ConPTY))
            )
        else:
            from ptyprocess import PtyProcess

            platform_backend = None
    except Exception as error:
        raise BackgroundPtyDependencyUnavailableError(
            "PTY dependency is unavailable"
        ) from error
    if not callable(getattr(PtyProcess, "spawn", None)):
        raise BackgroundPtyDependencyUnavailableError(
            "PTY dependency is unavailable"
        )
    return PtyProcess, platform_backend


class LocalPtyBackgroundProcessHandle(BackgroundProcessHandle):
    """封装 ConPTY 或 POSIX PTY，并实现通用后台 Handle 协议。"""

    def __init__(
        self,
        *,
        started_cwd: str,
        snapshot_path: Path,
        write_result_contract: str = _PTY_WRITE_RESULT_BYTE_COUNT,
    ) -> None:
        if (
            not isinstance(write_result_contract, str)
            or write_result_contract not in _PTY_WRITE_RESULT_CONTRACTS
        ):
            raise ValueError("PTY write result contract is invalid")
        # 构造函数只发布最小所有者；Job、PTY 和线程均在后续阶段建立。
        self._started_cwd = started_cwd
        self._write_result_contract = write_result_contract
        self._snapshot_path: Path | None = snapshot_path
        self._startup_gate_path: Path | None = None
        self._transport: object | None = None
        self._pid: int | None = None
        self._process_group_id: int | None = None
        self._job_handle: object | None = None
        self._job_assigned = False
        self._job_assignment_pending = False
        self._windows_process_handle_lock = threading.Lock()
        self._pending_windows_process_handles: list[object] = []
        self._last_exit_code: int | None = None
        self._tree_exit_confirmed = False

        self._operation_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._pending_output: deque[str] = deque()
        self._pending_chars = 0
        self._discarded_chars = 0
        self._output_error: Exception | None = None
        self._output_eof_event = threading.Event()
        self._output_thread: threading.Thread | None = None

        self._input_condition = threading.Condition()
        self._input_operation: _PtyInputOperation | None = None
        self._input_worker: threading.Thread | None = None
        self._input_worker_stop = False

        self._transport_close_state: _PtyCloseState | None = None
        self._closed = False

    @property
    def started_cwd(self) -> str:
        """返回 LocalBackend 在启动时冻结的 cwd。"""

        return self._started_cwd

    @property
    def terminal_mode(self) -> str:
        """PTY Handle 对外只暴露稳定传输模式。"""

        return "pty"

    @property
    def pid(self) -> int | None:
        """返回 ConPTY bootstrap 或 POSIX session leader 的 PID。"""

        with self._operation_lock:
            return self._pid

    def initialize_windows_job(self) -> None:
        """在 PTY 创建前建立 kill-on-close Job，并立即归 Handle 所有。"""

        if sys.platform != "win32":
            return
        job_handle = _create_windows_job(kill_on_close=True)
        with self._operation_lock:
            if self._closed or self._job_handle is not None:
                try:
                    _close_windows_job_strict(job_handle)
                finally:
                    if self._closed:
                        raise RuntimeError("PTY process handle is closed")
                    raise RuntimeError("Windows Job is already initialized")
            self._job_handle = job_handle

    def create_windows_start_gate(self) -> Path:
        """创建严格 LF 的 pending gate，并在返回前交给 Handle。"""

        if sys.platform != "win32":
            raise RuntimeError("PTY startup gate is Windows-only")
        with self._operation_lock:
            snapshot_path = self._snapshot_path
        if snapshot_path is None:
            raise RuntimeError("PTY snapshot is unavailable")

        while True:
            gate_path = snapshot_path.with_name(
                f"hermes-pty-gate-{uuid.uuid4().hex}.ready"
            )
            try:
                with gate_path.open("xb") as gate_file:
                    gate_file.write(b"pending\n")
            except FileExistsError:
                continue
            break

        with self._operation_lock:
            if self._closed or self._startup_gate_path is not None:
                try:
                    _remove_local_path_strict(gate_path)
                finally:
                    raise RuntimeError("PTY startup gate could not be owned")
            self._startup_gate_path = gate_path
        return gate_path

    def clear_windows_start_gate(self, gate_path: Path) -> None:
        """仅在严格删除已接受 gate 后清除 Handle 引用。"""

        _remove_path_with_retry(gate_path)
        with self._operation_lock:
            if self._startup_gate_path == gate_path:
                self._startup_gate_path = None

    def spawn_owned_transport(self, factory: Callable[[], object]) -> object:
        """在 PTY 创建返回的同一调用栈中立即发布资源所有权。"""

        transport: object | None = None
        try:
            transport = factory()
            self.attach_transport(transport)
            return transport
        except BaseException:
            if transport is not None:
                try:
                    self.attach_transport(transport)
                except BaseException:
                    # attach 的第一步就是保存对象；重复失败仍不会丢失 PTY 引用。
                    pass
            raise

    def attach_transport(self, transport: object) -> None:
        """先保存 PTY 对象，再解析 PID/PGID，封闭启动所有权窗口。"""

        with self._operation_lock:
            if self._closed:
                raise RuntimeError("PTY process handle is closed")
            if self._transport is not None and self._transport is not transport:
                raise RuntimeError("PTY process handle is initialized")
            self._transport = transport

        pid = getattr(transport, "pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise TypeError("PTY process pid must be a positive integer")
        process_group_id: int | None = None
        if sys.platform != "win32":
            try:
                process_group_id = os.getpgid(pid)
            except ProcessLookupError:
                # pty.fork() 建立的 session leader 初始 PGID 必然等于 PID。
                process_group_id = pid
            if process_group_id <= 0:
                raise RuntimeError("PTY process group is unavailable")

        with self._operation_lock:
            if self._transport is not transport:
                raise RuntimeError("PTY process ownership changed")
            self._pid = pid
            self._process_group_id = process_group_id

    def owns_process(self) -> bool:
        """返回 Handle 是否已经取得一个实际 PTY 对象。"""

        with self._operation_lock:
            return self._transport is not None

    def owns_managed_resources(self) -> bool:
        """返回启动失败后是否仍有资源必须通过公共 Handle 重试释放。"""

        with self._operation_lock:
            owns_resources = any((
                self._transport is not None,
                self._transport_close_state is not None,
                self._job_handle is not None,
                self._snapshot_path is not None,
                self._startup_gate_path is not None,
            ))
        if owns_resources:
            return True
        with self._windows_process_handle_lock:
            return bool(self._pending_windows_process_handles)

    def _open_owned_windows_process_handle(self, pid: int) -> object:
        """OpenProcess 成功后立即把临时句柄登记为 Handle 私有资源。"""

        with self._windows_process_handle_lock:
            process_handle = _open_windows_process_handle_for_job(pid)
            self._pending_windows_process_handles.append(process_handle)
            return process_handle

    def _close_owned_windows_process_handle(
        self,
        process_handle: object,
    ) -> None:
        """成功 CloseHandle 后才移除 pending 所有权，失败可继续重试。"""

        with self._windows_process_handle_lock:
            pending_index = next(
                (
                    index
                    for index, pending_handle in enumerate(
                        self._pending_windows_process_handles
                    )
                    if pending_handle is process_handle
                ),
                None,
            )
            if pending_index is None:
                return
            _close_windows_process_handle_strict(process_handle)
            del self._pending_windows_process_handles[pending_index]

    def assign_windows_job(self) -> None:
        """通过 ConPTY PID 临时打开句柄，并在放行 bootstrap 前加入 Job。"""

        if sys.platform != "win32":
            return
        with self._operation_lock:
            if self._job_assigned:
                return
            if self._job_assignment_pending:
                raise RuntimeError("Windows Job assignment is unresolved")
            pid = self._require_pid_locked()
            job_handle = self._job_handle
            self._job_assignment_pending = True

        process_handle = self._open_owned_windows_process_handle(pid)
        try:
            _assign_windows_process_handle_to_job(
                process_handle,
                job_handle,
            )
        except BaseException as assignment_error:
            try:
                self._close_owned_windows_process_handle(process_handle)
            except BaseException as close_error:
                combined_error = RuntimeError(
                    "Windows Job assignment and process handle release failed"
                )
                combined_error._assignment_error = assignment_error
                combined_error._close_error = close_error
                raise combined_error from assignment_error
            raise
        else:
            with self._operation_lock:
                self._job_assigned = True
                self._job_assignment_pending = False
            self._close_owned_windows_process_handle(process_handle)

    def start_output_reader(self) -> None:
        """在树级终止能力就绪后启动唯一 PTY reader。"""

        with self._operation_lock:
            if self._closed:
                raise RuntimeError("PTY process handle is closed")
            if self._transport is None:
                raise RuntimeError("PTY process handle is not initialized")
            if self._output_thread is not None:
                return
            output_thread = threading.Thread(
                target=self._collect_output,
                name=f"hermes-local-pty-output-{self._pid}",
                daemon=True,
            )
            self._output_eof_event.clear()
            self._output_thread = output_thread
        try:
            output_thread.start()
        except BaseException:
            try:
                thread_alive = output_thread.is_alive()
            except BaseException:
                # 状态不明时保留线程引用，避免 close 遗漏可能已启动的 reader。
                thread_alive = True
            with self._operation_lock:
                if self._output_thread is output_thread and not thread_alive:
                    self._output_thread = None
            with self._output_lock:
                if self._output_error is None:
                    self._output_error = RuntimeError(
                        "PTY output reader could not start"
                    )
            self._output_eof_event.set()
            raise

    def poll(self) -> int | None:
        """只有 Windows Job 或 POSIX PGID 整体结束后才返回退出码。"""

        with self._operation_lock:
            return self._poll_locked()

    def wait(self, timeout: float | None = None) -> int | None:
        """用短周期有限等待整个 Job/PGID，而不是只等待根进程。"""

        deadline = (
            None if timeout is None else time.monotonic() + max(timeout, 0.0)
        )
        while True:
            exit_code = self.poll()
            if exit_code is not None:
                return exit_code
            if deadline is None:
                wait_seconds = _PROCESS_TREE_POLL_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                wait_seconds = min(_PROCESS_TREE_POLL_SECONDS, remaining)
            time.sleep(wait_seconds)

    def process_tree_is_terminated(self) -> bool:
        """严格查询当前 Job/PGID 是否已经为空。"""

        return self.poll() is not None

    def startup_process_exit_code(self) -> int | None:
        """只查询 Windows bootstrap 根进程，用于 accepted 前快速失败。"""

        if sys.platform != "win32":
            raise RuntimeError("PTY startup process is Windows-only")
        with self._operation_lock:
            transport = self._transport
            if transport is None:
                raise RuntimeError("PTY process handle is not initialized")
            return self._transport_exit_code(transport)

    def read_available(self) -> BackgroundProcessOutput:
        """原子交付 PTY 原始追加流、丢弃计数和 reader 错误。"""

        if not self._has_pending_output_state() and self.poll() is not None:
            self._output_eof_event.wait(_FINAL_OUTPUT_WAIT_SECONDS)
        return self._take_output_batch()

    def read_final_output(self) -> BackgroundProcessOutput:
        """仅在成功 close 后消费不会再变化的最终输出。"""

        with self._operation_lock:
            if not self._closed:
                raise RuntimeError("PTY process handle is not closed")
            if self._output_thread is not None:
                raise RuntimeError("PTY output reader is still running")
            if self._transport_close_state is not None:
                raise RuntimeError("PTY transport close is still running")
        with self._input_condition:
            if self._input_worker is not None or self._input_operation is not None:
                raise RuntimeError("PTY input worker is still running")
        if not self._output_eof_event.is_set():
            raise RuntimeError("PTY output EOF is unavailable")
        return self._take_output_batch()

    def write_stdin(self, data: str) -> int:
        """把文本原样提交到 PTY，不保存输入副本。"""

        if not isinstance(data, str) or not data:
            raise ProcessInputError("Process input must be non-empty text")
        return self._submit_input_operation("write", data)

    def submit_stdin(self, data: str) -> int:
        """按平台追加 Enter：ConPTY 使用 CR，POSIX PTY 使用 LF。"""

        if not isinstance(data, str):
            raise ProcessInputError("Process input must be text")
        enter = "\r" if sys.platform == "win32" else "\n"
        return self._submit_input_operation("submit", data + enter)

    def close_stdin(self) -> bool:
        """不模拟 PTY EOF，也不改变后续 write/submit 资格。"""

        raise ProcessInputCloseUnsupportedError(
            "Closing stdin is not supported for PTY processes"
        )

    def interrupt(self) -> bool:
        """POSIX 向 PGID 发 SIGINT；Windows 经 ConPTY 写入 Ctrl+C。"""

        with self._operation_lock:
            if self._transport is None and self._pid is None:
                # 启动失败可能只留下 Job 或临时句柄，此时从未有可中断的进程。
                return False

        if sys.platform != "win32":
            with self._operation_lock:
                process_group_id = self._process_group_id
            if process_group_id is None:
                raise RuntimeError("PTY process group is unavailable")
            if not _posix_process_group_exists(process_group_id):
                return False
            try:
                os.killpg(process_group_id, signal.SIGINT)
                return True
            except ProcessLookupError:
                if not _posix_process_group_exists(process_group_id):
                    return False
                raise

        if not self._windows_target_is_active():
            return False
        try:
            self._submit_input_operation("interrupt", "\x03")
        except Exception as error:
            if not self._windows_target_is_active():
                return False
            raise RuntimeError("PTY interrupt could not be delivered") from error
        return True

    def kill(self) -> bool:
        """强制终止整个 Windows Job 或保存的 POSIX 进程组。"""

        with self._operation_lock:
            if self._transport is None and self._pid is None:
                # 返回 False 后 ProcessManager 仍会通过 poll 确认，并重试释放剩余资源。
                return False

        if sys.platform != "win32":
            with self._operation_lock:
                process_group_id = self._process_group_id
            if process_group_id is None:
                raise RuntimeError("PTY process group is unavailable")
            if not _posix_process_group_exists(process_group_id):
                return False
            try:
                os.killpg(process_group_id, signal.SIGKILL)
                return True
            except ProcessLookupError:
                if not _posix_process_group_exists(process_group_id):
                    return False
                raise

        with self._operation_lock:
            pid = self._require_pid_locked()
            job_handle = self._job_handle
            job_assigned = self._job_assigned
            job_assignment_pending = self._job_assignment_pending
            job_may_own_process = job_assigned or job_assignment_pending

        failures: list[BaseException] = []
        job_active: int | None = 0
        if job_may_own_process:
            try:
                job_active = _query_windows_job_active_processes(job_handle)
            except BaseException as error:
                job_active = None
                failures.append(error)

        if job_active is None or job_active > 0:
            try:
                return _terminate_windows_job(job_handle)
            except BaseException as error:
                failures.append(error)
        elif job_assigned:
            # 已确认分配的 Job 是树级权威；空 Job 不再按可能复用的 PID 操作。
            return False

        try:
            root_active = self._transport_is_alive()
        except BaseException as error:
            failures.append(error)
            root_active = None
        if job_active == 0 and root_active is False:
            return False

        if root_active is not False:
            try:
                return _terminate_windows_pid(pid)
            except BaseException as error:
                failures.append(error)

        if not self._windows_target_is_active():
            return False
        if failures:
            raise RuntimeError(
                "PTY process tree termination failed"
            ) from failures[-1]
        raise RuntimeError("PTY process tree termination failed")

    def close(self) -> None:
        """有界释放 PTY、reader、input worker、Job 与临时文件。"""

        deadline = time.monotonic() + _CLOSE_TOTAL_WAIT_SECONDS
        if not self._close_lock.acquire(timeout=self._remaining(deadline)):
            raise RuntimeError("PTY process handle close failed")
        try:
            self._close_serialized(deadline)
        finally:
            self._close_lock.release()

    def _close_serialized(self, deadline: float) -> None:
        """失败时保留未释放资源，使后续 cleanup 能继续重试。"""

        with self._operation_lock:
            if self._closed:
                return
            transport_exists = self._transport is not None
            tree_confirmed = self._tree_exit_confirmed
        if transport_exists and not tree_confirmed:
            exit_code = self.poll()
            if exit_code is None:
                raise RuntimeError("PTY process tree is still running")

        errors: list[BaseException] = []
        self._stop_input_worker(deadline, errors)
        if errors:
            self._raise_close_error(errors)

        with self._operation_lock:
            output_thread = self._output_thread
        if output_thread is None:
            self._output_eof_event.set()
        elif output_thread is threading.current_thread():
            errors.append(RuntimeError("PTY output reader cannot close itself"))
        else:
            self._output_eof_event.wait(
                min(_CLOSE_COMPONENT_WAIT_SECONDS, self._remaining(deadline))
            )

        self._close_transport(deadline, errors)

        if output_thread is not None and output_thread is not threading.current_thread():
            output_thread.join(
                timeout=min(
                    _CLOSE_COMPONENT_WAIT_SECONDS,
                    self._remaining(deadline),
                )
            )
            if output_thread.is_alive():
                errors.append(RuntimeError("PTY output reader did not stop"))
            else:
                with self._operation_lock:
                    if self._output_thread is output_thread:
                        self._output_thread = None

        self._release_pending_windows_process_handles(errors)

        if errors:
            self._raise_close_error(errors)

        with self._operation_lock:
            job_handle = self._job_handle
        if job_handle is not None:
            try:
                _close_windows_job_strict(job_handle)
            except BaseException as error:
                errors.append(error)
            else:
                with self._operation_lock:
                    if self._job_handle is job_handle:
                        self._job_handle = None
                        self._job_assigned = False
                        self._job_assignment_pending = False

        self._release_owned_paths(errors)

        with self._operation_lock:
            resources_released = (
                self._transport is None
                and self._transport_close_state is None
                and self._output_thread is None
                and self._job_handle is None
                and self._snapshot_path is None
                and self._startup_gate_path is None
            )
        with self._input_condition:
            resources_released = resources_released and (
                self._input_worker is None and self._input_operation is None
            )
        with self._windows_process_handle_lock:
            resources_released = resources_released and not (
                self._pending_windows_process_handles
            )
        if errors or not resources_released:
            if not errors:
                errors.append(RuntimeError("PTY process resources remain open"))
            self._raise_close_error(errors)
        with self._operation_lock:
            self._closed = True

    def _poll_locked(self) -> int | None:
        """在 operation lock 内查询 transport，并以树级资源作为完成边界。"""

        if self._tree_exit_confirmed:
            return 0 if self._last_exit_code is None else self._last_exit_code
        transport = self._transport
        if transport is None:
            # 仅持有 Job、临时 handle 或路径的失败启动也可交给 Manager 重试 close。
            if self._pid is not None:
                raise RuntimeError("PTY process status is unavailable")
            self._last_exit_code = 0
            self._tree_exit_confirmed = True
            return 0

        root_exit_code = self._transport_exit_code(transport)
        if sys.platform == "win32":
            if self._job_assigned:
                active_processes = _query_windows_job_active_processes(
                    self._job_handle
                )
                if active_processes > 0:
                    return None
            elif self._job_assignment_pending:
                active_processes = _query_windows_job_active_processes(
                    self._job_handle
                )
                if active_processes > 0 or root_exit_code is None:
                    return None
            elif root_exit_code is None:
                return None
        else:
            process_group_id = self._process_group_id
            if process_group_id is None:
                raise RuntimeError("PTY process group is unavailable")
            if _posix_process_group_exists(process_group_id):
                return None

        if root_exit_code is None:
            # 树级资源可能恰在首次根状态查询后归零，再读一次以尽量保留真实退出码。
            root_exit_code = self._transport_exit_code(transport)
        exit_code = 0 if root_exit_code is None else root_exit_code
        self._last_exit_code = exit_code
        self._tree_exit_confirmed = True
        return exit_code

    @staticmethod
    def _transport_exit_code(transport: object) -> int | None:
        """让底层 isalive 更新退出状态，并转换为 ProcessManager 退出码。"""

        isalive = LocalPtyBackgroundProcessHandle._transport_alive_callable(
            transport
        )
        if not callable(isalive):
            raise RuntimeError("PTY process status is unavailable")
        if bool(isalive()):
            return None
        exit_status = getattr(transport, "exitstatus", None)
        if isinstance(exit_status, int) and not isinstance(exit_status, bool):
            return exit_status
        signal_status = getattr(transport, "signalstatus", None)
        if isinstance(signal_status, int) and not isinstance(signal_status, bool):
            return -signal_status
        return 0

    def _transport_is_alive(self) -> bool:
        """保守查询根 PTY 进程；异常由调用方按状态不明处理。"""

        with self._operation_lock:
            transport = self._transport
        if transport is None:
            return False
        isalive = self._transport_alive_callable(transport)
        if not callable(isalive):
            raise RuntimeError("PTY process status is unavailable")
        return bool(isalive())

    @staticmethod
    def _transport_alive_callable(transport: object) -> Callable | None:
        """Windows 优先读底层 PTY，避免高层 isalive 提前禁止资源 close。"""

        if sys.platform == "win32":
            low_level_pty = getattr(transport, "pty", None)
            low_level_isalive = getattr(low_level_pty, "isalive", None)
            if callable(low_level_isalive):
                return low_level_isalive
        isalive = getattr(transport, "isalive", None)
        return isalive if callable(isalive) else None

    def _windows_target_is_active(self) -> bool:
        """Job 查询失败不得被当作树已退出。"""

        with self._operation_lock:
            job_handle = self._job_handle
            job_assigned = self._job_assigned
            job_assignment_pending = self._job_assignment_pending
        if job_assigned:
            return _query_windows_job_active_processes(job_handle) > 0
        if job_assignment_pending:
            return (
                _query_windows_job_active_processes(job_handle) > 0
                or self._transport_is_alive()
            )
        return self._transport_is_alive()

    def _submit_input_operation(self, kind: str, payload: str) -> int:
        """发布至多一个输入操作，并对调用线程执行有界等待。"""

        payload_size = self._validate_input_payload(payload)
        deadline = time.monotonic() + _INPUT_OPERATION_WAIT_SECONDS
        with self._input_condition:
            if self._closed:
                raise ProcessInputClosedError("Process stdin is already closed")
            current = self._input_operation
            if current is not None:
                if current.timed_out:
                    raise ProcessInputDeliveryError(
                        "Process input delivery could not be confirmed",
                        delivery_unknown=True,
                    )
                raise ProcessInputBusyError(
                    "A process input operation is still in progress"
                )
            self._ensure_input_worker_locked()
            operation = _PtyInputOperation(kind=kind, payload=payload)
            self._input_operation = operation
            self._input_condition.notify_all()

        if not operation.completed.wait(max(0.0, deadline - time.monotonic())):
            with self._input_condition:
                operation.timed_out = True
                operation.delivery_unknown = True
            raise ProcessInputDeliveryError(
                "Process input delivery could not be confirmed",
                delivery_unknown=True,
            )
        if operation.error is not None:
            raise ProcessInputDeliveryError(
                (
                    "Process input delivery could not be confirmed"
                    if operation.delivery_unknown
                    else "Process input could not be delivered"
                ),
                delivery_unknown=operation.delivery_unknown,
            ) from operation.error
        if operation.bytes_written != payload_size:
            raise ProcessInputDeliveryError(
                "Process input delivery could not be confirmed",
                delivery_unknown=True,
            )
        return operation.bytes_written

    @staticmethod
    def _validate_input_payload(payload: str) -> int:
        """按实际 UTF-8 字节数限制一次 PTY 输入。"""

        if not isinstance(payload, str):
            raise ProcessInputError("Process input must be text")
        try:
            payload_size = len(payload.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise ProcessInputError(
                "Process input must be valid UTF-8 text"
            ) from error
        if payload_size > MAX_PROCESS_STDIN_BYTES:
            raise ProcessInputError("Process input exceeds the size limit")
        return payload_size

    def _ensure_input_worker_locked(self) -> None:
        """在 condition 内保证最多一个永久 PTY 输入 worker。"""

        worker = self._input_worker
        if worker is not None and worker.is_alive():
            return
        if worker is not None:
            self._input_worker = None
        self._input_worker_stop = False
        worker = threading.Thread(
            target=self._input_worker_loop,
            name="hermes-local-pty-input",
            daemon=True,
        )
        self._input_worker = worker
        try:
            worker.start()
        except BaseException as error:
            try:
                worker_alive = worker.is_alive()
            except BaseException:
                worker_alive = True
            if self._input_worker is worker and not worker_alive:
                self._input_worker = None
            if not isinstance(error, Exception):
                raise
            raise ProcessInputUnavailableError(
                "Process stdin is not available"
            ) from error

    def _input_worker_loop(self) -> None:
        """串行执行 write/submit/interrupt，不建立输入队列或历史。"""

        current_thread = threading.current_thread()
        try:
            while True:
                with self._input_condition:
                    while (
                        self._input_operation is None
                        and not self._input_worker_stop
                    ):
                        self._input_condition.wait()
                    if self._input_worker_stop and self._input_operation is None:
                        return
                    operation = self._input_operation
                if operation is None:
                    continue
                self._execute_input_operation(operation)
                with self._input_condition:
                    if self._input_operation is operation:
                        self._input_operation = None
                    operation.payload = None
                    operation.completed.set()
                    self._input_condition.notify_all()
        finally:
            with self._input_condition:
                operation = self._input_operation
                if operation is not None and not operation.completed.is_set():
                    operation.error = RuntimeError(
                        "PTY input worker stopped unexpectedly"
                    )
                    operation.payload = None
                    operation.delivery_unknown = True
                    self._input_operation = None
                    operation.completed.set()
                if self._input_worker is current_thread:
                    self._input_worker = None
                self._input_condition.notify_all()

    def _execute_input_operation(self, operation: _PtyInputOperation) -> None:
        """隔离 pywinpty str 与 ptyprocess bytes 的写入差异。"""

        payload = operation.payload or ""
        with self._operation_lock:
            transport = self._transport
        if transport is None:
            operation.error = RuntimeError("PTY transport is unavailable")
            return
        write = getattr(transport, "write", None)
        if not callable(write):
            operation.error = RuntimeError("PTY input is unavailable")
            return

        try:
            if sys.platform == "win32":
                # 公开接口声明返回字节数，pywinpty 3.x ConPTY 成功时可能返回 0。
                # 该差异只由显式 transport contract 解释，其他 transport 仍严格计数。
                expected_bytes = len(payload.encode("utf-8"))
                operation.delivery_unknown = True
                written = write(payload)
                if (
                    isinstance(written, bool)
                    or not isinstance(written, int)
                    or written < 0
                    or written > expected_bytes
                ):
                    raise OSError("PTY input write returned an invalid result")
                if written == expected_bytes:
                    operation.bytes_written = expected_bytes
                    operation.delivery_unknown = False
                    return
                if (
                    written == 0
                    and self._write_result_contract
                    == _PTY_WRITE_RESULT_CONPTY_ZERO_SENTINEL
                ):
                    operation.bytes_written = expected_bytes
                    operation.delivery_unknown = False
                    return
                if written == 0:
                    # byte-count contract 的零字节结果表示没有送达。
                    operation.delivery_unknown = False
                    raise OSError("PTY input write made no progress")
                # 字节边界可能落在多字节字符中；部分写入不得续写或重放。
                operation.bytes_written = written
                operation.delivery_unknown = True
                raise OSError("PTY input write was incomplete")

            operation.delivery_unknown = True
            encoded = payload.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                written = write(encoded[offset:])
                if (
                    isinstance(written, bool)
                    or not isinstance(written, int)
                    or written <= 0
                    or written > len(encoded) - offset
                ):
                    raise OSError("PTY input write made no progress")
                offset += written
                operation.bytes_written = offset
            operation.delivery_unknown = False
        except BaseException as error:
            operation.error = error

    def _stop_input_worker(
        self,
        deadline: float,
        errors: list[BaseException],
    ) -> None:
        """停止唯一输入 worker；活动写未收敛时不伪造 close 成功。"""

        with self._input_condition:
            self._input_worker_stop = True
            worker = self._input_worker
            self._input_condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self._remaining(deadline))
            if worker.is_alive():
                errors.append(RuntimeError("PTY input worker did not stop"))
        with self._input_condition:
            if self._input_worker is not None and not self._input_worker.is_alive():
                self._input_worker = None
            if self._input_operation is not None:
                errors.append(RuntimeError("PTY input operation is unresolved"))

    def _collect_output(self) -> None:
        """阻塞读取 PTY，保留 CR/ANSI/echo，并通过公共批次上报异常。"""

        with self._operation_lock:
            transport = self._transport
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output_error: Exception | None = None
        try:
            if transport is None:
                raise RuntimeError("PTY transport is unavailable")
            read = getattr(transport, "read", None)
            if not callable(read):
                raise RuntimeError("PTY output is unavailable")
            while True:
                try:
                    chunk = read(_PTY_READ_CHARS)
                except EOFError:
                    break
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    if self._tree_ended_without_error():
                        break
                    raise
                except Exception:
                    if self._tree_ended_without_error():
                        break
                    raise
                if chunk in (b"", ""):
                    break
                if isinstance(chunk, bytes):
                    self._append_output(decoder.decode(chunk, final=False))
                elif isinstance(chunk, str):
                    self._append_output(chunk)
                else:
                    raise TypeError("PTY output must be text or bytes")
        except Exception as error:
            output_error = error
        finally:
            try:
                self._append_output(decoder.decode(b"", final=True))
            except Exception as error:
                if output_error is None:
                    output_error = error
            if output_error is not None:
                with self._output_lock:
                    if self._output_error is None:
                        self._output_error = output_error
            self._output_eof_event.set()

    def _tree_ended_without_error(self) -> bool:
        """reader 仅在树级 poll 可确认终态时把关闭异常视为 EOF。"""

        try:
            return self.poll() is not None
        except Exception:
            return False

    def _append_output(self, output: str) -> None:
        """只维护有限 pending 流；公共绝对 cursor 仍由 ProcessManager 维护。"""

        if not output:
            return
        with self._output_lock:
            self._pending_output.append(output)
            self._pending_chars += len(output)
            excess = self._pending_chars - _MAX_PENDING_OUTPUT_CHARS
            while excess > 0 and self._pending_output:
                oldest = self._pending_output[0]
                if len(oldest) <= excess:
                    self._pending_output.popleft()
                    self._pending_chars -= len(oldest)
                    self._discarded_chars += len(oldest)
                    excess -= len(oldest)
                else:
                    self._pending_output[0] = oldest[excess:]
                    self._pending_chars -= excess
                    self._discarded_chars += excess
                    excess = 0

    def _has_pending_output_state(self) -> bool:
        """查询是否有文本、截断或 reader 错误待 Manager 消费。"""

        with self._output_lock:
            return bool(
                self._pending_output
                or self._discarded_chars
                or self._output_error is not None
            )

    def _take_output_batch(self) -> BackgroundProcessOutput:
        """原子消费一次 Handle 输出状态。"""

        with self._output_lock:
            result = BackgroundProcessOutput(
                text="".join(self._pending_output),
                discarded_chars=self._discarded_chars,
                read_error=self._output_error,
            )
            self._pending_output.clear()
            self._pending_chars = 0
            self._discarded_chars = 0
            self._output_error = None
            return result

    def _close_transport(
        self,
        deadline: float,
        errors: list[BaseException],
    ) -> None:
        """在 daemon worker 中有界关闭底层 PTY，并保留状态供重试。"""

        with self._operation_lock:
            state = self._transport_close_state
            transport = self._transport
            if state is None and transport is not None:
                state = _PtyCloseState(transport=transport)
                state.thread = threading.Thread(
                    target=self._run_transport_close,
                    args=(state,),
                    name="hermes-local-pty-close",
                    daemon=True,
                )
                self._transport_close_state = state
        if state is None:
            return
        thread = state.thread
        if thread is None:
            errors.append(RuntimeError("PTY close worker is unavailable"))
            return
        if not state.started and not state.completed.is_set():
            try:
                thread.start()
            except BaseException as error:
                try:
                    close_worker_alive = thread.is_alive()
                except BaseException:
                    close_worker_alive = True
                if close_worker_alive:
                    # start 结果不明但线程可能已运行时继续追踪，禁止另起重复 close。
                    state.started = True
                else:
                    state.error = error
                    state.completed.set()
            else:
                state.started = True
        state.completed.wait(
            min(_CLOSE_COMPONENT_WAIT_SECONDS, self._remaining(deadline))
        )
        if not state.completed.is_set():
            errors.append(RuntimeError("PTY transport close timed out"))
            return
        if state.started:
            thread.join(timeout=self._remaining(deadline))
            if thread.is_alive():
                errors.append(RuntimeError("PTY close worker did not stop"))
                return
        with self._operation_lock:
            if state.error is not None:
                errors.append(state.error)
                if self._transport_close_state is state:
                    self._transport_close_state = None
                return
            if self._transport is state.transport:
                self._transport = None
            if self._transport_close_state is state:
                self._transport_close_state = None

    @staticmethod
    def _run_transport_close(state: _PtyCloseState) -> None:
        """关闭平台 PTY；POSIX 明确不在 close 中额外发送强杀。"""

        try:
            close = getattr(state.transport, "close")
            if sys.platform == "win32":
                close()
            else:
                try:
                    close(force=False)
                except TypeError:
                    close()
        except BaseException as error:
            state.error = error
        finally:
            state.completed.set()

    def _release_pending_windows_process_handles(
        self,
        errors: list[BaseException],
    ) -> None:
        """逐个重试临时 Windows handle，成功项才从 pending 集合移除。"""

        with self._windows_process_handle_lock:
            pending_handles = tuple(self._pending_windows_process_handles)
        for process_handle in pending_handles:
            try:
                self._close_owned_windows_process_handle(process_handle)
            except BaseException as error:
                errors.append(error)

    def _release_owned_paths(self, errors: list[BaseException]) -> None:
        """严格删除 snapshot/gate；失败路径保留引用供下一次 close。"""

        for attribute in ("_snapshot_path", "_startup_gate_path"):
            with self._operation_lock:
                path = getattr(self, attribute)
            if path is None:
                continue
            try:
                _remove_local_path_strict(path)
            except BaseException as error:
                errors.append(error)
            else:
                with self._operation_lock:
                    if getattr(self, attribute) == path:
                        setattr(self, attribute, None)

    @staticmethod
    def _raise_close_error(errors: list[BaseException]) -> None:
        """统一生成不包含资源 repr 的可重试 close 异常。"""

        close_error = RuntimeError("PTY process handle close failed")
        close_error._resource_errors = tuple(errors)
        raise close_error

    def _require_pid_locked(self) -> int:
        """在 operation lock 内取得已验证 PID。"""

        if self._pid is None:
            raise RuntimeError("PTY process pid is unavailable")
        return self._pid

    @staticmethod
    def _remaining(deadline: float) -> float:
        """使用 monotonic 绝对截止时间计算剩余等待。"""

        return max(0.0, deadline - time.monotonic())


def spawn_local_pty_background(
    *,
    shell_path: str,
    command: str,
    env: dict[str, str],
    started_cwd: str,
    snapshot_path: Path,
    pty_process_class,
    pty_platform_backend,
    cancel_checker: Callable[[], bool],
) -> BackgroundProcessHandle:
    """创建并初始化平台 PTY；失败清理无法确认时把 Handle 移交 Manager。"""

    write_result_contract = _PTY_WRITE_RESULT_BYTE_COUNT
    transport_backend = pty_platform_backend
    if (
        sys.platform == "win32"
        and isinstance(pty_platform_backend, _ConPtyPlatformBackend)
    ):
        write_result_contract = _PTY_WRITE_RESULT_CONPTY_ZERO_SENTINEL
        transport_backend = str(pty_platform_backend)

    handle = LocalPtyBackgroundProcessHandle(
        started_cwd=started_cwd,
        snapshot_path=snapshot_path,
        write_result_contract=write_result_contract,
    )
    try:
        _raise_if_cancelled(cancel_checker)
        if sys.platform == "win32":
            handle.initialize_windows_job()
            gate_path = handle.create_windows_start_gate()
            bootstrap_command = [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _WINDOWS_BOOTSTRAP,
                str(gate_path),
                shell_path,
                "-c",
                command,
            ]
            _raise_if_cancelled(cancel_checker)
            handle.spawn_owned_transport(
                lambda: pty_process_class.spawn(
                    bootstrap_command,
                    cwd=started_cwd,
                    env=env,
                    backend=transport_backend,
                )
            )
            _raise_if_cancelled(cancel_checker)
            handle.assign_windows_job()
            _raise_if_cancelled(cancel_checker)
            handle.start_output_reader()
            _raise_if_cancelled(cancel_checker)
            gate_path.write_bytes(b"ready\n")
            _await_windows_gate_accepted(
                handle,
                gate_path,
                cancel_checker=cancel_checker,
            )
            handle.clear_windows_start_gate(gate_path)
        else:
            _raise_if_cancelled(cancel_checker)
            handle.spawn_owned_transport(
                lambda: pty_process_class.spawn(
                    [shell_path, "-c", command],
                    cwd=started_cwd,
                    env=env,
                )
            )
            _raise_if_cancelled(cancel_checker)
            handle.start_output_reader()

        _raise_if_cancelled(cancel_checker)
        return handle
    except BaseException as start_error:
        try:
            _dispose_pty_before_return(handle)
        except BackgroundProcessCleanupError as cleanup_error:
            if handle.owns_managed_resources():
                if handle.owns_process():
                    try:
                        # 清理未确认时尽量恢复 reader，后续 Manager 可持续排空输出。
                        handle.start_output_reader()
                    except BaseException as reader_error:
                        cleanup_error._reader_start_error = reader_error
                handoff_error = BackgroundProcessStartCleanupError(
                    "PTY process start failed and cleanup could not be confirmed",
                    handle=handle,
                    start_error=start_error,
                    cleanup_error=cleanup_error,
                )
                raise handoff_error from _PtyStartFailureContext(
                    start_error=start_error,
                    cleanup_error=cleanup_error,
                )
            raise BackgroundPtyStartError(
                "PTY background process could not be started"
            ) from _PtyStartFailureContext(
                start_error=start_error,
                cleanup_error=cleanup_error,
            )

        if isinstance(start_error, BackgroundProcessCancelledError):
            raise
        if isinstance(
            start_error,
            (BackgroundPtyDependencyUnavailableError, BackgroundPtyStartError),
        ):
            raise
        if not isinstance(start_error, Exception):
            raise
        raise BackgroundPtyStartError(
            "PTY background process could not be started"
        ) from _PtyStartFailureContext(start_error=start_error)


def _dispose_pty_before_return(
    handle: LocalPtyBackgroundProcessHandle,
) -> None:
    """两轮终止并确认整个 Job/PGID 后，才允许释放 PTY 资源。"""

    if not handle.owns_process():
        try:
            handle.close()
        except BaseException as error:
            cleanup_error = BackgroundProcessCleanupError(
                "Could not confirm PTY process cleanup"
            )
            cleanup_error._close_error = error
            raise cleanup_error
        return

    last_error: BaseException | None = None
    tree_terminated = False
    for wait_seconds in (
        _FAILED_START_WAIT_SECONDS,
        _FAILED_START_RETRY_WAIT_SECONDS,
    ):
        try:
            tree_terminated = handle.process_tree_is_terminated()
        except BaseException as error:
            last_error = error
            tree_terminated = False
        if not tree_terminated:
            try:
                handle.kill()
            except BaseException as error:
                last_error = error
            try:
                handle.wait(timeout=wait_seconds)
            except BaseException as error:
                last_error = error
            try:
                tree_terminated = handle.process_tree_is_terminated()
            except BaseException as error:
                last_error = error
                tree_terminated = False
        if tree_terminated:
            break

    if not tree_terminated:
        cleanup_error = BackgroundProcessCleanupError(
            "Could not confirm PTY process cleanup"
        )
        cleanup_error._termination_error = last_error
        raise cleanup_error
    try:
        handle.close()
    except BaseException as error:
        cleanup_error = BackgroundProcessCleanupError(
            "Could not confirm PTY process cleanup"
        )
        cleanup_error._close_error = error
        raise cleanup_error


def _raise_if_cancelled(cancel_checker: Callable[[], bool]) -> None:
    """取消检查异常按未取消处理。"""

    try:
        cancelled = bool(cancel_checker())
    except Exception:
        cancelled = False
    if cancelled:
        raise BackgroundProcessCancelledError(
            "Background process start cancelled"
        )


def _await_windows_gate_accepted(
    handle: LocalPtyBackgroundProcessHandle,
    gate_path: Path,
    *,
    cancel_checker: Callable[[], bool],
) -> None:
    """等待 bootstrap 确认已离开 gate；等待期间 bootstrap 不读取 PTY 输入。"""

    deadline = time.monotonic() + _START_GATE_WAIT_SECONDS
    while True:
        _raise_if_cancelled(cancel_checker)
        try:
            state = gate_path.read_bytes()
        except OSError:
            state = b""
        if state == b"accepted\n":
            return
        if handle.startup_process_exit_code() is not None:
            raise BackgroundPtyStartError(
                "PTY background process could not be started"
            )
        if handle.poll() is not None:
            raise BackgroundPtyStartError(
                "PTY background process could not be started"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackgroundPtyStartError(
                "PTY background process could not be started"
            )
        time.sleep(min(_START_GATE_POLL_SECONDS, remaining))


def _remove_path_with_retry(path: Path) -> None:
    """有限重试删除 bootstrap 已确认不再读取的 gate。"""

    deadline = time.monotonic() + _START_GATE_REMOVE_WAIT_SECONDS
    while True:
        try:
            _remove_local_path_strict(path)
            return
        except OSError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_START_GATE_POLL_SECONDS, remaining))


__all__ = [
    "LocalPtyBackgroundProcessHandle",
    "load_local_pty_binding",
    "spawn_local_pty_background",
]
