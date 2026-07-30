"""Messaging Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="gateway_send_file",
        toolset="messaging",
        schema={
            "name": "gateway_send_file",
            "description": (
                "Create an approved persistent task to send one local file "
                "to the current Gateway conversation. This tool is only "
                "available in Gateway sessions. Every call requires a once "
                "approval, and approval binds the file path, size, SHA-256, "
                "stable file state, and current platform target. The current "
                "stage creates a pending delivery only; it does not upload or "
                "send the file. Paths must pass the shared filesystem policy "
                "and gateway.file_transfer.outbound_allowed_roots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Local path of the regular file to send.",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Optional plain file name shown to the recipient.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        execution_environments=("gateway",),
        default_enabled_environments=(),
        unattended_allowed=False,
        required_trusted_context=("gateway_file_delivery",),
        approval_mode="remote_once",
        risk_level="high",
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
