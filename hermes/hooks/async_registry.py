"""支持超时和同步回调的异步 Hook Registry。"""

from __future__ import annotations

import asyncio
import inspect
import logging

from hermes.hooks.contracts import (
    HookCallback,
    HookContext,
    HookDispatchResult,
    HookEvent,
    HookInvocationResult,
    HookName,
    HookRegistration,
)
from hermes.hooks.registry import _HookRegistryBase, _normalize_timeout


logger = logging.getLogger(__name__)


class AsyncHookRegistry(_HookRegistryBase):
    """按顺序异步分发 Hook，超时或失败不会阻断后续回调。"""

    def __init__(self, *, default_timeout_seconds: float | None = None) -> None:
        super().__init__()
        self._default_timeout_seconds = _normalize_timeout(default_timeout_seconds)

    def register(
        self,
        event_name: HookName,
        callback: HookCallback,
        *,
        hook_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> HookRegistration:
        """注册 Hook，并在未指定时使用 Registry 的默认超时。"""
        effective_timeout = (
            self._default_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        return super().register(
            event_name,
            callback,
            hook_id=hook_id,
            timeout_seconds=effective_timeout,
        )

    async def emit(self, event: HookEvent) -> HookDispatchResult:
        """顺序分发事件；每个 Hook 独立处理超时和异常。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        results: list[HookInvocationResult] = []
        for registration in self._registrations_for(event):
            try:
                invocation = self._invoke(registration.callback, event.context)
                value = (
                    await asyncio.wait_for(
                        invocation,
                        timeout=registration.timeout_seconds,
                    )
                    if registration.timeout_seconds is not None
                    else await invocation
                )
            except TimeoutError:
                logger.warning(
                    "Hook timed out: event=%s hook_id=%s timeout_seconds=%s",
                    event.name,
                    registration.hook_id,
                    registration.timeout_seconds,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type="TimeoutError",
                        timed_out=True,
                        error_message="hook execution timed out",
                    )
                )
            except Exception as exc:
                logger.exception(
                    "Hook failed: event=%s hook_id=%s",
                    event.name,
                    registration.hook_id,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
            else:
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=True,
                        value=value,
                    )
                )
        return HookDispatchResult(event=event, results=tuple(results))

    async def dispatch(
        self,
        event_name: HookName,
        context: HookContext,
    ) -> HookDispatchResult:
        """使用事件名称和上下文分发的异步便捷入口。"""
        return await self.emit(HookEvent(name=event_name, context=context))

    @staticmethod
    async def _invoke(callback, context: HookContext) -> object:
        """在线程中运行普通函数，并在当前事件循环中等待协程结果。"""
        value = await asyncio.to_thread(callback, context)
        if inspect.isawaitable(value):
            return await value
        return value
