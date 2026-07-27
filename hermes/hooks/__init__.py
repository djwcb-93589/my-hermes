"""与业务流程解耦的通用 Hook 基础设施。"""

from .async_registry import AsyncHookRegistry
from .contracts import (
    HookCallback,
    HookContext,
    HookDispatchResult,
    HookEvent,
    HookInvocationResult,
    HookName,
    HookRegistration,
    HookRegistrationError,
)
from .registry import HookRegistry, SyncHookRegistry


__all__ = [
    "AsyncHookRegistry",
    "HookCallback",
    "HookContext",
    "HookDispatchResult",
    "HookEvent",
    "HookInvocationResult",
    "HookName",
    "HookRegistration",
    "HookRegistrationError",
    "HookRegistry",
    "SyncHookRegistry",
]
