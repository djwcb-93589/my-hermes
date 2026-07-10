"""terminal 工具：审批检查 → backend.execute()。"""

from __future__ import annotations

import json

from hermes.backends import get_backend
from hermes.security import detect_dangerous_command, approve_command


def run_terminal(args, **kwargs):
    """terminal 工具处理函数：审批检查 → backend.execute()。

    每个 session_key 对应独立的 backend，cwd / 环境状态不会跨对话泄漏。
    session_key 由 run_conversation 从调用方的 session_id（CLI）或平台
    维度的 session_key（gateway）转发过来。未传时默认 "default"
    （例如 delegate.py 里的子代理不转发 session_key）。
    """
    command = args.get("command", "")

    matches = detect_dangerous_command(command)
    if matches and not approve_command(command, matches):
        return json.dumps({
            "ok": False,
            "error_type": "user_denied",
            "error": "Command denied by user.",
        })

    session_key = kwargs.get("session_key") or "default"
    backend = get_backend(session_key=session_key)
    result = backend.execute(command)

    output = result["output"].rstrip()
    if result["returncode"] != 0:
        output += f"\n(exit code: {result['returncode']})"

    body = output if output.strip() else "(no output)"
    return f"{body}\n(cwd: {backend.cwd})"


def register(registry):
    registry.register(
        name="terminal",
        toolset="terminal",
        schema={
            "name": "terminal",
            "description": (
                "Run a shell command via the active backend. "
                "On Windows the local backend uses Git Bash (MINGW/MSYS) — "
                "NOT PowerShell, CMD, or WSL. Always emit Bash/POSIX-compatible "
                "commands. Use forward-slash MSYS paths for absolute Windows "
                "locations (e.g. /d/my-project, not D:\\\\my-project). "
                "On Linux/macOS the local backend uses /bin/bash."
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
