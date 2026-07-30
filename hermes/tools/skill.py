"""Skill 工具适配层：兼容接口、JSON 响应和工具注册。"""

from __future__ import annotations

import json

from hermes.config import HERMES_HOME
from hermes.tool_declarations.skill import TOOL_DECLARATIONS
from hermes.tools import register_declared_handlers


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
    if actor == "background_review":
        required_options = {
            "edit": ("expected_revision", "expected_governance_revision"),
            "patch": ("expected_revision", "expected_governance_revision"),
            "write_file": ("expected_governance_revision",),
            "remove_file": ("expected_revision", "expected_governance_revision"),
        }
        for key in required_options.get(action, ()):
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
    register_declared_handlers(
        registry,
        TOOL_DECLARATIONS,
        {
            "skill_view": handle_skill_view,
            "skills_list": handle_skill_list,
            "skill_manage": handle_skill_manage,
        },
    )
