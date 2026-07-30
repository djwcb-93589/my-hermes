"""Delegate Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="delegate_task",
        toolset="delegate",
        schema={
            "name": "delegate_task",
            "description": (
                "Delegate a leaf sub-task to an isolated child agent. The "
                "child gets its own session_key (terminal / file backend "
                "isolated from the parent), cannot call delegate / memory / "
                "skill_manage / cron (no recursion, no persistent side "
                "effects across rounds), and only sees the goal + optional "
                "context. background=false (default) blocks until the child "
                "finishes and returns {ok, status, summary, iterations, "
                "tools_used, tool_batches, tool_call_count, "
                "child_session_key, error, error_type}. background=true "
                "submits a background job and immediately returns "
                "{ok, status='submitted', job_id, child_session_key}; poll "
                "with delegate_status / delegate_result / delegate_cancel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Task description for the child agent.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to inject into the child system prompt.",
                    },
                    "toolsets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Allowed child toolsets. Each item must be one "
                            "of {terminal, file, skill_read}. Unknown or "
                            "disallowed values (memory / delegate / cron) "
                            "cause invalid_args. Default "
                            "['terminal', 'file']."
                        ),
                    },
                    "background": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "false (default): synchronous — block until the "
                            "child finishes. true: submit a background job "
                            "and return immediately with job_id."
                        ),
                    },
                },
                "required": ["goal"],
            },
        },
        execution_environments=("cli", "gateway", "cron"),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="high",
        supports_cancellation=True,
    ),
    ToolDeclaration(
        name="delegate_status",
        toolset="delegate",
        schema={
            "name": "delegate_status",
            "description": (
                "Lightweight status probe for a background delegate job. "
                "Returns {ok, job_id, status ∈ queued|running|completed|"
                "failed|cancelled, child_status, cancel_requested, "
                "iterations, tools_used, error, timestamps}. Does NOT "
                "include summary (use delegate_result for that). Use this "
                "to check whether the job is still running or how it ended."
            ),
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=("cli",),
        unattended_allowed=False,
        approval_mode="none",
        risk_level="low",
    ),
    ToolDeclaration(
        name="delegate_result",
        toolset="delegate",
        schema={
            "name": "delegate_result",
            "description": (
                "Fetch the full result of a background delegate job. "
                "Non-blocking: if still queued/running, returns ok=false "
                "with error='Job is still running' and empty summary. If "
                "completed/failed/cancelled, returns ok (true only when "
                "status=completed), summary, iterations, tools_used, "
                "child_session_key, child_status, error."
            ),
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=("cli",),
        unattended_allowed=False,
        approval_mode="none",
        risk_level="low",
    ),
    ToolDeclaration(
        name="delegate_cancel",
        toolset="delegate",
        schema={
            "name": "delegate_cancel",
            "description": (
                "Cooperatively cancel a background delegate job. Sets the "
                "cancel_requested flag; the worker checks it at the next "
                "iteration boundary and exits with status=cancelled. Does "
                "not forcibly kill the worker thread."
            ),
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=("cli",),
        unattended_allowed=False,
        approval_mode="none",
        risk_level="medium",
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
