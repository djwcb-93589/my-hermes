"""供运行时注册和 Dashboard 目录共同使用的轻量工具声明。"""

from hermes.tool_declarations.contracts import (
    ToolDeclaration,
    capability_from_declaration,
    toolsets_from_capabilities,
)


__all__ = [
    "ToolDeclaration",
    "capability_from_declaration",
    "toolsets_from_capabilities",
]
