"""只按本地固定规范启动并验证 Gateway 子进程。"""

from __future__ import annotations

import os
import signal
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from hermes.backend_control import (
    BackendProcessBinding,
    BackendProcessInspection,
    BackendProcessLaunch,
    BackendProcessState,
)

from .config import SupervisorConfig


class GatewayLaunchError(RuntimeError):
    """固定 Gateway 子进程无法安全启动或控制。"""


@dataclass(frozen=True, slots=True)
class GatewayLaunchSpec:
    """仅由本地 composition root 创建的固定 Gateway 启动规范。"""

    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.argv) is not tuple
            or len(self.argv) != 3
            or any(type(item) is not str or not item for item in self.argv)
            or self.argv[2] != "--gateway-unified"
        ):
            raise ValueError("gateway launch argv is invalid")
        if not isinstance(self.cwd, Path) or not self.cwd.is_absolute():
            raise ValueError("gateway launch cwd is invalid")
        if not isinstance(self.environment, Mapping):
            raise TypeError("gateway launch environment is invalid")
        copied = dict(self.environment)
        if any(type(key) is not str or type(value) is not str for key, value in copied.items()):
            raise ValueError("gateway launch environment is invalid")
        object.__setattr__(self, "environment", MappingProxyType(copied))


def gateway_launch_spec(config: SupervisorConfig) -> GatewayLaunchSpec:
    """生成不可由 Dashboard 覆盖的正式 Gateway 入口参数。"""
    if not isinstance(config, SupervisorConfig):
        raise TypeError("supervisor config is invalid")
    return GatewayLaunchSpec(
        argv=(
            config.python_executable,
            str(config.project_root / "main.py"),
            "--gateway-unified",
        ),
        cwd=config.project_root,
        environment=config.launch_environment,
    )


class LocalGatewayProcessLauncher:
    """持有 OS 进程对象，但只向中立边界返回 PID 与创建身份。"""

    __slots__ = ("_processes", "_session_tokens", "_spec")

    def __init__(self, spec: GatewayLaunchSpec) -> None:
        if not isinstance(spec, GatewayLaunchSpec):
            raise TypeError("gateway launch spec is invalid")
        self._spec = spec
        self._processes: dict[str, subprocess.Popen] = {}
        self._session_tokens: dict[int, str] = {}

    def launch(self, launch_id: str) -> BackendProcessLaunch:
        """使用结构化 argv；继承标准流，避免 PIPE 无消费者阻塞。"""
        try:
            normalized_launch_id = str(uuid.UUID(launch_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise GatewayLaunchError("gateway launch identity is invalid") from exc
        if normalized_launch_id != launch_id:
            raise GatewayLaunchError("gateway launch identity is invalid")
        options: dict[str, object] = {
            "cwd": str(self._spec.cwd),
            "env": dict(self._spec.environment),
            "shell": False,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(list(self._spec.argv), **options)
        except (OSError, ValueError) as exc:
            raise GatewayLaunchError("gateway process launch failed") from exc
        token = _process_identity_token(process.pid)
        if token is None:
            token = f"session:{launch_id}"
            self._session_tokens[process.pid] = token
        self._processes[launch_id] = process
        return BackendProcessLaunch(
            launch_id=launch_id,
            pid=process.pid,
            process_identity_token=token,
            started_at=datetime.now(UTC),
        )

    def inspect(self, binding: BackendProcessBinding) -> BackendProcessInspection:
        if not isinstance(binding, BackendProcessBinding) or binding.pid is None:
            return BackendProcessInspection(BackendProcessState.NOT_FOUND)
        launch_id = binding.launch_id or ""
        process = self._processes.get(launch_id)
        if process is not None and process.pid == binding.pid:
            exit_code = process.poll()
            if exit_code is not None:
                self._processes.pop(launch_id, None)
                self._session_tokens.pop(binding.pid, None)
                return BackendProcessInspection(
                    BackendProcessState.NOT_FOUND,
                    exit_code=int(exit_code),
                )
        current = _process_identity_token(binding.pid)
        if current is None:
            if not _pid_exists(binding.pid):
                return BackendProcessInspection(
                    BackendProcessState.NOT_FOUND,
                    exit_code=None if process is None else process.poll(),
                )
            session_token = self._session_tokens.get(binding.pid)
            if session_token == binding.process_identity_token:
                return BackendProcessInspection(BackendProcessState.MATCHED)
            return BackendProcessInspection(BackendProcessState.UNAVAILABLE)
        if current != binding.process_identity_token:
            return BackendProcessInspection(BackendProcessState.MISMATCHED)
        return BackendProcessInspection(BackendProcessState.MATCHED)

    def request_graceful_stop(self, binding: BackendProcessBinding) -> bool:
        if self.inspect(binding).state is not BackendProcessState.MATCHED:
            return False
        process = self._processes.get(binding.launch_id or "")
        try:
            if process is not None and process.pid == binding.pid:
                process.send_signal(
                    signal.CTRL_BREAK_EVENT
                    if os.name == "nt"
                    else signal.SIGTERM
                )
            elif os.name == "nt":
                return _windows_control_break(binding)
            else:
                return _posix_signal_verified(binding, signal.SIGTERM)
            return True
        except (OSError, ValueError):
            return False

    def terminate(self, binding: BackendProcessBinding) -> bool:
        if self.inspect(binding).state is not BackendProcessState.MATCHED:
            return False
        process = self._processes.get(binding.launch_id or "")
        try:
            if process is not None and process.pid == binding.pid:
                process.terminate()
            elif os.name == "nt":
                return _windows_terminate_verified(binding)
            else:
                return _posix_signal_verified(binding, signal.SIGTERM)
            return True
        except (OSError, ValueError):
            return False

    def kill(self, binding: BackendProcessBinding) -> bool:
        if self.inspect(binding).state is not BackendProcessState.MATCHED:
            return False
        process = self._processes.get(binding.launch_id or "")
        try:
            if process is not None and process.pid == binding.pid:
                process.kill()
            elif os.name == "nt":
                return _windows_terminate_verified(binding)
            else:
                return _posix_signal_verified(binding, signal.SIGKILL)
            return True
        except (OSError, ValueError):
            return False


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_identity_token(pid: int) -> str | None:
    if os.name == "nt":
        return _windows_process_identity(pid)
    proc_token = _linux_process_identity(pid)
    return proc_token


def _linux_process_identity(pid: int) -> str | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = stat_text.rfind(")")
        fields = stat_text[closing + 2:].split()
        if closing < 0 or len(fields) <= 19:
            return None
        start_ticks = fields[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
        if not start_ticks.isdigit() or not boot_id:
            return None
        return f"linux:{boot_id}:{start_ticks}"
    except (OSError, UnicodeError, ValueError):
        return None


def _windows_process_identity(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        creation = FILETIME()
        exit_time = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows:{value}"
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, ValueError):
        return None


def _posix_signal_verified(
    binding: BackendProcessBinding,
    signal_number: int,
) -> bool:
    """恢复后仅通过 pidfd 向身份仍匹配的进程发送信号。"""
    if (
        binding.pid is None
        or binding.process_identity_token is None
        or not hasattr(os, "pidfd_open")
        or not hasattr(signal, "pidfd_send_signal")
    ):
        return False
    descriptor = -1
    try:
        descriptor = os.pidfd_open(binding.pid, 0)
        if _linux_process_identity(binding.pid) != binding.process_identity_token:
            return False
        signal.pidfd_send_signal(descriptor, signal_number)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _windows_control_break(binding: BackendProcessBinding) -> bool:
    """持有已验证进程句柄期间向原进程组发送 CTRL_BREAK。"""
    opened = _windows_open_verified_process(binding, access=0x1000)
    if opened is None or binding.pid is None:
        return False
    kernel32, handle = opened
    try:
        os.kill(binding.pid, signal.CTRL_BREAK_EVENT)
        return True
    except (OSError, ValueError):
        return False
    finally:
        kernel32.CloseHandle(handle)


def _windows_terminate_verified(binding: BackendProcessBinding) -> bool:
    """使用完成身份核对的同一个 Windows 句柄终止进程。"""
    opened = _windows_open_verified_process(
        binding,
        access=0x0001 | 0x1000,
    )
    if opened is None:
        return False
    kernel32, handle = opened
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _windows_open_verified_process(
    binding: BackendProcessBinding,
    *,
    access: int,
) -> tuple[object, object] | None:
    try:
        import ctypes
        from ctypes import wintypes

        if binding.pid is None or binding.process_identity_token is None:
            return None

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(access, False, binding.pid)
        if not handle:
            return None
        creation = FILETIME()
        exit_time = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        verified = False
        try:
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            if f"windows:{value}" != binding.process_identity_token:
                return None
            verified = True
            return kernel32, handle
        finally:
            if not verified:
                kernel32.CloseHandle(handle)
    except Exception:
        # 平台 API 无法可靠使用时按身份不可验证处理，绝不降级为裸 PID。
        return None


__all__ = [
    "GatewayLaunchError",
    "GatewayLaunchSpec",
    "LocalGatewayProcessLauncher",
    "gateway_launch_spec",
]
