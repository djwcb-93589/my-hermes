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


def build_system_prompt(cwd: str) -> str:
    """Assemble the full system prompt."""
    parts = []

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

    skills = discover_skills()
    if skills:
        lines = [
            f"- **{skill['name']}**: {skill['description']}"
            for skill in skills
        ]
        parts.append("# Available Skills\n" + "\n".join(lines))

    parts.append(
        "# Permissions\n"
        "Dangerous commands require user approval."
    )

    parts.append(
        "# Tool Use\n"
        "Prefer the file tool for file content, directory listings, and file "
        "metadata; use terminal for shell commands and processes. Relative "
        "file paths start at the current session cwd shared with terminal; "
        "after terminal changes directory, do not repeat that directory prefix. "
        "Tool-owned state such as memory and skills must be managed through "
        "their dedicated tools, never repaired by guessing paths in terminal."
    )

    project = find_project_context(cwd)
    if project:
        parts.append(f"# Project Context\n{project}")

    parts.append(
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Working directory: {cwd}\n"
        f"Hermes home: {HERMES_HOME}"
    )

    return "\n\n".join(parts)
