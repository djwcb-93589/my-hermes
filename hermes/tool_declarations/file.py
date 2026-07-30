"""File Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="file",
        toolset="file",
        schema={
            "name": "file",
            "description": (
                "IMPORTANT PATH RULE: every relative path resolves from the "
                "current session cwd, which is shared with and persisted by "
                "the terminal tool. After terminal changes directory, use a "
                "path relative to that new cwd; never prefix the cwd directory "
                "name again. Local file operations use host scope and may "
                "access paths outside the project when the operating system "
                "allows it. Prefer "
                "this tool over terminal for file content, directory listings, "
                "and metadata. Actions: "
                "read, read_range, write, append, replace, list, stat, "
                "pwd, context. "
                "Gateway remote sessions execute ordinary File operations "
                "without approval when the normalized path is neither blocked "
                "nor sensitive and does not match an approval_file_rules "
                "blacklist entry. Critical sensitive paths remain denied. "
                "Approval binds the complete arguments, normalized absolute "
                "path, and operation fingerprint. Do not retry an operation "
                "while approval is pending. "
                "replace, overwrite writes, and append operations also bind "
                "a file-state snapshot; execution returns "
                "error_type=approval_stale if the target changes first. "
                "Paths are relative to backend.cwd unless absolute. "
                "Paths blocked by the shared filesystem policy are always "
                "rejected with error_type=path_policy_denied before approval; "
                "do not try another tool to bypass that result. Critical "
                "hardline protected paths and configured File action/path "
                "deny rules are also evaluated before once/session grants. "
                "Structured File path checks are strong policy enforcement; "
                "they do not rely on Terminal command parsing. "
                "Paths matching configured sensitive file patterns are "
                "critical and denied rather than approvable. "
                "write defaults to no-overwrite; "
                "pass overwrite=true to replace (atomic via tmp + os.replace). "
                "Reads capped at 100KB; truncated=true means more data "
                "available. replace only supports UTF-8 files up to 100KB. "
                "Call again with offset for ranged reads. Docker/SSH backends "
                "stat returns mtime_utc and mtime_local with timezone data. "
                "All successful results include cwd, filesystem_scope, and "
                "the configured denied-path count without exposing paths. Docker/SSH "
                "backends return error_type=unsupported_backend for IO actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "read", "read_range", "write", "append",
                            "replace", "list", "stat", "pwd", "context",
                        ],
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative or absolute path; not required for "
                            "pwd/context. Relative paths start at the current "
                            "session cwd shared with terminal. After `cd work`, "
                            "use `report.md`, not `work/report.md`."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "for write/append",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "byte offset (read/read_range/replace)",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "max bytes to read",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "write: replace if exists",
                    },
                    "find": {
                        "type": "string",
                        "description": "replace: substring to find",
                    },
                    "replace": {
                        "type": "string",
                        "description": "replace: replacement text",
                    },
                    "all": {
                        "type": "boolean",
                        "default": True,
                        "description": "replace: replace all occurrences (default true)",
                    },
                },
                "required": ["action"],
            },
        },
        execution_environments=("cli", "gateway", "cron", "delegate"),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="interactive_or_remote",
        risk_level="high",
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
