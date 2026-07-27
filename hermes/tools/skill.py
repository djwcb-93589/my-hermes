"""Skill 工具适配层：兼容接口、JSON 响应和工具注册。"""

from __future__ import annotations

import json

from hermes.config import HERMES_HOME


# 保留此变量以兼容既有调用方和临时目录 monkeypatch。
SKILLS_DIR = HERMES_HOME / "skills"
_default_service: SkillService | None = None


def _service() -> SkillService:
    """使默认服务在每次调用时读取兼容层的目录覆盖。"""
    global _default_service
    if _default_service is None:
        from hermes.skills.service import SkillService

        _default_service = SkillService()
    _default_service.set_skills_dir(SKILLS_DIR)
    return _default_service


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def discover_skills() -> list[dict]:
    """兼容的 Skill 摘要发现接口。"""
    return _service().list_skills()["skills"]


def load_skill_body(name: str) -> dict:
    """兼容的结构化 Skill 内容加载接口。"""
    return _service().load_skill_body(name)


def render_skills_section() -> str | None:
    """兼容的 system prompt Skill 段落接口。"""
    return _service().render_skills_section()


def handle_skill_view(args, **kwargs):
    relative_path = args.get("relative_path")
    if relative_path:
        return _json(_service().read_support_file(
            args.get("name", ""),
            relative_path,
            actor=kwargs.get("skill_actor", "foreground"),
        ))
    return _json(_service().view_skill(
        args.get("name", ""),
        actor=kwargs.get("skill_actor", "foreground"),
        interactive_approval=kwargs.get("interactive_approval"),
    ))


def handle_skill_list(args, **kwargs):
    return _json(_service().list_skills(actor=kwargs.get("skill_actor", "foreground")))


def handle_skill_manage(args, **kwargs):
    actor = kwargs.get("skill_actor", "foreground")
    action = args.get("action", "")
    if actor == "background_review" and action not in {
        "create",
        "edit",
        "patch",
        "write_file",
        "remove_file",
    }:
        return _json({
            "ok": False,
            "error_type": "tool_not_authorized",
            "error": "background review cannot perform this skill action",
        })
    allowed_options = {
        "description",
        "version",
        "platforms",
        "metadata",
        "body",
        "old_text",
        "new_text",
        "relative_path",
        "content",
        "expected_revision",
        "expected_governance_revision",
    }
    options = {key: value for key, value in args.items() if key in allowed_options}
    for key in ("expected_revision", "expected_governance_revision"):
        if key in options and not isinstance(options[key], str):
            return _json({
                "ok": False,
                "error_type": "invalid_args",
                "error": f"{key} must be a string",
            })
    if actor == "background_review" and action in {
        "edit",
        "patch",
        "write_file",
        "remove_file",
    }:
        for key in ("expected_revision", "expected_governance_revision"):
            if not isinstance(options.get(key), str) or not options[key].strip():
                return _json({
                    "ok": False,
                    "error_type": "invalid_args",
                    "error": f"{key} is required for background review",
                })
    return _json(_service().manage_skill(
        action,
        args.get("name", ""),
        actor=actor,
        **options,
    ))


def register(registry):
    """注册 skill_view / skills_list / skill_manage 三个工具。"""
    registry.register(
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
        handler=handle_skill_view,
        execution_environments=("cli", "gateway", "cron", "delegate", "background_review"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="low",
        default_enabled_environments=("cli", "cron"),
    )
    registry.register(
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
        handler=handle_skill_list,
        execution_environments=("cli", "gateway", "cron", "delegate", "background_review"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="low",
        default_enabled_environments=("cli", "cron"),
    )
    registry.register(
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
                        "enum": ["create", "edit", "delete", "patch", "write_file", "remove_file"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "version": {"type": "string"},
                    "platforms": {"type": "array", "items": {"type": "string"}},
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
                        "description": "optional expected content revision",
                    },
                    "expected_governance_revision": {
                        "type": "string",
                        "description": "optional expected governance revision",
                    },
                },
                "required": ["action", "name"],
            },
        },
        handler=handle_skill_manage,
        execution_environments=("cli", "gateway", "cron", "background_review"),
        unattended_allowed=True,
        approval_mode="none",
        risk_level="medium",
        default_enabled_environments=("cli", "cron"),
    )
