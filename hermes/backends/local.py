"""
LocalBackend：通过 subprocess 在本机执行命令。

Windows 上明确优先使用 Git Bash，绝不回退到 WSL 的
``C:\\Windows\\System32\\bash.exe``。Snapshot / cwd 临时文件落在
``<HERMES_HOME>/cache/terminal/`` 下，并以两种形式跟踪（shell 形式
给 Git Bash 用，host 形式给 Python 用）。
"""

from __future__ import annotations

import codecs
from collections import deque
from dataclasses import dataclass, field
import os
import shlex
import signal
import stat as stat_mod
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path

from hermes.backends import (
    BackgroundProcessCancelledError,
    BaseExecutionEnvironment,
    filter_local_subprocess_environment,
)
from hermes.path_policy import PathAccessPolicy
from hermes.path_utils import (
    git_bash_to_windows_path as _bash_to_win_path,
    windows_to_git_bash_path as _win_to_bash_path,
)
from hermes.processes import (
    BackgroundProcessHandle,
    BackgroundProcessOutput,
    BackgroundProcessStartCleanupError,
    MAX_PROCESS_STDIN_BYTES,
    ProcessInputBusyError,
    ProcessInputCloseError,
    ProcessInputClosedError,
    ProcessInputDeliveryError,
    ProcessInputError,
    ProcessInputUnavailableError,
)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _LargeInteger(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_longlong)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _LargeInteger),
            ("PerJobUserTimeLimit", _LargeInteger),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JobObjectBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", _LargeInteger),
            ("TotalKernelTime", _LargeInteger),
            ("ThisPeriodTotalUserTime", _LargeInteger),
            ("ThisPeriodTotalKernelTime", _LargeInteger),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
    )
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _create_windows_job(*, kill_on_close: bool = False) -> object | None:
    """创建独立 Windows Job，但不在此处假定目标进程已经存在。"""

    if sys.platform != "win32":
        return None
    job_handle = _kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    if kill_on_close:
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags |= (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        configured = _kernel32.SetInformationJobObject(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
        if not configured:
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(job_handle)
            raise OSError(error, "SetInformationJobObject failed")
    return job_handle


def _assign_windows_job(
    proc: subprocess.Popen,
    job_handle: object | None,
) -> None:
    """将已启动的根进程加入既有 Windows Job。"""

    if sys.platform != "win32":
        return
    if not job_handle:
        raise RuntimeError("Windows Job handle is unavailable")
    assigned = _kernel32.AssignProcessToJobObject(
        job_handle,
        wintypes.HANDLE(int(proc._handle)),
    )
    if not assigned:
        error = ctypes.get_last_error()
        raise OSError(error, "AssignProcessToJobObject failed")


def _query_windows_job_active_processes(job_handle: object | None) -> int:
    """查询 Windows Job 当前仍活动的进程数量，失败时明确抛错。"""

    if sys.platform != "win32":
        return 0
    if not job_handle:
        raise RuntimeError("Windows Job handle is unavailable")
    accounting = _JobObjectBasicAccountingInformation()
    returned_length = wintypes.DWORD()
    queried = _kernel32.QueryInformationJobObject(
        job_handle,
        _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(accounting),
        ctypes.sizeof(accounting),
        ctypes.byref(returned_length),
    )
    if not queried:
        raise OSError(
            ctypes.get_last_error(),
            "QueryInformationJobObject failed",
        )
    return int(accounting.ActiveProcesses)


def _close_windows_job(job_handle: object | None) -> None:
    """释放由后台 Handle 持有的 Windows Job 句柄。"""

    if sys.platform == "win32" and job_handle:
        _kernel32.CloseHandle(job_handle)


def _close_windows_job_strict(job_handle: object | None) -> None:
    """严格关闭 Windows Job；失败时保留给调用方重试。"""

    if sys.platform != "win32" or not job_handle:
        return
    if not _kernel32.CloseHandle(job_handle):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")


def _attach_windows_job(proc: subprocess.Popen) -> None:
    """把前台进程放入独立 Job，保留旧路径的尽力而为语义。"""

    if sys.platform != "win32":
        return
    job_handle: object | None = None
    try:
        job_handle = _create_windows_job(kill_on_close=False)
        _assign_windows_job(proc, job_handle)
    except (OSError, RuntimeError):
        _close_windows_job(job_handle)
        return
    proc._hermes_job_handle = job_handle


def _posix_process_group_exists(process_group_id: int) -> bool:
    """通过信号 0 确认 POSIX 受管进程组是否仍然存在。"""

    if (
        isinstance(process_group_id, bool)
        or not isinstance(process_group_id, int)
        or process_group_id <= 0
    ):
        raise ValueError("process group id must be a positive integer")
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _root_process_has_exited(proc: subprocess.Popen) -> bool:
    """保守地查询根进程是否已经回收。"""

    try:
        return proc.poll() is not None
    except BaseException:
        return False


def _background_process_tree_has_exited(
    proc: subprocess.Popen,
    *,
    job_handle: object | None,
    job_assigned: bool,
    process_group_id: int | None,
) -> bool:
    """确认受管后台进程树是否已按平台语义完成收敛。"""

    if not _root_process_has_exited(proc):
        return False
    if sys.platform == "win32":
        if not job_assigned:
            return True
        if not job_handle:
            raise RuntimeError("Windows Job handle is unavailable")
        return _query_windows_job_active_processes(job_handle) == 0
    if process_group_id is None:
        return False
    try:
        return not _posix_process_group_exists(process_group_id)
    except (OSError, ValueError):
        return False


def _interrupt_local_process(
    proc: subprocess.Popen,
    *,
    process_group_id: int | None = None,
    interrupt_group_after_root_exit: bool = True,
) -> bool:
    """发送协作式中断；调用前已结束时明确返回 False。"""

    if sys.platform == "win32":
        if proc.poll() is not None:
            return False
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            return True
        except (OSError, ValueError) as interrupt_error:
            try:
                process_exited = proc.poll() is not None
            except Exception:
                raise interrupt_error
            if process_exited:
                return False
            raise

    process_group_id = (
        proc.pid if process_group_id is None else process_group_id
    )
    if process_group_id is None:
        raise RuntimeError("Local process group is unavailable")
    if not _posix_process_group_exists(process_group_id):
        return False
    if (
        not interrupt_group_after_root_exit
        and proc.poll() is not None
    ):
        return False
    try:
        os.killpg(process_group_id, signal.SIGINT)
        return True
    except ProcessLookupError:
        if not _posix_process_group_exists(process_group_id):
            return False
        raise
    except (OSError, ValueError) as interrupt_error:
        try:
            group_exists = _posix_process_group_exists(process_group_id)
        except Exception:
            raise interrupt_error
        if not group_exists:
            return False
        raise


def _kill_local_process_tree(
    proc: subprocess.Popen,
    job_handle: object | None = None,
    *,
    job_assigned: bool = False,
    process_group_id: int | None = None,
    terminate_job_after_root_exit: bool = True,
    terminate_group_after_root_exit: bool = True,
) -> bool:
    """强制终止本地进程树，并返回是否确实执行过终止操作。"""

    if sys.platform == "win32":
        root_exited = proc.poll() is not None
        if job_assigned and not job_handle:
            raise RuntimeError("Windows Job handle is unavailable")
        if root_exited and not terminate_job_after_root_exit:
            return False
        job_is_assigned = bool(job_handle and job_assigned)
        failures: list[BaseException] = []
        if job_is_assigned and root_exited:
            try:
                active_processes = _query_windows_job_active_processes(
                    job_handle
                )
            except Exception as error:
                # 显式终止不能因 Job 查询失败而跳过 TerminateJobObject。
                failures.append(error)
            else:
                if active_processes == 0:
                    return False
        if root_exited and not job_is_assigned:
            return False

        job_terminated = False
        taskkill_succeeded = False
        root_killed = False

        if job_is_assigned:
            try:
                job_result = _kernel32.TerminateJobObject(job_handle, 130)
            except Exception as error:
                failures.append(error)
            else:
                if job_result:
                    job_terminated = True
                else:
                    failures.append(
                        OSError(
                            ctypes.get_last_error(),
                            "TerminateJobObject failed",
                        )
                    )

        if proc.poll() is None:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except Exception as error:
                failures.append(error)
            else:
                if result.returncode == 0:
                    taskkill_succeeded = True
                elif proc.poll() is None:
                    failures.append(
                        subprocess.SubprocessError(
                            "taskkill failed with a non-zero return code"
                        )
                    )

        if proc.poll() is None:
            try:
                proc.kill()
            except Exception as error:
                if proc.poll() is None:
                    failures.append(error)
            else:
                root_killed = True

        if any((job_terminated, taskkill_succeeded, root_killed)):
            return True
        if job_is_assigned:
            try:
                active_processes = _query_windows_job_active_processes(
                    job_handle
                )
            except Exception as error:
                failures.append(error)
            else:
                if active_processes == 0:
                    return False
            if failures:
                raise failures[-1]
            raise RuntimeError("Could not terminate local Windows process tree")
        if proc.poll() is not None:
            return False
        if failures:
            raise failures[-1]
        raise RuntimeError("Could not terminate local Windows process tree")

    process_group_id = (
        proc.pid if process_group_id is None else process_group_id
    )
    if process_group_id is None:
        raise RuntimeError("Local process group is unavailable")
    if not _posix_process_group_exists(process_group_id):
        return False
    if (
        not terminate_group_after_root_exit
        and proc.poll() is not None
    ):
        return False
    try:
        os.killpg(process_group_id, signal.SIGKILL)
        return True
    except ProcessLookupError:
        if not _posix_process_group_exists(process_group_id):
            return False
        raise
    except (OSError, ValueError) as group_error:
        try:
            group_exists = _posix_process_group_exists(process_group_id)
        except Exception:
            raise group_error
        if not group_exists:
            return False
        try:
            proc.kill()
        except (OSError, ValueError) as process_error:
            try:
                group_exists = _posix_process_group_exists(process_group_id)
            except Exception:
                raise process_error
            if not group_exists:
                return False
            raise process_error from group_error
        return True


def _remove_local_path(path: Path | None) -> None:
    """幂等删除后台进程私有的临时文件。"""

    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _remove_local_path_strict(path: Path | None) -> None:
    """严格删除后台私有临时文件，仅忽略文件已经不存在。"""

    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class BackgroundProcessCleanupError(RuntimeError):
    """后台启动失败后，无法确认已创建进程完成清理。"""


class _BackgroundProcessStartCleanupContext(RuntimeError):
    """保留原始异常对象，仅在消息中呈现脱敏类型。"""

    def __init__(
        self,
        *,
        start_error: BaseException,
        cleanup_error: BaseException,
    ) -> None:
        self._start_error = start_error
        self._cleanup_error = cleanup_error
        super().__init__(
            "Background process start cleanup diagnostics: "
            f"start_error={type(start_error).__name__}, "
            f"cleanup_error={type(cleanup_error).__name__}"
        )


class _BackgroundProcessLaunchOwnershipError(RuntimeError):
    """保留已创建的 Popen，使外层在 Handle 绑定失败时仍可清理。"""

    def __init__(
        self,
        *,
        proc: subprocess.Popen,
        launch_error: BaseException,
        ownership_retry_error: BaseException,
    ) -> None:
        self.proc = proc
        self._launch_error = launch_error
        self._ownership_retry_error = ownership_retry_error
        super().__init__(
            "Background process ownership publication failed: "
            f"launch_error={type(launch_error).__name__}, "
            f"ownership_retry_error={type(ownership_retry_error).__name__}"
        )


@dataclass(slots=True)
class _DeferredResourceClose:
    """跟踪一个可能阻塞的资源 close worker。"""

    resource: object
    completed: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    started: bool = False
    error: BaseException | None = None


@dataclass(slots=True)
class _StdinOperation:
    """由唯一 stdin worker 串行执行的一次输入操作。"""

    kind: str
    payload: bytes | None = field(default=None, repr=False)
    completed: threading.Event = field(default_factory=threading.Event)
    bytes_written: int = 0
    close_performed: bool = False
    timed_out: bool = False
    delivery_unknown: bool = False
    error: BaseException | None = field(default=None, repr=False)


_BACKGROUND_DISPOSE_WAIT_SECONDS = 5.0
_BACKGROUND_DISPOSE_RETRY_WAIT_SECONDS = 5.0
_PROCESS_TREE_WAIT_POLL_SECONDS = 0.05
_BACKGROUND_GATE_ACCEPT_WAIT_SECONDS = 5.0
_BACKGROUND_GATE_ACCEPT_POLL_SECONDS = 0.05
_BACKGROUND_GATE_REMOVE_WAIT_SECONDS = 1.0


class LocalBackgroundProcessHandle(BackgroundProcessHandle):
    """由 LocalBackend 启动的本地后台进程及其独立资源。"""

    _OUTPUT_READ_CHUNK_BYTES = 8192
    _MAX_PENDING_OUTPUT_CHARS = 256_000
    _FINAL_OUTPUT_WAIT_SECONDS = 0.5
    _CLOSE_OUTPUT_WAIT_SECONDS = 0.5
    _PIPE_CLOSE_WAIT_SECONDS = 0.5
    _CLOSE_TOTAL_WAIT_SECONDS = 1.0
    _STDIN_OPERATION_WAIT_SECONDS = 1.0

    def __init__(
        self,
        *,
        job_handle: object | None = None,
        job_assigned: bool = False,
        snapshot_path: Path | None = None,
        startup_gate_path: Path | None = None,
        started_cwd: str | None = None,
    ) -> None:
        self._started_cwd = started_cwd
        self._proc: subprocess.Popen | None = None
        self._job_handle = job_handle
        self._job_assigned = job_assigned
        self._job_assignment_pending = False
        self._process_group_id: int | None = None
        self._snapshot_path = snapshot_path
        self._startup_gate_path = startup_gate_path
        self._operation_lock = threading.Lock()
        self._close_lock = threading.RLock()
        self._output_lock = threading.Lock()
        self._pending_output: deque[str] = deque()
        self._pending_chars = 0
        self._discarded_chars = 0
        self._stdout = None
        self._output_eof_event = threading.Event()
        self._output_error: Exception | None = None
        self._closed = False
        self._stdin_condition = threading.Condition()
        self._stdin_pipe = None
        self._stdin_supported = False
        self._stdin_close_requested = False
        self._stdin_closed = True
        self._stdin_operation: _StdinOperation | None = None
        self._stdin_worker: threading.Thread | None = None
        self._stdin_worker_stop = False
        self._stdout_close_state: _DeferredResourceClose | None = None
        self._output_thread: threading.Thread | None = None

    def attach_process(self, proc: subprocess.Popen) -> None:
        """把刚创建的 Popen 绑定到最小 Handle；同一进程可安全重入。"""

        with self._operation_lock:
            if self._closed:
                raise RuntimeError("Background process handle is closed")
            if self._proc is not None and self._proc is not proc:
                raise RuntimeError("Background process handle is initialized")
            # 根进程和 PGID 必须最先落入 Handle，后续管道读取失败也不丢失树级控制权。
            self._proc = proc
            if sys.platform != "win32":
                self._process_group_id = proc.pid
            self._stdout = proc.stdout
            stdin = proc.stdin
        with self._stdin_condition:
            self._stdin_pipe = stdin
            self._stdin_supported = stdin is not None
            self._stdin_closed = stdin is None
            self._stdin_close_requested = stdin is None

    def owns_process(self) -> bool:
        """返回最小 Handle 是否已经接管一个成功创建的 Popen。"""

        with self._operation_lock:
            return self._proc is not None

    def _require_process_locked(self) -> subprocess.Popen:
        """在 operation_lock 内取得根进程。"""

        if self._proc is None:
            raise RuntimeError("Background process handle is not initialized")
        return self._proc

    def assign_windows_job(self) -> None:
        """在 Handle 已建立所有权后，把根进程加入后台 Job。"""

        if sys.platform != "win32":
            return
        with self._operation_lock:
            if self._closed:
                raise RuntimeError("Background process handle is closed")
            if self._job_assigned:
                return
            if self._job_assignment_pending:
                raise RuntimeError(
                    "Windows Job assignment state is unknown"
                )
            proc = self._require_process_locked()
            self._job_assignment_pending = True
            try:
                _assign_windows_job(proc, self._job_handle)
            except Exception:
                # 普通 API 失败可以确认未分配；异步 BaseException 则保留 unknown。
                self._job_assignment_pending = False
                raise
            else:
                self._job_assigned = True
                self._job_assignment_pending = False

    def start_output_reader(self) -> None:
        """在 Handle 已可清理后启动唯一的后台输出读取线程。"""

        with self._operation_lock:
            if self._closed:
                raise RuntimeError("Background process handle is closed")
            if self._output_thread is not None:
                return
            if self._stdout is None:
                output_error = RuntimeError(
                    "Background process stdout pipe is unavailable"
                )
                with self._output_lock:
                    self._output_error = output_error
                raise output_error
            proc = self._require_process_locked()
            output_thread = threading.Thread(
                target=self._collect_output,
                name=f"hermes-local-background-output-{proc.pid}",
                daemon=True,
            )
            self._output_eof_event.clear()
            self._output_thread = output_thread
        try:
            output_thread.start()
        except BaseException:
            with self._operation_lock:
                if self._output_thread is output_thread:
                    self._output_thread = None
            with self._output_lock:
                if self._output_error is None:
                    self._output_error = RuntimeError(
                        "Background output reader could not start"
                    )
            self._output_eof_event.set()
            raise

    @property
    def started_cwd(self) -> str | None:
        """返回创建 Handle 时冻结的后台进程启动目录。"""

        return self._started_cwd

    @property
    def pid(self) -> int | None:
        """返回受管宿主进程的 PID，供诊断使用。"""

        with self._operation_lock:
            if self._proc is None:
                return None
            return self._proc.pid

    def poll(self) -> int | None:
        """非阻塞查询本地进程是否结束。"""

        with self._operation_lock:
            self._require_process_locked()
            return self._poll_process_tree_locked()

    def read_available(self) -> BackgroundProcessOutput:
        """原子返回增量文本、丢弃字符计数和读取错误。"""

        if (
            not self._has_pending_output_state()
            and self.poll() is not None
        ):
            self._wait_output_eof(self._FINAL_OUTPUT_WAIT_SECONDS)
        return self._take_pending_output_batch()

    def read_final_output(self) -> BackgroundProcessOutput:
        """仅在成功 close 后原子消费不会再增长的最终输出批次。"""

        with self._operation_lock:
            if not self._closed:
                raise RuntimeError("Background process handle is not closed")
            if self._output_thread is not None:
                raise RuntimeError(
                    "Background output reader is still running"
                )
            if self._stdout_close_state is not None:
                raise RuntimeError(
                    "Background pipe close is still running"
                )
            if not self._output_eof_event.is_set():
                raise RuntimeError("Background output EOF is unavailable")
        with self._stdin_condition:
            if (
                self._stdin_operation is not None
                or self._stdin_worker is not None
            ):
                raise RuntimeError(
                    "Background stdin worker is still running"
                )
        return self._take_pending_output_batch()

    def _take_pending_output_batch(self) -> BackgroundProcessOutput:
        """统一消费 pending 文本、丢弃计数和读取错误。"""

        with self._output_lock:
            output = "".join(self._pending_output)
            discarded_chars = self._discarded_chars
            read_error = self._output_error
            result = BackgroundProcessOutput(
                text=output,
                discarded_chars=discarded_chars,
                read_error=read_error,
            )
            self._pending_output.clear()
            self._pending_chars = 0
            self._discarded_chars = 0
            self._output_error = None
            return result

    def wait(self, timeout: float | None = None) -> int | None:
        """有限等待本地进程；超时不向上泄漏 TimeoutExpired。"""

        deadline = (
            None if timeout is None else time.monotonic() + max(timeout, 0.0)
        )
        while True:
            with self._operation_lock:
                proc = self._require_process_locked()
                exit_code = self._poll_process_tree_locked()
                root_process_exited = proc.poll() is not None
            if exit_code is not None:
                return exit_code

            if deadline is None:
                wait_seconds = _PROCESS_TREE_WAIT_POLL_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                wait_seconds = min(
                    _PROCESS_TREE_WAIT_POLL_SECONDS,
                    remaining,
                )

            if root_process_exited:
                time.sleep(wait_seconds)
                continue
            try:
                proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _encode_stdin_payload(data: str) -> bytes:
        """在 Handle 边界再次校验文本与 UTF-8 字节上限。"""

        if not isinstance(data, str):
            raise ProcessInputError("Process input must be text")
        try:
            payload = data.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProcessInputError(
                "Process input must be valid UTF-8 text"
            ) from error
        if len(payload) > MAX_PROCESS_STDIN_BYTES:
            raise ProcessInputError("Process input exceeds the size limit")
        return payload

    def write_stdin(self, data: str) -> int:
        """通过唯一 stdin worker 完整写入并 flush 一段 UTF-8 文本。"""

        payload = self._encode_stdin_payload(data)
        deadline = time.monotonic() + self._STDIN_OPERATION_WAIT_SECONDS
        with self._stdin_condition:
            if not self._stdin_supported:
                raise ProcessInputUnavailableError(
                    "Process stdin is not available"
                )
            if self._stdin_closed or self._stdin_close_requested:
                raise ProcessInputClosedError(
                    "Process stdin is already closed"
                )
            current = self._stdin_operation
            if current is not None:
                if current.timed_out:
                    raise ProcessInputDeliveryError(
                        "Process input delivery could not be confirmed",
                        delivery_unknown=True,
                    )
                raise ProcessInputBusyError(
                    "A process input operation is still in progress"
                )

            self._ensure_stdin_worker_locked()
            operation = _StdinOperation(
                kind="write",
                payload=payload,
            )
            # 先唤醒再发布仍安全：worker 只能在 condition 释放后读取状态。
            self._stdin_condition.notify_all()
            self._stdin_operation = operation

        self._wait_for_stdin_operation(
            operation,
            deadline=deadline,
            close_operation=False,
        )
        if operation.bytes_written != len(payload):
            raise ProcessInputDeliveryError(
                "Process input delivery could not be confirmed",
                delivery_unknown=True,
            )
        return operation.bytes_written

    def close_stdin(self) -> bool:
        """有界关闭真实 stdin pipe；重复关闭返回 False。"""

        deadline = time.monotonic() + self._STDIN_OPERATION_WAIT_SECONDS
        return self._close_stdin_before(deadline)

    def _close_stdin_before(self, deadline: float) -> bool:
        """在绝对截止时间前串行收敛 write，并执行或确认一次 close。"""

        while True:
            with self._stdin_condition:
                if not self._stdin_supported:
                    raise ProcessInputUnavailableError(
                        "Process stdin is not available"
                    )
                if self._stdin_closed:
                    return False

                # 一旦收到 close 请求，任何后续 write 都必须永久拒绝。
                self._stdin_close_requested = True
                current = self._stdin_operation
                if current is None:
                    self._ensure_stdin_worker_locked()
                    operation = _StdinOperation(kind="close")
                    # condition 释放前 worker 不可观察，因此不会丢失发布。
                    self._stdin_condition.notify_all()
                    self._stdin_operation = operation
                    worker = self._stdin_worker
                    break
                if current.kind == "close":
                    operation = current
                    worker = self._stdin_worker
                    break

                remaining = max(0.0, deadline - time.monotonic())
                if remaining <= 0:
                    raise ProcessInputCloseError(
                        "Process stdin closure could not be confirmed"
                    )
                self._stdin_condition.wait(timeout=remaining)

        self._wait_for_stdin_operation(
            operation,
            deadline=deadline,
            close_operation=True,
        )
        if not operation.close_performed:
            raise ProcessInputCloseError(
                "Process stdin closure could not be confirmed"
            )

        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                raise ProcessInputCloseError(
                    "Process stdin closure could not be confirmed"
                )
        return True

    def _ensure_stdin_worker_locked(self) -> None:
        """在 stdin condition 内确保至多一个串行 worker 存活。"""

        worker = self._stdin_worker
        if worker is not None and worker.is_alive():
            return
        if worker is not None:
            self._stdin_worker = None
        self._stdin_worker_stop = False
        worker = threading.Thread(
            target=self._stdin_worker_loop,
            name="hermes-local-background-stdin",
            daemon=True,
        )
        self._stdin_worker = worker
        try:
            worker.start()
        except BaseException as error:
            if (
                self._stdin_worker is worker
                and not worker.is_alive()
            ):
                self._stdin_worker = None
            if not isinstance(error, Exception):
                raise
            raise ProcessInputUnavailableError(
                "Process stdin is not available"
            ) from error

    def _stdin_worker_loop(self) -> None:
        """串行执行唯一活动输入操作，不维护无界输入队列。"""

        current_thread = threading.current_thread()
        try:
            while True:
                with self._stdin_condition:
                    while (
                        self._stdin_operation is None
                        and not self._stdin_worker_stop
                    ):
                        self._stdin_condition.wait()
                    if (
                        self._stdin_worker_stop
                        and self._stdin_operation is None
                    ):
                        return
                    operation = self._stdin_operation

                if operation is None:
                    continue
                self._execute_stdin_operation(operation)

                with self._stdin_condition:
                    if self._stdin_operation is operation:
                        self._stdin_operation = None
                    if (
                        operation.kind == "close"
                        and operation.close_performed
                    ):
                        self._stdin_worker_stop = True
                    operation.completed.set()
                    self._stdin_condition.notify_all()
        finally:
            with self._stdin_condition:
                operation = self._stdin_operation
                if (
                    operation is not None
                    and not operation.completed.is_set()
                ):
                    operation.error = RuntimeError(
                        "Background stdin worker stopped unexpectedly"
                    )
                    operation.payload = None
                    if operation.kind == "write":
                        operation.delivery_unknown = True
                    self._stdin_operation = None
                    operation.completed.set()
                if self._stdin_worker is current_thread:
                    self._stdin_worker = None
                self._stdin_condition.notify_all()

    def _execute_stdin_operation(
        self,
        operation: _StdinOperation,
    ) -> None:
        """执行一次 write/close，并只在完成事件中发布结果。"""

        with self._stdin_condition:
            stdin = self._stdin_pipe
        if stdin is None:
            operation.error = RuntimeError(
                "Background process stdin pipe is unavailable"
            )
            operation.payload = None
            return

        try:
            if operation.kind == "write":
                payload = operation.payload or b""
                offset = 0
                view = memoryview(payload)
                while offset < len(payload):
                    # 调用一旦开始，异常可能发生在底层已接收部分字节之后。
                    operation.delivery_unknown = True
                    written = stdin.write(view[offset:])
                    if (
                        isinstance(written, bool)
                        or not isinstance(written, int)
                        or written <= 0
                        or written > len(payload) - offset
                    ):
                        raise OSError("Background stdin write made no progress")
                    offset += written
                    operation.bytes_written = offset
                operation.delivery_unknown = True
                stdin.flush()
            elif operation.kind == "close":
                stdin.close()
                with self._stdin_condition:
                    if self._stdin_pipe is stdin:
                        self._stdin_pipe = None
                    self._stdin_closed = True
                operation.close_performed = True
            else:
                raise RuntimeError("Unknown background stdin operation")
        except BaseException as error:
            operation.error = error
            if operation.kind == "close":
                try:
                    pipe_closed = bool(getattr(stdin, "closed"))
                except BaseException:
                    pipe_closed = False
                if pipe_closed:
                    with self._stdin_condition:
                        if self._stdin_pipe is stdin:
                            self._stdin_pipe = None
                        self._stdin_closed = True
                    operation.close_performed = True
        finally:
            operation.payload = None

    def _wait_for_stdin_operation(
        self,
        operation: _StdinOperation,
        *,
        deadline: float,
        close_operation: bool,
    ) -> None:
        """有限等待单次操作；超时保留原操作并报告未知结果。"""

        remaining = max(0.0, deadline - time.monotonic())
        if not operation.completed.wait(timeout=remaining):
            with self._stdin_condition:
                if not operation.completed.is_set():
                    operation.timed_out = True
            if not operation.completed.is_set():
                if close_operation:
                    raise ProcessInputCloseError(
                        "Process stdin closure could not be confirmed"
                    )
                raise ProcessInputDeliveryError(
                    "Process input delivery could not be confirmed",
                    delivery_unknown=True,
                )

        if operation.error is None:
            return
        if close_operation:
            raise ProcessInputCloseError(
                "Process stdin closure could not be confirmed"
            ) from operation.error
        delivery_unknown = operation.delivery_unknown
        raise ProcessInputDeliveryError(
            (
                "Process input delivery could not be confirmed"
                if delivery_unknown
                else "Process input could not be delivered"
            ),
            delivery_unknown=delivery_unknown,
        ) from operation.error

    def interrupt(self) -> bool:
        """请求整个本地进程组协作式退出，并返回是否真正发送了信号。"""

        with self._operation_lock:
            proc = self._require_process_locked()
            process_group_id = self._process_group_id
            job_may_be_assigned = (
                self._job_assigned or self._job_assignment_pending
            )
            if sys.platform != "win32" and process_group_id is None:
                raise RuntimeError("Local process group is unavailable")
            if (
                sys.platform == "win32"
                and job_may_be_assigned
                and proc.poll() is not None
            ):
                if (
                    _query_windows_job_active_processes(self._job_handle)
                    == 0
                ):
                    return False
                raise RuntimeError(
                    "Could not interrupt local Windows process tree"
                )
            signal_sent = _interrupt_local_process(
                proc,
                process_group_id=process_group_id,
                interrupt_group_after_root_exit=True,
            )
            if (
                not signal_sent
                and sys.platform == "win32"
                and job_may_be_assigned
                and _query_windows_job_active_processes(self._job_handle) > 0
            ):
                raise RuntimeError(
                    "Could not interrupt local Windows process tree"
                )
            return signal_sent

    def kill(self) -> bool:
        """强制终止整个本地进程树，并返回是否真正执行了终止操作。"""

        with self._operation_lock:
            proc = self._require_process_locked()
            process_group_id = self._process_group_id
            if sys.platform != "win32" and process_group_id is None:
                raise RuntimeError("Local process group is unavailable")
            return _kill_local_process_tree(
                proc,
                self._job_handle,
                job_assigned=(
                    self._job_assigned
                    or self._job_assignment_pending
                ),
                process_group_id=process_group_id,
                terminate_job_after_root_exit=True,
                terminate_group_after_root_exit=True,
            )

    def process_tree_is_terminated(self) -> bool:
        """确认当前 Handle 受管的本地进程树是否已经结束。"""

        with self._operation_lock:
            self._require_process_locked()
            return self._process_tree_is_terminated_locked()

    def close(self) -> None:
        """有限等待输出回收，并保留失败资源供后续 close 重试。"""

        close_deadline = (
            time.monotonic() + self._CLOSE_TOTAL_WAIT_SECONDS
        )
        acquired = self._close_lock.acquire(
            timeout=self._remaining_close_time(close_deadline)
        )
        if not acquired:
            close_error = RuntimeError(
                "Background process handle close failed"
            )
            close_error._resource_errors = (
                RuntimeError("Background close serialization timed out"),
            )
            raise close_error
        try:
            self._close_serialized(close_deadline)
        finally:
            self._close_lock.release()

    def _close_serialized(self, close_deadline: float) -> None:
        """在已取得 close 锁后推进一次有界资源释放。"""

        with self._close_lock:
            errors: list[BaseException] = []

            # Windows Job 必须先关闭，避免存活子进程持有 stdout 造成循环等待。
            with self._operation_lock:
                if self._closed:
                    return
                job_handle = self._job_handle
                if sys.platform == "win32" and job_handle is not None:
                    try:
                        _close_windows_job_strict(job_handle)
                    except BaseException as error:
                        errors.append(error)
                    else:
                        if self._job_handle is job_handle:
                            self._job_handle = None
                            self._job_assigned = False
                            self._job_assignment_pending = False

            if errors:
                close_error = RuntimeError(
                    "Background process handle close failed"
                )
                close_error._resource_errors = tuple(errors)
                raise close_error

            with self._operation_lock:
                output_thread = self._output_thread
            if output_thread is None:
                self._output_eof_event.set()
            elif threading.current_thread() is output_thread:
                errors.append(
                    RuntimeError(
                        "Background output reader cannot close itself"
                    )
                )
            else:
                try:
                    output_deadline = min(
                        close_deadline,
                        (
                            time.monotonic()
                            + self._CLOSE_OUTPUT_WAIT_SECONDS
                        ),
                    )
                    self._wait_output_eof(
                        self._remaining_close_time(output_deadline)
                    )
                    output_thread.join(
                        timeout=self._remaining_close_time(output_deadline)
                    )
                except BaseException as error:
                    errors.append(error)
                else:
                    if output_thread.is_alive():
                        errors.append(
                            RuntimeError(
                                "Background output reader did not stop"
                            )
                        )
                    else:
                        with self._operation_lock:
                            if self._output_thread is output_thread:
                                self._output_thread = None

            # 输出线程仍活跃时保留 pipe，避免 stdout.close() 等待其内部读锁。
            if errors:
                close_error = RuntimeError(
                    "Background process handle close failed"
                )
                close_error._resource_errors = tuple(errors)
                raise close_error

            self._close_pipes_with_workers(
                close_deadline=close_deadline,
                errors=errors,
            )

            with self._operation_lock:
                snapshot_path = self._snapshot_path
            if snapshot_path is not None:
                try:
                    _remove_local_path_strict(snapshot_path)
                except BaseException as error:
                    errors.append(error)
                else:
                    with self._operation_lock:
                        if self._snapshot_path == snapshot_path:
                            self._snapshot_path = None

            with self._operation_lock:
                startup_gate_path = self._startup_gate_path
            if startup_gate_path is not None:
                try:
                    _remove_local_path_strict(startup_gate_path)
                except BaseException as error:
                    errors.append(error)
                else:
                    with self._operation_lock:
                        if self._startup_gate_path == startup_gate_path:
                            self._startup_gate_path = None

            with self._stdin_condition:
                stdin_released = (
                    self._stdin_closed
                    and self._stdin_pipe is None
                    and self._stdin_operation is None
                    and self._stdin_worker is None
                )
            with self._operation_lock:
                resources_released = (
                    self._job_handle is None
                    and not self._job_assignment_pending
                    and self._output_thread is None
                    and self._stdout is None
                    and stdin_released
                    and self._stdout_close_state is None
                    and self._snapshot_path is None
                    and self._startup_gate_path is None
                )
                if not errors and resources_released:
                    self._closed = True

            if errors or not resources_released:
                if not errors:
                    errors.append(
                        RuntimeError(
                            "Background process resources remain open"
                        )
                    )
                close_error = RuntimeError(
                    "Background process handle close failed"
                )
                close_error._resource_errors = tuple(errors)
                raise close_error

    @staticmethod
    def _remaining_close_time(deadline: float) -> float:
        """按 monotonic 绝对截止时间计算本次仍可等待的秒数。"""

        return max(0.0, deadline - time.monotonic())

    @staticmethod
    def _run_deferred_resource_close(
        state: _DeferredResourceClose,
    ) -> None:
        """在独立 daemon worker 中执行真实资源 close。"""

        try:
            close = getattr(state.resource, "close")
            close()
        except BaseException as error:
            state.error = error
        finally:
            state.completed.set()

    def _new_deferred_resource_close(
        self,
        resource: object,
        *,
        name: str,
    ) -> _DeferredResourceClose:
        """创建但不启动一个资源 close worker。"""

        state = _DeferredResourceClose(resource=resource)
        state.thread = threading.Thread(
            target=self._run_deferred_resource_close,
            args=(state,),
            name=name,
            daemon=True,
        )
        return state

    @staticmethod
    def _start_deferred_resource_close(
        state: _DeferredResourceClose,
    ) -> None:
        """启动 worker；启动失败也通过同一完成状态向 close 汇报。"""

        thread = state.thread
        if thread is None:
            state.error = RuntimeError(
                "Background pipe close worker is unavailable"
            )
            state.completed.set()
            return
        try:
            thread.start()
        except BaseException as error:
            state.thread = None
            state.error = error
            state.completed.set()
        else:
            state.started = True

    def _close_pipes_with_workers(
        self,
        *,
        close_deadline: float,
        errors: list[BaseException],
    ) -> None:
        """先收敛唯一 stdin worker，再有限关闭 stdout pipe。"""

        with self._stdin_condition:
            stdin_needs_close = not self._stdin_closed
        if stdin_needs_close:
            try:
                self._close_stdin_before(close_deadline)
            except BaseException as error:
                errors.append(error)

        states_to_start: list[_DeferredResourceClose] = []
        states_to_wait: list[_DeferredResourceClose] = []
        with self._operation_lock:
            stdout = self._stdout
            if stdout is not None:
                if self._stdout_close_state is None:
                    self._stdout_close_state = (
                        self._new_deferred_resource_close(
                            stdout,
                            name="hermes-local-stdout-close",
                        )
                    )
                    states_to_start.append(self._stdout_close_state)
                states_to_wait.append(self._stdout_close_state)

        for state in states_to_start:
            self._start_deferred_resource_close(state)

        pipe_deadline = min(
            close_deadline,
            time.monotonic() + self._PIPE_CLOSE_WAIT_SECONDS,
        )
        for state in states_to_wait:
            if not state.completed.is_set():
                state.completed.wait(
                    timeout=self._remaining_close_time(pipe_deadline)
                )
            thread = state.thread
            if (
                state.completed.is_set()
                and state.started
                and thread is not None
            ):
                thread.join(
                    timeout=self._remaining_close_time(pipe_deadline)
                )

        with self._operation_lock:
            for state in states_to_wait:
                if not state.completed.is_set():
                    errors.append(
                        RuntimeError(
                            "Background pipe close timed out"
                        )
                    )
                    continue
                thread = state.thread
                if (
                    state.started
                    and thread is not None
                    and thread.is_alive()
                ):
                    errors.append(
                        RuntimeError(
                            "Background pipe close worker did not stop"
                        )
                    )
                    continue
                if state.error is not None:
                    errors.append(state.error)
                    if self._stdout_close_state is state:
                        self._stdout_close_state = None
                    continue

                if self._stdout_close_state is state:
                    if self._stdout is state.resource:
                        self._stdout = None
                    self._stdout_close_state = None

    def _poll_process_tree_locked(self) -> int | None:
        """仅在受管 Windows Job 或 POSIX 进程组结束后报告退出。"""

        proc = self._require_process_locked()
        exit_code = proc.poll()
        if sys.platform == "win32" and (
            self._job_assigned or self._job_assignment_pending
        ):
            active_processes = _query_windows_job_active_processes(
                self._job_handle
            )
            if active_processes > 0:
                return None
        if exit_code is None:
            return None
        if sys.platform != "win32":
            process_group_id = self._process_group_id
            if process_group_id is None:
                raise RuntimeError("Local process group is unavailable")
            if _posix_process_group_exists(process_group_id):
                return None
        return exit_code

    def _process_tree_is_terminated_locked(self) -> bool:
        """在操作锁内确认根进程与其受管进程树均已结束。"""

        proc = self._require_process_locked()
        return _background_process_tree_has_exited(
            proc,
            job_handle=self._job_handle,
            job_assigned=(
                self._job_assigned
                or self._job_assignment_pending
            ),
            process_group_id=self._process_group_id,
        )

    def _collect_output(self) -> None:
        """分块读取 stdout，并把增量文本交给非阻塞读取接口。"""

        stdout = self._stdout
        decoder = None
        output_error: Exception | None = None
        try:
            if stdout is None:
                return
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                output = os.read(stdout.fileno(), self._OUTPUT_READ_CHUNK_BYTES)
                if not output:
                    return
                self._append_pending_output(decoder.decode(output, final=False))
        except Exception as error:
            # 先保存到线程局部，decoder 尾部刷新后再原子发布错误。
            output_error = error
        finally:
            try:
                if decoder is not None:
                    self._append_pending_output(decoder.decode(b"", final=True))
            except Exception as error:
                if output_error is None:
                    output_error = error
            finally:
                if output_error is not None:
                    with self._output_lock:
                        if self._output_error is None:
                            self._output_error = output_error
                self._output_eof_event.set()

    def _append_pending_output(self, output: str) -> None:
        """追加尚未被 ProcessManager 读取的有限增量输出。"""

        if not output:
            return
        with self._output_lock:
            self._pending_output.append(output)
            self._pending_chars += len(output)
            excess = self._pending_chars - self._MAX_PENDING_OUTPUT_CHARS
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
        """查询是否有文本、丢弃计数或读取错误尚需上报。"""

        with self._output_lock:
            return bool(
                self._pending_output
                or self._discarded_chars
                or self._output_error is not None
            )

    def _wait_output_eof(self, timeout: float) -> bool:
        """有限等待输出线程确认 EOF，避免固定 join 导致尾部日志丢失。"""

        return self._output_eof_event.wait(timeout=max(0.0, timeout))


# ---------------------------------------------------------------------------
# Git Bash 发现（仅 Windows；POSIX 系统继续用普通的 `bash`）。
# ---------------------------------------------------------------------------

_GIT_BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
]


def _find_git_bash() -> str:
    """在 Windows 上定位 Git Bash。返回绝对 Windows 路径。

    解析顺序：
      1. config.yaml 里的 terminal.git_bash_path
      2. 环境变量 $HERMES_GIT_BASH_PATH
      3. C:\\Program Files\\Git 下的常见安装位置

    找不到时抛 RuntimeError —— 绝不静默回退到
    ``C:\\Windows\\System32\\bash.exe``（那是 WSL）。
    """
    from hermes.config import _config

    cfg_path = _config.get("terminal", {}).get("git_bash_path")
    if cfg_path and Path(cfg_path).is_file():
        return cfg_path

    env_path = os.environ.get("HERMES_GIT_BASH_PATH")
    if env_path and Path(env_path).is_file():
        return env_path

    for cand in _GIT_BASH_CANDIDATES:
        if Path(cand).is_file():
            return cand

    raise RuntimeError(
        "Git Bash not found. Set `terminal.git_bash_path` in config.yaml "
        "or HERMES_GIT_BASH_PATH env var to the full path of "
        "'C:\\Program Files\\Git\\bin\\bash.exe'. "
        "Hermes refuses to fall back to WSL bash."
    )


# ---------------------------------------------------------------------------
# LocalBackend。
# ---------------------------------------------------------------------------

class LocalBackend(BaseExecutionEnvironment):
    """通过 subprocess 在本机执行命令。

    Windows 上使用 Git Bash（绝不用 WSL）。临时文件放在
    HERMES_HOME/cache/terminal/ 下。cwd 内部以 Windows 形式保存，注入
    bash 命令时转成 MSYS 形式。
    """

    terminal_path_preflight_enabled = True
    backend_type = "local"

    def __init__(
        self,
        cwd: str,
        timeout: int = 180,
        *,
        path_policy: PathAccessPolicy | None = None,
        tool_approval_policy=None,
        env_passthrough: Iterable[str] = (),
        infrastructure_secret_values: Iterable[str] = (),
    ):
        self._env_passthrough = frozenset(env_passthrough)
        self._infrastructure_secret_values = frozenset(
            infrastructure_secret_values
        )
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            path_policy=path_policy,
            tool_approval_policy=tool_approval_policy,
        )

    def _setup_paths(self):
        """Windows 下把 snapshot/cwd 文件迁到 HERMES_HOME/cache/terminal/。

        POSIX 系统沿用基类的 /tmp 默认值。
        """
        if sys.platform != "win32":
            return
        from hermes.config import HERMES_HOME

        tmp_dir = HERMES_HOME / "cache" / "terminal"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        snap_host = tmp_dir / f"hermes-snap-{self._session_id}.sh"
        cwd_host = tmp_dir / f"hermes-cwd-{self._session_id}.txt"

        self._snapshot_host = str(snap_host)
        self._cwd_host = str(cwd_host)
        self._snapshot_shell = _win_to_bash_path(self._snapshot_host)
        self._cwd_shell = _win_to_bash_path(self._cwd_host)

    def _cwd_to_shell(self, cwd: str) -> str:
        if sys.platform == "win32":
            return _win_to_bash_path(cwd)
        return cwd

    def _normalize_cwd(self, raw: str) -> str:
        if sys.platform == "win32" and raw.startswith("/") and len(raw) >= 3 and raw[2] == "/":
            return _bash_to_win_path(raw)
        return raw

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        # 解析 bash 可执行文件。Windows 上必须显式指定 ——
        # `["bash", ...]` 会沿 PATH 查找，可能命中 WSL 的 bash。
        bash = _find_git_bash() if sys.platform == "win32" else "bash"

        env = filter_local_subprocess_environment(
            os.environ,
            env_passthrough=self._env_passthrough,
            infrastructure_secret_values=self._infrastructure_secret_values,
        )
        process_group_options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            [bash, "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",   # bash 输出 UTF-8；不让 Windows 用 GBK 解码
            errors="replace",
            env=env,
            **process_group_options,
        )
        _attach_windows_job(proc)
        return proc

    def spawn_background(
        self,
        command: str,
        *,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> BackgroundProcessHandle:
        """启动不占用前台执行锁的本地后台进程。"""

        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if self._cancel_requested(cancel_checker):
            raise BackgroundProcessCancelledError(
                "Background process start cancelled"
            )

        snapshot_copy: Path | None = None
        startup_gate_path: Path | None = None
        proc: subprocess.Popen | None = None
        job_handle: object | None = None
        job_assigned = False
        handle: LocalBackgroundProcessHandle | None = None
        try:
            # 只在复制当前会话状态时占用前台锁；后台进程运行期间不持有它。
            with self._execute_lock:
                if not self._snapshot_ready:
                    self.init_session()
                started_cwd = self.cwd
                snapshot_copy = self._copy_background_snapshot_locked()
                cwd_shell = self._cwd_to_shell(started_cwd)
                env = filter_local_subprocess_environment(
                    os.environ,
                    env_passthrough=self._env_passthrough,
                    infrastructure_secret_values=(
                        self._infrastructure_secret_values
                    ),
                )

            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )

            snapshot_shell = self._cwd_to_shell(str(snapshot_copy))
            if sys.platform == "win32":
                startup_gate_path = self._new_background_start_gate_path(
                    snapshot_copy
                )
            wrapped = self._wrap_background_command(
                command,
                cwd_shell=cwd_shell,
                snapshot_shell=snapshot_shell,
                startup_gate_shell=(
                    self._cwd_to_shell(str(startup_gate_path))
                    if startup_gate_path is not None
                    else None
                ),
            )
            launch_env = env
            if sys.platform == "win32":
                # 非交互 Bash 会在脚本前执行 BASH_ENV；先移除它，避免绕过启动闸门。
                launch_env = dict(env)
                launch_env.pop("BASH_ENV", None)
                job_handle = _create_windows_job(kill_on_close=True)
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            # 先建立资源所有者；Popen 一旦成功即绑定，消除无 Handle 的存活分支。
            handle = LocalBackgroundProcessHandle(
                job_handle=job_handle,
                job_assigned=False,
                snapshot_path=snapshot_copy,
                startup_gate_path=startup_gate_path,
                started_cwd=started_cwd,
            )
            proc = self._run_background_bash(
                wrapped,
                handle=handle,
                env=launch_env,
            )
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            if sys.platform == "win32":
                handle.assign_windows_job()
                job_assigned = True
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            handle.start_output_reader()
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            if startup_gate_path is not None:
                self._release_background_start_gate(startup_gate_path)
                self._await_background_start_gate_accepted(
                    startup_gate_path,
                    proc,
                    cancel_checker=cancel_checker,
                )
                self._remove_accepted_background_start_gate(
                    startup_gate_path
                )

            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            return handle
        except BaseException as start_error:
            if isinstance(
                start_error,
                _BackgroundProcessLaunchOwnershipError,
            ):
                proc = start_error.proc
            if proc is not None and handle is not None:
                try:
                    # 覆盖 Popen 返回后、首个绑定调用前被异步异常打断的极窄窗口。
                    handle.attach_process(proc)
                except BaseException:
                    # 后续清理会根据 owns_process 选择 Handle 或原始 Popen 路径。
                    pass
            try:
                self._force_dispose_background_before_return(
                    proc=proc,
                    handle=handle,
                    job_handle=job_handle,
                    job_assigned=job_assigned,
                    snapshot_path=snapshot_copy,
                    startup_gate_path=startup_gate_path,
                )
            except BackgroundProcessCleanupError as cleanup_error:
                if handle is not None and handle.owns_process():
                    handoff_error = BackgroundProcessStartCleanupError(
                        (
                            "Background process start failed and cleanup "
                            "could not be confirmed"
                        ),
                        handle=handle,
                        start_error=start_error,
                        cleanup_error=cleanup_error,
                    )
                    raise handoff_error from (
                        _BackgroundProcessStartCleanupContext(
                            start_error=start_error,
                            cleanup_error=cleanup_error,
                        )
                    )
                raise cleanup_error from (
                    _BackgroundProcessStartCleanupContext(
                        start_error=start_error,
                        cleanup_error=cleanup_error,
                    )
                )
            raise

    def _copy_background_snapshot_locked(self) -> Path:
        """在前台锁保护下复制当前 session 的环境快照。"""

        source_path = Path(self._snapshot_host)
        snapshot_copy = source_path.with_name(
            f"hermes-background-snap-{uuid.uuid4().hex}.sh"
        )
        snapshot_copy.write_text(
            source_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return snapshot_copy

    def _wrap_background_command(
        self,
        command: str,
        *,
        cwd_shell: str,
        snapshot_shell: str,
        startup_gate_shell: str | None,
    ) -> str:
        """恢复一次性快照并运行命令，但不写回共享 cwd 或环境。"""

        quoted_snapshot = shlex.quote(snapshot_shell)
        parts: list[str] = []
        if startup_gate_shell is not None:
            quoted_gate = shlex.quote(startup_gate_shell)
            # Popen 不稳定暴露主线程句柄时，使用纯 Bash 内建的启动闸门。
            # 根 Bash 在加入 Job 前不会启动用户命令或外部子进程，并在 stdin 交付前限速轮询。
            parts.extend(
                [
                    (
                        f"while :; do IFS= read -r _hermes_gate < "
                        f"{quoted_gate} || _hermes_gate=; "
                        "[[ $_hermes_gate == ready ]] && break; "
                        "IFS= read -r -t 0.05 _hermes_gate_wait || :; done"
                    ),
                    f"printf 'accepted\\n' > {quoted_gate} || exit",
                ]
            )
        parts.extend(
            [
                f"source {quoted_snapshot} 2>/dev/null",
                f"rm -f {quoted_snapshot}",
                f"cd {shlex.quote(cwd_shell)} 2>/dev/null",
                command,
            ]
        )
        return "; ".join(parts)

    def _run_background_bash(
        self,
        cmd_string: str,
        *,
        handle: LocalBackgroundProcessHandle,
        env: dict[str, str],
    ) -> subprocess.Popen:
        """创建 Bash 后在 helper 返回或抛错前发布给预建 Handle。"""

        bash = _find_git_bash() if sys.platform == "win32" else "bash"
        process_group_options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                [bash, "-c", cmd_string],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                env=env,
                # 后台 stdin 始终保留为二进制 pipe，由 Handle 在协议边界编码。
                **process_group_options,
            )
            handle.attach_process(proc)
            return proc
        except BaseException as launch_error:
            if proc is None:
                raise
            try:
                # 首次绑定可能只完成了根进程字段；重入以补齐 PGID 和管道状态。
                handle.attach_process(proc)
            except BaseException as ownership_retry_error:
                raise _BackgroundProcessLaunchOwnershipError(
                    proc=proc,
                    launch_error=launch_error,
                    ownership_retry_error=ownership_retry_error,
                ) from None
            else:
                raise

    @staticmethod
    def _new_background_start_gate_path(snapshot_copy: Path) -> Path:
        """原子创建尚未放行的 Windows 后台启动闸门。"""

        while True:
            gate_path = snapshot_copy.with_name(
                f"hermes-background-gate-{uuid.uuid4().hex}.ready"
            )
            try:
                with gate_path.open("xb") as gate_file:
                    gate_file.write(b"pending\n")
            except FileExistsError:
                continue
            else:
                return gate_path

    @staticmethod
    def _release_background_start_gate(startup_gate_path: Path) -> None:
        """仅在根 Bash 加入 Job 后放行其执行用户命令。"""

        startup_gate_path.write_bytes(b"ready\n")

    def _await_background_start_gate_accepted(
        self,
        startup_gate_path: Path,
        proc: subprocess.Popen,
        *,
        cancel_checker: Callable[[], bool] | None,
    ) -> None:
        """有限等待 Bash 确认已永久离开 startup gate 的 stdin read。"""

        deadline = time.monotonic() + _BACKGROUND_GATE_ACCEPT_WAIT_SECONDS
        while True:
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            try:
                gate_state = startup_gate_path.read_bytes()
            except OSError:
                gate_state = b""
            if gate_state == b"accepted\n":
                return
            if proc.poll() is not None:
                raise RuntimeError(
                    "Background startup gate acknowledgement failed"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    "Background startup gate acknowledgement failed"
                )
            time.sleep(
                min(_BACKGROUND_GATE_ACCEPT_POLL_SECONDS, remaining)
            )

    @staticmethod
    def _remove_accepted_background_start_gate(
        startup_gate_path: Path,
    ) -> None:
        """有限重试删除已接受 gate，规避 Windows 文件句柄关闭瞬间竞态。"""

        deadline = time.monotonic() + _BACKGROUND_GATE_REMOVE_WAIT_SECONDS
        while True:
            try:
                _remove_local_path_strict(startup_gate_path)
                return
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(
                    min(_BACKGROUND_GATE_ACCEPT_POLL_SECONDS, remaining)
                )

    @staticmethod
    def _close_unmanaged_background_stdin_pipe(
        proc: subprocess.Popen,
    ) -> None:
        """仅在未交给 Handle 的失败清理路径关闭 stdin pipe。"""

        try:
            stdin = proc.stdin
        except BaseException:
            return
        if stdin is None:
            return
        try:
            stdin.close()
        except BaseException:
            pass

    @staticmethod
    def _background_process_tree_has_exited(
        proc: subprocess.Popen,
        *,
        handle: LocalBackgroundProcessHandle | None,
        job_handle: object | None,
        job_assigned: bool,
        process_group_id: int | None,
    ) -> bool:
        """按本地后端的受管进程树语义确认清理完成。"""

        if handle is not None:
            try:
                return handle.process_tree_is_terminated()
            except BaseException:
                return False
        return _background_process_tree_has_exited(
            proc,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
        )

    @staticmethod
    def _wait_for_background_process_exit(
        proc: subprocess.Popen,
        timeout: float,
        *,
        handle: LocalBackgroundProcessHandle | None = None,
    ) -> None:
        """有限等待后台根进程，实际完成条件由进程树检查决定。"""

        try:
            if handle is not None:
                handle.wait(timeout=timeout)
            else:
                proc.wait(timeout=timeout)
        except BaseException:
            pass

    @staticmethod
    def _force_terminate_background_tree(
        proc: subprocess.Popen,
        *,
        handle: LocalBackgroundProcessHandle | None,
        job_handle: object | None,
        job_assigned: bool,
        process_group_id: int | None,
    ) -> bool:
        """对已创建但尚未返回的后台进程执行一次树级强制终止。"""

        if handle is not None:
            return handle.kill()
        return _kill_local_process_tree(
            proc,
            job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
            terminate_job_after_root_exit=True,
            terminate_group_after_root_exit=True,
        )

    @staticmethod
    def _release_unmanaged_background_resources(
        proc: subprocess.Popen,
        job_handle: object | None,
        *,
        snapshot_path: Path | None,
        startup_gate_path: Path | None,
    ) -> None:
        """仅在确认受管进程树结束后释放尚未交给 Handle 的资源。"""

        LocalBackend._close_unmanaged_background_stdin_pipe(proc)
        try:
            _close_windows_job(job_handle)
        except BaseException:
            pass
        try:
            stdout = proc.stdout
        except BaseException:
            stdout = None
        if stdout is not None:
            try:
                stdout.close()
            except BaseException:
                pass
        _remove_local_path(snapshot_path)
        _remove_local_path(startup_gate_path)

    @staticmethod
    def _force_dispose_owned_background_handle(
        handle: LocalBackgroundProcessHandle,
    ) -> None:
        """仅通过 Handle 内部状态执行两轮终止确认和严格关闭。"""

        last_error: BaseException | None = None
        try:
            tree_exited = handle.process_tree_is_terminated()
        except BaseException as error:
            last_error = error
            tree_exited = False

        for wait_seconds in (
            _BACKGROUND_DISPOSE_WAIT_SECONDS,
            _BACKGROUND_DISPOSE_RETRY_WAIT_SECONDS,
        ):
            if tree_exited:
                break
            try:
                handle.kill()
            except BaseException as error:
                last_error = error
            try:
                handle.wait(timeout=wait_seconds)
            except BaseException as error:
                last_error = error
            try:
                tree_exited = handle.process_tree_is_terminated()
            except BaseException as error:
                last_error = error
                tree_exited = False

        if not tree_exited:
            cleanup_error = BackgroundProcessCleanupError(
                "Could not confirm background process cleanup"
            )
            cleanup_error._termination_error = last_error
            raise cleanup_error

        try:
            handle.close()
        except BaseException as close_error:
            cleanup_error = BackgroundProcessCleanupError(
                "Could not confirm background process cleanup"
            )
            cleanup_error._close_error = close_error
            raise cleanup_error

    def _force_dispose_background_before_return(
        self,
        *,
        proc: subprocess.Popen | None,
        handle: LocalBackgroundProcessHandle | None,
        job_handle: object | None,
        job_assigned: bool,
        snapshot_path: Path | None,
        startup_gate_path: Path | None,
    ) -> None:
        """在后台启动失败后确认整个受管进程树退出，随后释放资源。"""

        handle_owns_process = False
        if handle is not None:
            try:
                handle_owns_process = handle.owns_process()
            except BaseException as ownership_error:
                cleanup_error = BackgroundProcessCleanupError(
                    "Could not confirm background process cleanup"
                )
                cleanup_error._ownership_error = ownership_error
                raise cleanup_error

        if handle is not None and handle_owns_process:
            self._force_dispose_owned_background_handle(handle)
            return

        if proc is None:
            if handle is not None:
                try:
                    handle.close()
                except BaseException as close_error:
                    cleanup_error = BackgroundProcessCleanupError(
                        "Could not confirm background process cleanup"
                    )
                    cleanup_error._close_error = close_error
                    raise cleanup_error
                return
            try:
                _close_windows_job(job_handle)
            except BaseException:
                pass
            _remove_local_path(snapshot_path)
            _remove_local_path(startup_gate_path)
            return

        process_group_id = (
            proc.pid if sys.platform != "win32" else None
        )
        if not self._background_process_tree_has_exited(
            proc,
            handle=None,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
        ):
            try:
                self._force_terminate_background_tree(
                    proc,
                    handle=None,
                    job_handle=job_handle,
                    job_assigned=job_assigned,
                    process_group_id=process_group_id,
                )
            except BaseException:
                pass
            self._wait_for_background_process_exit(
                proc,
                _BACKGROUND_DISPOSE_WAIT_SECONDS,
                handle=None,
            )

        if not self._background_process_tree_has_exited(
            proc,
            handle=None,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
        ):
            try:
                self._force_terminate_background_tree(
                    proc,
                    handle=None,
                    job_handle=job_handle,
                    job_assigned=job_assigned,
                    process_group_id=process_group_id,
                )
            except BaseException:
                pass
            self._wait_for_background_process_exit(
                proc,
                _BACKGROUND_DISPOSE_RETRY_WAIT_SECONDS,
                handle=None,
            )

        if not self._background_process_tree_has_exited(
            proc,
            handle=None,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
        ):
            raise BackgroundProcessCleanupError(
                "Could not confirm background process cleanup"
            )

        self._release_unmanaged_background_resources(
            proc,
            job_handle,
            snapshot_path=snapshot_path,
            startup_gate_path=startup_gate_path,
        )

    def _interrupt_process(self, proc: subprocess.Popen) -> None:
        """向整组进程发送与终端 Ctrl+C 等价的软中断。"""

        _interrupt_local_process(
            proc,
            interrupt_group_after_root_exit=False,
        )

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """软中断无效时强制结束本地命令的进程树。"""

        try:
            _kill_local_process_tree(
                proc,
                getattr(proc, "_hermes_job_handle", None),
                job_assigned=(
                    getattr(proc, "_hermes_job_handle", None) is not None
                ),
                terminate_job_after_root_exit=False,
                terminate_group_after_root_exit=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            # 前台取消沿用既有的尽力而为语义；后台 Handle 则必须保留真实失败。
            pass

    def _release_process_resources(self, proc: subprocess.Popen) -> None:
        """关闭 Windows Job 句柄；正常结束时不终止后台子进程。"""
        if sys.platform != "win32":
            return
        job_handle = getattr(proc, "_hermes_job_handle", None)
        if not job_handle:
            return
        _close_windows_job(job_handle)
        proc._hermes_job_handle = None

    def cleanup(self):
        """通过 HOST 路径删除 snapshot/cwd 文件。"""
        for path in [self._snapshot_host, self._cwd_host]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    # --- 文件 IO：LocalBackend 直接走 Python 标准库 ---

    def resolve_path(self, rel_path: str) -> str:
        """相对路径以 cwd 为基准；同时接受 MSYS 形式（``/d/...``）方便 LLM。"""
        rel_path = os.path.expandvars(os.path.expanduser(rel_path))
        if (
            sys.platform == "win32"
            and rel_path.startswith("/")
            and len(rel_path) >= 2
            and rel_path[1].isalpha()
            and (len(rel_path) == 2 or rel_path[2] == "/")
        ):
            rel_path = _bash_to_win_path(rel_path)
        return super().resolve_path(rel_path)

    def read_file(self, path: str, offset: int = 0, limit: int | None = None) -> bytes:
        """按字节读取文件，offset/limit 都是字节单位。"""
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(limit) if limit is not None else f.read()

    def write_file(self, path: str, content: bytes, mode: str = "write") -> None:
        """写入文件。write 模式走 tmp + os.replace 原子替换；append 直接追加。"""
        # 父目录不存在时自动创建，对齐旧 write_file 的行为。
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if mode == "append":
            with open(path, "ab") as f:
                f.write(content)
            return

        tmp = path + ".hermes.tmp"
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, path)

    def list_dir(self, path: str) -> list[str]:
        """返回目录下条目名（不含路径前缀）。"""
        return [entry.name for entry in os.scandir(path)]

    def stat_file(self, path: str) -> dict:
        """返回文件元数据。"""
        st = os.stat(path)
        return {
            "size": st.st_size,
            "is_dir": stat_mod.S_ISDIR(st.st_mode),
            "is_file": stat_mod.S_ISREG(st.st_mode),
            "mtime": st.st_mtime,
        }
