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
from .events import HookEventName
from .observations import (
    build_post_llm_call_payload,
    build_post_tool_call_payload,
    build_run_end_payload,
)
from .registry import HookRegistry, SyncHookRegistry


__all__ = [
    "AsyncHookRegistry",
    "HookCallback",
    "HookContext",
    "HookDispatchResult",
    "HookEvent",
    "HookEventName",
    "HookInvocationResult",
    "HookName",
    "HookRegistration",
    "HookRegistrationError",
    "HookRegistry",
    "SyncHookRegistry",
    "build_post_llm_call_payload",
    "build_post_tool_call_payload",
    "build_run_end_payload",
]
