"""Process Tool 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="process",
        toolset="terminal",
        schema={
            "name": "process",
            "description": (
                "Manage background processes started by "
                "terminal(background=true) in the current session. The "
                "process_id comes from the terminal result. list returns "
                "session-owned processes; poll performs a non-blocking status "
                "and log query; log reads output with an absolute cursor and "
                "the next call should pass the returned next_cursor; wait "
                "limits only this wait call and never terminates the process "
                "on timeout; kill first requests cooperative termination and "
                "forces termination only when needed; write sends text to "
                "stdin unchanged; submit sends transport-specific Enter; "
                "close delivers real EOF only for regular pipe processes and "
                "never kills the process. LocalBackend PTY processes support "
                "write and submit but intentionally reject close because PTY "
                "EOF is not implemented. PTY output is a raw append stream "
                "that may contain carriage returns, ANSI sequences, and input "
                "echo; it is not a virtual screen. Resize and reliable "
                "full-screen TUI control are not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "poll",
                            "log",
                            "wait",
                            "kill",
                            "write",
                            "submit",
                            "close",
                        ],
                    },
                    "process_id": {"type": "string"},
                    "data": {
                        "type": "string",
                        "maxLength": 65_536,
                        "description": (
                            "Text sent to stdin for write or submit. write "
                            "sends it unchanged; submit adds Enter according "
                            "to the process transport."
                        ),
                    },
                    "include_finished": {
                        "type": "boolean",
                        "default": True,
                    },
                    "cursor": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20_000,
                        "default": 20_000,
                    },
                    "timeout": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 300,
                        "default": 30,
                    },
                    "grace_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                        "default": 2,
                    },
                },
                "required": ["action"],
            },
        },
        execution_environments=("cli", "gateway", "cron", "delegate"),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="medium",
        retry_safe=False,
        unknown_on_crash=True,
        supports_cancellation=True,
        has_status_check=True,
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
