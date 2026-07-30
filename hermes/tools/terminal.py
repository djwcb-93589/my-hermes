"""terminal 工具：审批检查 → backend.execute()。"""

from __future__ import annotations

import json

from hermes.approval import build_assessment_response, is_remote_approval
from hermes.backends import INFRASTRUCTURE_CREDENTIAL_ENV_VARS, get_backend
from hermes.path_policy import ALLOW_ALL_PATH_POLICY, PathAccessDeniedError
from hermes.redaction import redact_terminal_output
from hermes.terminal_path_preflight import preflight_terminal_command
from hermes.tool_declarations.terminal import TOOL_DECLARATIONS
from hermes.tools.terminal_approval import (
    assess_terminal_operation,
    assess_terminal_path_policy_denial,
    normalize_terminal_command,
    register_terminal_approval_handler,
)


def run_terminal(args, **kwargs):
    """terminal 工具处理函数：审批检查 → backend.execute()。

    每个 session_key 对应独立的 backend，cwd / 环境状态不会跨对话泄漏。
    session_key 由 run_conversation 从调用方的 session_id（CLI）或平台
    会话 conversation_id（gateway）转发过来；Delegate 使用独立的
    child_session_key。仅直接嵌入式调用未传时兼容回退到 "default"。
    """
    if any(field in args for field in ("approval_grant", "session_grant")):
        return json.dumps({
            "ok": False,
            "error_type": "invalid_args",
            "error": "unexpected internal-only argument",
        }, ensure_ascii=False)
    session_key = kwargs.get("session_key") or "default"
    backend = get_backend(session_key=session_key)
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
        assessment = assess_terminal_operation(
            args,
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


def register(registry):
    """注册 Terminal 的运行时 handler 和审批处理器。"""
    register_terminal_approval_handler()
    registry.register_declaration(TOOL_DECLARATIONS[0], run_terminal)
