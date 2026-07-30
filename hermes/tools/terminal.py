"""terminal 工具：审批检查 → backend.execute()。"""

from __future__ import annotations

import json

from hermes.approval import build_assessment_response, is_remote_approval
from hermes.backends import (
    INFRASTRUCTURE_CREDENTIAL_ENV_VARS,
    BackgroundProcessCancelledError,
    BackgroundProcessUnsupportedError,
    BaseExecutionEnvironment,
    get_backend,
)
from hermes.path_policy import ALLOW_ALL_PATH_POLICY, PathAccessDeniedError
from hermes.processes import (
    ProcessError,
    ProcessLimitError,
    ProcessStartError,
    ProcessStatus,
    ProcessTerminationError,
)
from hermes.redaction import redact_terminal_output
from hermes.terminal_path_preflight import preflight_terminal_command
from hermes.tool_declarations.terminal import TOOL_DECLARATIONS
from hermes.tools.terminal_approval import (
    assess_terminal_operation,
    assess_terminal_path_policy_denial,
    normalize_terminal_command,
    register_terminal_approval_handler,
)


def _background_error(
    *,
    error_type: str,
    error: str,
    cancelled: bool = False,
) -> str:
    """生成不包含命令、cwd 或 Backend 异常文本的后台启动错误。"""

    return json.dumps({
        "ok": False,
        "status": "cancelled" if cancelled else "failed",
        "output": "(cancelled)" if cancelled else "(background not started)",
        "error_type": error_type,
        "fatal": cancelled,
        "error": error,
    }, ensure_ascii=False)


def _background_start_cancelled(cancel_checker) -> bool:
    """尽力读取启动取消状态；检查器异常按未取消处理。"""

    if not callable(cancel_checker):
        return False
    try:
        return bool(cancel_checker())
    except Exception:
        return False


def _backend_supports_background(backend) -> bool:
    """通过公共 Backend 覆盖点判断是否实现了后台启动。"""

    spawner = getattr(backend, "spawn_background", None)
    if not callable(spawner):
        return False
    return (
        getattr(spawner, "__func__", None)
        is not BaseExecutionEnvironment.spawn_background
    )


def _run_terminal_background(
    *,
    command: str,
    backend,
    session_key: str,
    process_manager,
    cancel_checker,
) -> str:
    """只通过 ProcessManager 登记后台进程，并返回脱敏启动结果。"""

    if process_manager is None or not callable(
        getattr(process_manager, "spawn", None)
    ):
        return _background_error(
            error_type="process_manager_unavailable",
            error="Background process manager is unavailable.",
        )
    if not _backend_supports_background(backend):
        return _background_error(
            error_type="unsupported_backend",
            error="Current backend does not support background processes.",
        )
    if _background_start_cancelled(cancel_checker):
        return _background_error(
            error_type="cancelled",
            error="Background process start cancelled.",
            cancelled=True,
        )

    starter_failure: str | None = None

    def starter():
        """记录稳定的 Backend 失败类别，实际清理由 ProcessManager 负责。"""

        nonlocal starter_failure
        try:
            if callable(cancel_checker):
                return backend.spawn_background(
                    command,
                    cancel_checker=cancel_checker,
                )
            return backend.spawn_background(command)
        except BackgroundProcessUnsupportedError:
            starter_failure = "unsupported"
            raise
        except BackgroundProcessCancelledError:
            starter_failure = "cancelled"
            raise

    try:
        snapshot = process_manager.spawn(
            session_key=session_key,
            command=command,
            backend_type=str(
                getattr(backend, "backend_type", "unknown") or "unknown"
            ),
            cwd=str(getattr(backend, "cwd", "") or ""),
            starter=starter,
        )
    except BackgroundProcessUnsupportedError:
        return _background_error(
            error_type="unsupported_backend",
            error="Current backend does not support background processes.",
        )
    except BackgroundProcessCancelledError:
        return _background_error(
            error_type="cancelled",
            error="Background process start cancelled.",
            cancelled=True,
        )
    except ProcessLimitError:
        return _background_error(
            error_type="process_limit",
            error="Active background process limit reached.",
        )
    except ProcessStartError:
        if starter_failure == "unsupported":
            return _background_error(
                error_type="unsupported_backend",
                error=(
                    "Current backend does not support background processes."
                ),
            )
        if starter_failure == "cancelled":
            return _background_error(
                error_type="cancelled",
                error="Background process start cancelled.",
                cancelled=True,
            )
        return _background_error(
            error_type="process_start_failed",
            error="Background process could not be started.",
        )
    except ProcessTerminationError:
        return _background_error(
            error_type="process_termination_failed",
            error="Background process cleanup could not be confirmed.",
        )
    except ProcessError:
        return _background_error(
            error_type="process_error",
            error="Background process operation failed.",
        )
    except Exception:
        return _background_error(
            error_type="process_start_failed",
            error="Background process could not be started.",
        )

    status = snapshot.status
    if not isinstance(status, ProcessStatus):
        return json.dumps({
            "ok": False,
            "error_type": "background_start_incomplete",
            "error": "Background process registration did not complete",
        }, ensure_ascii=False)
    common = {
        "status": status.value,
        "process_id": snapshot.process_id,
        "pid": snapshot.pid,
    }
    if status is ProcessStatus.RUNNING:
        return json.dumps({
            "ok": True,
            **common,
            "output": "Background process started",
            "error": None,
        }, ensure_ascii=False)
    if status is ProcessStatus.EXITED:
        return json.dumps({
            "ok": True,
            **common,
            "exit_code": snapshot.exit_code,
            "command_succeeded": snapshot.exit_code == 0,
            "output": "Background process started and already exited",
            "error": None,
        }, ensure_ascii=False)
    if status is ProcessStatus.KILLED:
        return json.dumps({
            "ok": False,
            "error_type": "background_start_interrupted",
            **common,
            "error": "Background process was terminated during startup",
        }, ensure_ascii=False)
    if status is ProcessStatus.LOST:
        return json.dumps({
            "ok": False,
            "error_type": "background_start_lost",
            **common,
            "error": "Background process state could not be confirmed",
        }, ensure_ascii=False)
    if status is ProcessStatus.FAILED_START:
        return json.dumps({
            "ok": False,
            "error_type": "background_start_failed",
            **common,
            "error": "Background process failed to start",
        }, ensure_ascii=False)
    return json.dumps({
        "ok": False,
        "error_type": "background_start_incomplete",
        "error": "Background process registration did not complete",
    }, ensure_ascii=False)


def run_terminal(args, *, process_manager=None, **kwargs):
    """terminal 工具处理函数：审批检查 → backend.execute()。

    每个 session_key 对应独立的 backend，cwd / 环境状态不会跨对话泄漏。
    session_key 由 run_conversation 从调用方的 session_id（CLI）或平台
    会话 conversation_id（gateway）转发过来；Delegate 使用独立的
    child_session_key。仅前台直接嵌入式调用未传时兼容回退到 "default"。
    """
    if any(field in args for field in ("approval_grant", "session_grant")):
        return json.dumps({
            "ok": False,
            "error_type": "invalid_args",
            "error": "unexpected internal-only argument",
        }, ensure_ascii=False)
    background = args.get("background", False)
    if not isinstance(background, bool):
        return json.dumps({
            "ok": False,
            "error_type": "invalid_args",
            "error": "background must be a boolean",
        }, ensure_ascii=False)

    raw_session_key = kwargs.get("session_key")
    if background:
        if not isinstance(raw_session_key, str) or not raw_session_key.strip():
            return _background_error(
                error_type="session_unavailable",
                error="Background process requires a valid session.",
            )
        session_key = raw_session_key
    else:
        session_key = raw_session_key or "default"
    try:
        backend = get_backend(session_key=session_key)
    except Exception:
        if background:
            return _background_error(
                error_type="backend_unavailable",
                error="Background process backend is unavailable.",
            )
        raise
    try:
        command = normalize_terminal_command(args.get("command", ""))
    except ValueError as exc:
        return json.dumps({
            "ok": False,
            "error_type": "invalid_args",
            "error": str(exc),
        }, ensure_ascii=False)

    cron_guard = kwargs.get("cron_capability_guard")
    if cron_guard is not None:
        denial = cron_guard.authorize_terminal(command, cwd=backend.cwd)
        if denial is not None:
            return json.dumps(denial, ensure_ascii=False)

    path_policy = getattr(
        backend,
        "path_policy",
        ALLOW_ALL_PATH_POLICY,
    )

    # Local Terminal 的路径检查是审批前尽力预检，不是不可绕过的沙箱。
    if getattr(backend, "terminal_path_preflight_enabled", False):
        try:
            preflight_terminal_command(
                command,
                cwd=backend.cwd,
                path_policy=path_policy,
            )
        except PathAccessDeniedError:
            return build_assessment_response(
                assess_terminal_path_policy_denial(session_key=session_key),
                "执行 Terminal 命令",
            )

    try:
        if getattr(backend, "terminal_path_preflight_enabled", False):
            normalized_cwd = path_policy.normalize_path(
                backend.cwd,
                cwd=backend.cwd,
            )
        else:
            # 远端 backend 的 cwd 属于远端命令语义，不按 host 路径解释。
            normalized_cwd = str(backend.cwd or "").strip()
        approval_args = args
        if not background and "background" in args:
            # 显式 false 与历史省略参数使用同一前台审批身份。
            approval_args = dict(args)
            approval_args.pop("background", None)
        assessment = assess_terminal_operation(
            approval_args,
            normalized_cwd=normalized_cwd,
            session_key=session_key,
            remote_approval=is_remote_approval(kwargs),
            interactive_approval=(
                kwargs.get("interactive_approval", True) is not False
            ),
            approval_grant=kwargs.get("approval_grant"),
            security_policy=backend.tool_approval_policy,
            backend_context=backend.approval_risk_context(),
            intelligent_advisor=backend.intelligent_approval_advisor,
        )
    except ValueError as exc:
        return json.dumps({
            "ok": False,
            "error_type": "invalid_args",
            "error": str(exc),
        }, ensure_ascii=False)

    policy_response = build_assessment_response(
        assessment,
        "执行 Terminal 命令",
    )
    if policy_response is not None:
        return policy_response

    command = assessment.normalized_command or command

    cancel_checker = kwargs.get("cancel_checker")
    if background:
        return _run_terminal_background(
            command=command,
            backend=backend,
            session_key=session_key,
            process_manager=process_manager,
            cancel_checker=cancel_checker,
        )

    if callable(cancel_checker):
        result = backend.execute(command, cancel_checker=cancel_checker)
    else:
        result = backend.execute(command)

    if result.get("cancelled"):
        return json.dumps({
            "ok": False,
            "command_succeeded": False,
            "error_type": "cancelled",
            "fatal": True,
            "error": "Command cancelled by user.",
            "output": "(cancelled)",
            "exit_code": 130,
            "cwd": backend.cwd,
            "cwd_persisted": True,
            "environment_persisted": True,
        }, ensure_ascii=False)

    # Local Terminal 不是沙箱。输出脱敏只能减少凭证进入模型上下文，
    # 不能阻止子进程自己读取数据或通过网络外传。
    output = redact_terminal_output(
        result["output"].rstrip(),
        command,
        infrastructure_env_names=INFRASTRUCTURE_CREDENTIAL_ENV_VARS,
    )
    return json.dumps({
        "ok": True,
        "command_succeeded": result["returncode"] == 0,
        "output": output if output.strip() else "(no output)",
        "exit_code": result["returncode"],
        "cwd": backend.cwd,
        "cwd_persisted": True,
        "environment_persisted": True,
    }, ensure_ascii=False)


def register(registry, *, process_manager=None):
    """注册 Terminal 的运行时 handler 和审批处理器。"""

    active_process_manager = process_manager
    if active_process_manager is None:
        from hermes.processes import (
            process_manager as default_process_manager,
        )

        active_process_manager = default_process_manager
    if not callable(getattr(active_process_manager, "spawn", None)):
        raise TypeError("process_manager must provide spawn()")

    def handler(args, **kwargs):
        """绑定应用级共享 ProcessManager，拒绝运行时上下文覆盖。"""

        kwargs.pop("process_manager", None)
        return run_terminal(
            args,
            process_manager=active_process_manager,
            **kwargs,
        )

    register_terminal_approval_handler()
    registry.register_declaration(TOOL_DECLARATIONS[0], handler)
