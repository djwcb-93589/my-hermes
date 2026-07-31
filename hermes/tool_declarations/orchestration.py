"""Orchestration Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="orchestration_run",
        toolset="orchestration",
        schema={
            "name": "orchestration_run",
            "description": (
                "Synchronously create and run one fixed, persistent task DAG "
                "with a small set of isolated worker roles. Tasks may form a "
                "pipeline, fan-out/fan-in, or mixed DAG. The call waits until "
                "the workflow completes, fails, blocks, is cancelled, or "
                "reaches a safe runner stop. This first version has no "
                "background manager, automatic restart recovery, public "
                "resume/unblock operation, dynamic task creation, workdir, "
                "or autonomous worker claiming. The call is not retry-safe: "
                "a crash may leave a persisted workflow that must not be "
                "blindly submitted again. Prefer a final synthesizer task and "
                "select it with result_task_key when the DAG has multiple "
                "sinks. Worker toolsets, models, leases, and internal IDs are "
                "fixed by the application and cannot be supplied here. The "
                "bundled roles currently receive only low-risk skill_read "
                "tools; they cannot use Terminal, File, Delegate, Cron, or "
                "memory-writing tools."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "goal": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100_000,
                    },
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 128,
                                },
                                "title": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 500,
                                },
                                "prompt": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 100_000,
                                },
                                "role": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 128,
                                    "enum": [
                                        "researcher",
                                        "engineer",
                                        "reviewer",
                                        "synthesizer",
                                    ],
                                },
                                "depends_on": {
                                    "type": "array",
                                    "maxItems": 32,
                                    "uniqueItems": True,
                                    "items": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 128,
                                    },
                                },
                                "priority": {
                                    "type": "integer",
                                    "minimum": -1_000_000,
                                    "maximum": 1_000_000,
                                },
                                "max_attempts": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 100,
                                },
                                "input_metadata": {
                                    "type": "object",
                                    "maxProperties": 256,
                                },
                            },
                            "required": [
                                "key",
                                "title",
                                "prompt",
                                "role",
                            ],
                        },
                    },
                    "result_task_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "max_concurrency": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
                "required": ["title", "goal", "tasks"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=("cli", "gateway"),
        unattended_allowed=False,
        approval_mode="none",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
        supports_cancellation=True,
        has_status_check=False,
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
