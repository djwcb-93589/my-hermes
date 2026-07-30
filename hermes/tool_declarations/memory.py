"""Memory Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="memory",
        toolset="memory",
        schema={
            "name": "memory",
            "description": (
                "Manage persistent memory (MEMORY.md or USER.md). Entries are "
                "joined with § separators. Actions: "
                "add (dedup by strip), remove (unique substring match), "
                "replace (unique old_text match → content), read. Writes that "
                "would exceed the char limit are rejected and the file stays "
                "unchanged. Response includes used_chars / limit_chars for "
                "capacity tracking. On ambiguous match, up to 5 candidate "
                "entries are returned in `matches`. Content with invisible "
                "Unicode or credential/injection patterns is blocked. Writes "
                "are serialized per-file via a lock and applied atomically."
                " Storage directories are initialized automatically; if an "
                "operation fails, report the structured error instead of using "
                "terminal to create or repair memory paths. Successful reads "
                "and writes return entry_count; writes also return action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "replace", "read"],
                    },
                    "target": {
                        "type": "string",
                        "enum": ["memory", "user"],
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "add: new entry text; remove: substring to match; "
                            "replace: the replacement text. Whitespace is stripped."
                        ),
                    },
                    "old_text": {
                        "type": "string",
                        "description": "replace only: substring identifying the entry to replace. Whitespace is stripped.",
                    },
                },
                "required": ["action"],
            },
        },
        execution_environments=("cli", "gateway", "cron", "background_review"),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="medium",
    ),
)


__all__ = ["TOOL_DECLARATIONS"]
