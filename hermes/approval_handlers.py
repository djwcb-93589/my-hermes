"""工具专用审批 Binding 的注册表。"""

from __future__ import annotations

from typing import Protocol


class ApprovalHandler(Protocol):
    """只解释本工具 Binding，不参与持久化、队列或实际执行。"""

    def validate_request_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        """校验待审批记录中的 Binding。"""

    def validate_grant_binding(
        self, *, arguments: dict, binding: dict, session_key: str
    ) -> bool:
        """校验签发给工具的可信 Binding。"""


_HANDLERS: dict[str, ApprovalHandler] = {}


def register_approval_handler(tool_name: str, handler: ApprovalHandler) -> None:
    """注册一个工具唯一的审批 Binding 校验器。"""
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("approval handler tool name is invalid")
    if tool_name in _HANDLERS:
        raise ValueError(f"approval handler already registered: {tool_name}")
    if not callable(getattr(handler, "validate_request_binding", None)) or not callable(
        getattr(handler, "validate_grant_binding", None)
    ):
        raise TypeError("approval handler is invalid")
    _HANDLERS[tool_name] = handler


def get_approval_handler(tool_name: str) -> ApprovalHandler | None:
    """返回已注册的工具校验器；未注册工具不能进入审批链。"""
    return _HANDLERS.get(tool_name)
