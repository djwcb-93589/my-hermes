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
from hermes.hooks.controls import (
    AddContext,
    Block,
    HookControlDispatchResult,
    build_control_dispatch_result,
    control_error_message,
    control_failure_reason,
    normalize_control_value,
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
            task = asyncio.create_task(
                self._invoke(registration.callback, event.context)
            )
            try:
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=registration.timeout_seconds,
                )
            except asyncio.CancelledError:
                task.cancel()
                self._observe_detached_task(
                    task,
                    event_name=event.name,
                    hook_id=registration.hook_id,
                )
                raise

            if task not in done:
                task.cancel()
                self._observe_detached_task(
                    task,
                    event_name=event.name,
                    hook_id=registration.hook_id,
                )
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
                continue

            try:
                value = task.result()
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    task.cancel()
                    self._observe_detached_task(
                        task,
                        event_name=event.name,
                        hook_id=registration.hook_id,
                    )
                    raise
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type="CancelledError",
                        error_message="hook task was cancelled",
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

    async def emit_control(self, event: HookEvent) -> HookControlDispatchResult:
        """顺序分发控制 Hook；超时、异常和无效返回值都默认阻止。"""
        if not isinstance(event, HookEvent):
            raise TypeError("event must be a HookEvent")
        results: list[HookInvocationResult] = []
        added_context: list[str] = []
        for registration in self._registrations_for(event):
            task = asyncio.create_task(
                self._invoke(registration.callback, event.context)
            )
            try:
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=registration.timeout_seconds,
                )
            except asyncio.CancelledError:
                task.cancel()
                self._observe_detached_task(
                    task,
                    event_name=event.name,
                    hook_id=registration.hook_id,
                )
                raise

            if task not in done:
                task.cancel()
                self._observe_detached_task(
                    task,
                    event_name=event.name,
                    hook_id=registration.hook_id,
                )
                logger.warning(
                    "Control Hook timed out: event=%s hook_id=%s timeout_seconds=%s",
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
                return build_control_dispatch_result(
                    event,
                    results,
                    block_reason=control_failure_reason(),
                    added_context=added_context,
                )

            try:
                value = task.result()
                control_value = normalize_control_value(event, value)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    task.cancel()
                    self._observe_detached_task(
                        task,
                        event_name=event.name,
                        hook_id=registration.hook_id,
                    )
                    raise
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type="CancelledError",
                        error_message="hook task was cancelled",
                    )
                )
                return build_control_dispatch_result(
                    event,
                    results,
                    block_reason=control_failure_reason(),
                    added_context=added_context,
                )
            except Exception as exc:
                logger.exception(
                    "Control Hook failed: event=%s hook_id=%s",
                    event.name,
                    registration.hook_id,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=registration.hook_id,
                        success=False,
                        error_type=type(exc).__name__,
                        error_message=control_error_message(exc),
                    )
                )
                return build_control_dispatch_result(
                    event,
                    results,
                    block_reason=control_failure_reason(),
                    added_context=added_context,
                )

            results.append(
                HookInvocationResult(
                    hook_id=registration.hook_id,
                    success=True,
                    value=control_value,
                )
            )
            if isinstance(control_value, Block):
                return build_control_dispatch_result(
                    event,
                    results,
                    block_reason=control_value.reason,
                    added_context=added_context,
                )
            if isinstance(control_value, AddContext):
                added_context.append(control_value.text)
        return build_control_dispatch_result(
            event,
            results,
            added_context=added_context,
        )

    async def dispatch(
        self,
        event_name: HookName,
        context: HookContext,
    ) -> HookDispatchResult:
        """使用事件名称和上下文分发的异步便捷入口。"""
        return await self.emit(HookEvent(name=event_name, context=context))

    @staticmethod
    def _observe_detached_task(
        task: asyncio.Task[object],
        *,
        event_name: HookName,
        hook_id: str,
    ) -> None:
        """消费超时或外部取消后脱离分发流程的 Task 结果。"""
        def consume_result(completed_task: asyncio.Task[object]) -> None:
            try:
                completed_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "Detached Hook task failed: event=%s hook_id=%s",
                    event_name,
                    hook_id,
                )

        task.add_done_callback(consume_result)

    @staticmethod
    async def _invoke(callback, context: HookContext) -> object:
        """在线程中运行普通函数，并在当前事件循环中等待协程结果。

        同步函数超时时只会停止等待，无法强制终止已经在线程中运行的函数。
        """
        # 超时仅取消当前等待；线程中的同步函数仍可能继续执行。
        value = await asyncio.to_thread(callback, context)
        if inspect.isawaitable(value):
            return await value
        return value
