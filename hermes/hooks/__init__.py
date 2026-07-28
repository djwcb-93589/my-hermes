"""与业务流程解耦的通用 Hook 基础设施。"""

from .async_registry import AsyncHookRegistry
from .controls import (
    AddContext,
    Allow,
    Block,
    HookControlDispatchResult,
    HookControlError,
    ControlHookValue,
)
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
from .events import HookEventName, normalize_hook_event_name
from .observations import (
    build_post_llm_call_payload,
    build_post_tool_call_payload,
    build_run_end_payload,
)
from .registry import HookRegistry, SyncHookRegistry


__all__ = [
    "AsyncHookRegistry",
    "AddContext",
    "Allow",
    "Block",
    "ControlHookValue",
    "HookCallback",
    "HookContext",
    "HookControlDispatchResult",
    "HookControlError",
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
    "normalize_hook_event_name",
]
