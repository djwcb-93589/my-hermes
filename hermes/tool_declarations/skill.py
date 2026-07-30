"""Skill Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="skill_view",
        toolset="skill_read",
        schema={
            "name": "skill_view",
            "description": (
                "Load full content of a skill by name (frontmatter + body). "
                "Returns structured JSON with name/description/version/platforms/"
                "metadata/body fields plus risk and content-bound trust state. "
                "Skills live under skills/<name>/SKILL.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "skill name; must match [A-Za-z0-9_-]+",
                    },
                    "relative_path": {
                        "type": "string",
                        "description": "optional support file path under references/, templates/, scripts/, or assets/",
                    },
                },
                "required": ["name"],
            },
        },
        execution_environments=(
            "cli", "gateway", "cron", "delegate", "background_review",
        ),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="low",
    ),
    ToolDeclaration(
        name="skills_list",
        toolset="skill_read",
        schema={
            "name": "skills_list",
            "description": (
                "List all available skills with name, description, version, "
                "relative_path and metadata summary."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        execution_environments=(
            "cli", "gateway", "cron", "delegate", "background_review",
        ),
        default_enabled_environments=("cli", "cron"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="low",
    ),
    ToolDeclaration(
        name="skill_manage",
        toolset="skill_manage",
        schema={
            "name": "skill_manage",
            "description": (
                "Create / edit / delete / patch a skill, or write / remove an allowed support file. Names must match "
                "[A-Za-z0-9_-]+; path traversal is rejected. Writes are "
                "serialized via a per-skill operation lock and applied atomically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create", "edit", "delete", "patch", "write_file",
                            "remove_file",
                        ],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "version": {"type": "string"},
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "metadata": {"type": "object"},
                    "body": {
                        "type": "string",
                        "description": "create/edit: full Markdown body",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "patch: unique substring to find",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "patch: replacement text",
                    },
                    "relative_path": {
                        "type": "string",
                        "description": "write_file/remove_file: allowed support-file relative path",
                    },
                    "content": {
                        "type": "string",
                        "description": "write_file: full UTF-8 text content",
                    },
                    "expected_revision": {
                        "type": "string",
                        "description": "edit/patch: SKILL.md revision; write_file/remove_file: target support-file revision; omit for write_file creation",
                    },
                    "expected_governance_revision": {
                        "type": "string",
                        "description": "optional expected governance revision; required for background_review mutations",
                    },
                },
                "required": ["action", "name"],
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
