"""
Tool registry.

ToolRegistry holds (name → ToolEntry) where each entry has a toolset tag,
an OpenAI-format schema, and a handler callable. register_all() imports each
tool module and asks it to register itself — call this once during bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class ToolEntry:
    """A registered tool with its metadata and handler."""
    name: str
    toolset: str
    schema: dict
    handler: Callable


class ToolRegistry:
    """Central registry for all agent tools."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
    ):
        """Register a tool by name with its schema and handler."""
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
        )

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Look up a tool by name and execute its handler."""
        entry = self._tools.get(name)
        if not entry:
            import json
            return json.dumps({"error": f"Unknown tool: {name}"})
        return entry.handler(args, **kwargs)

    def get_definitions(
        self,
        enabled_toolsets: list[str] | None = None,
    ) -> list[dict]:
        """Return tool definitions; ``[]`` explicitly means no tools."""
        if enabled_toolsets is not None and not enabled_toolsets:
            return []

        definitions = []
        for entry in self._tools.values():
            if (
                enabled_toolsets is not None
                and entry.toolset not in enabled_toolsets
            ):
                continue
            definitions.append({
                "type": "function",
                "function": entry.schema,
            })
        return definitions


registry = ToolRegistry()


def register_all():
    """Import every tool module and register it. Idempotent."""
    from hermes.tools.terminal import register as _terminal
    from hermes.tools.file import register as _file
    from hermes.tools.memory import register as _memory
    from hermes.tools.skill import register as _skill
    from hermes.tools.delegate import register as _delegate
    from hermes.cron.tool import register as _cron

    _terminal(registry)
    _file(registry)
    _memory(registry)
    _skill(registry)
    _delegate(registry)
    _cron(registry)
