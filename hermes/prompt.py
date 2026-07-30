"""System prompt assembly."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from hermes.config import HERMES_HOME
from hermes.tools.memory import render_memory_section
from hermes.tools.skill import render_skills_section


_CRON_ROUTING_GUIDANCE = (
    "# Scheduled Task Routing\n"
    "For any request to perform work in the future, after a delay, or on a "
    "recurring schedule, your first tool call must be cron. Do not call any "
    "other tool first.\n"
    "Do not perform or prepare the scheduled task body in the current "
    "conversation turn. This includes inspecting or discovering its runtime "
    "files or directories, calculating runtime-dependent dates, and creating, "
    "reading, modifying, copying, or otherwise staging its outputs.\n"
    "Put runtime discovery, file-selection criteria, and date calculations in "
    "the cron prompt so they happen when the job runs. Build the schedule, "
    "workdir, and least-privilege capabilities directly from the user's "
    "request when it is safe to do so. Ask a clarifying question only when a "
    "safe schedule or capability scope cannot be constructed from the "
    "available information.\n"
    "For a one-time relative delay, pass a duration schedule directly: use "
    "`5m`, `2h`, or `1d`. Never convert a relative delay into a five-field "
    "calendar cron expression, because that expression repeats. Use `every "
    "5m` or a five-field expression only when the user explicitly requests "
    "recurrence. A five-field expression requires `recurring: true` in the "
    "cron arguments."
)

_SKILL_FIRST_GUIDANCE = (
    "## Skill-First Workflow\n"
    "When a task clearly matches a discovered Skill above, the first "
    "domain-specific tool call in the execution context must be `skill_view` "
    "for that Skill. Read its instructions before taking any related action. "
    "Until the matching Skill has been read, do not install third-party "
    "packages, create temporary or replacement implementation scripts, or "
    "directly manipulate the domain file format. An available Skill is the "
    "primary supported workflow for its domain.\n"
    "Fall back only when the Skill is unavailable, cannot satisfy the request, "
    "or the user explicitly asks for another implementation. State the reason "
    "before using the fallback. This rule governs domain execution and does "
    "not expand toolsets, path access, network access, or approval authority. "
    "If a required capability is unavailable, report or follow the Skill's "
    "documented fallback; never bypass the boundary.\n"
    "For future, delayed, or recurring work, the existing cron routing remains "
    "first: do not call `skill_view` or begin domain work in the scheduling "
    "turn. Include the exact discovered Skill name in the routed execution's "
    "`skills` input so it is loaded before the work."
)


def _script_workspace_guidance() -> str:
    """给可使用本地工具的会话提供统一的临时脚本落盘位置。"""
    workspace = HERMES_HOME / "scripts"
    workspace.mkdir(parents=True, exist_ok=True)
    return (
        "# Generated Scripts\n"
        f"When creating helper scripts, write them only under `{workspace}` "
        "using an absolute path. Run the same absolute path, and do not "
        "create generated scripts in the project working directory."
    )


def find_project_context(cwd: str) -> str:
    """Find and load the project configuration file by priority."""
    for name in [".hermes.md", "HERMES.md"]:
        path = Path(cwd) / name
        if path.exists():
            return path.read_text(encoding="utf-8")[:20000]

    for name in ["AGENTS.md", "CLAUDE.md", ".cursorrules"]:
        path = Path(cwd) / name
        if path.exists():
            return path.read_text(encoding="utf-8")[:20000]

    return ""


def build_system_prompt(
    cwd: str,
    enabled_toolsets: list[str] | None = None,
    *,
    include_soul: bool = True,
    include_memory: bool = True,
    include_user_profile: bool = True,
    include_project_context: bool = True,
) -> str:
    """按会话能力组装 system prompt；空列表表示显式无工具。"""
    parts = []
    selected_toolsets = (
        None
        if enabled_toolsets is None
        else set(enabled_toolsets)
    )
    no_tool_capabilities = None

    if include_soul:
        soul_path = HERMES_HOME / "SOUL.md"
        if soul_path.exists():
            parts.append(soul_path.read_text(encoding="utf-8")[:20000])
        else:
            parts.append("You are a helpful assistant.")
    else:
        parts.append("You are a helpful assistant.")

    if include_memory or include_user_profile:
        memory_section = render_memory_section(
            include_long=include_memory,
            include_user=include_user_profile,
        )
        if memory_section is not None:
            parts.append(memory_section)

    skill_catalog_enabled = (
        selected_toolsets is None
        or bool({"skill_read", "skill_manage"} & set(selected_toolsets))
    )
    skill_read_enabled = (
        selected_toolsets is None
        or "skill_read" in selected_toolsets
    )
    if skill_catalog_enabled:
        skills_section = render_skills_section()
        if skills_section is not None:
            parts.append(
                (
                    f"{skills_section}\n\n{_SKILL_FIRST_GUIDANCE}"
                    if skill_read_enabled
                    else skills_section
                )
            )

    if selected_toolsets is None:
        # None 保持 CLI 原有完整工具提示。
        parts.append(
            "# Permissions\n"
            "Dangerous commands require user approval."
        )
        parts.append(
            "# Tool Use\n"
            "Prefer the file tool for file content, directory listings, and "
            "file metadata; use terminal for shell commands and processes. "
            "Relative file paths start at the current session cwd shared with "
            "terminal; after terminal changes directory, do not repeat that "
            "directory prefix. Tool-owned state such as memory and skills must "
            "be managed through their dedicated tools, never repaired by "
            "guessing paths in terminal."
        )
        parts.append(_script_workspace_guidance())
        parts.append(_CRON_ROUTING_GUIDANCE)
    elif not selected_toolsets:
        no_tool_capabilities = (
            "# Capabilities\n"
            "This gateway session has no access to local tools, files, "
            "terminals, memory modification, delegation, skills, scheduled "
            "tasks, or external computer control. Answer only from the current "
            "conversation and the read-only context included in this prompt. "
            "Never claim that a local action was executed."
        )
    else:
        tool_lines = []
        if "file" in selected_toolsets:
            tool_lines.append(
                "Use the file tool for file content, directory listings, and "
                "file metadata."
            )
        if "terminal" in selected_toolsets:
            parts.append(
                "# Permissions\n"
                "Dangerous commands require user approval."
            )
            tool_lines.append(
                "Use terminal for shell commands and processes."
            )
        if "memory" in selected_toolsets:
            tool_lines.append(
                "Use the memory tool for persistent memory changes."
            )
        if "skill_read" in selected_toolsets:
            tool_lines.append(
                "Use the skill tools to inspect explicitly available skills."
            )
        if "skill_manage" in selected_toolsets:
            tool_lines.append("Skill management is available through skill_manage.")
        if "delegate" in selected_toolsets:
            tool_lines.append(
                "Delegation is available only through the delegate tools."
            )
        if "cron" in selected_toolsets:
            tool_lines.append(
                "Scheduled tasks are available only through the cron tools."
            )
        if tool_lines:
            parts.append("# Tool Use\n" + "\n".join(tool_lines))
        if {"file", "terminal"} & selected_toolsets:
            parts.append(_script_workspace_guidance())
        if "cron" in selected_toolsets:
            parts.append(_CRON_ROUTING_GUIDANCE)

    if include_project_context:
        project = find_project_context(cwd)
        if project:
            parts.append(f"# Project Context\n{project}")

    environment = f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if selected_toolsets is None or selected_toolsets:
        environment += (
            f"\nWorking directory: {cwd}"
            f"\nHermes home: {HERMES_HOME}"
        )
    parts.append(environment)
    if no_tool_capabilities is not None:
        # 能力边界始终放在全部只读上下文之后，避免前文产生越权暗示。
        parts.append(no_tool_capabilities)

    return "\n\n".join(parts)
