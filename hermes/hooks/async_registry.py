"""支持超时和同步回调的异步 Hook Registry。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from weakref import WeakKeyDictionary

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


@dataclass(frozen=True, slots=True)
class _AsyncControlRegistration:
    """控制 Hook 的内部执行句柄，保留注册项及其独立异步锁。"""

    event_name: HookName
    hook_id: str
    callback: HookCallback
    execution_lock: asyncio.Lock


@dataclass(frozen=True, slots=True)
class _AsyncControlSnapshot:
    """供一次控制分发或桥接使用的不可变注册快照。"""

    handle: _AsyncControlRegistration
    timeout_seconds: float | None

    @property
    def hook_id(self) -> str:
        """返回稳定的 Hook 标识。"""
        return self.handle.hook_id

    @property
    def callback(self) -> HookCallback:
        """返回原始注册回调，仅供事件循环内部分发。"""
        return self.handle.callback

    @property
    def event_name(self) -> HookName:
        """返回快照所属的控制事件名称。"""
        return self.handle.event_name


class AsyncHookRegistry(_HookRegistryBase):
    """按顺序异步分发 Hook，超时或失败不会阻断后续回调。"""

    def __init__(self, *, default_timeout_seconds: float | None = None) -> None:
        super().__init__()
        self._default_timeout_seconds = _normalize_timeout(default_timeout_seconds)
        self._control_registrations: WeakKeyDictionary[
            HookRegistration,
            _AsyncControlRegistration,
        ] = WeakKeyDictionary()

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
        registration = super().register(
            event_name,
            callback,
            hook_id=hook_id,
            timeout_seconds=effective_timeout,
        )
        # asyncio.Lock 仅在首次竞争时绑定运行循环；注册项被释放后弱引用表
        # 不会永久保留其锁。控制分发与桥接共用同一个句柄。
        with self._lock:
            self._control_registrations[registration] = _AsyncControlRegistration(
                event_name=registration.event_name,
                hook_id=registration.hook_id,
                callback=registration.callback,
                execution_lock=asyncio.Lock(),
            )
        return registration

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
        return await self._emit_control_snapshot(
            event,
            self._control_snapshot((event.name,)),
        )

    def _control_snapshot(
        self,
        event_names: tuple[HookName, ...],
        *,
        fallback_timeout_seconds: float | None = None,
    ) -> tuple[_AsyncControlSnapshot, ...]:
        """在创建桥接的事件循环中取得控制注册及执行锁的稳定快照。"""
        fallback = _normalize_timeout(fallback_timeout_seconds)
        snapshots: list[_AsyncControlSnapshot] = []
        with self._lock:
            for event_name in event_names:
                for registration in self._registrations.get(event_name, ()):
                    handle = self._control_registrations.get(registration)
                    if handle is None:
                        handle = _AsyncControlRegistration(
                            event_name=registration.event_name,
                            hook_id=registration.hook_id,
                            callback=registration.callback,
                            execution_lock=asyncio.Lock(),
                        )
                        self._control_registrations[registration] = handle
                    timeout = registration.timeout_seconds
                    if timeout is None:
                        timeout = fallback
                    snapshots.append(
                        _AsyncControlSnapshot(
                            handle=handle,
                            timeout_seconds=timeout,
                        )
                    )
        return tuple(snapshots)

    async def _emit_control_snapshot(
        self,
        event: HookEvent,
        snapshots: tuple[_AsyncControlSnapshot, ...],
    ) -> HookControlDispatchResult:
        """在当前事件循环中按快照复用标准控制分发语义。"""
        results: list[HookInvocationResult] = []
        added_context: list[str] = []
        for snapshot in snapshots:
            task = asyncio.create_task(
                self._invoke_control_callback(snapshot, event.context)
            )
            try:
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=snapshot.timeout_seconds,
                )
            except asyncio.CancelledError:
                task.cancel()
                self._observe_detached_task(
                    task,
                    event_name=event.name,
                    hook_id=snapshot.hook_id,
                )
                raise

            if task not in done:
                task.cancel()
                self._observe_detached_task(
                    task,
                    event_name=event.name,
                    hook_id=snapshot.hook_id,
                )
                logger.warning(
                    "Control Hook timed out: event=%s hook_id=%s timeout_seconds=%s",
                    event.name,
                    snapshot.hook_id,
                    snapshot.timeout_seconds,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=snapshot.hook_id,
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
                        hook_id=snapshot.hook_id,
                    )
                    raise
                results.append(
                    HookInvocationResult(
                        hook_id=snapshot.hook_id,
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
                    snapshot.hook_id,
                )
                results.append(
                    HookInvocationResult(
                        hook_id=snapshot.hook_id,
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
                    hook_id=snapshot.hook_id,
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

    async def _invoke_control_callback(
        self,
        snapshot: _AsyncControlSnapshot,
        context: HookContext,
    ) -> object:
        """持有单个注册项锁运行回调，超时后仍避免同一回调并发执行。"""
        handle = snapshot.handle
        await handle.execution_lock.acquire()
        callback_task = asyncio.create_task(
            self._invoke(handle.callback, context)
        )
        release_lock = True
        try:
            return await asyncio.shield(callback_task)
        except asyncio.CancelledError:
            # async def 回调可协作取消；同步函数在工作线程中不能强制停止，
            # 因此保留其 Task 与锁直到真正结束，防止父子运行并发调用同一对象。
            if inspect.iscoroutinefunction(
                handle.callback
            ) or inspect.iscoroutinefunction(
                getattr(handle.callback, "__call__", None)
            ):
                callback_task.cancel()
            release_lock = False
            self._release_control_lock_when_done(handle, callback_task)
            raise
        finally:
            if release_lock:
                handle.execution_lock.release()

    @staticmethod
    def _release_control_lock_when_done(
        handle: _AsyncControlRegistration,
        callback_task: asyncio.Task[object],
    ) -> None:
        """消费延迟回调结果后释放其专属锁，避免遗漏异常或永久占锁。"""
        def release(completed_task: asyncio.Task[object]) -> None:
            try:
                completed_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception(
                    "Detached control Hook task failed: hook_id=%s",
                    handle.hook_id,
                )
            finally:
                if handle.execution_lock.locked():
                    handle.execution_lock.release()

        callback_task.add_done_callback(release)

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
        if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)
        ):
            return await callback(context)
        # 超时仅取消当前等待；线程中的同步函数仍可能继续执行。
        value = await asyncio.to_thread(callback, context)
        if inspect.isawaitable(value):
            return await value
        return value
