"""Plugin 注册 Hook 时使用的受限上下文。"""

from __future__ import annotations

from collections.abc import Callable

from hermes.hooks import (
    HookCallback,
    HookEventName,
    HookRegistration,
    HookRegistrationError,
)
from hermes.hooks.events import normalize_hook_event_name


HookRegistrar = Callable[
    [HookEventName | str, HookCallback, str | None, float | None],
    HookRegistration,
]


class PluginContext:
    """只向 Plugin 暴露注册操作，不暴露底层 Registry。"""

    def __init__(self, registrar: HookRegistrar) -> None:
        if not callable(registrar):
            raise TypeError("registrar must be callable")
        self._registrar = registrar

    def register_hook(
        self,
        event_name: HookEventName | str,
        callback: HookCallback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """向当前 Plugin 的隔离暂存区注册一个固定事件 Hook。"""
        try:
            normalized_event_name = normalize_hook_event_name(event_name)
        except ValueError as exc:
            raise HookRegistrationError(str(exc)) from exc
        return self._registrar(
            normalized_event_name,
            callback,
            hook_id,
            timeout_seconds,
        )


class SyncPluginContext(PluginContext):
    """供同步 Runtime 注入的 Plugin 注册上下文。"""


class AsyncPluginContext(PluginContext):
    """供异步 Runtime 注入的 Plugin 注册上下文。"""
