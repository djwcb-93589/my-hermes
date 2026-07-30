"""Terminal Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
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
                "Gateway remote sessions default to executing commands that do "
                "not match the configured approval blacklist. Destructive "
                "file removal, permission changes, external network access, "
                "deployment tooling, and any configured patterns require "
                "explicit approval. Do not retry an "
                "operation while approval is pending. Approval binds the "
                "normalized command, current cwd, session key, and operation "
                "fingerprint; cwd defines command semantics but is not an "
                "access boundary. "
                "Low/medium approvals may offer a constrained session grant "
                "that re-parses executable/argv and requires the same cwd; "
                "high risk is once-only and critical commands are denied. "
                "Hardline safety rules and configured command, executable, "
                "or protected-path deny rules run before every once/session "
                "grant and cannot be approved. Hardline coverage includes "
                "root or disk-root recursive deletion, filesystem formatting, "
                "raw device writes, fork bombs, critical security-service "
                "damage, and explicit attempts to modify Hermes approval "
                "configuration. "
                "Backend risk is explicit: local and ordinary unmounted Docker "
                "still use command risk, SSH and Docker host mounts require "
                "high-risk once approval, and Docker socket access is critical "
                "and denied. Docker is never auto-approved merely because it "
                "is named a sandbox. "
                "On Windows the local backend uses Git Bash (MINGW/MSYS) — "
                "NOT PowerShell, CMD, or WSL. Always emit Bash/POSIX-compatible "
                "commands. Use forward-slash MSYS paths for absolute Windows "
                "locations (e.g. /d/my-project, not D:\\\\my-project). "
                "On Linux/macOS the local backend uses /bin/bash. A foreground "
                "result is JSON with output, exit_code, cwd, and session-state "
                "flags. "
                "Set background=true for long-running commands that should be "
                "registered and return immediately without waiting for command "
                "completion. A successful background start returns a "
                "process_id that the process tool uses for status, logs, "
                "waiting, and termination. "
                "Local terminal path enforcement is best-effort and is not a "
                "sandbox. Commands that clearly reference a path blocked by "
                "the shared filesystem policy are rejected before approval, "
                "but complex dynamic scripts may not be detected. Do not try "
                "to bypass error_type=path_policy_denied through another tool. "
                "A Gateway /stop request interrupts the active local process "
                "group like Ctrl+C, then force-stops it if it does not exit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "background": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Start a long-running command in the background "
                            "and return immediately after registration. The "
                            "response includes a process_id for the process "
                            "management tool."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
        execution_environments=("cli", "gateway", "cron", "delegate"),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="interactive_or_remote",
        risk_level="high",
        supports_cancellation=True,
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
