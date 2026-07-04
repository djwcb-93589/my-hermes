"""terminal tool: approval check → backend.execute()."""

from __future__ import annotations

import json

from hermes.backends import get_backend
from hermes.security import detect_dangerous_command, approve_command


def run_terminal(args, **kwargs):
    """Terminal tool handler: approval check → backend.execute()."""
    command = args.get("command", "")

    matches = detect_dangerous_command(command)
    if matches and not approve_command(command, matches):
        return json.dumps({"error": "Command denied by user."})

    backend = get_backend()
    result = backend.execute(command)

    output = result["output"]
    if result["returncode"] != 0:
        output += f"\n(exit code: {result['returncode']})"

    return output if output.strip() else "(no output)"


def register(registry):
    registry.register(
        name="terminal",
        toolset="terminal",
        schema={
            "name": "terminal",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
        handler=run_terminal,
    )
