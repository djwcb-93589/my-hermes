"""Cron Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="cron",
        toolset="cron",
        schema={
            "name": "cron",
            "description": (
                "Manage Cron task lifecycle: create, list, get, update, pause, "
                "resume, run, delete, and history. Cron management is unavailable "
                "inside Cron execution. A create request must explicitly provide the "
                "minimum toolsets needed by the task: read-only file work normally "
                "uses only file; do not request terminal unless command execution is "
                "needed, and do not request delegate unless a child agent is needed. "
                "Cron terminal also requires a narrow terminal_allowed_executables "
                "allowlist in capability_spec. To deliver files to the conversation, "
                "set delivery_policy to \"text_and_files\" and "
                "capability_spec.allow_file_write to true; the sub-agent writes "
                "files to the artifact directory (shown in its system prompt) and "
                "Gateway delivers them as attachments when the run finishes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create", "list", "get", "update", "pause",
                            "resume", "run", "delete", "history",
                        ],
                    },
                    "job_id": {"type": "string"},
                    "name": {"type": "string"},
                    "schedule": {
                        "type": "string",
                        "description": (
                            "One-time delays use a duration such as '5m' or '2h'. "
                            "Recurring durations use 'every 5m'. Five-field calendar "
                            "expressions are recurring and require recurring=true."
                        ),
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": (
                            "Set true only for an intentional five-field recurring "
                            "calendar schedule; omit for one-time durations."
                        ),
                    },
                    "timezone": {"type": "string"},
                    "prompt": {"type": "string"},
                    "toolsets": {
                        "type": "array",
                        "description": "Required for create. Request only the minimum Cron toolsets required; never include cron.",
                        "items": {"type": "string"},
                    },
                    "skills": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Working directory for the Cron run. It also defines "
                            "the file access boundary: at run time the sub-agent "
                            "may only read or write paths inside this directory "
                            "(plus the system-managed artifact root). Any path "
                            "mentioned in the prompt must fall under this "
                            "directory; create and update reject a prompt that "
                            "references an absolute path outside workdir, even if "
                            "allow_file_write is true. On Windows, Git Bash "
                            "forms like /e/path and Windows forms like E:\\\\path "
                            "are both accepted and normalized to the same "
                            "absolute directory."
                        ),
                    },
                    "timeout": {"type": "number"},
                    "max_agent_iterations": {"type": "integer"},
                    "overlap_policy": {
                        "type": "string",
                        "enum": ["skip", "queue", "parallel"],
                    },
                    "misfire_policy": {
                        "type": "string",
                        "enum": ["skip", "run_once", "catch_up"],
                    },
                    "retry_policy": {"type": "object"},
                    "delivery_policy": {
                        "oneOf": [{"type": "string"}, {"type": "object"}],
                        "description": (
                            "Controls what Gateway sends to the conversation after "
                            "the run. String values: \"text\" (default, send only "
                            "the final text summary), \"text_and_files\" (send the "
                            "summary plus any files the sub-agent wrote to the "
                            "artifact directory as attachments), \"failure_only\" "
                            "(send only when the run fails), \"silent\" (send "
                            "nothing). Use \"text_and_files\" when the task must "
                            "deliver files."
                        ),
                    },
                    "artifact_policy": {"type": "object"},
                    "capability_spec": {
                        "type": "object",
                        "description": (
                            "Capability constraints for unattended execution. "
                            "Defaults below are conservative reminders of the safe "
                            "baseline; they are not recommendations to copy. Judge "
                            "each field against what the task actually needs to do "
                            "and override only the ones the task requires. "
                            "allow_file_write (default false): set to true when the "
                            "task must create, modify, or delete files via the file "
                            "tool; a write action without this stays denied at run "
                            "time. Set this true together with "
                            "delivery_policy=\"text_and_files\" when the task must "
                            "deliver files to the conversation. "
                            "terminal_allowed_executables is required when "
                            "toolsets includes terminal. terminal_allow_shell_operators, "
                            "terminal_allow_redirection, terminal_allow_background, and "
                            "terminal_allow_network default to false. "
                            "max_artifact_file_bytes and max_artifact_total_bytes "
                            "default to 20MB and 50MB. artifact_root is system-managed "
                            "and cannot be set here."
                        ),
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["action"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=("cli",),
        unattended_allowed=False,
        approval_mode="remote_once",
        risk_level="high",
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
