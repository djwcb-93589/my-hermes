"""terminal 工具：审批检查 → backend.execute()。"""

from __future__ import annotations

import json

from hermes.approval import (
    build_approval_required,
    has_approval_grant,
    is_cwd_only_terminal_command,
    is_remote_approval,
)
from hermes.backends import (
    INFRASTRUCTURE_CREDENTIAL_ENV_VARS,
    get_backend,
)
from hermes.redaction import redact_terminal_output
from hermes.security import detect_dangerous_command, approve_command


def run_terminal(args, **kwargs):
    """terminal 工具处理函数：审批检查 → backend.execute()。

    每个 session_key 对应独立的 backend，cwd / 环境状态不会跨对话泄漏。
    session_key 由 run_conversation 从调用方的 session_id（CLI）或平台
    维度的 session_key（gateway）转发过来。未传时默认 "default"
    （例如 delegate.py 里的子代理不转发 session_key）。
    """
    command = args.get("command", "")

    remote_approval = is_remote_approval(kwargs)
    approval_granted = has_approval_grant(kwargs, "terminal", args)
    session_key = kwargs.get("session_key") or "default"
    backend = None
    if (
        remote_approval
        and not approval_granted
        and not is_cwd_only_terminal_command(command)
    ):
        backend = get_backend(session_key=session_key)
        return build_approval_required(
            "terminal",
            "执行 Terminal 命令",
            details={"command": command, "cwd": backend.cwd},
        )

    matches = detect_dangerous_command(command)
    if (
        matches
        and not approval_granted
        and kwargs.get("interactive_approval", True) is False
    ):
        return json.dumps({
            "ok": False,
            "error_type": "safety_blocked",
            "fatal": True,
            "error": (
                "Dangerous commands require interactive server approval, "
                "which is unavailable for this session."
            ),
            "matches": [description for _, _, description in matches],
        }, ensure_ascii=False)
    if matches and not approval_granted and not approve_command(command, matches):
        return json.dumps({
            "ok": False,
            "error_type": "user_denied",
            "error": "Command denied by user.",
        })

    if backend is None:
        backend = get_backend(session_key=session_key)
    result = backend.execute(command)

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
    registry.register(
        name="terminal",
        toolset="terminal",
        schema={
            "name": "terminal",
            "description": (
                "Run a shell command via the active backend. "
                "The current directory and exported environment variables "
                "persist across terminal calls in the same session. Relative "
                "paths used by the file tool resolve from this same cwd. "
                "Use the file tool for file reads, writes, directory listings, "
                "and metadata; use terminal for shell commands and processes. "
                "Gateway remote sessions pause for user approval before every "
                "command except a strict standalone cd or pwd. Do not retry an "
                "operation while approval is pending. "
                "On Windows the local backend uses Git Bash (MINGW/MSYS) — "
                "NOT PowerShell, CMD, or WSL. Always emit Bash/POSIX-compatible "
                "commands. Use forward-slash MSYS paths for absolute Windows "
                "locations (e.g. /d/my-project, not D:\\\\my-project). "
                "On Linux/macOS the local backend uses /bin/bash. The result "
                "is JSON with output, exit_code, cwd, and session-state flags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
        handler=run_terminal,
    )
