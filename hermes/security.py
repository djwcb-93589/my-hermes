"""
Dangerous command detection and approval.

Match patterns are regex → description tuples. Approval state is per-session
plus a permanent allowlist persisted to HERMES_HOME/allowlist.json.
"""

from __future__ import annotations

import json
import re

from hermes.config import HERMES_HOME


DANGEROUS_PATTERNS = [
    (
        r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|.*--no-preserve-root)",
        "Recursive/force delete",
    ),
    (r"rm\s+-[a-zA-Z]*r", "Recursive delete"),
    (r"mkfs\.", "Filesystem format"),
    (r"dd\s+if=", "Raw disk write"),
    (r">\s*/dev/sd[a-z]", "Direct device write"),
    (r"chmod\s+(-R\s+)?777", "World-writable permissions"),
    (r"chown\s+-R\s+", "Recursive ownership change"),
    (r"shutdown|reboot|poweroff|init\s+[06]", "System shutdown/reboot"),
    (r"kill\s+-9\s+(-1|1\b)", "Kill all processes"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;", "Fork bomb"),
    (r"DROP\s+(TABLE|DATABASE|INDEX)", "SQL destructive"),
    (r"TRUNCATE\s+TABLE", "SQL truncate"),
    (r"DELETE\s+FROM\s+\w+\s*;?\s*$", "SQL delete without WHERE"),
    (r"curl\s+.*\|\s*(bash|sh|zsh)", "Pipe to shell"),
    (r"wget\s+.*\|\s*(bash|sh|zsh)", "Pipe to shell"),
]

_compiled_patterns = [
    (re.compile(pattern, re.IGNORECASE), description)
    for pattern, description in DANGEROUS_PATTERNS
]
_session_approved: set[int] = set()
_ALLOWLIST_FILE = HERMES_HOME / "allowlist.json"


def _load_allowlist() -> set[str]:
    """Load the permanent allowlist."""
    if _ALLOWLIST_FILE.exists():
        try:
            return set(
                json.loads(_ALLOWLIST_FILE.read_text(encoding="utf-8"))
            )
        except Exception:
            pass
    return set()


def _save_allowlist(allowlist: set[str]):
    """Save the permanent allowlist to disk."""
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    _ALLOWLIST_FILE.write_text(
        json.dumps(sorted(allowlist)),
        encoding="utf-8",
    )


_permanent_allowlist: set[str] = _load_allowlist()


def detect_dangerous_command(
    command: str,
) -> list[tuple[int, str, str]]:
    """Detect if a command matches dangerous patterns."""
    matches = []
    for index, (regex, description) in enumerate(_compiled_patterns):
        if regex.search(command):
            matches.append((
                index,
                DANGEROUS_PATTERNS[index][0],
                description,
            ))
    return matches


def approve_command(
    command: str,
    matches: list[tuple[int, str, str]],
) -> bool:
    """Prompt the user to approve a dangerous command."""
    global _permanent_allowlist

    unapproved = [
        (index, pattern_str, description)
        for index, pattern_str, description in matches
        if index not in _session_approved
        and pattern_str not in _permanent_allowlist
    ]
    if not unapproved:
        return True

    print(f"\n  *** DANGEROUS COMMAND ***\n  Command: {command}")
    for _, _, description in unapproved:
        print(f"  - {description}")
    print("  [o]nce / [s]ession / [a]lways / [d]eny")

    choice = input("  Approve? ").strip().lower()

    if choice in ("o", "once"):
        return True

    if choice in ("s", "session"):
        for index, _, _ in unapproved:
            _session_approved.add(index)
        return True

    if choice in ("a", "always"):
        for _, pattern_str, _ in unapproved:
            _permanent_allowlist.add(pattern_str)
        _save_allowlist(_permanent_allowlist)
        for index, _, _ in unapproved:
            _session_approved.add(index)
        return True

    return False
