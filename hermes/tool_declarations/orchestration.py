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
                "with isolated workers. Define agents first; every agent has "
                "only a name and clear, non-overlapping responsibility "
                "instructions, and every task role must exactly reference a "
                "defined agent name. One definition may be used by multiple "
                "tasks, but every task run still has an independent session "
                "and no shared conversation memory. Agent definitions are "
                "per-call responsibility templates, not persistent profiles, "
                "and are not automatically recoverable after restart. "
                "Workers cannot create agents, call Delegate or "
                "orchestration_run, claim tasks, or modify workflow state. "
                "Worker tools, models, iterations, approval policy, and other "
                "execution settings are fixed by the application and cannot "
                "be configured here; current workers receive only low-risk "
                "skill_read tools. Tasks may form a pipeline, fan-out/fan-in, "
                "or mixed DAG. A final aggregation task should depend on every "
                "upstream result it must combine. Use result_task_key when the "
                "DAG has multiple sinks. The call waits for a stable stop and "
                "is not retry-safe after an unknown crash outcome."
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
                    "agents": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 32,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 128,
                                },
                                "instructions": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 100_000,
                                },
                            },
                            "required": ["name", "instructions"],
                        },
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
                "required": ["title", "goal", "agents", "tasks"],
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
