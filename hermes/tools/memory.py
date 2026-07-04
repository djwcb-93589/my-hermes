"""
Memory tool + storage helpers.

Memory entries live under HERMES_HOME/memories/ as MEMORY.md and USER.md,
delimited by the section mark (§). save_memory trims from the end when over
the configured char limit.
"""

from __future__ import annotations

from pathlib import Path

from hermes.config import (
    HERMES_HOME,
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
)


MEMORY_DIR = HERMES_HOME / "memories"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"
ENTRY_SEP = "\n\n§\n\n"


def parse_entries(text: str) -> list[str]:
    """Split section-mark-delimited text into a list of entries."""
    if not text.strip():
        return []
    return [entry.strip() for entry in text.split("§") if entry.strip()]


def render_entries(entries: list[str]) -> str:
    """Join entries back into section-mark-delimited text."""
    return ENTRY_SEP.join(entries)


def load_memory(file_path: Path) -> list[str]:
    """Load memory entries from a file."""
    if not file_path.exists():
        return []
    return parse_entries(file_path.read_text(encoding="utf-8"))


def save_memory(
    file_path: Path,
    entries: list[str],
    char_limit: int,
) -> str:
    """Save memory entries to a file, trimming if over the char limit."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    text = render_entries(entries)
    warning = ""

    if len(text) > char_limit:
        while entries and len(render_entries(entries)) > char_limit:
            entries.pop()
        text = render_entries(entries)
        warning = f"Trimmed to {len(entries)} entries."

    file_path.write_text(text, encoding="utf-8")
    return warning


def handle_memory(args, **kwargs):
    """Handle the memory tool: add / remove / read operations."""
    action = args.get("action", "")
    target = args.get("target", "memory")
    content = args.get("content", "")

    file_path = USER_FILE if target == "user" else MEMORY_FILE
    char_limit = USER_CHAR_LIMIT if target == "user" else MEMORY_CHAR_LIMIT

    if action == "read":
        entries = load_memory(file_path)
        if not entries:
            return f"({target} is empty)"
        return (
            f"=== {target.upper()} ({len(entries)} entries) ===\n"
            + render_entries(entries)
        )

    elif action == "add":
        if not content:
            return "(error: content required)"
        entries = load_memory(file_path)
        entries.append(content)
        warning = save_memory(file_path, entries, char_limit)
        message = f"Added to {target}. Total: {len(entries)}."
        if warning:
            message += f" {warning}"
        return message

    elif action == "remove":
        if not content:
            return "(error: keyword required)"
        entries = load_memory(file_path)
        before_count = len(entries)
        entries = [
            entry for entry in entries
            if content.lower() not in entry.lower()
        ]
        if before_count == len(entries):
            return f"No match for '{content}'."
        save_memory(file_path, entries, char_limit)
        removed_count = before_count - len(entries)
        return (
            f"Removed {removed_count}. Remaining: {len(entries)}."
        )

    return f"(error: unknown action '{action}')"


def register(registry):
    registry.register(
        name="memory",
        toolset="memory",
        schema={
            "name": "memory",
            "description": (
                "Manage persistent memory. Actions: add/remove/read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "read"],
                    },
                    "target": {
                        "type": "string",
                        "enum": ["memory", "user"],
                    },
                    "content": {
                        "type": "string",
                    },
                },
                "required": ["action"],
            },
        },
        handler=handle_memory,
    )
