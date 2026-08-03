"""把 Claude Code 运行端口映射到现有 Backend 与 ProcessManager。"""

from __future__ import annotations

import math
import os
import shlex
import shutil
from collections.abc import Callable
from dataclasses import replace

from hermes.backends import (
    INFRASTRUCTURE_CREDENTIAL_ENV_VARS,
    BackgroundProcessCancelledError,
    BackgroundProcessUnsupportedError,
    BackgroundPtyDependencyUnavailableError,
    BackgroundPtyStartError,
    BackgroundPtyUnsupportedError,
    get_backend,
)
from hermes.claude_code.contracts import (
    CLAUDE_CODE_ACTIVE_PROCESS_STATUSES,
    CLAUDE_CODE_PROCESS_STATUSES,
    ClaudeCodeProcessLog,
    ClaudeCodeProcessSnapshot,
    ClaudeCodeRuntimeError,
)
from hermes.path_policy import ALLOW_ALL_PATH_POLICY, PathAccessDeniedError
from hermes.path_utils import windows_to_git_bash_path
from hermes.processes import (
    ProcessError,
    ProcessInputDeliveryError,
    ProcessInputError,
    ProcessInputUnavailableError,
    ProcessLimitError,
    ProcessNotFoundError,
    ProcessStartError,
    ProcessTerminationError,
    ProcessWaitCancelled,
)
from hermes.redaction import redact_terminal_output


_MAX_READ_CHARS = 20_000
_MAX_WAIT_SECONDS = 300.0
_MAX_KILL_GRACE_SECONDS = 30.0
_CTRL_C = "\x03"


class ProcessManagerClaudeCodePort:
    """只调用公共接口的 ProcessManager Adapter。"""

    def __init__(
        self,
        process_manager,
        *,
        backend_provider: Callable[[str], object] = get_backend,
    ) -> None:
        if process_manager is None:
            raise TypeError("process_manager is required")
        if not callable(backend_provider):
            raise TypeError("backend_provider must be callable")
        self._process_manager = process_manager
        self._backend_provider = backend_provider

    def preflight_start(
        self,
        *,
        session_owner: str,
        cwd: str,
        executable: str,
    ) -> str:
        """检查 LocalBackend、PathPolicy、CLI 与 Manager 公共能力。"""

        _, normalized_cwd, _ = self._prepare_start(
            session_owner=session_owner,
            cwd=cwd,
            executable=executable,
        )
        return normalized_cwd

    def start(
        self,
        *,
        session_owner: str,
        cwd: str,
        executable: str,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeProcessSnapshot:
        """通过 LocalBackend 后台 PTY 与 ProcessManager 建立真实进程。"""

        if cancel_checker is not None and not callable(cancel_checker):
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "Claude Code start cancellation check is invalid",
            )
        backend, normalized_cwd, command = self._prepare_start(
            session_owner=session_owner,
            cwd=cwd,
            executable=executable,
        )
        starter_failure: str | None = None

        def starter():
            """仅启动 PTY；注册、reader、cursor 和清理由 Manager 拥有。"""

            nonlocal starter_failure
            try:
                return backend.spawn_background(
                    command,
                    cancel_checker=cancel_checker,
                    pty=True,
                    cwd=normalized_cwd,
                )
            except (
                BackgroundPtyDependencyUnavailableError,
                BackgroundPtyUnsupportedError,
                BackgroundProcessUnsupportedError,
            ):
                starter_failure = "pty_unavailable"
                raise
            except BackgroundPtyStartError:
                starter_failure = "process_start_failed"
                raise
            except BackgroundProcessCancelledError:
                starter_failure = "process_start_failed"
                raise

        try:
            snapshot = self._process_manager.spawn(
                session_key=session_owner,
                command=command,
                backend_type="local",
                cwd=normalized_cwd,
                starter=starter,
            )
        except ProcessLimitError as exc:
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "Claude Code could not start because the process limit was reached",
                retryable=True,
            ) from exc
        except ProcessStartError as exc:
            if starter_failure == "pty_unavailable":
                raise ClaudeCodeRuntimeError(
                    "pty_unavailable",
                    "Background PTY support is unavailable for Claude Code",
                ) from exc
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "Claude Code background PTY could not be started",
            ) from exc
        except ProcessTerminationError as exc:
            raise ClaudeCodeRuntimeError(
                "session_registration_failed",
                "Claude Code start cleanup could not be confirmed",
            ) from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "Claude Code background PTY could not be registered",
            ) from exc

        process_id = self._registered_process_id(snapshot)
        try:
            converted = self._convert_snapshot(
                snapshot,
                error_type="process_start_failed",
            )
            if converted.status in {"killed", "lost", "failed_start"}:
                raise ClaudeCodeRuntimeError(
                    "process_start_failed",
                    "Claude Code did not enter a usable managed process state",
                )
            if converted.terminal_mode != "pty":
                raise ClaudeCodeRuntimeError(
                    "pty_unavailable",
                    "Claude Code did not start with a PTY",
                )

            path_policy = getattr(
                backend,
                "path_policy",
                ALLOW_ALL_PATH_POLICY,
            )
            try:
                actual_cwd = path_policy.normalize_path(
                    converted.cwd,
                    cwd=normalized_cwd,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise ClaudeCodeRuntimeError(
                    "process_start_failed",
                    "Claude Code start directory could not be confirmed",
                ) from exc
            if actual_cwd != normalized_cwd:
                raise ClaudeCodeRuntimeError(
                    "process_start_failed",
                    "Claude Code started outside the requested directory",
                )
            return replace(converted, cwd=normalized_cwd)
        except ClaudeCodeRuntimeError:
            self._cleanup_post_spawn_failure(session_owner, process_id)
            raise
        except Exception as exc:
            self._cleanup_post_spawn_failure(session_owner, process_id)
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "Claude Code post-start validation failed",
            ) from exc

    def read(
        self,
        *,
        session_owner: str,
        process_id: str,
        cursor: int,
        limit: int,
    ) -> ClaudeCodeProcessLog:
        """读取一页公开脱敏日志，不改写绝对 cursor。"""

        page, _ = self._read_page(
            session_owner=session_owner,
            process_id=process_id,
            cursor=cursor,
            limit=limit,
        )
        return page

    def _read_for_observation(
        self,
        *,
        session_owner: str,
        process_id: str,
        cursor: int,
        limit: int,
    ) -> tuple[ClaudeCodeProcessLog, str]:
        """只供 Runtime 单次观察使用的原生临时副本，不进入公开日志契约。"""

        return self._read_page(
            session_owner=session_owner,
            process_id=process_id,
            cursor=cursor,
            limit=limit,
        )

    def _read_page(
        self,
        *,
        session_owner: str,
        process_id: str,
        cursor: int,
        limit: int,
    ) -> tuple[ClaudeCodeProcessLog, str]:
        """从同一次 Manager 读取构造公开副本与观察期临时文本。"""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_READ_CHARS
        ):
            raise ClaudeCodeRuntimeError(
                "read_failed",
                "Claude Code read limit is invalid",
            )
        try:
            snapshot = self._process_manager.poll(session_owner, process_id)
            result = self._process_manager.log(
                session_owner,
                process_id,
                cursor=cursor,
                limit=limit,
            )
        except ProcessNotFoundError as exc:
            raise self._session_not_found() from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "read_failed",
                "Claude Code output could not be read",
                retryable=True,
            ) from exc

        try:
            status = self._status_value(getattr(result, "status", None))
            interaction_output = getattr(result, "output", "")
            if not isinstance(interaction_output, str):
                raise TypeError("process output must be text")
            output = redact_terminal_output(
                interaction_output,
                getattr(snapshot, "command", ""),
                infrastructure_env_names=INFRASTRUCTURE_CREDENTIAL_ENV_VARS,
            )
            return (
                ClaudeCodeProcessLog(
                    process_id=result.process_id,
                    status=status,
                    output=output,
                    requested_cursor=result.requested_cursor,
                    available_from_cursor=result.available_from_cursor,
                    next_cursor=result.next_cursor,
                    output_truncated=result.output_truncated,
                    exit_code=result.exit_code,
                ),
                interaction_output,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaudeCodeRuntimeError(
                "read_failed",
                "Claude Code output metadata is invalid",
            ) from exc

    def write(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """映射到 write_stdin，不追加 Enter，也不保留输入历史。"""

        return self._send_input(
            operation="write",
            session_owner=session_owner,
            process_id=process_id,
            data=data,
        )

    def submit(
        self,
        *,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """映射到 submit_stdin，由现有 PTY transport 提交 Enter。"""

        return self._send_input(
            operation="submit",
            session_owner=session_owner,
            process_id=process_id,
            data=data,
        )

    def status(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> ClaudeCodeProcessSnapshot:
        """只返回 ProcessManager 可确认的原始生命周期状态。"""

        try:
            snapshot = self._process_manager.poll(session_owner, process_id)
        except ProcessNotFoundError as exc:
            raise self._session_not_found() from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "status_failed",
                "Claude Code process status could not be read",
                retryable=True,
            ) from exc
        return self._convert_snapshot(snapshot, error_type="status_failed")

    def wait(
        self,
        *,
        session_owner: str,
        process_id: str,
        timeout: float,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> ClaudeCodeProcessSnapshot:
        """映射到 ProcessManager 有界等待，禁止 None 无限等待。"""

        timeout = self._bounded_seconds(
            timeout,
            maximum=_MAX_WAIT_SECONDS,
            error_type="wait_failed",
        )
        try:
            snapshot = self._process_manager.wait(
                session_owner,
                process_id,
                timeout=timeout,
                cancel_checker=cancel_checker,
            )
        except ProcessNotFoundError as exc:
            raise self._session_not_found() from exc
        except ProcessWaitCancelled as exc:
            raise ClaudeCodeRuntimeError(
                "wait_failed",
                "Claude Code wait was cancelled",
            ) from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "wait_failed",
                "Claude Code process wait failed",
                retryable=True,
            ) from exc
        return self._convert_snapshot(snapshot, error_type="wait_failed")

    def interrupt(
        self,
        *,
        session_owner: str,
        process_id: str,
    ) -> int:
        """通过 Manager 的 owner-scoped 输入入口发送单个 Ctrl+C。"""

        try:
            return self._process_manager.write_stdin(
                session_owner,
                process_id,
                _CTRL_C,
            )
        except ProcessNotFoundError as exc:
            raise self._session_not_found() from exc
        except ProcessInputDeliveryError as exc:
            raise ClaudeCodeRuntimeError(
                "interrupt_failed",
                "Claude Code interrupt delivery could not be confirmed",
                retryable=False,
                delivery_unknown=exc.delivery_unknown,
            ) from exc
        except ProcessInputError as exc:
            raise ClaudeCodeRuntimeError(
                "interrupt_failed",
                "Claude Code could not be interrupted cooperatively",
            ) from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "interrupt_failed",
                "Claude Code interrupt failed",
            ) from exc

    def kill(
        self,
        *,
        session_owner: str,
        process_id: str,
        grace_seconds: float,
    ) -> ClaudeCodeProcessSnapshot:
        """把协作式中断、强杀和树清理完整交给 ProcessManager。"""

        grace_seconds = self._bounded_seconds(
            grace_seconds,
            maximum=_MAX_KILL_GRACE_SECONDS,
            error_type="kill_failed",
        )
        try:
            snapshot = self._process_manager.kill(
                session_owner,
                process_id,
                grace_seconds=grace_seconds,
            )
        except ProcessNotFoundError as exc:
            raise self._session_not_found() from exc
        except ProcessTerminationError as exc:
            raise ClaudeCodeRuntimeError(
                "kill_failed",
                "Claude Code termination could not be confirmed",
                retryable=True,
            ) from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "kill_failed",
                "Claude Code termination failed",
                retryable=True,
            ) from exc
        return self._convert_snapshot(snapshot, error_type="kill_failed")

    def _prepare_start(
        self,
        *,
        session_owner: str,
        cwd: str,
        executable: str,
    ) -> tuple[object, str, str]:
        """完成不启动进程的低层 preflight，并构造固定 CLI 命令。"""

        self._require_manager_capabilities()
        if not isinstance(session_owner, str) or not session_owner.strip():
            raise ClaudeCodeRuntimeError(
                "session_owner_mismatch",
                "Claude Code requires a valid owning session",
            )
        if not isinstance(cwd, str) or not cwd.strip():
            raise ClaudeCodeRuntimeError(
                "cwd_required",
                "Claude Code requires an explicit working directory",
            )
        try:
            backend = self._backend_provider(session_owner)
        except Exception as exc:
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "The current session backend is unavailable",
            ) from exc
        if getattr(backend, "backend_type", None) != "local" or not callable(
            getattr(backend, "spawn_background", None)
        ):
            raise ClaudeCodeRuntimeError(
                "pty_unavailable",
                "Claude Code managed PTY requires LocalBackend",
            )

        path_policy = getattr(backend, "path_policy", ALLOW_ALL_PATH_POLICY)
        try:
            normalized_cwd = path_policy.require_allowed(
                cwd,
                cwd=str(getattr(backend, "cwd", "") or os.getcwd()),
            )
        except (PathAccessDeniedError, OSError, RuntimeError, ValueError) as exc:
            raise ClaudeCodeRuntimeError(
                "cwd_not_allowed",
                "Claude Code working directory is not allowed",
            ) from exc
        if not os.path.isdir(normalized_cwd):
            raise ClaudeCodeRuntimeError(
                "cwd_not_allowed",
                "Claude Code working directory is not an accessible directory",
            )

        command = self._resolve_command(executable)
        return backend, normalized_cwd, command

    def _require_manager_capabilities(self) -> None:
        """只按公开方法确认启动、读取、等待和终止能力。"""

        required = (
            "spawn",
            "log",
            "poll",
            "wait",
            "kill",
            "cleanup_session",
        )
        if any(
            not callable(getattr(self._process_manager, name, None))
            for name in required
        ):
            raise ClaudeCodeRuntimeError(
                "process_start_failed",
                "ProcessManager lifecycle operations are unavailable",
            )
        input_operations = ("write_stdin", "submit_stdin")
        if any(
            not callable(getattr(self._process_manager, name, None))
            for name in input_operations
        ):
            raise ClaudeCodeRuntimeError(
                "process_input_unavailable",
                "ProcessManager input operations are unavailable",
            )

    @staticmethod
    def _resolve_command(executable: str) -> str:
        """只接受可被当前环境解析的单个 executable，不接受参数注入。"""

        if (
            not isinstance(executable, str)
            or not executable.strip()
            or any(character in executable for character in ("\0", "\r", "\n"))
        ):
            raise ClaudeCodeRuntimeError(
                "claude_not_found",
                "Claude Code executable is unavailable",
            )
        try:
            resolved = shutil.which(executable)
        except (OSError, ValueError) as exc:
            raise ClaudeCodeRuntimeError(
                "claude_not_found",
                "Claude Code executable is unavailable",
            ) from exc
        if resolved is None or not os.path.isfile(resolved):
            raise ClaudeCodeRuntimeError(
                "claude_not_found",
                "Claude Code executable is unavailable",
            )
        shell_path = (
            windows_to_git_bash_path(resolved) if os.name == "nt" else resolved
        )
        return f"{shlex.quote(shell_path)} --ax-screen-reader"

    def _send_input(
        self,
        *,
        operation: str,
        session_owner: str,
        process_id: str,
        data: str,
    ) -> int:
        """统一映射 write/submit，并保留 delivery unknown 事实。"""

        method = (
            self._process_manager.write_stdin
            if operation == "write"
            else self._process_manager.submit_stdin
        )
        try:
            return method(session_owner, process_id, data)
        except ProcessNotFoundError as exc:
            raise self._session_not_found() from exc
        except ProcessInputUnavailableError as exc:
            raise ClaudeCodeRuntimeError(
                "process_input_unavailable",
                "Claude Code process input is unavailable",
            ) from exc
        except ProcessInputDeliveryError as exc:
            raise ClaudeCodeRuntimeError(
                "write_failed",
                "Claude Code input delivery could not be confirmed",
                retryable=False,
                delivery_unknown=exc.delivery_unknown,
            ) from exc
        except ProcessInputError as exc:
            raise ClaudeCodeRuntimeError(
                "write_failed",
                "Claude Code input could not be delivered",
            ) from exc
        except ProcessError as exc:
            raise ClaudeCodeRuntimeError(
                "write_failed",
                "Claude Code input failed",
            ) from exc

    @staticmethod
    def _registered_process_id(snapshot) -> str:
        """在完整快照转换前保存 ProcessManager 分配的唯一身份。"""

        try:
            process_id = snapshot.process_id
        except Exception as exc:
            raise ClaudeCodeRuntimeError(
                "session_registration_failed",
                "Claude Code registered process identity is unavailable",
            ) from exc
        if not isinstance(process_id, str) or not process_id.strip():
            raise ClaudeCodeRuntimeError(
                "session_registration_failed",
                "Claude Code registered process identity is unavailable",
            )
        return process_id

    def _cleanup_post_spawn_failure(
        self,
        session_owner: str,
        process_id: str,
    ) -> None:
        """只收敛本次登记进程，并确认其不再处于 active 状态。"""

        try:
            current = self._process_manager.poll(session_owner, process_id)
            current_status = self._known_process_status(current)
        except Exception:
            # 无法确认状态时仍尝试按唯一 process id 终止，不扩大到整个 session。
            current_status = None
        if (
            current_status is not None
            and current_status not in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES
        ):
            return

        try:
            stopped = self._process_manager.kill(
                session_owner,
                process_id,
                grace_seconds=0.0,
            )
            stopped_status = self._known_process_status(stopped)
        except Exception as exc:
            raise ClaudeCodeRuntimeError(
                "session_registration_failed",
                "Claude Code post-start cleanup could not be confirmed",
            ) from exc
        if stopped_status in CLAUDE_CODE_ACTIVE_PROCESS_STATUSES:
            raise ClaudeCodeRuntimeError(
                "session_registration_failed",
                "Claude Code post-start cleanup remained active",
            )

    @staticmethod
    def _known_process_status(snapshot) -> str:
        """只读取清理确认所需的稳定 ProcessStatus。"""

        status = ProcessManagerClaudeCodePort._status_value(snapshot.status)
        if status not in CLAUDE_CODE_PROCESS_STATUSES:
            raise ValueError("process status is unsupported")
        return status

    @staticmethod
    def _convert_snapshot(snapshot, *, error_type: str) -> ClaudeCodeProcessSnapshot:
        """复制 ProcessManager 安全字段，不暴露 command、PID 或 Handle。"""

        try:
            terminal_mode_value = getattr(
                snapshot.terminal_mode,
                "value",
                snapshot.terminal_mode,
            )
            return ClaudeCodeProcessSnapshot(
                process_id=snapshot.process_id,
                status=ProcessManagerClaudeCodePort._status_value(snapshot.status),
                cwd=snapshot.cwd,
                terminal_mode=terminal_mode_value,
                exit_code=snapshot.exit_code,
                started_at=snapshot.started_at,
                finished_at=snapshot.finished_at,
                output_base_cursor=snapshot.output_base_cursor,
                output_end_cursor=snapshot.output_end_cursor,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ClaudeCodeRuntimeError(
                error_type,
                "Claude Code process metadata is invalid",
            ) from exc

    @staticmethod
    def _status_value(status: object) -> str:
        """读取 ProcessStatus 的稳定字符串值。"""

        value = getattr(status, "value", status)
        if not isinstance(value, str):
            raise ValueError("process status must be text")
        return value

    @staticmethod
    def _bounded_seconds(
        value: float,
        *,
        maximum: float,
        error_type: str,
    ) -> float:
        """拒绝无限、负数、bool 和超过公开上限的等待。"""

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or value > maximum
        ):
            raise ClaudeCodeRuntimeError(
                error_type,
                "Claude Code operation timeout is invalid",
            )
        return float(value)

    @staticmethod
    def _session_not_found() -> ClaudeCodeRuntimeError:
        """隐藏其他 session 的进程是否存在。"""

        return ClaudeCodeRuntimeError(
            "session_not_found",
            "Claude Code session was not found for the current owner",
        )


__all__ = ["ProcessManagerClaudeCodePort"]
