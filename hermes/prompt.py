"""System prompt assembly."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from hermes.config import HERMES_HOME, MEMORY_CHAR_LIMIT, USER_CHAR_LIMIT
from hermes.tools.memory import (
    load_memory,
    render_entries,
    _current_chars,
    MEMORY_FILE,
    USER_FILE,
)
from hermes.tools.skill import discover_skills


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
) -> str:
    """按会话能力组装 system prompt；空列表表示显式无工具。"""
    parts = []
    selected_toolsets = (
        None
        if enabled_toolsets is None
        else set(enabled_toolsets)
    )
    no_tool_capabilities = None

    soul_path = HERMES_HOME / "SOUL.md"
    if soul_path.exists():
        parts.append(soul_path.read_text(encoding="utf-8")[:20000])
    else:
        parts.append("You are a helpful assistant.")

    memory_entries = load_memory(MEMORY_FILE)
    if memory_entries:
        used = _current_chars(MEMORY_FILE)
        header = f"# Memory ({len(memory_entries)} entries, {used}/{MEMORY_CHAR_LIMIT} chars)"
        parts.append(header + "\n" + render_entries(memory_entries))

    user_entries = load_memory(USER_FILE)
    if user_entries:
        used = _current_chars(USER_FILE)
        header = f"# User Profile ({len(user_entries)} entries, {used}/{USER_CHAR_LIMIT} chars)"
        parts.append(header + "\n" + render_entries(user_entries))

    skill_enabled = (
        selected_toolsets is None
        or "skill" in selected_toolsets
    )
    if skill_enabled:
        skills = discover_skills()
        if skills:
            lines = [
                f"- **{skill['name']}**: {skill['description']}"
                for skill in skills
            ]
            parts.append("# Available Skills\n" + "\n".join(lines))

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
        if "skill" in selected_toolsets:
            tool_lines.append(
                "Use the skill tool for explicitly available skills."
            )
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

    project = find_project_context(cwd)
    if project:
        parts.append(f"# Project Context\n{project}")
    if no_tool_capabilities is not None:
        # 能力边界放在可选项目上下文之后，避免只读上下文产生越权暗示。
        parts.append(no_tool_capabilities)

    environment = f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if selected_toolsets is None or selected_toolsets:
        environment += (
            f"\nWorking directory: {cwd}"
            f"\nHermes home: {HERMES_HOME}"
        )
    parts.append(environment)

    return "\n\n".join(parts)
