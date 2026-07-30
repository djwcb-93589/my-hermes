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
    BackgroundProcessStartCleanupError,
    BaseExecutionEnvironment,
    filter_local_subprocess_environment,
)
from hermes.path_policy import PathAccessPolicy
from hermes.path_utils import (
    git_bash_to_windows_path as _bash_to_win_path,
    windows_to_git_bash_path as _win_to_bash_path,
)
from hermes.processes import BackgroundProcessHandle


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
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


def _close_windows_job(job_handle: object | None) -> None:
    """释放由后台 Handle 持有的 Windows Job 句柄。"""

    if sys.platform == "win32" and job_handle:
        _kernel32.CloseHandle(job_handle)


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
    tree_termination_sent: bool = False,
) -> bool:
    """确认受管后台进程树是否已按平台语义完成收敛。"""

    if not _root_process_has_exited(proc):
        return False
    if sys.platform == "win32":
        if not job_assigned:
            return True
        if not job_handle:
            return False
        # 后台 Job 已收到树级终止请求后，close 的 kill-on-close 会兜底剩余成员。
        return tree_termination_sent
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
        if root_exited and not job_is_assigned:
            return False

        job_terminated = False
        taskkill_succeeded = False
        root_killed = False
        failures: list[BaseException] = []

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


class BackgroundProcessCleanupError(RuntimeError):
    """后台启动失败后，无法确认已创建进程完成清理。"""


_BACKGROUND_DISPOSE_WAIT_SECONDS = 5.0
_BACKGROUND_DISPOSE_RETRY_WAIT_SECONDS = 5.0
_PROCESS_TREE_WAIT_POLL_SECONDS = 0.05


class LocalBackgroundProcessHandle(BackgroundProcessHandle):
    """由 LocalBackend 启动的本地后台进程及其独立资源。"""

    _OUTPUT_READ_CHUNK_BYTES = 8192
    _MAX_PENDING_OUTPUT_CHARS = 256_000
    _FINAL_OUTPUT_WAIT_SECONDS = 0.5
    _CLOSE_OUTPUT_WAIT_SECONDS = 0.5

    def __init__(
        self,
        proc: subprocess.Popen,
        *,
        job_handle: object | None = None,
        job_assigned: bool | None = None,
        snapshot_path: Path | None = None,
        startup_gate_path: Path | None = None,
    ) -> None:
        if proc.stdout is None:
            raise RuntimeError("Background process stdout pipe is unavailable")

        self._proc = proc
        self._job_handle = job_handle
        self._job_assigned = (
            job_handle is not None if job_assigned is None else job_assigned
        )
        self._process_group_id = (
            proc.pid if sys.platform != "win32" else None
        )
        self._tree_termination_sent = False
        self._snapshot_path = snapshot_path
        self._startup_gate_path = startup_gate_path
        self._operation_lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._pending_output: deque[str] = deque()
        self._pending_chars = 0
        self._pending_output_truncated = False
        self._stdout = proc.stdout
        self._output_eof_event = threading.Event()
        self._output_error: Exception | None = None
        self._closed = False
        self._output_thread: threading.Thread | None = None

    def start_output_reader(self) -> None:
        """在 Handle 已可清理后启动唯一的后台输出读取线程。"""

        with self._operation_lock:
            if self._closed:
                raise RuntimeError("Background process handle is closed")
            if self._output_thread is not None:
                return
            output_thread = threading.Thread(
                target=self._collect_output,
                name=f"hermes-local-background-output-{self._proc.pid}",
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
            self._output_eof_event.set()
            raise

    @property
    def pid(self) -> int | None:
        """返回受管宿主进程的 PID，供诊断使用。"""

        return self._proc.pid

    def poll(self) -> int | None:
        """非阻塞查询本地进程是否结束。"""

        with self._operation_lock:
            return self._poll_process_tree_locked()

    def read_available(self) -> str:
        """立即返回输出线程已收集的增量文本。"""

        if self.poll() is not None and not self._has_pending_output():
            self._wait_output_eof(self._FINAL_OUTPUT_WAIT_SECONDS)
        with self._output_lock:
            if not self._pending_output:
                return ""
            output = "".join(self._pending_output)
            self._pending_output.clear()
            self._pending_chars = 0
            return output

    def wait(self, timeout: float | None = None) -> int | None:
        """有限等待本地进程；超时不向上泄漏 TimeoutExpired。"""

        deadline = (
            None if timeout is None else time.monotonic() + max(timeout, 0.0)
        )
        while True:
            with self._operation_lock:
                exit_code = self._poll_process_tree_locked()
                proc = self._proc
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

    def interrupt(self) -> bool:
        """请求整个本地进程组协作式退出，并返回是否真正发送了信号。"""

        with self._operation_lock:
            return _interrupt_local_process(
                self._proc,
                process_group_id=self._process_group_id,
                interrupt_group_after_root_exit=True,
            )

    def kill(self) -> bool:
        """强制终止整个本地进程树，并返回是否真正执行了终止操作。"""

        with self._operation_lock:
            signal_sent = _kill_local_process_tree(
                self._proc,
                self._job_handle,
                job_assigned=self._job_assigned,
                process_group_id=self._process_group_id,
                terminate_job_after_root_exit=True,
                terminate_group_after_root_exit=True,
            )
            if signal_sent:
                self._tree_termination_sent = True
            return signal_sent

    def process_tree_is_terminated(self) -> bool:
        """确认当前 Handle 受管的本地进程树是否已经结束。"""

        with self._operation_lock:
            return self._process_tree_is_terminated_locked()

    def close(self) -> None:
        """释放管道、输出读取线程所用资源和 Windows Job。"""

        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            try:
                process_exited = _root_process_has_exited(self._proc)
            except Exception:
                process_exited = False
            stdout = self._stdout
            self._stdout = None
            job_handle = self._job_handle
            self._job_handle = None
            snapshot_path = self._snapshot_path
            self._snapshot_path = None
            startup_gate_path = self._startup_gate_path
            self._startup_gate_path = None
            output_thread = self._output_thread
            try:
                stdin = self._proc.stdin
            except BaseException:
                stdin = None

        try:
            try:
                # 后台 Job 的最后一个句柄关闭会终止尚未退出的成员进程。
                _close_windows_job(job_handle)
            except BaseException:
                pass
            if output_thread is None:
                self._output_eof_event.set()
            if process_exited or job_handle is not None:
                try:
                    self._wait_output_eof(self._CLOSE_OUTPUT_WAIT_SECONDS)
                except BaseException:
                    pass
            if stdin is not None:
                try:
                    stdin.close()
                except BaseException:
                    pass
            if stdout is not None:
                try:
                    stdout.close()
                except BaseException:
                    pass
            if (
                output_thread is not None
                and threading.current_thread() is not output_thread
            ):
                try:
                    output_thread.join(timeout=self._CLOSE_OUTPUT_WAIT_SECONDS)
                except BaseException:
                    pass
        finally:
            _remove_local_path(snapshot_path)
            _remove_local_path(startup_gate_path)

    def _poll_process_tree_locked(self) -> int | None:
        """仅在受管 POSIX 进程组消失后报告根进程退出。"""

        exit_code = self._proc.poll()
        if exit_code is None:
            return None
        if (
            sys.platform != "win32"
            and self._process_group_id is not None
            and _posix_process_group_exists(self._process_group_id)
        ):
            return None
        return exit_code

    def _process_tree_is_terminated_locked(self) -> bool:
        """在操作锁内确认根进程与其受管进程树均已结束。"""

        return _background_process_tree_has_exited(
            self._proc,
            job_handle=self._job_handle,
            job_assigned=self._job_assigned,
            process_group_id=self._process_group_id,
            tree_termination_sent=self._tree_termination_sent,
        )

    def _collect_output(self) -> None:
        """分块读取 stdout，并把增量文本交给非阻塞读取接口。"""

        stdout = self._stdout
        decoder = None
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
            # 输出读取器异常不能使后台进程或主流程崩溃。
            with self._output_lock:
                self._output_error = error
        finally:
            try:
                if decoder is not None:
                    self._append_pending_output(decoder.decode(b"", final=True))
            except Exception as error:
                with self._output_lock:
                    if self._output_error is None:
                        self._output_error = error
            finally:
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
                    excess -= len(oldest)
                else:
                    self._pending_output[0] = oldest[excess:]
                    self._pending_chars -= excess
                    excess = 0
                self._pending_output_truncated = True

    def _has_pending_output(self) -> bool:
        """在不消费输出的前提下查询是否已有待取日志。"""

        with self._output_lock:
            return bool(self._pending_output)

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
                snapshot_copy = self._copy_background_snapshot_locked()
                cwd_shell = self._cwd_to_shell(self.cwd)
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
            proc = self._run_background_bash(
                wrapped,
                env=launch_env,
                wait_for_start_gate=startup_gate_path is not None,
            )
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            if sys.platform == "win32":
                _assign_windows_job(proc, job_handle)
                job_assigned = True
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            handle = LocalBackgroundProcessHandle(
                proc,
                job_handle=job_handle,
                job_assigned=job_assigned,
                snapshot_path=snapshot_copy,
                startup_gate_path=startup_gate_path,
            )
            handle.start_output_reader()
            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            if startup_gate_path is not None:
                self._release_background_start_gate(startup_gate_path)
                self._close_background_gate_wait_pipe(proc)

            if self._cancel_requested(cancel_checker):
                raise BackgroundProcessCancelledError(
                    "Background process start cancelled"
                )
            return handle
        except BaseException as start_error:
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
                if handle is not None:
                    handoff_error = BackgroundProcessStartCleanupError(
                        (
                            "Background process start failed and cleanup "
                            "could not be confirmed"
                        ),
                        handle=handle,
                    )
                    handoff_error.add_note(
                        "Cleanup confirmation error: "
                        f"{type(cleanup_error).__name__}"
                    )
                    raise handoff_error from start_error
                raise cleanup_error from start_error
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
            # 根 Bash 在加入 Job 前不会启动用户命令或外部子进程，并通过私有管道限速轮询。
            parts.extend(
                [
                    (
                        f"while :; do IFS= read -r _hermes_gate < "
                        f"{quoted_gate} || _hermes_gate=; "
                        "[[ $_hermes_gate == ready ]] && break; "
                        "IFS= read -r -t 0.05 _hermes_gate_wait || :; done"
                    ),
                    f"rm -f {quoted_gate}",
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
        env: dict[str, str],
        wait_for_start_gate: bool = False,
    ) -> subprocess.Popen:
        """独立启动 Bash 和进程组，不复用前台 execute 流程。"""

        bash = _find_git_bash() if sys.platform == "win32" else "bash"
        process_group_options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        gate_wait_options = (
            {"stdin": subprocess.PIPE}
            if wait_for_start_gate
            else {}
        )
        return subprocess.Popen(
            [bash, "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            env=env,
            # 此管道只用于 Windows 启动闸门的 Bash 内建等待，不向用户暴露 stdin。
            **gate_wait_options,
            **process_group_options,
        )

    @staticmethod
    def _new_background_start_gate_path(snapshot_copy: Path) -> Path:
        """原子创建尚未放行的 Windows 后台启动闸门。"""

        while True:
            gate_path = snapshot_copy.with_name(
                f"hermes-background-gate-{uuid.uuid4().hex}.ready"
            )
            try:
                with gate_path.open("x", encoding="ascii") as gate_file:
                    gate_file.write("pending\n")
            except FileExistsError:
                continue
            else:
                return gate_path

    @staticmethod
    def _release_background_start_gate(startup_gate_path: Path) -> None:
        """仅在根 Bash 加入 Job 后放行其执行用户命令。"""

        startup_gate_path.write_text("ready\n", encoding="ascii")

    @staticmethod
    def _close_background_gate_wait_pipe(proc: subprocess.Popen) -> None:
        """关闭仅供启动闸门限速等待的私有 stdin 管道。"""

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
        tree_termination_sent: bool,
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
            tree_termination_sent=tree_termination_sent,
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

        LocalBackend._close_background_gate_wait_pipe(proc)
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

        if proc is None:
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
        tree_termination_sent = False

        if not self._background_process_tree_has_exited(
            proc,
            handle=handle,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
            tree_termination_sent=tree_termination_sent,
        ):
            try:
                tree_termination_sent = (
                    self._force_terminate_background_tree(
                        proc,
                        handle=handle,
                        job_handle=job_handle,
                        job_assigned=job_assigned,
                        process_group_id=process_group_id,
                    )
                    or tree_termination_sent
                )
            except BaseException:
                pass
            self._wait_for_background_process_exit(
                proc,
                _BACKGROUND_DISPOSE_WAIT_SECONDS,
                handle=handle,
            )

        if not self._background_process_tree_has_exited(
            proc,
            handle=handle,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
            tree_termination_sent=tree_termination_sent,
        ):
            try:
                tree_termination_sent = (
                    self._force_terminate_background_tree(
                        proc,
                        handle=handle,
                        job_handle=job_handle,
                        job_assigned=job_assigned,
                        process_group_id=process_group_id,
                    )
                    or tree_termination_sent
                )
            except BaseException:
                pass
            self._wait_for_background_process_exit(
                proc,
                _BACKGROUND_DISPOSE_RETRY_WAIT_SECONDS,
                handle=handle,
            )

        if not self._background_process_tree_has_exited(
            proc,
            handle=handle,
            job_handle=job_handle,
            job_assigned=job_assigned,
            process_group_id=process_group_id,
            tree_termination_sent=tree_termination_sent,
        ):
            raise BackgroundProcessCleanupError(
                "Could not confirm background process cleanup"
            )

        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
            return

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
