"""Dashboard 使用的轻量工具声明目录。"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

from hermes.observability.contracts import (
    CapabilityDescriptor,
    ToolsetDescriptor,
)
from hermes.tool_declarations.contracts import (
    ToolDeclaration,
    capability_from_declaration,
    toolsets_from_capabilities,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolsetDeclarationSource:
    """一个可独立失败的轻量 Toolset 声明来源。"""

    name: str
    module_name: str
    toolsets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolsetCatalogSnapshot:
    """应用装配期间生成一次的不可变 Catalog 快照。"""

    capabilities: tuple[CapabilityDescriptor, ...]
    toolsets: tuple[ToolsetDescriptor, ...]


_SOURCES = (
    ToolsetDeclarationSource(
        name="terminal",
        module_name="hermes.tool_declarations.terminal",
        toolsets=("terminal",),
    ),
    ToolsetDeclarationSource(
        name="file",
        module_name="hermes.tool_declarations.file",
        toolsets=("file",),
    ),
    ToolsetDeclarationSource(
        name="memory",
        module_name="hermes.tool_declarations.memory",
        toolsets=("memory",),
    ),
    ToolsetDeclarationSource(
        name="skill",
        module_name="hermes.tool_declarations.skill",
        toolsets=("skill_read", "skill_manage"),
    ),
    ToolsetDeclarationSource(
        name="delegate",
        module_name="hermes.tool_declarations.delegate",
        toolsets=("delegate",),
    ),
    ToolsetDeclarationSource(
        name="messaging",
        module_name="hermes.tool_declarations.messaging",
        toolsets=("messaging",),
    ),
    ToolsetDeclarationSource(
        name="media",
        module_name="hermes.tool_declarations.media",
        toolsets=("media",),
    ),
    ToolsetDeclarationSource(
        name="browser",
        module_name="hermes.tool_declarations.browser",
        toolsets=("browser",),
    ),
    ToolsetDeclarationSource(
        name="cron",
        module_name="hermes.tool_declarations.cron",
        toolsets=("cron",),
    ),
)


def build_toolset_catalog_snapshot() -> ToolsetCatalogSnapshot:
    """按来源隔离加载声明，单个失败不会阻断其他 Toolset。"""
    capabilities: list[CapabilityDescriptor] = []
    unavailable_toolsets: set[str] = set()
    loaded_tool_names: set[str] = set()
    for source in _SOURCES:
        try:
            declarations = _load_source_declarations(source)
            source_tool_names = {
                declaration.name
                for declaration in declarations
            }
            if source_tool_names & loaded_tool_names:
                raise ValueError(
                    "tool declaration source has cross-source name conflicts"
                )
            source_capabilities = tuple(
                capability_from_declaration(declaration)
                for declaration in declarations
            )
            capabilities.extend(source_capabilities)
            loaded_tool_names.update(source_tool_names)
        except Exception as exc:
            unavailable_toolsets.update(source.toolsets)
            logger.warning(
                "Tool declaration catalog failed: stage=load source=%s error_type=%s",
                source.name,
                type(exc).__name__,
            )

    capability_snapshot = tuple(sorted(
        capabilities,
        key=lambda descriptor: (descriptor.toolset, descriptor.name),
    ))
    available_toolsets = {
        descriptor.name: descriptor
        for descriptor in toolsets_from_capabilities(capability_snapshot)
    }
    for name in unavailable_toolsets:
        available_toolsets[name] = ToolsetDescriptor(
            name=name,
            tool_names=(),
            execution_environments=(),
            default_enabled_environments=(),
            available=False,
        )
    return ToolsetCatalogSnapshot(
        capabilities=capability_snapshot,
        toolsets=tuple(
            available_toolsets[name]
            for name in sorted(available_toolsets)
        ),
    )


def _load_source_declarations(
    source: ToolsetDeclarationSource,
) -> tuple[ToolDeclaration, ...]:
    """导入单个轻量来源并确认其声明边界。"""
    module = importlib.import_module(source.module_name)
    declarations = getattr(module, "TOOL_DECLARATIONS")
    if not isinstance(declarations, tuple) or not declarations:
        raise TypeError("tool declarations must be a non-empty tuple")
    if not all(isinstance(item, ToolDeclaration) for item in declarations):
        raise TypeError("tool declarations must contain ToolDeclaration")
    declaration_names = tuple(item.name for item in declarations)
    if len(declaration_names) != len(set(declaration_names)):
        raise ValueError("tool declaration source contains duplicate names")
    declared_toolsets = {item.toolset for item in declarations}
    if declared_toolsets != set(source.toolsets):
        raise ValueError("tool declaration source toolsets do not match")
    return declarations


__all__ = [
    "ToolsetCatalogSnapshot",
    "build_toolset_catalog_snapshot",
]
