"""受管 Claude Code Agent Tool 的默认关闭声明。"""

from hermes.claude_code.agent_adapter import (
    CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT,
)
from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="claude_code",
        toolset="claude_code",
        schema={
            "name": "claude_code",
            "description": (
                "Run a user-authorized managed Claude Code workflow through "
                "the existing Controller. Supported actions are start, poll, "
                "send_instruction, request_interrupt, and terminate. "
                "send_instruction requires a previously returned process_id "
                "and round_id plus a new explicit instruction. The tool does not expose "
                "session owners, notification targets, executable paths, "
                "permission bypass flags, raw PTY output, or native prompt "
                "reply operations. It is disabled unless a trusted current "
                "user invocation grant is supplied by the host."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "start",
                            "poll",
                            "send_instruction",
                            "request_interrupt",
                            "terminate",
                        ],
                    },
                    "cwd": {
                        "type": "string",
                        "maxLength": 4_096,
                    },
                    "task": {
                        "type": "string",
                        "maxLength": 65_535,
                    },
                    "process_id": {
                        "type": "string",
                        "maxLength": 512,
                    },
                    "round_id": {
                        "type": "string",
                        "maxLength": 512,
                    },
                    "instruction": {
                        "type": "string",
                        "maxLength": 65_535,
                    },
                },
                "required": ["action"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=(),
        unattended_allowed=False,
        required_trusted_context=(CLAUDE_CODE_REQUIRED_TRUSTED_CONTEXT,),
        approval_mode="interactive_or_remote",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
        supports_cancellation=True,
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
