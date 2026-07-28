"""将异步 Registry 的控制规则安全快照为同步 Delegate 可用接口。"""

from __future__ import annotations

import inspect
from threading import RLock

from hermes.hooks.async_registry import AsyncHookRegistry
from hermes.hooks.contracts import HookContext
from hermes.hooks.controls import HookControlError
from hermes.hooks.events import HookEventName
from hermes.hooks.registry import SyncHookRegistry


_CONTROL_EVENTS = (
    HookEventName.PRE_LLM_CALL,
    HookEventName.PRE_TOOL_CALL,
)


def _sync_bridge_callback(callback, lock: RLock):
    """包装一个快照回调，拒绝在子线程驱动异步回调或父事件循环。"""
    is_async = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    )

    def invoke(context: HookContext):
        with lock:
            if is_async:
                raise HookControlError(
                    "async control hook cannot execute in a sync delegate"
                )
            value = callback(context)
            if inspect.isawaitable(value):
                close = getattr(value, "close", None)
                if callable(close):
                    close()
                raise HookControlError(
                    "async control hook cannot execute in a sync delegate"
                )
            return value

    return invoke


def build_sync_control_bridge(
    registry: AsyncHookRegistry,
) -> SyncHookRegistry:
    """建立仅含当前控制 Hook 注册快照的线程安全同步 Registry。

    异步回调不能在同步 Delegate 线程中安全执行，会作为控制失败由同步
    Registry 默认阻止；普通同步回调在同一把锁下按原注册顺序运行。
    """
    if not isinstance(registry, AsyncHookRegistry):
        raise TypeError("registry must be an AsyncHookRegistry")
    bridge = SyncHookRegistry()
    lock = RLock()
    for event_name in _CONTROL_EVENTS:
        for registration in registry.registered_hooks(event_name.value):
            bridge.register(
                event_name.value,
                _sync_bridge_callback(registration.callback, lock),
                hook_id=registration.hook_id,
            )
    return bridge
