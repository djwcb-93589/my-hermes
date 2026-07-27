"""Plugin 显式注册观察型 Hook 所需的最小上下文。"""

from __future__ import annotations

from hermes.hooks import (
    AsyncHookRegistry,
    HookCallback,
    HookEventName,
    HookRegistration,
    HookRegistrationError,
    SyncHookRegistry,
)
from hermes.hooks.events import normalize_observation_event_name


class PluginContext:
    """只包装调用方注入的 Hook Registry，不管理 Plugin 生命周期。"""

    def __init__(
        self,
        hook_registry: SyncHookRegistry | AsyncHookRegistry,
    ) -> None:
        if not isinstance(hook_registry, (SyncHookRegistry, AsyncHookRegistry)):
            raise TypeError(
                "hook_registry must be a SyncHookRegistry or AsyncHookRegistry"
            )
        self._hook_registry = hook_registry

    def register_hook(
        self,
        event_name: HookEventName | str,
        callback: HookCallback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """向固定观察事件注册回调，注册错误统一为 HookRegistrationError。"""
        try:
            normalized_event_name = normalize_observation_event_name(event_name)
        except ValueError as exc:
            raise HookRegistrationError(str(exc)) from exc
        return self._hook_registry.register(
            normalized_event_name,
            callback,
            hook_id=hook_id,
            timeout_seconds=timeout_seconds,
        )


class SyncPluginContext(PluginContext):
    """只接受同步 Registry 的 Plugin 注册上下文。"""

    def __init__(self, hook_registry: SyncHookRegistry) -> None:
        if not isinstance(hook_registry, SyncHookRegistry):
            raise TypeError("hook_registry must be a SyncHookRegistry")
        super().__init__(hook_registry)


class AsyncPluginContext(PluginContext):
    """只接受异步 Registry 的 Plugin 注册上下文。"""

    def __init__(self, hook_registry: AsyncHookRegistry) -> None:
        if not isinstance(hook_registry, AsyncHookRegistry):
            raise TypeError("hook_registry must be an AsyncHookRegistry")
        super().__init__(hook_registry)
